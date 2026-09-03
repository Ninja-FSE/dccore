"""Regression tests for irc.py trigger matching and connection scoping.

Two independent things are pinned down here.

1. ``irc.get_bot_aliases()`` - the fix for the 433 nick-collision blackout. The
   master list is written by ``update_list.py`` as a subprocess, so every pasted
   request line is stamped with the config.py default nick. While the bot ran as
   the alternate nick the dispatcher matched only the LIVE nick, so every
   "!DCCore Song.flac" was dropped silently.

2. The trigger expressions themselves. Rather than retyping them - which would
   make the tests agree with themselves and not with the daemon - the conditions
   are READ OUT OF irc.py and evaluated. If somebody edits the dispatch chain the
   tests here evaluate the edited condition, so a regression shows up as a
   failure instead of as a test that quietly stops describing the code.
"""

import os
import time
import unittest

from tests.support import (DCCoreTestCase, reset_config, install_fake_oserve,
                           silence_debug, no_disk_writes, queue_row,
                           CapturedDispatch, TempTree, RecordingSocket, DeadSocket)

import defaults as config
import irc


IRC_SOURCE_PATH = os.path.abspath(irc.__file__)
if IRC_SOURCE_PATH.endswith(".pyc"):  # pragma: no cover - defensive
    IRC_SOURCE_PATH = IRC_SOURCE_PATH[:-1]

with open(IRC_SOURCE_PATH, "r", encoding="utf-8") as _handle:
    IRC_LINES = _handle.read().splitlines()


# The two conditions this module evaluates, identified by a fragment unique to
# each. The live-nick fragment keeps its closing quote attached so it does NOT
# match the "-que"/"-remove" variants further down the chain.
ALIAS_FRAGMENT = 'startswith(f"!{alias} ")'
LIVE_NICK_FRAGMENT = 'msg_lower == f"@{config.NICKNAME.lower()}"'


def _elif_conditions(fragment=None):
    """Return (line_index, condition_source) for every ``elif`` line in irc.py.

    Only single-line ``elif ...:`` headers are considered; the dispatch chain is
    written that way throughout.
    """
    found = []
    for index, raw in enumerate(IRC_LINES):
        stripped = raw.strip()
        if not stripped.startswith("elif ") or not stripped.endswith(":"):
            continue
        if fragment is not None and fragment not in stripped:
            continue
        found.append((index, stripped[len("elif "):-1].strip()))
    return found


def _one_elif_condition(fragment):
    """The single dispatch-chain ``elif`` carrying ``fragment``."""
    matches = _elif_conditions(fragment)
    if len(matches) != 1:
        raise AssertionError(
            "expected exactly one 'elif' in irc.py containing %r, found %d"
            % (fragment, len(matches)))
    return matches[0]


def _flood_gate_source():
    """The ``is_bot_command = (...)`` expression, read out of irc.py."""
    start = None
    for index, raw in enumerate(IRC_LINES):
        if raw.strip() == "is_bot_command = (":
            start = index
            break
    if start is None:
        raise AssertionError("could not find the 'is_bot_command = (' gate in irc.py")
    body = []
    for raw in IRC_LINES[start + 1:]:
        stripped = raw.strip()
        if stripped == ")":
            return "(\n" + "\n".join(body) + "\n)"
        body.append(stripped)
    raise AssertionError("unterminated 'is_bot_command' expression in irc.py")


ALIAS_LINE, ALIAS_CONDITION = _one_elif_condition(ALIAS_FRAGMENT)
LIVE_NICK_LINE, LIVE_NICK_CONDITION = _one_elif_condition(LIVE_NICK_FRAGMENT)
FLOOD_GATE_SOURCE = _flood_gate_source()


def _evaluate(source, msg, target_chan=None):
    """Evaluate a condition lifted from irc.py against one PRIVMSG payload.

    ``msg``/``msg_lower``/``bot_aliases``/``target_chan`` are bound exactly as
    irc.py binds them at the point the condition runs, so the surrounding
    config state decides the outcome just as it does in the daemon. irc.py
    strips the payload before dispatching, so the corpus here is pre-stripped
    too.

    ``target_chan`` defaults to the bot's own nick - i.e. a private message -
    since that is what every existing corpus entry here implicitly assumes;
    only the DCC SEND clause (#219) reads it at all, and it needs an explicit
    value from a caller checking that one.
    """
    namespace = {
        "msg": msg,
        "msg_lower": msg.lower(),
        "bot_aliases": irc.get_bot_aliases(),
        "config": config,
        "target_chan": target_chan if target_chan is not None else config.NICKNAME,
    }
    return bool(eval(source, namespace))  # source comes from irc.py itself


class BotAliasTests(DCCoreTestCase):
    """irc.get_bot_aliases() - the nick-collision blackout fix."""

    def test_single_nick_collapses_to_one_alias(self):
        """Defect guard: widening the trigger must not change normal operation.

        With NICKNAME == ORIGINAL_NICK the alias set is one entry, so the trigger
        built from it is byte-identical to the old hardcoded "!dccore " one.
        """
        reset_config(NICKNAME="DCCore", ORIGINAL_NICK="DCCore")
        self.assertEqual(irc.get_bot_aliases(), ["dccore"])

    def test_fallback_returns_both_lowercased_current_first(self):
        """Defect: after a 433 fallback the bot ignored every "!DCCore <file>".

        The master list is stamped by the update_list.py subprocess with the
        config.py default nick, so ORIGINAL_NICK has to stay answerable.
        """
        reset_config(NICKNAME="DCCore_", ORIGINAL_NICK="DCCore")
        self.assertEqual(irc.get_bot_aliases(), ["dccore_", "dccore"])

    def test_list_base_name_is_not_an_alias(self):
        """Defect guard: LIST_BASE_NAME is a filename constant, not a nick.

        Letting it become a live public trigger would let the list filename
        dictate what the bot answers to on IRC.
        """
        reset_config(NICKNAME="DCCore", ORIGINAL_NICK="DCCore",
                     LIST_BASE_NAME="MusicPack")
        aliases = irc.get_bot_aliases()
        self.assertEqual(aliases, ["dccore"])
        self.assertNotIn("musicpack", aliases)

    def test_previous_nick_is_honoured_when_present(self):
        """Defect guard: !rehash renames the bot; PREVIOUS_NICK keeps the request
        lines of the already-distributed list working until it is regenerated."""
        reset_config(NICKNAME="DCCore2", ORIGINAL_NICK="DCCore2",
                     PREVIOUS_NICK="DCCore")
        self.assertEqual(irc.get_bot_aliases(), ["dccore2", "dccore"])

    def test_previous_nick_absent_is_not_an_error(self):
        """Defect guard: PREVIOUS_NICK only exists after a rehash rename, so the
        attribute is normally missing entirely and must be tolerated."""
        reset_config()
        self.assertFalse(hasattr(config, "PREVIOUS_NICK"))
        self.assertEqual(irc.get_bot_aliases(), ["dccore"])

    def test_blank_values_are_skipped(self):
        """Defect guard: an empty or whitespace nick must not become the alias
        "" - "!<empty> " would degrade into a bare "! " prefix trigger."""
        reset_config(NICKNAME="DCCore", ORIGINAL_NICK="", PREVIOUS_NICK="   ")
        self.assertEqual(irc.get_bot_aliases(), ["dccore"])
        self.assertNotIn("", irc.get_bot_aliases())

    def test_none_values_are_skipped(self):
        """Defect guard: a None nick must be dropped, not stringified to "none",
        which would make "!none <file>" a live public trigger."""
        reset_config(NICKNAME="DCCore", ORIGINAL_NICK=None, PREVIOUS_NICK=None)
        self.assertEqual(irc.get_bot_aliases(), ["dccore"])
        self.assertNotIn("none", irc.get_bot_aliases())

    def test_aliases_are_deduplicated(self):
        """Defect guard: duplicates would make the "!<alias> " generator match the
        same message repeatedly, and would double-dispatch a request the moment
        the chain stopped short-circuiting."""
        reset_config(NICKNAME="DCCore", ORIGINAL_NICK="dccore",
                     PREVIOUS_NICK="DCCORE")
        self.assertEqual(irc.get_bot_aliases(), ["dccore"])

    def test_case_and_whitespace_are_normalised(self):
        """Defect guard: aliases are compared against a lowercased message, so an
        unlowered or padded nick would never match anything at all."""
        reset_config(NICKNAME="  DcCoRe_  ", ORIGINAL_NICK="DCCore")
        self.assertEqual(irc.get_bot_aliases(), ["dccore_", "dccore"])


class TriggerExpressionTests(DCCoreTestCase):
    """The dispatch conditions, evaluated as they are actually written."""

    def test_conditions_were_lifted_from_the_source(self):
        """Guard on the test method itself: if the dispatch chain is rewritten so
        these fragments no longer appear, this module must fail loudly rather
        than silently testing nothing."""
        self.assertIn("bot_aliases", ALIAS_CONDITION)
        self.assertIn("config.NICKNAME", LIVE_NICK_CONDITION)
        self.assertIn("bot_aliases", FLOOD_GATE_SOURCE)
        # The alias dispatch sits in the chain, below the list-request branch.
        self.assertGreater(ALIAS_LINE, LIVE_NICK_LINE)

    # --- normal operation: identical to the old hardcoded trigger -----------

    def test_main_nick_alias_trigger_matches_as_before(self):
        """Defect guard: on the main nick the alias trigger must behave exactly
        like the old hardcoded 'msg_lower.startswith("!dccore ")'."""
        reset_config(NICKNAME="DCCore", ORIGINAL_NICK="DCCore")
        for msg in ("!DCCore Song.flac",
                    "!dccore Artist/Album/Song.flac",
                    "!DcCoRe !rar Artist/Album",
                    "!DCCore  spaced.flac",
                    "!DCCoreX Song.flac",
                    "!DCCore",
                    "hello !DCCore Song.flac",
                    "!other Song.flac",
                    "@DCCore"):
            with self.subTest(msg=msg):
                self.assertEqual(_evaluate(ALIAS_CONDITION, msg),
                                 msg.lower().startswith("!dccore "))

    def test_main_nick_list_request_matches(self):
        """Defect guard: "@<nick>" is the master-list request and stays an exact
        match, so "@DCCore-que" is not swallowed by it."""
        reset_config(NICKNAME="DCCore", ORIGINAL_NICK="DCCore")
        self.assertTrue(_evaluate(LIVE_NICK_CONDITION, "@DCCore"))
        self.assertTrue(_evaluate(LIVE_NICK_CONDITION, "@dccore"))
        for msg in ("@DCCore-que", "@DCCore-remove", "@DCCore please",
                    "@DCCoreX", "DCCore"):
            with self.subTest(msg=msg):
                self.assertFalse(_evaluate(LIVE_NICK_CONDITION, msg))

    # --- during a 433 fallback ---------------------------------------------

    def test_fallback_alias_request_dispatches(self):
        """Defect: running as "DCCore_", every "!DCCore Song.flac" pasted from the
        master list was dropped with no reply and no log line."""
        reset_config(NICKNAME="DCCore_", ORIGINAL_NICK="DCCore")
        self.assertTrue(_evaluate(ALIAS_CONDITION, "!DCCore Song.flac"))
        self.assertTrue(_evaluate(ALIAS_CONDITION, "!DCCore_ Song.flac"))
        # The pre-fix, live-nick-only trigger would have rejected the first one,
        # which is exactly the blackout this fix removed.
        self.assertFalse("!DCCore Song.flac".lower().startswith(
            "!" + config.NICKNAME.lower() + " "))

    def test_fallback_list_request_stays_live_nick_only(self):
        """Defect guard, deliberate asymmetry: "@<nick>" is typed live and the
        advert publishes the LIVE nick, so during a fallback "@DCCore" addresses
        whichever client now holds the main nick - the bot must not answer it."""
        reset_config(NICKNAME="DCCore_", ORIGINAL_NICK="DCCore")
        self.assertFalse(_evaluate(LIVE_NICK_CONDITION, "@DCCore"))
        self.assertFalse(_evaluate(LIVE_NICK_CONDITION, "@dccore"))
        self.assertTrue(_evaluate(LIVE_NICK_CONDITION, "@DCCore_"))

    def test_fallback_rejects_near_miss_nicks(self):
        """Defect guard: widening to an alias set must not turn the trigger into
        a loose prefix match - "!DCCoreX" belongs to a different bot."""
        reset_config(NICKNAME="DCCore_", ORIGINAL_NICK="DCCore")
        for msg in ("!DCCoreX Song.flac", "!DCCore_X Song.flac",
                    "!DCCore-que Song.flac", "!DCCore", "!DCCore_",
                    "!DCC Song.flac"):
            with self.subTest(msg=msg):
                self.assertFalse(_evaluate(ALIAS_CONDITION, msg))

    def test_previous_nick_request_dispatches_after_rehash(self):
        """Defect guard: after a !rehash rename the lists already in users' hands
        still carry the old nick and must keep working."""
        reset_config(NICKNAME="DCCore2", ORIGINAL_NICK="DCCore2",
                     PREVIOUS_NICK="DCCore")
        self.assertTrue(_evaluate(ALIAS_CONDITION, "!DCCore Song.flac"))
        self.assertTrue(_evaluate(ALIAS_CONDITION, "!DCCore2 Song.flac"))

    def test_alias_branch_is_actually_reachable(self):
        """Defect guard: the alias dispatch is the LAST elif in the chain, so an
        earlier branch matching "!DCCore <file>" would shadow it completely."""
        reset_config(NICKNAME="DCCore_", ORIGINAL_NICK="DCCore")
        msg = "!DCCore Song.flac"
        shadowing = []
        for index, condition in _elif_conditions():
            if not (LIVE_NICK_LINE <= index < ALIAS_LINE):
                continue
            try:
                matched = _evaluate(condition, msg)
            except NameError:
                # Branch guarded by names bound elsewhere in the read loop
                # (ctcp_cmd, line, ...); not a plain message predicate.
                continue
            if matched:
                shadowing.append(condition)
        self.assertEqual(shadowing, [],
                         "an earlier elif shadows the alias download trigger")


class FloodGateCoverageTests(DCCoreTestCase):
    """The is_bot_command gate must meter every request trigger it dispatches."""

    def _gate(self, msg):
        return _evaluate(FLOOD_GATE_SOURCE, msg)

    def test_gate_covers_every_public_request_trigger(self):
        """Defect guard: a gate narrower than the dispatch is an unmetered
        command path - which is what pre-fix fallback traffic was, neither
        dispatched nor even seen by the flood counter."""
        for nickname, original in (("DCCore", "DCCore"), ("DCCore_", "DCCore")):
            reset_config(NICKNAME=nickname, ORIGINAL_NICK=original)
            for msg in ("@" + nickname, "@find metallica", "@locator metallica",
                        "!" + nickname + " Song.flac", "!DCCore Song.flac"):
                with self.subTest(nick=nickname, msg=msg):
                    self.assertTrue(self._gate(msg))

    def test_gate_is_never_narrower_than_the_dispatch(self):
        """Defect guard: every message the alias or list-request branch would
        dispatch must first have passed through the flood meter."""
        corpus = ["!DCCore Song.flac", "!DCCore_ Song.flac", "!dccore x",
                  "!DCCore !rar Artist/Album", "!DCCoreX Song.flac",
                  "!DCCore2 Song.flac", "@DCCore", "@DCCore_", "@DCCore2",
                  "@find x", "@locator x", "hello world", "!ping"]
        for nickname, original, previous in (("DCCore", "DCCore", None),
                                             ("DCCore_", "DCCore", None),
                                             ("DCCore2", "DCCore2", "DCCore")):
            kwargs = {"NICKNAME": nickname, "ORIGINAL_NICK": original}
            if previous:
                kwargs["PREVIOUS_NICK"] = previous
            reset_config(**kwargs)
            for msg in corpus:
                with self.subTest(nick=nickname, msg=msg):
                    if _evaluate(ALIAS_CONDITION, msg):
                        self.assertTrue(self._gate(msg))
                    if _evaluate(LIVE_NICK_CONDITION, msg):
                        self.assertTrue(self._gate(msg))

    def test_gate_is_not_wider_than_the_dispatch(self):
        """Defect guard: a gate wider than the dispatch charges users flood points
        for messages the bot ignores - during a fallback "@DCCore" is not
        answered, so it must not be metered either."""
        reset_config(NICKNAME="DCCore_", ORIGINAL_NICK="DCCore")
        self.assertFalse(_evaluate(LIVE_NICK_CONDITION, "@DCCore"))
        self.assertFalse(self._gate("@DCCore"))
        self.assertFalse(self._gate("just chatting about DCCore"))
        self.assertFalse(self._gate("!DCCoreX Song.flac"))

    def test_a_private_dcc_send_ctcp_is_metered(self):
        """#219: dcc_fetch.handle_incoming_offer() is dispatched to a thread
        for any DCC SEND CTCP sent privately to the bot - its own admission
        control only decides afterward whether the offer was solicited, so
        deciding that costs work regardless. Nothing bounded how many of these
        any single, non-banned user could send until this clause existed."""
        reset_config(NICKNAME="DCCore", ORIGINAL_NICK="DCCore")
        offer = "\x01DCC SEND Track.flac 3232235521 5001 12345\x01"

        self.assertTrue(_evaluate(FLOOD_GATE_SOURCE, offer, target_chan="DCCore"))

    def test_a_dcc_send_ctcp_addressed_to_a_channel_is_not_metered(self):
        """The control: the real dispatch only ever acts on a DCC SEND
        addressed to the bot itself (a copy landing in a channel is
        meaningless - any member could have sent it), so the gate must not
        meter what the dispatch would not act on either."""
        reset_config(NICKNAME="DCCore", ORIGINAL_NICK="DCCore")
        offer = "\x01DCC SEND Track.flac 3232235521 5001 12345\x01"

        self.assertFalse(_evaluate(FLOOD_GATE_SOURCE, offer, target_chan="#chan"))

    def test_gate_uses_the_same_alias_set_as_the_dispatch(self):
        """Defect guard: the gate and the dispatch must be built from the same
        get_bot_aliases() call, not from two hardcoded lists that can drift."""
        self.assertIn("bot_aliases", FLOOD_GATE_SOURCE)
        self.assertIn("bot_aliases", ALIAS_CONDITION)
        reset_config(NICKNAME="DCCore2", ORIGINAL_NICK="DCCore2",
                     PREVIOUS_NICK="DCCore")
        self.assertTrue(_evaluate(ALIAS_CONDITION, "!DCCore Song.flac"))
        self.assertTrue(self._gate("!DCCore Song.flac"))


class ConnectionScopeTests(DCCoreTestCase):
    """irc._release_socket() - the stale shared-socket reference."""

    def test_release_socket_clears_the_shared_reference(self):
        """Defect: queue_mgr and announce.send_debug both guard on
        "if current_sock:", so a reference left pointing at a CLOSED socket made
        those guards useless for the whole reconnect."""
        self.oserve.irc_connection = RecordingSocket()
        irc._release_socket()
        self.assertIsNone(self.oserve.irc_connection)

    def test_release_socket_survives_a_dead_socket(self):
        """Defect guard: the reference is dropped without touching the socket, so
        a peer that vanished in a netsplit cannot raise out of the cleanup."""
        self.oserve.irc_connection = DeadSocket()
        irc._release_socket()
        self.assertIsNone(self.oserve.irc_connection)

    def test_release_socket_without_oserve_is_a_no_op(self):
        """Defect guard: _release_socket also runs during teardown, where the
        oserve module may already be gone; it must not raise."""
        import sys
        saved = sys.modules.pop("oserve", None)
        try:
            irc._release_socket()
        finally:
            if saved is not None:
                sys.modules["oserve"] = saved


class BroadcastSearchCaptureTests(DCCoreTestCase):
    """irc._capture_broadcast_search_reply() - the cross-bot search broadcast
    capture hook. webserver.py's POST /api/search/broadcast is what opens the
    window this reads; here the window is opened directly on config so the
    capture function itself is what's under test."""

    def setUp(self):
        super().setUp()
        config.NICKNAME = "DCCore"
        config.broadcast_search_inprogress = True
        config.broadcast_search_deadline = __import__("time").time() + 30
        config.broadcast_search_results = []

    def test_a_reply_addressed_to_our_nick_is_captured(self):
        irc._capture_broadcast_search_reply("OtherBot", "DCCore", "Some raw reply text")
        self.assertEqual(len(config.broadcast_search_results), 1)
        entry = config.broadcast_search_results[0]
        self.assertEqual(entry["from"], "OtherBot")
        self.assertEqual(entry["text"], "Some raw reply text")
        self.assertNotIn("bot", entry)

    def test_target_match_is_case_insensitive(self):
        irc._capture_broadcast_search_reply("OtherBot", "dccore", "hello")
        self.assertEqual(len(config.broadcast_search_results), 1)

    def test_channel_chatter_is_not_captured(self):
        irc._capture_broadcast_search_reply("SomeUser", "#dccore-test", "just chatting")
        self.assertEqual(config.broadcast_search_results, [])

    def test_nothing_is_captured_outside_an_open_window(self):
        config.broadcast_search_inprogress = False
        irc._capture_broadcast_search_reply("OtherBot", "DCCore", "hello")
        self.assertEqual(config.broadcast_search_results, [])

    def test_nothing_is_captured_after_the_deadline_even_if_flag_is_stale(self):
        config.broadcast_search_deadline = __import__("time").time() - 1
        irc._capture_broadcast_search_reply("OtherBot", "DCCore", "hello")
        self.assertEqual(config.broadcast_search_results, [])

    def test_control_codes_are_stripped_from_captured_text(self):
        irc._capture_broadcast_search_reply("OtherBot", "DCCore", "\x0304,05Song.flac\x0f\x02!\x02")
        entry = config.broadcast_search_results[0]
        self.assertNotIn("\x03", entry["text"])
        self.assertNotIn("\x02", entry["text"])
        self.assertNotIn("\x0f", entry["text"])

    def test_a_bot_filename_token_is_extracted_when_present(self):
        irc._capture_broadcast_search_reply("OtherBot", "DCCore", "!OtherBot Enter Sandman.flac")
        entry = config.broadcast_search_results[0]
        self.assertEqual(entry["bot"], "OtherBot")
        self.assertEqual(entry["filename"], "Enter Sandman.flac")

    def test_the_info_marker_is_stripped_regardless_of_which_bot_sent_it(self):
        """BUG regression: a broadcast-search reply is very often itself a
        master-list line, which carries a trailing "::INFO:: <size and
        branding>" tag - but bots on the network do not agree on the
        whitespace around that marker, or on what they append after it.
        list.strip_info_suffix() only handled this project's own exact
        "  ::INFO:: " (two spaces) convention, so anything from a
        differently-formatted bot kept its entire size/branding tail
        attached to the "filename" - meaning the real DCC SEND offer that
        later came back (bearing only the bare filename) never matched it,
        and the fetch was rejected as unsolicited. These are drawn from
        genuine observed traffic, one per differently-formatted bot on the
        network, each with its own spacing/branding convention.
        """
        real_world_replies = {
            "AvenIo": (
                "!AvenIo Hi-Res Masters 1984 - 38 - Metallica - Ride The "
                "Lightning (Remastered).flac ::INFO:: 153.03MB © OmeNServE v2.60 ©",
                "Hi-Res Masters 1984 - 38 - Metallica - Ride The Lightning (Remastered).flac",
            ),
            "Oakwood": (
                "!Oakwood 092 - Metallica - Until It Sleeps.mp3 ::INFO:: "
                "6.32Mb 4m30s 192/44.10/JS  OmeNServE v2.60",
                "092 - Metallica - Until It Sleeps.mp3",
            ),
            "[rigserv]": (
                "![rigserv] 047. Metallica - Master Of Puppets (Remastered).mp3 "
                "::INFO:: 19.95MB : OmenServe v2.71 :",
                "047. Metallica - Master Of Puppets (Remastered).mp3",
            ),
            "quirkz": (
                "!quirkz 101-metallica-the-ecstasy_of_gold.mp3 ::INFO:: "
                "3.7MB 2m1s VBR/256/44.1/JS * OmenServe v2.71 *",
                "101-metallica-the-ecstasy_of_gold.mp3",
            ),
        }
        for bot, (raw_reply, expected_filename) in real_world_replies.items():
            with self.subTest(bot=bot):
                config.broadcast_search_results = []
                irc._capture_broadcast_search_reply("OtherBot", "DCCore", raw_reply)
                entry = config.broadcast_search_results[0]
                self.assertEqual(entry["bot"], bot)
                self.assertEqual(entry["filename"], expected_filename)
                self.assertNotIn("::INFO::", entry["filename"])

    def test_no_token_means_no_bot_filename_fields(self):
        irc._capture_broadcast_search_reply("OtherBot", "DCCore", "Found 3 matches, use my list command")
        entry = config.broadcast_search_results[0]
        self.assertNotIn("bot", entry)
        self.assertNotIn("filename", entry)

    def test_our_own_outbound_broadcast_cannot_capture_itself(self):
        """The PRIVMSG dispatch's existing self-skip (user.lower() ==
        config.NICKNAME.lower(): continue) runs BEFORE the capture call, so
        our own outbound "@find <term>" bouncing back from the server as our
        own PRIVMSG is never even offered to this function. Documented here
        by exercising the function the way the self-skip guarantees it is
        actually reached: never with our own nick as `user`."""
        before = list(config.broadcast_search_results)
        # Even if it WERE reached with our own nick as sender, the function
        # itself has no special-case for that - the guarantee lives in the
        # call site (irc.py's `if user.lower() == config.NICKNAME.lower():
        # continue`, which sits above every call to this function), not here.
        irc._capture_broadcast_search_reply(config.NICKNAME, config.NICKNAME, "@find sandman")
        self.assertEqual(len(config.broadcast_search_results), len(before) + 1)



class BroadcastRepliesFromRealBots(DCCoreTestCase):
    """Captured from a live @find in a real channel, not invented.

    Every bot answering an @find sends a HEADER line and then one line per
    match, and the header contains a "!" token too - it is telling the user
    what to type:

        Search Result 1 Match For X  Copy And Paste !Echosonic FILENAME To ...
        !Echosonic 50 Oldies Party - ... .mp3  ::INFO:: 4.6MB

    The extraction searched anywhere in the line, so the header matched as
    well and produced a Download button for a file literally named
    "FILENAME To The Channel To Request. (25/25) Free Slots...".

    Ordinary channel chatter arriving inside the 30-second window was worse.
    A KeepTrack thank-you note - "Thank You !!! I have now received 1
    file(s)..." - yielded bot="!!" and offered to fetch from it.

    Both would have sent a nonsense "!" line into a live public channel on
    click. Anchoring the token to the start of the line separates them: a
    result line always begins with its token, a sentence mentioning one
    never does.
    """

    # Verbatim from a broadcast for "Testament Souls" in #dccore-test.
    HEADERS = [
        ("Echosonic", "Search Result 1 Match For Testament Souls Copy And Paste "
                      "!Echosonic FILENAME To The Channel To Request. (25/25) "
                      "Free Slots, 0 Queued OmeNServE v2.60"),
        ("ValOgg", "Search Result ~ 2 Matches For Testament Souls ~ Copy And "
                   "Paste !ValOgg FILENAME To The Channel To Request. (5/5) "
                   "Free Slots, 0 Queued OmeNServE v2.60"),
        ("[rigserv]", "Search Result : 1 Match For Testament Souls : Copy And "
                     "Paste ![rigserv] FILENAME To The Channel To Request. (6/6)"),
    ]

    RESULTS = [
        ("Echosonic", "!Echosonic 50 Oldies Party - 29614 - Testament - Souls Of "
                      "Black.mp3  ::INFO:: 4.6MB OmeNServE v2.60",
         "50 Oldies Party - 29614 - Testament - Souls Of Black.mp3"),
        ("`Glider", "!`Glider Testament - Original Album Series - 404 - Souls "
                     "Of Black.mp3  ::INFO:: 7.7MB OmeNServE v2.60",
         "Testament - Original Album Series - 404 - Souls Of Black.mp3"),
        ("[rigserv]", "![rigserv] Testament (1990) Souls Of Black.rar  ::INFO:: "
                     "89.92MB : OmenServe v2.71 :",
         "Testament (1990) Souls Of Black.rar"),
        ("kurtb66", "!kurtb66 14 - Testament - Souls Of Black.mp3  ::INFO:: "
                    "4.8MB OmenServe v2.71",
         "14 - Testament - Souls Of Black.mp3"),
    ]

    CHATTER = [
        "Thank You !!! I have now received 1 file(s) 702 Kb from you, for a "
        "total of 24,024 file(s) 104 GbB leeched since 31st December 2008 "
        "KeepTrack 6.2 by ^OmeN^",
        "Matches for *Testament*Souls* Copy and Paste in Channel to Request a "
        "File (Slot:0/) (Que:0/16) in Use",
    ]

    def setUp(self):
        super().setUp()
        config.NICKNAME = "DCCore"
        config.broadcast_search_inprogress = True
        config.broadcast_search_deadline = time.time() + 30
        config.broadcast_search_results = []

    def _capture(self, sender, text):
        irc._capture_broadcast_search_reply(sender, "DCCore", text)
        return config.broadcast_search_results[-1]

    def test_a_header_line_offers_no_download(self):
        """The header names the bot mid-sentence and uses the literal word
        FILENAME as a placeholder. Offering to fetch that sends the words
        "FILENAME To The Channel To Request." into a public channel."""
        for sender, text in self.HEADERS:
            with self.subTest(bot=sender):
                entry = self._capture(sender, text)
                self.assertNotIn("filename", entry,
                                 f"header line offered a download: {entry.get('filename')!r}")

    def test_unrelated_chatter_offers_no_download(self):
        """A KeepTrack thank-you note landed mid-window and produced
        bot="!!" - a fetch request addressed to a bot that does not exist."""
        for text in self.CHATTER:
            with self.subTest(text=text[:40]):
                entry = self._capture("SomeUser", text)
                self.assertNotIn("filename", entry)
                self.assertNotIn("bot", entry)

    def test_a_real_result_still_offers_the_download(self):
        """The control, and the reason the fix cannot simply drop anything
        containing a header-ish phrase: these are what the feature is for."""
        for sender, text, expected in self.RESULTS:
            with self.subTest(bot=sender):
                entry = self._capture(sender, text)
                self.assertEqual(entry.get("bot"), sender)
                self.assertEqual(entry.get("filename"), expected)

    def test_awkward_nicks_survive(self):
        """Real nicks in these channels carry punctuation - a backtick, square
        brackets. The token is whitespace-delimited on purpose so those keep
        working rather than being split apart."""
        entry = self._capture("`Glider", self.RESULTS[1][1])
        self.assertEqual(entry.get("bot"), "`Glider")

        entry = self._capture("[rigserv]", self.RESULTS[2][1])
        self.assertEqual(entry.get("bot"), "[rigserv]")

    def test_every_reply_is_still_recorded_either_way(self):
        """Nothing is dropped. A line with no usable token is still shown to
        the operator, just without a button - the header lines carry the free
        slot counts and queue depth, which are worth reading."""
        for _sender, text in self.HEADERS:
            self._capture("SomeBot", text)
        for text in self.CHATTER:
            self._capture("SomeUser", text)

        self.assertEqual(len(config.broadcast_search_results),
                         len(self.HEADERS) + len(self.CHATTER))
        for entry in config.broadcast_search_results:
            self.assertTrue(entry["text"], "the raw reply text was lost")



class SearchHeaderStats(DCCoreTestCase):
    """irc.parse_search_header() - reading the line a bot sends before its
    matches, so it can become the heading above them instead of a row.

    Every fixture here is verbatim from a live @find in #dccore-test. Two bot
    families answer in these channels and they report different things, which
    is why nothing below assumes a field is present.
    """

    def test_an_omenserve_header(self):
        stats = irc.parse_search_header(
            "Search Result 4 Matches For Testament Souls Copy And Paste "
            "!kurtb66 FILENAME To The Channel To Request. (10/10) Free Slots, "
            "0 Queued OmenServe v2.71")

        self.assertEqual(stats["family"], "omenserve")
        self.assertEqual(stats["matches"], 4)
        self.assertEqual(stats["slots_free"], 10)
        self.assertEqual(stats["slots_total"], 10)
        self.assertEqual(stats["queued"], 0)
        self.assertEqual(stats["server"], "OmenServe v2.71")

    def test_operators_pick_their_own_separators(self):
        """The same format, decorated with ':', '~' or '*' between sections.
        Nothing may anchor on punctuation."""
        for sep in (":", "~", "*"):
            with self.subTest(separator=sep):
                stats = irc.parse_search_header(
                    f"Search Result {sep} 1 Match For X {sep} Copy And Paste "
                    f"!bot FILENAME To The Channel To Request. (2/2) Free "
                    f"Slots, 0 Queued {sep} OmeNServE v2.60 {sep}")
                self.assertEqual(stats["matches"], 1)
                self.assertEqual(stats["slots_free"], 2)
                self.assertEqual(stats["server"], "OmeNServE v2.60")

    def test_a_bot_that_found_more_than_it_sent(self):
        """Beezer answers with a list size and a truncation notice instead of
        slot counts. The gap between 'matches' and what arrives is the whole
        reason to show it - otherwise five looks like all there is."""
        stats = irc.parse_search_header(
            "Search Result  12 Matches For Testament Souls   Get My List Of "
            "94,952 Files By Typing @Beezer In The Channel Or Refine Your "
            "Search. Sending first 5 Results   OmeNServE v2.60")

        self.assertEqual(stats["matches"], 12)
        self.assertEqual(stats["sending"], 5)
        self.assertEqual(stats["list_size"], 94952)
        self.assertNotIn("slots_free", stats, "this header reports no slots - "
                                              "absent must not become zero")

    def test_an_spqr_header(self):
        """SPQR is an older mIRC script, still run by a minority of operators.
        It reports slots IN USE rather than free, gives a queue capacity, and
        carries no version string or match count at all."""
        stats = irc.parse_search_header(
            "Matches for *Testament*Souls* Copy and Paste in Channel to "
            "Request a File (Slot:0/) (Que:0/16) in Use")

        self.assertEqual(stats["family"], "spqr")
        self.assertEqual(stats["slots_in_use"], 0)
        self.assertEqual(stats["queued"], 0)
        self.assertEqual(stats["queue_total"], 16)
        self.assertNotIn("slots_free", stats,
                         "in-use slots must never be reported as free")
        self.assertNotIn("server", stats)
        self.assertNotIn("matches", stats)

    def test_a_result_line_is_never_a_header(self):
        """A match ends in the same version string inside its ::INFO:: tail,
        so without an explicit guard it parses as a header and every result
        row would carry the sender's stats."""
        self.assertIsNone(irc.parse_search_header(
            "!Echosonic 50 Oldies Party - Testament.mp3  ::INFO:: 4.6MB "
            "OmeNServE v2.60"))

    def test_an_spqr_result_line_is_never_a_header(self):
        """SPQR matches carry no ::INFO:: tag at all - a bare token and a
        filename."""
        self.assertIsNone(irc.parse_search_header(
            "!BigRig Testament - Souls of Black.mp3"))

    def test_unrelated_chatter_is_not_a_header(self):
        self.assertIsNone(irc.parse_search_header(
            "Thank You !!! I have now received 1 file(s) 702 Kb from you, for "
            "a total of 24,024 file(s) 104 GbB leeched since 31st December "
            "2008 KeepTrack 6.2 by ^OmeN^"))
        self.assertIsNone(irc.parse_search_header(""))
        self.assertIsNone(irc.parse_search_header("just some words"))

    def test_the_capture_attaches_stats_to_the_header_only(self):
        """End to end: a bot's header and its match land as two entries, one
        carrying stats and one carrying a fetchable filename - never both."""
        config.NICKNAME = "DCCore"
        config.broadcast_search_inprogress = True
        config.broadcast_search_deadline = time.time() + 30
        config.broadcast_search_results = []

        irc._capture_broadcast_search_reply(
            "kurtb66", "DCCore",
            "Search Result 4 Matches For X Copy And Paste !kurtb66 FILENAME "
            "To The Channel To Request. (10/10) Free Slots, 0 Queued "
            "OmenServe v2.71")
        irc._capture_broadcast_search_reply(
            "kurtb66", "DCCore",
            "!kurtb66 14 - Testament - Souls Of Black.mp3  ::INFO:: 4.8MB")

        header, result = config.broadcast_search_results
        self.assertIn("header", header)
        self.assertNotIn("filename", header)
        self.assertEqual(result["filename"], "14 - Testament - Souls Of Black.mp3")
        self.assertNotIn("header", result)


if __name__ == "__main__":
    unittest.main()
