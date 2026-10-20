"""Nothing that ships names a real person, bot, channel or address.

`.gitattributes` export-ignores only development-only documents and the
project page's source - see its export-ignore rules. EVERYTHING else in the
repository reaches the public tree, `tests/` very much included, and this
project has shipped
identifiers six times already: a serving bot's nick in two test files, a
real channel in a changelog, an operator's own paths in fixtures, the
fourth time three of an operator's real channel names plus a real bot's
nick in a fix's own comments, docstrings and assertions, the fifth time
five more real bot nicks that had each ridden in on an unrelated bug
report - a marker-format fix, a rename fix, an admin's own account name in
a speed-reporting test, and two list-browser fixtures - and the sixth,
found independently and at the same time as the fifth, four more real bot
nicks and requesters pasted from a live console into a comment or a
fixture. Every one of the six arrived the same way: a realistic-sounding
example is, every time, somebody's actual nick.

Greps done by hand keep missing them, because they are written to find the
thing already known about. This asks the export itself - and, since the
development repository is public as well, every other tracked file too
(NothingIdentifyingIsTracked).

WHY THE CO-MAINTAINER'S HANDLE COUNTS

It was used in good faith - dozens of comments attributed an observation to
the person who made it, which is ordinary practice. But this is a public
repository read by strangers, the handle is the same one on the issue tracker
and on IRC, and none of the comments need it: "an operator reported" carries
the same weight and dates better. The attributions were rewritten rather than
deleted, so the reasoning survives without the name.
"""

import hashlib
import io
import os
import re
import unicodedata
import subprocess
import sys
import tempfile
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# HASHED, not written out. This file SHIPS - it is in tests/, like everything
# else here - so a denylist spelling the names would publish the very strings
# it exists to remove. The first version did exactly that, and had to skip
# itself to pass: the one file guaranteed to contain every forbidden string
# was the one file never checked.
#
# docs/PUBLIC-REPO-WORKFLOW.md already records the principle, learnt from the
# licence check: "Assert what should be true, not a list of what shouldn't."
# No positive property distinguishes a person's handle from any other word,
# so the next best thing is a denylist nobody can read.
#
# AND HASHING WAS NOT ENOUGH. THE LIST IS NOT IN THE REPOSITORY AT ALL
# (tests/forbidden_identifiers.py, untracked and named in .gitignore), and
# neither is the recipe for adding to it.
#
# It used to be right here: the hashes, the recipe for making one on the line
# above them, and a label beside every entry saying what it was. That is a
# puzzle whose answer is the maintainers' identity.
#
# NOT TRACKED, rather than tracked and export-ignored. export-ignore keeps a
# file out of the release tree and does nothing else: the development
# repository is public too, and every file tracked there can be read by
# anyone. So each maintainer keeps a local copy, and CI writes one from the
# FORBIDDEN_IDENTIFIERS secret before the suite runs
# (.github/workflows/tests.yml).
#
# ABSENT IS A VALID STATE, not an error: that is what the published tree, a
# fresh clone and a CI run without the secret (a pull request from a fork)
# all look like. The denylist test skips there and says the sweep did not
# run; every structural check below still runs, because those are the half
# that generalises to a contributor nobody has met.
#
# DENYLIST is that file's path, for messages: scripts/preflight.py asks this
# module for it rather than keeping a copy of its own.
DENYLIST = "tests/forbidden_identifiers.py"
try:
    from tests.forbidden_identifiers import FORBIDDEN
except ImportError:  # pragma: no cover - the published tree takes this path
    try:
        from forbidden_identifiers import FORBIDDEN
    except ImportError:
        FORBIDDEN = None

# Compared lowercased, so one hash covers every capitalisation.
WORD = re.compile(r"[A-Za-z][A-Za-z0-9_-]*")

# ESCAPE SEQUENCES ARE BLANKED BEFORE ANY WORD IS READ, and this is the whole
# reason three scrub passes missed three real nicks sitting in plain sight.
#
# The scan runs over SOURCE TEXT, not over the values that source evaluates
# to. In a fixture holding a captured IRC line the colour codes are still
# escapes, so the source reads:
#
#     'Record: 4788.6 by \x0311SomeNick\x032]'
#
# WORD starts at the "x" - a letter like any other - and takes the whole of
# "x0311SomeNick" as ONE word. That hashes to something nobody has ever put
# in FORBIDDEN, so a forbidden name in that position is INVISIBLE here no
# matter how many hashes are added above it.
#
# Verified by counterfactual rather than argument: four names ALREADY in
# FORBIDDEN also go undetected when placed immediately after a colour escape,
# while the same name written as "@Name" is caught. That is exactly the
# pattern of what the earlier passes found and what they walked past.
#
# Each escape becomes the SAME NUMBER OF SPACES rather than an empty string,
# so every byte offset after it is unchanged and the reported line numbers
# stay true.
ESCAPE = re.compile(
    r"\\(?:x[0-9A-Fa-f]{2}|u[0-9A-Fa-f]{4}|U[0-9A-Fa-f]{8}"
    r"|N\{[^}]*\}|[0-7]{1,3}|[abfnrtv])")


# HOW AN ESCAPE DECODES, for the second pass words() makes. Only the forms
# that can appear INSIDE a name: a high byte written as \xNN, or a code point
# written as \uNNNN. A name is not spelled with \n or \t.
DECODABLE = re.compile(r"\\(?:x[0-9A-Fa-f]{2}|u[0-9A-Fa-f]{4})")


def decoded(text):
    r"""`text` with \xNN and \uNNNN turned into the characters they mean.

    THE OTHER HALF OF THE ESCAPE PROBLEM (found by the pre-publication sweep).
    Blanking an escape to spaces is right when the escape sits BEFORE a name -
    "\\x0311Nick" then yields "Nick" as its own word, which is what the
    colour-code case needed. It is exactly wrong when the escape sits INSIDE
    one: a bot tag written with two of them became three fragments, none of
    which is a name, and no number of hashes could ever have matched it.

    So words() reads both: the blanked text for offsets, and this for names
    that are only whole once decoded. An undecodable escape is left alone
    rather than guessed at.
    """
    def one(match):
        try:
            return match.group(0).encode("ascii").decode("unicode_escape")
        except (UnicodeDecodeError, UnicodeEncodeError):
            return match.group(0)
    return DECODABLE.sub(one, str(text))


# The decoded pass needs letters WORD does not have. A name written with
# high-byte escapes decodes to a word with accents in the middle of it, and an
# ASCII-only character class splits it right back into the fragments the
# decoding existed to join.
WORD_ANY_LETTER = re.compile(r"[^\W\d_][\w-]*", re.UNICODE)


def folded(word):
    """`word` with its accents removed, or unchanged if it has none.

    So a name can be denied once rather than once per spelling. An operator
    typing the ASCII form of an accented tag means the same identifier, and a
    denylist that had to carry both would be a denylist nobody keeps current.
    """
    stripped = "".join(part for part in unicodedata.normalize("NFKD", word)
                       if not unicodedata.combining(part))
    return stripped or word


def words(text):
    """(word, offset) for every word in `text`, read TWO ways.

    The offset is into the ORIGINAL text, which is what lets the caller keep
    reporting a real line number.

    FIRST, escapes blanked to spaces of the same length. That is what makes
    "\\x0311Nick" yield "Nick" as a word of its own, and keeps every byte
    offset after it true.

    SECOND, escapes DECODED (see decoded()). Blanking is exactly wrong when
    the escape sits inside a name rather than before it: a bot tag written
    with two high-byte escapes became three fragments, and no hash could
    match it. The decoded pass reads the same span again as one word, with
    its accents folded so the ASCII spelling of the same name matches too.
    Offsets from this pass are approximate by nature - decoding changes the
    length - so it reports the START of the region rather than pretending.
    """
    flattened = ESCAPE.sub(lambda m: " " * len(m.group(0)), text)
    found = [(m.group(0), m.start()) for m in WORD.finditer(flattened)]

    text = str(text)
    if DECODABLE.search(text):
        for match in DECODABLE.finditer(text):
            # The run this escape belongs to: back to the last whitespace,
            # forward to the next one. Decoding just that keeps the rest of
            # the line's offsets out of it.
            start = max(text.rfind(" ", 0, match.start()),
                        text.rfind("\n", 0, match.start())) + 1
            end = min(x for x in (text.find(" ", match.end()),
                                  text.find("\n", match.end()), len(text))
                      if x != -1)
            for word_match in WORD_ANY_LETTER.finditer(decoded(text[start:end])):
                word = word_match.group(0)
                found.append((word, start))
                if folded(word) != word:
                    found.append((folded(word), start))
    return found


def word_hash(word):
    return hashlib.sha256(word.lower().encode("utf-8")).hexdigest()[:16]

# Documentation and private ranges are the ONLY literal addresses that may
# ship. Everything else is somebody's real machine.
ALLOWED_IP = re.compile(
    r"^(127\.|0\.0\.0\.0|10\.|192\.168\.|169\.254\.|172\.(1[6-9]|2\d|3[01])\.|"
    r"192\.0\.2\.|198\.51\.100\.|203\.0\.113\.|1\.2\.3\.4|8\.8\.8\.8|1\.1\.1\.1|"
    # 100.64.0.0/10: carrier-grade NAT's shared space (RFC 6598), which is
    # never routed on the internet - a range like 10/8, not a machine.
    r"100\.(6[4-9]|[7-9]\d|1[01]\d|12[0-7])\.|"
    # 224-255: multicast, reserved and broadcast. Not anybody's machine, and
    # the address tests use them deliberately to check they are refused.
    r"2(2[4-9]|[3-4]\d|5[0-5])\.)")
IP = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")

# A CHANNEL NAME IS NOT A WORD (found by the pre-publication sweep). RFC 2812
# allows everything except NUL, BELL, CR, LF, space, comma and colon - so
# a channel whose name contains "&" or "^" is a single legal name that WORD
# splits into pieces, and a hash of the whole name could never match any of
# them. Two real channels were invisible to the guard for exactly this reason
# - and are deliberately not written out here, since this file ships.
#
# Matched separately rather than by widening WORD: widening it would merge
# ordinary adjacent punctuation into every token in the tree and turn the
# denylist into noise. A channel is recognisable by its leading marker.
CHANNEL = re.compile(r"[#&+!][^\s,:\x00\x07\r\n]{1,49}")


def channel_tokens(text):
    """(name, offset) for every channel-shaped token, marker stripped.

    The marker goes because the denylist holds bare names - an operator writes
    "#thing" in one place and "thing" in another, and both are the same
    identifier. Trailing punctuation that is plainly prose ("#thing," "#thing.")
    is trimmed for the same reason.
    """
    found = []
    for match in CHANNEL.finditer(str(text)):
        name = match.group(0)[1:].rstrip(".,;)]}'\"")
        if name:
            found.append((name, match.start() + 1))
    return found

# AN ADDRESS DOES NOT HAVE TO BE WRITTEN AS ONE (found by the pre-publication
# sweep). Consumer reverse-DNS encodes the octets with dashes -
# "cpe-198-51-100-7.isp.net" is that same address in dotted form - and IP
# above only matches dotted
# quads, so that form was invisible. The address it encodes is checked against
# exactly the same allowlist, so a documentation range written this way is
# still fine.
DASHED_IP = re.compile(r"\b(\d{1,3})-(\d{1,3})-(\d{1,3})-(\d{1,3})\b")


def dashed_addresses(text):
    """(dotted, offset) for every address written with dashes."""
    found = []
    for match in DASHED_IP.finditer(str(text)):
        octets = match.groups()
        if all(int(part) < 256 for part in octets):
            found.append((".".join(octets), match.start()))
    return found


# EVERY FILE IS READ UNLESS IT IS KNOWN NOT TO BE TEXT (#1443).
#
# This used to be the other way round: a list of TEXT suffixes, and a file
# whose suffix was missing from it was a file nobody scanned. .mrc and
# .command each shipped unscanned that way until somebody noticed, and the
# mIRC DLL's C sources, the dotfiles and LICENSE were still unscanned when an
# audit found the list. A list of what to read fails OPEN on every new kind
# of file; a list of what to skip fails CLOSED, because a binary nobody listed
# does not decode and read_text_files() reports it.
#
# Skipped by suffix without being opened, compared lowercased. Shared with
# tests/test_no_conflict_marker_is_left_behind.py, which imports it. Every
# `*.<suffix> binary` line in .gitattributes must have its suffix here too;
# tests/test_every_shipped_text_file_is_swept.py checks that.
BINARY_SUFFIXES = (".dll", ".exe", ".pyd", ".so", ".dylib", ".pyc",
                   ".png", ".ico", ".gif", ".jpg", ".jpeg", ".bmp", ".webp",
                   ".ttf", ".otf", ".woff", ".woff2",
                   ".zip", ".rar", ".7z", ".gz", ".tar", ".pdf",
                   ".flac", ".mp3", ".ogg", ".wav", ".m4a")

# Files with no binary suffix that are allowed not to decode. EMPTY, and an
# entry needs a reason: a file named here is never read, so a text file in an
# encoding nobody expected must be re-saved as UTF-8, not listed.
UNDECODABLE_BY_DESIGN = frozenset()


def is_binary_name(name):
    """True if `name` is skipped by suffix, unread."""
    return name.lower().endswith(BINARY_SUFFIXES)


def decode_text(data):
    """`data` as text, or None if it is not text this sweep can read.

    UTF-8, or UTF-16 when a byte-order mark says so - which is how Visual
    Studio saves a resource script (.rc), the likeliest next file type in the
    DLL's sources. Anything else is None, never a lossy errors="replace"
    read: a file the sweep only half-read must not count as scanned.

    A NUL in the result is None too. UTF-16 WITHOUT a byte-order mark is
    valid UTF-8 whenever its text is ASCII - every other byte a NUL - and
    WORD reads each letter between the NULs as a word of its own, so a name
    in it would pass unseen. Most binaries carry NULs as well, so one that
    is missing from BINARY_SUFFIXES is reported rather than read as text.
    No tracked text file contains one.
    """
    text = _decode(data)
    return None if text is None or "\0" in text else text


def _decode(data):
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        try:
            return data.decode("utf-16")
        except UnicodeDecodeError:
            return None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None


def read_text_files(names, root):
    """({name: text}, unread) for the paths in `names`, relative to `root`.

    THE ONE DEFINITION of which tracked file is scanned as text, for both
    sweeps. A file is skipped unread if is_binary_name() says so, and read
    otherwise. `unread` lists the files that were read but did not decode
    and are not in UNDECODABLE_BY_DESIGN: each one is a file the sweep did
    not look at, so the callers fail on it rather than pass it by silently.

    `root` has no default on purpose: it must be the directory the caller
    asked git for `names` in, and a default bound here would be a second
    copy of that fact (CONVENTIONS rule 2).

    A SYMLINK IS READ AS ITS LINK TEXT. That is what git stores and what
    `git archive` ships, so it is what can carry a name; opening the path
    would follow the link instead - to some other file's content, to a
    directory (raises), or to nothing at all (a dangling link, which would
    look like a deleted file and be skipped). A submodule (mode 160000) is a
    directory and raises on open, loudly; none is tracked.
    """
    texts, unread = {}, []
    for name in names:
        path = os.path.join(root, name)
        if os.path.islink(path):
            texts[name] = os.readlink(path)
            continue
        if is_binary_name(name):
            continue
        try:
            with io.open(path, "rb") as handle:
                text = decode_text(handle.read())
        except FileNotFoundError:
            if os.path.lexists(path):
                # Something is there and could not be opened: not scanned.
                raise
            if "\ufffd" in name:
                # Not a deleted file: a name git listed in bytes that are
                # not UTF-8, decoded with "replace" into a path that does
                # not exist. Whatever is under the real name was not read.
                unread.append(name)
            # Otherwise tracked but deleted from the working tree and not
            # yet committed: nothing there to ship or to scan.
            continue
        if text is not None:
            texts[name] = text
        elif name not in UNDECODABLE_BY_DESIGN:
            unread.append(name)
    return texts, unread


def tracked_paths():
    """(every tracked file, the set of those export-ignored, None), or
    (None, None, reason) when git cannot answer.

    THE WORKING TREE, not `git archive HEAD`. The archive reads the last
    COMMIT, so a guard built on it cannot see the change being made - it
    would have passed the scrub that introduced it and failed on everything
    before, which is precisely backwards for something meant to stop an
    identifier being committed.

    export-ignore is still the authority on what ships: `git check-attr`
    answers the same question `git archive` does, against the files as they
    are now.
    """
    try:
        listed = subprocess.run(["git", "ls-files", "-z"], cwd=REPO_ROOT,
                                check=True, capture_output=True, timeout=180)
        names = [n for n in listed.stdout.decode("utf-8", "replace").split("\0") if n]
        attrs = subprocess.run(["git", "check-attr", "--stdin", "-z", "export-ignore"],
                               cwd=REPO_ROOT, check=True, timeout=180,
                               input="\0".join(names).encode("utf-8"),
                               capture_output=True)
        fields = attrs.stdout.decode("utf-8", "replace").split("\0")
        ignored = set()
        # check-attr -z emits path, attribute, value as three NUL-separated
        # fields per line.
        for index in range(0, len(fields) - 2, 3):
            if fields[index + 2] == "set":
                ignored.add(fields[index])
    except Exception as err:  # noqa: BLE001 - git may not be available
        return None, None, f"could not list the tracked files ({err})"
    return names, ignored, None


def shipped_paths():
    """Every tracked file that would ship, or (None, reason). See
    tracked_paths()."""
    names, ignored, reason = tracked_paths()
    if reason:
        return None, reason
    return [name for name in names if name not in ignored], None


def forbidden_hits(texts, forbidden):
    """{label: ["path:line", ...]} for every token in `texts` ({path: text})
    whose hash is a key of `forbidden` ({hash: label})."""
    found = {}
    for path, text in texts.items():
        # WORDS AND CHANNELS ARE DIFFERENT TOKENS. A channel may legally
        # contain "&", "^" and more, which WORD stops at - so a real
        # channel was split into pieces no hash could ever match. Asked
        # separately rather than by widening WORD; see channel_tokens().
        for word, offset in list(words(text)) + channel_tokens(text):
            why = forbidden.get(word_hash(word))
            if why is None:
                continue
            line = text.count("\n", 0, offset) + 1
            found.setdefault(why, []).append(f"{path}:{line}")
    return found


# THE LABEL STAYS OFF A PUBLIC LOG. Each denylist entry carries a label
# saying what kind of identifier it is, and locally that label is what makes
# a hit quick to act on. The development repository's CI logs can be read
# by anyone, so on GitHub Actions a hit is reported by place alone -
# file:line and "a listed identifier" - and not grouped by entry either,
# since even the number of distinct entries hit says more than a public log
# needs to.
def hits_report(found, environ=None,
                heading="these ship to the public repository:"):
    """The failure message for forbidden_hits()'s `found`, fit for where the
    run's output goes: without labels when GITHUB_ACTIONS is "true"."""
    environ = os.environ if environ is None else environ
    if environ.get("GITHUB_ACTIONS") == "true":
        places = sorted({place for where in found.values() for place in where})
        return (heading + "\n  "
                + "\n  ".join(f"{place}  a listed identifier" for place in places))
    return (heading + "\n  "
            + "\n  ".join(f"{why}\n    {sorted(set(where))[:6]}"
                           for why, where in found.items()))


# THE DEVELOPMENT REPOSITORY'S OWN NAMES, allowed where the release does not
# reach. A few denylist entries are names that development-only documents
# carry by right: the development repository's name and the maintainers'
# public account names. They are listed so that nothing in the release names
# them; in an export-ignored file - the internal changelog, the workflow and
# project-board notes - they are what the text is about. Held as word_hash()
# values, so this file spells none of them. Every other entry is reported
# wherever it is, and in a file that ships these are reported too.
ALLOWED_OUTSIDE_THE_RELEASE = frozenset({
    "13ea59307fc3f4ec",
    "f0756a8e416936e7",
    "73ef176d9f12809e",
})


def tracked_hits(texts, ignored, forbidden):
    """forbidden_hits() over every tracked file in `texts`, with the entries
    in ALLOWED_OUTSIDE_THE_RELEASE left out for the paths in `ignored` (the
    export-ignored ones) and kept for every other path."""
    found = forbidden_hits({path: text for path, text in texts.items()
                            if path not in ignored}, forbidden)
    outside = {key: why for key, why in forbidden.items()
               if key not in ALLOWED_OUTSIDE_THE_RELEASE}
    for why, where in forbidden_hits({path: text for path, text in texts.items()
                                      if path in ignored}, outside).items():
        found.setdefault(why, []).extend(where)
    return found


class NothingIdentifyingShips(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.shipped, cls.reason = shipped_paths()
        # Read once for every test below, not once per test.
        cls.texts, cls.unread = read_text_files(cls.shipped or [], REPO_ROOT)

    @classmethod
    def tearDownClass(cls):
        # Every shipped file's text - about 13 MB - is not kept for the rest
        # of the process once these tests are done.
        del cls.texts, cls.unread

    def setUp(self):
        if self.reason:
            self.skipTest(self.reason)

    def files(self):
        # NO SELF-EXEMPTION any more. The first version spelled the names out
        # and had to skip itself to pass, which meant the one file guaranteed
        # to contain every forbidden string was the one file never checked -
        # and it shipped them. Hashing removed the exemption and the leak in
        # the same change.
        return iter(self.texts)

    def read(self, path):
        return self.texts[path]

    def test_every_shipped_file_is_read_or_known_binary(self):
        """The half that makes reading everything fail closed (#1443).

        A file that does not decode is a file no check below looked at. If
        it is a binary, its suffix belongs on BINARY_SUFFIXES (or, with no
        suffix to go by, its path in UNDECODABLE_BY_DESIGN); if it is text
        in another encoding, it belongs in UTF-8.
        """
        self.assertEqual(self.unread, [],
                         "these ship but were not scanned: they do not "
                         "decode as UTF-8 text and their suffix is not on "
                         "BINARY_SUFFIXES")

    def test_no_forbidden_identifier_is_in_the_export(self):
        if FORBIDDEN is None:
            self.skipTest("the identifier sweep did not run: "
                          f"{DENYLIST} is absent or defines no "
                          "FORBIDDEN. It is not "
                          "tracked - a maintainer keeps a local copy, and CI "
                          "writes it from the FORBIDDEN_IDENTIFIERS secret "
                          "(see .gitignore). Every structural check in this "
                          "file still runs")
        found = forbidden_hits({path: self.read(path) for path in self.files()},
                               FORBIDDEN)
        # self.fail, not assertEqual(found, {}): assertEqual would print the
        # dict itself, labels and all, whatever the message says.
        if found:
            self.fail(hits_report(found))

    def test_no_real_ip_address_is_in_the_export(self):
        found = []
        for path in self.files():
            text = self.read(path)
            for match in IP.finditer(text):
                if ALLOWED_IP.match(match.group(0)):
                    continue
                line = text.count("\n", 0, match.start()) + 1
                found.append(f"{path}:{line} {match.group(0)}")
            # AND THE SAME ADDRESS WRITTEN WITH DASHES. Consumer reverse-DNS
            # spells the octets that way, so an address hidden in a hostname
            # is a real routable one that IP above cannot see. Judged against
            # the same allowlist, so a documentation range written this way
            # is still fine.
            for dotted, offset in dashed_addresses(text):
                if ALLOWED_IP.match(dotted):
                    continue
                line = text.count("\n", 0, offset) + 1
                found.append(f"{path}:{line} {dotted} (written with dashes)")

        self.assertEqual(found, [],
                         "only loopback, private and documentation ranges may "
                         "ship; these are somebody's real machine")

    def test_the_tests_are_covered_by_this_guard(self):
        """The premise the whole file rests on: nothing export-ignores
        `tests/`, so a fixture is as public as the daemon and has to be
        scanned like one.

        What ships, and what must not, is NOT re-asserted here -
        tests/test_internal_files_do_not_ship.py owns that question and had it
        first. Two guards for one property drift apart, and the one nobody
        looks at is the one that quietly stops meaning anything.
        """
        scanned = [p for p in self.files() if p.startswith("tests/")]

        self.assertGreater(len(scanned), 100,
                           "tests/ is missing from the scan - if it ever "
                           "stopped shipping, this file's premise changed")


class NothingIdentifyingIsTracked(unittest.TestCase):
    """The same denylist over EVERY tracked text file, export-ignored ones
    included.

    NothingIdentifyingShips reads what the release carries, and export-ignore
    is all that decides that. The development repository is public too, so
    the internal changelog, the workflow notes and the changelog fragments
    can be read by anyone although they never ship - and that sweep never
    reads them. This one does, with ALLOWED_OUTSIDE_THE_RELEASE left out for
    the export-ignored files only.

    Needs git: in the extracted public tree there is no repository to ask,
    and it skips saying so.
    """

    @classmethod
    def setUpClass(cls):
        cls.tracked, cls.ignored, cls.reason = tracked_paths()
        if not cls.reason and not cls.tracked:
            cls.reason = "git lists no tracked file here, so there is nothing to sweep"
        cls.texts, cls.unread = read_text_files(cls.tracked or [], REPO_ROOT)

    @classmethod
    def tearDownClass(cls):
        del cls.texts, cls.unread

    def setUp(self):
        if self.reason:
            self.skipTest(self.reason)

    def test_every_tracked_file_is_read_or_known_binary(self):
        """As test_every_shipped_file_is_read_or_known_binary, for the files
        that do not ship: one that does not decode was not swept."""
        self.assertEqual(self.unread, [],
                         "these are tracked but were not scanned: they do not "
                         "decode as UTF-8 text and their suffix is not on "
                         "BINARY_SUFFIXES")

    def test_no_forbidden_identifier_is_tracked(self):
        if FORBIDDEN is None:
            self.skipTest("the identifier sweep of the tracked files did not "
                          f"run: {DENYLIST} is absent or defines no FORBIDDEN "
                          "(see .gitignore)")
        found = tracked_hits(self.texts, self.ignored, FORBIDDEN)
        # self.fail, not assertEqual(found, {}), as in the export's sweep:
        # assertEqual would print the labels with the dict.
        if found:
            self.fail(hits_report(
                found, heading="these are in the development repository, "
                               "which anyone can read:"))


class TheTokenisersThisGuardNeeded(unittest.TestCase):
    """The three holes the pre-publication sweep found in this file.

    Each one let a real identifier through while the guard reported success,
    and each is tested here rather than only exercised by the export scan - an
    export that happens to be clean would pass whatever these did.
    """

    def test_a_channel_is_one_token_however_it_is_punctuated(self):
        """RFC 2812 lets a channel name carry "&" and "^". WORD stops at both,
        so two real channels were split into fragments no hash could match."""
        found = dict(channel_tokens("see #one&two and #three^four here"))

        self.assertIn("one&two", found)
        self.assertIn("three^four", found)

    def test_the_marker_is_not_part_of_the_name(self):
        """An operator writes "#thing" in one place and "thing" in another.
        One hash has to cover both."""
        self.assertEqual([name for name, _ in channel_tokens("#thing")], ["thing"])

    def test_prose_punctuation_is_not_part_of_the_name(self):
        """"#thing," at the end of a sentence is the same channel."""
        names = [name for name, _ in channel_tokens("joined #thing, then left")]

        self.assertEqual(names, ["thing"])

    def test_an_address_written_with_dashes_is_still_an_address(self):
        """Consumer reverse-DNS spells the octets that way, so a real routable
        address hides inside a hostname where the dotted-quad regex cannot see
        it. One shipped in three files."""
        found = dict(dashed_addresses("host cpe-198-51-100-7.isp.net here"))

        self.assertIn("198.51.100.7", found)

    def test_something_that_is_not_an_address_is_not_read_as_one(self):
        """The other half: four dash-separated numbers are not automatically an
        address, and reporting a version or a date as one would make this
        check noise, which is how a check gets deleted."""
        self.assertEqual(dashed_addresses("build 900-1000-1100-1200"), [])

    def test_a_name_split_by_escapes_is_read_whole(self):
        """The half the earlier escape fix got wrong. Blanking an escape is
        right before a name and destroys one that contains them."""
        escaped = "tag <<<A" + chr(92) + "xe8B" + chr(92) + "xe1Cd>>> here"

        found = [word for word, _ in words(escaped)]

        self.assertIn("AeBaCd", found,
                      "a name written with high-byte escapes is still only "
                      "fragments, so no hash could ever match it")

    def test_the_blanked_pass_still_works(self):
        """Both passes, not one instead of the other: a name AFTER an escape
        must still be found, which is what the first fix was for."""
        line = "'by " + chr(92) + "x0311SomeNick" + chr(92) + "x032]'"

        found = [word for word, _ in words(line)]

        self.assertIn("SomeNick", found)

    def test_an_accented_name_also_answers_to_its_plain_spelling(self):
        """So the list carries one entry per identifier rather than one per
        way of typing it."""
        accented = "R" + chr(233) + "n" + chr(233) + "e"

        self.assertEqual(folded(accented), "Renee")
        self.assertEqual(folded("plain"), "plain")

    def test_an_undecodable_escape_is_left_alone(self):
        """A guess here would corrupt the text every other check reads."""
        broken = chr(92) + "xZZ"

        self.assertEqual(decoded(broken), broken)


class TheDenylistIsAllowedToBeAbsent(unittest.TestCase):
    """What the published tree looks like, asserted rather than assumed."""

    def test_the_guard_imports_without_it(self):
        """FORBIDDEN is None there, and that is a state this file handles -
        not an ImportError at collection time, which would take the whole
        suite down for every contributor who clones the public repo."""
        self.assertTrue("FORBIDDEN" in globals())

    def test_the_structural_checks_do_not_depend_on_it(self):
        """The half that generalises. These are what protect a contributor
        nobody has met, so they must not be wired to the list."""
        self.assertEqual(dashed_addresses("cpe-198-51-100-7.x")[0][0],
                         "198.51.100.7")
        self.assertTrue(channel_tokens("#a&b"))


class ReadingWordsOutOfSource(unittest.TestCase):
    """words(), on its own, with no repository involved.

    Deliberately NOT part of NothingIdentifyingShips. That class asks git for
    the list of files that would ship and skips itself wherever git cannot
    answer - a reasonable thing for a scan of the export to do, and exactly
    the wrong thing for these two, which test a pure function over a string
    and are the only cover the blanking step has.

    Left in that class they skipped in every environment without git, which
    is to say the regression tests for the hole would not have run in the
    place the hole was found.
    """

    def test_a_name_next_to_an_escape_is_still_one_word(self):
        """The hole that let three real nicks through three scrub passes.

        A captured IRC line keeps its colour codes as ESCAPES in the source,
        and the scan reads source rather than evaluated values. Without the
        blanking step the "x" of "\x0311" starts a word and swallows the nick
        behind it, so the name hashes to something no FORBIDDEN entry can ever
        match. Every hash above is worthless in that position.
        """
        line = "Record: 4788.6 by " + chr(92) + "x0311SomeNick" + chr(92) + "x032]"

        found = [word for word, _offset in words(line)]

        self.assertIn("SomeNick", found,
                      "a nick written straight after a colour escape must "
                      "still be read as its own word - it is not, if the "
                      "escape is left in place for WORD to start on")
        self.assertNotIn("x0311SomeNick", found)

    def test_blanking_an_escape_does_not_move_the_line_numbers(self):
        """Escapes become spaces, not nothing. An empty replacement shortens
        the text and every line number reported after the first escape in a
        file becomes wrong - which is worse than useless in a failure whose
        entire job is to say WHERE."""
        text = ("first" + chr(10) + "second " + chr(92) + "x03" + "Target"
                + chr(10) + "third")

        offset = [o for w, o in words(text) if w == "Target"][0]

        self.assertEqual(text.count(chr(10), 0, offset) + 1, 2)
        self.assertEqual(text[offset:offset + 6], "Target")

if __name__ == "__main__":
    unittest.main()
