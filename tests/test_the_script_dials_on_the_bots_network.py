"""The script never recorded which network the bot lives on (audit M59,
#661).

`dcc chat <bot>` ran in whatever connection fired it - on CONNECT, JOIN,
401 and CHATCLOSE the event's own connection, on /dccore connect the
active window's - and the retry timer is one global name. On a client on
two networks the CTCP went to the wrong one: a 401 there, a retry loop
stuck on it ("X is not online" every two minutes while the bot was up),
and every reconnect of the other network saying "already open".

The network is kept from the moment the operator types /dccore connect or
/dccore pair (that connection IS the bot's) or from the bot's own JOIN;
dccore.connect finds that network's connection id and moves itself onto it
with /scid; the CONNECT and JOIN triggers fire only there. mIRC is not on
this machine, so this reads the script (as every test of it does) for the
shape the semantics rest on.
"""

import io
import os
import re
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)


def script():
    with io.open(os.path.join(REPO_ROOT, "scripts", "mirc", "dccore.mrc"),
                 encoding="ascii", newline="") as handle:
        return handle.read().replace("\r\n", "\n")


def alias(name):
    text = script()
    start = text.index("alias %s {" % name)
    return text[start:].split("\n}\n", 1)[0]


def event(name):
    text = script()
    start = text.index(name)
    return text[start:].split("\n}\n", 1)[0]


class TheNetworkIsRecorded(unittest.TestCase):

    def test_when_the_operator_connects_or_pairs_by_hand(self):
        main = alias("dccore")
        for cmd in ("connect", "pair"):
            with self.subTest(cmd=cmd):
                branch = main.split("if (%%cmd == %s) {" % cmd, 1)[1].split("\n  }", 1)[0]
                self.assertIn("dccore.remember.net", branch)

    def test_as_the_network_name_with_the_server_as_the_fallback(self):
        remember = alias("dccore.remember.net")

        self.assertIn("dccore.set net $iif($network,$network,$server)", remember)
        self.assertIn("if ($server)", remember, "nothing to remember while disconnected")

    def test_from_the_bots_own_join_when_nothing_is_recorded_yet(self):
        join = event("on *:JOIN:#: {")

        self.assertIn("if ($dccore.opt(net) == $null) { dccore.remember.net }", join)

    def test_and_forgotten_on_unpair(self):
        unpair = alias("dccore").split("if (%cmd == unpair) {", 1)[1].split("\n  }", 1)[0]

        self.assertIn("dccore.forget net", unpair)


class EveryDialGoesToThatNetwork(unittest.TestCase):

    def test_connect_moves_itself_onto_the_bots_connection(self):
        connect = alias("dccore.connect")
        move = connect.index("if (%cid != $cid) { scid %cid dccore.connect $1- | return }")
        dial = connect.index("dcc chat $dccore.bot")

        self.assertLess(move, dial, "the dial runs before the connection is chosen")
        self.assertIn("var %cid = $dccore.cid", connect[:move])

    def test_and_says_so_when_that_network_is_not_connected(self):
        connect = alias("dccore.connect")

        self.assertIn("if (%cid == $null) { dccore.sys Not connected to $dccore.opt(net)", connect)

    def test_the_connection_is_found_by_network_name_across_every_connection(self):
        cid = alias("dccore.cid")

        self.assertIn("while (%i <= $scon(0))", cid)
        self.assertIn("$scon(%i).network == %net", cid)
        self.assertIn("return $scon(%i).cid", cid)
        self.assertIn("if (%net == $null) { return $cid }", cid, "nothing recorded: this connection, as before")

    def test_the_connect_and_join_triggers_fire_only_there(self):
        for name in ("on *:CONNECT: {", "on *:JOIN:#: {"):
            with self.subTest(event=name):
                body = event(name)
                self.assertIn("($dccore.here)", body)

    def test_here_means_the_recorded_network_or_none_recorded(self):
        here = alias("dccore.here")

        self.assertIn("$dccore.opt(net) == $null,$true", here)
        self.assertIn("$network == $dccore.opt(net),$true", here)


class EverythingItUsesIsMirc6(unittest.TestCase):
    """/scid, $scon(N).network and $cid are mIRC 6.0's multi-server set."""

    def test_no_identifier_newer_than_6(self):
        used = set(re.findall(r"\$(?:scon|scid|cid|network|server)\b", alias("dccore.cid") + alias("dccore.here")))

        self.assertTrue(used <= {"$scon", "$cid", "$network", "$server"}, used)


if __name__ == "__main__":
    unittest.main()
