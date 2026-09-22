"""support.py did not redirect ADMIN_TOKENS_FILE; every password check read
the operator's real token store (audit L40, #704).

DCCoreTestCase redirects sixteen state files but not db.ADMIN_TOKENS_FILE,
and every _check_password() path goes through db.load_admin_tokens() on
it. So test_adminchat's login tests read ./data/adminchat_tokens.json from
the cwd - on a machine whose bot has paired dccore.mrc, each wrong-password
test verified PBKDF2 against every real token - and a `pair` reached from
any DCCoreTestCase but the two that redirected the path themselves would
have written the operator's live store; preflight's data/ walk would have
caught the write, nothing the read. (FETCHED_FILES_DIR, the audit's other
name, was redirected by #643.) The harness redirects the store now, and
restores the constant on teardown.
"""

import os
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import adminchat  # noqa: E402
import db  # noqa: E402
import defaults as config  # noqa: E402

from tests.support import DCCoreTestCase  # noqa: E402

REAL_STORE = os.path.abspath(os.path.join(REPO_ROOT, "data", "adminchat_tokens.json"))


class _Session:
    """What _check_password() touches on a session, and nothing more."""
    peer_ip = "127.0.0.1"
    nick = "SysOp"
    host = "sysop.users.undernet.org"
    authenticated = False
    attempts = 0
    last_activity = 0.0
    structured = False

    def send(self, _text):
        pass


class InsideTheHarness(DCCoreTestCase):

    def test_the_store_is_a_temp_file_in_both_places(self):
        self.assertNotEqual(os.path.abspath(db.ADMIN_TOKENS_FILE), REAL_STORE)
        self.assertEqual(os.path.abspath(config.ADMIN_TOKENS_FILE), os.path.abspath(db.ADMIN_TOKENS_FILE))
        self.assertIn(self._fetch_history_dir, os.path.abspath(db.ADMIN_TOKENS_FILE))

    def test_a_password_check_probes_only_the_redirected_store(self):
        """The audit's probe: os.path.exists wrapped, a wrong password
        checked with no hash configured."""
        probed = []
        real_exists = db.os.path.exists

        def recording(path):
            probed.append(os.path.abspath(str(path)))
            return real_exists(path)
        db.os.path.exists = recording
        self.addCleanup(setattr, db.os.path, "exists", real_exists)
        self.set_config(ADMIN_PASSWORD_HASH="")

        import contextlib
        import io
        with contextlib.redirect_stdout(io.StringIO()):
            adminchat._check_password(_Session(), "wrong")

        self.assertNotIn(REAL_STORE, probed)
        self.assertIn(os.path.abspath(db.ADMIN_TOKENS_FILE), probed)

    def test_a_pair_writes_the_temp_store_not_the_real_one(self):
        before = os.path.exists(REAL_STORE) and os.path.getmtime(REAL_STORE)

        db.save_admin_tokens({"dccore.mrc-test": {"hash": "x"}})

        self.assertTrue(os.path.exists(db.ADMIN_TOKENS_FILE))
        self.assertEqual(os.path.exists(REAL_STORE) and os.path.getmtime(REAL_STORE), before,
                         "the operator's token store was touched")


class AfterTheHarness(unittest.TestCase):

    def test_the_constant_is_put_back(self):
        """A plain TestCase after a DCCoreTestCase: the module constant is
        the real path again, so nothing outside the harness is redirected
        by accident."""
        case = InsideTheHarness("test_the_store_is_a_temp_file_in_both_places")
        result = unittest.TestResult()
        case.run(result)
        self.assertEqual((result.failures, result.errors), ([], []))

        self.assertEqual(os.path.abspath(db.ADMIN_TOKENS_FILE), REAL_STORE)


if __name__ == "__main__":
    unittest.main()
