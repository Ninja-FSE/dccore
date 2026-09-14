# stats_mgr.py - Size formatting, transfer speed and uptime figures
import time

import defaults as config
import db
import runtime

# The bot's start time is kept here.
#
# Guarded, because !rehash reloads this module and importlib.reload re-executes
# the body against the SAME module dict - a bare assignment reset the uptime to
# zero on every rehash. The name already being bound is exactly what tells us
# this is a reload rather than the first import, so the original value stands.
try:
    start_time
except NameError:
    start_time = time.time()

def load_stats():
    """Read the 7-column format from the configured path, via the db module."""
    return db.load_advanced_stats()

def get_total_sent():
    """The total number of files sent, as a plain integer (index 0)."""
    stats = load_stats()
    try:
        # If the store returns a nested list [[319]], take the innermost value
        if isinstance(stats, list) and len(stats) > 0:
            val = stats[0]
            if isinstance(val, list):
                val = val[0]
            return int(str(val).strip())
    except:
        pass
    return 0

def get_total_sent_bytes():
    """The total number of bytes sent, as a plain integer (index 1)."""
    stats = load_stats()
    try:
        # If the store returns a nested list [[319], [9303203296]], take the innermost value
        if isinstance(stats, list) and len(stats) > 1:
            val = stats[1]
            if isinstance(val, list):
                val = val[0]
            return int(str(val).strip())
    except:
        pass
    return 0

def format_size_human(bytes_size):
    """Turn raw bytes into a readable size (e.g. 361.2GB)."""
    try:
        bytes_size = float(bytes_size)
    except:
        return "0B"
    for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
        if bytes_size < 1024.0:
            return f"{bytes_size:.1f}{unit}"
        bytes_size /= 1024.0
    return f"{bytes_size:.1f}PB"

def format_speed(bytes_per_sec):
    """Turn raw bytes per second into a readable rate (e.g. 4.5MB/s)."""
    if bytes_per_sec == 0:
        return "0k/s"
    kb = bytes_per_sec / 1024
    if kb < 1024:
        return f"{kb:.1f}k/s"
    mb = kb / 1024
    return f"{mb:.2f}MB/s"

# How long a completed transfer must have run before its measured rate is
# allowed to stand as a record.
#
# dcc.py floors the measured duration at 0.1s and derives it from a start_time
# that is not guaranteed to be bound - when it is not, the duration comes out
# as 0, is floored, and the rate reads as ten times the file size however long
# the send really took. A record is permanent and public: one bad sample and
# the advert publishes a number the bot can never beat, for good. Requiring a
# real second of transfer throws away nothing anyone would recognise as a
# record and closes that off.
MIN_RECORD_SECONDS = 1.0


def speed_is_measurable(duration):
    """Whether a transfer of this duration has a rate worth reporting.

    sendall() returns once the bytes are in the KERNEL, not once the peer has
    them. For a file LARGER than the socket send buffer the two converge - the
    kernel blocks once the buffer is full, so the send paces itself against the
    network and the clock measures something real. For a file that fits inside
    the buffer they do not converge at all: the whole thing is handed over in
    one go and the clock measures a memory copy.

    From the beta, on a list zip: "Sent ... [138.63MB/s] this seems to high to
    be true". It was. Raising the default send buffer to 4 MB widened the
    window of files this applies to from under 1 MB to under 4 MB, which is
    most list archives and many single tracks.

    MIN_RECORD_SECONDS is the floor the speed RECORD has always applied, for
    exactly this reason. This is that same judgement, made available to
    everything that reports a rate rather than only to the thing that stores
    one - because a number too unreliable to keep is too unreliable to say.
    """
    try:
        return duration is not None and float(duration) >= MIN_RECORD_SECONDS
    except (TypeError, ValueError):
        return False


def update_speed_record(bytes_per_sec, duration=None):
    """Store `bytes_per_sec` if it beats the saved record. Returns the record
    in force afterwards, whether or not this call changed it.

    Reads and writes through db, so the atomic write and the disk lock are the
    same ones every other persisted counter uses.

    Refuses a sample that is not a positive number, and - when a duration is
    given - one measured over less than MIN_RECORD_SECONDS. Refusing is silent
    on purpose: a transfer too short to time is the ordinary case for a small
    file, not something worth a line in the log on every send.
    """
    current = db.get_speed_record()

    try:
        speed = int(bytes_per_sec)
    except (TypeError, ValueError):
        return current

    if speed <= 0:
        return current
    if duration is not None and duration < MIN_RECORD_SECONDS:
        return current
    # The compare and the write happen under ONE lock in db, not here. This
    # used to read the record, compare, and then save - three steps with two
    # separate lock acquisitions and the decision in between. MAX_DCC_SLOTS
    # transfers finish concurrently, so two could both read the old record,
    # both decide they had beaten it, and the SLOWER one save last: a 5 MB/s
    # record permanently replaced by a 1.2 MB/s one, with nothing anywhere to
    # recompute it.
    #
    # The cheap rejections above stay here, because they are about whether the
    # sample is worth offering at all and do not touch the file.
    return db.raise_speed_record_to(speed)


# The shortest window a sample may be measured over, and so also the most often
# one is taken. Two things want this number to be the same:
#
#   * A rate needs a window. bytes_sent is a LIFETIME counter, so the figure is
#     bytes moved since the previous observation over the time since it - and a
#     window of a few milliseconds either reports nothing or, if a pass happens
#     to straddle a second, something wildly inflated.
#   * More than one caller now wants this number. Sampling consumes the window:
#     if the advert and the dashboard each took their own, each would see part
#     of the movement and both would be wrong. Caching the result for the same
#     second means any number of callers can ask as often as they like and all
#     get the same, correct answer.
MIN_SAMPLE_SECONDS = 1.0


def live_speed(now=None):
    """Aggregate bytes/sec across the transfers currently sending.

    The SUM of what every sending transfer is moving, which is what "live
    speed" means and what this docstring always said. The implementation
    disagreed with it: it summed the per-transfer rates and then divided by
    the number of contributors, so a bot with three slots each moving 2 MB/s
    advertised 2.0MB/s against 6 MB/s of real outbound traffic - understating
    itself in a public channel by a factor of the slots in use, worst exactly
    when it was busiest.

    Cached for MIN_SAMPLE_SECONDS: a call inside that window returns the last
    figure rather than taking a second sample that would measure a fraction of
    the movement and report a fraction of the speed.

    The value is also left in runtime.live_speed_bps for readers that must not
    import the daemon - webserver.py in particular, which imports `list` lazily
    for exactly that reason and has a test pinning that it stays light.
    """
    now = time.time() if now is None else now

    if now - runtime.live_speed_sampled_at < MIN_SAMPLE_SECONDS:
        return runtime.live_speed_bps

    # Imported here rather than at module scope: dcc imports stats_mgr itself,
    # and this module is small enough to be pulled in by things that must not
    # drag the daemon along behind it.
    import dcc

    total = 0
    contributors = 0

    # dcc.queue_lock, not config.queue_lock - every append and removal on
    # config.active_transfers throughout dcc.py holds dcc's own module-level
    # lock. They are different objects, and guarding the same list with two of
    # them is guarding it with neither.
    with dcc.queue_lock:
        for tx in config.active_transfers:
            sent = tx.get("bytes_sent", 0)
            previous_bytes = tx.get("_speed_bytes")
            previous_time = tx.get("_speed_time")
            tx["_speed_bytes"] = sent
            tx["_speed_time"] = now

            if previous_time is None:
                continue                      # first sighting: no window yet

            window = now - previous_time
            if window < MIN_SAMPLE_SECONDS:
                continue

            moved = sent - previous_bytes
            if moved > 0:
                total += int(moved / window)
                contributors += 1

    # THE SUM, not a mean. "Live speed" is what the bot is moving right now,
    # so three slots at 2 MB/s each is 6 MB/s - which is what the channel
    # advert's "Speed:" has always claimed to publish and what the dashboard's
    # "Speed now" tile means.
    #
    # It used to divide by the number of contributing transfers, and the
    # comment here defended that as protecting the MEAN from a transfer with
    # no sample window yet. With a sum that concern disappears on its own: a
    # skipped transfer contributes nothing, which is exactly right, rather
    # than dragging an average down. `contributors` is kept only to tell "no
    # transfers moved anything" apart from "they moved zero bytes".
    value = int(total) if contributors else 0

    runtime.live_speed_bps = value
    runtime.live_speed_sampled_at = now
    return value


def get_uptime_seconds():
    """Uptime in seconds, for the channel advert."""
    return int(time.time() - start_time)
