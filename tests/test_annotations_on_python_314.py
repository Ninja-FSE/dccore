"""Every declared setting is found, including on Python 3.14.

FOUND BY RUNNING THE DAEMON, not by a test. During the RC1 beta the dashboard
came up and the Settings page was empty - every category rendered its heading
and not one field. The API was correct: it returned 8 categories and 92 fields
when asked from a shell.

The difference was the interpreter. `start-dccore.bat` runs the daemon with
`py -3`, which on that machine is Python 3.14; the shell probe used `python`,
which is 3.13.

PEP 649, new in 3.14, made annotations lazy. A module now carries an
`__annotate__` function, and `__annotations__` is built the first time the
ATTRIBUTE is read. `vars(module)` returns the raw `__dict__`, which does not
contain it until then - so `declared_types(vars(config))` came back empty
while `config.__annotations__` held all 94.

    Python 3.13:  vars(config)["__annotations__"] -> 94 entries
    Python 3.14:  vars(config).get("__annotations__") -> None

WHAT THAT COST

Nothing raised, which is why it reached a beta rather than a stack trace.
Every caller simply saw a configuration with no declared settings:

  * the dashboard's Settings page rendered zero editable fields
  * apply_to() and save() both fell back to the default value's runtime type,
    so `WEBUI_CONSOLE_ENABLED: bool = None` typed as NoneType and came
    through as raw text rather than a bool

CI covers 3.10 and 3.12. The README says "Python 3.10+", which now includes
3.14, so nothing in the matrix was wrong - the matrix was just behind. It has
3.14 in it now; this file is the unit-level guard that does not depend on
which interpreter happens to run it.
"""

import os
import sys
import types
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import defaults as config  # noqa: E402
import settings_file  # noqa: E402


class EverySettingIsDeclared(unittest.TestCase):

    def test_the_real_config_reports_its_settings(self):
        """The symptom, at the level it was seen: zero settings means an empty
        Settings page."""
        declared = settings_file.declared_types(vars(config))

        self.assertGreater(len(declared), 50,
                           "config declares far more settings than this - a "
                           "near-empty answer is the 3.14 failure")

    def test_it_agrees_with_the_attribute(self):
        """`vars()` and the attribute must give the same answer. On 3.14 they
        did not, and only the attribute was right."""
        from_vars = settings_file.declared_types(vars(config))
        direct = {name: kind
                  for name, kind in getattr(config, "__annotations__", {}).items()
                  if isinstance(kind, type)}

        self.assertEqual(from_vars, direct)

    def test_a_few_settings_of_each_type_are_present(self):
        """Guard on the guard: a dict of the right SIZE could still be the
        wrong contents."""
        declared = settings_file.declared_types(vars(config))

        self.assertEqual(declared.get("MAX_DCC_SLOTS"), int)
        self.assertEqual(declared.get("NICKNAME"), str)
        self.assertEqual(declared.get("WEBUI_ENABLED"), bool)


class TheLazyCaseIsHandledOnEveryVersion(unittest.TestCase):
    """Reproduced by construction rather than by interpreter version, so this
    guards the fix on 3.10 through 3.14 alike."""

    def module_without_annotations_in_dict(self):
        """A module whose annotations are reachable ONLY as an attribute -
        which is exactly the shape PEP 649 gives every module on 3.14."""
        module = types.ModuleType("dccore_fake_config")
        module.SOME_INT = 5
        module.SOME_TEXT = "x"
        # Set on the TYPE so it is not in the instance __dict__ that vars()
        # returns - the same asymmetry 3.14 introduces.
        module.__dict__.pop("__annotations__", None)
        sys.modules[module.__name__] = module
        self.addCleanup(sys.modules.pop, module.__name__, None)
        return module

    def test_annotations_only_on_the_attribute_are_still_found(self):
        module = self.module_without_annotations_in_dict()
        namespace = dict(vars(module))
        namespace.pop("__annotations__", None)
        module.__annotations__ = {"SOME_INT": int, "SOME_TEXT": str}

        declared = settings_file.declared_types(namespace)

        self.assertEqual(declared, {"SOME_INT": int, "SOME_TEXT": str})

    def test_annotations_in_the_dict_are_still_used(self):
        """The pre-3.14 path must keep working, and must not be overridden by
        a stale module of the same name."""
        namespace = {"__name__": "dccore_fake_config",
                     "__annotations__": {"A": int}}

        self.assertEqual(settings_file.declared_types(namespace), {"A": int})

    def test_a_namespace_with_neither_is_empty_not_an_error(self):
        self.assertEqual(settings_file.declared_types({}), {})

    def test_an_unknown_module_name_is_empty_not_an_error(self):
        self.assertEqual(
            settings_file.declared_types({"__name__": "no.such.module.here"}),
            {})

    def test_non_type_annotations_are_still_ignored(self):
        """Unchanged: anything that is not a plain type is skipped rather than
        guessed at, and the caller falls back to the default's own type."""
        namespace = {"__annotations__": {"A": int, "B": "a string annotation"}}

        self.assertEqual(settings_file.declared_types(namespace), {"A": int})


class TheSettingsPageWouldHaveFields(unittest.TestCase):
    """The end the operator sees. declared_types() feeding the payload is the
    whole reason an empty answer emptied the page."""

    def test_the_settings_payload_is_not_empty(self):
        import webserver

        payload = webserver.build_settings_payload()
        total = sum(len(c["fields"]) for c in payload["categories"])

        self.assertGreater(payload["categories"], [], "no categories at all")
        self.assertGreater(total, 50,
                           "the Settings page would render headings with no "
                           "fields under them - the 3.14 symptom exactly")


if __name__ == "__main__":
    unittest.main()
