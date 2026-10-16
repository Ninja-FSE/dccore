"""A list rebuild stops with the bot that started it (#1356, #1365).

update_list.py is a process of its own. Stopped from Tools > Stop while a
scheduled rebuild was scanning, the bot ended and the rebuild did not: half an
hour later it was at folder 10 of 20, holding the list lock, and the restarted
bot's Update list was refused with "Another list rebuild is already running".

Now the rebuild asks, while it scans and once more before it publishes,
whether the bot asked it to stop or is gone (the bot's pid, for a bot that
ended without its shutdown). Stopped, it publishes nothing and the previous
list stays in use, and it says it stopped rather than that it failed. From
the swap on it is never stopped: the new list is the one in use by then.

The bot asks by the token it gave the run (#1365), never through the
reading's stop file: a terminal's rebuild has no token and goes through, and
the bot's own child is asked wherever it is. The token is in the list lock
too, so a restarted bot can tell a rebuild its last run left behind from a
terminal's.

Rebuilds of a real throwaway library, in this process; the stop arrives
through the same file and the same question the running child asks.
"""

import contextlib
import io
import os
import sys
import threading
import time
import types
import unittest
from unittest import mock

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import commands  # noqa: E402
import defaults as config  # noqa: E402
import list as list_mod  # noqa: E402
import platform_compat  # noqa: E402
import runtime  # noqa: E402
import update_list  # noqa: E402

from tests.support import DCCoreTestCase  # noqa: E402

TOKEN = "token-of-this-run"
OTHER = "token-of-another-run"


def as_a_run(case, token=TOKEN, daemon=""):
    """This process as update_list.py started with `token` ("" is by hand)."""
    env = mock.patch.dict(os.environ, {update_list.RUN_TOKEN_ENV: token,
                                       update_list.DAEMON_PID_ENV: daemon})
    env.start()
    case.addCleanup(env.stop)


def tidy(case):
    case.addCleanup(update_list.clear_stop_request)
    case.addCleanup(update_list.clear_run_stop_request)
    case.addCleanup(update_list.reset_rebuild_stop)
    update_list.reset_rebuild_stop()


class Clock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now


class TheQuestion(DCCoreTestCase):
    """rebuild_stop_reason(): what it answers, and how often it looks."""

    def setUp(self):
        super().setUp()
        tree = self.make_tree()
        self.set_config(LIST_PROGRESS_FILE=os.path.join(tree.root, "progress.json"))
        tidy(self)
        as_a_run(self)
        self.clock = Clock()

    def ask(self, force=False, gone=False):
        return update_list.rebuild_stop_reason(clock=self.clock, force=force,
                                               should_stop=lambda: gone)

    def test_nothing_asked_and_the_bot_alive_is_no_stop(self):
        self.assertIsNone(self.ask(force=True))

    def test_the_bot_asking_for_this_run_stops_it(self):
        self.assertTrue(update_list.request_run_stop([TOKEN]))
        self.assertEqual(self.ask(force=True), "the bot is stopping and asked it to stop")

    def test_a_request_for_another_run_does_not(self):
        update_list.request_run_stop([OTHER])
        self.assertIsNone(self.ask(force=True))

    def test_the_readings_stop_file_does_not_stop_a_rebuild(self):
        """The file any reading obeys: a rebuild that read it could be a
        terminal's, or have it cleared under it by the lock it waited for."""
        update_list.request_stop()
        self.assertIsNone(self.ask(force=True))

    def test_a_run_started_by_hand_is_never_asked(self):
        as_a_run(self, token="")
        update_list.request_run_stop(["", OTHER])
        update_list.request_stop()
        self.assertIsNone(self.ask(force=True))

    def test_a_gone_bot_is_a_stop(self):
        self.assertEqual(self.ask(force=True, gone=True), "the bot that started it is no longer running")

    def test_the_answer_is_kept(self):
        """So a multi-list run does not start the next list."""
        update_list.request_run_stop([TOKEN])
        self.ask(force=True)
        update_list.clear_run_stop_request()
        self.assertIsNotNone(self.ask(force=True))
        self.assertEqual(update_list.stopped_reason(), "the bot is stopping and asked it to stop")
        update_list.reset_rebuild_stop()
        self.assertIsNone(self.ask(force=True))

    def test_it_looks_at_most_once_a_second_unless_forced(self):
        self.assertIsNone(self.ask())
        update_list.request_run_stop([TOKEN])
        self.clock.now += 0.5
        self.assertIsNone(self.ask(), "looked again within the second")
        self.assertIsNotNone(self.ask(force=True))

    def test_a_second_later_it_looks_again(self):
        self.assertIsNone(self.ask())
        update_list.request_run_stop([TOKEN])
        self.clock.now += update_list.REBUILD_STOP_CHECK_SECONDS
        self.assertIsNotNone(self.ask())

    def test_the_reading_a_stopped_rebuild_began_stops_too(self):
        """Stopped while it published: the same process goes on to read
        audio lengths, and that reading is asked by the token as well."""
        fresh = {"checked": 0.0, "gone": False}
        self.assertFalse(update_list.reading_should_stop(_state=fresh))
        update_list.request_run_stop([OTHER])
        self.assertFalse(update_list.reading_should_stop(_state=fresh))
        update_list.request_run_stop([TOKEN])
        self.assertTrue(update_list.reading_should_stop(_state=fresh))

    def test_earlier_requests_are_kept_beside_a_new_one(self):
        update_list.request_run_stop([OTHER])
        update_list.request_run_stop([TOKEN])
        self.assertTrue(update_list.run_stop_requested(OTHER))
        self.assertTrue(update_list.run_stop_requested(TOKEN))


class TheLock(DCCoreTestCase):
    """The list lock says whose run holds it, and taking it forgets an
    earlier run's answer whichever way it was taken."""

    def setUp(self):
        super().setUp()
        tree = self.make_tree()
        self.set_config(LIST_PROGRESS_FILE=os.path.join(tree.root, "progress.json"))
        tidy(self)

    def take(self, kind="rebuild", **kwargs):
        self.assertIsNone(update_list.take_list_lock(kind, **kwargs))
        self.addCleanup(update_list.release_list_lock)

    def test_a_run_the_bot_started_carries_its_token(self):
        as_a_run(self)
        self.take()
        self.assertEqual(update_list.list_lock_owner(), (os.getpid(), "rebuild", TOKEN))
        self.assertEqual(update_list.list_lock_holder(), (os.getpid(), "rebuild"))
        update_list.mark_list_lock("reading")
        self.assertEqual(update_list.list_lock_owner(), (os.getpid(), "reading", TOKEN))
        self.assertEqual(update_list.list_lock_holder(), (os.getpid(), "reading"))

    def test_a_run_by_hand_carries_none(self):
        as_a_run(self, token="")
        self.take()
        self.assertEqual(update_list.list_lock_owner(), (os.getpid(), "rebuild", ""))

    def test_a_refused_run_is_told_the_kind_alone(self):
        as_a_run(self)
        holder = platform_compat.take_file_lock(
            platform_compat.long_path(update_list.list_lock_path()), "rebuild " + OTHER)
        self.addCleanup(platform_compat.release_file_lock, holder)
        refused = update_list.take_list_lock("rebuild", log=lambda line: None, wait=0)
        self.assertEqual(refused[1], "rebuild")

    def test_taking_it_forgets_an_earlier_answer(self):
        as_a_run(self)
        update_list.rebuild_stop_reason(force=True, should_stop=lambda: True)
        self.assertIsNotNone(update_list.stopped_reason())
        self.take()
        self.assertIsNone(update_list.stopped_reason())

    def test_taking_it_from_a_reading_forgets_an_earlier_answer_too(self):
        """The rebuild that waited out a reading kept it: its next question
        answered with an earlier run's stop."""
        as_a_run(self)
        reading = platform_compat.take_file_lock(
            platform_compat.long_path(update_list.list_lock_path()), "reading")
        self.addCleanup(platform_compat.release_file_lock, reading)

        def obey():
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                if update_list.stop_requested():
                    platform_compat.release_file_lock(reading)
                    return
                time.sleep(0.01)

        reader = threading.Thread(target=obey, daemon=True)
        reader.start()
        update_list.rebuild_stop_reason(force=True, should_stop=lambda: True)
        self.take(log=lambda line: None, wait=5)
        reader.join(10)
        self.assertIsNone(update_list.stopped_reason())

    def test_a_request_for_this_run_survives_taking_it(self):
        """The bot asked while its rebuild still waited for the lock."""
        as_a_run(self)
        update_list.request_run_stop([TOKEN])
        self.take()
        self.assertEqual(update_list.rebuild_stop_reason(force=True),
                         "the bot is stopping and asked it to stop")


class ARebuildThatIsStopped(DCCoreTestCase):
    """generate_all_lists() over a real library, stopped at each place it asks."""

    def setUp(self):
        super().setUp()
        self.tree = self.make_tree()
        self.set_config(LIST_BASE_NAME="alfa", NICKNAME="alfa",
                        LIST_PROGRESS_FILE=os.path.join(self.tree.root, "progress.json"))
        tidy(self)
        as_a_run(self)
        # A first list, published, that a stopped rebuild must leave alone.
        self.assertTrue(self.rebuild())
        self.latest = list_mod.find_latest_list()
        with io.open(self.latest, encoding="utf-8") as handle:
            self.published = handle.read()
        self.files_before = sorted(os.listdir(self.tree.lists))
        # And something new in the library, so a rebuild that went through
        # would have written a different list.
        with open(os.path.join(self.tree.album, "03 - Holier Than Thou.flac"), "wb") as handle:
            handle.write(b"\x00" * 4096)

    def rebuild(self, stop=update_list.rebuild_stop_reason):
        """As rebuild_main() runs it: holding the list lock, asked `stop`."""
        self.out = io.StringIO()
        self.assertIsNone(update_list.take_list_lock("rebuild"))
        try:
            with contextlib.redirect_stdout(self.out):
                result = update_list.generate_all_lists(log=print, stop=stop)
        finally:
            update_list.release_list_lock()
        self.output = self.out.getvalue()
        return result

    def assert_the_previous_list_is_in_use(self):
        self.assertEqual(sorted(os.listdir(self.tree.lists)), self.files_before)
        self.assertEqual(list_mod.find_latest_list(), self.latest)
        with io.open(self.latest, encoding="utf-8") as handle:
            self.assertEqual(handle.read(), self.published)
        self.assertFalse(os.path.exists(config.LIST_PROGRESS_FILE), "the progress file was left behind")

    def test_a_stop_during_the_scan_publishes_nothing_and_ends_the_walk(self):
        """Looked at every directory here (the once-a-second limit set to
        0), so the walk ends after the directory the stop arrived in; and
        the walk is closed before the run says it stopped."""
        real = update_list.walk_with_sizes
        yielded, closed_after_saying_so = [], []

        def walk(top, onerror=None, workers=None):
            inner = real(top, onerror=onerror, workers=workers)
            try:
                for index, item in enumerate(inner):
                    if index == 0:
                        update_list.request_run_stop([TOKEN])     # the bot stops
                    yielded.append(item[0])
                    yield item
            finally:
                closed_after_saying_so.append("Stopped before publishing" in self.out.getvalue())
                inner.close()

        with mock.patch.object(update_list, "walk_with_sizes", walk), \
                mock.patch.object(update_list, "REBUILD_STOP_CHECK_SECONDS", 0):
            self.assertFalse(self.rebuild())
        self.assertIn("Stopped before publishing: the bot is stopping", self.output)
        self.assertEqual(len(yielded), 1, "the walk went on after the stop")
        self.assertEqual(closed_after_saying_so, [False])
        self.assert_the_previous_list_is_in_use()

        # The next run, with the stop gone, is not stopped by this one's answer.
        update_list.clear_run_stop_request()
        self.assertTrue(self.rebuild())

    def test_a_stop_after_the_scan_is_honoured_before_the_swap(self):
        """And said as a stop, not as "Failed to generate the lists"."""
        real_progress = update_list.write_progress
        swapped = []

        def progress(phase, *args, **kwargs):
            if phase == "writing":
                update_list.request_run_stop([TOKEN])
            return real_progress(phase, *args, **kwargs)

        with mock.patch.object(update_list, "write_progress", progress), \
                mock.patch.object(update_list, "_publish_artifacts", lambda swaps: swapped.append(swaps)):
            self.assertFalse(self.rebuild())
        self.assertEqual(swapped, [])
        self.assertIn("Stopped before publishing: the bot is stopping and asked it to stop. "
                      "The previous list was left untouched", self.output)
        self.assertNotIn("ERROR", self.output)
        self.assert_the_previous_list_is_in_use()
        self.assertEqual([n for n in os.listdir(self.tree.lists) if n.endswith(".new")], [],
                         "a staged list was left behind")

    def test_a_bot_that_is_gone_stops_it(self):
        as_a_run(self, daemon="4242")
        with mock.patch.object(update_list.platform_compat, "pid_alive", lambda pid: False), \
                mock.patch.dict(update_list._daemon_state, {"checked": 0.0, "gone": False}):
            self.assertFalse(self.rebuild())
        self.assertIn("the bot that started it is no longer running", self.output)
        self.assert_the_previous_list_is_in_use()

    def test_a_rebuild_run_by_hand_is_not_stopped(self):
        """No token: whatever stop the bot asks for is not this run's."""
        as_a_run(self, token="")
        update_list.request_run_stop([OTHER])
        update_list.request_stop()
        self.assertTrue(self.rebuild())
        with io.open(list_mod.find_latest_list(), encoding="utf-8") as handle:
            self.assertIn("Holier Than Thou", handle.read())

    def test_a_build_that_is_not_given_the_question_is_never_stopped(self):
        """A caller other than rebuild_main() - the audio-reading tests build
        lists this way - passes no `stop`."""
        update_list.request_run_stop([TOKEN])
        self.assertTrue(self.rebuild(stop=None))

    def test_a_bot_that_is_alive_does_not_stop_it(self):
        as_a_run(self, daemon="4242")
        with mock.patch.object(update_list.platform_compat, "pid_alive", lambda pid: True), \
                mock.patch.dict(update_list._daemon_state, {"checked": 0.0, "gone": False}):
            self.assertTrue(self.rebuild())

    def test_a_folder_that_fails_closes_its_walk_before_the_failure_leaves(self):
        """The walk's scan threads end with the folder, not whenever the
        generator is collected: the next list must not be built with them
        still listing."""
        real = update_list.walk_with_sizes
        closed_before_it_left, left = [], []

        def walk(top, onerror=None, workers=None):
            inner = real(top, onerror=onerror, workers=workers)
            try:
                yield from inner
            finally:
                closed_before_it_left.append(not left)
                inner.close()

        real_progress = update_list.write_progress

        def progress(phase, *args, **kwargs):
            if phase == "scanning" and not kwargs.get("force"):
                raise OSError("the mount went away")
            return real_progress(phase, *args, **kwargs)

        with mock.patch.object(update_list, "walk_with_sizes", walk), \
                mock.patch.object(update_list, "write_progress", progress):
            try:
                self.rebuild()
            except OSError as failure:
                left.append(failure)
        self.assertEqual([str(f) for f in left], ["the mount went away"])
        self.assertEqual(closed_before_it_left, [True])


class AStopBetweenFolders(DCCoreTestCase):
    """Two served folders: a stop that arrives as the first one's walk ends
    is seen before the second is started, not a second later inside it."""

    def test_the_next_folder_is_not_walked(self):
        import library
        tree = self.make_tree()
        second = os.path.join(tree.root, "more")
        os.makedirs(os.path.join(second, "Some Band", "Some Album (2001)"))
        with open(os.path.join(second, "Some Band", "Some Album (2001)", "01 - Intro.flac"), "wb") as handle:
            handle.write(b"\x00" * 4096)
        self.set_config(LIST_BASE_NAME="alfa", NICKNAME="alfa",
                        LIST_PROGRESS_FILE=os.path.join(tree.root, "progress.json"),
                        LIBRARY_FOLDERS_FILE=os.path.join(tree.root, "library_folders.json"))
        library.save_folders([library.Folder("Music", tree.music), library.Folder("More", second)])
        tidy(self)
        as_a_run(self)

        real = update_list.walk_with_sizes
        walked = []

        def walk(top, onerror=None, workers=None):
            walked.append(top)
            inner = real(top, onerror=onerror, workers=workers)
            try:
                yield from inner
            finally:
                update_list.request_run_stop([TOKEN])      # the bot stops as this folder ends
                inner.close()

        with mock.patch.object(update_list, "walk_with_sizes", walk), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertFalse(update_list.generate_all_lists(log=print, stop=update_list.rebuild_stop_reason))
        self.assertEqual(len(walked), 1, walked)


class AStoppedMultiListRun(DCCoreTestCase):

    def setUp(self):
        super().setUp()
        tree = self.make_tree()
        self.set_config(LIST_PROGRESS_FILE=os.path.join(tree.root, "progress.json"))
        tidy(self)
        as_a_run(self)
        self.lists = [mock.Mock(folders=[]), mock.Mock(folders=[])]
        self.lists[0].name, self.lists[1].name = "Main", "Films"

    def run_with(self, build):
        with mock.patch.object(update_list, "generate_master_list", build), \
                mock.patch("library.lists", return_value=self.lists), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            result = update_list.generate_all_lists(log=print, stop=update_list.rebuild_stop_reason)
        return result, out.getvalue()

    def test_no_further_list_is_built_and_it_says_stopped(self):
        built = []

        def build(name=None, stop=None, **kwargs):
            built.append(name)
            update_list.request_run_stop([TOKEN])
            stop(force=True)
            return False

        result, said = self.run_with(build)
        self.assertFalse(result)
        self.assertEqual(built, ["Main"])
        self.assertIn("Not building 'Films'", said)
        self.assertIn("[LIST-GEN] Stopped: these lists were not rebuilt", said)
        self.assertNotIn("ERROR", said)

    def test_a_stop_asked_in_the_last_second_of_a_list_is_seen_before_the_next(self):
        """The list's own walk asked within the second; the question between
        two lists is forced, so it does not wait for the next list's."""
        built = []

        def build(name=None, stop=None, **kwargs):
            built.append(name)
            stop()                                  # the walk's throttled question
            update_list.request_run_stop([TOKEN])   # the bot stops right after
            return True

        result, said = self.run_with(build)
        self.assertFalse(result)
        self.assertEqual(built, ["Main"])

    def test_a_failure_is_still_a_failure(self):
        result, said = self.run_with(lambda name=None, stop=None, **kwargs: False)
        self.assertFalse(result)
        self.assertIn("[LIST-GEN ERROR] These lists were not rebuilt", said)


class TheRunsEnd(DCCoreTestCase):
    """rebuild_main()'s exit code and last line: a stop is not a failure."""

    def setUp(self):
        super().setUp()
        tree = self.make_tree()
        self.set_config(LIST_BASE_NAME="alfa", NICKNAME="alfa",
                        LIST_PROGRESS_FILE=os.path.join(tree.root, "progress.json"))
        tidy(self)
        as_a_run(self)

    def test_a_stopped_rebuild_exits_with_its_own_code(self):
        def build(log=print, reading_jobs=None, stop=None):
            update_list.request_run_stop([TOKEN])
            stop(force=True)
            return False

        said = []
        with mock.patch.object(update_list, "generate_all_lists", build):
            code = update_list.rebuild_main(log=said.append)
        self.assertEqual(code, update_list.REBUILD_STOPPED_EXIT)
        self.assertIn("--- Stopped: the bot is stopping and asked it to stop. "
                      "The published list stays as it is. ---", said)
        self.assertFalse(any("ERROR" in line for line in said), said)

    def test_a_failed_one_still_exits_1(self):
        said = []
        with mock.patch.object(update_list, "generate_all_lists",
                               lambda log=print, reading_jobs=None, stop=None: False):
            code = update_list.rebuild_main(log=said.append)
        self.assertEqual(code, 1)
        self.assertIn("--- ERROR: could not generate the list. ---", said)


class TheBotStopsItsRebuild(DCCoreTestCase):
    """commands.stop_rebuild(), which the shutdown calls."""

    def setUp(self):
        super().setUp()
        tree = self.make_tree()
        self.set_config(LIST_PROGRESS_FILE=os.path.join(tree.root, "progress.json"),
                        update_inprogress=True)
        tidy(self)
        tokens = mock.patch.object(runtime, "run_tokens", (OTHER, TOKEN))
        tokens.start()
        self.addCleanup(tokens.stop)
        self.clock = Clock()
        self.asked_while_waiting = []

    def stop(self, owners, wait=1.0, ends_at=None):
        """`owners`: the lock's owner at each poll, the last one repeated;
        the bot's rebuild ends (update_inprogress drops) at poll `ends_at`."""
        answers = list(owners)
        polls = [0]

        def owner():
            polls[0] += 1
            if ends_at is not None and polls[0] > ends_at:
                config.update_inprogress = False
            self.asked_while_waiting.append(update_list.run_stop_requested(TOKEN))
            return answers.pop(0) if len(answers) > 1 else answers[0]

        def sleep(seconds):
            self.clock.now += seconds

        with mock.patch.object(update_list, "list_lock_owner", owner), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            result = commands.stop_rebuild(wait=wait, poll=0.25, sleep=sleep, clock=self.clock)
        self.said = out.getvalue()
        return result

    def test_it_asks_by_token_and_clears_the_request_once_its_rebuild_has_ended(self):
        mine = (77, "rebuild", TOKEN)
        self.assertTrue(self.stop([mine, mine, mine, None], ends_at=3))
        self.assertTrue(all(self.asked_while_waiting), "asked by its token")
        self.assertFalse(update_list.run_stop_requested(TOKEN), "cleared at the end")
        self.assertFalse(update_list.stop_requested(), "the reading's file is not how it asks")

    def test_a_rebuild_still_holding_on_keeps_the_request(self):
        """It may be in the middle of a directory; the file is what it will see."""
        self.assertTrue(self.stop([(77, "rebuild", TOKEN)], wait=1.0))
        self.assertTrue(update_list.run_stop_requested(TOKEN))

    def test_the_reading_it_began_after_publishing_is_waited_for(self):
        """Stopped while it published: the child swapped and marked the lock
        `reading`, and the bot's thread let go of update_inprogress. That
        reading is still this bot's run, asked by the same token."""
        self.assertTrue(self.stop([(77, "rebuild", TOKEN), (77, "reading", TOKEN)], ends_at=1))
        self.assertTrue(update_list.run_stop_requested(TOKEN), "cleared under the reading")

    def test_a_terminals_rebuild_is_not_asked(self):
        """The bot's own child was refused because a terminal's rebuild holds
        the lock; the bot stopped before its thread let go of the flag."""
        self.assertTrue(self.stop([(88, "rebuild", "")], ends_at=1))
        self.assertFalse(update_list.run_stop_requested(""))
        self.assertFalse(update_list.stop_requested())
        self.assertFalse(update_list.run_stop_requested(TOKEN), "done once its own run ended")

    def test_its_rebuild_still_waiting_behind_a_reading_is_asked(self):
        """The child has not got the lock yet; it is asked all the same, and
        sees the request once it has."""
        self.assertTrue(self.stop([(99, "reading", "")]))
        self.assertTrue(update_list.run_stop_requested(TOKEN))

    def test_no_rebuild_of_its_own_is_left_alone(self):
        self.set_config(update_inprogress=False)
        self.assertFalse(self.stop([(77, "rebuild", TOKEN)]))
        self.assertFalse(update_list.run_stop_requested(TOKEN))

    def test_a_request_that_cannot_be_written_is_not_waited_for(self):
        naps = []

        def sleep(seconds):
            naps.append(seconds)
            self.clock.now += seconds

        with mock.patch.object(update_list, "request_run_stop", lambda tokens: False), \
                mock.patch.object(update_list, "list_lock_owner", lambda: (77, "rebuild", TOKEN)), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertTrue(commands.stop_rebuild(wait=10.0, sleep=sleep, clock=self.clock))
        self.assertEqual(naps, [])
        self.assertIn("Could not ask the list rebuild to stop", out.getvalue())

    def test_the_bot_keeps_the_tokens_it_gives(self):
        """How it knows its runs at shutdown: every run it starts, the last
        RUN_TOKENS_KEPT of them."""
        runtime.run_tokens = ()
        given = [commands.audio_run_env()[0] for _ in range(runtime.RUN_TOKENS_KEPT + 2)]
        self.assertEqual(list(runtime.run_tokens), given[-runtime.RUN_TOKENS_KEPT:])


class AnOrphanedRebuild(DCCoreTestCase):
    """commands.stop_orphaned_rebuild(), which the startup calls."""

    def setUp(self):
        super().setUp()
        tree = self.make_tree()
        self.set_config(LIST_PROGRESS_FILE=os.path.join(tree.root, "progress.json"))
        tidy(self)
        tokens = mock.patch.object(runtime, "run_tokens", ())
        tokens.start()
        self.addCleanup(tokens.stop)

    def look(self, owner):
        with mock.patch.object(update_list, "list_lock_owner", lambda: owner), \
                contextlib.redirect_stdout(io.StringIO()):
            return commands.stop_orphaned_rebuild()

    def test_one_the_last_run_started_is_asked_to_stop(self):
        self.assertTrue(self.look((55, "rebuild", OTHER)))
        self.assertTrue(update_list.run_stop_requested(OTHER))

    def test_a_terminals_is_left_alone(self):
        self.assertFalse(self.look((55, "rebuild", "")))
        self.assertFalse(os.path.exists(update_list.run_stop_path()))

    def test_a_reading_is_for_stop_orphaned_reading(self):
        self.assertFalse(self.look((55, "reading", OTHER)))
        self.assertFalse(update_list.run_stop_requested(OTHER))

    def test_nothing_held_is_nothing_to_do(self):
        self.assertFalse(self.look(None))


class TheShutdown(DCCoreTestCase):
    """oserve._shut_down(): the rebuild is asked before the reading, and a
    rebuild that cannot be asked does not leave the reading running."""

    def shut_down(self, rebuild=None):
        import announce
        import dcc
        import irc
        import stopping
        from tests.test_startup import real_oserve
        oserve = real_oserve()

        calls = []

        def stop_rebuild(wait=None):
            calls.append("rebuild")
            if rebuild is not None:
                raise rebuild

        with mock.patch.object(commands, "stop_rebuild", stop_rebuild), \
                mock.patch.object(commands, "stop_audio_reading",
                                  lambda wait=None: calls.append("reading")), \
                mock.patch.object(stopping, "clear_stale_stop_file", lambda: None), \
                mock.patch.object(irc, "_flush_known_bots", lambda force=False: None), \
                mock.patch.object(announce, "flush_private_messages", lambda: None), \
                mock.patch.object(dcc, "record_transfers_cut_off", lambda: None), \
                contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(SystemExit) as ended:
                oserve._shut_down()
        self.assertEqual(ended.exception.code, 0)
        return calls

    def test_the_rebuild_is_asked_before_the_reading(self):
        self.assertEqual(self.shut_down(), ["rebuild", "reading"])

    def test_a_rebuild_that_could_not_be_asked_still_stops_the_reading(self):
        self.assertEqual(self.shut_down(rebuild=RuntimeError("no")), ["rebuild", "reading"])


class TheBotReportsAStoppedRebuild(DCCoreTestCase):
    """async_list_updater(): update_list.py's REBUILD_STOPPED_EXIT is said as
    a stop, not announced as a failed rebuild."""

    def test_it_is_not_announced_as_a_failure(self):
        import announce
        from tests.test_commands import _SyncThread

        debug = []
        self.set_config(update_inprogress=False, search_inprogress=False)
        result = types.SimpleNamespace(returncode=update_list.REBUILD_STOPPED_EXIT,
                                       stdout="--- Stopped: the bot is stopping. ---\n", stderr="")
        with mock.patch.object(commands, "run_watching_for_a_stall", lambda argv, **kwargs: result), \
                mock.patch.object(commands, "stop_audio_reading", lambda *a, **k: False), \
                mock.patch.object(threading, "Thread", _SyncThread), \
                mock.patch.object(announce, "send_debug",
                                  lambda message, *a, **k: debug.append(message)), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            commands.handle_list_update_request("admin", "#chan", authorised=True)
        self.assertFalse(any("failed" in message.lower() for message in debug), debug)
        self.assertIn("The list rebuild stopped with the bot", out.getvalue())
        self.assertFalse(config.last_list_update_ok)
        self.assertIn("stopped", config.last_list_update_error)
        self.assertFalse(config.update_inprogress)


if __name__ == "__main__":
    unittest.main()
