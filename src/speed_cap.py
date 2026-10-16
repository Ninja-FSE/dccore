"""The most the bot's transfers may use of the line, per direction (#1352).

MAX_UPLOAD_KB caps every DCC send together - what the bot serves to people -
and MAX_DOWNLOAD_KB every fetch together: list fetches, AutoGet's grabs and
files and folders fetched from other bots. In KB/s, 0 for no limit, which is
the default; nothing is slower until an operator asks for it.

ONE PACE PER DIRECTION, SHARED. Each chunk a transfer moves reserves its
share of the line before it goes - the time that many bytes take at the cap
- and waits for its turn. Every transfer in that direction reserves from the
same clock, so three sends under a 600 KB/s cap move about 200 KB/s each,
and the other two speed up the moment one ends. An idle direction saves up
nothing: the first chunk after a quiet spell goes at once, and the cap holds
from the second.

SMALL CHUNKS UNDER A CAP. A 64 KB block is half a second of a 128 KB/s line,
and a transfer moving in steps that size reads as a stutter on the dashboard.
Under a cap a chunk is a tenth of a second of line, never under MIN_CHUNK and
never over the transfer's own block.

READ PER CHUNK, so a changed cap applies to transfers already running. A cap
changed mid-reservation is not chased: a chunk waits out the reservation it
made, which is a tenth of a second of line at the old cap, and the next one
is reserved at the new pace. During a !rehash every setting reads its shipped
default for a few milliseconds (see runtime.config_reload_lock), so a chunk
reserved in that window goes uncapped - one chunk per transfer, once per
rehash, which is not worth a lock in the hottest loop the bot has.

The clock lives in runtime.py, not here, for the reason every lock there
does: a module reloaded by !rehash would start a second clock beside the one
the running transfers are still reserving from.
"""

import time

import defaults as config
import runtime

UPLOAD = "MAX_UPLOAD_KB"
DOWNLOAD = "MAX_DOWNLOAD_KB"

# A chunk under a cap is this many seconds of line...
CHUNK_SECONDS = 0.1
# ...but never smaller than this, in bytes: a 1 KB/s cap still moves 4 KB
# chunks rather than a stream of tiny ones.
MIN_CHUNK = 4096


def rate(direction):
    """The cap for `direction` (UPLOAD or DOWNLOAD) in bytes per second, or 0
    for no limit. A value that is not a positive number is no limit."""
    try:
        kb = float(getattr(config, direction, 0) or 0)
    except (TypeError, ValueError):
        return 0.0
    return kb * 1024 if kb > 0 else 0.0


def chunk_size(direction, block):
    """How many bytes a transfer moving `block` at a time should move next:
    `block` with no cap, otherwise CHUNK_SECONDS of line within
    MIN_CHUNK..block."""
    bytes_per_second = rate(direction)
    if not bytes_per_second:
        return block
    return min(block, max(MIN_CHUNK, int(bytes_per_second * CHUNK_SECONDS)))


def take(direction, nbytes, clock=time.monotonic, sleep=time.sleep):
    """Reserve `nbytes` of `direction`'s line and wait for their turn.

    Returns the seconds waited, 0.0 with no cap. A sender calls it before a
    chunk goes out; a receiver after one has come in, which paces the next
    recv() and, through TCP's window, the sender at the far end.
    """
    bytes_per_second = rate(direction)
    if not bytes_per_second or nbytes <= 0:
        return 0.0
    with runtime.speed_cap_lock:
        now = clock()
        pace, free_at = runtime.speed_cap_clocks.get(direction, (None, now))
        # A new cap starts a new clock: reservations made at the old pace say
        # nothing about the new one.
        if pace != bytes_per_second or free_at < now:
            free_at = now
        runtime.speed_cap_clocks[direction] = (bytes_per_second, free_at + nbytes / bytes_per_second)
    wait = free_at - now
    if wait <= 0:
        return 0.0
    sleep(wait)
    return wait
