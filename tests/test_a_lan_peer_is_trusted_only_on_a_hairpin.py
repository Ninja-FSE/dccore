"""A private address reaches the console only on a NAT hairpin.

#881 let the console listener take a connection from any private address,
for the operator behind the same router as the bot: their client advertises
the router's public IP, and their connection arrives from a LAN one. But
wherever the source address is not the real client's - a proxy or container
rewriting it, a LAN or provider network shared with others - any peer with a
private address could reach the port first and take the one listener: the
banner, the password prompts, and the operator's own connect finding the port
gone. Now a private address is taken only when the address the operator
advertised is the bot's own public one - the hairpin itself. The same over a
real socket is in test_a_lan_hairpin_reaches_the_console.
"""

import unittest

from tests import support  # noqa: F401  (path setup)

import adminchat  # noqa: E402

OWN = "203.0.113.50"


class WhoIsTheOperator(unittest.TestCase):
    def test_the_advertised_address_always(self):
        self.assertTrue(adminchat._is_the_operator("198.51.100.7", "198.51.100.7", OWN))

    def test_any_peer_when_nothing_was_advertised(self):
        self.assertTrue(adminchat._is_the_operator("192.168.1.5", None, OWN))

    def test_a_lan_peer_on_the_hairpin(self):
        self.assertTrue(adminchat._is_the_operator("192.168.1.5", OWN, OWN))

    def test_a_lan_peer_when_the_operator_is_somewhere_else(self):
        """The audit's case: the operator advertised their own public IP,
        and a neighbour - or a proxy's own address - reached the port."""
        self.assertFalse(adminchat._is_the_operator("192.168.1.5", "198.51.100.7", OWN))
        self.assertFalse(adminchat._is_the_operator("172.17.0.1", "198.51.100.7", OWN))

    def test_a_public_stranger_never(self):
        self.assertFalse(adminchat._is_the_operator("8.8.8.8", OWN, OWN))

    def test_not_when_the_bot_does_not_know_its_own_address(self):
        self.assertFalse(adminchat._is_the_operator("192.168.1.5", OWN, None))


if __name__ == "__main__":
    unittest.main()
