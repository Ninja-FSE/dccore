"""A loopback probe skipped its module when its ports were only in TIME_WAIT (#1469).

Four modules decide whether to run their real-socket tests with a
loopback_is_usable() probe: bind a listener on the range the tests use, dial
it, accept. Two of them bound without SO_REUSEADDR. A port a run has just
used stays in TIME_WAIT for a minute or so, and on POSIX a plain bind on it
is refused, so a second run soon after the first found no port in its window
and skipped every test as "cannot bind a listener and dial loopback" - on a
machine where loopback works, and where the daemon's own listener, which sets
the option through platform_compat.prepare_listener(), binds the same port
without trouble. A skip there says "not here", which was not true.

The other two set SO_REUSEADDR themselves, on every platform. On Windows
that option means something else (see platform_compat.prepare_listener()),
and the daemon's listener sets SO_EXCLUSIVEADDRUSE there instead, so those
probes did not bind the way the listener they stand in for does.

There was also one probe for many windows. The shared probe checked
test_dcc_resume_end_to_end's 51200-51210, and five modules that import it
send on 51300, 51320, 51340 and 51360: a free 51200 ran their real-socket
tests on a window that could be taken, and a taken one skipped them on a
window that was free. The probe now takes the window, and every module asks
about the one its sends are configured with.

The probe, pointed at one port of this test's own that is in TIME_WAIT and
nothing else, must still find loopback usable; and each module's probe must
try exactly the ports its real sends are given.
"""

import importlib
import os
import socket
import sys
import unittest
from unittest import mock

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import platform_compat  # noqa: E402

from tests.support import DCCoreTestCase  # noqa: E402
from tests import test_dcc_resume_end_to_end as e2e  # noqa: E402

# The probe each module's real-socket class is guarded by, and that class:
# its setUp configures the window its sends use.
#
# KEPT BY HAND. A skipUnless() is evaluated when its module is imported and
# leaves no record of which probe it called, so this list cannot be derived
# from the classes. A new class guarded by a loopback_is_usable() - the
# shared one in test_dcc_resume_end_to_end or a module's own wrapper of it -
# belongs here, or nothing checks that it probes the ports it sends on.
GUARDS = (
    ("tests.test_dcc_resume_end_to_end", "loopback_is_usable", "TheWholeExchange"),
    ("tests.test_queue_progress_is_recorded", "loopback_is_usable",
     "TheTransferRowCarriesItsOwnSize"),
    ("tests.test_complete_means_the_receiver_acked_it", "loopback_is_usable", "ARealReceiver"),
    ("tests.test_a_receiver_that_never_connects_is_said_so", "loopback_is_usable",
     "ARealListenerNobodyDials"),
    ("tests.test_a_bigger_send_block_keeps_the_slow_link_floor", "loopback_is_usable",
     "ARealTransfer"),
    ("tests.test_a_record_of_finished_transfers", "loopback_is_usable", "ASendIsRecorded"),
    ("tests.test_the_speed_caps", "loopback_is_usable", "ASendUnderACap"),
    ("tests.test_a_nick_change_mid_transfer_keeps_the_slot_and_the_queue", "loopback_is_usable",
     "ARenameDuringASend"),
    ("tests.test_the_queue_save_never_touches_the_live_dict", "loopback_is_usable",
     "AFailedWakeStillReleasesTheUser"),
    ("tests.test_the_longest_waiting_nick_gets_the_slot", "renamed.loopback_is_usable",
     "ARealSendPutsTheNickAtTheBack"),
    ("tests.test_a_list_request_goes_ahead_of_queued_files", "renamed.loopback_is_usable",
     "ARealListSendKeepsThePlace"),
)


def resolve(module, dotted):
    for name in dotted.split("."):
        module = getattr(module, name)
    return module


def a_port_in_time_wait(test):
    """A loopback port whose only use is a closed connection in TIME_WAIT.

    The listener's side closes first, so it is the listener's port - the one
    a later listener binds - that holds the TIME_WAIT, as after a DCC send."""
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    client = conn = None
    try:
        platform_compat.prepare_listener(listener)
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
        listener.listen(1)
        client = socket.create_connection(("127.0.0.1", port), timeout=2)
        conn, _ = listener.accept()
        conn.close()                 # the active close: TIME_WAIT lands on `port`
        client.settimeout(2)
        client.recv(1)               # the FIN has arrived
    except OSError as exc:
        test.skipTest("this runner cannot bind a listener and dial loopback: %s" % exc)
    finally:
        for sock in (client, conn, listener):
            if sock is not None:
                try:
                    sock.close()
                except OSError:
                    pass
    return port


def a_plain_bind_is_refused(port):
    """True when a bind with no address-reuse option fails on `port`."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(("0.0.0.0", port))
        return False
    except OSError:
        return True
    finally:
        sock.close()


class EveryProbeFindsLoopbackUsableOnAPortInTimeWait(unittest.TestCase):

    def setUp(self):
        if platform_compat.IS_WINDOWS:
            self.skipTest("exercises the POSIX address-reuse case (SO_REUSEADDR); the "
                          "daemon's Windows listener sets SO_EXCLUSIVEADDRUSE instead, "
                          "which the probe already matches through prepare_listener()")
        self.port = a_port_in_time_wait(self)
        # The premise, checked rather than assumed: without the option the
        # port really is refused. Where this host lets the bind through, the
        # probes are not being tested against TIME_WAIT at all.
        if not a_plain_bind_is_refused(self.port):
            self.skipTest("this host lets a plain bind through over TIME_WAIT, "
                          "so the hazard cannot be set up here")

    def test_the_probe_answers_true(self):
        self.assertTrue(e2e.loopback_is_usable(self.port, self.port),
                        "the probe found no usable port on a loopback port in TIME_WAIT")


class _NothingBinds:
    """Stands in for the socket module the probe uses: records every port a
    listener is bound to, and refuses each, so the probe tries them all."""

    AF_INET = socket.AF_INET
    SOCK_STREAM = socket.SOCK_STREAM

    def __init__(self):
        self.ports = []

    def socket(self, *_args, **_kwargs):
        tried = self.ports

        class Listener:
            def setsockopt(self, *_args):
                pass

            def bind(self, address):
                tried.append(address[1])
                raise OSError("refused by the test")

            def close(self):
                pass
        return Listener()

    def create_connection(self, *_args, **_kwargs):
        raise OSError("never reached")


class EveryModuleProbesTheWindowItSendsOn(unittest.TestCase):

    # Each class below is set up and torn down by hand to read its window:
    # the settings a DCCoreTestCase sets are set inside this test too, on
    # purpose, and are not its leak.
    SETTINGS_THE_HARNESS_SETS = DCCoreTestCase.SETTINGS_THE_HARNESS_SETS

    def window_of(self, case_class):
        """(DCC_PORT_START, DCC_PORT_END) as the class's own setUp sets them.

        The setUp is stopped right there: one of them goes on to start a
        real send, which is its test's business, not this one's."""
        name = unittest.defaultTestLoader.getTestCaseNames(case_class)[0]
        case = case_class(name)
        window = {}

        class GotTheWindow(Exception):
            pass

        def set_config(**overrides):
            DCCoreTestCase.set_config(case, **overrides)
            for key in ("DCC_PORT_START", "DCC_PORT_END"):
                if key in overrides:
                    window[key] = overrides[key]
            if len(window) == 2:
                raise GotTheWindow()
        case.set_config = set_config
        try:
            case.setUp()
        except GotTheWindow:
            pass
        finally:
            try:
                case.tearDown()
            finally:
                case.doCleanups()
        self.assertEqual(len(window), 2, "%s's setUp gives its sends no port range" % case_class.__name__)
        return window["DCC_PORT_START"], window["DCC_PORT_END"]

    def ports_probed_by(self, probe):
        fake = _NothingBinds()
        with mock.patch.object(e2e, "socket", fake):
            self.assertFalse(probe())
        return fake.ports

    def test_each_probe_tries_exactly_the_ports_its_sends_use(self):
        for module_name, probe_name, class_name in GUARDS:
            with self.subTest(module=module_name, cls=class_name):
                module = importlib.import_module(module_name)
                start, end = self.window_of(getattr(module, class_name))

                probed = self.ports_probed_by(resolve(module, probe_name))

                self.assertEqual(probed, list(range(start, end + 1)),
                                 "%s's probe asks about ports its sends never use" % module_name)


if __name__ == "__main__":
    unittest.main()
