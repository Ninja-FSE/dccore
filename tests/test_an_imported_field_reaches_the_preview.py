"""The OmenServe import preview maps its rows by LABEL, and nothing checks
that the labels on the two sides still agree.

`web/app.js`'s `renderImportPreview()` turns each preview row into a "now" and
an "after" figure by looking its label up in a map written by hand. The labels
it looks for live in `omenserve_import.FIELDS`. Change one of those labels, or
add a field, and the JavaScript silently has no entry for it - so that row
renders with no "now" and no "after".

That failure reads as "nothing has been imported yet", not as a fault. Nobody
chases it, which is exactly how #414 went unnoticed: the packed/plain split in
#417 added a second field feeding `total_files`, and the preview mapping had
to be updated by hand to match. It was - but the full suite stayed green when
that update was removed again, which means nothing was holding it.

A field with `target` None is preview-only. It is shown for context and never
lands in the stats, so it wants no map entry and none is asserted.
"""

import io
import os
import unittest

import omenserve_import

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _app_js():
    with io.open(os.path.join(REPO_ROOT, "web", "app.js"),
                 encoding="utf-8") as handle:
        return handle.read()


class EveryImportedFieldReachesThePreview(unittest.TestCase):

    def test_every_importing_field_label_is_mapped_in_app_js(self):
        source = _app_js()
        mapped = [field.label for field in omenserve_import.FIELDS
                  if field.target is not None]
        # A guard over an empty list passes while asserting nothing. The two
        # counters feeding total_files are the case this exists for, so the
        # floor is above one rather than above zero.
        self.assertGreater(
            len(mapped), 1,
            "No importing fields found - omenserve_import.FIELDS has changed "
            "shape and this guard is no longer reading it correctly.")
        for label in mapped:
            self.assertTrue(
                '"%s"' % label in source,
                "web/app.js's import preview does not map %r, so that row "
                "shows no 'now' or 'after' figure." % label)

    def test_more_than_one_field_feeds_the_files_total(self):
        """The specific shape #417 established, and the one a future edit is
        most likely to undo: an install running both add-ons has two separate
        real counts of files sent, and both belong in the imported total."""
        feeding = [field.label for field in omenserve_import.FIELDS
                   if field.target == "total_files"]
        self.assertGreater(
            len(feeding), 1,
            "Only %r feeds total_files. OmenServe's plain-file counter and "
            "mxrarserver's packed counter are both real send counts - "
            "dropping either undercounts the import (#414)." % (feeding,))
