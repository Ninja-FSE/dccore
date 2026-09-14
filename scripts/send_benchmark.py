"""Measure DCCore's own send loop, with the network taken out of it.

WHY THIS EXISTS

"It feels slower than mIRC" is not a number, and the two obvious explanations
pull in opposite directions: either this loop is slow, or the network path is.
Nothing in the daemon can tell them apart, because every real transfer has both
in it.

So this runs the EXACT shape of dcc.start_dcc_send()'s inner loop - the same
read size, the same per-chunk bookkeeping, the same socket options - over a
loopback socket. Loopback has no bandwidth limit worth the name and a
round-trip measured in microseconds, so whatever comes out is close to the
ceiling this code can reach on this machine.

READ IT LIKE THIS

  * A loopback result FAR above your real transfers - say 1 GB/s here against
    30 MB/s to a peer - means the loop is not what is limiting you. The link,
    the peer, or the path between you is.
  * A loopback result CLOSE to your real transfers means the opposite, and the
    per-chunk work in that loop is worth attacking.

It deliberately does not touch the queue, the IRC socket, or any real file: it
is a measurement of one loop, not a test of the daemon.

    python scripts/send_benchmark.py
    python scripts/send_benchmark.py --size 512 --blocks 4096,65536,131072
"""

import argparse
import os
import socket
import sys
import threading
import time

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)


def _drain(sock, done):
    """Receive and throw away, as fast as the kernel will hand it over.

    The receiver is deliberately as cheap as it can be: anything it spends
    time on would be counted against the sender, and the sender is what is
    being measured.
    """
    try:
        while True:
            if not sock.recv(1 << 20):
                break
    except OSError:
        pass
    finally:
        done.set()


def measure(block_size, total_bytes, mimic_bookkeeping=True):
    """Send `total_bytes` over loopback in `block_size` chunks. Returns MB/s.

    `mimic_bookkeeping` reproduces the per-chunk work the real loop does -
    the linear scan of active_transfers, the lowercase comparison, the
    sys.modules lookup. Running it both ways is what shows whether that
    bookkeeping costs anything at these speeds, rather than assuming either
    way.
    """
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]

    done = threading.Event()
    received = {}

    def accept_and_drain():
        conn, _addr = listener.accept()
        received["conn"] = conn
        _drain(conn, done)

    receiver = threading.Thread(target=accept_and_drain, daemon=True)
    receiver.start()

    sender = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sender.connect(("127.0.0.1", port))
    # The same options the real transfer sets, so this measures that socket
    # rather than a friendlier one.
    sender.settimeout(60.0)
    sender.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    sender.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)

    chunk = b"\0" * block_size
    # Stand-ins for the real loop's per-chunk work.
    active_transfers = [{"user": "SomeUser", "bytes_sent": 0}]
    user = "someuser"
    sent = 0

    start = time.time()
    try:
        while sent < total_bytes:
            sender.sendall(chunk)
            sent += block_size
            if mimic_bookkeeping:
                for tx in active_transfers:
                    if tx["user"].lower() == user.lower():
                        tx["bytes_sent"] += block_size
                module = sys.modules.get("oserve")
                if module:
                    pass
    finally:
        elapsed = time.time() - start
        sender.close()
        done.wait(timeout=5.0)
        conn = received.get("conn")
        if conn:
            conn.close()
        listener.close()

    return (sent / (1024.0 * 1024.0)) / elapsed if elapsed > 0 else 0.0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--size", type=int, default=256,
                        help="megabytes to push per run (default 256)")
    parser.add_argument("--blocks", default="4096,8192,16384,32768,65536,131072",
                        help="comma-separated block sizes to try")
    args = parser.parse_args()

    total = args.size * 1024 * 1024
    try:
        blocks = [int(b) for b in args.blocks.split(",") if b.strip()]
    except ValueError:
        print("--blocks wants comma-separated numbers, e.g. 65536,131072")
        return 1

    print(f"Sending {args.size} MB over loopback at each block size.")
    print("Loopback has no bandwidth limit worth the name, so this is close to")
    print("the ceiling this loop can reach on this machine.")
    print("")
    print(f"  {'block':>8}  {'MB/s':>9}  {'MB/s (no bookkeeping)':>22}")
    print(f"  {'-' * 8}  {'-' * 9}  {'-' * 22}")

    for block in blocks:
        with_work = measure(block, total, mimic_bookkeeping=True)
        without = measure(block, total, mimic_bookkeeping=False)
        print(f"  {block:>8}  {with_work:>9.1f}  {without:>22.1f}")

    print("")
    print("If these are far above your real transfers, the loop is not what is")
    print("limiting you - the link, the peer, or the path between you is. If")
    print("they are close to them, the per-chunk work is worth attacking.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
