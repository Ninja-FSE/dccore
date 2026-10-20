"""A channel can take the advert but not the "Sent:" line (#1444).

What was wrong before: ANNOUNCE_TRANSFERS was all or nothing. With it on,
every channel was told of every finished transfer, and the only way to keep
the line out of one channel that does not allow that kind of message was to
set the channel Quiet or Request only (#1204) - which stops the advert there
too. Nothing said "advertise here, but do not announce transfers".

ANNOUNCE_TRANSFERS_EXCLUDE names those channels. Held here, by driving the
real code:

  - send_transfer_complete() queues no channel line for a listed channel and
    logs why, naming the setting; the SENT feed event still goes out; any
    other channel is told as before; with the tickbox off, or for a private
    request, the log is the one it was;
  - the names are compared under the server's casemapping (#1271), not by
    str.lower(): "#Alpha" matches "#alpha", and "#a[1]" matches "#a{1}" on an
    RFC 1459 network and not on an ascii one;
  - both settings are read under runtime.config_reload_lock: a transfer that
    finishes while a !rehash has put the shipped [] back, a moment before
    settings.conf is applied again, waits for the operator's list rather
    than announcing in a channel that asked not to be told;
  - settings_file.channel_names() and coerce()/save() take only channels,
    drop blanks and case-repeats, and a settings.conf line keeps the
    channels it has (load_value()), with CHANNEL's own note unchanged;
  - the Settings page offers it right after the tickbox, labelled in every
    language, and the mIRC settings window places it once, in its Advert
    section.
"""

import contextlib
import io
import json
import os
import sys
import threading
import time
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MIRC_DIR = os.path.join(REPO_ROOT, "scripts", "mirc")
for _path in (REPO_ROOT, os.path.join(REPO_ROOT, "src"), MIRC_DIR):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import announce  # noqa: E402
import commands  # noqa: E402
import defaults as config  # noqa: E402
import runtime  # noqa: E402
import settings_file  # noqa: E402
import webserver  # noqa: E402

from tests.support import DCCoreTestCase, silence_debug  # noqa: E402

KEY = "ANNOUNCE_TRANSFERS_EXCLUDE"
EXCLUDE_LOG = "is not told (ANNOUNCE_TRANSFERS_EXCLUDE)"
TICKBOX_LOG = "channel notice is off (ANNOUNCE_TRANSFERS)"
PRIVATE_LOG = "asked for by private message"


class SentLineCase(DCCoreTestCase):

    def setUp(self):
        super().setUp()
        self.debug = silence_debug(announce)
        self.set_config(NICKNAME="SampleBot", CHANNEL="#alpha,#beta",
                        ANNOUNCE_TRANSFERS=True, ANNOUNCE_TRANSFERS_EXCLUDE=[])
        self.addCleanup(setattr, runtime, "server_casemapping", runtime.server_casemapping)

    def finish(self, channel, private=False):
        """One finished transfer to alice, as dcc.py reports it; returns
        what the console was told."""
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            announce.send_transfer_complete(channel, "alice", "Song.flac", 4096,
                                            time.time() - 30, 1000, duration=1.0,
                                            private=private)
        return out.getvalue()

    def told(self):
        """The channel lines queued through the stub oserve."""
        return [message for kind, message, *_ in self.oserve.queued
                if kind == "channel_announce"]

    def sent_events(self):
        return [text for category, text in self.debug if category == "SENT"]


class AListedChannelIsNotTold(SentLineCase):

    def test_no_channel_line_and_the_log_names_the_setting(self):
        self.set_config(ANNOUNCE_TRANSFERS_EXCLUDE=["#alpha"])

        printed = self.finish("#alpha")

        self.assertEqual(self.told(), [])
        self.assertIn("#alpha " + EXCLUDE_LOG, printed)
        self.assertNotIn(TICKBOX_LOG, printed)

    def test_the_sent_feed_event_still_goes_out(self):
        """The operator's own record of the send is not the channel's line."""
        self.set_config(ANNOUNCE_TRANSFERS_EXCLUDE=["#alpha"])

        self.finish("#alpha")

        events = self.sent_events()
        self.assertEqual(len(events), 1, self.debug)
        self.assertIn("Song.flac", events[0])
        self.assertIn("alice", events[0])

    def test_a_channel_not_in_the_list_is_still_told(self):
        self.set_config(ANNOUNCE_TRANSFERS_EXCLUDE=["#alpha"])

        printed = self.finish("#beta")

        lines = self.told()
        self.assertEqual(len(lines), 1, printed)
        self.assertTrue(lines[0].startswith("PRIVMSG #beta :"), lines[0])
        self.assertIn("Song.flac", lines[0])
        self.assertNotIn(EXCLUDE_LOG, printed)
        self.assertEqual(len(self.sent_events()), 1)

    def test_an_empty_list_tells_every_channel(self):
        for channel in ("#alpha", "#beta"):
            with self.subTest(channel=channel):
                self.oserve.queued.clear()
                printed = self.finish(channel)
                self.assertEqual(len(self.told()), 1, printed)
                self.assertNotIn(EXCLUDE_LOG, printed)

    def test_a_comma_separated_string_works_too(self):
        """A value set from admin_config.py as one string, not a list."""
        self.set_config(ANNOUNCE_TRANSFERS_EXCLUDE="#gamma, #alpha")

        self.assertIn(EXCLUDE_LOG, self.finish("#alpha"))
        self.assertEqual(self.told(), [])
        self.finish("#beta")
        self.assertEqual(len(self.told()), 1)


class TheOtherReasonsAreLoggedAsTheyWere(SentLineCase):

    def test_tickbox_off_names_the_tickbox_not_the_list(self):
        """The tickbox is the reason nothing is posted anywhere: an excluded
        channel must not make the log blame the list instead."""
        self.set_config(ANNOUNCE_TRANSFERS=False, ANNOUNCE_TRANSFERS_EXCLUDE=["#alpha"])

        printed = self.finish("#alpha")

        self.assertEqual(self.told(), [])
        self.assertIn(TICKBOX_LOG, printed)
        self.assertNotIn(EXCLUDE_LOG, printed)
        self.assertEqual(len(self.sent_events()), 1)

    def test_a_private_request_is_unchanged(self):
        self.set_config(ANNOUNCE_TRANSFERS_EXCLUDE=["#alpha"])

        for channel in ("#alpha", "#beta"):
            with self.subTest(channel=channel):
                self.debug.clear()
                printed = self.finish(channel, private=True)
                self.assertEqual(self.told(), [])
                self.assertIn(PRIVATE_LOG, printed)
                self.assertNotIn(EXCLUDE_LOG, printed)
                self.assertEqual(len(self.sent_events()), 1)

    def test_a_quiet_channel_not_in_the_list_is_still_quiet(self):
        """The mode gate after the new check is the one it was (#1204)."""
        tree = self.make_tree()
        store = os.path.join(tree.root, "lists.json")
        with io.open(store, "w", encoding="utf-8") as handle:
            json.dump([{"name": "Main", "primary": True, "channels": ["#alpha", "#beta"],
                        "folders": [{"name": "Music", "path": tree.music}],
                        "modes": {"#beta": "quiet"}}], handle)
        self.set_config(LISTS_FILE=store,
                        LIBRARY_FOLDERS_FILE=os.path.join(tree.root, "library_folders.json"),
                        FILE_DIRECTORY=tree.music,
                        ANNOUNCE_TRANSFERS_EXCLUDE=["#alpha"])

        printed = self.finish("#beta")

        self.assertEqual(self.told(), [])
        self.assertIn(TICKBOX_LOG, printed)
        self.assertNotIn(EXCLUDE_LOG, printed)


class NamesAreComparedAsTheServerComparesThem(SentLineCase):

    def test_case_does_not_matter(self):
        for listed, channel in (("#Alpha", "#alpha"), ("#alpha", "#ALPHA")):
            with self.subTest(listed=listed, channel=channel):
                self.oserve.queued.clear()
                self.set_config(ANNOUNCE_TRANSFERS_EXCLUDE=[listed])
                self.assertIn(EXCLUDE_LOG, self.finish(channel))
                self.assertEqual(self.told(), [])

    def test_rfc1459_brackets_are_one_channel(self):
        runtime.server_casemapping = "rfc1459"
        self.set_config(ANNOUNCE_TRANSFERS_EXCLUDE=["#a[1]"])

        self.assertTrue(announce._channel_excluded_from_announce("#a{1}", ["#a[1]"]))
        self.assertIn(EXCLUDE_LOG, self.finish("#a{1}"))
        self.assertEqual(self.told(), [])

    def test_but_not_on_an_ascii_server(self):
        runtime.server_casemapping = "ascii"
        self.set_config(ANNOUNCE_TRANSFERS_EXCLUDE=["#a[1]"])

        self.assertFalse(announce._channel_excluded_from_announce("#a{1}", ["#a[1]"]))
        self.assertTrue(announce._channel_excluded_from_announce("#A[1]", ["#a[1]"]))
        printed = self.finish("#a{1}")
        self.assertEqual(len(self.told()), 1, printed)

    def test_blank_entries_and_a_blank_channel_match_nothing(self):
        listed = ["", "  ", "#alpha"]

        self.assertFalse(announce._channel_excluded_from_announce("", listed))
        self.assertFalse(announce._channel_excluded_from_announce(None, listed))
        self.assertFalse(announce._channel_excluded_from_announce("#beta", listed))
        self.assertTrue(announce._channel_excluded_from_announce(" #alpha ", listed))
        self.assertFalse(announce._channel_excluded_from_announce("#alpha", []))
        self.assertFalse(announce._channel_excluded_from_announce("#alpha", None))


class WatchedLock:
    """Stands in for runtime.config_reload_lock around the real one, so a
    test knows - without sleeping - that a thread has asked for it.

    `asked` is set the moment a `with` reaches the acquire, `entries` counts
    the acquires that got through, and `after_first_exit`, if set, runs once
    on the thread that leaves the first `with`, just after it lets go.
    """

    def __init__(self, inner):
        self.inner = inner
        self.asked = threading.Event()
        self.entries = 0
        self.after_first_exit = None

    def __enter__(self):
        self.asked.set()
        self.inner.acquire()
        self.entries += 1
        return self

    def __exit__(self, *exc):
        self.inner.release()
        hook, self.after_first_exit = self.after_first_exit, None
        if hook is not None:
            hook()
        return False


WAIT = 10       # seconds: only ever reached when something is already wrong


class ASendDuringARehashWaitsForTheOperatorsList(SentLineCase):
    """A !rehash reloads defaults.py, which puts every setting back to its
    shipped value for a moment before settings.conf is applied again. A
    transfer finishing in that moment read ANNOUNCE_TRANSFERS_EXCLUDE as []
    and told the channel that had asked not to be told. The reload holds
    runtime.config_reload_lock throughout; the two reads must wait for it.

    The main thread plays the rehash: it holds the real lock, sets the
    shipped value, and puts the operator's back before letting go.
    """

    def setUp(self):
        super().setUp()
        self.lock = WatchedLock(runtime.config_reload_lock)
        self.addCleanup(setattr, runtime, "config_reload_lock", runtime.config_reload_lock)
        runtime.config_reload_lock = self.lock
        self.errors = []
        self.out = io.StringIO()
        redirect = contextlib.redirect_stdout(self.out)
        redirect.__enter__()
        self.addCleanup(redirect.__exit__, None, None, None)

    def start_send(self, channel):
        def finish():
            try:
                announce.send_transfer_complete(channel, "alice", "Song.flac", 4096,
                                                time.time() - 30, 1000, duration=1.0)
            except BaseException as err:     # reported by the test, not lost on a thread
                self.errors.append(err)
        worker = threading.Thread(target=finish, daemon=True)
        worker.start()
        self.addCleanup(worker.join, WAIT)
        return worker

    def finished(self, worker):
        worker.join(WAIT)
        self.assertFalse(worker.is_alive(), "the send never finished")
        self.assertEqual(self.errors, [])

    def test_the_send_waits_and_then_reads_the_list_put_back(self):
        self.set_config(ANNOUNCE_TRANSFERS=True, ANNOUNCE_TRANSFERS_EXCLUDE=["#alpha"])

        self.lock.inner.acquire()
        try:
            config.ANNOUNCE_TRANSFERS_EXCLUDE = []          # the reload's literal
            worker = self.start_send("#alpha")
            self.assertTrue(self.lock.asked.wait(WAIT), "the send never asked for the lock")
            # Held by this thread, so nothing can have got through: no
            # timing involved in either assertion.
            self.assertEqual(self.lock.entries, 0)
            self.assertTrue(worker.is_alive())
            self.assertEqual(self.told(), [])
            config.ANNOUNCE_TRANSFERS_EXCLUDE = ["#alpha"]  # settings.conf applied again
        finally:
            self.lock.inner.release()

        self.finished(worker)
        self.assertEqual(self.told(), [])
        self.assertIn("#alpha " + EXCLUDE_LOG, self.out.getvalue())

    def test_the_tickbox_is_read_under_the_lock_too(self):
        """The operator switched announcing off; the reload's literal is on."""
        self.set_config(ANNOUNCE_TRANSFERS=False, ANNOUNCE_TRANSFERS_EXCLUDE=[])

        self.lock.inner.acquire()
        try:
            config.ANNOUNCE_TRANSFERS = True
            worker = self.start_send("#beta")
            self.assertTrue(self.lock.asked.wait(WAIT), "the send never asked for the lock")
            self.assertEqual(self.lock.entries, 0)
            config.ANNOUNCE_TRANSFERS = False
        finally:
            self.lock.inner.release()

        self.finished(worker)
        self.assertEqual(self.told(), [])
        self.assertIn(TICKBOX_LOG, self.out.getvalue())

    def test_the_list_read_under_the_lock_is_the_one_used(self):
        """Read once, under the lock, and that value decides. A second read
        of the setting later, outside the lock, can land in the next
        rehash's window: here one starts the moment the first read is done."""
        self.set_config(ANNOUNCE_TRANSFERS=True, ANNOUNCE_TRANSFERS_EXCLUDE=["#alpha"])
        read, reloading = threading.Event(), threading.Event()

        def a_rehash_starts():
            read.set()
            reloading.wait(WAIT)

        self.lock.after_first_exit = a_rehash_starts
        worker = self.start_send("#alpha")
        self.assertTrue(read.wait(WAIT), "the send never read the settings")
        self.lock.inner.acquire()
        try:
            config.ANNOUNCE_TRANSFERS_EXCLUDE = []
            reloading.set()
            worker.join(WAIT)      # needs no lock from here; bounded if it does
            config.ANNOUNCE_TRANSFERS_EXCLUDE = ["#alpha"]
        finally:
            self.lock.inner.release()

        self.finished(worker)
        self.assertEqual(self.told(), [])
        self.assertIn("#alpha " + EXCLUDE_LOG, self.out.getvalue())


class ItShipsEmpty(unittest.TestCase):

    def test_the_default_announces_everywhere(self):
        """An upgrade must not silently stop announcing anywhere."""
        self.assertEqual(config.SHIPPED_VALUES[KEY], [])

    def test_it_is_declared_a_list(self):
        self.assertIs(settings_file.declared_types(vars(config))[KEY], list)


class OnlyChannelsAreTaken(DCCoreTestCase):

    def setUp(self):
        super().setUp()
        self.keep_every_setting()
        self.home = self.make_temp_dir(prefix="dccore-exclude-")
        self.path = os.path.join(self.home, "settings.conf")

    def coerce(self, raw):
        types = settings_file.declared_types(vars(config))
        return settings_file.coerce(KEY, raw, config.SHIPPED_VALUES[KEY], types.get(KEY))

    def save(self, value):
        return settings_file.save(vars(config), {KEY: value}, path=self.path,
                                  log=lambda *_: None)

    def load(self, text):
        with open(self.path, "w", encoding="utf-8") as handle:
            handle.write(text)
        said = []
        report = settings_file.apply_to(dict(vars(config)), path=self.path, log=said.append)
        return report, said

    def test_channel_names_tidies_a_list(self):
        self.assertEqual(settings_file.channel_names("#a, #A ,, #b"), ["#a", "#b"])
        self.assertEqual(settings_file.channel_names("#Alpha,#alpha"), ["#Alpha"])
        self.assertEqual(settings_file.channel_names("&chan"), ["&chan"])
        self.assertEqual(settings_file.channel_names(""), [])
        self.assertEqual(settings_file.channel_names(" , "), [])

    def test_channel_names_refuses_what_is_not_a_channel(self):
        for bad in ("alpha", "#a b", "#", "#alpha, beta"):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError) as caught:
                    settings_file.channel_names(bad)
                self.assertIn("not a channel", str(caught.exception))

    def test_a_missing_hash_is_refused_not_added(self):
        with self.assertRaises(ValueError) as caught:
            self.coerce("alpha")
        self.assertIn("#", str(caught.exception))

    def test_coerce_reads_it_the_same_way(self):
        self.assertEqual(self.coerce("#a, #A ,, #b"), ["#a", "#b"])
        self.assertEqual(self.coerce(""), [])
        self.assertEqual(self.coerce("&chan"), ["&chan"])
        for bad in ("alpha", "#a b", "#"):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    self.coerce(bad)

    def test_check_change_is_the_same_rule(self):
        self.assertEqual(settings_file.check_change(vars(config), KEY, "#a, #A ,, #b"),
                         ["#a", "#b"])

    def test_save_refuses_a_bad_entry_and_writes_nothing(self):
        for bad in ("alpha", "#a b", "#", "#alpha, beta"):
            with self.subTest(bad=bad):
                with self.assertRaises(settings_file.SettingsWriteError):
                    self.save(bad)
                self.assertFalse(os.path.exists(self.path))

    def test_save_writes_the_tidied_list(self):
        self.assertEqual(self.save("#a, #A ,, #b")["written"], [KEY])
        with open(self.path, encoding="utf-8") as handle:
            written = settings_file.parse(handle.read())
        self.assertEqual(self.coerce(written[KEY]), ["#a", "#b"])

    def test_save_takes_empty(self):
        self.save("")
        with open(self.path, encoding="utf-8") as handle:
            text = handle.read()
        report, _said = self.load(text)
        self.assertEqual(report["applied"][KEY], [])
        self.assertEqual(report["bad"], [])

    def test_a_file_line_keeps_its_channels_and_says_what_it_dropped(self):
        report, said = self.load("# mine\nANNOUNCE_TRANSFERS_EXCLUDE = #alpha, beta, #ALPHA, #gamma\n")
        self.assertEqual(report["applied"][KEY], ["#alpha", "#gamma"])
        self.assertEqual(report["bad"], [])
        self.assertEqual([name for name, *_ in report["repaired"]], [KEY])
        lines = [line for line in said if KEY in line]
        self.assertEqual(len(lines), 1, said)
        self.assertIn("line 2", lines[0])
        self.assertIn("'beta'", lines[0])
        self.assertIn("#alpha, #gamma", lines[0])

    def test_the_note_names_where_the_list_can_be_saved(self):
        """configure.py does not ask for this setting; the places that do
        are the ones to name."""
        _value, note = settings_file.load_value(KEY, "#alpha, beta", [], list)
        for place in ("the Settings page", "the console", "the mIRC settings window"):
            self.assertIn(place, note)
        self.assertNotIn("configure.py", note)

    def test_channels_own_note_is_word_for_word_what_it_was(self):
        """CHANNEL's branch now shares its helper with this one; the line an
        operator of CHANNEL reads must not have moved with it (#1272)."""
        value, note = settings_file.load_value("CHANNEL", "#example-room, jazzroom", None, str)
        self.assertEqual(value, "#example-room")
        self.assertEqual(note,
                         "ignoring 'jazzroom' ('jazzroom' does not start with #; a channel is "
                         "written like #jazzroom) and joining #example-room. Correct the line; "
                         "the Settings page and configure.py only save a list where every "
                         "entry is a channel")
        value, note = settings_file.load_value("CHANNEL", "jazzroom, #a, #B, bad room", None, str)
        self.assertEqual(value, "#a,#B")
        self.assertIn("and joining #a, #B. ", note)
        self.assertIsNone(settings_file.load_value("CHANNEL", "#a, #b", None, str)[1])

    def test_load_value_says_nothing_for_a_clean_line(self):
        value, note = settings_file.load_value(KEY, "#alpha, #beta", [], list)
        self.assertEqual(value, ["#alpha", "#beta"])
        self.assertIsNone(note)

    def test_a_file_line_with_no_channel_at_all_is_ignored(self):
        report, _said = self.load("ANNOUNCE_TRANSFERS_EXCLUDE = alpha, beta\n")
        self.assertNotIn(KEY, report["applied"])
        self.assertEqual([name for name, _why in report["bad"]], [KEY])


class TheSettingsPageOffersIt(DCCoreTestCase):

    def setUp(self):
        super().setUp()
        self.keep_every_setting()
        # A 200 from the route dispatches the real rehash on its own thread;
        # a no-op here, as tests/test_webserver.py does, keeps it in this test.
        real_rehash = commands.handle_rehash_request
        commands.handle_rehash_request = lambda *a, **kw: None
        self.addCleanup(setattr, commands, "handle_rehash_request", real_rehash)

    def test_it_sits_right_after_the_tickbox(self):
        fields = dict((cid, keys) for cid, _label, keys in webserver.SETTINGS_CATEGORIES)
        advertising = fields["advertising"]
        self.assertEqual(advertising.index(KEY), advertising.index("ANNOUNCE_TRANSFERS") + 1)
        self.assertEqual(sum(keys.count(KEY) for keys in fields.values()), 1)

    def test_the_page_shows_it_as_a_list_with_a_label(self):
        self.set_config(**{KEY: ["#alpha", "#beta"]})
        payload = webserver.build_settings_payload()
        found = [field for category in payload["categories"] if category["id"] == "advertising"
                 for field in category["fields"] if field["name"] == KEY]
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["type"], "list")
        self.assertEqual(found[0]["value"], ["#alpha", "#beta"])
        self.assertEqual(found[0]["label"], webserver.SETTINGS_LABELS[KEY])
        self.assertTrue(found[0]["help"])

    def test_every_language_labels_it(self):
        for lang in ("en", "es", "fr"):
            with self.subTest(lang=lang):
                with open(os.path.join(REPO_ROOT, "web", "lang", lang + ".json"),
                          encoding="utf-8") as handle:
                    strings = json.load(handle)
                self.assertTrue(strings.get("settings.field." + KEY, "").strip())
                if lang != "en":
                    self.assertTrue(strings.get("settings.field." + KEY + ".help", "").strip())
        with open(os.path.join(REPO_ROOT, "web", "lang", "en.json"), encoding="utf-8") as handle:
            self.assertEqual(json.load(handle)["settings.field." + KEY],
                             webserver.SETTINGS_LABELS[KEY])

    def test_the_page_refuses_a_name_without_its_hash(self):
        status, payload = webserver.apply_settings_changes({KEY: "#alpha, beta"})
        self.assertEqual(status, 400, payload)
        self.assertIn(KEY, payload["error"])
        self.assertFalse(os.path.exists(settings_file.settings_path()))

    def test_the_page_saves_a_good_list(self):
        status, payload = webserver.apply_settings_changes({KEY: "#alpha, #Alpha, #beta"})
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["written"], [KEY])
        with open(settings_file.settings_path(), encoding="utf-8") as handle:
            written = settings_file.parse(handle.read())
        self.assertEqual(settings_file.channel_names(written[KEY]), ["#alpha", "#beta"])


class TheMircWindowPlacesIt(unittest.TestCase):

    def test_once_in_the_advert_section_after_the_interval(self):
        """General > Advertising > Advert: with the advert it is about, not
        with the General Settings tickboxes, where a text field had no room."""
        import settings_window_layout as layout
        places = []
        for group, pages in layout.GROUPS:
            for page, sections in pages:
                for header, items in sections:
                    keys = [item if isinstance(item, str) else item.get("key") for item in items]
                    if KEY in keys:
                        places.append((group, page, header, keys))
        self.assertEqual(len(places), 1, places)
        group, page, header, keys = places[0]
        self.assertEqual((group, page, header), ("General", "Advertising", "Advert"))
        self.assertEqual(keys.index(KEY), keys.index("ANNOUNCE_INTERVAL") + 1)
        self.assertNotIn(KEY, layout.EXCLUDED)

    def test_the_generated_block_carries_it_as_a_list(self):
        import build_settings_window as generator
        block, _builder = generator.generate()
        rows = [line.split() for line in block
                if line.strip().startswith("hadd dccore.swm k.%s " % KEY)]
        self.assertEqual(len(rows), 1, rows)
        self.assertEqual(rows[0][4], "list")
        labels = [line for line in block if line.strip().startswith("hadd dccore.swm n.%s " % KEY)]
        self.assertEqual([line.strip().split(" ", 3)[3] for line in labels],
                         [webserver.SETTINGS_LABELS[KEY]])


if __name__ == "__main__":
    unittest.main()
