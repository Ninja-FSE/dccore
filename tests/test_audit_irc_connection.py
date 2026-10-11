"""Four IRC-connection findings from the 2026-10-10 audit (#1271).

1. A connection whose channels all refused the first JOIN never claimed
   channel sync. The activation found channel_users empty and rightly did not
   claim it - but it runs once per connection, so when the rejoin on the
   advert timer got the bot in and NAMES filled the member list, nothing set
   config.bot_joined_channel until the next reconnect. Everything gated on it
   (cross-bot fetches, the debug drain, the freeze sweep, auto-refetch) stayed
   off for the rest of the connection.

2. The DCC address was looked up once per process, before the reconnect
   loop, and written into MY_IP_OR_DOCK - the name that means "pinned". A
   lookup that failed at boot refused every send until a restart; an address
   the ISP changed was offered for ever.

3. A configured channel the bot had given up rejoining kept its member list,
   so everyone in it stayed "present" to dcc.py for the rest of the
   connection.

4. RFC 1459 casemapping was ignored. "#music[1]" and "#music{1}" are one
   channel on ircu and on every server whose 005 says CASEMAPPING=rfc1459;
   str.lower() never matched them, so a configured "#music[1]" the server
   spells "#music{1}" was never confirmed, never advertised, and rejoined
   every advert cycle.

Each is driven through the real irc.irc_loop() against a scripted socket
(no network), the way tests/test_a_third_nick_when_both_are_taken.py does,
alongside direct tests of the functions the fixes added.
"""

import contextlib
import io
import os
import socket
import sys
import threading
import time
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import announce  # noqa: E402
import dcc  # noqa: E402
import dcc_fetch  # noqa: E402
import defaults as config  # noqa: E402
import irc  # noqa: E402
import runtime  # noqa: E402
import serverschat  # noqa: E402

from tests.support import DCCoreTestCase, silence_debug  # noqa: E402

REAL_SLEEP = time.sleep
SERVER = ":irc.example.net"
WAIT_LIMIT = 10.0


class _StopTheLoop(BaseException):
    """Raised from the loop thread's own sleep - BaseException so nothing in
    irc_loop()'s handlers swallows it."""


class _ScriptedSocket:
    """One scripted server line per recv(), each after its condition holds
    (WAIT_LIMIT at most); the link breaks when the script runs out."""

    def __init__(self, script):
        self.script = list(script)
        self.sent = []
        self.lock = threading.Lock()

    def settimeout(self, *_a): pass
    def setsockopt(self, *_a): pass
    def connect(self, *_a): pass
    def close(self): pass

    def sendall(self, payload):
        with self.lock:
            self.sent.append(payload.decode("utf-8", "replace"))

    def send(self, payload):
        self.sendall(payload)
        return len(payload)

    def sent_text(self):
        with self.lock:
            return "".join(self.sent)

    def recv(self, _n):
        if not self.script:
            raise socket.error("scripted end of link")
        wait_for, line = self.script.pop(0)
        deadline = time.monotonic() + WAIT_LIMIT
        while wait_for is not None and not wait_for(self) and time.monotonic() < deadline:
            REAL_SLEEP(0.01)
        return (line + "\r\n").encode("utf-8")


def until(condition):
    """A script condition: the line waits until `condition()` holds."""
    return lambda _sock: condition()


def activated(_sock):
    return announce.is_ready is True


def sent_contains(text):
    return lambda sock: text in sock.sent_text()


class _DrivesTheReadLoop(DCCoreTestCase):
    """Runs irc_loop() on a thread. time.sleep is scaled down for the helper
    threads (the watchdog's 20 s, the settles' 5 s); on the loop thread
    itself it is the reconnect wait, which ends the run once every scripted
    link has been used."""

    def setUp(self):
        super().setUp()
        self.set_config(NICKNAME="SomeBot", ALT_NICKNAME="SomeBot_",
                        SERVER="irc.example.invalid", PORT=6667,
                        CHANNEL="#music", DEBUG_CHANNEL="",
                        MY_IP_OR_DOCK="203.0.113.5", ANNOUNCE_INTERVAL=3600,
                        REJOIN_ATTEMPTS=3)
        config.ORIGINAL_NICK = "SomeBot"
        # What a fresh process reads; the harness presets True.
        config.bot_joined_channel = False
        silence_debug(announce)
        self._threads_before = set(threading.enumerate())
        self.addCleanup(setattr, irc.time, "sleep", REAL_SLEEP)
        self.addCleanup(setattr, irc.socket, "socket", irc.socket.socket)
        # Registered last, so it runs first - while time.sleep is still the
        # scaled one, or a watchdog started late would sleep a real 20 s.
        self.addCleanup(self._finish_the_threads)

    def _finish_the_threads(self):
        """Every thread the connection started - watchdog, settles, the
        advert worker - has stood down by the end of the test. They all
        check the connection epoch, which the disconnect moved on."""
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            # Again on every pass: a thread joined here may have started
            # the advert worker, which claims the worker id as it starts.
            announce.current_worker_id = -1
            running = [thread for thread in set(threading.enumerate()) - self._threads_before
                       if thread is not threading.current_thread() and thread.is_alive()]
            if not running:
                return
            try:
                running[0].join(0.5)
            except RuntimeError:
                REAL_SLEEP(0.01)  # enumerated while still starting

    def drive(self, *scripts):
        sockets = [_ScriptedSocket(script) for script in scripts]
        made = []
        loop = {}

        def next_socket(*_a, **_k):
            made.append(sockets[len(made)])
            return made[-1]

        def scaled_sleep(seconds):
            if threading.current_thread() is loop.get("thread"):
                if len(made) >= len(sockets):
                    raise _StopTheLoop()
                return
            REAL_SLEEP(min(float(seconds) * 0.01, 0.2))

        irc.socket.socket = next_socket
        irc.time.sleep = scaled_sleep
        outcome = {}

        def body():
            with contextlib.redirect_stdout(io.StringIO()) as out:
                try:
                    irc.irc_loop()
                except _StopTheLoop:
                    outcome["stopped"] = True
                except BaseException as err:  # noqa: BLE001 - reported below
                    outcome["error"] = repr(err)
            outcome["log"] = out.getvalue()

        thread = threading.Thread(target=body, daemon=True)
        loop["thread"] = thread
        thread.start()
        thread.join(60)
        self.assertFalse(thread.is_alive(), "irc_loop() did not reach its reconnect wait")
        self.assertTrue(outcome.get("stopped"), outcome)
        return sockets, outcome


# --- 1. channel sync claimed when the member list arrives late ---------------

class ChannelSyncArrivesWithTheLateMemberList(_DrivesTheReadLoop):

    def test_a_rejoin_after_an_empty_activation_claims_channel_sync(self):
        """CHANNEL is +r and the X login is slower than the JOIN: 477, the
        watchdog activates with nobody known, then the rejoin works."""
        seen = {}

        def synced():
            if not getattr(config, "bot_joined_channel", False):
                return False
            seen["readiness"] = dcc_fetch._bot_readiness(["alfa"], time.time())
            return True

        _sockets, outcome = self.drive([
            (None, SERVER + " 001 SomeBot :Welcome to the network"),
            (sent_contains("JOIN #music"),
             SERVER + " 477 SomeBot #music :Cannot join channel, you need to be identified (+r)"),
            (activated, ":SomeBot!somebot@host.example JOIN #music"),
            (None, SERVER + " 353 SomeBot = #music :SomeBot @alfa bravo"),
            (None, SERVER + " 366 SomeBot #music :End of /NAMES list."),
            (until(synced), ":alfa!a@host.example PRIVMSG #music :hello"),
        ])

        self.assertIn("No channel members known yet", outcome["log"],
                      "the activation found members - not the late case")
        self.assertIn("channel sync claimed now", outcome["log"])
        self.assertEqual(seen.get("readiness"), {"alfa": ""},
                         "fetches still wait as 'joining' with the bot in the channel")

    def test_names_before_the_activation_are_claimed_by_the_activation(self):
        """The ordinary path is unchanged: every channel answers, and the
        activation claims after its settle - the read loop does not."""
        seen = {}

        def activated_and_synced(sock):
            if not activated(sock):
                return False
            seen["synced"] = config.bot_joined_channel
            return True

        _sockets, outcome = self.drive([
            (None, SERVER + " 001 SomeBot :Welcome"),
            (sent_contains("JOIN #music"), ":SomeBot!s@host.example JOIN #music"),
            (None, SERVER + " 353 SomeBot = #music :SomeBot alfa"),
            (None, SERVER + " 366 SomeBot #music :End of /NAMES list."),
            (activated_and_synced, ":alfa!a@host.example PRIVMSG #music :hi"),
        ])

        self.assertIs(seen.get("synced"), True)
        self.assertIn("All channels joined successfully", outcome["log"])
        self.assertNotIn("channel sync claimed now", outcome["log"])


class ClaimingChannelSync(DCCoreTestCase):
    """irc.claim_channel_sync() itself: once per connection, never ahead of
    the activation, never with nobody known."""

    def setUp(self):
        super().setUp()
        self.set_config(AUTO_REFETCH_LISTS=False)
        config.bot_joined_channel = False
        config.connection_epoch = 7
        self.woken = []
        real_wake = dcc.wake_restored_queues
        dcc.wake_restored_queues = lambda sock: self.woken.append(
            getattr(config, "bot_joined_channel", None))
        self.addCleanup(setattr, dcc, "wake_restored_queues", real_wake)
        self.addCleanup(setattr, runtime, "freeze_clock_paused_at", None)

    def claim(self, epoch=7, late=False):
        return irc.claim_channel_sync(object(), epoch, late=late, log=lambda *_: None)

    def wait_for_the_wake(self, count):
        deadline = time.monotonic() + WAIT_LIMIT
        while len(self.woken) < count and time.monotonic() < deadline:
            REAL_SLEEP(0.01)
        self.assertEqual(len(self.woken), count)

    def test_nobody_known_is_not_claimed_and_waits(self):
        self.assertFalse(self.claim())

        self.assertFalse(config.bot_joined_channel)
        self.assertEqual(runtime.channel_sync_waiting, 7)

    def test_the_late_claim_after_an_empty_activation(self):
        self.claim()
        config.channel_users["#music"] = {"alfa"}

        self.assertTrue(self.claim(late=True))

        self.assertTrue(config.bot_joined_channel)
        self.assertIsNone(runtime.channel_sync_waiting)
        self.wait_for_the_wake(1)
        self.assertEqual(self.woken, [True], "the sweep ran before the claim")

    def test_a_late_claim_never_runs_ahead_of_the_activation(self):
        """A 366 during the activation's own settle is the activation's to
        claim, after the settle."""
        config.channel_users["#music"] = {"alfa"}

        self.assertFalse(self.claim(late=True))
        self.assertFalse(config.bot_joined_channel)

    def test_once_per_connection(self):
        self.claim()
        config.channel_users["#music"] = {"alfa"}
        self.claim(late=True)
        self.wait_for_the_wake(1)

        self.assertFalse(self.claim(late=True))
        REAL_SLEEP(0.05)
        self.assertEqual(len(self.woken), 1, "a second NAMES woke the queues again")

    def test_not_for_a_connection_that_is_gone(self):
        self.claim(epoch=7)
        config.connection_epoch = 8
        config.channel_users["#music"] = {"alfa"}

        self.assertFalse(self.claim(epoch=7, late=True))
        self.assertFalse(config.bot_joined_channel)


# --- 2. the DCC address, looked up again on reconnect -----------------------

class _FakeResponse:
    def __init__(self, body):
        self.body = body

    def read(self):
        return self.body.encode("utf-8")


class TheDccAddressIsLookedUpAgain(_DrivesTheReadLoop):

    def test_a_failed_boot_lookup_and_a_changed_address_are_both_fixed(self):
        """Case 1: the boot lookup fails (network not up yet) and the next
        registration finds the address. Case 2: the ISP reconnect hands the
        line a new address, and the next registration finds that too."""
        self.set_config(MY_IP_OR_DOCK="")
        answers = [OSError("network is unreachable"), "198.51.100.20", "198.51.100.30"]
        calls = []

        def fake_urlopen(_request, timeout=None):
            calls.append(timeout)
            answer = answers[len(calls) - 1]
            if isinstance(answer, Exception):
                raise answer
            return _FakeResponse(answer)

        real_urlopen = irc.urllib.request.urlopen
        irc.urllib.request.urlopen = fake_urlopen
        self.addCleanup(setattr, irc.urllib.request, "urlopen", real_urlopen)
        seen = {}

        def found(address):
            return until(lambda: runtime.dcc_address_detected == address)

        def a_day_later(_sock):
            # The last lookup is old by the time the link comes back.
            runtime.dcc_address_found_at -= 86400
            return True

        def note_offered():
            if runtime.dcc_address_detected != "198.51.100.30":
                return False
            seen["offered"] = dcc.dcc_address()
            seen["ip long"] = dcc.get_public_ip_long()
            return True

        self.drive(
            [(None, SERVER + " 001 SomeBot :Welcome"),
             (found("198.51.100.20"), SERVER + " NOTICE SomeBot :tick")],
            [(a_day_later, SERVER + " 001 SomeBot :Welcome"),
             (until(note_offered), SERVER + " NOTICE SomeBot :tick")],
        )

        self.assertEqual(len(calls), 3, "boot, then once per registration")
        self.assertEqual(seen.get("offered"), "198.51.100.30")
        self.assertEqual(seen.get("ip long"), (198 << 24) + (51 << 16) + (100 << 8) + 30)
        self.assertEqual(config.MY_IP_OR_DOCK, "",
                         "the detection was written where the pin lives")

    def test_a_pinned_address_is_never_looked_up(self):
        def refuse(*_a, **_k):
            raise AssertionError("looked up despite MY_IP_OR_DOCK")

        real_urlopen = irc.urllib.request.urlopen
        irc.urllib.request.urlopen = refuse
        self.addCleanup(setattr, irc.urllib.request, "urlopen", real_urlopen)

        _sockets, outcome = self.drive([
            (None, SERVER + " 001 SomeBot :Welcome"),
            (sent_contains("JOIN #music"), SERVER + " NOTICE SomeBot :tick"),
        ])

        self.assertNotIn("AssertionError", outcome["log"])
        self.assertEqual(dcc.dcc_address(), "203.0.113.5")


class RefreshingTheDccAddress(DCCoreTestCase):
    """irc.refresh_dcc_address() and dcc.dcc_address() themselves."""

    def setUp(self):
        super().setUp()
        self.set_config(MY_IP_OR_DOCK="")
        self.calls = []

    def lookup(self, *answers):
        def ask():
            answer = answers[min(len(self.calls), len(answers) - 1)]
            self.calls.append(answer)
            if isinstance(answer, Exception):
                raise answer
            return answer
        return ask

    def refresh(self, lookup, now, force=False):
        return irc.refresh_dcc_address(lookup=lookup, log=lambda *_: None,
                                       now=now, force=force)

    def test_a_recent_answer_is_not_asked_for_again(self):
        """A link that flaps does not hammer the lookup service."""
        ask = self.lookup("198.51.100.20", "198.51.100.30")
        self.refresh(ask, now=1000.0)

        found = self.refresh(ask, now=1000.0 + irc.DCC_ADDRESS_RECHECK_SECONDS - 1)

        self.assertEqual(found, "198.51.100.20")
        self.assertEqual(len(self.calls), 1)

    def test_an_old_answer_is_asked_for_again(self):
        ask = self.lookup("198.51.100.20", "198.51.100.30")
        self.refresh(ask, now=1000.0)

        found = self.refresh(ask, now=1000.0 + irc.DCC_ADDRESS_RECHECK_SECONDS + 1)

        self.assertEqual(found, "198.51.100.30")
        self.assertEqual(dcc.dcc_address(), "198.51.100.30")

    def test_after_a_failure_the_next_one_asks_at_once(self):
        ask = self.lookup(OSError("no route to host"), "198.51.100.20")
        self.assertEqual(self.refresh(ask, now=1000.0), "")

        self.assertEqual(self.refresh(ask, now=1001.0), "198.51.100.20")

    def test_a_failure_keeps_the_address_found_before_it(self):
        ask = self.lookup("198.51.100.20", OSError("no route to host"))
        self.refresh(ask, now=1000.0)

        found = self.refresh(ask, now=1000.0, force=True)

        self.assertEqual(found, "198.51.100.20")
        self.assertEqual(len(self.calls), 2)

    def test_a_pin_is_used_and_nothing_is_looked_up(self):
        self.set_config(MY_IP_OR_DOCK="203.0.113.9")
        ask = self.lookup("198.51.100.20")

        self.assertEqual(self.refresh(ask, now=1000.0, force=True), "203.0.113.9")
        self.assertEqual(self.calls, [])
        self.assertEqual(runtime.dcc_address_detected, "")

    def test_the_pin_wins_over_a_detection(self):
        runtime.dcc_address_detected = "198.51.100.20"
        self.set_config(MY_IP_OR_DOCK="203.0.113.9")

        self.assertEqual(dcc.dcc_address(), "203.0.113.9")

    def test_the_detection_is_offered_when_nothing_is_pinned(self):
        runtime.dcc_address_detected = "198.51.100.20"

        self.assertEqual(dcc.get_public_ip_long(),
                         (198 << 24) + (51 << 16) + (100 << 8) + 20)

    def test_nothing_known_is_still_refused(self):
        self.assertEqual(dcc.dcc_address(), "")
        self.assertEqual(dcc.get_public_ip_long(), 0)
        self.assertFalse(dcc.is_offerable_to_strangers())


# --- 3. a channel given up on forgets its members ---------------------------

class AChannelGivenUpOnForgetsItsMembers(_DrivesTheReadLoop):

    def test_three_refusals_after_a_kick_drop_the_member_list(self):
        seen = {}

        def snapshot(key):
            def take(_sock):
                seen[key] = (dcc.user_is_present_in_ram("alfa"), irc.gave_up_on())
                return True
            return take

        def refused(count):
            return until(lambda: (config.kicked_channels.get("#music") or {})
                         .get("refusals") == count)

        self.drive([
            (None, SERVER + " 001 SomeBot :Welcome"),
            (sent_contains("JOIN #music"), ":SomeBot!s@host.example JOIN #music"),
            (None, SERVER + " 353 SomeBot = #music :SomeBot @op alfa bravo"),
            (None, SERVER + " 366 SomeBot #music :End of /NAMES list."),
            (activated, ":op!o@host.example KICK #music SomeBot :out"),
            (None, SERVER + " 474 SomeBot #music :Cannot join channel (+b)"),
            (refused(1), SERVER + " 474 SomeBot #music :Cannot join channel (+b)"),
            (snapshot("two left"), SERVER + " 474 SomeBot #music :Cannot join channel (+b)"),
            (refused(3), SERVER + " NOTICE SomeBot :tick"),
            (snapshot("given up"), SERVER + " NOTICE SomeBot :tick"),
        ])

        self.assertEqual(seen["two left"], (True, []),
                         "a channel still being retried keeps its list")
        self.assertEqual(seen["given up"], (False, ["#music"]),
                         "a channel given up on kept its members present")

    def test_a_kick_with_rejoining_off_drops_it_at_once(self):
        self.set_config(REJOIN_ATTEMPTS=0)
        seen = {}

        def kicked():
            if "#music" not in config.kicked_channels:
                return False
            seen["alfa present"] = dcc.user_is_present_in_ram("alfa")
            return True

        self.drive([
            (None, SERVER + " 001 SomeBot :Welcome"),
            (sent_contains("JOIN #music"), ":SomeBot!s@host.example JOIN #music"),
            (None, SERVER + " 353 SomeBot = #music :SomeBot @op alfa"),
            (None, SERVER + " 366 SomeBot #music :End of /NAMES list."),
            (activated, ":op!o@host.example KICK #music SomeBot :out"),
            (until(kicked), SERVER + " NOTICE SomeBot :tick"),
        ])

        self.assertIs(seen.get("alfa present"), False)


# --- 4. channel names compared under the server's casemapping ---------------

class CasemappingIsTheServers(_DrivesTheReadLoop):

    def test_a_bracket_channel_the_server_spells_with_braces_is_confirmed(self):
        self.set_config(CHANNEL="#music[1]")
        seen = {}

        def snapshot(_sock):
            seen["out of"] = sorted(irc.channels_we_are_out_of())
            seen["to rejoin"] = irc.channels_to_rejoin()
            seen["members"] = {k: sorted(v) for k, v in config.channel_users.items()}
            return True

        sockets, outcome = self.drive([
            (None, SERVER + " 001 SomeBot :Welcome"),
            (None, SERVER + " 005 SomeBot CASEMAPPING=rfc1459 CHANTYPES=# :are supported by this server"),
            (sent_contains("JOIN #music[1]"), ":SomeBot!s@host.example JOIN #music{1}"),
            (None, SERVER + " 353 SomeBot = #music{1} :SomeBot alfa"),
            (None, SERVER + " 366 SomeBot #music{1} :End of /NAMES list."),
            (activated, ":bravo!b@host.example JOIN #music{1}"),
            (snapshot, ":bravo!b@host.example PRIVMSG #music{1} :hi"),
        ])

        self.assertIn("All channels joined successfully", outcome["log"])
        self.assertNotIn("unconfirmed channel", outcome["log"])
        self.assertEqual(seen["out of"], [])
        self.assertEqual(seen["to rejoin"], [])
        self.assertEqual(seen["members"], {"#music[1]": ["alfa", "bravo", "somebot"]},
                         "filed under a key no per-channel lookup uses")

    def test_the_servers_005_is_read_and_a_reconnect_starts_from_the_default(self):
        seen = {}

        def remember(_sock):
            seen["mapping"] = runtime.server_casemapping
            return True

        self.drive(
            [(None, SERVER + " 001 SomeBot :Welcome"),
             (None, SERVER + " 005 SomeBot NICKLEN=15 CASEMAPPING=ascii :are supported by this server"),
             (remember, SERVER + " NOTICE SomeBot :tick")],
            [(lambda _s: runtime.server_casemapping == "rfc1459",
              SERVER + " 001 SomeBot :Welcome")],
        )

        self.assertEqual(seen["mapping"], "ascii")
        self.assertEqual(runtime.server_casemapping, "rfc1459")


class FoldingLikeTheServer(DCCoreTestCase):

    def test_rfc1459_is_the_default(self):
        self.assertEqual(irc.irc_lower("#Music[1]\\~"), "#music{1}|^")

    def test_strict_rfc1459_leaves_the_tilde(self):
        runtime.server_casemapping = "strict-rfc1459"

        self.assertEqual(irc.irc_lower("#Music[1]\\~"), "#music{1}|~")

    def test_ascii_folds_letters_only(self):
        runtime.server_casemapping = "ascii"

        self.assertEqual(irc.irc_lower("#Music[1]"), "#music[1]")

    def test_an_unknown_mapping_folds_like_ascii(self):
        runtime.server_casemapping = "rfc7613"

        self.assertEqual(irc.irc_lower("#Music[1]"), "#music[1]")

    def test_the_005_token(self):
        line = SERVER + " 005 SomeBot CHANTYPES=# CASEMAPPING=Strict-RFC1459 :are supported"

        self.assertEqual(irc.isupport_casemapping(line), "strict-rfc1459")
        self.assertIsNone(irc.isupport_casemapping(SERVER + " 005 SomeBot NICKLEN=15 :are supported"))
        self.assertIsNone(irc.isupport_casemapping(
            ":alfa!a@host.example PRIVMSG #music :005 CASEMAPPING=ascii"))

    def test_a_configured_channel_keeps_its_own_spelling(self):
        self.set_config(CHANNEL="#Music[1],#other", DEBUG_CHANNEL="#ops")

        self.assertEqual(irc.channel_key("#MUSIC{1}"), "#music[1]")
        self.assertEqual(irc.configured_spelling("#music{1}"), "#Music[1]")
        self.assertEqual(irc.channel_key("#elsewhere[2]"), "#elsewhere{2}")
        self.assertIsNone(irc.configured_spelling("#elsewhere"))

    def test_the_debug_channel_is_not_joined_twice_under_another_spelling(self):
        self.set_config(CHANNEL="#music[1]", DEBUG_CHANNEL="#MUSIC{1}")

        self.assertEqual(irc.channels_we_should_be_in(), ["#music[1]"])

    def test_a_who_round_ends_under_the_servers_spelling(self):
        """DCCore Chat asks WHO for "#music[1]"; the 315 names "#music{1}"."""
        self.set_config(CHANNEL="#music[1]")
        runtime.chat_who_round["#music[1]"] = set()
        self.addCleanup(runtime.chat_who_round.pop, "#music[1]", None)

        with contextlib.redirect_stdout(io.StringIO()):
            serverschat.finish_who_round("#music{1}")

        self.assertNotIn("#music[1]", runtime.chat_who_round)


if __name__ == "__main__":
    unittest.main()
