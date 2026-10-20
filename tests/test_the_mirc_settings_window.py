"""#1264: dccore.mrc's settings window - the bot's settings, edited from mIRC.

`/dccore settings` opens `dialog dccore.set`: six tabs, the pages of the
chosen tab as a column of buttons, Apply / OK / Cancel. It speaks the console protocol
docs/ADMIN-CONSOLE.md defines under "Settings over the console".

mIRC cannot run here, so the script is read as source. Comment lines are
dropped first (a ";" starts a comment only at the start of a line): every
statement asserted on has to be code, not a sentence about it. The dialog
table and its lookup data are generated (tests/test_the_settings_window_
generator.py); this file checks the table's geometry and the hand-written
mSL around it.

Mutation checks (each run by hand against this file, see the PR): removing
a reply handler, breaking the value decoder, dropping a snapshot's count
check, and sending a `set` outside setbegin ... setcommit each fail a test
here.
"""

import io
import os
import re
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(REPO_ROOT, "scripts", "mirc", "dccore.mrc")
for path in (REPO_ROOT, os.path.join(REPO_ROOT, "src"), os.path.join(REPO_ROOT, "tests"),
             os.path.join(REPO_ROOT, "scripts", "mirc")):
    if path not in sys.path:
        sys.path.insert(0, path)

import adminchat  # noqa: E402
import console_settings  # noqa: E402
from tests.support import without_menu_icons  # noqa: E402
from test_the_options_dialog_labels_fit_their_controls import CHAR_PX, CHECK_BOX_PX, PX_PER_DBU  # noqa: E402

BEGIN = "; ==== BEGIN GENERATED settings window"
END = "; ==== END GENERATED settings window"


def raw():
    with io.open(SCRIPT, encoding="ascii", newline="") as handle:
        return without_menu_icons(handle.read().replace("\r\n", "\n"))


def code(text=None):
    """The script (or `text`) with its comment lines removed."""
    text = raw() if text is None else text
    return "\n".join(line for line in text.split("\n") if not line.lstrip().startswith(";"))


def block(header, source=None):
    """The block that starts with `header` (ending in "{"), braces matched."""
    source = code() if source is None else source
    start = source.index(header)
    depth = 0
    for j in range(source.index("{", start), len(source)):
        depth += source[j] == "{"
        depth -= source[j] == "}"
        if depth == 0:
            return source[start:j + 1]
    raise AssertionError(header)


def alias(name):
    return block("alias %s {" % name)


def statements(body):
    """The statements of a block, without its header and closing brace - a
    one-line block's single statement too."""
    if "\n" not in body.strip():
        return [body[body.index("{") + 1:body.rindex("}")].strip()]
    return [line.strip() for line in body.split("\n")[1:-1] if line.strip()]


def handwritten():
    """The settings window's own mSL: its section, without the generated block."""
    text = raw()
    start = text.index(";  Settings window (#1264)")
    end = text.index(BEGIN)
    return code(text[start:end])


def generated_data():
    """{item: value} of the generated `alias dccore.sw.data`."""
    data = {}
    for line in alias("dccore.sw.data").split("\n"):
        line = line.strip()
        if line.startswith("hadd dccore.swm "):
            parts = line.split(" ", 3)
            data[parts[2]] = parts[3] if len(parts) > 3 else ""
    return data


def untext(text):
    return re.sub(r"~([0-9A-F][0-9A-F])", lambda m: chr(int(m.group(1), 16)), text)


CONTROL = re.compile(
    r'^\s+(check|text|button|box|edit|combo|list|tab|radio|scroll) (?:"([^"]*)", )?(\d+)(?:, (\d+) (\d+) (\d+) (\d+))?(?:, (.*))?$',
    re.M)


def dialog(name):
    """(kind, label, id, x, y, w, h, style) for every control of a dialog."""
    table = block("dialog %s {" % name)
    return [(k, (label or "").replace("&&", "&"), int(i), int(x or 0), int(y or 0), int(w or 0), int(h or 0),
             style or "")
            for k, label, i, x, y, w, h, style in CONTROL.findall(table)]


def ids_of(value):
    return [int(i) for i in value.split(",") if i]


def page_ids(data):
    pages = {}
    for page in range(1, int(data["pages"]) + 1):
        ids = []
        for part in range(1, int(data["p.%d.n" % page]) + 1):
            ids.extend(ids_of(data["p.%d.%d" % (page, part)]))
        pages[page] = ids
    return pages


def slot_ids():
    """The page buttons: one per slot, 1020 up, as many as the busiest tab has pages."""
    return set(range(1020, 1020 + int(generated_data()["slots"])))


GLOBAL_IDS = set(range(1001, 1008)) | {1012, 1013, 1014, 1015, 1016, 1017, 1018} | slot_ids()


# ---------------------------------------------------------------------------
# The value encoding, in Python, from the script's own patterns
# ---------------------------------------------------------------------------

REGSUBEX = re.compile(r"\$regsubex\((?:\$1|%v),/(.+?)/([gi]*),(.+)\)$")


def regsubex_calls(alias_name):
    """[(pattern, flags, replacement)] of each $regsubex line of an alias, in order."""
    found = []
    for line in statements(alias(alias_name)):
        match = REGSUBEX.search(line)
        if match:
            found.append(match.groups())
    return found


def python_sub(pattern, flags, replacement, text):
    """One mIRC $regsubex, run by Python's re. The replacements the script
    uses are recognised by their text, so a changed one fails loudly."""
    if replacement == r"$chr($base(\1,16,10))":
        repl = lambda m: chr(int(m.group(1), 16))  # noqa: E731
    elif replacement == "$chr(37) $+ 25":
        repl = lambda m: "%25"  # noqa: E731
    elif replacement == "$chr(37) $+ 20":
        repl = lambda m: "%20"  # noqa: E731
    elif replacement == r"$chr(37) $+ $base($asc(\1),10,16,2)":
        repl = lambda m: "%%%02X" % ord(m.group(1))  # noqa: E731
    else:
        raise AssertionError("a replacement this test does not know: %s" % replacement)
    return re.sub(pattern, repl, text, count=0 if "g" in flags else 1,
                  flags=re.I if "i" in flags else 0)


def mirc_dec(text):
    (pattern, flags, replacement), = regsubex_calls("dccore.sw.dec")
    return python_sub(pattern, flags, replacement, text)


ENC_REPLACEMENTS = ("$chr(37) $+ 25", "$chr(37) $+ 20", r"$chr(37) $+ $base($asc(\1),10,16,2)")


def mirc_enc(text):
    """$dccore.sw.enc: three $regsubex nested in one expression (a /var in
    between would close up runs of spaces), innermost first."""
    (statement,) = statements(alias("dccore.sw.enc"))
    assert statement.startswith("return $regsubex($regsubex($regsubex($1,/"), statement
    patterns = re.findall(r",/([^/]+)/([gi]*),", statement)
    assert len(patterns) == 3, patterns
    for (pattern, flags), replacement in zip(patterns, ENC_REPLACEMENTS):
        assert ",%s)" % replacement in statement, replacement
        text = python_sub(pattern, flags, replacement, text)
    return text


def collapse(text):
    """What a command's parameters do to a text in mIRC: every run of
    spaces closed up to one, the spaces at either end gone."""
    return re.sub(" +", " ", text).strip(" ")


def mirc_tok(text):
    body = statements(alias("dccore.sw.tok"))
    assert body == ["if ($1 == $null) { return - }",
                    "if ($1 == -) { return $chr(37) $+ 2D }",
                    "return $replace($dccore.sw.enc($1),$chr(32),$chr(37) $+ 20)"], body
    if text == "":
        return "-"
    if text == "-":
        return "%2D"
    return mirc_enc(text).replace(" ", "%20")


BATTERY = ["", "plain", "Some Album", "a  b", "a   b", " lead", "trail ", "  ", " ", "%admin%", "%nick%",
           "100%", "50% off", "%41", "%20", "%2520", "%25", "%2d", "%2D", "x%2Dy", "%7f", "%1F", "%0a",
           "%", "%%20", "-", "--", "#music", "\\x0304,01", "tab\there", "bell\x07", "a\x7fb",
           "\x01start", "end\x1f", "mixed %20 and  spaces ", "PRIVMSG X :LOGIN alfa secret",
           ".mp3, .flac, .m4a", "./data/x.json", "C:\\Music\\A b", "%%%", "%2", "%g0"]


class TheValueEncodingMatchesTheBots(unittest.TestCase):

    def test_the_decoder_is_the_documented_one(self):
        self.assertEqual(statements(alias("dccore.sw.dec")),
                         [r"return $regsubex($1,/%(0[0-9a-f]|1[0-9a-f]|2[05d]|7f)/gi,$chr($base(\1,16,10)))"])

    def test_the_decoder_decodes_what_the_bot_encodes(self):
        for value in BATTERY:
            with self.subTest(value=value):
                encoded = console_settings.encode_value(value)
                self.assertEqual(mirc_dec(encoded), console_settings.decode_value(encoded))
                self.assertEqual(mirc_dec(encoded), value)

    def test_the_decoder_agrees_with_the_bot_on_text_it_did_not_encode(self):
        for value in BATTERY:
            with self.subTest(value=value):
                self.assertEqual(mirc_dec(value), console_settings.decode_value(value))

    def test_the_encoder_writes_what_the_bot_writes(self):
        for value in BATTERY:
            with self.subTest(value=value):
                self.assertEqual(mirc_enc(value), console_settings.encode_value(value))
                self.assertEqual(console_settings.decode_value(mirc_enc(value)), value)

    def test_a_token_is_what_the_bot_makes_one(self):
        for value in BATTERY:
            with self.subTest(value=value):
                self.assertEqual(mirc_tok(value), console_settings.encode_token(value))
                self.assertNotIn(" ", mirc_tok(value))

    def test_a_broken_decoder_would_be_caught(self):
        """Mutation check of the emulation: the same battery tells a pattern
        missing the %20 escape from the real one."""
        broken = r"%(0[0-9a-f]|1[0-9a-f]|2[5d]|7f)"
        replacement = r"$chr($base(\1,16,10))"
        self.assertTrue(any(python_sub(broken, "gi", replacement, console_settings.encode_value(v)) != v
                            for v in BATTERY))


# ---------------------------------------------------------------------------
# The protocol: every reply has a handler, every command is real
# ---------------------------------------------------------------------------

def documented_reply_types():
    """The DCCORE line types the doc's "Settings over the console" table defines."""
    with io.open(os.path.join(REPO_ROOT, "docs", "ADMIN-CONSOLE.md"), encoding="utf-8") as handle:
        doc = handle.read()
    section = doc[doc.index("## Settings over the console"):doc.index("## The window, in mIRC")]
    # OUT is the console's ordinary reply (a stalled snapshot ends with one):
    # dccore.out shows it, and dccore.sw.old reads the one that matters here.
    return sorted(set(re.findall(r"`DCCORE ([A-Z]+)", section)) - {"OUT"})


class EveryReplyHasAHandler(unittest.TestCase):

    def test_the_doc_lists_the_ones_this_test_expects(self):
        types = documented_reply_types()
        for kind in ("CAPS", "SETBEGIN", "SETF", "SETEND", "SETOPEN", "SETERR", "SETDONE", "PVBEGIN", "PVLINE",
                     "PVEND", "SRVBEGIN", "SRVLIST", "SRVCHAN", "SRVFOLDER", "SRVEND", "SRVOPEN", "SRVERR",
                     "SRVDONE", "FLDBEGIN", "FLDROW", "FLDEND", "FLDOPEN", "FLDERR", "FLDDONE", "OCBEGIN",
                     "OCLINE", "OCEND", "OCOPEN", "OCERR", "OCDONE", "OCRESEND", "BANBEGIN", "BANP", "BANT",
                     "BANEND"):
            self.assertIn(kind, types)

    def test_every_documented_reply_is_routed_to_the_window(self):
        routed = statements(alias("dccore.sw.types"))[0].split(" ", 1)[1].split()
        self.assertEqual(sorted(routed), documented_reply_types())

    def test_every_documented_reply_has_a_branch(self):
        body = alias("dccore.sw.line")
        for kind in documented_reply_types():
            with self.subTest(kind=kind):
                self.assertRegex(body, r"\(%%t == %s\)" % kind)

    def test_setdone_handles_every_status(self):
        body = alias("dccore.sw.done")
        for status in ("ok", "error", "confirm", "aborted"):
            self.assertIn("if ($1 == %s)" % status, body)

    def test_the_structured_feed_routes_them_while_the_window_is_open(self):
        body = alias("dccore.structured")
        self.assertIn("if ($istok($dccore.sw.types,%type,32)) && (($dccore.sw.wants(%type)) || ($dccore.st(sw.tail))) "
                      "{ dccore.sw.line $1- | return }", body)
        # and before the fallback that shows an unknown type as a line
        self.assertLess(body.index("dccore.sw.line $1-"), body.index("dccore.echo $dccore.tag(%type,info) $2-"))

    def test_an_older_bot_is_told_apart(self):
        body = alias("dccore.structured")
        self.assertIn("if ($dccore.sw.old($2-)) { return }", body)
        self.assertIn("if (Unknown command: consolecaps* !iswm $1-) { return $false }", alias("dccore.sw.old"))
        self.assertIn("This bot is too old for the settings window - update it.", alias("dccore.sw.old"))
        self.assertIn("dccore.send consolecaps", alias("dccore.sw.start"))


def sent_commands():
    """Every console line the window sends: (command, rest) of each
    `dccore.send ...` in its section, and each snapshot it asks for."""
    sent = []
    for line in handwritten().split("\n"):
        for match in re.finditer(r"dccore\.send (\S+)(?: (\S+))?", line):
            sent.append(match.groups())
        for match in re.finditer(r"dccore\.sw\.(?:re)?ask (\w+)", line):
            sent.append((match.group(1), None))
    for item, value in generated_data().items():
        if item.endswith(".ask"):
            sent.extend((word, None) for word in value.split())
    return sent


SUBCOMMANDS = {
    "served": ("begin", "list", "chan", "folder", "commit", "abort"),
    "folders": ("begin", "row", "commit", "abort"),
    "onconnect": ("begin", "delay", "line", "commit", "abort", "resend"),
}


class EveryCommandIsReal(unittest.TestCase):

    def test_the_subcommands_are_the_ones_the_bot_accepts(self):
        source = io.open(os.path.join(REPO_ROOT, "src", "console_settings.py"), encoding="utf-8").read()
        for command, subs in SUBCOMMANDS.items():
            body = source[source.index("def cmd_%s(" % command):]
            body = body[:body.index("\ndef ", 1)]
            accepted = set(re.findall(r'sub == "(\w+)"', body)) | set(
                word for tup in re.findall(r"sub not in \(([^)]*)\)", body) for word in re.findall(r'"(\w+)"', tup))
            self.assertEqual(set(subs) - accepted, set(), command)

    def test_every_command_the_window_sends_is_a_console_command(self):
        sent = sent_commands()
        self.assertGreater(len(sent), 30)
        for command, rest in sent:
            if command.startswith("$") or command.startswith("%"):
                continue
            with self.subTest(command=command, rest=rest):
                self.assertIn(command, adminchat.COMMANDS)
                if command in SUBCOMMANDS and rest is not None and not rest.startswith(("$", "%")):
                    self.assertIn(rest, SUBCOMMANDS[command])

    def test_the_window_sends_every_part_of_the_protocol(self):
        sent = {command for command, _rest in sent_commands()}
        for command in ("consolecaps", "settings", "setbegin", "set", "setcommit", "setabort", "setpreview",
                        "served", "folders", "onconnect", "banlist", "ban", "unban", "ignore", "unignore"):
            self.assertIn(command, sent)

    def test_the_settings_commands_are_still_kept_out_of_the_menu_plumbing_list(self):
        """The window is how they are clicked (test_the_mirc_menu_has_every_command
        lists them as plumbing for that reason)."""
        self.assertIn("dccore.settings", alias("dccore"))


class ApplyIsOneTransaction(unittest.TestCase):

    def test_apply_sends_setbegin_then_each_set_then_setcommit(self):
        body = statements(alias("dccore.sw.apply"))
        begin = body.index("dccore.send setbegin")
        sets = [n for n, line in enumerate(body) if line.startswith("dccore.send set $gettok(%keys,%i,32) ")]
        commit = body.index("dccore.send setcommit")
        self.assertEqual(len(sets), 1)
        self.assertLess(begin, sets[0])
        self.assertLess(sets[0], commit)
        self.assertIn("$dccore.sw.wire($gettok(%keys,%i,32))", body[sets[0]])
        # the loop around the set is between the two
        self.assertTrue(any(line.startswith("while (%i <= $numtok(%keys,32))") for line in body[begin:commit]))

    def test_no_set_is_ever_sent_outside_a_transaction(self):
        """A lone `set` saves at once: every alias that sends one opens a
        transaction first and ends it after."""
        section = handwritten()
        senders = re.findall(r"\nalias (\S+) \{", section)
        checked = 0
        for name in senders:
            body = alias(name)
            if not re.search(r"dccore\.send set ", body):
                continue
            checked += 1
            first_set = re.search(r"dccore\.send set ", body).start()
            self.assertIn("dccore.send setbegin", body[:first_set], name)
            self.assertRegex(body[first_set:], r"dccore\.send set(commit|abort)", name)
        self.assertEqual(checked, 2)        # Apply and the preview

    def test_the_preview_never_commits(self):
        body = alias("dccore.sw.preview")
        self.assertNotIn("setcommit", body)
        self.assertLess(body.index("dccore.send setpreview"), body.index("dccore.send setabort"))

    def test_only_changed_settings_are_sent(self):
        self.assertIn("var %keys = $iif($dccore.sw.s(loaded),$dccore.sw.changed)", alias("dccore.sw.apply"))
        dirty = statements(alias("dccore.sw.dirty"))
        self.assertIn("if ($+(=,$dccore.sw.shownenc($1)) === $dccore.sw.s(d. $+ $1)) { return $false }", dirty)

    def test_ok_closes_only_once_the_bot_has_saved(self):
        done = alias("dccore.sw.done")
        ok_at = done.index("if ($1 == ok) {")
        error_at = done.index("if ($1 == error) {", ok_at)
        ok = done[ok_at:error_at]
        # through maybeclose: the lists or the on-connect commands may still
        # be saving (Apply sends them too, as transactions of their own)
        self.assertIn("if ($dccore.sw.s(close)) { dccore.sys Settings: %said | dccore.sw.maybeclose | return }", ok)
        self.assertEqual(statements(alias("dccore.sw.maybeclose")),
                         ["if (!$dccore.sw.s(close)) { return }",
                          "if ($dccore.sw.s(saving.srv)) || ($dccore.sw.s(saving.oc)) || "
                          "($istok(apply confirm aborting,$dccore.sw.s(phase),32)) { return }",
                          "dialog -x dccore.set"])
        error = done[error_at:]
        self.assertNotIn("dialog -x", error)
        self.assertIn("hdel dccore.sws close", error)
        # OK with nothing changed closes at once
        self.assertIn("if ($1 == close) { dialog -x dccore.set | return }", alias("dccore.sw.apply"))

    def test_the_confirm_question_is_asked_from_a_timer_and_answered(self):
        self.assertIn(".timerdccoreSwAsk -m 1 0 dccore.sw.confirm", alias("dccore.sw.done"))
        confirm = alias("dccore.sw.confirm")
        self.assertIn("if ($input(%q,yq,DCCore - Control Panel)) {", confirm)
        self.assertIn("dccore.send setcommit confirm", confirm)
        self.assertIn("dccore.send setabort", confirm)

    def test_a_file_location_asks_before_it_is_sent(self):
        apply = alias("dccore.sw.apply")
        ask = apply.index("if ($dccore.sw.risky(%keys)) && ($2 != asked) {")
        self.assertLess(ask, apply.index("dccore.send setbegin"))
        self.assertIn(".timerdccoreSwAsk -m 1 0 dccore.sw.riskyask", apply[ask:apply.index("dccore.send setbegin")])
        riskyask = alias("dccore.sw.riskyask")
        self.assertIn("if ($input(Change where the bot keeps its files?", riskyask)
        self.assertIn("{ dccore.sw.apply %how asked | return }", riskyask)
        data = generated_data()
        files = data["confirm"].split()
        self.assertIn("TMP_ZIP_DIR", files)
        self.assertIn("DCC_QUEUE_FILE", files)
        self.assertEqual(len(files), 23)

    def test_cancel_sends_nothing(self):
        cancel = [c for c in dialog("dccore.set") if c[2] == 1016]
        self.assertEqual(cancel[0][7], "cancel")
        self.assertNotIn("1016", alias("dccore.sw.click"))


class EverySnapshotIsCounted(unittest.TestCase):
    """The outbox can drop lines, and a stalled snapshot ends with no END
    at all: nothing is shown from one that did not arrive whole."""

    def branch(self, kind):
        body = alias("dccore.sw.line")
        start = body.index("if (%%t == %s) {" % kind)
        end = body.find("\n  if (%t ==", start + 1)
        return body[start:] if end < 0 else body[start:end]

    def test_each_end_compares_what_came_with_what_was_announced(self):
        self.assertIn("if ($dccore.sw.s(setgot) != $2) || ($dccore.sw.s(setwant) != $2) {", self.branch("SETEND"))
        self.assertIn("if ($dccore.sw.s(srv.got) != $2-4) {", self.branch("SRVEND"))
        self.assertIn("if ($dccore.sw.s(fld.got) != $2) || ($dccore.sw.s(fld.want) != $2)", self.branch("FLDEND"))
        self.assertIn("if ($dccore.sw.s(oc.got) != $2) || ($dccore.sw.s(oc.want) != $2)", self.branch("OCEND"))
        self.assertIn("if ($dccore.sw.s(ban.gp) != $iif($2 > 200,200,$2)) || ($dccore.sw.s(ban.gt) != "
                      "$iif($3 > 200,200,$3)) {", self.branch("BANEND"))
        self.assertIn("if ($dccore.sw.s(pvgot) != $2)", self.branch("PVEND"))

    def test_the_settings_are_only_shown_once_complete(self):
        end = self.branch("SETEND")
        self.assertLess(end.index("!= $2"), end.index("dccore.sw.fillall"))
        self.assertLess(end.index("return"), end.index("dccore.sw.fillall"))

    def test_the_count_check_counts(self):
        self.assertIn("hinc dccore.sws setgot", self.branch("SETF"))
        self.assertIn("hadd dccore.sws setgot 0", self.branch("SETBEGIN"))
        self.assertIn("dccore.sw.srv.count 1", self.branch("SRVLIST"))
        self.assertIn("dccore.sw.srv.count 2", self.branch("SRVCHAN"))
        self.assertIn("dccore.sw.srv.count 3", self.branch("SRVFOLDER"))

    def test_a_missing_end_times_out(self):
        self.assertIn("dccore.sw.wait settings", alias("dccore.sw.load"))
        self.assertIn("dccore.sw.wait $1", alias("dccore.sw.ask"))
        for kind, what in (("SETEND", "settings"), ("SRVEND", "served"), ("FLDEND", "folders"),
                           ("OCEND", "onconnect"), ("BANEND", "banlist")):
            self.assertIn("dccore.sw.waited %s" % what, self.branch(kind))
        self.assertIn("dccore.sw.waited settings", alias("dccore.sw.done"))
        self.assertIn(".timerdccoreSwTick 0 5 dccore.sw.tick", alias("dccore.sw.init"))
        tick = alias("dccore.sw.tick")
        self.assertIn("if ($ctime > $dccore.sw.s(%item)) { hdel dccore.sws %item | dccore.sw.timeout "
                      "$gettok(%item,2,46) }", tick)
        self.assertIn("hadd dccore.sws phase idle", alias("dccore.sw.timeout"))


# ---------------------------------------------------------------------------
# The dialog itself
# ---------------------------------------------------------------------------

def text_px(text):
    return sum(CHAR_PX[c] for c in text)


def wrapped_lines(text, width_dbu):
    width = width_dbu * PX_PER_DBU
    lines, current = 1, 0
    for word in text.split(" "):
        need = text_px(word)
        if current == 0:
            current = need
        elif current + CHAR_PX[" "] + need <= width:
            current += CHAR_PX[" "] + need
        else:
            lines += 1
            current = need
    return lines


class TheDialog(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.controls = dialog("dccore.set")
        cls.data = generated_data()
        cls.pages = page_ids(cls.data)
        cls.size = tuple(map(int, re.search(r"size -1 -1 (\d+) (\d+)", block("dialog dccore.set {")).groups()))

    def texts(self):
        """(kind, text, id, w, h) for every labelled control, with the texts
        the init handler writes in (t.<id>) in place of their empty strings."""
        rows = []
        for kind, label, cid, _x, _y, w, h, _s in self.controls:
            if kind not in ("check", "text", "button", "tab", "box"):
                continue
            if "t.%d" % cid in self.data:
                label = untext(self.data["t.%d" % cid]).replace("&&", "&")
            rows.append((kind, label, cid, w, h))
        return rows

    def test_the_size_is_the_mockups(self):
        # 488 x 364 dialog units: the mockup's proportions at a size that
        # fits a 1080p screen, with room for the frames, the labels above
        # their fields and the page's name under the tabs (#1288, phase 3)
        self.assertEqual(self.size, (488, 364))

    def test_ids_are_unique_and_clear_of_the_other_dialogs(self):
        ids = [c[2] for c in self.controls]
        self.assertEqual(len(ids), len(set(ids)))
        others = {c[2] for c in dialog("dccore.dq")}
        self.assertEqual(set(ids) & others, set())
        self.assertGreater(min(ids), 1000)

    def test_every_control_is_on_exactly_one_page(self):
        seen = {}
        for page, ids in self.pages.items():
            for cid in ids:
                self.assertNotIn(cid, seen, "%d is on pages %s and %d" % (cid, seen.get(cid), page))
                seen[cid] = page
        table = {c[2] for c in self.controls}
        self.assertEqual(set(seen) | GLOBAL_IDS, table)
        self.assertEqual(set(seen) & GLOBAL_IDS, set())

    def test_no_control_is_attached_to_a_tab(self):
        """mIRC would show a tab's controls again whenever it is clicked."""
        for kind, _label, cid, _x, _y, _w, _h, style in self.controls:
            self.assertNotRegex(style, r"\btab\b", cid)

    def test_every_label_fits(self):
        clipped = []
        for kind, text, cid, w, h in self.texts():
            if kind == "tab" or not text:
                continue
            available = w * PX_PER_DBU
            if kind == "check":
                if text_px(text) + CHECK_BOX_PX > available:
                    clipped.append("%d %r (check)" % (cid, text))
            elif kind in ("button", "box"):
                if text_px(text) + 8 > available:
                    clipped.append("%d %r (%s)" % (cid, text, kind))
            elif wrapped_lines(text, w) * 8 > h:
                clipped.append("%d %r needs %d lines in %d dbu" % (cid, text, wrapped_lines(text, w), h))
        self.assertEqual(clipped, [])
        self.assertGreater(len(self.texts()), 200)

    def test_every_label_is_in_the_width_table(self):
        for _kind, text, cid, _w, _h in self.texts():
            self.assertEqual(sorted(set(text) - set(CHAR_PX)), [], cid)

    def test_the_label_arithmetic_would_catch_a_long_one(self):
        """Mutation check: a dashboard label in a narrow box is clipped."""
        self.assertGreater(wrapped_lines("Check the on-connect commands worked every (minutes, 0 = never)", 186), 1)
        self.assertGreater(text_px("Also send requests, queue positions, starts and searches to the IRC debug "
                                   "channel") + CHECK_BOX_PX, 157 * PX_PER_DBU)

    def test_every_control_is_inside_the_dialog_and_no_two_on_a_page_overlap(self):
        where = {c[2]: c for c in self.controls}

        def box(cid):
            kind, _l, _i, x, y, w, h, _s = where[cid]
            return x, y, x + w, y + (12 if kind == "combo" else h)
        for cid in where:
            if where[cid][0] == "tab" and cid != 1001:
                continue
            x0, y0, x1, y1 = box(cid)
            self.assertLessEqual(x1, self.size[0], cid)
            self.assertLessEqual(y1, self.size[1], cid)
        for page, ids in self.pages.items():
            for n, a in enumerate(ids):
                for b in ids[n + 1:]:
                    A, B = box(a), box(b)
                    overlap = A[0] < B[2] and B[0] < A[2] and A[1] < B[3] and B[1] < A[3]
                    frames = [c for c in (a, b) if where[c][0] == "box"]
                    if len(frames) == 1:
                        # a frame holds its section's controls: each is inside
                        # it whole, or clear of it - never across its edge
                        F, C = (A, B) if frames[0] == a else (B, A)
                        inside = F[0] <= C[0] and C[2] <= F[2] and F[1] <= C[1] and C[3] <= F[3]
                        self.assertTrue(inside or not overlap, "page %d: %d straddles the frame %d"
                                        % (page, b if frames[0] == a else a, frames[0]))
                        continue
                    self.assertFalse(overlap, "page %d: %d and %d overlap" % (page, a, b))

    def test_the_tab_control_frames_the_body(self):
        """Its page border runs under every tab and round the body: a strip
        of tabs only had Windows draw that border cut off (#1288)."""
        import build_settings_window as generator
        tabs = [c for c in self.controls if c[0] == "tab"]
        self.assertEqual([t[1] for t in tabs], ["General", "Sharing", "Downloads", "Security",
                                                "Dashboard & Console", "Advanced", "mIRC"])
        _kind, _label, _cid, tx, ty, tw, th, _style = tabs[0]
        body = [c for c in self.controls if c[0] not in ("tab", "button") and c[2] not in (1013, 1018)]
        self.assertGreaterEqual(min(c[4] for c in body), ty + generator.TAB_HEADER_H)
        self.assertLessEqual(max(c[4] + (12 if c[0] == "combo" else c[6]) for c in body), ty + th)
        self.assertGreater(min(c[3] for c in body), tx)
        self.assertLess(max(c[3] + c[5] for c in body), tx + tw)
        # the connection and status lines and the buttons sit under it
        self.assertGreater(min(c[4] for c in self.controls if c[0] == "button" and c[2] in (1014, 1015, 1016, 1017)),
                           ty + th)

    def test_the_footer_has_apply_ok_and_cancel(self):
        buttons = {c[2]: c[1] for c in self.controls if c[0] == "button"}
        self.assertEqual((buttons[1014], buttons[1015], buttons[1016]), ("Apply", "OK", "Cancel"))
        click = alias("dccore.sw.click")
        self.assertIn("if (%id == 1014) { dccore.sw.apply apply | return }", click)
        self.assertIn("if (%id == 1015) { dccore.sw.apply close | return }", click)
        # OK is not mIRC's own "ok" button, which would close before the bot answers
        ok = next(c for c in self.controls if c[2] == 1015)
        self.assertNotIn("ok", ok[7])

    def test_the_ids_the_handwritten_code_uses_exist(self):
        table = {c[2] for c in self.controls}
        used = set(int(i) for i in re.findall(r"\bdccore\.set (1[0-9]{3})\b", handwritten()))
        used |= set(int(i) for i in re.findall(r"%id == (1[0-9]{3})\b", handwritten()))
        self.assertGreater(len(used), 20)
        self.assertEqual(used - table, set())


class TheWindowsBehaviour(unittest.TestCase):

    def test_the_browse_buttons_need_a_local_bot(self):
        self.assertEqual(statements(alias("dccore.sw.local"))[0],
                         "if ($istok(127.0.0.1 ::1,$chat($dccore.bot).ip,32)) { return $true }")
        self.assertTrue(statements(alias("dccore.sw.browse"))[0].startswith("if (!$dccore.sw.local) {"))
        enable = alias("dccore.sw.enable")
        self.assertIn("if ($1) && (!$dccore.sw.local) {", enable)
        self.assertGreater(len([k for k in generated_data() if k.startswith("br.")]), 25)

    def test_the_input_prompts_have_no_literal_comma(self):
        for match in re.finditer(r"\$input\(([^%][^)]*)\)", handwritten()):
            self.assertEqual(match.group(1).count(","), 2, match.group(0))

    def test_dccore_options_opens_the_window_on_its_mirc_side(self):
        # /dccore options: the same window, on this mIRC's own settings
        self.assertEqual(statements(alias("dccore.options")), ["dccore.settings mirc"])
        self.assertIn("if (%cmd == options) { dccore.options | return }", alias("dccore"))
        self.assertIn("if (%cmd == settings) { dccore.settings | return }", alias("dccore"))
        settings = alias("dccore.settings")
        self.assertIn("hadd dccore.live sw.open $iif($1 == mirc,mirc,start)", settings)
        # a window already open goes there: the mIRC side with DCX, the mIRC
        # tab (the last group) in the native form
        self.assertIn("if ($1 == mirc) { dccore.sw.go $iif($dccore.sw.s(dcx),side 2,tab $dccore.sw.m(groups)) }", settings)
        self.assertIn("if ($dccore.sw.s(dcx)) { dccore.sw.side $iif($hget(dccore.live,sw.open) == mirc,2,0) }",
                      alias("dccore.sw.init"))
        self.assertNotIn("dialog dccore.opt {", code())

    def test_the_local_switches_are_this_mircs(self):
        data = generated_data()
        self.assertEqual(data["locals"].split(), ["start.main", "start.chat", "start.downloads", "auto", "panel",
                                          "titlebar", "separate", "beep", "nodcx", "chat.all", "chat.popup"])
        save = alias("dccore.sw.savelocal")
        self.assertIn("if (%state != $dccore.opt(%o)) { hadd dccore %o %state | inc %n }", save)
        self.assertIn("if (%n) { dccore.save }", save)

    def test_every_menu_with_options_has_settings_next_to_it(self):
        text = code()
        # one entry, the control panel, where the two settings entries were:
        # the window holds both sides now
        menus = re.findall(r"\nmenu [^\n]+ \{\n(.*?)\n\}", text, re.S)
        with_panel = 0
        for menu in menus:
            lines = [line.strip().lstrip(".") for line in menu.split("\n")]
            with_panel += lines.count("Control Panel...:dccore.settings")
        self.assertEqual(with_panel, 3)
        self.assertIn("\n  Control Panel:dccore.settings\n", block("menu @DCCore {"))
        self.assertNotIn("Script Settings", text)
        self.assertNotIn("Bot Settings", text)

    def test_the_preview_window_draws_the_decoded_lines(self):
        body = alias("dccore.sw.line")
        self.assertIn("echo @DCCore-preview $+($chr(3),14,$2,:,$chr(15)) $dccore.sw.pvtext($3-)", body)


def mirc_pvtext(text):
    """$dccore.sw.pvtext, in Python: the script's own decoder pattern, then
    every space a non-breaking one."""
    (statement,) = statements(alias("dccore.sw.pvtext"))
    decoder = statements(alias("dccore.sw.dec"))[0][len("return "):]
    assert statement == "return $replace(%s,$chr(32),$chr(160))" % decoder, statement
    return mirc_dec(text).replace(" ", "\u00a0")


class ThePreviewKeepsItsFrame(unittest.TestCase):
    """The theme preview's lines arrive encoded (colour codes as %03, runs of
    spaces as %20): mIRC hands a chat line to a script with each run of
    spaces collapsed, and echo collapses them again - and a theme's frame is
    runs of spaces painted with a background colour."""

    LINES = ["\x0301,01   \x0300,04 Files \x0301,01   \x0f 12,345 \x0308|\x0f end ",
             "  \x0302,02    \x0300,01 Sent: Some Album  (1.2 GB) \x0302,02    ",
             "plain line", " ", "100% %nick% \x0304red\x03"]

    def chat_text(self, line):
        """What the PVLINE handler's $3- holds: the bot's line as mIRC passes
        it on - every run of spaces collapsed to one."""
        out = console_settings.preview_lines({"advert": line, "notice": "x"})[1]
        self.assertTrue(out.startswith("DCCORE PVLINE advert "))
        text = out[len("DCCORE PVLINE advert "):]
        return re.sub(" +", " ", text).strip(" ")

    def test_a_line_decodes_to_the_bots_line_exactly(self):
        for line in self.LINES:
            with self.subTest(line=line):
                drawn = mirc_pvtext(self.chat_text(line))
                self.assertEqual(drawn.replace("\u00a0", " "), line)

    def test_what_is_echoed_has_no_plain_space_to_collapse(self):
        for line in self.LINES:
            with self.subTest(line=line):
                self.assertNotIn(" ", mirc_pvtext(self.chat_text(line)))

    def test_the_colour_codes_survive(self):
        drawn = mirc_pvtext(self.chat_text(self.LINES[0]))
        self.assertEqual(drawn.count("\x03"), self.LINES[0].count("\x03"))
        self.assertIn("\x0300,04\u00a0Files\u00a0", drawn)

    def test_a_raw_line_would_have_lost_the_frame(self):
        """Mutation check of the fixture: the same line sent raw comes out of
        mIRC narrower, so the tests above test something."""
        line = self.LINES[0]
        self.assertNotEqual(re.sub(" +", " ", line).strip(" "), line)

    def test_the_window_needs_mirc_617_for_regsubex(self):
        self.assertIn("if ($version < 6.17) {", alias("dccore.settings"))

    def test_a_reconnect_or_a_closed_console_starts_the_window_again(self):
        self.assertIn("dccore.sw.lost", block("on *:CHATCLOSE: {"))
        hello = alias("dccore.structured")
        hello = hello[hello.index("if (%type == HELLO) {"):hello.index("if (%type == STATUS)")]
        self.assertIn("dccore.sw.lost", hello)

    def test_the_lines_are_well_under_mircs_limit(self):
        self.assertLess(max(len(line) for line in raw().split("\n")), 900)


class TheExamplesAreInvented(unittest.TestCase):
    """The mockup the layout follows holds real channel, nick and network
    names. Assert what the shipped files hold instead of listing what they
    must not: every channel is #music and every host is *.example."""

    def files(self):
        names = ["scripts/mirc/settings_window_layout.py", "scripts/mirc/build_settings_window.py"]
        texts = [io.open(os.path.join(REPO_ROOT, n), encoding="utf-8").read() for n in names]
        return texts + [handwritten()]

    def test_every_channel_and_host_is_an_invented_one(self):
        for text in self.files():
            for channel in re.findall(r"(?<![\w&])#[A-Za-z][\w-]*", text):
                self.assertIn(channel, ("#music",), channel)
            for host in re.findall(r"\*!\*@[\w.-]+|\*\.[\w-]+\.[\w.-]+", text):
                self.assertTrue(host.endswith(".example"), host)


class TheVersion(unittest.TestCase):

    def test_the_script_is_at_least_1_20_0(self):
        """The window came with 1.20.0: a later release moves the version on,
        and this only refuses one that went back."""
        found = re.search(r"\nalias dccore\.ver \{ return ([0-9.]+) \}\n", code())
        self.assertIsNotNone(found, "dccore.ver has no plain version")
        self.assertGreaterEqual(tuple(int(n) for n in found.group(1).split(".")), (1, 20, 0))


class TheReviewOfTheFirstVersion(unittest.TestCase):
    """What an independent review of the window's first version found, each
    pinned here (and mutation-checked: putting the old line back fails)."""

    def line_branch(self, kind):
        body = alias("dccore.sw.line")
        start = body.index("if (%%t == %s)" % kind)
        end = body.find("\n  if (%t ==", start + 1)
        return body[start:] if end < 0 else body[start:end]

    # 1. A refused save keeps the lists' edits, so OK still refuses to close.
    def test_a_refused_lists_save_keeps_its_edits(self):
        self.assertNotIn("hdel dccore.sws srv.dirty", alias("dccore.sw.srv.save"))
        done = self.line_branch("SRVDONE) || (%t == FLDDONE")
        lines = {status: next(line for line in statements(done) if line.startswith("if ($2 == %s)" % status))
                 for status in ("ok", "unchanged", "error")}
        self.assertIn("hdel dccore.sws srv.dirty", lines["ok"])
        self.assertIn("hdel dccore.sws srv.dirty", lines["unchanged"])
        self.assertNotIn("srv.dirty", lines["error"])
        self.assertIn("Your changes are still here", lines["error"])
        self.assertIn("if ($dccore.sw.s(srv.dirty)) { return Lists and Channel bindings }", alias("dccore.sw.unsaved"))

    # 2. The operator's data is compared case-sensitively.
    def test_a_case_only_rename_is_a_change(self):
        self.assertIn("($dccore.sw.srv.sig === $dccore.sw.s(srv.sig0))", alias("dccore.sw.srv.save"))

    def test_no_operator_data_is_compared_ignoring_case(self):
        """== and != ignore case in mSL. Every comparison of a name, a path,
        a value or a signature is === (or a test for empty)."""
        found = 0
        for line in handwritten().split("\n"):
            for left, op, right in re.findall(r"\((\S+) (===|==|!=) ([^)]*\)?)\)", line):
                operands = left + " " + right
                if not re.search(r"\.text\b|sig\b|sig0|oc\.s\.|%now|d\. \$\+|shownenc|\$did\(dccore\.set,1501,"
                                 r"|%bot|myhost", operands):
                    continue
                found += 1
                if "$null" in right or right.strip() == "-":
                    continue
                # A computer's name is the same name in any case: == is right
                # for the machine check (dccore.sw.local), and only there.
                if left == "%bot" and right.startswith("$dccore.sw.myhost"):
                    continue
                self.assertEqual(op, "===", line.strip())
        self.assertGreater(found, 5)

    # 3. The confirm question's wait starts once it is answered.
    def test_the_confirm_wait_starts_after_the_answer(self):
        body = alias("dccore.sw.confirm")
        asked = body.index("$input(%q,yq,DCCore - Control Panel)")
        self.assertEqual(body.count("dccore.sw.wait settings"), 2)
        self.assertGreater(body.index("dccore.sw.wait settings"), asked)
        for send in ("dccore.send setcommit confirm", "dccore.send setabort"):
            before = body[:body.index(send)]
            self.assertGreater(before.rindex("dccore.sw.wait settings"), asked)

    # A. Rows are kept encoded and sent back as they came.
    def test_encoded_text_survives_a_commands_parameters(self):
        """Why keeping the encoded form works: it has no run of spaces and no
        space at either end, so /hadd and an alias's parameters leave it as
        it is - where the decoded form would be closed up."""
        for value in BATTERY:
            with self.subTest(value=value):
                encoded = console_settings.encode_value(value)
                self.assertEqual(collapse(encoded), encoded)
                self.assertEqual(mirc_dec(collapse(encoded)), value)
        self.assertNotEqual(collapse("a  b"), "a  b")

    def test_the_snapshots_are_stored_encoded(self):
        self.assertIn("hadd dccore.sws v. $+ $2 = $+ $4-", self.line_branch("SETF"))
        self.assertIn("hadd dccore.sws srv.l. $+ %id $3 $4-", self.line_branch("SRVLIST"))
        self.assertIn("dccore.sw.srv.addfolder $dccore.sw.s(srv.n. $+ $2) $3 $4-", self.line_branch("SRVFOLDER"))
        self.assertIn("hadd dccore.sws fld.r. $+ $dccore.sw.s(fld.got) $3 $4-", self.line_branch("FLDROW"))
        self.assertIn("hadd dccore.sws oc.l. $+ $dccore.sw.s(oc.got) = $+ $3-", self.line_branch("OCLINE"))
        for kind in ("SETF", "SRVLIST", "SRVFOLDER", "FLDROW", "OCLINE"):
            self.assertNotIn("$dccore.sw.dec(", self.line_branch(kind), kind)

    def test_the_lists_go_back_as_stored(self):
        save = alias("dccore.sw.srv.save")
        self.assertIn("dccore.send served list %i %l", save)
        self.assertIn("dccore.send served folder %i $gettok(%row,2-,32)", save)
        self.assertIn("dccore.send folders row %n $gettok(%row,2-,32)", save)
        self.assertNotIn("$dccore.sw.enc(", save)

    def test_what_the_operator_types_is_encoded_straight_from_the_control(self):
        """Every name, label or path typed into the list window is stored
        as enc() or tok() of $did(...).text - never via a /var or a
        command's parameters first."""
        stored = 0
        for name in ("dccore.sw.srv.lok", "dccore.sw.srv.lclick"):
            for line in statements(alias(name)):
                if (("hadd dccore.sws srv." in line or "hadd dccore.swl f." in line or "dccore.sw.srv.lfolder " in line)
                        and "$did(" in line):
                    stored += 1
                    for use in re.findall(r"(\$\w[\w.]*)\(\$did\(dccore\.setl,(?:2|7|9)\)\.text\)", line):
                        self.assertIn(use, ("$dccore.sw.enc", "$dccore.sw.tok"), line)
                    self.assertNotRegex(line, r"(?<!\()\$did\(dccore\.setl,(?:2|7|9)\)\.text(?!\))", line)
        self.assertGreaterEqual(stored, 3)

    def test_an_untouched_on_connect_line_goes_back_byte_for_byte(self):
        fill = alias("dccore.sw.oc.fill")
        self.assertIn("hadd dccore.sws oc.s. $+ %i $+(=,$dccore.sw.enc($did(dccore.set,1501,%i)))", fill)
        wire = statements(alias("dccore.sw.oc.wire"))
        self.assertIn("var %now = $+(=,$dccore.sw.enc($did(dccore.set,1501,$1))), %k = 1", wire)
        self.assertIn("if ($dccore.sw.s(oc.s. $+ %k) === %now) { return $mid($dccore.sw.s(oc.l. $+ %k),2) }",
                      wire)
        self.assertIn("dccore.send onconnect line %n $dccore.sw.oc.wire(%i)", alias("dccore.sw.oc.save"))

    def test_the_on_connect_model(self):
        """The same logic, run: what the bot sent, shown (and maybe closed up
        by did -a), read back, then sent - untouched lines as they came,
        edited lines as typed."""
        sent = ["PRIVMSG X :LOGIN alfa  two  spaces", "MODE %nick% +x", " lead"]
        stored = [console_settings.encode_value(c) for c in sent]
        shown = [collapse(mirc_dec(e)) for e in stored]             # what the box shows
        baseline = [mirc_enc(t) for t in shown]                      # oc.s.<n>
        edited = list(shown)
        edited[1] = "MODE %nick% +ix"

        def wire(text):
            now = mirc_enc(text)
            for n, base in enumerate(baseline):
                if base == now:
                    return stored[n]
            return now
        out = [wire(t) for t in edited]
        self.assertEqual(out[0], stored[0])
        self.assertEqual(console_settings.decode_value(out[0]), sent[0])
        self.assertEqual(console_settings.decode_value(out[1]), "MODE %nick% +ix")
        self.assertEqual(out[2], stored[2])

    # B. An untouched setting is never sent.
    def test_the_dirty_baseline_is_what_the_control_shows(self):
        fill = alias("dccore.sw.fill")
        self.assertIn("hadd dccore.sws d. $+ $1 = $+ $dccore.sw.shownenc($1)", fill)
        self.assertLess(fill.index("dccore.sw.put %id %v"), fill.index("$dccore.sw.shownenc($1)"))
        shownenc = statements(alias("dccore.sw.shownenc"))
        self.assertEqual(shownenc[-1], "return $dccore.sw.enc($did(dccore.set,$gettok($dccore.sw.m(k. $+ $1),1,32)).text)")

    def test_an_edit_is_sent_encoded_from_the_control(self):
        wire = statements(alias("dccore.sw.wire"))
        self.assertEqual(wire[-1], "return $dccore.sw.enc($did(dccore.set,%id).text)")
        # the line selected through dccore.sw.cb.sel: a menu is filled when
        # its page is first shown, its selection kept till then (#1382)
        self.assertIn("if (%kind == choice) && (!$dccore.sw.cb.sel(%id)) { return $mid($dccore.sw.s(v. $+ $1),2) }",
                      wire)
        self.assertIn("if (%kind == colour) && ($dccore.sw.cb.sel(%id) == 18) { return $mid($dccore.sw.s(v. $+ $1),2) }",
                      wire)
        for name in ("dccore.sw.apply", "dccore.sw.preview"):
            body = alias(name)
            self.assertNotIn("$dccore.sw.enc($dccore.sw.value(", body, name)
            self.assertIn("$dccore.sw.wire(", body, name)

    def test_the_settings_model(self):
        """Run: a value with a run of spaces, shown closed up by did -a. The
        baseline is the read-back, so leaving it alone sends nothing; typing
        sends exactly what was typed."""
        value = "a  b  "
        shown = collapse(value)
        baseline = mirc_enc(shown)
        self.assertEqual(mirc_enc(shown), baseline)                  # untouched: not dirty
        typed = "a   b c"
        self.assertNotEqual(mirc_enc(typed), baseline)
        self.assertEqual(console_settings.decode_value(mirc_enc(typed)), typed)

    # C. $input never runs inside a dialog event.
    def test_every_question_is_asked_from_a_timer(self):
        section = handwritten()
        asking = sorted(name for name in re.findall(r"\nalias (\S+) \{", section) if "$input(" in alias(name))
        self.assertEqual(asking, ["dccore.sw.confirm", "dccore.sw.leaveask", "dccore.sw.riskyask", "dccore.sw.unlockask"])
        for name in asking:
            timers = re.findall(r"\.timerdccoreSw(?:Ask|Unlock) -m 1 0 %s\b" % re.escape(name), section)
            self.assertTrue(timers, name)
            rest = section.replace("alias %s {" % name, "")
            for timer in set(timers):
                rest = rest.replace(timer, "")
            self.assertNotRegex(rest, r"(?<![\w.])%s(?![\w.])" % re.escape(name), "%s is called directly" % name)

    # The nits.
    def test_a_channels_refusal_is_named_by_its_label(self):
        data = generated_data()
        self.assertEqual(data["k.CHANNEL"].split()[3], "0")
        self.assertEqual(untext(data["n.CHANNEL"]), "Channels")
        self.assertEqual(statements(alias("dccore.sw.label")),
                         ["var %name = $dccore.sw.m(n. $+ $1)",
                          "return $iif(%name != $null,$dccore.sw.untext(%name),$1)"])

    def test_an_as_set_colour_shows_all_of_itself(self):
        # the whole value on the 18th line - added now, or when the menu is
        # filled if its page was not shown yet (#1382)
        fill = statements(alias("dccore.sw.colourfill"))
        self.assertIn("%x = As set: $2-", fill)
        self.assertIn("dccore.sw.cb.extra $1 %x", fill)
        self.assertIn("if ($2- != $null) { did -a dccore.set $1 $2- }", alias("dccore.sw.cb.extra"))
        self.assertIn("if ($dccore.sw.cb.waits($1)) { dccore.sw.keep cbx. $+ $1 $2- | return }",
                      alias("dccore.sw.cb.extra"))
        self.assertIn("if (%x != $null) { did -a dccore.set $1 %x }", alias("dccore.sw.cb.fill"))

    def test_a_new_start_forgets_the_old_waits(self):
        start = alias("dccore.sw.start")
        self.assertLess(start.index("hdel -w dccore.sws w.*"), start.index("dccore.sw.put 1013 Not connected"))

    def test_no_did_c_selects_line_0(self):
        """$findtok answers 0 for something it does not find."""
        self.assertNotRegex(handwritten(), r"did -c dccore\.set \S+ \$findtok\(")
        self.assertIn("if (!%at) { return $1 }", alias("dccore.sw.srv.modelabel"))
        self.assertEqual(statements(alias("dccore.sw.pickline")),
                         ["if ($2) { did -c dccore.set $1 $2 }", "else { did -u dccore.set $1 }"])



class ThePageButtons(unittest.TestCase):
    """The pages of a tab are a column of push-style radio buttons. They were a
    listbox, whose row height a script cannot set: at a display scale above
    100% its highlight was shorter than the text, which looked cut."""

    @classmethod
    def setUpClass(cls):
        cls.data = generated_data()
        cls.slots = int(cls.data["slots"])
        cls.controls = dialog("dccore.set")

    def test_there_is_no_page_list_any_more(self):
        self.assertNotIn("1010", code())
        self.assertFalse([c for c in self.controls if c[0] == "list" and c[3] < 106])

    def test_one_button_per_slot_enough_for_the_busiest_tab(self):
        import settings_window_layout as layout
        self.assertEqual(self.slots, max(len(pages) for _group, pages in layout.GROUPS))
        buttons = [c for c in self.controls if c[0] == "radio"]
        self.assertEqual([c[2] for c in buttons], sorted(slot_ids()))
        self.assertEqual([c[7] for c in buttons], ["push group"] + ["push"] * (self.slots - 1))
        for kind, _label, cid, x, y, w, h, _style in buttons:
            self.assertEqual((x, w), (10, 88), cid)
            self.assertGreaterEqual(h, 14, cid)
        tops = [c[4] for c in buttons]
        self.assertTrue(all(b - a >= buttons[0][6] for a, b in zip(tops, tops[1:])), tops)
        # air between the column and the page: the title beside the column's
        # edge looked squeezed against it
        import build_settings_window as generator
        self.assertGreaterEqual(generator.CONTENT_X - (generator.PAGE_SLOT_X + generator.PAGE_SLOT_W), 12)
        self.assertGreater(generator.DCX_TITLE[1], generator.DCX_PAGES[1])

    def test_every_page_name_fits_its_button(self):
        width = 88 * PX_PER_DBU
        for page in range(1, int(self.data["pages"]) + 1):
            name = untext(self.data["p.%d" % page])
            self.assertLessEqual(text_px(name) + 8, width, name)

    def test_every_page_is_reachable(self):
        """Each tab's pages fit its slots, and a click on slot N shows the
        tab's Nth page."""
        pages = set()
        for group in range(1, int(self.data["groups"]) + 1):
            numbers = self.data["g.%d.pages" % group].split()
            self.assertLessEqual(len(numbers), self.slots)
            pages.update(numbers)
        self.assertEqual(pages, {str(n) for n in range(1, int(self.data["pages"]) + 1)})
        click = alias("dccore.sw.click")
        self.assertIn("if (%id >= 1020) && (%id < $calc(1020 + $dccore.sw.m(slots))) {", click)
        self.assertIn("var %p = $gettok($dccore.sw.m(g. $+ $dccore.sw.s(tab) $+ .pages),$calc(%id - 1019),32)",
                      click)
        self.assertIn("if (%p) { dccore.sw.go page %p }", click)

    def test_a_tab_names_its_buttons_and_hides_the_spare_ones(self):
        body = statements(alias("dccore.sw.tab"))
        self.assertIn("while (%i <= $dccore.sw.m(slots)) {", body)
        self.assertIn("if (%i <= $numtok(%pages,32)) {", body)
        self.assertIn("did -ra dccore.set $calc(1019 + %i) $replace($dccore.sw.untext($dccore.sw.m(p. $+ "
                      "$gettok(%pages,%i,32))),&,&&)", body)
        # shown again only in the built-in window - with DCX the tree replaces them (#1288)
        self.assertIn("if (!$dccore.sw.s(dcx)) { did -v dccore.set $calc(1019 + %i) }", body)
        self.assertIn("else { did -h dccore.set $calc(1019 + %i) }", body)

    def test_the_page_shown_has_its_button_pressed_and_only_it(self):
        body = statements(alias("dccore.sw.page"))
        self.assertIn("var %at = $findtok($dccore.sw.m(g. $+ $dccore.sw.s(tab) $+ .pages),$1,1,32), %i = 1", body)
        self.assertIn("if (%i == %at) { did -c dccore.set $calc(1019 + %i) }", body)
        self.assertIn("else { did -u dccore.set $calc(1019 + %i) }", body)

    def test_the_slot_arithmetic_agrees_with_the_ids(self):
        """1019 + N is slot N's button, for every slot (the mSL above), and
        the generator starts the buttons at 1020."""
        import build_settings_window as generator
        self.assertEqual(generator.FIRST_SLOT_ID, 1020)
        for n in range(1, self.slots + 1):
            self.assertIn(1019 + n, slot_ids())

    def test_a_refused_setting_still_shows_its_page(self):
        goto = alias("dccore.sw.goto")
        self.assertIn("dccore.sw.tab %g", goto)
        self.assertIn("hadd dccore.sws last. $+ %g %page", goto)



class TheBrowseButtonsKnowTheBotsMachine(unittest.TestCase):
    """The "..." buttons pick a path on mIRC's computer, which is only the
    bot's when both are the same machine. A DCC chat to a bot on the same PC
    arrives from the public address, so the console's address cannot say;
    the bot ends its CAPS line with machine:<its name> and the window
    compares that with its own ($host)."""

    def caps(self, name):
        original = console_settings.machine_name
        console_settings.machine_name = lambda: name
        try:
            return console_settings.caps_line()
        finally:
            console_settings.machine_name = original

    def mirc_local(self, caps, ip, host):
        """dccore.sw.local and its two helpers, run in Python from the
        script's own statements."""
        self.assertEqual(statements(alias("dccore.sw.machine")),
                         ["return $gettok($wildtok($dccore.sw.s(caps),machine:*,1,32),2-,58)"])
        (myhost,) = statements(alias("dccore.sw.myhost"))
        match = re.fullmatch(r"return \$regsubex\(\$host,/(.+)/g,-\)", myhost)
        self.assertIsNotNone(match, myhost)
        self.assertEqual(statements(alias("dccore.sw.local")), [
            "if ($istok(127.0.0.1 ::1,$chat($dccore.bot).ip,32)) { return $true }",
            "var %bot = $dccore.sw.machine",
            "if (%bot == $null) || (%bot == -) { return $false }",
            "return $iif(%bot == $dccore.sw.myhost,$true,$false)"])
        if ip in ("127.0.0.1", "::1"):
            return True
        token = next((t for t in caps.split(" ") if t.lower().startswith("machine:")), "")
        bot = token.split(":", 1)[1] if ":" in token else ""
        if bot in ("", "-"):
            return False
        mine = re.sub(match.group(1), "-", host)
        return bot.lower() == mine.lower()                # mIRC's == ignores case

    def test_the_machine_token_ends_the_caps_line(self):
        line = self.caps("Desk-PC")
        self.assertTrue(line.startswith("CAPS settings:1 "))
        self.assertEqual(line.split(" ")[-1], "machine:Desk-PC")

    def test_the_same_machine_in_another_case_is_local(self):
        self.assertTrue(self.mirc_local(self.caps("DESK-PC"), "203.0.113.5", "desk-pc"))

    def test_a_name_with_a_space_is_made_one_token_both_sides(self):
        bot = "".join("-" if ch.isspace() or ch == ":" else ch for ch in "Desk PC")
        self.assertTrue(self.mirc_local(self.caps(bot), "203.0.113.5", "Desk PC"))

    def test_another_machine_is_not_local(self):
        self.assertFalse(self.mirc_local(self.caps("Desk-PC"), "203.0.113.5", "Laptop"))

    def test_an_older_bot_without_the_token_is_not_local(self):
        self.assertFalse(self.mirc_local("CAPS settings:1 preview:1 served:1 folders:1 onconnect:1 banlist:1",
                                         "203.0.113.5", "Desk-PC"))
        self.assertFalse(self.mirc_local(self.caps("-"), "203.0.113.5", "-"))

    def test_loopback_is_local_whatever_the_names(self):
        self.assertTrue(self.mirc_local("CAPS settings:1", "127.0.0.1", "Laptop"))
        self.assertTrue(self.mirc_local("CAPS settings:1", "::1", "Laptop"))

    def test_a_remote_bot_is_still_told_to_type_the_path(self):
        self.assertTrue(statements(alias("dccore.sw.browse"))[0].startswith(
            "if (!$dccore.sw.local) { dccore.sw.status The bot runs on another computer: type the path as it is there."))



class FakeSession:
    """Enough of a console session for console_settings to answer on."""

    def __init__(self):
        self.structured = True
        self.unlocked = False
        self.sent = []

    def send(self, line):
        self.sent.append(line)


class TheAuditOfBothHalves(unittest.TestCase):
    """What an audit of the bot side and the window together found."""

    # 1. The lock: a token login reads settings but does not change them
    #    until `unlock <password>`.

    LOCKABLE = ("setcommit", "set", "served commit", "folders commit", "onconnect commit", "onconnect resend",
                "onconnect", "checkupdates", "consolefeed", "pair", "unpair")

    def locked_line(self, what):
        session = FakeSession()
        self.assertTrue(console_settings.refused_while_locked(session, what))
        (line,) = session.sent
        self.assertTrue(line.startswith("DCCORE LOCKED "), line)
        return line

    def test_the_refused_command_is_read_from_the_right_words(self):
        """dccore.sw.locked's first statement, run on the bot's own LOCKED
        lines: one word, or two when the second is commit or resend."""
        first = statements(alias("dccore.sw.locked"))[0]
        self.assertEqual(first, "var %what = $iif($istok(commit resend,$2,32),$1-2,$1)")
        for what in self.LOCKABLE:
            with self.subTest(what=what):
                words = self.locked_line(what).split(" ")[2:]      # $2- of the line: the handler's $1-
                cmd = " ".join(words[:2]) if words[1] in ("commit", "resend") else words[0]
                self.assertEqual(cmd, what)

    def test_the_lock_line_typed_by_hand_is_said_in_the_window(self):
        body = alias("dccore.structured")
        self.assertIn("if (%type == LOCKED) { dccore.sys $dccore.bot $+ : $iif($istok(commit resend,$3,32),$4-,$3-) "
                      "| return }", body)
        self.assertIn("if (%type == UNLOCK) { dccore.sys Not unlocked: $3- | return }", body)
        line = self.locked_line("pair")
        words = line.split(" ")[1:]                                 # $1- of dccore.structured
        sentence = " ".join(words[3:] if words[2] in ("commit", "resend") else words[2:])
        self.assertTrue(sentence.startswith("Type unlock"), sentence)

    def test_a_refused_save_asks_for_the_password_from_a_timer(self):
        body = alias("dccore.sw.locked")
        self.assertIn("hadd dccore.sws relock %what", body)
        self.assertIn(".timerdccoreSwUnlock -m 1 0 dccore.sw.unlockask", body)
        setcommit = body[body.index("if (%what == setcommit) {"):]
        self.assertIn("dccore.sw.waited settings", setcommit)
        self.assertIn("hadd dccore.sws phase locked", setcommit)

    def test_the_on_connect_listing_says_locked_and_offers_unlock(self):
        body = alias("dccore.sw.locked")
        listing = body[body.index("if (%what == onconnect) {"):body.index("if (%what == setcommit) {")]
        self.assertIn("dccore.sw.put 1501 Locked - press Unlock to see the on-connect commands.", listing)
        self.assertIn("return", listing)
        self.assertNotIn("unlockask", listing)
        self.assertIn('button "Unlock", 1506,', block("dialog dccore.set {"))
        self.assertIn("if (%id == 1506) { dccore.sw.unlockbutton | return }", alias("dccore.sw.click"))

    def test_the_password_is_masked_and_goes_nowhere_but_the_unlock_line(self):
        body = statements(alias("dccore.sw.unlockask"))
        ask = next(line for line in body if "$input(" in line)
        # p: masked; no comma in the prompt. Never in a /var (#1281): the
        # answer goes straight into $dccore.sw.unlockarg, nested.
        match = re.fullmatch(r"dccore\.sw\.unlocksend \$dccore\.sw\.unlockarg\("
                             r"\$input\(([^,]+),po,DCCore - Unlock\)\)", ask)
        self.assertIsNotNone(match, ask)
        self.assertEqual(body, ["if (!$dialog(dccore.set)) { return }", ask])
        self.assertEqual(statements(alias("dccore.sw.unlocksend")), [
            "if ($1- == $null) { dccore.sw.unlockdrop Not saved: no password was given. | return }",
            "hadd dccore.sws unlocking 1",
            "dccore.sw.wait unlock",
            "dccore.send unlock $1-"])
        # held in no variable, no table, no echo - in none of the three aliases
        for name in ("dccore.sw.unlockask", "dccore.sw.unlockarg", "dccore.sw.unlocksend"):
            self.assertNotRegex(alias(name), r"\bvar\b|%pw|hadd[^\n]*\$1|echo|dccore\.sys", name)

    def test_a_typed_unlock_is_never_echoed(self):
        for window in ("@DCCore", "@DCCore-console"):
            self.assertIn("$iif($1 == unlock,unlock ********,$1-)", block("on *:INPUT:%s: {" % window))

    def test_unlocked_sends_the_refused_command_again(self):
        body = alias("dccore.sw.unlocked")
        self.assertIn("hadd dccore.live sw.unlocked 1", body)
        self.assertIn("dccore.send $dccore.sw.s(lastcommit)", body)
        self.assertIn("dccore.sw.reask onconnect", body)
        self.assertTrue(statements(body)[-1] == "dccore.send %what")
        self.assertIn("hadd dccore.sws lastcommit setcommit confirm", alias("dccore.sw.confirm"))
        self.assertIn("hadd dccore.sws lastcommit setcommit", alias("dccore.sw.apply"))

    def test_no_password_or_a_wrong_one_drops_the_transaction_and_keeps_the_edits(self):
        body = alias("dccore.sw.unlockdrop")
        self.assertIn("dccore.send setabort", body)
        self.assertIn("dccore.send $gettok(%what,1,32) abort", body)
        self.assertIn("Your changes are still here.", body)
        self.assertIn("dccore.sw.unlockdrop Not unlocked: $1-", alias("dccore.sw.unlockfailed"))

    def test_the_unlock_lasts_one_connection(self):
        self.assertIn("hdel dccore.live sw.unlocked", block("on *:CHATCLOSE: {"))
        hello = alias("dccore.structured")
        hello = hello[hello.index("if (%type == HELLO) {"):hello.index("if (%type == STATUS)")]
        self.assertIn("hdel dccore.live sw.unlocked", hello)

    # 2. SETAPPLIED: reload when the rehash has finished, not at SETDONE.

    def test_a_save_reloads_at_setapplied(self):
        done = alias("dccore.sw.done")
        ok = done[done.index("if ($1 == ok) {"):done.index("if ($1 == error) {", done.index("if ($1 == ok) {"))]
        written = ok[ok.index("if ($2 > 0) {"):]
        written = written[:written.index("return") + len("return")]
        self.assertIn("dccore.sw.rebase", written)
        self.assertIn("hadd dccore.sws applying $2", written)
        self.assertIn("Applying...", written)
        self.assertNotIn("dccore.sw.load", written)
        # a save that wrote nothing: no SETAPPLIED comes, reload at once
        self.assertLess(ok.index("if ($2 > 0) {"), ok.rindex("dccore.sw.load"))
        applied = alias("dccore.sw.line")
        applied = applied[applied.index("if (%t == SETAPPLIED) {"):]
        applied = applied[:applied.index("\n  }") + 4]
        self.assertIn("Saved and applied", applied)
        self.assertIn("dccore.sw.load", applied)
        self.assertLess(applied.index("$dccore.sw.changed != $null"), applied.index("dccore.sw.load"))

    def test_what_was_sent_is_the_baseline_until_then(self):
        self.assertIn("hadd dccore.sws sent %keys", alias("dccore.sw.apply"))
        self.assertIn("hadd dccore.sws d. $+ $gettok(%keys,%i,32) = $+ $dccore.sw.shownenc($gettok(%keys,%i,32))",
                      alias("dccore.sw.rebase"))

    # 3. A reply is the window's only when it is waiting for it.

    def test_only_awaited_replies_reach_the_window(self):
        wants = alias("dccore.sw.wants")
        for kind in statements(alias("dccore.sw.types"))[0].split(" ", 1)[1].split():
            self.assertRegex(wants, r"\b%s\b" % kind, kind)
        for fragment in ('if (%t == CAPS) { return $iif(%phase == caps,$true,$false) }',
                         'if ($istok(SETBEGIN SETF SETEND,%t,32)) { return $iif(%phase == load,$true,$false) }',
                         'if ($istok(SRVBEGIN SRVLIST SRVCHAN SRVFOLDER SRVEND,%t,32)) { return '
                         '$iif($dccore.sw.s(w.served) != $null,$true,$false) }',
                         'if ($istok(BANBEGIN BANP BANT BANEND,%t,32)) { return '
                         '$iif($dccore.sw.s(w.banlist) != $null,$true,$false) }',
                         'if (%t == SETAPPLIED) { return $iif($dccore.sw.s(applying) != $null,$true,$false) }'):
            self.assertIn(fragment, wants)
        self.assertEqual(statements(wants)[-1], "return $false")
        for flag, where in (("saving.srv", "dccore.sw.srv.save"), ("saving.oc", "dccore.sw.oc.save")):
            self.assertIn("hadd dccore.sws %s 1" % flag, alias(where))

    # 4. A reconnect keeps edits not saved yet.

    def test_a_reconnect_keeps_unsaved_edits(self):
        lost = alias("dccore.sw.lost")
        self.assertIn("if ($dccore.sw.changed != $null) || ($dccore.sw.unsaved != $null) { hadd dccore.sws keep 1 }", lost)
        self.assertLess(lost.index("keep 1"), lost.index("dccore.sw.start"))
        self.assertIn("if (!$1) || (!$dccore.sw.dirty($gettok(%keys,%i,32))) { dccore.sw.fill $gettok(%keys,%i,32) }",
                      alias("dccore.sw.fillall"))
        line = alias("dccore.sw.line")
        self.assertIn("dccore.sw.fillall $dccore.sw.s(keep)", line)
        self.assertIn("Reconnected - your unsaved changes are kept; press Reload to discard them.", line)
        self.assertIn("(!$dccore.sw.keeps($gettok(%asks,%i,32)))", alias("dccore.sw.page"))
        self.assertIn("hdel dccore.sws keep", alias("dccore.sw.reload"))
        self.assertNotIn("keep", alias("dccore.sw.start"))

    # 5. The public changelog names one version for dccore.mrc, and it is
    #    the version the script says it is.

    def test_the_public_changelog_names_one_script_version(self):
        # The public changelog is docs/UPDATES-PUBLIC.md here and
        # docs/UPDATES.md once extraction has renamed it onto that path
        # (docs/PUBLIC-REPO-WORKFLOW.md, step 3) - try both, in that order.
        public_changelog = os.path.join(REPO_ROOT, "docs", "UPDATES-PUBLIC.md")
        if not os.path.exists(public_changelog):
            public_changelog = os.path.join(REPO_ROOT, "docs", "UPDATES.md")
        with io.open(public_changelog, encoding="utf-8") as handle:
            text = handle.read()
        top, newest = script_versions_named(with_pending_public_entries(text))
        self.assertLessEqual(len(top), 1, "the top section asks for more than one dccore.mrc: %s" % sorted(top))
        self.assertEqual(newest, script_version(),
                         "the newest dccore.mrc the changelog asks for is not the one the script says it is")

    def test_a_new_section_without_a_script_bullet_still_passes(self):
        # Right after a release the next change opens "## Unreleased" above
        # the rolled version, usually without touching the script. The check
        # must still find the newest version below it, not demand one there.
        text = ("# Changelog\n\n## Unreleased\n\n- **Fixed: something else.**\n\n"
                "## v9.9.9 - Old\n\n- **New.** Update `dccore.mrc` to 1.2.3 and reload it.\n\n"
                "## v9.9.8 - Older\n\n- **New.** Update `dccore.mrc` to 1.2.2.\n")
        self.assertEqual(script_versions_named(text), (set(), "1.2.3"))

    def test_two_versions_in_one_section_are_both_seen(self):
        text = ("# Changelog\n\n## Unreleased\n\n- Update `dccore.mrc` to 1.2.4.\n"
                "- Update `dccore.mrc` to 1.2.5.\n\n## v9.9.9 - Old\n\n- Update `dccore.mrc` to 1.2.3.\n")
        self.assertEqual(script_versions_named(text), ({"1.2.4", "1.2.5"}, "1.2.4"))


SCRIPT_VERSION_NAMED = re.compile(r"`dccore\.mrc` to (\d+(?:\.\d+)+)")


def script_versions_named(text):
    """(versions the topmost "## " section asks for, newest one asked for
    anywhere). The topmost section is "## Unreleased" between releases and
    the just-rolled version right after one."""
    start = text.index("\n## ") + 1
    following = text.find("\n## ", start)
    top = text[start:following if following >= 0 else len(text)]
    named = SCRIPT_VERSION_NAMED.search(text, start)
    return set(SCRIPT_VERSION_NAMED.findall(top)), named.group(1) if named else None


def with_pending_public_entries(text):
    """The public changelog as the next release will read: the public
    fragments still waiting in docs/changes/ (#1307) as a section above the
    top one, in the order scripts/roll_changelog.py rolls them. The public
    tree has no docs/changes/, so there it is the file as it is."""
    sys.path.insert(0, os.path.join(REPO_ROOT, "scripts"))
    try:
        import roll_changelog
    finally:
        sys.path.remove(os.path.join(REPO_ROOT, "scripts"))
    fragments, _problems = roll_changelog.load(REPO_ROOT)
    public = sorted((f for f in fragments if f.public), key=lambda f: f.order, reverse=True)
    if not public:
        return text
    return roll_changelog.insert_release(text, "## Unreleased", "\n".join(f.text for f in public))


def script_version():
    with io.open(SCRIPT, encoding="utf-8") as handle:
        found = re.search(r"^alias dccore\.ver \{ return (\S+) \}$", handle.read(), re.M)
    return found.group(1) if found else None


# ---------------------------------------------------------------------------
# A lint over the whole script: /var and commas
# ---------------------------------------------------------------------------

def without_parens(text):
    """`text` without anything inside parentheses, at any depth."""
    out, depth = [], 0
    for ch in text:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth = max(0, depth - 1)
        elif depth == 0:
            out.append(ch)
    return "".join(out)


def var_statements(source):
    """Every `var` statement in `source`: one after a `|`, and one opening
    a one-line block (`if (...) { var ... }`), too."""
    found = []
    for line in source.split("\n"):
        for part in line.strip().split(" | "):
            statement = re.search(r"(?:^|\{ )(var(?: -\w+)? %.*)$", part.strip())
            if statement:
                found.append(statement.group(1).rstrip("}").strip())
    return found


def literal_commas(statement):
    """The values of a `var` statement that hold a literal comma. A comma
    inside an identifier's parentheses separates its arguments, and
    `, %name` starts the next variable: neither counts."""
    return [value for value in re.split(r", (?=%)", without_parens(statement)) if "," in value]


class NoVarHoldsALiteralComma(unittest.TestCase):
    """mIRC's /var reads a literal comma as the start of another variable
    (`var %a = 1, %b = 2`), so a sentence with a comma in it is cut short
    there (#1434's review)."""

    def test_no_var_in_the_script_has_one(self):
        found = var_statements(code())
        self.assertGreater(len(found), 100)
        for statement in found:
            with self.subTest(statement=statement[:80]):
                self.assertEqual(literal_commas(statement), [], statement)

    def test_the_check_bites(self):
        self.assertEqual(literal_commas("var %said = Saved, but not applied. $2-"),
                         ["var %said = Saved, but not applied. $2-"])
        self.assertEqual(literal_commas("var %a = $iif(%x,1,2), %b = 2"), [])
        self.assertEqual(var_statements("  if (%x) { var %a = 1 | var -n %b = 2 }"),
                         ["var %a = 1", "var -n %b = 2"])


if __name__ == "__main__":
    unittest.main()
