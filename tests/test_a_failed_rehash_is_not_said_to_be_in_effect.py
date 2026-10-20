"""#1434: a settings save whose rehash failed is not said to be in effect.

A save from the console (`set` / `setcommit`, which is what dccore.mrc's
settings window sends) writes settings.conf and then rehashes. The rehash
catches a failed reload itself - an admin_config.py left with a syntax
error, for one - prints "[REHASH CRITICAL ERROR]" and used to return like a
success, so the console always sent `DCCORE SETAPPLIED` and mIRC said the
saved settings were in effect.

Now the rehash returns whether its reload worked, the save hands that to
`on_applied(ok)`, and the console sends `DCCORE SETAPPLYERR <message>` (or a
sentence, to a person) instead of `SETAPPLIED`. dccore.mrc awaits the new
line as it awaits SETAPPLIED, and says it in @DCCore when the window is not
waiting for it.

The rehash runs here in-process with its reload stubbed, as
tests/test_rehash_lock_and_queue_safety.py does: a real importlib.reload()
of the daemon's modules mid-suite would reset state the rest of the run
stands on. dccore.mrc cannot run here, so it is read as source, with its
comment lines dropped, and the two routing decisions this change makes are
evaluated from the script's own conditions rather than matched as text.
"""

import contextlib
import io
import os
import re
import sys
import threading
import time
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _path in (REPO_ROOT, os.path.join(REPO_ROOT, "tests")):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import announce  # noqa: E402
import commands  # noqa: E402
import webserver  # noqa: E402

from tests.support import DCCoreTestCase, install_fake_oserve, silence_debug  # noqa: E402
from tests.test_console_settings_commands import _real_apply, make_session, run  # noqa: E402
from tests.test_the_mirc_settings_window import (  # noqa: E402
    alias, block, dialog, literal_commas, statements, var_statements, wrapped_lines)

ADMIN_CONSOLE_DOC = os.path.join(REPO_ROOT, "docs", "ADMIN-CONSOLE.md")

# What a half-finished edit of admin_config.py makes the reload raise.
BROKEN_ADMIN_CONFIG = SyntaxError("invalid syntax", ("admin_config.py", 3, 17, "MAX_DCC_SLOTS = (\n"))


def broken_reload(*_args, **_kwargs):
    raise BROKEN_ADMIN_CONFIG


def harmless_reload(*_args, **_kwargs):
    return []


class RehashCase(DCCoreTestCase):
    """The real rehash, with only its module reload stood in for."""

    def setUp(self):
        super().setUp()
        self.set_config(NICKNAME="ExampleBot", CHANNEL="#example", ADMIN_NICK="operator")
        self.debug = silence_debug(announce)
        self.addCleanup(setattr, commands, "reload_modules_in_order", commands.reload_modules_in_order)
        self.reloads = []

    def reload_with(self, stub):
        def record(*args, **kwargs):
            self.reloads.append(args)
            return stub(*args, **kwargs)
        commands.reload_modules_in_order = record

    def rehash(self, user="operator", **kwargs):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            result = commands.handle_rehash_request(user, "#example", **kwargs)
        return result, out.getvalue()


class TheRehashSaysWhetherItWorked(RehashCase):

    def test_a_reload_that_raised_is_false(self):
        self.reload_with(broken_reload)
        result, out = self.rehash(authorised=True)
        self.assertIs(result, False)
        self.assertEqual(len(self.reloads), 1)
        self.assertIn("[REHASH CRITICAL ERROR]", out)
        self.assertTrue(any("Rehash FAILED" in text for _cat, text in self.debug), self.debug)

    def test_a_reload_that_went_through_is_true(self):
        self.reload_with(harmless_reload)
        result, out = self.rehash(authorised=True)
        self.assertIs(result, True)
        self.assertEqual(len(self.reloads), 1)
        self.assertIn("[REHASH SUCCESS]", out)
        self.assertNotIn("[REHASH CRITICAL ERROR]", out)

    def test_a_channel_sync_that_fails_after_the_reload_is_still_true(self):
        # The sync runs only with a socket to sync over.
        install_fake_oserve(irc_connection=object())
        self.addCleanup(sys.modules.pop, "oserve", None)
        self.addCleanup(setattr, commands, "sync_channels", commands.sync_channels)

        def failing_sync(*_args, **_kwargs):
            raise RuntimeError("example sync failure")
        commands.sync_channels = failing_sync
        self.reload_with(harmless_reload)
        result, out = self.rehash(authorised=True)
        self.assertIn("[REHASH SYNC ERROR]", out)       # the sync did fail, here
        self.assertIs(result, True)
        self.assertNotIn("[REHASH CRITICAL ERROR]", out)

    def test_an_unauthorised_caller_is_false_and_reloads_nothing(self):
        self.reload_with(harmless_reload)
        result, out = self.rehash(user="stranger")
        self.assertIs(result, False)
        self.assertEqual(self.reloads, [])
        self.assertIn("[REHASH SECURITY]", out)

    def test_the_admin_nick_unflagged_gets_the_bodys_answer(self):
        self.reload_with(broken_reload)
        self.assertIs(self.rehash(user="operator")[0], False)
        self.reload_with(harmless_reload)
        self.assertIs(self.rehash(user="operator")[0], True)


class TheOuterHandlerPassesTheAnswerThrough(DCCoreTestCase):

    def setUp(self):
        super().setUp()
        self.addCleanup(setattr, commands, "_handle_rehash_request", commands._handle_rehash_request)

    def test_true_and_false_both_come_back(self):
        for answer in (True, False):
            commands._handle_rehash_request = lambda *a, _answer=answer, **kw: _answer
            with self.subTest(answer=answer):
                self.assertIs(commands.handle_rehash_request("someone", "#example", authorised=True), answer)

    def test_the_lock_is_released_after_a_failure(self):
        import runtime
        commands._handle_rehash_request = lambda *a, **kw: False
        commands.handle_rehash_request("someone", "#example", authorised=True)
        self.assertTrue(runtime.rehash_lock.acquire(blocking=False))
        runtime.rehash_lock.release()


# ---------------------------------------------------------------------------
# The save: on_applied(ok)
# ---------------------------------------------------------------------------

class TheSaveSaysWhetherItApplied(DCCoreTestCase):
    """apply_settings_changes(on_applied=...) - the console's path - with the
    rehash stood in for by one that answers as told."""

    def setUp(self):
        super().setUp()
        self.addCleanup(setattr, commands, "handle_rehash_request", commands.handle_rehash_request)
        self.addCleanup(setattr, webserver, "apply_settings_changes", webserver.apply_settings_changes)
        webserver.apply_settings_changes = _real_apply
        self.calls = []
        self.told = threading.Event()

    def on_applied(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        self.told.set()

    def save_with(self, rehash):
        commands.handle_rehash_request = rehash
        with contextlib.redirect_stdout(io.StringIO()):
            status, result = webserver.apply_settings_changes(
                {"MAX_DCC_SLOTS": "7"}, on_applied=self.on_applied)
        self.assertEqual(status, 200, result)
        self.assertTrue(self.told.wait(10), "on_applied was never called")
        return self.calls

    def test_true_false_and_none_from_the_rehash(self):
        # Fails closed: only an explicit True is "in effect". A None - a
        # bare `return` somewhere in the rehash - is not.
        for answer, ok in ((True, True), (False, False), (None, False)):
            self.calls = []
            self.told.clear()
            with self.subTest(answer=answer):
                calls = self.save_with(lambda *a, _answer=answer, **kw: _answer)
                self.assertEqual(calls, [((ok,), {})])

    def test_a_rehash_that_raised_is_a_failure(self):
        raised = threading.Event()
        seen = []

        def hook(args):
            seen.append(args.exc_type)
            raised.set()
        self.addCleanup(setattr, threading, "excepthook", threading.excepthook)
        threading.excepthook = hook

        def rehash(*_args, **_kwargs):
            raise RuntimeError("example rehash crash")
        calls = self.save_with(rehash)
        self.assertEqual(calls, [((False,), {})])
        self.assertTrue(raised.wait(10))
        self.assertEqual(seen, [RuntimeError])      # told, and not swallowed either


# ---------------------------------------------------------------------------
# The console: SETAPPLYERR instead of SETAPPLIED
# ---------------------------------------------------------------------------

class ConsoleCase(DCCoreTestCase):
    """The real console path (adminchat -> console_settings -> the real
    save), with the rehash it starts recorded so it can be joined before
    tearDown puts the daemon's functions back."""

    def setUp(self):
        super().setUp()
        self.addCleanup(setattr, webserver, "apply_settings_changes", webserver.apply_settings_changes)
        webserver.apply_settings_changes = _real_apply
        self.addCleanup(setattr, commands, "handle_rehash_request", commands.handle_rehash_request)
        self.addCleanup(setattr, commands, "reload_modules_in_order", commands.reload_modules_in_order)
        self.set_config(MAX_DCC_SLOTS=2, NICKNAME="ExampleBot", CHANNEL="#example", ADMIN_NICK="operator")
        self.debug = silence_debug(announce)
        self.release = threading.Event()
        self.threads = []
        # The rehash thread prints after run() has stopped capturing.
        self.quiet = contextlib.redirect_stdout(io.StringIO())
        self.quiet.__enter__()

    def tearDown(self):
        self.release.set()
        for thread in self.threads:
            thread.join(10)
        self.quiet.__exit__(None, None, None)
        super().tearDown()

    def rehash_answers(self, answer, held=False):
        """The rehash stood in for: returns `answer`, after `self.release`
        when `held`."""
        def rehash(*_args, **_kwargs):
            self.threads.append(threading.current_thread())
            if held:
                self.release.wait(10)
            return answer
        commands.handle_rehash_request = rehash

    def real_rehash_with(self, reload):
        """The real rehash, its reload replaced by `reload`."""
        real = commands.handle_rehash_request

        def rehash(*args, **kwargs):
            self.threads.append(threading.current_thread())
            return real(*args, **kwargs)
        commands.handle_rehash_request = rehash
        commands.reload_modules_in_order = reload

    def wait_for(self, session, wanted, limit=10.0):
        """The first line `wanted(line)` accepts, once one has been sent."""
        deadline = time.monotonic() + limit
        while time.monotonic() < deadline:
            found = [line for line in list(session.sent) if wanted(line)]
            if found:
                return found[0]
            time.sleep(0.01)
        return None

    def finished(self):
        for thread in self.threads:
            thread.join(10)
            self.assertFalse(thread.is_alive())


def is_applyerr(line):
    return line.startswith("DCCORE SETAPPLYERR ")


def is_applied(line):
    return line == "DCCORE SETAPPLIED" or line.startswith("DCCORE SETAPPLIED ")


class AStructuredSessionHearsTheFailure(ConsoleCase):

    def setUp(self):
        super().setUp()
        self.session = make_session(self)

    def assert_failure_after(self, done):
        line = self.wait_for(self.session, is_applyerr)
        self.assertIsNotNone(line, self.session.sent)
        self.finished()
        sent = list(self.session.sent)
        self.assertFalse([x for x in sent if is_applied(x)], sent)
        self.assertLess(sent.index(done), sent.index(line))
        return line

    def test_a_failing_rehash_sends_setapplyerr_after_setdone(self):
        self.rehash_answers(False, held=True)
        reply = run(self.session, "set MAX_DCC_SLOTS 4")
        self.assertTrue(reply[0].startswith("DCCORE SETDONE ok 1 0 "), reply)
        self.assertFalse([x for x in self.session.sent if is_applyerr(x)])   # still running
        self.release.set()
        self.assert_failure_after(reply[0])

    def test_a_fast_failing_rehash_still_reports_after_setdone(self):
        self.release.set()
        self.rehash_answers(False)
        reply = run(self.session, "set MAX_DCC_SLOTS 4")
        self.assert_failure_after(reply[0])

    def test_a_transaction_commit_too(self):
        self.rehash_answers(False)
        self.assertEqual(run(self.session, "setbegin")[0].split()[1], "SETOPEN")
        run(self.session, "set MAX_DCC_SLOTS 4")
        reply = run(self.session, "setcommit")
        self.assertTrue(reply[0].startswith("DCCORE SETDONE ok 1 0 "), reply)
        self.assert_failure_after(reply[0])

    def test_the_line_is_a_type_an_older_script_does_not_read_as_success(self):
        # A dccore.mrc from before #1434 reads every SETAPPLIED as success,
        # whatever follows - so the failure must not be a SETAPPLIED field.
        self.rehash_answers(False)
        reply = run(self.session, "set MAX_DCC_SLOTS 4")
        line = self.assert_failure_after(reply[0])
        self.assertEqual(line.split()[1], "SETAPPLYERR")
        message = line.split(" ", 2)[2]
        self.assertTrue(message.strip())
        self.assertNotIn("in effect", message)
        self.assertNotIn("\n", line)

    def test_the_real_rehash_with_a_broken_admin_config(self):
        # The defect as reported: the reload raises, the rehash catches it.
        self.real_rehash_with(broken_reload)
        reply = run(self.session, "set MAX_DCC_SLOTS 4")
        self.assertTrue(reply[0].startswith("DCCORE SETDONE ok 1 0 "), reply)
        self.assert_failure_after(reply[0])
        # the real failure handler ran
        self.assertTrue(any("Rehash FAILED" in text for _cat, text in self.debug), self.debug)

    def test_the_real_rehash_that_worked_still_says_setapplied(self):
        self.real_rehash_with(harmless_reload)
        reply = run(self.session, "set MAX_DCC_SLOTS 4")
        self.assertIsNotNone(self.wait_for(self.session, is_applied), self.session.sent)
        self.finished()
        sent = list(self.session.sent)
        self.assertIn("DCCORE SETAPPLIED", sent)
        self.assertFalse([x for x in sent if is_applyerr(x)], sent)
        self.assertLess(sent.index(reply[0]), sent.index("DCCORE SETAPPLIED"))

    def test_a_rehash_that_worked_says_setapplied_and_nothing_else(self):
        self.rehash_answers(True)
        run(self.session, "set MAX_DCC_SLOTS 4")
        self.assertIsNotNone(self.wait_for(self.session, is_applied))
        self.finished()
        self.assertFalse([x for x in self.session.sent if is_applyerr(x)])


class APersonHearsItInASentence(ConsoleCase):

    def setUp(self):
        super().setUp()
        self.session = make_session(self, structured=False)

    def after_the_save(self, answer):
        self.rehash_answers(answer)
        reply = run(self.session, "set MAX_DCC_SLOTS 4")
        self.assertEqual(len(reply), 1, reply)
        line = self.wait_for(self.session, lambda x: x not in reply)
        self.assertIsNotNone(line, self.session.sent)
        self.finished()
        self.assertEqual(self.session.sent, reply + [line])
        return line

    def test_a_failure_is_said_and_not_called_in_effect(self):
        line = self.after_the_save(False)
        self.assertFalse(line.startswith("DCCORE"), line)
        self.assertNotIn("in effect", line)
        self.assertIn("settings.conf", line)
        self.assertIn("failed", line)

    def test_a_success_is_still_in_effect(self):
        line = self.after_the_save(True)
        self.assertIn("in effect", line)


# ---------------------------------------------------------------------------
# dccore.mrc
# ---------------------------------------------------------------------------

def _if_split(statement):
    """`if (...) { ... }` as (condition, body): the condition from after
    "if " to the brace that opens the body, and the body inside that brace -
    None when the block goes on over the lines below. Anything after the
    closing brace (an `else`) is an error, so it is never skipped unread."""
    assert statement.startswith("if "), statement
    text, depth = statement[3:], 0
    for i, ch in enumerate(text):
        depth += ch == "("
        depth -= ch == ")"
        if depth == 0 and text[i:i + 2] == " {":
            condition, rest = text[:i].strip(), text[i + 1:].strip()
            break
    else:
        raise AssertionError(statement)
    if rest == "{":
        return condition, None
    depth = 0
    for j, ch in enumerate(rest):
        depth += ch == "{"
        depth -= ch == "}"
        if depth == 0:
            if rest[j + 1:].strip():
                raise AssertionError("not understood after the block: %r" % statement)
            return condition, rest[1:j].strip()
    raise AssertionError(statement)


def _condition(statement):
    """The condition of `if (...) ... {`: from after "if " to the brace that
    opens the body."""
    return _if_split(statement)[0]


def _pieces(body):
    """The statements of a one-line body, split at its own `|`s - not those
    inside a nested block."""
    found, depth, start = [], 0, 0
    for i, ch in enumerate(body):
        depth += ch == "{"
        depth -= ch == "}"
        if depth == 0 and body[i:i + 3] == " | ":
            found.append(body[start:i].strip())
            start = i + 3
    found.append(body[start:].strip())
    return [piece for piece in found if piece]


def _run_body(body, atoms, ran):
    """Run a one-line body: the statements it carries out go to `ran`, a
    nested inline `if` is evaluated with `atoms`. True once a `return` is
    reached."""
    for piece in _pieces(body):
        if piece == "return":
            return True
        if piece.startswith(("else", "elseif")):
            raise AssertionError("not understood: %r" % piece)
        if piece.startswith("if "):
            condition, inner = _if_split(piece)
            if inner is None:
                raise AssertionError("not understood: %r" % piece)
            if _evaluate(condition, atoms) and _run_body(inner, atoms, ran):
                return True
            continue
        ran.append(piece)
    return False


def _evaluate(condition, atoms):
    """An mSL condition made of the atoms in `atoms` ({regex: callable(match)
    -> bool}; an atom's own groups are named), `!`, `&&`, `||` and
    parentheses, evaluated. Anything else in it is an error, so a condition
    this does not understand is never guessed."""
    expr = condition
    for pattern, value in atoms.items():
        expr = re.sub(r"(?P<neg>!?)" + pattern,
                      lambda m, _value=value: str(bool(_value(m)) != bool(m.group("neg"))), expr)
    expr = expr.replace("&&", " and ").replace("||", " or ")
    if not re.fullmatch(r"[\s()]*(?:(?:True|False|and|or)[\s()]*)*", expr):
        raise AssertionError("not understood: %r -> %r" % (condition, expr))
    return eval(expr, {"__builtins__": {}}, {})


def top_level(name):
    """The statements of an alias at its own level - not those inside one
    of its blocks, which run only when that block's condition held."""
    return [line.strip() for line in alias(name).split("\n")[1:-1]
            if re.match(r"  \S", line)]


def sw_types():
    return statements(alias("dccore.sw.types"))[0].split()[1:]


def handling(kind, wants, tail, line=None, dialog=False):
    """What `alias dccore.structured` does with a line of type `kind`, the
    window waiting for it (`wants`) or in its tail, and open (`dialog`) or
    not: [(statement, ran)] for
    each statement of the alias, in order, that carried something out, up to
    the one whose `return` ended it. `ran` is what that statement carried
    out; None for a block over lines of its own, which is taken as the end.

    `line` is the line as the alias gets it ($1-, without "DCCORE "), for a
    condition that reads its fields ($2 ...); a field the line does not have
    is $null, as in mIRC."""
    fields = (line if line is not None else kind).split(" ")

    def field(m):
        number = int(m.group("n"))
        return fields[number - 1] if number <= len(fields) else ""

    def field_in(m):
        return field(m).lower() in [word.lower() for word in m.group("a").split()]   # $istok ignores case

    atoms = {
        r"%type == (?P<a>\w+)": lambda m: m.group("a") == kind,
        r"\$istok\(\$dccore\.sw\.types,%type,32\)": lambda m: kind in sw_types(),
        r"\$istok\((?P<a>[\w ]+),\$(?P<n>\d),32\)": field_in,
        r"\$(?P<n>\d) == (?P<a>\w+)": lambda m: field(m).lower() == m.group("a").lower(),   # == ignores case
        r"\$dialog\(dccore\.set\)": lambda m: dialog,
        r"\$dccore\.sw\.wants\(%type\)": lambda m: wants,
        r"\$dccore\.st\(sw\.tail\)": lambda m: tail,
    }
    path = []
    for statement in top_level("dccore.structured"):
        if statement == "}" or statement.startswith("var "):
            continue
        if not statement.startswith("if "):
            path.append((statement, [statement]))       # the catch-all at the end
            continue
        condition, body = _if_split(statement)
        if not _evaluate(condition, atoms):
            continue
        if body is None:
            path.append((statement, None))
            return path
        ran = []
        returned = _run_body(body, atoms, ran)
        if ran or returned:
            path.append((statement, ran))
        if returned:
            return path
    return path


def route(kind, wants, tail, line=None, dialog=False):
    """The statement of `alias dccore.structured` a line of type `kind` is
    handled by - the one whose `return` ends it - the window waiting for it
    (`wants`) or in its tail."""
    path = handling(kind, wants, tail, line, dialog)
    return path[-1][0] if path else None


def wants(kind, applying):
    """What `alias dccore.sw.wants` answers for a line of type `kind`, the
    window open and its save applying (or not)."""
    atoms = {
        r"%t == (?P<a>\w+)": lambda m: m.group("a") == kind,
        r"\$istok\((?P<a>[A-Z ]+),%t,32\)": lambda m: kind in m.group("a").split(),
        r"\$dccore\.sw\.s\(applying\) != \$null": lambda m: applying,
    }
    body = top_level("dccore.sw.wants")
    # the window is open: the first statement's early "return $false" is not taken
    assert body[0].startswith("if (!$dialog(dccore.set))"), body[0]
    for statement in body[1:]:
        if statement.startswith("var ") or statement == "}":
            continue
        if statement == "return $false":
            return False
        returns = re.fullmatch(r"if (\(.*\)) \{ return \$iif\((.*),\$true,\$false\) \}", statement)
        if returns is None:
            # a block of its own (LOCKED's): fine as long as it is not this kind's
            if _evaluate(_condition(statement), atoms):
                raise AssertionError("%s is answered by a block this does not read" % kind)
            continue
        if _evaluate(returns.group(1), atoms):
            return _evaluate("(" + returns.group(2) + ")", atoms)
    raise AssertionError("dccore.sw.wants has no final return $false")


def applyerr_block():
    return block("if (%t == SETAPPLYERR) {", alias("dccore.sw.line"))


class TheScriptAwaitsTheNewLine(unittest.TestCase):

    def test_it_is_one_of_the_windows_types(self):
        self.assertIn("SETAPPLYERR", sw_types())

    def test_the_window_waits_for_it_while_a_save_applies(self):
        self.assertIs(wants("SETAPPLYERR", applying=True), True)
        self.assertIs(wants("SETAPPLYERR", applying=False), False)
        # the reader is right about the line it sits beside
        self.assertIs(wants("SETAPPLIED", applying=True), True)
        self.assertIs(wants("SETAPPLIED", applying=False), False)

    def test_awaited_it_goes_to_the_window(self):
        statement = route("SETAPPLYERR", wants=True, tail=False)
        self.assertIsNotNone(statement)
        self.assertIn("dccore.sw.line $1-", statement)

    def test_not_awaited_it_is_an_alert_in_dccore_even_in_the_windows_tail(self):
        for tail in (False, True):
            statement = route("SETAPPLYERR", wants=False, tail=tail)
            with self.subTest(tail=tail):
                self.assertIsNotNone(statement)
                self.assertNotIn("dccore.sw.line", statement)
                self.assertIn("dccore.alert ", statement)
                self.assertIn("$2-", statement)              # the bot's sentence
                self.assertNotIn("in effect", statement)
                self.assertTrue(statement.endswith("| return }"), statement)

    def test_the_router_reads_the_other_lines_as_before(self):
        # A control for route(): SETAPPLIED out of the window is still "in effect".
        self.assertIn("The saved settings are in effect now.", route("SETAPPLIED", wants=False, tail=False))
        self.assertIn("dccore.sw.line $1-", route("SETAPPLIED", wants=True, tail=False))


class TheWindowSaysSavedButNotApplied(unittest.TestCase):

    def test_it_ends_the_wait_and_the_applying_state(self):
        body = statements(applyerr_block())
        self.assertIn("dccore.sw.waited applied", body)
        self.assertIn("hdel dccore.sws applying", body)
        self.assertEqual(body[-1], "return")

    def test_it_reads_the_count_before_it_forgets_it(self):
        body = statements(applyerr_block())
        said = [i for i, s in enumerate(body) if s.startswith("var %said = ")]
        self.assertEqual(len(said), 1, body)
        self.assertIn("$dccore.sw.s(applying)", body[said[0]])
        self.assertLess(said[0], body.index("hdel dccore.sws applying"))

    def test_the_bots_sentence_goes_to_dccore_whole_not_to_the_status(self):
        body = statements(applyerr_block())
        alerts = [s for s in body if s.startswith("dccore.alert ")]
        self.assertEqual(len(alerts), 1, body)
        self.assertRegex(alerts[0], r" \$2- ")                  # the bot's sentence, whole
        self.assertIn("settings.conf", alerts[0])
        self.assertNotIn("in effect", alerts[0])
        for statement in body:
            if statement.startswith(("var %said = ", "dccore.sw.status ")):
                self.assertNotIn("$2", statement)

    def test_the_status_fits_its_one_line_control(self):
        # dccore.sw.status writes control 1018 of `dialog dccore.set`: a
        # one-line text, 310 dialog units (465 px of Tahoma 8pt) wide. A
        # longer sentence is cut off at its edge, mid-word.
        body = statements(applyerr_block())
        said = [s for s in body if s.startswith("var %said = ")][0][len("var %said = "):]
        status = [s for s in body if s.startswith("dccore.sw.status ")][0][len("dccore.sw.status "):]
        text = status.replace("%said", said).replace("$dccore.sw.s(applying)", "12")   # a two-digit count
        self.assertNotRegex(text, r"[$%]\w", text)              # nothing left that is not literal
        self.assertLessEqual(len(text), 80, text)
        control = [c for c in dialog("dccore.set") if c[2] == 1018]
        self.assertEqual(len(control), 1)
        kind, _label, _cid, _x, _y, width, height, _style = control[0]
        self.assertEqual((kind, width, height), ("text", 310, 10))
        self.assertEqual(wrapped_lines(text, width), 1, text)

    def test_it_does_not_reload_the_page_nor_claim_success(self):
        body = statements(applyerr_block())
        for statement in body:
            with self.subTest(statement=statement):
                self.assertFalse(re.search(r"(^|\| )dccore\.sw\.load\b", statement), statement)
                self.assertNotIn("in effect", statement)
                self.assertNotIn("and applied", statement)
                self.assertFalse(statement.startswith("hadd dccore.sws after"), statement)
        status = [s for s in body if s.startswith("dccore.sw.status ")]
        self.assertEqual(len(status), 1, body)
        self.assertIn("%said", status[0])
        said = [s for s in body if s.startswith("var %said = ")][0]
        self.assertIn("could not apply", said)


class TheNewVarHoldsNoLiteralComma(unittest.TestCase):
    """mIRC's /var reads a literal comma as the start of another variable.
    The script-wide check, and the proof it bites, are in
    tests/test_the_mirc_settings_window.py (NoVarHoldsALiteralComma)."""

    def test_the_message_has_none(self):
        found = var_statements(applyerr_block())
        self.assertEqual(len(found), 1, found)
        self.assertEqual(literal_commas(found[0]), [], found[0])


class TheProtocolIsDocumented(unittest.TestCase):

    def test_admin_console_md_has_a_row_for_it(self):
        with io.open(ADMIN_CONSOLE_DOC, encoding="utf-8") as handle:
            rows = [line for line in handle.read().split("\n") if line.startswith("| `DCCORE SETAPPLYERR")]
        self.assertEqual(len(rows), 1, rows)
        self.assertTrue(rows[0].startswith("| `DCCORE SETAPPLYERR <message>` |"), rows[0])
        self.assertIn("instead of `SETAPPLIED`", rows[0])


if __name__ == "__main__":
    unittest.main()
