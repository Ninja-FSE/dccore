"""#550: "the channel is missing from SEARCH, and empty in SENDING and SENT."

The structured lines carried no channel at all, so a client had nothing to
show. Every event line now has a `<channel>` token straight after the nick
(`-` when the event has none), and each emitter passes the channel it knows:
SEARCH the channel it was typed in, REQUEST and QUEUED the channel of the
request, SENDING the one the send is started for (a request picked up from
the queue later included), SENT and FAIL the channel of the transfer.
RESUMED still has none to give and says `-`.

The line format is tested with the rest of it in
test_a_structured_feed_for_the_admin_chat.py; this file is the wiring: that
each emitter hands the channel to feed_event, read from a real event sink
with the real function driven.

DRIVEN, NOT READ (#644, audit M42). The SEARCH and REQUEST wirings used to be
regex matches on list.py and dcc.py under a docstring saying the emitters
were "not callable without a live socket and a list on disk" - while
execute_search() was driven in three other test files and
handle_download_request() in ten. A regex on the call is satisfied by a
call whose `channel` variable has been shadowed with the wrong value two
lines above it; a sink is not. The queue-pickup SENDING sites were checked
the same way, by counting call sites; three of the four are driven below.
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
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import announce  # noqa: E402
import db  # noqa: E402
import dcc  # noqa: E402
import defaults as config  # noqa: E402
import list as list_mod  # noqa: E402

from tests.support import DCCoreTestCase, RecordingSocket, no_disk_writes, silence_debug  # noqa: E402
from tests.test_path_security import InlineThread  # noqa: E402
from tests.test_webserver import write_master_list  # noqa: E402

SERVED = "#somechannel"
OTHER = "#otherchannel"
SWEEP = "system_next_trigger_fallback"   # check_queue_and_send()'s own name for "nobody in particular"


def source(name):
    with io.open(os.path.join(REPO_ROOT, name), encoding="utf-8") as handle:
        return handle.read()


class ListensToTheFeed(DCCoreTestCase):
    """A real event sink, and the notices silenced."""

    def setUp(self):
        super().setUp()
        self.events = []
        real = announce.send_debug
        announce.send_debug = lambda *a, **k: None
        self.addCleanup(setattr, announce, "send_debug", real)
        announce.add_event_sink(self.sink)
        self.addCleanup(announce.remove_event_sink, self.sink)
        self.set_config(CONSOLE_SHOW_SENDS=True, CONSOLE_SHOW_QUEUE=True,
                        ANNOUNCE_TRANSFERS=False, MAX_DCC_SLOTS=3)

    def sink(self, kind, fields, text):
        self.events.append((kind, fields))

    def last(self, kind):
        return [fields for k, fields in self.events if k == kind][-1]

    def kinds(self):
        return [k for k, _fields in self.events]


class TheEmittersHandTheChannelOn(ListensToTheFeed):

    def test_sent(self):
        announce.send_transfer_complete("#chan", "dave", "A.flac", 1000, time.time() - 5, 200, duration=5.0)

        self.assertEqual(self.last("SENT")["channel"], "#chan")

    def test_sending(self):
        announce.send_dcc_sending_notice("dave", "A.flac", channel="#chan")

        self.assertEqual(self.last("SENDING")["channel"], "#chan")

    def test_sending_without_one_says_none_rather_than_guessing(self):
        announce.send_dcc_sending_notice("dave", "A.flac")

        self.assertIsNone(self.last("SENDING")["channel"])

    def test_queued(self):
        announce.send_dcc_queue_notice("dave", "A.flac", 2, channel="#chan")

        self.assertEqual(self.last("QUEUED")["channel"], "#chan")

    def test_fail(self):
        dcc._report_transfer_failure("dave", "A.flac", "stopped", channel="#chan")

        self.assertEqual(self.last("FAIL")["channel"], "#chan")


class ServesARealRequest(ListensToTheFeed):
    """A library with one track and one album, a master list naming the
    track, two configured channels, and dcc's threads recorded instead of
    started - the same fixture pieces test_path_security.py and
    test_search_does_not_block_transfers.py drive these functions with."""

    def setUp(self):
        super().setUp()
        self.tree = self.make_tree()
        write_master_list(self.tree.lists, "DCCoreTest", [(None, [("Song.flac", "4KB")])])
        with io.open(os.path.join(self.tree.music, "Song.flac"), "wb") as handle:
            handle.write(b"\x00" * 4096)
        self.set_config(FILE_DIRECTORY=self.tree.music, LOCAL_LIST_DIR=self.tree.lists,
                        LIST_BASE_NAME="DCCoreTest", NICKNAME="DCCoreTest",
                        CHANNEL="%s,%s" % (SERVED, OTHER),
                        search_inprogress=False, update_inprogress=False,
                        bot_joined_channel=True)
        no_disk_writes(db)
        silence_debug(announce)
        InlineThread.dispatched = []
        self._real_thread = threading.Thread
        dcc.threading.Thread = InlineThread
        self.addCleanup(setattr, dcc.threading, "Thread", self._real_thread)
        # Present in the channel the requests come from, so a queued row is
        # something the pickup will dispatch rather than freeze.
        config.channel_users[OTHER] = {"dave"}
        self.sock = RecordingSocket()

    def quietly(self, call, *args):
        with contextlib.redirect_stdout(io.StringIO()):
            return call(self.sock, *args)

    def request(self, what, channel=OTHER, user="dave"):
        self.quietly(dcc.handle_download_request, user, what, channel)

    def fill_the_slots(self):
        self.set_config(MAX_DCC_SLOTS=1)
        config.active_transfers.append({"user": "someoneelse", "file": "X.flac", "bytes_sent": 0})

    def free_the_slots(self):
        config.active_transfers.clear()


class TheSearchNamesTheChannelItWasTypedIn(ServesARealRequest):

    def test_a_search_with_a_hit(self):
        self.quietly(list_mod.execute_search, "dave", "song", OTHER)

        self.assertEqual(self.last("SEARCH")["channel"], OTHER)
        self.assertEqual(self.last("SEARCH")["results"], 1)

    def test_and_one_without(self):
        self.quietly(list_mod.execute_search, "dave", "nothing-of-the-sort", SERVED)

        self.assertEqual(self.last("SEARCH")["channel"], SERVED)
        self.assertEqual(self.last("SEARCH")["results"], 0)


class TheRequestsNameTheChannelTheyCameFrom(ServesARealRequest):

    def test_a_file_request(self):
        self.request("Song.flac", channel=OTHER)

        self.assertEqual(self.last("REQUEST")["channel"], OTHER)
        self.assertEqual(self.last("REQUEST")["kind"], "file")

    def test_a_folder_request(self):
        self.request("!rar Metallica/Black Album (1991)", channel=SERVED)

        self.assertEqual(self.last("REQUEST")["channel"], SERVED)
        self.assertEqual(self.last("REQUEST")["kind"], "folder")

    def test_the_folder_is_queued_where_it_was_asked_for(self):
        self.request("!rar Metallica/Black Album (1991)", channel=SERVED)

        self.assertEqual(self.last("QUEUED")["channel"], SERVED)


class EverySendPassesTheChannelItStartsFor(ServesARealRequest):
    """A send started later, from the queue, still says where it was asked
    for. Three of the four send sites are driven: the direct one, the
    per-user pickup, and the global sweep. The fourth - a packed archive
    picked up from the queue - needs a real rar run, so it is checked in
    the text below, and says so."""

    def test_a_send_that_starts_at_once(self):
        self.request("Song.flac", channel=OTHER)

        self.assertEqual(self.kinds()[-2:], ["REQUEST", "SENDING"])
        self.assertEqual(self.last("SENDING")["channel"], OTHER)

    def test_a_send_picked_up_from_the_queue_for_its_user(self):
        self.fill_the_slots()
        self.request("Song.flac", channel=OTHER)
        self.assertEqual(self.last("QUEUED")["channel"], OTHER)
        self.assertNotIn("SENDING", self.kinds(), "the slots were not full")

        self.free_the_slots()
        self.quietly(dcc.check_queue_and_send, "dave")

        self.assertEqual(self.last("SENDING")["channel"], OTHER)
        self.assertEqual(self.last("SENDING")["name"], "Song.flac")

    def test_a_send_picked_up_by_the_global_sweep(self):
        self.fill_the_slots()
        self.request("Song.flac", channel=OTHER)
        self.free_the_slots()

        self.quietly(dcc.check_queue_and_send, SWEEP)

        self.assertEqual(self.last("SENDING")["channel"], OTHER)
        self.assertEqual(self.last("SENDING")["name"], "Song.flac")

    def test_the_packed_archive_pickup_names_its_channel_too(self):
        """The one site not driven here: it runs after a real rar packing.
        The notice and the start_dcc_send that follows it must name the
        same channel."""
        body = source("dcc.py")
        self.assertRegex(body, r"send_dcc_sending_notice\([^\n]*rar_filename[^\n]*channel=target_chan")
        self.assertIn("target_rar_path, rar_filename, target_chan", body)


class EveryFailureOfATransferCarriesItsChannel(unittest.TestCase):

    def start_dcc_send_source(self):
        body = source("dcc.py")
        start = body.index("def start_dcc_send(")
        return body[start:]

    def test_start_dcc_send_reports_through_one_wrapper_that_adds_the_channel(self):
        body = self.start_dcc_send_source()

        self.assertIn("_report_transfer_failure(*args, channel=channel, **kwargs)", body)
        # the wrapper is the only direct caller left in the function
        self.assertEqual(body.count("_report_transfer_failure("), 1)
        self.assertGreaterEqual(body.count("report_failure("), 9)


if __name__ == "__main__":
    unittest.main()
