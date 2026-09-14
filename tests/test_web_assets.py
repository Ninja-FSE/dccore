"""The dashboard's static files are never parsed by anything until a browser
loads them.

There are over a thousand tests in this suite and, until this file, not one of
them read web/. That gap cost a whole feature: two PRs that each appended a
rule to the end of web/style.css were merged, the join lost one closing brace,
and every rule after it - the entire File Lists folder grouping, including
`.file-row.is-hidden` which is what collapsing a folder actually does - became
part of an unclosed block and stopped applying. The dashboard rendered every
folder permanently expanded, Expand all and Collapse all did nothing, and the
suite stayed green.

A browser will not tell you either. CSS is specified to recover from errors
silently: an unclosed block swallows what follows and the page just looks
wrong. That is why this is a test rather than something anyone would notice.

These checks are deliberately structural, not stylistic. They do not care what
the CSS says, only that a browser will read all of it; not what the JavaScript
does, only that it parses; and not what the page looks like, only that the
elements the script reaches for are actually in it. Each one corresponds to a
mistake that has actually happened here.
"""

import io
import os
import re
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WEB = os.path.join(REPO_ROOT, "web")


def read(name):
    with io.open(os.path.join(WEB, name), encoding="utf-8") as handle:
        return handle.read()


def strip_css_comments(text):
    return re.sub(r"/\*.*?\*/", "", text, flags=re.S)


class TheStylesheetIsWellFormed(unittest.TestCase):

    def test_every_block_is_closed(self):
        """The bug this file was written for.

        An unclosed rule does not fail loudly anywhere: the browser folds the
        rest of the file into it, so rules that read perfectly well in the
        source never apply.
        """
        css = strip_css_comments(read("style.css"))

        depth, line, unclosed = 0, 1, []
        for char in css:
            if char == "\n":
                line += 1
            elif char == "{":
                depth += 1
                unclosed.append(line)
            elif char == "}":
                depth -= 1
                self.assertGreaterEqual(
                    depth, 0, f"style.css line {line}: closing brace with "
                              f"nothing open")
                if unclosed:
                    unclosed.pop()

        self.assertEqual(
            unclosed, [],
            "style.css has block(s) opened and never closed, starting at "
            "line(s) " + ", ".join(str(n) for n in unclosed) +
            " - everything after the first one is swallowed and never applies")

    def test_the_collapse_rule_is_reachable(self):
        """Named explicitly because it is the one that broke, and because a
        rule can be present in the file and still never apply.

        Counting braces before it is how you tell the difference between "the
        rule is in the stylesheet" and "the browser will use it".
        """
        css = strip_css_comments(read("style.css"))
        needle = ".file-row.is-hidden"

        self.assertIn(needle, css, "the rule that collapses a folder is gone")
        before = css[:css.index(needle)]
        self.assertEqual(
            before.count("{") - before.count("}"), 0,
            f"{needle} sits inside an unclosed block, so a collapsed folder "
            f"would stay visible")


class TheScriptIsWellFormed(unittest.TestCase):

    def test_brackets_and_strings_balance(self):
        """Not a parser - a structural check for the mistakes that actually
        happen when JavaScript is assembled from concatenated strings: a
        bracket left open, or a quote left unterminated."""
        source = read("app.js")
        pairs = {")": "(", "]": "[", "}": "{"}
        stack, line, i, n, problems = [], 1, 0, len(source), []

        while i < n:
            char = source[i]
            if char == "\n":
                line += 1
                i += 1
                continue
            if source.startswith("//", i):
                i = source.find("\n", i)
                i = n if i < 0 else i
                continue
            if source.startswith("/*", i):
                end = source.find("*/", i + 2)
                line += source.count("\n", i, end)
                i = end + 2
                continue
            if char in "\"'":
                quote, j = char, i + 1
                while j < n:
                    if source[j] == "\\":
                        j += 2
                        continue
                    if source[j] == quote:
                        break
                    if source[j] == "\n":
                        problems.append(f"line {line}: unterminated string")
                        break
                    j += 1
                i = j + 1
                continue
            if char in "([{":
                stack.append((char, line))
            elif char in ")]}":
                if not stack or stack[-1][0] != pairs[char]:
                    problems.append(f"line {line}: unexpected {char!r}")
                elif stack:
                    stack.pop()
            i += 1

        problems += [f"line {ln}: {ch!r} never closed" for ch, ln in stack]
        self.assertEqual(problems, [], "app.js: " + "; ".join(problems))


class TheScriptAndThePageAgree(unittest.TestCase):

    def test_every_element_the_script_looks_up_exists(self):
        """getElementById returns null for a missing id, and the failure lands
        later and elsewhere - an addEventListener on null, halfway through
        setup, taking the rest of the script's initialisation with it."""
        page = read("index.html")

        missing = sorted({
            element_id
            for element_id in re.findall(r'getElementById\("([^"]+)"\)', read("app.js"))
            if f'id="{element_id}"' not in page
        })

        self.assertEqual(
            missing, [],
            "app.js looks up element id(s) that index.html does not define: "
            + ", ".join(missing))

    def test_the_script_never_removes_a_view_section(self):
        """The sibling of the test above: an element that exists at load time
        but is DELETED at runtime fails exactly the same way, and that check
        cannot see it because index.html still defines the id.

        It happened. `disableConsoleUi()` removed `#view-console` when the
        Console was switched off, and `activateView()` walks every key in
        `views` calling `getElementById("view-" + key).classList` on each - so
        after the removal that threw on EVERY view switch. The Console went
        away and took the rest of the navigation with it: Settings stayed on
        its "Loading" placeholder because the exception fired before the branch
        that loads it, and Queue, Stats and Downloads stopped refreshing too.
        Reported from a real install, running the shipped default.

        This is structural, not behavioural - nothing here executes
        JavaScript (see this module's own docstring). It cannot prove the
        navigation works; it can refuse the specific move that broke it, which
        is deleting a section the router still expects to find. Hiding one is
        fine and is what the fix does.
        """
        script = read("app.js")

        # `var x = document.getElementById("view-...")` followed later by
        # `x.remove()`, and the direct form.
        removed = set(re.findall(
            r'getElementById\("(view-[^"]+)"\)\s*;?\s*\n?[^\n]*\.remove\(\)', script))
        removed |= set(re.findall(
            r'getElementById\("(view-[^"]+)"\)\.remove\(\)', script))

        # The indirect form the real defect used: assigned to a name, removed
        # a line or two later.
        for name, view_id in re.findall(
                r'var\s+(\w+)\s*=\s*document\.getElementById\("(view-[^"]+)"\)', script):
            if re.search(r'\b' + re.escape(name) + r'\.remove\(\)', script):
                removed.add(view_id)

        self.assertEqual(
            sorted(removed), [],
            "app.js removes view section(s) that activateView() still looks up "
            "on every switch: " + ", ".join(sorted(removed)) +
            ". Hide them instead - .hidden = true - or the router throws and "
            "takes every other view down with it.")

    def test_the_view_router_tolerates_a_missing_section(self):
        """Belt to the braces above. Even with no section deleted, a null from
        getElementById must not take the whole router down - the failure that
        made one disabled feature disable the entire dashboard."""
        script = read("app.js")

        router = script[script.index("function activateView"):]
        router = router[:router.index("\n  }")]

        self.assertNotRegex(
            router, r'getElementById\("view-" \+ key\)\.classList',
            "activateView() dereferences getElementById directly. One absent "
            "section then throws before the per-view loaders below it run, so "
            "every other view stops working too. Assign it and check for null.")

    def test_every_view_the_nav_offers_exists_in_both(self):
        """A nav button whose view is missing throws on the first click; a view
        the script does not know about can never be activated."""
        page = read("index.html")
        script = read("app.js")

        nav = set(re.findall(r'data-view="([^"]+)"', page))
        sections = set(re.findall(r'id="view-([^"]+)"', page))

        # A button with no section throws on the first click, so that
        # direction is absolute. The other direction allows a section the
        # script opens itself - see opened_in_code() above for why one exists.
        opened = TheNavTheViewsAndTheSectionsAgree.opened_in_code(script)

        self.assertEqual(sorted(nav - sections), [],
                         f"nav button(s) with no view section, which throw on "
                         f"the first click: {sorted(nav - sections)}")
        self.assertEqual(sorted(sections - nav - opened), [],
                         f"view section(s) nothing can open: "
                         f"{sorted(sections - nav - opened)}")
        for name in sorted(nav):
            self.assertRegex(
                script, r"\b%s:\s*\{" % re.escape(name),
                f"index.html offers the {name!r} view but app.js's views table "
                f"has no entry for it, so its title and subtitle would be "
                f"undefined")



class TheElementMapHasNoDuplicateKeys(unittest.TestCase):
    """Two entries with the same name in `el` merge cleanly and break loudly
    nowhere.

    A duplicate key in a JavaScript object literal is not an error: the last
    one silently wins. So two branches that each add a button and reach for
    the same obvious name - `filelistsPurgeBtn` for a per-list purge on one
    and a bulk purge on the other - produce a textually clean git merge and a
    dashboard where one of the two buttons does nothing at all, with no
    console error and nothing in any diff to look at.

    Caught between #388 and #389, which did exactly that.
    """

    @staticmethod
    def keys():
        js = read("app.js")
        block = js.split("var el = {", 1)[1]
        # To the close of the literal: the first line that is a lone "};".
        # To the close of the literal: the first line that is a lone "};",
        # built without an escape so no editing tool can mangle it.
        block = block.split(chr(10) + "  };", 1)[0]
        return re.findall(r"^\s{4}([A-Za-z_]\w*)\s*:", block, re.M)

    def test_the_map_this_reads_is_the_right_one(self):
        """Fixture invariant: an empty list has no duplicates in it either."""
        found = self.keys()

        self.assertGreater(len(found), 20)
        self.assertIn("navItems", found)

    def test_no_name_is_used_twice(self):
        found = self.keys()
        seen, twice = set(), []
        for name in found:
            if name in seen:
                twice.append(name)
            seen.add(name)

        self.assertEqual(twice, [],
                         "these el keys are declared more than once, and only "
                         "the last of each survives at runtime: "
                         + ", ".join(twice))


class TheNoticeBadgeKeepsRefreshing(unittest.TestCase):
    """A badge that only draws once is a badge that is always out of date.

    loadQueue() is called ONCE, at startup - the sidebar's recurring refresh
    is a separate inline function beside it that fetches /api/queue directly.
    So a call placed in loadQueue() looks like it rides on the queue poll and
    does not: the badge would be correct at page load and then frozen until
    somebody pressed F5, which is exactly the moment it is least useful.

    Nothing in this suite executes JavaScript (see this module's docstring),
    so this reads the recurring block itself rather than trusting where the
    call appears to be.
    """

    @staticmethod
    def recurring_block():
        """The body of the interval that refreshes the sidebar status card."""
        js = read("app.js")
        after = js.split("setInterval(function () {", 1)[1]
        return after.split("}, REFRESH_MS);", 1)[0]

    def test_the_block_this_reads_is_the_right_one(self):
        """Fixture invariant. If the split ever stopped matching, the checks
        below would run against an empty string and pass vacuously."""
        body = self.recurring_block()

        self.assertIn("/api/queue", body)
        self.assertIn("renderSidebarStatus", body)

    def test_the_badge_is_refreshed_on_every_tick(self):
        self.assertIn("loadNotices(", self.recurring_block(),
                      "the notice badge is not refreshed by the recurring "
                      "sidebar poll, so it would never change while the "
                      "dashboard stayed open")

    def test_it_is_also_drawn_before_the_first_tick(self):
        """REFRESH_MS of no badge after a reload, otherwise - on the one page
        load where the operator is most likely to be looking for it.

        Read from the INITIALISATION region only, between the first call to
        loadQueue() and the interval that follows it. Everything before that
        point includes loadNotices's own definition and the activateView()
        branch that opens the panel, so a search over it matches whether or
        not the startup call exists - which is how the first version of this
        test passed against code with the call deleted.
        """
        js = read("app.js")
        # The call statement, not the definition: "  loadQueue();" with
        # its two-space indent occurs once, and only as the startup call.
        startup = js.split("  loadQueue();", 1)[1]
        startup = startup.split("setInterval(function () {", 1)[0]

        self.assertIn("loadNotices(", startup,
                      "nothing draws the badge between page load and the "
                      "first tick of the sidebar poll")

    def test_the_panel_underneath_redraws_only_when_it_is_on_screen(self):
        """Same rule the queue table already follows: a background poll must
        not clobber what the operator is reading on another view."""
        self.assertIn('loadNotices(state.active === "notices")',
                      self.recurring_block())


class HiddenActuallyHides(unittest.TestCase):
    """`hidden` is not a class, it is an attribute, and the UA stylesheet
    gives it `display: none` at the lowest specificity there is.

    So ANY class rule that sets `display` on the same element outranks it, and
    the element stays on screen while the markup, the script and the reviewer
    all say it is hidden. `.filelists-filter-actions { display: flex }` did
    exactly this: the row of buttons was visible from first paint, before
    anything had been selected for them to act on.
    """

    @staticmethod
    def classes_with_hidden():
        """Every class on an element that carries the `hidden` attribute."""
        html = read("index.html")
        found = set()
        for tag in re.findall(r"<[a-zA-Z][^>]*>", html):
            if not re.search(r"[\s\"']hidden(?=[\s>=])", tag):
                continue
            match = re.search(r'class="([^"]*)"', tag)
            if match:
                found.update(match.group(1).split())
        return found

    @staticmethod
    def sets_display(css, selector):
        for block in re.findall(
                re.escape(selector) + r"\s*\{([^}]*)\}", css):
            if re.search(r"(?<![-\w])display\s*:", block):
                return True
        return False

    def test_every_hideable_element_wins_against_its_own_display_rule(self):
        css = strip_css_comments(read("style.css"))
        for name in sorted(self.classes_with_hidden()):
            if not self.sets_display(css, "." + name):
                continue
            with self.subTest(css_class=name):
                self.assertTrue(
                    self.sets_display(css, ".%s[hidden]" % name),
                    "." + name + " sets display, so `hidden` on that element "
                    "does nothing without a matching .%s[hidden] rule" % name)

    def test_the_check_can_see_a_class_that_needs_the_pair(self):
        """The guard is only worth having if something is actually in its
        scope - an empty sweep passes for the wrong reason."""
        css = strip_css_comments(read("style.css"))
        needing = [n for n in self.classes_with_hidden()
                   if self.sets_display(css, "." + n)]

        self.assertTrue(needing, "nothing was examined")

    def test_the_console_nav_button_is_covered_too(self):
        """#438: classes_with_hidden() only harvests `hidden` off elements
        that carry it literally in index.html's markup - the console nav
        button never does. disableConsoleUi() sets `navButton.hidden = true`
        at runtime instead, the first time /api/console/log 404s (the
        Console's shipped-default state on any non-loopback install), so
        this class was structurally invisible to the sweep above even
        though it needed the exact same pair.

        Named directly rather than taught to the general sweep: finding
        every element hidden from JS rather than markup would mean tracing
        every `.hidden = ` assignment back to the selector or element
        reference that produced it, which is a materially bigger scanner
        than one bug's regression test justifies.
        """
        js = read("app.js")
        css = strip_css_comments(read("style.css"))

        self.assertIn('".nav-item[data-view=\\"console\\"]"', js,
                     "fixture invariant: disableConsoleUi() no longer "
                     "selects the nav button this way - the scan below is "
                     "checking the wrong thing")
        self.assertIn("navButton.hidden = true", js,
                     "fixture invariant: disableConsoleUi() no longer hides "
                     "the button this way - the scan below is checking the "
                     "wrong thing")

        self.assertTrue(self.sets_display(css, ".nav-item"),
                        "fixture invariant: .nav-item no longer sets "
                        "display - the [hidden] pair would no longer be "
                        "needed")
        self.assertTrue(
            self.sets_display(css, ".nav-item[hidden]"),
            ".nav-item sets display, so `hidden` on the console nav button "
            "does nothing without a matching .nav-item[hidden] rule - the "
            "button stays on screen, clickable, opening an empty log pane "
            "that posts into a 404 route")


class RulesThatOverrideActuallyWin(unittest.TestCase):
    """A CSS rule can be present, well-formed, and still do nothing.

    Dimming the file rows under a folder heading was written as
    `.file-row td`, which is specificity (0,1,1). `.data-table tbody td` had
    already set the full-strength colour at (0,1,2), and specificity beats
    source order - so the new rule lost wherever it was placed in the file,
    and the rows rendered exactly as before.

    That is the same failure as the unclosed brace in shape: the stylesheet
    reads correctly and the page ignores it. Nothing here checks appearance,
    only that a rule written to override another one is capable of doing so.
    """

    @staticmethod
    def specificity(selector):
        """(ids, classes, elements) - CSS's own ordering.

        Parsed by walking the selector rather than by regex: the element count
        needs word boundaries, and every attempt to express those through the
        layers between here and the file turned one into a literal backspace.
        This is duller and cannot be mangled.
        """
        ids = classes = elements = 0
        # Combinators separate compound selectors; they carry no weight.
        for part in (selector.replace(">", " ").replace("+", " ")
                     .replace("~", " ").split()):
            i, n = 0, len(part)
            while i < n:
                char = part[i]
                if char == "#":
                    ids += 1
                    i += 1
                elif char == ".":
                    classes += 1
                    i += 1
                elif char == "[":
                    classes += 1
                    close = part.find("]", i)
                    i = n if close < 0 else close + 1
                    continue
                elif char == ":":
                    # ::before and friends count as elements, :hover as a class.
                    if part.startswith("::", i):
                        elements += 1
                        i += 2
                    else:
                        classes += 1
                        i += 1
                elif char == "*":
                    i += 1
                    continue
                elif char.isalpha():
                    elements += 1
                else:
                    i += 1
                    continue
                # Consume the name that follows the sigil (or the bare element).
                while i < n and (part[i].isalnum() or part[i] in "-_"):
                    i += 1
        return (ids, classes, elements)

    def selectors_setting(self, prop):
        """[(selector, specificity)] for every rule that sets `prop`."""
        css = strip_css_comments(read("style.css"))
        found = []
        for match in re.finditer(r"([^{}]+)\{([^}]*)\}", css):
            selector, body = match.group(1).strip(), match.group(2)
            if re.search(r"(^|;)\s*%s\s*:" % re.escape(prop), body):
                for one in selector.split(","):
                    found.append((one.strip(), self.specificity(one)))
        return found

    def test_the_file_row_colour_beats_the_general_cell_colour(self):
        """The concrete case. Both set `color` on the same cells, so the more
        specific one is the one that renders."""
        rules = dict(self.selectors_setting("color"))

        general = [s for s in rules if s == ".data-table tbody td"]
        self.assertTrue(general, "the general table cell colour rule is gone; "
                                 "this test needs updating rather than deleting")

        overrides = [(s, spec) for s, spec in rules.items()
                     if "file-row" in s and ":hover" not in s]
        self.assertTrue(overrides, "nothing dims the file rows any more")

        for selector, spec in overrides:
            self.assertGreater(
                spec, rules[".data-table tbody td"],
                f"{selector!r} has specificity {spec}, which does not beat "
                f"'.data-table tbody td' at {rules['.data-table tbody td']} - "
                f"so the file rows keep the full-strength colour and the rule "
                f"does nothing")

    def test_the_specificity_calculation_is_correct(self):
        """Fixture invariant, and asserted on exact values rather than on
        ordering.

        An earlier version compared only orderings, and passed while the
        element count was stuck at zero for every selector - the word-boundary
        escape in its regex had been mangled into a literal backspace. Every
        comparison still held, because they were all equally wrong.
        """
        cases = [
            ("td", (0, 0, 1)),
            (".x td", (0, 1, 1)),
            (".file-row td", (0, 1, 1)),
            (".data-table tbody td", (0, 1, 2)),
            (".data-table tbody tr.file-row td", (0, 2, 3)),
            (".file-row:hover td", (0, 2, 1)),
            ("#id .c e", (1, 1, 1)),
            ("a[href]", (0, 1, 1)),
            ("li::before", (0, 0, 2)),
            ("ul > li + li", (0, 0, 3)),
        ]

        wrong = [f"{sel!r}: got {self.specificity(sel)}, expected {expected}"
                 for sel, expected in cases
                 if self.specificity(sel) != expected]

        self.assertEqual(wrong, [], "; ".join(wrong))


class APostJsonResultIsReadTheWayPostJsonReturnsIt(unittest.TestCase):
    """postJson() resolves with { ok, status, data } - and nothing here
    executes JavaScript, so reading a property it does not have is silent.

    Found by writing it wrong. The served-folder editor (#164 step 4) was
    written against `res.body`, which is `undefined` on every one of those
    objects: the save would have thrown a TypeError on its first success, in a
    handler with no catch, and the page would have gone quiet with no error
    anywhere. Exactly the shape of #267 - a JavaScript defect that every test
    in this suite passed straight through, found by a real click.

    Cheap to state as a rule, so it is stated as one rather than left to the
    next author to remember which of the two helpers returns which shape.
    """

    def test_no_caller_reads_res_body(self):
        source = read("app.js")

        self.assertNotIn("res.body", source,
                         "postJson() resolves with { ok, status, data } - "
                         "res.body is undefined, and reading through it throws "
                         "inside a promise nothing catches")

    def test_the_helper_still_returns_that_shape(self):
        """The assertion above is only meaningful while this is true. If
        postJson ever does return `body`, this fails first and says so, rather
        than the rule silently becoming wrong."""
        source = read("app.js")

        body = source.split("function postJson(", 1)[1].split("\n  }", 1)[0]

        self.assertIn("ok: res.ok", body)
        self.assertIn("data: data", body)


class TheNavTheViewsAndTheSectionsAgree(unittest.TestCase):
    """Three lists that have to name the same set, and nothing checks them at
    runtime because nothing here executes JavaScript.

    activateView() walks every key in `views` and looks up "view-" + key, so a
    key with no section is a null dereference that takes the whole router
    down - which is exactly what #267 was. A nav button with no `views` entry
    reads `views[name].title` on undefined. And a section nobody can reach is
    dead markup that still costs a lookup on every switch.

    Written when Queue was folded into Stats and three tabs were renamed
    (#133), because that change touched all three lists at once and a
    half-applied rename would have been invisible until somebody clicked.
    """

    def sets(self):
        import re

        js = read("app.js")
        html = read("index.html")
        block = js.split("var views = {", 1)[1].split("};", 1)[0]
        return (set(re.findall(r"^\s*([a-z]+):", block, re.M)),
                set(re.findall(r'id="view-([a-z]+)"', html)),
                set(re.findall(r'data-view="([a-z]+)"', html)),
                self.opened_in_code(js))

    @staticmethod
    def opened_in_code(js):
        """Views something in the script opens directly, with no nav button.

        The nav rail is where you GO; a view can instead be somewhere you are
        SENT. "What happened" is the first: it is opened by the notices badge,
        which appears only when there is something in it, and a permanent nav
        entry for a page that is almost always empty is a permanent reminder
        of nothing.

        This keeps the reachability check honest rather than relaxing it - a
        section still has to be openable by SOMETHING, and dead markup with
        neither a button nor a call is still caught.
        """
        return set(re.findall(r'activateView\("([a-z]+)"\)', js))

    def test_every_views_key_has_a_section(self):
        views, sections, _nav, _opened = self.sets()

        self.assertEqual(sorted(views - sections), [],
                         "activateView() looks up view-<key> for every key in "
                         "`views`; a missing section is a null dereference on "
                         "EVERY view switch, not just that one")

    def test_every_nav_button_has_a_views_entry(self):
        views, _sections, nav, _opened = self.sets()

        self.assertEqual(sorted(nav - views), [],
                         "activateView() reads views[name].title, which is a "
                         "TypeError for a nav button with no entry")

    def test_every_section_is_reachable(self):
        """By a nav button, or by an activateView() call somewhere in the
        script. A section with neither is markup nobody can ever see, and it
        still costs a lookup on every single view switch."""
        _views, sections, nav, opened = self.sets()

        self.assertEqual(sorted(sections - nav - opened), [],
                         "a view section with no nav button and nothing "
                         "calling activateView() for it cannot be opened")

    def test_the_openers_are_real_views(self):
        """The other direction, and the reason the test above is not a way to
        wave anything through: a name passed to activateView() that is not a
        `views` key reads views[name].title on undefined - a TypeError that
        takes the router down for every view, not just that one."""
        views, _sections, _nav, opened = self.sets()

        self.assertEqual(sorted(opened - views), [],
                         "activateView() is called with a name the views "
                         "table does not define")

    def test_the_default_view_exists(self):
        """state.active starts at "search" before any click."""
        views, sections, nav, _opened = self.sets()

        for name, group in (("views", views), ("sections", sections),
                            ("nav", nav)):
            with self.subTest(group=name):
                self.assertIn("search", group)


class EveryElementReferenceIsDeclared(unittest.TestCase):
    """`el` is built once at load from getElementById calls, and every use goes
    through it. A property that was never declared is `undefined`, and what
    happens next is the whole problem:

      * `el.thing.textContent = x` is a TypeError inside whatever handler ran
      * `if (!el.thing) { return; }` - the careful form - makes the feature a
        SILENT NO-OP: the element exists in the page, the code runs, and
        nothing ever appears

    The second is worse and is what this caught. The List Browser's staleness
    banner was written against `el.filelistsFreshness` without adding it to the
    map, so the guard at the top of its render function returned every time and
    the banner could never have shown - on a page nothing in this project
    executes.

    The id check above cannot see it: that reads getElementById("literal")
    calls, and a missing declaration means there is no such call to find.
    """

    def sets(self):
        import re

        js = read("app.js")
        used = set(re.findall(r"\bel\.([A-Za-z][A-Za-z0-9]*)", js))
        block = js.split("var el = {", 1)[1].split("\n  };", 1)[0]
        declared = set(re.findall(r"^\s*([A-Za-z][A-Za-z0-9]*):", block, re.M))
        return used, declared

    def test_nothing_reads_an_undeclared_element(self):
        used, declared = self.sets()

        self.assertEqual(sorted(used - declared), [],
                         "app.js reads el.<name> for a name the el map never "
                         "declares - undefined, so the feature is either a "
                         "TypeError or, if guarded, silently does nothing")

    def test_the_map_is_not_full_of_things_nobody_uses(self):
        """The other direction. A declared element nobody reads is a lookup on
        every page load for nothing, and usually the leftover of a removed
        feature - the Queue view's ids were checked against this when that tab
        was folded into Stats."""
        used, declared = self.sets()

        self.assertEqual(sorted(declared - used), [],
                         "the el map declares element(s) nothing reads")


class AskingAgainForAFetchThatFailed(unittest.TestCase):
    """Requested: a failed or rejected fetch is the one an operator most wants to
    retry, and the only way to do it was to go back to the List Browser and
    retype the nick."""

    def setUp(self):
        self.js = read("app.js")

    def test_the_button_is_offered_on_the_rows_that_need_it(self):
        self.assertIn("fetch-retry-btn", self.js)
        self.assertIn("Redownload", self.js)

    def test_it_is_not_offered_on_a_row_that_succeeded(self):
        """There is a Download button there, and re-fetching a list already
        held is what the List Browser's own refresh is for."""
        line = [l for l in self.js.splitlines() if "fetch-retry-btn" in l
                and "closest" not in l]
        self.assertTrue(line)
        guard = self.js.split("var retryBtn", 1)[1][:200]

        self.assertIn('rejected || state === "failed"', guard)

    def test_neither_the_bot_nor_the_filename_goes_into_an_attribute(self):
        """escapeHtml() is textContent -> innerHTML: it encodes & < > and
        leaves a double quote alone, and both of those values come off the
        wire. The row is looked up by its id instead - that one is ours, and
        it is hex."""
        block = self.js.split("var retryBtn", 1)[1].split("var action", 1)[0]

        self.assertNotIn("data-bot=", block)
        self.assertNotIn("data-filename=", block)
        self.assertIn("data-request-id=", block)

    def test_a_list_and_a_file_are_asked_for_the_way_they_were_asked_for(self):
        """They always were different: a list is "@<bot>" and we cannot know
        what the bot will call its archive, while a file is named outright.
        Retrying through the wrong one would create a row of a different kind
        from the one being retried."""
        body = self.js.split("function redownloadFetchRow", 1)[1].split(
            chr(10) + "  // Delegated", 1)[0]

        self.assertIn('request_type === "list"', body)
        self.assertIn("/api/filelists/fetch", body)
        self.assertIn("/api/fetch/enqueue", body)

    def test_the_row_being_retried_is_not_deleted(self):
        """It is the record of what happened, and throwing it away as a side
        effect of retrying would remove the reason the retry was needed."""
        body = self.js.split("function redownloadFetchRow", 1)[1].split(
            chr(10) + "  // Delegated", 1)[0]

        self.assertNotIn("/delete", body)


class TheConsoleLogSelfHealsAfterBeingEnabled(unittest.TestCase):
    """#439: disableConsoleUi() used to clearInterval() and null out
    consoleLogTimer FOR GOOD on the first 404 - the shipped-default state on
    any non-loopback install. Ticking WEBUI_CONSOLE_ENABLED on in Settings
    and saving applies live (the save's own rehash is enough, no restart),
    but nothing in this file ever re-armed that timer: pollConsoleLog() had
    exactly one call site (the page's own init) and nothing called it again.
    Typed commands kept working (POST is unaffected), but the ambient log -
    which is the only place an async command's result ever appears - stayed
    on "Nothing logged yet." for the rest of that tab's life, recoverable
    only with an F5 the page never asked for.

    Nothing here executes JavaScript (see this module's own docstring) -
    these check that the self-healing PATH exists in the source, the same
    structural style as the rest of this file.
    """

    @classmethod
    def setUpClass(cls):
        cls.js = read("app.js")

    def _function_body(self, name):
        marker = "function " + name + "("
        start = self.js.index(marker)
        # The next top-level (2-space-indented) function declaration ends
        # this one - the same slicing technique already used elsewhere in
        # this file for a JS function with no other clean delimiter.
        rest = self.js[start:]
        end = rest.index("\n  function ", 1)
        return rest[:end]

    def test_disable_no_longer_stops_the_poll_for_good(self):
        body = self._function_body("disableConsoleUi")

        self.assertNotIn("consoleLogTimer = null", body,
                         "the timer is nulled out with nothing left to ever "
                         "call pollConsoleLog() again - the log would stay "
                         "dead until an F5 even after the Console is turned "
                         "back on")

    def test_disable_swaps_to_the_slow_recheck_cadence_instead(self):
        body = self._function_body("disableConsoleUi")

        self.assertIn("CONSOLE_LOG_RECHECK_MS", body,
                     "disableConsoleUi() must keep polling at SOME cadence, "
                     "or re-enabling the Console is never noticed")

    def test_a_success_response_can_re_enable_the_ui(self):
        body = self._function_body("pollConsoleLog")

        self.assertIn("enableConsoleUiIfNeeded", body,
                     "nothing on the success path can undo disableConsoleUi()")

    def test_re_enabling_restores_the_fast_cadence(self):
        body = self._function_body("enableConsoleUiIfNeeded")

        self.assertIn("CONSOLE_LOG_POLL_MS", body,
                     "re-enabling must swap back to the fast cadence, not "
                     "leave the slow recheck running forever")

    def test_re_enabling_unhides_the_nav_button(self):
        body = self._function_body("enableConsoleUiIfNeeded")

        self.assertIn("hidden = false", body,
                     "disableConsoleUi() hides the nav button - nothing "
                     "else in the file ever un-hides it")


if __name__ == "__main__":
    unittest.main()
