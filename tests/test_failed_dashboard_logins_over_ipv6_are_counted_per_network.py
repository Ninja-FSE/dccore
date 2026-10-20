"""Failed dashboard logins over IPv6 are counted per /64 network, and per /48.

The dashboard's failed-login pool (_web_bad_ips) and its count of logins
being checked (_web_logins_in_flight) count under web_login._login_counts():
an IPv4 address as it is; an IPv6 address as its /64 ("2001:db8:1:2::/64",
zone id dropped) with the usual MAX_PASSWORD_ATTEMPTS, and as its /48 with
WEB_LOGIN_WIDE_ATTEMPTS (30); an IPv6 address that carries an IPv4 one
(IPv4-mapped ::ffff:192.0.2.1, NAT64 64:ff9b::192.0.2.1, 6to4, Teredo) as
that IPv4 address. web_login._login_key() is the narrowest of those keys.
Anything that is not an address is passed through, so "no address" behaves
as before.

Only documentation addresses are used: 2001:db8::/32, 192.0.2.0/24 and
198.51.100.0/24, alone or inside the NAT64, 6to4 and Teredo prefixes. The
pure half runs everywhere; the HTTP half needs Flask.
"""

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

import adminchat  # noqa: E402
import web_login  # noqa: E402
import webserver  # noqa: E402

from tests.support import DCCoreTestCase  # noqa: E402
from tests.test_every_route_is_behind_the_login import PASSWORD, RouteCase  # noqa: E402

LIMIT = adminchat.MAX_PASSWORD_ATTEMPTS
WINDOW = adminchat.BAD_IP_BLOCK_SECONDS
T0 = 1_700_000_000.0

# Five addresses in one /64, and one in the next /64 of the same /48.
ONE_NETWORK = ["2001:db8::1", "2001:db8::2", "2001:db8::3", "2001:db8::4", "2001:db8::5"]
ONE_NETWORK_KEY = "2001:db8::/64"
NEXT_NETWORK = "2001:db8:0:1::1"

# The /48 those are in, the decided limit for it, one address in each of
# its /64s, and an address in another /48.
WIDE_KEY = "2001:db8::/48"
WIDE_LIMIT = 30
OTHER_WIDE_NETWORK = "2001:db8:1::1"


def in_own_64(number):
    """An address in the /64 `number` of 2001:db8::/48, outside the /64s above."""
    return "2001:db8:0:%x::1" % (0x100 + number)


class _Clock:
    """web_login's time.time(), held at a value the test moves. Only
    web_login's own name `time` is replaced; the time module is not."""

    def __init__(self):
        self.now = T0
        self._patch = mock.patch.object(web_login, "time",
                                        types.SimpleNamespace(time=lambda: self.now))
        self._patch.start()

    def restore(self):
        self._patch.stop()


class TheLoginKey(unittest.TestCase):
    """web_login._login_key() itself."""

    def key(self, value):
        return web_login._login_key(value)

    def assertKey(self, value, expected):
        with self.subTest(value=value):
            self.assertEqual(self.key(value), expected)

    def test_an_ipv4_address_is_its_own_key(self):
        self.assertKey("127.0.0.1", "127.0.0.1")
        self.assertKey("192.0.2.1", "192.0.2.1")
        self.assertKey("198.51.100.200", "198.51.100.200")

    def test_an_ipv6_address_counts_as_its_64(self):
        self.assertKey("2001:DB8:1:2::9", "2001:db8:1:2::/64")
        self.assertKey("2001:db8:1:2:aaaa:bbbb:cccc:dddd", "2001:db8:1:2::/64")
        self.assertKey("2001:db8::1", ONE_NETWORK_KEY)
        self.assertKey(NEXT_NETWORK, "2001:db8:0:1::/64")
        self.assertKey("::1", "::/64")

    def test_a_zone_id_is_dropped(self):
        self.assertKey("fe80::1%eth0", "fe80::/64")
        self.assertKey("fe80::2%3", "fe80::/64")
        # With no host bits set inside the /64, the network address is the
        # address itself, and a key built from the text would keep the zone.
        self.assertKey("fe80::%eth0", "fe80::/64")
        self.assertKey("2001:db8:1:2::%7", "2001:db8:1:2::/64")

    def test_an_ipv4_mapped_address_is_the_ipv4_address(self):
        self.assertKey("::ffff:192.0.2.1", "192.0.2.1")
        self.assertKey("::ffff:c000:201", "192.0.2.1")
        self.assertKey("::FFFF:198.51.100.7", "198.51.100.7")

    def test_what_is_not_an_address_is_returned_as_it_is(self):
        for value in (None, "", "junk", "192.0.2.4%x", "localhost", "2001:db8::/64x"):
            with self.subTest(value=value):
                self.assertIs(self.key(value), value)

    def test_a_value_that_is_not_text_is_returned_as_it_is(self):
        """ipaddress would read these as addresses; the key does not."""
        for value in (2 ** 64, 3221225985, bytes(16), b"\xc0\x00\x02\x01"):
            with self.subTest(value=value):
                self.assertIs(self.key(value), value)

    def test_a_key_passed_in_again_is_unchanged(self):
        for value in ("127.0.0.1", "192.0.2.1", "2001:DB8:1:2::9", "fe80::1%eth0",
                      "fe80::%eth0", "::ffff:192.0.2.1", "::1", None, "", "junk"):
            with self.subTest(value=value):
                once = self.key(value)
                self.assertEqual(self.key(once), once)

    def test_a_nat64_address_is_the_ipv4_address(self):
        self.assertKey("64:ff9b::c000:201", "192.0.2.1")
        self.assertKey("64:ff9b::198.51.100.7", "198.51.100.7")
        # Only the well-known /96 carries an address.
        self.assertKey("64:ff9b:1::c000:201", "64:ff9b:1::/64")

    def test_a_6to4_address_is_the_ipv4_address(self):
        self.assertKey("2002:c000:201::1", "192.0.2.1")
        self.assertKey("2002:c633:6407:5::9", "198.51.100.7")

    def test_a_teredo_address_is_the_client_ipv4_address(self):
        # Server 192.0.2.1, client 198.51.100.7 (stored inverted).
        self.assertKey("2001:0:c000:201:0:0:39cc:9bf8", "198.51.100.7")

    def test_webserver_answers_with_the_same_function(self):
        self.assertIs(webserver._login_key, web_login._login_key)
        self.assertIs(webserver._login_counts, web_login._login_counts)
        self.assertEqual(webserver.WEB_LOGIN_IPV6_PREFIX, 64)


class TheLoginCounts(unittest.TestCase):
    """web_login._login_counts(): every key a login is counted under."""

    def test_an_ipv6_address_is_counted_at_its_64_and_its_48(self):
        self.assertEqual(web_login._login_counts("2001:db8:1:2::9"),
                         (("2001:db8:1:2::/64", LIMIT), ("2001:db8:1::/48", WIDE_LIMIT)))
        self.assertEqual(web_login._login_counts("fe80::1%eth0"),
                         (("fe80::/64", LIMIT), ("fe80::/48", WIDE_LIMIT)))

    def test_an_ipv4_client_has_one_count(self):
        for value in ("192.0.2.1", "::ffff:192.0.2.1", "64:ff9b::c000:201",
                      "2002:c000:201::1"):
            with self.subTest(value=value):
                self.assertEqual(web_login._login_counts(value), (("192.0.2.1", LIMIT),))

    def test_no_address_has_no_count(self):
        for value in (None, ""):
            with self.subTest(value=value):
                self.assertEqual(web_login._login_counts(value), ())
        self.assertEqual(web_login._login_counts("junk"), (("junk", LIMIT),))

    def test_the_narrowest_key_is_the_login_key(self):
        for value in ("192.0.2.1", "2001:db8:1:2::9", "64:ff9b::c000:201", "junk"):
            with self.subTest(value=value):
                self.assertEqual(web_login._login_key(value),
                                 web_login._login_counts(value)[0][0])


class _PoolCase(DCCoreTestCase):

    def setUp(self):
        super().setUp()
        webserver._web_bad_ips.clear()
        webserver._web_logins_in_flight.clear()
        self.addCleanup(webserver._web_bad_ips.clear)
        self.addCleanup(webserver._web_logins_in_flight.clear)
        self.clock = _Clock()
        self.addCleanup(self.clock.restore)

    def fail_from(self, address):
        """One wrong password from `address`, the way login() spends it."""
        self.assertEqual(webserver._reserve_web_login(address), "ok",
                         "%s could not claim an attempt" % address)
        webserver._settle_web_login(address, False)


class FailuresWithinOneNetworkShareOneCount(_PoolCase):

    def test_failures_from_three_addresses_block_the_fourth(self):
        for address in ONE_NETWORK[:LIMIT]:
            self.fail_from(address)

        self.assertEqual(webserver._reserve_web_login(ONE_NETWORK[LIMIT]), "blocked")
        self.assertTrue(webserver._is_bad_web_ip(ONE_NETWORK[LIMIT]))
        self.assertEqual(webserver._web_logins_in_flight, {})

    def test_the_next_network_keeps_its_own_count(self):
        for address in ONE_NETWORK[:LIMIT]:
            self.fail_from(address)

        self.assertFalse(webserver._is_bad_web_ip(NEXT_NETWORK))
        self.assertEqual(webserver._reserve_web_login(NEXT_NETWORK), "ok")
        webserver._settle_web_login(NEXT_NETWORK, None)

    def test_logins_being_checked_are_shared_within_the_network(self):
        """Two in flight from two addresses plus one failure from a third use
        up the allowance; a fourth address of the /64 is turned away."""
        first, second, third, fourth, fifth = ONE_NETWORK
        self.assertEqual(webserver._reserve_web_login(first), "ok")
        self.assertEqual(webserver._reserve_web_login(second), "ok")
        webserver._note_bad_web_login(third)

        self.assertEqual(webserver._reserve_web_login(fourth), "busy")

        # Settled from the addresses that claimed them, the claims are
        # released from the same count they were taken from.
        webserver._settle_web_login(first, None)
        webserver._settle_web_login(second, None)
        self.assertEqual(webserver._web_logins_in_flight, {})
        self.assertEqual(webserver._reserve_web_login(fifth), "ok")
        webserver._settle_web_login(fifth, None)
        self.assertEqual(webserver._web_logins_in_flight, {})

    def test_settled_failures_from_the_claims_block_the_network(self):
        claimed = ONE_NETWORK[:LIMIT]
        for address in claimed:
            self.assertEqual(webserver._reserve_web_login(address), "ok")
        self.assertEqual(webserver._reserve_web_login(ONE_NETWORK[LIMIT]), "busy")

        for address in claimed:
            webserver._settle_web_login(address, False)

        self.assertEqual(webserver._web_logins_in_flight, {})
        self.assertEqual(webserver._reserve_web_login(ONE_NETWORK[LIMIT]), "blocked")

    def test_a_failure_counted_under_the_lock_is_counted_for_the_network(self):
        """_note_bad_web_login_locked() keys its own argument too, so a
        caller holding the lock may pass an address."""
        with webserver._web_bad_ips_lock:
            for address in ONE_NETWORK[:LIMIT]:
                webserver._note_bad_web_login_locked(address, self.clock.now)

        self.assertTrue(webserver._is_bad_web_ip(ONE_NETWORK[LIMIT]))
        self.assertEqual(webserver._reserve_web_login(ONE_NETWORK[LIMIT]), "blocked")

    def test_a_right_password_from_one_address_clears_the_network(self):
        first, second, third, fourth, _ = ONE_NETWORK
        self.fail_from(first)
        self.fail_from(second)

        self.assertEqual(webserver._reserve_web_login(third), "ok")
        webserver._settle_web_login(third, True)

        self.assertEqual(webserver._web_bad_ips, {})
        # The whole allowance is back: three more claims fit.
        for address in (first, second, fourth):
            self.assertEqual(webserver._reserve_web_login(address), "ok")
            webserver._settle_web_login(address, None)

    def test_clearing_one_address_clears_the_network(self):
        for address in ONE_NETWORK[:LIMIT]:
            self.fail_from(address)
        self.assertTrue(webserver._is_bad_web_ip(ONE_NETWORK[0]))

        webserver._clear_bad_web_ip(ONE_NETWORK[LIMIT])

        self.assertFalse(webserver._is_bad_web_ip(ONE_NETWORK[0]))
        self.assertEqual(webserver._web_bad_ips, {})

    def test_the_block_still_runs_out_after_the_window(self):
        for address in ONE_NETWORK[:LIMIT]:
            self.fail_from(address)

        self.clock.now = T0 + WINDOW - 1
        self.assertEqual(webserver._reserve_web_login(ONE_NETWORK[3]), "blocked")
        self.assertTrue(webserver._is_bad_web_ip(ONE_NETWORK[3]))

        self.clock.now = T0 + WINDOW
        self.assertFalse(webserver._is_bad_web_ip(ONE_NETWORK[4]))
        self.assertEqual(webserver._reserve_web_login(ONE_NETWORK[4]), "ok")
        webserver._settle_web_login(ONE_NETWORK[4], None)
        self.assertEqual(webserver._web_bad_ips, {})
        self.assertEqual(webserver._web_logins_in_flight, {})


class IPv4ClientsAreCountedAsBefore(_PoolCase):

    def test_a_mapped_client_and_a_plain_client_share_one_count(self):
        self.fail_from("::ffff:192.0.2.1")
        self.fail_from("::ffff:c000:201")
        self.fail_from("192.0.2.1")

        self.assertTrue(webserver._is_bad_web_ip("192.0.2.1"))
        self.assertTrue(webserver._is_bad_web_ip("::ffff:192.0.2.1"))
        self.assertEqual(webserver._reserve_web_login("192.0.2.1"), "blocked")
        self.assertEqual(webserver._reserve_web_login("::ffff:192.0.2.1"), "blocked")
        self.assertEqual(sorted(webserver._web_bad_ips), ["192.0.2.1"])

    def test_different_ipv4_clients_stay_separate(self):
        for _ in range(LIMIT):
            self.fail_from("192.0.2.1")

        self.assertEqual(webserver._reserve_web_login("192.0.2.1"), "blocked")
        for neighbour in ("192.0.2.2", "198.51.100.1", "::ffff:192.0.2.2"):
            with self.subTest(neighbour=neighbour):
                self.assertFalse(webserver._is_bad_web_ip(neighbour))
                self.assertEqual(webserver._reserve_web_login(neighbour), "ok")
                webserver._settle_web_login(neighbour, None)

    def test_no_address_claims_nothing_and_counts_nothing(self):
        for value in (None, ""):
            with self.subTest(value=value):
                self.assertEqual(webserver._reserve_web_login(value), "ok")
                webserver._settle_web_login(value, False)
                webserver._note_bad_web_login(value)
                self.assertIs(webserver._is_bad_web_ip(value), False)
                self.assertEqual(webserver._web_bad_ips, {})
                self.assertEqual(webserver._web_logins_in_flight, {})


class FailuresWithinOneWideNetworkShareOneCount(_PoolCase):
    """The /48 count, on top of the /64 one."""

    def fail_across_64s(self, how_many, first=0):
        for number in range(first, first + how_many):
            self.fail_from(in_own_64(number))

    def test_thirty_failures_across_a_48_block_a_fresh_64(self):
        self.fail_across_64s(WIDE_LIMIT)

        fresh = in_own_64(WIDE_LIMIT)
        self.assertEqual(webserver._reserve_web_login(fresh), "blocked")
        self.assertTrue(webserver._is_bad_web_ip(fresh))
        self.assertEqual(webserver._web_logins_in_flight, {})

    def test_another_48_keeps_its_own_count(self):
        self.fail_across_64s(WIDE_LIMIT)

        self.assertFalse(webserver._is_bad_web_ip(OTHER_WIDE_NETWORK))
        self.assertEqual(webserver._reserve_web_login(OTHER_WIDE_NETWORK), "ok")
        webserver._settle_web_login(OTHER_WIDE_NETWORK, None)

    def test_twenty_nine_failures_do_not_block_the_48(self):
        self.fail_across_64s(WIDE_LIMIT - 1)

        fresh = in_own_64(WIDE_LIMIT)
        self.assertFalse(webserver._is_bad_web_ip(fresh))
        self.assertEqual(webserver._reserve_web_login(fresh), "ok")
        webserver._settle_web_login(fresh, None)
        self.assertEqual(webserver._web_bad_ips[WIDE_KEY][0], WIDE_LIMIT - 1)
        self.assertFalse(webserver._web_bad_ips[WIDE_KEY][1])

    def test_a_64_still_blocks_at_three_inside_a_48(self):
        """The /48 count does not loosen the /64 one."""
        for address in ONE_NETWORK[:LIMIT]:
            self.fail_from(address)

        self.assertEqual(webserver._reserve_web_login(ONE_NETWORK[LIMIT]), "blocked")
        self.assertEqual(webserver._reserve_web_login(in_own_64(0)), "ok")
        webserver._settle_web_login(in_own_64(0), None)

    def test_a_right_password_clears_the_64_and_the_48(self):
        operator = in_own_64(0)
        self.fail_from(operator)
        self.fail_from(operator)
        self.fail_across_64s(WIDE_LIMIT - 3, first=1)
        self.assertEqual(webserver._web_bad_ips[WIDE_KEY][0], WIDE_LIMIT - 1)

        self.assertEqual(webserver._reserve_web_login(operator), "ok")
        webserver._settle_web_login(operator, True)

        self.assertNotIn(WIDE_KEY, webserver._web_bad_ips)
        self.assertNotIn(web_login._login_key(operator), webserver._web_bad_ips)
        # Both counts start again: 29 more failures across the /48 do not
        # block it, and the operator's /64 has its three again.
        self.fail_across_64s(WIDE_LIMIT - 1 - 2, first=100)
        self.fail_from(operator)
        self.fail_from(operator)
        self.assertEqual(webserver._reserve_web_login(operator), "ok")
        webserver._settle_web_login(operator, None)

    def test_clearing_one_address_clears_its_48(self):
        self.fail_across_64s(WIDE_LIMIT)
        fresh = in_own_64(WIDE_LIMIT)
        self.assertTrue(webserver._is_bad_web_ip(fresh))

        webserver._clear_bad_web_ip(fresh)

        self.assertFalse(webserver._is_bad_web_ip(fresh))
        self.assertNotIn(WIDE_KEY, webserver._web_bad_ips)

    def test_logins_being_checked_are_shared_within_the_48(self):
        claimed = [in_own_64(number) for number in range(WIDE_LIMIT)]
        for address in claimed:
            self.assertEqual(webserver._reserve_web_login(address), "ok")
        self.assertEqual(webserver._web_logins_in_flight[WIDE_KEY], WIDE_LIMIT)

        self.assertEqual(webserver._reserve_web_login(in_own_64(WIDE_LIMIT)), "busy")

        for address in claimed:
            webserver._settle_web_login(address, False)
        self.assertEqual(webserver._web_logins_in_flight, {})
        self.assertEqual(webserver._reserve_web_login(in_own_64(WIDE_LIMIT)), "blocked")

    def test_failures_and_claims_together_reach_the_48_limit(self):
        self.fail_across_64s(WIDE_LIMIT - 2)
        for number in (200, 201):
            self.assertEqual(webserver._reserve_web_login(in_own_64(number)), "ok")

        self.assertEqual(webserver._reserve_web_login(in_own_64(202)), "busy")

        webserver._settle_web_login(in_own_64(200), None)
        webserver._settle_web_login(in_own_64(201), None)
        self.assertEqual(webserver._web_logins_in_flight, {})

    def test_a_turned_away_claim_takes_nothing_at_either_count(self):
        address = ONE_NETWORK[0]
        for _ in range(LIMIT):
            self.assertEqual(webserver._reserve_web_login(address), "ok")
        self.assertEqual(webserver._reserve_web_login(address), "busy")

        self.assertEqual(webserver._web_logins_in_flight,
                         {ONE_NETWORK_KEY: LIMIT, WIDE_KEY: LIMIT})
        for _ in range(LIMIT):
            webserver._settle_web_login(address, None)
        self.assertEqual(webserver._web_logins_in_flight, {})

    def slow_claims(self):
        """The in-flight counts, with a short pause on every read: between a
        claim's check and its count other threads then run, so the burst
        tests below need the lock to come out right, not only luck."""
        class SlowCounts(dict):
            def get(self, key, default=None):
                time.sleep(0.002)
                return dict.get(self, key, default)
        counts = SlowCounts()
        for module in (web_login, webserver):
            patcher = mock.patch.object(module, "_web_logins_in_flight", counts)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_parallel_claims_from_many_64s_stop_at_the_48_limit(self):
        self.slow_claims()
        burst = WIDE_LIMIT + 10
        barrier = threading.Barrier(burst)
        answers = []

        def claim(number):
            barrier.wait()
            answers.append(webserver._reserve_web_login(in_own_64(number)))

        threads = [threading.Thread(target=claim, args=(number,), daemon=True)
                   for number in range(burst)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(10)

        self.assertEqual(answers.count("ok"), WIDE_LIMIT)
        self.assertEqual(answers.count("busy"), burst - WIDE_LIMIT)
        self.assertEqual(webserver._web_logins_in_flight[WIDE_KEY], WIDE_LIMIT)

    def test_parallel_claims_from_one_64_stop_at_three(self):
        self.slow_claims()
        burst = 12
        barrier = threading.Barrier(burst)
        answers = []

        def claim(address):
            barrier.wait()
            answers.append(webserver._reserve_web_login(address))

        threads = [threading.Thread(target=claim, args=(ONE_NETWORK[number % 5],),
                                    daemon=True) for number in range(burst)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(10)

        self.assertEqual(answers.count("ok"), LIMIT)
        self.assertEqual(webserver._web_logins_in_flight,
                         {ONE_NETWORK_KEY: LIMIT, WIDE_KEY: LIMIT})

    def test_both_blocks_run_out_after_the_window(self):
        blocked_64 = ONE_NETWORK[0]
        self.fail_across_64s(WIDE_LIMIT - LIMIT)
        for _ in range(LIMIT):
            self.fail_from(blocked_64)
        fresh = in_own_64(WIDE_LIMIT)

        self.clock.now = T0 + WINDOW - 1
        self.assertEqual(webserver._reserve_web_login(fresh), "blocked")
        self.assertEqual(webserver._reserve_web_login(blocked_64), "blocked")

        self.clock.now = T0 + WINDOW
        self.assertFalse(webserver._is_bad_web_ip(fresh))
        self.assertEqual(webserver._reserve_web_login(fresh), "ok")
        webserver._settle_web_login(fresh, None)
        self.assertEqual(webserver._reserve_web_login(blocked_64), "ok")
        webserver._settle_web_login(blocked_64, None)
        self.assertEqual(webserver._web_bad_ips, {})
        self.assertEqual(webserver._web_logins_in_flight, {})

    def test_the_48_block_alone_runs_out_after_the_window(self):
        self.fail_across_64s(WIDE_LIMIT)
        fresh = in_own_64(WIDE_LIMIT)

        self.clock.now = T0 + WINDOW - 1
        self.assertTrue(webserver._is_bad_web_ip(fresh))

        self.clock.now = T0 + WINDOW
        self.assertFalse(webserver._is_bad_web_ip(fresh))
        self.assertNotIn(WIDE_KEY, webserver._web_bad_ips)


class ClientsCarryingAnIPv4AddressAreThatAddress(_PoolCase):

    def test_a_nat64_client_and_a_plain_client_share_one_count(self):
        self.fail_from("64:ff9b::c000:201")
        self.fail_from("64:ff9b::192.0.2.1")
        self.fail_from("192.0.2.1")

        self.assertEqual(webserver._reserve_web_login("192.0.2.1"), "blocked")
        self.assertEqual(webserver._reserve_web_login("64:ff9b::c000:201"), "blocked")
        self.assertEqual(sorted(webserver._web_bad_ips), ["192.0.2.1"])

    def test_nat64_clients_of_different_ipv4_addresses_stay_separate(self):
        for _ in range(LIMIT):
            self.fail_from("64:ff9b::c000:201")

        self.assertEqual(webserver._reserve_web_login("64:ff9b::c000:202"), "ok")
        webserver._settle_web_login("64:ff9b::c000:202", None)


@unittest.skipUnless(webserver.HAVE_FLASK, "Flask not installed; CI installs requirements-web.txt")
class LoginsPostedFromOneNetwork(RouteCase):
    """POST /login through the real app, with the client address set the
    way the server sets it."""

    def setUp(self):
        super().setUp()
        webserver._web_logins_in_flight.clear()
        self.addCleanup(webserver._web_bad_ips.clear)
        self.addCleanup(webserver._web_logins_in_flight.clear)
        self.checked = []
        real_check = webserver._check_web_password

        def counted_check(stored, supplied, wait=None):
            self.checked.append(supplied)
            return real_check(stored, supplied, wait)

        webserver._check_web_password = counted_check
        self.addCleanup(setattr, webserver, "_check_web_password", real_check)

    def post(self, address, password):
        return self.client.post("/login", data={"password": password},
                                environ_base={"REMOTE_ADDR": address})

    def test_three_wrong_passwords_from_a_64_block_a_fourth_address(self):
        for number, address in enumerate(ONE_NETWORK[:LIMIT]):
            resp = self.post(address, "wrong-%d" % number)
            self.assertEqual(resp.status_code, 401)
            self.assertIn(b"Incorrect password", resp.data)
        self.assertEqual(len(self.checked), LIMIT)

        resp = self.post(ONE_NETWORK[LIMIT], PASSWORD)

        self.assertEqual(len(self.checked), LIMIT,
                         "a fourth address in the same /64 had its password checked")
        self.assertEqual(resp.status_code, 401)
        self.assertIn(b"Too many failed attempts", resp.data)
        self.assertEqual(webserver._web_logins_in_flight, {})

    def test_the_next_64_still_logs_in(self):
        for number, address in enumerate(ONE_NETWORK[:LIMIT]):
            self.post(address, "wrong-%d" % number)

        resp = self.post(NEXT_NETWORK, PASSWORD)

        self.assertEqual(resp.status_code, 302)
        self.assertEqual(len(self.checked), LIMIT + 1)

    def test_a_mapped_client_and_a_plain_client_share_one_count(self):
        self.post("::ffff:192.0.2.1", "wrong-1")
        self.post("192.0.2.1", "wrong-2")
        self.post("::ffff:c000:201", "wrong-3")
        self.assertEqual(len(self.checked), LIMIT)

        resp = self.post("192.0.2.1", PASSWORD)

        self.assertEqual(resp.status_code, 401)
        self.assertIn(b"Too many failed attempts", resp.data)
        self.assertEqual(len(self.checked), LIMIT)

    def test_a_right_password_inside_the_64_forgets_its_failures(self):
        """Control: two typos, then the operator gets in from another
        address of the same /64, and the count starts again."""
        self.post(ONE_NETWORK[0], "wrong-1")
        self.post(ONE_NETWORK[1], "wrong-2")

        self.assertEqual(self.post(ONE_NETWORK[2], PASSWORD).status_code, 302)
        self.assertEqual(webserver._web_bad_ips, {})

    def test_thirty_wrong_passwords_across_a_48_block_a_fresh_64(self):
        for number in range(WIDE_LIMIT):
            resp = self.post(in_own_64(number), "wrong-%d" % number)
            self.assertEqual(resp.status_code, 401)
            self.assertIn(b"Incorrect password", resp.data)
        self.assertEqual(len(self.checked), WIDE_LIMIT)

        resp = self.post(in_own_64(WIDE_LIMIT), PASSWORD)

        self.assertEqual(len(self.checked), WIDE_LIMIT,
                         "a fresh /64 in the same /48 had its password checked")
        self.assertEqual(resp.status_code, 401)
        self.assertIn(b"Too many failed attempts", resp.data)

        resp = self.post(OTHER_WIDE_NETWORK, PASSWORD)
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(webserver._web_logins_in_flight, {})

    def test_twenty_nine_wrong_passwords_across_a_48_leave_it_open(self):
        for number in range(WIDE_LIMIT - 1):
            self.post(in_own_64(number), "wrong-%d" % number)

        self.assertEqual(self.post(in_own_64(WIDE_LIMIT), PASSWORD).status_code, 302)

    def test_a_nat64_client_counts_as_its_ipv4_address(self):
        self.post("64:ff9b::c000:201", "wrong-1")
        self.post("64:ff9b::c000:201", "wrong-2")
        self.post("192.0.2.1", "wrong-3")

        resp = self.post("64:ff9b::c000:201", PASSWORD)

        self.assertEqual(resp.status_code, 401)
        self.assertIn(b"Too many failed attempts", resp.data)
        self.assertEqual(len(self.checked), LIMIT)

    def test_a_login_turned_away_while_others_are_checked_says_so(self):
        for address in ONE_NETWORK[:LIMIT]:
            self.assertEqual(webserver._reserve_web_login(address), "ok")
            self.addCleanup(webserver._settle_web_login, address, None)

        resp = self.post(ONE_NETWORK[LIMIT], PASSWORD)

        self.assertEqual(resp.status_code, 429)
        self.assertIn(b"Another login from this address or network is still being checked",
                      resp.data)
        self.assertEqual(self.checked, [])

if __name__ == "__main__":
    unittest.main()
