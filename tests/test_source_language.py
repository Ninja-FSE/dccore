"""The daemon's own source stays English and ASCII.

TWO INVARIANTS, AND WHY BOTH ARE NEEDED

#65 and #66 translated every module from Swedish. Two things then went wrong,
and each of these tests exists because of one of them:

  * #57 added new Swedish comments to config.py after the translation had
    branched, so they were never in its scope and shipped untranslated.
    Translation is not a one-off - without a guard the next feature branch
    reintroduces it.

  * The translation itself was driven by searching for accented characters,
    which silently missed eighteen lines of Swedish that happen to contain no
    a-ring, a-umlaut or o-umlaut - "Kunde inte skicka JOIN", "Startar
    tidtagaruret", and so on. An accent scan cannot see those.

So one test looks for non-ASCII, and one looks for Swedish WORDS, because
neither finds what the other does.

A THIRD ROUND, AND WHAT IT SAYS ABOUT WORD LISTS

The word list below missed sixteen more comments across six modules - the
colour-block themes in announce.py and list.py, a channel-sync header in
commands.py, two settings in config.py, a path comment in dcc.py and two
sentences in irc.py - and this test passed the entire time.

Two of them had reached settings.conf.sample, a GENERATED file an operator
reads, so the miss was shipping Swedish to users rather than merely leaving it
in a comment.

That is the same failure the docstring above already describes, one round
later: a check built from a list finds what is on the list. The list is now
longer, which helps and does not fix the shape of the problem. The honest
summary is that this test raises the cost of reintroducing Swedish; it does
not prove there is none.

WHY THIS IS NOT MERELY TIDINESS

print() encodes with whatever code page the attached stream has. cp1252
contains a-ring/a-umlaut/o-umlaut; cp1253, cp1251, cp932 and ascii do not, and
an uncaught UnicodeEncodeError kills the thread that printed. #59 added a guard
so no character can take the process down, but the guard is a safety net -
English ASCII source is the actual fix.

tests/ is deliberately exempt. Its fixtures include "Bjork/Joga.flac" and a
400-character run of a-umlaut precisely to prove non-ASCII FILENAMES work;
translating those would destroy what they test.
"""
import io
import os
import re
import tokenize
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Words with no English homograph, so a single hit is conclusive. Deliberately
# excludes ones that read as English too - "men", "till", "per", "med", "name"
# - which produced false positives when this list was first calibrated.
SWEDISH = re.compile(
    r"\b("
    r"och|att|inte|ingen|inget|inga|kunde|skicka|skickar|skickas|skickade|"
    # Added after two lines survived every earlier pass: the sent-file
    # notice in dcc.py and the address line irc.py prints on every boot.
    # "skickade" was listed but "skickades" was not, which is the whole
    # story - the list grows one real miss at a time, not by guessing.
    r"skickades|filen|felfritt|identifiering|klar|klart|satt|"
    r"startar|returnerar|misslyckades|ladda|laddade|hitta|hittade|"
    r"konvertera|konverterar|loggar|anvandare|nekade|rensar|sparar|"
    r"vantar|packar|raderade|filerna|mappen|namnet|minnet|heltal|antalet|"
    r"totala|botens|gick|handskakning|understreck|apostrof|saknar|"
    r"originalet|matchar|textfiler|huvudkanaler|konsolen|slutnotis|"
    r"tidtagaruret|kanalen|fildelning|sokning|sokvag|listan|kon|fran|"
    # Round two. Every word below was in the tree while this test reported
    # it clean - sixteen comments across six modules, two of which had
    # reached the generated, user-facing settings.conf.sample.
    # Round three, and the first one that was not about the list at all: the
    # scan below could not see the MIDDLE lines of a docstring, so a Swedish
    # paragraph in commands.py survived every pass. These are the words that
    # paragraph and the rehash notice were written in.
    #
    # "alla" is deliberately NOT here even though it appears in both. It
    # matches "Alla marcia" in tests/test_announce_output.py's Beethoven
    # fixture - the exact false-positive trap the note at the top of this
    # list already describes. "tar" is out for the same reason: archive.tar.gz.
    r"blivit|moduler|uppdaterade|enbart|manuellt|ovan|efter|"
    r"turkos|kant|fetstil|understruken|kursiv|bakgrundsplatta|vita|"
    r"antal|samtidiga|nedladdningar|textrader|spottas|vid|"
    r"helautomatisk|synk|borttagna|nya|spika|direkt|"
    r"tystade|annonsering|kanalannonsering|reklamklockan|kanalreklam|"
    r"felmeddelande|minsta|utan|vakthunden|trigga|samma|"
    r"aktiveringslogik|eventuella|dubbla|snedstreck|slutet|innan|mappar|"
    # "mot" was here and is now removed, for the reason the note at the top
    # of this list gives: once stems take suffixes, Swedish "mot" + "or"
    # is the English word "motor", and irc.py's "Breaking to reconnect
    # motor..." tripped it immediately. Same call as "alla" (Alla marcia)
    # and "tar" (archive.tar.gz) - a three-letter preposition cannot be
    # conclusive on a single hit, and Swedish prose containing it will
    # contain something else on this list too.
    r"bygger|meddelandet|officiella|centralstyrda|exakt|kopia|vackra|"
    r"paustid|sekunder|mellan|varje|"
    # Round four, and the first one where the LIST was not the problem. Two
    # lines shipped the whole way to the release prep:
    #
    #   oserve.py  "[CRITICAL MAIN ERROR] Huvudloopen dippade"
    #   irc.py     "[ERROR] Fel under server-handskakningen"
    #
    # The second is the instructive one. "handskakning" was ALREADY on this
    # list. The code says "handskakningen" - the definite form - and the
    # trailing word boundary refused to match a stem with a suffix on it.
    # Adding "handskakningen" would have fixed that one line and left the next
    # inflection of the next word to be found by hand all over again.
    #
    # So the group below matches Swedish's definite and plural endings after
    # any listed stem. That is a different kind of fix from lengthening the
    # list: it makes every word already here cover its own inflections.
    #
    # "huvudloop" and "dippa" are added the old way, because no stem of theirs
    # was ever listed - a suffix group cannot help with a word the list has
    # never seen, which remains the real shape of this problem.
    r"huvudloop|dippa"
    r")(?:en|et|erna|arna|orna|ar|er|or|na|n|t|s)?\b",
    re.IGNORECASE,
)


def _modules():
    """Every .py file the daemon itself is built from."""
    out = []
    for name in sorted(os.listdir(REPO_ROOT)):
        if name.endswith(".py") and name != "admin_config.py":
            out.append(os.path.join(REPO_ROOT, name))
    return out


# Python 3.12 stopped emitting an f-string as one STRING token and started
# splitting it into FSTRING_START / FSTRING_MIDDLE / FSTRING_END. Nearly every
# message in this codebase is an f-string, so reading token.string alone made
# almost all of them invisible on 3.12+ while still passing on 3.11 - a hole
# far bigger than the one being fixed, and only visible because putting the
# rehash notice back in Swedish did not fail this test.
#
# Looked up by name rather than named directly: CI runs two Python versions and
# the older one has no such constants to import.
_PROSE_TOKENS = {tokenize.COMMENT, tokenize.STRING}
for _name in ("FSTRING_START", "FSTRING_MIDDLE", "FSTRING_END"):
    if hasattr(tokenize, _name):
        _PROSE_TOKENS.add(getattr(tokenize, _name))


def _prose_lines(path):
    """(line number, text) for every line of every comment and string literal.

    This used to be a line filter: a line counted as prose if it started with
    "#" or contained a quote character. That only ever saw the FIRST and LAST
    lines of a docstring - the middle ones start with neither - so a Swedish
    paragraph in the middle of one was invisible to this test through three
    separate rounds of "the last Swedish lines".

    Tokenising decides WHICH lines are prose; the text returned is still the
    raw source line, because how a string's interior is tokenised changed
    between the Python versions CI runs and the span never did. A line holding
    both code and a string is returned whole, exactly as the old filter did.
    """
    with io.open(path, encoding="utf-8") as handle:
        lines = handle.read().splitlines()

    wanted = set()
    with io.open(path, "rb") as handle:
        for token in tokenize.tokenize(handle.readline):
            if token.type in _PROSE_TOKENS:
                wanted.update(range(token.start[0], token.end[0] + 1))
    return [(n, lines[n - 1]) for n in sorted(wanted) if n <= len(lines)]


class TheSourceIsAscii(unittest.TestCase):
    """#59's table: cp1253, cp1251, cp932 and ascii cannot encode a-ring,
    a-umlaut or o-umlaut, and an uncaught UnicodeEncodeError from a print()
    kills the thread that ran it."""

    def test_no_module_contains_a_non_ascii_character(self):
        offenders = []
        for path in _modules():
            with io.open(path, encoding="utf-8") as handle:
                lines = handle.readlines()
            for n, line in enumerate(lines, 1):
                if any(ord(c) > 127 for c in line):
                    offenders.append(
                        f"{os.path.basename(path)}:{n}: "
                        + line.strip().encode("ascii", "backslashreplace").decode()[:70])
        self.assertEqual(
            offenders, [],
            "non-ASCII in module source:\n  " + "\n  ".join(offenders))


class TheSourceIsEnglish(unittest.TestCase):
    """An accent scan cannot see 'Kunde inte skicka JOIN'. This can."""

    def test_no_module_contains_swedish(self):
        offenders = []
        for path in _modules():
            for n, text in _prose_lines(path):
                found = SWEDISH.findall(text)
                if found:
                    offenders.append(
                        f"{os.path.basename(path)}:{n}: {sorted(set(w.lower() for w in found))} "
                        f"-> {text.strip()[:60]}")
        self.assertEqual(
            offenders, [],
            "Swedish in module source:\n  " + "\n  ".join(offenders))

    def test_it_can_see_the_middle_of_a_docstring(self):
        """Control for the defect that motivated the rewrite, not for the word
        list. A line filter passes this; the tokenizer does not."""
        lines = [
            "def f():",
            '    """A summary line in English.',
            "    kunde inte skicka anything",
            '    """',
        ]
        import tempfile
        with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False,
                                         encoding="utf-8") as handle:
            handle.write(chr(10).join(lines) + chr(10))
            path = handle.name
        self.addCleanup(os.remove, path)

        prose = [text for _n, text in _prose_lines(path)]

        self.assertTrue(any(SWEDISH.findall(text) for text in prose),
                        "the middle of a docstring is still invisible")

    def test_it_can_see_inside_an_f_string(self):
        """Control for the second miss, found by mutation: 3.12 splits an
        f-string into its own token types, and nearly every message in this
        codebase is an f-string."""
        lines = ["def f(user):",
                 '    print(f"[REHASH] Alla moduler har blivit uppdaterade av {user}")']
        import tempfile
        with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False,
                                         encoding="utf-8") as handle:
            handle.write(chr(10).join(lines) + chr(10))
            path = handle.name
        self.addCleanup(os.remove, path)

        prose = [text for _n, text in _prose_lines(path)]

        self.assertTrue(any(SWEDISH.findall(text) for text in prose),
                        "the inside of an f-string is invisible on this Python")

    def test_the_detector_actually_detects(self):
        """Control. A word list that matched nothing would let this pass on a
        fully Swedish file."""
        sample = 'print(f"[DB ERROR] Kunde inte skicka ban-notis: {err}")'
        self.assertTrue(SWEDISH.findall(sample),
                        "the detector no longer recognises Swedish")

    def test_it_sees_a_stem_with_a_suffix_on_it(self):
        """Control for the suffix group specifically.

        Added after a mutation run deleted that group and NOTHING failed -
        there is no Swedish left in the tree, so the mechanism had nothing to
        prove itself against. That is the same shape as the miss it was added
        for: "handskakning" was on the list, the code said "handskakningen",
        and the word boundary refused the match for four rounds.
        """
        for word in ("handskakningen", "handskakningar", "filerna",
                     "meddelandet", "sekunderna"):
            with self.subTest(word=word):
                self.assertTrue(SWEDISH.findall(word),
                                "an inflected form is invisible again")

    def test_the_suffix_group_does_not_swallow_english(self):
        """The other side of it. Widening a matcher is only safe if it stays
        specific - "motor" is Swedish "mot" plus "-or", which is why "mot" is
        no longer on the list at all."""
        for phrase in ("Breaking to reconnect motor...",
                       "the sender and the receiver",
                       "a list of files and folders"):
            with self.subTest(phrase=phrase):
                self.assertEqual(SWEDISH.findall(phrase), [])


def _docs():
    """Every Markdown file shipped to a reader."""
    out = [os.path.join(REPO_ROOT, "README.md")]
    docs = os.path.join(REPO_ROOT, "docs")
    for name in sorted(os.listdir(docs)):
        if name.endswith(".md"):
            out.append(os.path.join(docs, name))
    return out


_FENCE = re.compile(r"^\s*(```|~~~)")
_CODE_SPAN = re.compile(r"`[^`]*`")


def _markdown_prose(path):
    """(line number, text) for the prose of a Markdown file.

    Fenced blocks are dropped and inline `code spans` are blanked, because
    docs/UPDATES.md's v1.10.0-RC3 entry quotes the Swedish it is reporting
    the removal of - `"Kunde inte skicka JOIN"`, `"Startar tidtagaruret"` -
    and the accented characters an accent scan cannot see. Those quotations
    are the evidence for the entry; a check that failed on them would be
    demanding the changelog lie about what it changed.

    Blanking rather than deleting keeps the line numbers honest in a failure
    message.
    """
    with io.open(path, encoding="utf-8") as handle:
        lines = handle.read().splitlines()

    out, in_fence = [], False
    for n, line in enumerate(lines, 1):
        if _FENCE.match(line):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        out.append((n, _CODE_SPAN.sub("``", line)))
    return out


class TheShippedDocsAreEnglish(unittest.TestCase):
    """The modules were translated in #65/#66 and are held there by the tests
    above. docs/UPDATES.md kept its Swedish half until the release prep, on the
    reasoning that a changelog records history as it was written - which is
    true of the facts and not of the language they are in. It was the largest
    file in the repository and the least readable to a newcomer.

    The same argument the module guard makes applies here: without a check, the
    next branch reintroduces it. Same word list, so a word learned from a real
    miss in a module is learned for the docs too.
    """

    def test_no_shipped_document_contains_swedish(self):
        scanned, offenders = [], []
        for path in _docs():
            scanned.append(path)
            for n, text in _markdown_prose(path):
                found = SWEDISH.findall(text)
                if found:
                    offenders.append(
                        f"{os.path.basename(path)}:{n}: "
                        f"{sorted(set(w.lower() for w in found))} "
                        f"-> {text.strip()[:60]}")
        self.assertEqual(
            offenders, [],
            "Swedish in shipped documentation:\n  " + "\n  ".join(offenders))
        self.assertIn("UPDATES.md", [os.path.basename(q) for q in scanned],
                      "the changelog was not scanned - an empty file "
                      "list makes every assertion above vacuously true")

    def test_prose_outside_a_code_span_is_still_read(self):
        """Control, and the one that matters. Blanking code spans is what lets
        the RC3 entry keep its quotations; blanking too much would make the
        test above pass on a wholly Swedish document without noticing."""
        import tempfile
        with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False,
                                         encoding="utf-8") as handle:
            handle.write("Boten kunde inte skicka filen.\n")
            path = handle.name
        self.addCleanup(os.remove, path)

        prose = [text for _n, text in _markdown_prose(path)]

        self.assertTrue(any(SWEDISH.findall(text) for text in prose),
                        "bare prose is no longer being read")

    def test_a_fenced_block_is_skipped(self):
        """Console transcripts and config samples get pasted into fences, and
        they are not prose an author chose the language of. Added after a
        mutation run: removing the fence handling broke nothing, because no
        document happens to have Swedish in a fence today."""
        import tempfile
        with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False,
                                         encoding="utf-8") as handle:
            handle.write("Run it:" + chr(10) + "```" + chr(10) +
                         "kunde inte skicka filen" + chr(10) + "```" + chr(10))
            path = handle.name
        self.addCleanup(os.remove, path)

        prose = [text for _n, text in _markdown_prose(path)]

        self.assertFalse(any(SWEDISH.findall(text) for text in prose),
                         "the inside of a fenced block is being read as prose")

    def test_a_quotation_inside_a_code_span_is_exempt(self):
        """The RC3 entry, in miniature. Without this the changelog could not
        name the strings it removed."""
        import tempfile
        with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False,
                                         encoding="utf-8") as handle:
            handle.write('Removed `"Kunde inte skicka JOIN"` from the boot path.\n')
            path = handle.name
        self.addCleanup(os.remove, path)

        prose = [text for _n, text in _markdown_prose(path)]

        self.assertFalse(any(SWEDISH.findall(text) for text in prose),
                         "a quoted string is being reported as untranslated prose")


# Plain files an operator reads or copies. Not Markdown and not Python, so
# neither guard above looked at them - and two Swedish comments had already
# reached settings.conf.sample once, which is GENERATED and is the file an
# operator copies to settings.conf.
PLAIN_FILES = (
    ".gitignore",
    "requirements.txt",
    "settings.conf.sample",
    "admin_config.py.sample",
)

_ACCENTS = "åäöÅÄÖ"


def has_swedish_accent(line):
    """A separate function so a control can drive it directly.

    Inline, it had no control: a mutation run disabled the check and
    nothing failed, because no file in the tree currently has an accent
    in it. A scan that only passes because there is nothing to find
    proves nothing about whether it can find anything.
    """
    return any(c in line for c in _ACCENTS)


def _plain_lines(name):
    path = os.path.join(REPO_ROOT, name)
    if not os.path.exists(path):
        return []
    with io.open(path, encoding="utf-8") as handle:
        return list(enumerate(handle.read().splitlines(), 1))


class ThePlainOperatorFilesAreEnglish(unittest.TestCase):
    """The gap the two guards above left between them.

    .gitignore shipped two Swedish comments the whole way to the release
    prep - "Ignorera allt innehall i lists/" and "Men ignorera INTE
    .gitkeep-filen i lists/" - because the module guard reads .py files and
    the docs guard reads .md files, and .gitignore is neither.

    admin_config.py.sample ends in .py.sample, not .py, so it fell through the
    same gap. settings.conf.sample is generated from defaults.py, which IS
    covered - but only the parts of it that survive into the generated file,
    and the docstring on the module guard already records two Swedish comments
    reaching that sample once.

    Both checks run here, because neither finds what the other does: an accent
    scan cannot see "Kunde inte skicka JOIN", and a word list cannot see a word
    nobody thought to add.
    """

    def test_no_plain_file_contains_swedish(self):
        offenders = []
        for name in PLAIN_FILES:
            for number, line in _plain_lines(name):
                found = SWEDISH.findall(line)
                if found:
                    offenders.append(
                        f"{name}:{number}: {sorted(set(w.lower() for w in found))} "
                        f"-> {line.strip()[:60]}")

        self.assertEqual(offenders, [],
                         "Swedish in a file an operator reads:\n  " +
                         "\n  ".join(offenders))

    def test_no_plain_file_contains_a_swedish_accent(self):
        """Cheap, and catches the words the list does not have. These files
        have no reason to carry a-ring, a-umlaut or o-umlaut at all: unlike
        the docs, none of them quotes anything."""
        offenders = []
        for name in PLAIN_FILES:
            for number, line in _plain_lines(name):
                if has_swedish_accent(line):
                    offenders.append(f"{name}:{number}: {line.strip()[:60]}")

        self.assertEqual(offenders, [],
                         "a Swedish accent in a file an operator reads:\n  " +
                         "\n  ".join(offenders))

    def test_the_files_it_names_are_actually_there(self):
        """A renamed or deleted file would make this pass by reading nothing,
        which is the failure mode of every allowlist-shaped check."""
        missing = [name for name in PLAIN_FILES
                   if not os.path.exists(os.path.join(REPO_ROOT, name))]

        self.assertEqual(missing, [],
                         "named here but not in the tree, so nothing is scanned")

    def test_it_reads_something(self):
        """Control. Empty files would satisfy both scans above."""
        for name in PLAIN_FILES:
            with self.subTest(file=name):
                self.assertTrue(_plain_lines(name))

    def test_the_accent_scan_actually_detects(self):
        """Control for the scan itself, added after a mutation run turned it
        off and nothing failed - there is no accent in the tree today, so the
        assertion had nothing to prove it worked."""
        for accent in _ACCENTS:
            with self.subTest(accent=accent):
                self.assertTrue(has_swedish_accent(f"ignorera n{accent}got"))

        self.assertFalse(has_swedish_accent("# Ignore everything inside lists/"))


if __name__ == "__main__":
    unittest.main()
