"""library.py's list model - stage 1 of #26.

NOTHING THE DAEMON DOES CHANGES HERE. With no lists.json on disk, which is
every install, lists() returns ONE list built from exactly the folders
folders() already resolved, and every one of the thirteen existing callers
sees what it saw before. That equivalence is the property most worth pinning,
because the whole argument for putting the accessor in first is that the
folder set can move inside a list without touching those callers again.

The rest is the rules an operator meets the first time they define two lists,
and the answers this module has to give for a request to be routable at all:
which list has no channel, which list a private message means, and what
happens to a channel bound to nothing.
"""

import io
import json
import os
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)
if os.path.join(REPO_ROOT, "tests") not in sys.path:
    sys.path.insert(0, os.path.join(REPO_ROOT, "tests"))

import defaults as config  # noqa: E402
import library  # noqa: E402
import list as list_mod  # noqa: E402
import webserver  # noqa: E402

from tests.support import DCCoreTestCase  # noqa: E402


class ListCase(DCCoreTestCase):
    """A scratch tree, and a lists file that does not exist yet."""

    def setUp(self):
        super().setUp()
        self.tree = self.make_tree()
        self.store = os.path.join(self.tree.root, "lists.json")
        self.folder_store = os.path.join(self.tree.root, "library_folders.json")
        self.set_config(LISTS_FILE=self.store,
                        LIBRARY_FOLDERS_FILE=self.folder_store,
                        FILE_DIRECTORY=self.tree.music)

    def write(self, entries, path=None):
        with io.open(path or self.store, "w", encoding="utf-8") as handle:
            json.dump(entries, handle)

    def one(self, **over):
        entry = {"name": "Main", "primary": True, "channels": [],
                 "folders": [{"name": "Music", "path": self.tree.music}]}
        entry.update(over)
        return entry


class WithNoFileNothingChanges(ListCase):
    """The equivalence this whole stage rests on."""

    def test_there_is_exactly_one_list(self):
        every = library.lists()

        self.assertEqual(len(every), 1)
        self.assertTrue(every[0].primary)
        self.assertEqual(every[0].name, library.DEFAULT_LIST_NAME)

    def test_it_holds_exactly_the_folders_folders_already_resolved(self):
        """Not a copy of the resolution - the same one. Two answers to "which
        folders" is how they drift, and the folder file, FILE_DIRECTORY and
        the "neither" case all have to keep behaving identically."""
        self.assertEqual(library.lists()[0].folders, library.folders())

    def test_the_folder_file_still_wins_over_file_directory(self):
        other = os.path.join(self.tree.root, "other")
        os.makedirs(other, exist_ok=True)
        self.write([{"name": "Other", "path": other}], path=self.folder_store)

        self.assertEqual([f.path for f in library.folders()], [other])

    def test_an_install_that_has_chosen_nothing_still_has_a_list(self):
        """[] folders, not [] lists. "Which list is this request for" has to
        stay answerable on a fresh install, where FILE_DIRECTORY is
        deliberately allowed to be blank until the dashboard sets it."""
        self.set_config(FILE_DIRECTORY="")

        every = library.lists()

        self.assertEqual(len(every), 1)
        self.assertEqual(every[0].folders, [])


class WhichListARequestMeans(ListCase):

    def test_a_private_message_means_the_primary(self):
        """A PM carries no channel, which is the whole reason a primary
        exists."""
        self.write([self.one(name="Films", primary=False),
                    self.one(name="Music", primary=True)])

        self.assertEqual(library.primary_list().name, "Music")

    def test_a_channel_bound_to_a_list_means_that_list(self):
        self.write([self.one(name="Music", primary=True, channels=["#music"]),
                    self.one(name="Films", primary=False, channels=["#films"])])

        self.assertEqual(library.list_for_channel("#films").name, "Films")

    def test_a_channel_is_matched_however_it_is_capitalised(self):
        """IRC channel names are case-insensitive, and this one arrives off
        the wire."""
        self.write([self.one(name="Music", channels=["#Music"])])

        self.assertEqual(library.list_for_channel("#MUSIC").name, "Music")

    def test_a_channel_bound_to_nothing_gets_nothing(self):
        """None is a real answer, not a gap to paper over. #26: a channel with
        no list bound gets no advert and no requests answered - quietly
        serving the primary instead would be the opposite of that."""
        self.write([self.one(name="Music", channels=["#music"])])

        self.assertIsNone(library.list_for_channel("#somewhere-else"))

    def test_a_list_can_be_found_by_name(self):
        self.write([self.one(name="Films")])

        self.assertEqual(library.list_by_name("films").name, "Films")
        self.assertIsNone(library.list_by_name("nothing"))


class ExactlyOnePrimary(ListCase):
    """Decided once, on load, rather than at every call site. A PM has no
    channel, so the primary is the only answer to "which list" - and "none of
    them" and "two of them" are both answers somebody would have to handle."""

    def test_a_file_with_no_primary_promotes_the_first(self):
        self.write([self.one(name="A", primary=False),
                    self.one(name="B", primary=False)])

        every = library.lists()

        self.assertEqual([e.primary for e in every], [True, False])
        self.assertEqual(library.primary_list().name, "A")

    def test_a_file_with_several_keeps_the_first(self):
        self.write([self.one(name="A", primary=True),
                    self.one(name="B", primary=True),
                    self.one(name="C", primary=True)])

        self.assertEqual([e.primary for e in library.lists()],
                         [True, False, False])

    def test_the_order_is_the_operators(self):
        self.write([self.one(name="C"), self.one(name="A"), self.one(name="B")])

        self.assertEqual([e.name for e in library.lists()], ["C", "A", "B"])


class WhichFoldersAListIsBuiltFrom(ListCase):

    def test_no_name_means_the_primary(self):
        """What every existing caller does, unchanged."""
        first = os.path.join(self.tree.root, "one")
        second = os.path.join(self.tree.root, "two")
        for path in (first, second):
            os.makedirs(path, exist_ok=True)
        self.write([
            {"name": "Music", "primary": True,
             "folders": [{"name": "M", "path": first}]},
            {"name": "Films", "folders": [{"name": "F", "path": second}]}])

        self.assertEqual([f.path for f in library.folders()], [first])

    def test_a_name_picks_that_list(self):
        first = os.path.join(self.tree.root, "one")
        second = os.path.join(self.tree.root, "two")
        for path in (first, second):
            os.makedirs(path, exist_ok=True)
        self.write([
            {"name": "Music", "primary": True,
             "folders": [{"name": "M", "path": first}]},
            {"name": "Films", "folders": [{"name": "F", "path": second}]}])

        self.assertEqual([f.path for f in library.folders("Films")], [second])

    def test_an_unknown_name_serves_nothing_rather_than_somebody_elses(self):
        """A caller asking for a list that is not configured has a bug.
        Falling back to the primary would hide it behind output that looks
        perfectly plausible."""
        self.write([self.one(name="Music")])

        self.assertEqual(library.folders("Films"), [])


class AFileItCannotRead(ListCase):
    """None rather than [], for the reason load_folders() gives: [] reads as
    "the operator configured no lists" and serves nothing, when the truth is
    "we could not read the file"."""

    def test_broken_json_falls_back_to_the_single_list(self):
        with io.open(self.store, "w", encoding="utf-8") as handle:
            handle.write("{not json at all")

        self.assertIsNone(library.load_lists())
        self.assertEqual(len(library.lists()), 1)

    def test_a_file_that_is_not_a_list_says_so(self):
        self.write({"name": "Music"})

        self.assertIsNone(library.load_lists())

    def test_an_entry_with_no_name_is_skipped(self):
        """A list with no name cannot be bound to a channel, picked in the
        dashboard, or named in a log line."""
        self.write([{"folders": [{"path": self.tree.music}]},
                    self.one(name="Real")])

        self.assertEqual([e.name for e in library.lists()], ["Real"])

    def test_a_file_of_nothing_usable_falls_back(self):
        self.write([{"folders": []}, "not a dict", 5])

        self.assertIsNone(library.load_lists())
        self.assertEqual(library.lists()[0].name, library.DEFAULT_LIST_NAME)

    def test_a_list_with_no_folders_is_still_a_list(self):
        """An operator who has defined a list and not yet pointed it anywhere
        has a list. It serves nothing, which is exactly what they configured."""
        self.write([{"name": "Empty", "folders": []}])

        every = library.lists()

        self.assertEqual([e.name for e in every], ["Empty"])
        self.assertEqual(every[0].folders, [])


class TheFileLocationIsResolvedPerCall(ListCase):

    def test_rehash_moving_it_takes_effect(self):
        """Same reasoning as folders_file(): !rehash reloads config, and a
        path baked in at import would keep pointing at the old location for
        the life of the process."""
        moved = os.path.join(self.tree.root, "moved.json")
        self.write([self.one(name="Moved")], path=moved)
        self.set_config(LISTS_FILE=moved)

        self.assertEqual(library.lists()[0].name, "Moved")


class WhereAListsFilesLive(ListCase):
    """#26 stage 2. A subdirectory rather than a longer filename, and the
    primary keeps the root it already has."""

    def setUp(self):
        super().setUp()
        self.lists_dir = os.path.join(self.tree.root, "lists")
        os.makedirs(self.lists_dir, exist_ok=True)
        self.set_config(LOCAL_LIST_DIR=self.lists_dir)

    def test_with_no_name_it_is_the_directory_every_install_uses(self):
        self.assertEqual(list_mod.list_dir(), self.lists_dir)

    def test_the_primary_list_uses_that_same_directory(self):
        """Nothing moves. A single-list install cannot tell this function
        exists, and no upgrade migrates anything."""
        self.write([self.one(name="Music", primary=True)])

        self.assertEqual(list_mod.list_dir("Music"), self.lists_dir)

    def test_another_list_gets_its_own_subdirectory(self):
        self.write([self.one(name="Music", primary=True),
                    self.one(name="Films", primary=False)])

        self.assertEqual(list_mod.list_dir("Films"),
                         os.path.join(self.lists_dir, "Films"))

    def test_an_unknown_name_does_not_invent_a_directory(self):
        """Callers looking there find nothing, which is the truthful outcome.
        A build never writes to a name it did not get from lists()."""
        self.write([self.one(name="Music")])

        self.assertEqual(list_mod.list_dir("Nope"), self.lists_dir)

    def test_the_side_files_follow_the_list(self):
        self.write([self.one(name="Music", primary=True),
                    self.one(name="Films", primary=False)])

        self.assertTrue(list_mod.size_file_path("Films").startswith(
            os.path.join(self.lists_dir, "Films")))
        self.assertTrue(list_mod.rawbytes_file_path("Films").startswith(
            os.path.join(self.lists_dir, "Films")))
        self.assertEqual(list_mod.size_file_path(),
                         list_mod.size_file_path("Music"))

    def test_the_finders_look_in_that_lists_directory(self):
        """The behaviour, not the string. A list's own index is found under
        its own directory and the primary's is not mistaken for it."""
        self.write([self.one(name="Music", primary=True),
                    self.one(name="Films", primary=False)])
        films = os.path.join(self.lists_dir, "Films")
        os.makedirs(films, exist_ok=True)
        self.set_config(LIST_BASE_NAME="DCCore")
        primary_index = os.path.join(self.lists_dir, "DCCore-2026-09-01.txt")
        films_index = os.path.join(films, "DCCore-2026-09-02.txt")
        for path in (primary_index, films_index):
            with io.open(path, "w", encoding="utf-8") as handle:
                handle.write("!DCCore Song.flac\n")

        self.assertEqual(list_mod.find_latest_list(), primary_index)
        self.assertEqual(list_mod.find_latest_list("Films"), films_index)

    def test_all_list_paths_threads_the_name_through(self):
        self.write([self.one(name="Music", primary=True),
                    self.one(name="Films", primary=False)])
        films = os.path.join(self.lists_dir, "Films")
        os.makedirs(films, exist_ok=True)
        self.set_config(LIST_BASE_NAME="DCCore")
        index = os.path.join(films, "DCCore-2026-09-02.txt")
        with io.open(index, "w", encoding="utf-8") as handle:
            handle.write("!DCCore Song.flac\n")

        self.assertEqual(list_mod.all_list_paths("Films"), [index])
        self.assertEqual(list_mod.all_list_paths(), [])


class ALessThanObviousName(ListCase):
    """A list name is operator-facing and never typed in a channel, so it can
    hold anything - including what a filesystem refuses."""

    def test_a_plain_name_is_its_own_directory(self):
        """The common case stays readable: "Films" is the Films directory."""
        self.assertEqual(list_mod.list_slug("Films"), "Films")

    def test_nothing_that_could_traverse_survives(self):
        for name in ("..", "../../etc", "a/b", "a\\b", "."):
            with self.subTest(name=name):
                slug = list_mod.list_slug(name)
                self.assertNotIn("..", slug)
                self.assertNotIn("/", slug)
                self.assertNotIn(chr(92), slug)

    def test_two_names_that_flatten_the_same_stay_apart(self):
        """Replacing characters is not injective: "A/B" and "A B" both flatten
        to "A_B", and two lists in one directory would overwrite each other's
        index, archive and side files with no error anywhere."""
        self.assertNotEqual(list_mod.list_slug("A/B"), list_mod.list_slug("A B"))
        self.assertNotEqual(list_mod.list_slug("A:B"), list_mod.list_slug("A/B"))

    def test_it_is_the_same_answer_every_time(self):
        """Stable across restarts and across reordering the lists - it depends
        on that one name and nothing else. hash() is randomised per process
        and would rename every directory on each start."""
        first = list_mod.list_slug("My Films / 2024")

        self.assertEqual(first, list_mod.list_slug("My Films / 2024"))
        self.assertNotIn(str(id(self)), first)

    def test_an_empty_name_still_produces_something_usable(self):
        self.assertTrue(list_mod.list_slug(""))
        self.assertTrue(list_mod.list_slug("   "))


class WhichListARequestIsAnsweredFrom(ListCase):
    """#26 stage 3. The rule has three parts and each exists to stop a
    different thing going wrong."""

    def test_one_list_with_no_bindings_answers_everywhere(self):
        """Every install today. Without this rule, adding routing would be an
        upgrade that silences every bot in existence."""
        self.assertEqual(library.list_for_request("#anywhere").name,
                         library.DEFAULT_LIST_NAME)
        self.assertEqual(library.list_for_request("#somewhere-else").name,
                         library.DEFAULT_LIST_NAME)

    def test_a_bound_channel_gets_its_own_list(self):
        self.write([self.one(name="Music", primary=True, channels=["#music"]),
                    self.one(name="Films", primary=False, channels=["#films"])])

        self.assertEqual(library.list_for_request("#films").name, "Films")
        self.assertEqual(library.list_for_request("#music").name, "Music")

    def test_an_unbound_channel_gets_nothing_once_the_primary_names_its_own(self):
        """What makes binding mean something. The operator has said where each
        list belongs, so a channel they did not name is one this bot does not
        serve - #26's own rule."""
        self.write([self.one(name="Music", primary=True, channels=["#music"]),
                    self.one(name="Films", primary=False, channels=["#films"])])

        self.assertIsNone(library.list_for_request("#lobby"))

    def test_a_primary_that_binds_nothing_is_the_catch_all(self):
        """The useful middle: one list for everywhere, plus a list for one
        channel. Without it an operator adding a second list would silence
        every channel they had not thought to name."""
        self.write([self.one(name="Music", primary=True, channels=[]),
                    self.one(name="Films", primary=False, channels=["#films"])])

        self.assertEqual(library.list_for_request("#anything").name, "Music")
        self.assertEqual(library.list_for_request("#films").name, "Films")

    def test_a_private_message_is_always_the_primary(self):
        """A PM carries nothing to route on. This holds even when the primary
        binds channels - a PM is not "a channel with nothing bound", it is not
        a channel at all."""
        self.write([self.one(name="Music", primary=True, channels=["#music"]),
                    self.one(name="Films", primary=False, channels=["#films"])])

        for target in (None, "", "SomeNick"):
            with self.subTest(target=target):
                self.assertEqual(library.list_for_request(target).name, "Music")

    def test_the_name_helper_gives_none_for_an_unbound_channel(self):
        """The list-reading functions take a name, and None has to survive the
        trip - a helper that turned it into the primary's name would undo the
        rule at the point it is used."""
        self.write([self.one(name="Music", primary=True, channels=["#music"])])

        self.assertIsNone(library.list_name_for_request("#lobby"))
        self.assertEqual(library.list_name_for_request("#music"), "Music")


class TheHandlersAskThatQuestion(ListCase):
    """Read out of the source. Driving a real request needs a socket, a peer
    and a DCC engine; what matters here is that each entry point routes at
    all, and that a channel bound to nothing is answered with silence."""

    def source(self, name):
        with io.open(os.path.join(REPO_ROOT, name), encoding="utf-8") as handle:
            return handle.read()

    def test_every_folder_lookup_in_the_request_path_is_scoped(self):
        """COUNTED, not merely present. There are two library.folders() calls
        in that function - the "is there anywhere to look" guard and the roots
        a bare filename is searched in - and asserting one was there passed a
        mutation run while the other still read the primary's."""
        code = self.source("dcc.py")
        body = code.split("def handle_download_request(", 1)[1].split(chr(10) + "def ", 1)[0]

        self.assertGreater(body.count("library.folders("), 0)
        self.assertEqual(body.count("library.folders("),
                         body.count("library.folders(wanted_list)"),
                         "a library.folders() call in the request path is not "
                         "scoped to this request's list")

    def test_the_artifact_comes_from_the_requests_own_list_directory(self):
        """Every list writes its archive under the SAME name, in its own
        directory - so looking in the root answers "file not found" for every
        list but one, which is the whole of what send_file_list() just
        offered them. Found by a test that drove the real path."""
        code = self.source("dcc.py")
        body = code.split("def handle_download_request(", 1)[1].split(chr(10) + "def ", 1)[0]

        self.assertIn("list_mod.list_dir(wanted_list)", body)

    def test_the_search_routes(self):
        code = self.source("list.py")
        body = code.split("def execute_search(", 1)[1].split("\ndef ", 1)[0]

        self.assertIn("list_name_for_request(channel)", body)
        self.assertIn("find_latest_list(wanted)", body)

    def test_the_file_request_routes(self):
        code = self.source("dcc.py")
        body = code.split("def handle_download_request(", 1)[1].split("\ndef ", 1)[0]

        self.assertIn("list_name_for_request(target_chan)", body)
        self.assertIn("library.folders(wanted_list)", body)
        self.assertIn("all_list_paths(wanted_list)", body)

    def test_the_file_request_resolves_the_list_before_it_uses_it(self):
        """Two things need the same answer - the folders a bare filename is
        looked for in, and the list the name is resolved against - and they
        must not disagree. One resolution, above both uses."""
        code = self.source("dcc.py")
        body = code.split("def handle_download_request(", 1)[1].split("\ndef ", 1)[0]

        self.assertLess(body.index("wanted_list = library.list_name_for_request"),
                        body.index("library.folders(wanted_list)"))
        self.assertLess(body.index("wanted_list = library.list_name_for_request"),
                        body.index("all_list_paths(wanted_list)"))

    def test_a_heading_is_resolved_against_its_own_lists_folders(self):
        """A label names a folder OF a list. Resolving one list's heading
        against another's folders would send a request into the wrong
        library."""
        code = self.source("dcc.py")

        self.assertIn("resolve_list_folder_with_root(\n                win_path, wanted_list)",
                      code)


class SendingTheListArchive(ListCase):
    """send_file_list() driven for real. The two source-reading checks it used
    to have both survived a mutation that broke it, which is what a weak
    assertion looks like."""

    def setUp(self):
        super().setUp()
        self.lists_dir = os.path.join(self.tree.root, "lists")
        os.makedirs(self.lists_dir, exist_ok=True)
        # send_file_list() hands off to dcc.handle_download_request(), which
        # refuses a target_chan this bot is not configured to be in - so the
        # fixture has to be in the channels it is testing.
        self.set_config(LOCAL_LIST_DIR=self.lists_dir, LIST_BASE_NAME="DCCore",
                        NICKNAME="DCCore", update_inprogress=False,
                        CHANNEL="#music,#films,#lobby,#anywhere")
        # list.py bound `oserve` at IMPORT time, so the fake the harness puts
        # in sys.modules is not the object it calls. Point the module's own
        # reference at the fake instead - patching sys.modules alone leaves
        # every notice going to the real one, which is how this test spent
        # three runs asserting on an empty list.
        import dcc
        self.addCleanup(setattr, list_mod, "oserve", list_mod.oserve)
        list_mod.oserve = self.oserve
        real_send = dcc.start_dcc_send
        dcc.start_dcc_send = lambda *a, **k: None
        self.addCleanup(setattr, dcc, "start_dcc_send", real_send)

    @property
    def sent(self):
        return [(user, message) for user, message, _vip in self.oserve.queued]

    def artifact(self, directory, date="2026-09-06"):
        os.makedirs(directory, exist_ok=True)
        path = os.path.join(directory, f"DCCore-{date}.zip")
        with io.open(path, "w", encoding="utf-8") as handle:
            handle.write("archive")
        return path

    def test_an_unbound_channel_is_answered_with_silence(self):
        """Not an error: an error implies something went wrong, and nothing
        did - this bot does not serve there."""
        self.write([self.one(name="Music", primary=True, channels=["#music"])])
        self.artifact(self.lists_dir)

        list_mod.send_file_list(None, "someone", "#lobby")

        self.assertEqual(self.sent, [])

    def test_a_bound_channel_is_sent_its_own_lists_archive(self):
        self.write([self.one(name="Music", primary=True, channels=["#music"]),
                    self.one(name="Films", primary=False, channels=["#films"])])
        self.artifact(self.lists_dir)
        films = self.artifact(os.path.join(self.lists_dir, "Films"), "2026-09-05")

        list_mod.send_file_list(None, "someone", "#films")

        notices = [msg for who, msg in self.sent if who == "someone"]
        self.assertTrue(notices, "nothing was sent at all")
        self.assertIn(os.path.basename(films), notices[0])

    def test_a_single_list_install_still_gets_its_archive(self):
        """The case every operator is in."""
        self.artifact(self.lists_dir)

        list_mod.send_file_list(None, "someone", "#anywhere")

        self.assertTrue([m for who, m in self.sent if who == "someone"])


class TheAdvertFollowsTheChannel(ListCase):
    """#26 stage 4. The advert loop already read the figures once per channel;
    it just read the same ones every time."""

    def source(self):
        with io.open(os.path.join(REPO_ROOT, "announce.py"), encoding="utf-8") as fh:
            return fh.read()

    def test_the_figures_are_read_for_that_channels_list(self):
        """A count and a size from another channel's library is a claim
        nobody there can act on."""
        body = self.source()

        self.assertIn("list_name_for_request(chan)", body)
        self.assertIn("get_file_count_date_size_and_raw_bytes(wanted)", body)

    def test_a_channel_with_no_list_is_not_advertised_in(self):
        """The advert is where #26's rule is most visible: a bot silently
        present in a channel it does not serve, rather than one announcing a
        library it will refuse to send from."""
        body = self.source()
        after = body.split("list_name_for_request(chan)", 1)[1][:300]

        self.assertIn("if wanted is None:", after)
        self.assertIn("continue", after)

    def test_the_lookup_precedes_the_figures(self):
        """Reading the figures first and discarding them would still be
        correct, and would also mean every channel paid for a list read it
        was never going to use."""
        body = self.source()

        self.assertLess(body.index("list_name_for_request(chan)"),
                        body.index("get_file_count_date_size_and_raw_bytes(wanted)"))

    def test_the_counter_reads_the_list_it_is_asked_for(self):
        """Behavioural, under the source checks above: the advert is only as
        right as this is."""
        lists_dir = os.path.join(self.tree.root, "lists")
        films_dir = os.path.join(lists_dir, "Films")
        os.makedirs(films_dir, exist_ok=True)
        self.set_config(LOCAL_LIST_DIR=lists_dir, LIST_BASE_NAME="DCCore",
                        NICKNAME="DCCore")
        self.write([self.one(name="Music", primary=True, channels=["#music"]),
                    self.one(name="Films", primary=False, channels=["#films"])])
        with io.open(os.path.join(lists_dir, "DCCore-2026-09-06.txt"),
                     "w", encoding="utf-8") as handle:
            handle.write("header" + chr(10) + "!DCCore One.flac" + chr(10))
        with io.open(os.path.join(films_dir, "DCCore-2026-09-06.txt"),
                     "w", encoding="utf-8") as handle:
            handle.write("header" + chr(10)
                         + "!DCCore A.mkv" + chr(10)
                         + "!DCCore B.mkv" + chr(10))

        self.assertEqual(
            list_mod.get_file_count_date_size_and_raw_bytes("Music")[0], 1)
        self.assertEqual(
            list_mod.get_file_count_date_size_and_raw_bytes("Films")[0], 2)

    def test_a_list_with_no_index_says_so_even_when_another_has_one(self):
        """The existence check and the date come from THIS list's index. With
        them reading the primary's, a list that had never been built would
        report the primary's date and be advertised as though it had - which
        is the sentinel the advert skips on, so the channel would advertise a
        library that does not exist."""
        lists_dir = os.path.join(self.tree.root, "lists")
        os.makedirs(lists_dir, exist_ok=True)
        self.set_config(LOCAL_LIST_DIR=lists_dir, LIST_BASE_NAME="DCCore",
                        NICKNAME="DCCore")
        self.write([self.one(name="Music", primary=True, channels=["#music"]),
                    self.one(name="Films", primary=False, channels=["#films"])])
        with io.open(os.path.join(lists_dir, "DCCore-2026-09-06.txt"),
                     "w", encoding="utf-8") as handle:
            handle.write("header" + chr(10) + "!DCCore One.flac" + chr(10))

        count, date, _size, _raw = list_mod.get_file_count_date_size_and_raw_bytes("Films")

        self.assertEqual(date, "No List")
        self.assertEqual(count, 0)


class DefiningListsFromTheDashboard(ListCase):
    """#26 stage 5. The rules an operator meets the first time they define a
    second list, and the endpoint pair that writes them."""

    def entry(self, name, primary=False, channels=(), folders=None):
        return {"name": name, "primary": primary, "channels": list(channels),
                "folders": folders if folders is not None
                else [{"name": "M", "path": self.tree.music}]}

    def save(self, rows):
        return webserver.apply_list_changes({"lists": rows})

    def test_a_valid_set_is_written_and_read_back(self):
        status, result = self.save([
            self.entry("Music", primary=True, channels=["#music"]),
            self.entry("Films", channels=["#films"])])

        self.assertEqual(status, 200, result)
        self.assertEqual([e.name for e in library.lists()], ["Music", "Films"])
        self.assertEqual(library.list_for_request("#films").name, "Films")

    def test_the_same_channel_twice_is_refused_by_name(self):
        """"Invalid list configuration" tells an operator with four lists
        nothing about which two to look at."""
        status, result = self.save([
            self.entry("Music", primary=True, channels=["#shared"]),
            self.entry("Films", channels=["#SHARED"])])

        self.assertEqual(status, 400)
        self.assertIn("Music", result["error"])
        self.assertIn("Films", result["error"])

    def test_two_lists_with_the_same_name_are_refused(self):
        """Case-insensitively: list_by_name() matches that way, and on Windows
        two directories differing only in case are one directory."""
        status, result = self.save([self.entry("Music", primary=True),
                                    self.entry("music")])

        self.assertEqual(status, 400)
        self.assertIn("already a list called", result["error"])

    def test_a_nameless_list_is_refused(self):
        status, result = self.save([self.entry("", primary=True)])

        self.assertEqual(status, 400)
        self.assertIn("no name given", result["error"])

    def test_something_that_is_not_a_channel_is_refused(self):
        status, result = self.save([
            self.entry("Music", primary=True, channels=["nohash"])])

        self.assertEqual(status, 400)
        self.assertIn("must start with # or &", result["error"])

    def test_no_primary_is_refused_on_save(self):
        """Not fatal on LOAD - load_lists() promotes the first - but on a save
        the operator is looking at the screen and can be told."""
        status, result = self.save([self.entry("Music"), self.entry("Films")])

        self.assertEqual(status, 400)
        self.assertIn("primary", result["error"])

    def test_every_fault_is_reported_at_once(self):
        """An operator fixing three things should be told about three
        things."""
        status, result = self.save([
            self.entry("", channels=["nope"]),
            self.entry("Films", channels=["#f"]),
            self.entry("films", channels=["#f"])])

        self.assertEqual(status, 400)
        self.assertGreaterEqual(len(result["error"].split(chr(10))), 2)

    def test_a_bad_folder_inside_a_list_is_reported_against_that_list(self):
        """The folder rules are not repeated in list_problems() - problems()
        already owns them - so a bad label has to come back named."""
        status, result = self.save([
            self.entry("Music", primary=True,
                       folders=[{"name": "a/b", "path": self.tree.music}])])

        self.assertEqual(status, 400)
        self.assertIn("Music", result["error"])

    def test_an_empty_set_goes_back_to_one_list(self):
        """The file is REMOVED rather than written as [], because load_lists()
        already falls back on an empty file - so [] would leave something on
        disk that does nothing."""
        self.save([self.entry("Music", primary=True, channels=["#music"])])
        self.assertTrue(os.path.exists(self.store))

        status, _result = self.save([])

        self.assertEqual(status, 200)
        self.assertFalse(os.path.exists(self.store))
        self.assertEqual(len(library.lists()), 1)

    def test_the_payload_says_whether_a_list_was_configured_or_implied(self):
        """With no file there is still exactly one list in the payload - the
        implicit one - and an operator needs to know editing it is what makes
        it real."""
        self.assertEqual(webserver.build_lists_payload()["source"], "implied")

        self.save([self.entry("Music", primary=True)])

        self.assertEqual(webserver.build_lists_payload()["source"], "file")

    def test_more_lists_than_the_cap_are_refused(self):
        status, _result = self.save(
            [self.entry("L%d" % i, primary=(i == 0)) for i in range(40)])

        self.assertEqual(status, 400)

    def test_a_body_that_is_not_the_right_shape_is_refused(self):
        for body in ("nope", {"lists": "nope"}, {"lists": ["nope"]},
                     {"lists": [{"name": "A", "channels": "nope"}]},
                     {"lists": [{"name": "A", "folders": "nope"}]}):
            with self.subTest(body=body):
                status, _result = webserver.apply_list_changes(body)
                self.assertEqual(status, 400)


class FileDirectoryIsOnlyTheFallback(ListCase):
    """From an operator: "under Paths & Storage, this is not needed anymore".

    Not quite - it is still what an install with no folder list serves from,
    which is most of them. But three places treated it as the ONLY truth, and
    two of those were bugs an operator with folders configured would meet.
    """

    def test_startup_asks_the_library_not_the_setting(self):
        """A stale FILE_DIRECTORY pointing at a drive that is no longer there
        made the daemon REFUSE TO BOOT, while the folders it actually serves
        from sat there perfectly readable. A setting nothing reads any more
        was able to stop the bot starting."""
        with io.open(os.path.join(REPO_ROOT, "oserve.py"), encoding="utf-8") as handle:
            code = chr(10).join(line.split("#", 1)[0]
                                for line in handle.read().splitlines())
        body = code.split("def startup(", 1)[1].split(chr(10) + "def ", 1)[0]

        self.assertIn("library.folders()", body)
        self.assertNotIn("if not config.FILE_DIRECTORY:", body)
        self.assertNotIn("os.path.exists(config.FILE_DIRECTORY)", body)

    def test_startup_refuses_only_when_nothing_at_all_is_readable(self):
        """EVERY one missing, not any one. A single unavailable folder is a
        scan-time condition the build already skips with a warning; only a
        library with nothing readable at all is a real misconfiguration."""
        with io.open(os.path.join(REPO_ROOT, "oserve.py"), encoding="utf-8") as handle:
            code = handle.read()
        body = code.split("def startup(", 1)[1].split(chr(10) + "def ", 1)[0]

        self.assertIn("not any(os.path.exists(folder.path)", body)

    def test_the_build_no_longer_reads_it_at_all(self):
        """A single-folder leftover: `scan_root` built from FILE_DIRECTORY and
        then immediately overwritten inside the per-folder loop. Dead since
        #164 - and it is exactly the kind of leftover that makes a superseded
        setting look load-bearing."""
        with io.open(os.path.join(REPO_ROOT, "update_list.py"), encoding="utf-8") as handle:
            code = chr(10).join(line.split("#", 1)[0]
                                for line in handle.read().splitlines())

        self.assertNotIn("config.FILE_DIRECTORY", code)

    def test_the_settings_label_says_it_is_a_fallback(self):
        """Presenting it as a plain "Music directory" beside a folder editor
        that overrides it is what made it read as the setting that matters."""
        import webserver

        label = webserver.SETTINGS_LABELS["FILE_DIRECTORY"]

        self.assertIn("only when no folders", label)

    def test_it_is_still_what_an_install_with_no_folder_list_serves(self):
        """The half of that point which is not true: removing it would take
        the fallback with it, and that is what every install without a folder
        file is using."""
        self.set_config(FILE_DIRECTORY=self.tree.music)

        self.assertEqual([f.path for f in library.folders()], [self.tree.music])


if __name__ == "__main__":
    unittest.main()
