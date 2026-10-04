"""Stress a running DCCore bot with simulated IRC clients and read what it costs.

WHY THIS EXISTS

The performance audit (#1120) measured the daemon's hot paths on synthetic
data. This measures the whole bot instead, from the outside: simulated clients
connect to a test IRC server, flood it, advertise as file-server bots, churn in
and out of the channel, and quit together, while a probe samples the bot
process (CPU, memory, threads) and the dashboard's response time. The results
of the first run are in #1153.

USE A TEST BOT ON A PRIVATE TEST SERVER. The flood phase gets the simulated
nicks muted and banned, so the bot's ban list and known-bot registry change.
Never point this at a bot on a real network.

Linux only: the bot process is read from /proc and the connection count from
`ss`. Standard library only.

    python scripts/stress_test.py --pid 1234 --server 127.0.0.1:6667 --channel '#somechannel'
    python scripts/stress_test.py --pid 1234 --server 127.0.0.1:6667 --channel '#somechannel' \\
        --test flood,churn --clients 20

THE PHASES (--test takes a comma list; the default is the first five)

  flood      every client sends 15 @find lines in about a second
  adverts    every client posts 12 file-server adverts
  churn      every client PARTs and JOINs the channel 20 times
  quit       all clients QUIT together, then rejoin under an alt nick
  dashboard  20 threads hit /api/status for 20 s without logging in, so the
             server answers 401: this measures the HTTP layer, not the pages
  storm      30 paced adverts per client; run it BEFORE any flood on the same
             nicks, because the flood gets those nicks banned and a banned
             sender's adverts are dropped
  autograb   6 bots with random nicks advertise every 45 s for --minutes and
             the automatic list requests the bot sends back are listed. The
             simulated bots never answer, so replies and retries are not
             exercised.

READING THE NUMBERS (measured in-process for #1153, through the real read loop)

  flood      The read loop costs about 0.2-0.5 ms per line, whichever kind:
             a search it starts, the line that mutes, a line from a banned
             nick (which costs no search at all). What grows the per-line
             figure is the searches. Up to MAX_REQUESTS @find lines per nick
             each start one, and they run one at a time (another is refused
             while one runs), so while unmuted clients keep searching, one
             core scans the bot's list back to back. A test server paces
             each client after a short burst, so the flood reaches the bot
             over several seconds rather than one, and the CPU per line then
             follows the size of the bot's list: about 0.8 ms with no list,
             2 ms at 10,000 rows and 3.7 ms at 40,000 when the 600 lines
             arrive over 6 s. Confirmed on a Linux test bot with a list of
             about 64,000 rows: 30% CPU on average through the flood, and 3%
             with its lists folder emptied (v1.14.0: 63-91% and 3%).
  storm      The registry keeps about 0.6 KB per advertising bot, at most
             irc.KNOWN_BOTS_MAX of them, and the read loop's first storm
             imports three small modules. Memory that grows by tens of MB
             is not the registry; compare a run with MALLOC_ARENA_MAX=2 in
             the bot's environment to see how much is the C allocator. (On a
             2 GB Linux test container the growth did not appear at all,
             38 to 39 MB with or without it, so only a machine that shows
             the growth can tell.) "known bots after" is read once the bot
             has written its registry: it does so at most every 30 s, and
             only when an advert arrives, so the storm sends one more.
"""

import argparse
import json
import os
import random
import re
import socket
import statistics
import string
import sys
import threading
import time
import urllib.request

# Read where they exist, so the file still imports elsewhere (the suite reads
# it on Windows too); main() refuses to run without /proc.
TICK = os.sysconf("SC_CLK_TCK") if hasattr(os, "sysconf") else 100
PAGE = os.sysconf("SC_PAGE_SIZE") if hasattr(os, "sysconf") else 4096
DEFAULT_PHASES = "flood,adverts,churn,quit,dashboard"
ALL_PHASES = DEFAULT_PHASES.split(",") + ["storm", "autograb"]


def nick_for(i, tag="Str"):
    """A stable, letters-only nick, since many servers refuse digits in some places."""
    return tag + "".join(chr(97 + int(d)) for d in f"{i:03d}")


class Client(object):
    """One simulated IRC client: registers, answers PING, keeps what the bot says to it."""

    def __init__(self, host, port, nick):
        self.nick = nick
        self.received = 0
        self.private = []
        self.dead = None
        self.alive = True
        self.ready = False
        self.sock = socket.create_connection((host, port), timeout=10)
        self.sock.settimeout(0.5)
        self.send(f"NICK {nick}")
        self.send(f"USER st 0 * :st {nick}")
        threading.Thread(target=self._reader, daemon=True).start()

    def send(self, line):
        try:
            self.sock.sendall((line + "\r\n").encode())
        except OSError:
            self.alive = False

    def _reader(self):
        buf = b""
        while self.alive:
            try:
                data = self.sock.recv(65536)
            except socket.timeout:
                continue
            except OSError:
                break
            if not data:
                break
            buf += data
            while b"\r\n" in buf:
                raw, buf = buf.split(b"\r\n", 1)
                line = raw.decode(errors="replace")
                self.received += 1
                if line.startswith("PING"):
                    self.send("PONG " + line.split(" ", 1)[1])
                elif " 376 " in line or " 422 " in line:
                    self.ready = True
                elif re.match(r"^:\S+ (ERROR|4\d\d) ", line) or line.startswith("ERROR"):
                    self.dead = line
                elif f" PRIVMSG {self.nick} " in line or f" NOTICE {self.nick} " in line:
                    self.private.append((time.time(), line))
        self.alive = False

    def close(self):
        self.send("QUIT :stress done")
        time.sleep(0.05)
        self.alive = False
        try:
            self.sock.close()
        except OSError:
            pass


class Bot(object):
    """What is read from the bot under test."""

    def __init__(self, pid, dashboard, log_path, data_dir):
        self.pid = pid
        self.dashboard = dashboard.rstrip("/")
        self.log_path = log_path
        self.data_dir = data_dir

    def cpu_seconds(self):
        fields = open(f"/proc/{self.pid}/stat").read().rsplit(")", 1)[1].split()
        return (int(fields[11]) + int(fields[12])) / TICK

    def rss_mb(self):
        return int(open(f"/proc/{self.pid}/statm").read().split()[1]) * PAGE / 1e6

    def threads(self):
        return len(os.listdir(f"/proc/{self.pid}/task"))

    def log_size(self):
        return os.path.getsize(self.log_path) if self.log_path else 0

    def log_since(self, pos):
        if not self.log_path:
            return ""
        with open(self.log_path, "rb") as handle:
            handle.seek(pos)
            return handle.read().decode(errors="replace")

    def known_bots(self):
        if not self.data_dir:
            return -1
        try:
            with open(os.path.join(self.data_dir, "known_bots.json")) as handle:
                data = json.load(handle)
            return len(data)
        except (OSError, ValueError):
            return -1


def connected(host):
    pattern = host.replace(".", "\\.")
    return os.popen(f"ss -tn state established | grep -c '{pattern}'").read().strip()


class Probe(threading.Thread):
    """Samples the bot's CPU once a second and the dashboard's (login redirect) latency every 150 ms."""

    def __init__(self, bot):
        super().__init__(daemon=True)
        self.bot = bot
        self.stop = False
        self.latency = []
        self.cpu = []

    def run(self):
        last_cpu, last_time = self.bot.cpu_seconds(), time.time()
        next_sample = 0
        while not self.stop:
            started = time.time()
            try:
                urllib.request.urlopen(self.bot.dashboard + "/", timeout=5).read(1)
            except Exception:
                pass
            self.latency.append(time.time() - started)
            if time.time() >= next_sample:
                now_cpu, now = self.bot.cpu_seconds(), time.time()
                self.cpu.append(100 * (now_cpu - last_cpu) / (now - last_time))
                last_cpu, last_time = now_cpu, now
                next_sample = now + 1
            time.sleep(0.15)


def phase(bot, host, name, work, settle=5):
    """Run `work` under a Probe and print one line of what it cost."""
    probe = Probe(bot)
    log_pos = bot.log_size()
    rss0, threads0 = bot.rss_mb(), bot.threads()
    probe.start()
    started = time.time()
    work()
    time.sleep(settle)
    probe.stop = True
    probe.join()
    elapsed = time.time() - started
    log = bot.log_since(log_pos)
    errors = [line for line in log.splitlines()
              if re.search(r"Traceback|Error|Exception|CRASH", line)]
    latency = sorted(probe.latency)
    cpu = probe.cpu
    print(f"[{name}] {elapsed:.0f}s | bot CPU avg {statistics.mean(cpu) if cpu else 0:.0f}% "
          f"max {max(cpu) if cpu else 0:.0f}% | RSS {rss0:.0f}->{bot.rss_mb():.0f} MB | "
          f"threads {threads0}->{bot.threads()} | dashboard GET / p50 "
          f"{1000 * latency[len(latency) // 2]:.0f}ms max {1000 * latency[-1]:.0f}ms "
          f"({len(latency)} probes) | new log lines {len(log.splitlines())}, "
          f"errors {len(errors)} | connected to the server: {connected(host)}", flush=True)
    for line in errors[:3]:
        print("    ", line[:200])
    return log


def spawn(host, port, nicks, gap=0.12):
    clients = []
    for nick in nicks:
        try:
            clients.append(Client(host, port, nick))
        except OSError as err:
            print("connect failed", nick, err)
            break
        time.sleep(gap)
    deadline = time.time() + 15
    while time.time() < deadline and not all(c.ready or c.dead for c in clients):
        time.sleep(0.2)
    return [c for c in clients if c.ready and not c.dead]


def on_all(clients, one):
    threads = [threading.Thread(target=one, args=(c,)) for c in clients]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()


def run_flood(bot, args, host, port, clients):
    def one(c):
        for k in range(15):
            c.send(f"PRIVMSG {args.channel} :@find stress test number {k}")
            time.sleep(0.05)
    phase(bot, host, f"flood ({len(clients)} clients x 15 @find)",
          lambda: on_all(clients, one))


def run_adverts(bot, args, host, port, clients):
    def one(c):
        for k in range(12):
            c.send(f"PRIVMSG {args.channel} :Type: @{c.nick} For My List Of: "
                   f"{1000 + k * 37:,} Files List: Sep {k + 1}th Slots: 3/5 Queue: 0/20")
            time.sleep(0.1)
    phase(bot, host, f"adverts ({len(clients)} fake bots x 12 adverts)",
          lambda: on_all(clients, one), settle=75)
    print("    private messages the fake bots got from the bot (list grabs etc.):",
          sum(len(c.private) for c in clients))


def run_storm(bot, args, host, port, clients):
    print("    known bots before:", bot.known_bots())

    def one(c):
        for k in range(30):
            c.send(f"PRIVMSG {args.channel} :Type: @{c.nick} For My List Of: "
                   f"{1000 + k * 37:,} Files List: Sep {k % 28 + 1}th Slots: 3/5 Queue: 0/20")
            time.sleep(0.3)
    phase(bot, host, f"storm ({len(clients)} fake bots x 30 paced adverts)",
          lambda: on_all(clients, one), settle=90)
    # The bot writes known_bots.json at most once per KNOWN_BOTS_FLUSH_SECONDS
    # (30 s), and only when an advert arrives, so the storm's last adverts are
    # still only in its memory. One more advert from a bot it already knows,
    # well past that interval, writes them without adding a bot.
    nudger = next((c for c in clients if c.alive), None)
    if nudger is not None:
        nudger.send(f"PRIVMSG {args.channel} :Type: @{nudger.nick} For My List Of: "
                    f"1,000 Files List: Sep 1th Slots: 3/5 Queue: 0/20")
        time.sleep(3)
    print("    known bots after:", bot.known_bots())


def run_churn(bot, args, host, port, clients):
    def one(c):
        for _ in range(20):
            c.send(f"PART {args.channel}")
            time.sleep(0.04)
            c.send(f"JOIN {args.channel}")
            time.sleep(0.04)
    phase(bot, host, f"churn ({len(clients)} clients x 20 cycles)",
          lambda: on_all(clients, one))


def run_quit(bot, args, host, port, clients):
    rejoined = []

    def work():
        for c in clients:
            c.send("QUIT :Ping timeout: simulated")
        time.sleep(1.5)
        for c in clients:
            c.close()
        rejoined.extend(spawn(host, port, [nick_for(i) + "_" for i in range(args.clients)],
                              gap=0.1))
        for c in rejoined:
            c.send(f"JOIN {args.channel}")
        time.sleep(2)
    phase(bot, host, "quit and alt-nick rejoin", work)
    clients[:] = rejoined


def run_dashboard(bot, args, host, port, clients):
    latency = []

    def work():
        stop = time.time() + 20

        def hit():
            while time.time() < stop:
                started = time.time()
                try:
                    urllib.request.urlopen(bot.dashboard + "/api/status", timeout=10).read()
                except Exception:
                    pass
                latency.append(time.time() - started)
        threads = [threading.Thread(target=hit) for _ in range(20)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
    phase(bot, host, "dashboard (20 threads, 401 path)", work, settle=0)
    latency.sort()
    if latency:
        print(f"    {len(latency)} requests in 20 s, p50 {1000 * latency[len(latency) // 2]:.0f}ms "
              f"p99 {1000 * latency[int(len(latency) * .99)]:.0f}ms "
              f"max {1000 * latency[-1]:.0f}ms")


def run_autograb(args, host, port):
    rnd = random.SystemRandom()
    nicks = []
    while len(nicks) < 6:
        nick = rnd.choice(string.ascii_uppercase) + "".join(
            rnd.choice(string.ascii_lowercase) for _ in range(rnd.randint(6, 9)))
        if nick not in nicks:
            nicks.append(nick)
    bots = spawn(host, port, nicks, gap=0.3)
    for c in bots:
        c.send(f"JOIN {args.channel}")
    time.sleep(2)
    started = time.time()
    print(f"autograb: {len(bots)} fake bots advertising every 45 s for {args.minutes} min",
          flush=True)
    k = 0
    while time.time() - started < args.minutes * 60:
        for c in bots:
            c.send(f"PRIVMSG {args.channel} :Type: @{c.nick} For My List Of: "
                   f"{2000 + k * 11:,} Files List: Oct 3th Slots: 3/5 Queue: 0/20")
            time.sleep(0.2)
        k += 1
        time.sleep(45)
    for number, c in enumerate(bots, 1):
        print(f"fake bot #{number}: {len(c.private)} message(s) received")
        for when, line in c.private[:4]:
            shown = re.sub(r":[^ ]+![^ ]+ ", ":<x> ", line)[:160].replace(c.nick, "<bot>")
            print(f"   +{when - started:.0f}s  {shown}")
    for c in bots:
        c.close()


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Stress a running DCCore test bot with simulated IRC clients.")
    parser.add_argument("--pid", type=int, required=True, help="process id of the bot under test")
    parser.add_argument("--server", default="127.0.0.1:6667",
                        help="host:port of the TEST IRC server the bot is on")
    parser.add_argument("--channel", required=True, help="channel the bot is in, e.g. '#somechannel'")
    parser.add_argument("--clients", type=int, default=40, help="simulated clients (default 40)")
    parser.add_argument("--dashboard", default="http://127.0.0.1:8420",
                        help="the bot's dashboard URL (default http://127.0.0.1:8420)")
    parser.add_argument("--log", default="", help="the bot's dccore.log, to count errors in it")
    parser.add_argument("--data-dir", default="",
                        help="the bot's data/ folder, to count known bots")
    parser.add_argument("--test", default=DEFAULT_PHASES,
                        help="comma list of: " + ", ".join(ALL_PHASES))
    parser.add_argument("--minutes", type=int, default=12, help="length of the autograb phase")
    args = parser.parse_args(argv)

    wanted = [name.strip() for name in args.test.split(",") if name.strip()]
    unknown = [name for name in wanted if name not in ALL_PHASES]
    if unknown:
        parser.error("unknown phase: " + ", ".join(unknown))
    host, _, port = args.server.rpartition(":")
    if not host or not port.isdigit():
        parser.error("--server must be host:port")
    port = int(port)
    if not os.path.isdir(f"/proc/{args.pid}"):
        parser.error(f"no /proc/{args.pid}: this reads the bot from /proc, so it runs on "
                     f"Linux only, next to a running test bot")
    bot = Bot(args.pid, args.dashboard, args.log, args.data_dir)

    print(f"bot pid {args.pid} | baseline RSS {bot.rss_mb():.0f} MB, threads {bot.threads()}")
    if "autograb" in wanted:
        run_autograb(args, host, port)
        wanted.remove("autograb")
    if not wanted:
        return 0

    clients = spawn(host, port, [nick_for(i) for i in range(args.clients)])
    for c in clients:
        c.send(f"JOIN {args.channel}")
    time.sleep(2)
    print(f"clients ready: {len(clients)} of {args.clients}", flush=True)
    runners = {"flood": run_flood, "adverts": run_adverts, "storm": run_storm,
               "churn": run_churn, "quit": run_quit, "dashboard": run_dashboard}
    # The storm first: a flood bans the nicks, and a banned sender's adverts are dropped.
    order = sorted(wanted, key=lambda name: 0 if name == "storm" else 1)
    for name in order:
        runners[name](bot, args, host, port, clients)
    for c in clients:
        c.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
