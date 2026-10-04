"""Duration and quality for the list's file rows (#567).

    !DCCore Artist - Album - 01 - Track.mp3  ::INFO:: 10.3MB 4m31s 320/44.1/JS
    !DCCore Artist - Album - 02 - Track.flac  ::INFO:: 16.7MB 2m5s 1115/44.1/S

After the size: the duration, then bitrate kbps / sample rate kHz / channels.
The spelling is the one other servers' lists already use, so every reader that
parses theirs - this project's own list.py included - reads ours unchanged. A
VBR MP3's number is its average, marked with a leading "~" (~245/44.1/JS); a
FLAC's is its real bitrate, what the file actually costs per second. Channels:
S stereo, JS joint stereo, DC dual channel, M mono, and "6ch" beyond two.

STANDARD LIBRARY ONLY. Reading audio metadata normally means mutagen, and zero
third-party packages is a property of the project. MP3 and FLAC are the two the
issue asked for and the two these lists are made of; both answer from a seek
and a few KB, never a full read:

- MP3: skip any ID3v2 tags, find the first frame whose header decodes AND whose
  successor sits where the header says, decode version / layer / bitrate /
  sample rate / channel mode, then look inside that frame for a Xing / Info /
  VBRI header, which carries the frame count - the only honest duration for
  VBR. Without one it is CBR and the duration is the audio bytes over the
  bitrate (an ID3v1 tag at the end is not audio).
- FLAC: the "fLaC" magic, then the metadata blocks; STREAMINFO has the sample
  rate, channels and total samples, and where the blocks end is where the audio
  starts - which is what the real bitrate is measured from.

Anything else, and anything these cannot make sense of, is None: the row keeps
its size and nothing more. A malformed file must never take a list build down,
so read() never raises.

FEW REQUESTS PER FILE, MANY FILES AT ONCE. Measured on a real library on an
NFS mount (#914): read one file at a time, with a stat, a 64 KB read and a
seek to the end each, it managed 9.8 files a second - two hours for 64,136
files, every search blocked meanwhile. On a network mount the time is round
trips, not bytes. So each file is opened unbuffered and read in as few
requests as the format allows: one 16 KB read covers the ID3 header, the
first frame and its Xing header, or FLAC's STREAMINFO, in the ordinary case;
a big tag (cover art) or a big picture block costs one more. The ID3v1 tag at
the end is no longer looked for - a request to shave 128 bytes, a few
milliseconds, off a CBR duration. And the files are read several at a
time (LIST_AUDIO_INFO_THREADS), the way QuickList - OmenServe's list maker -
reads them: round trips overlap, bytes do not matter.

THE CACHE, the way QuickList keeps its own: loaded into memory once, checked
against the SIZE the directory scan already knows - no stat, no request, for a
file that has not changed - and written back once. SQLite (stdlib) at
LIST_AUDIO_INFO_CACHE. Rows for files no longer in the library are dropped only
when a rebuild publishes; what a failed or stopped rebuild read is kept.

IN THE BACKGROUND, AFTER THE LIST IS PUBLISHED (#1182). The rebuild used to
read every new or changed file before it wrote the list, within
LIST_AUDIO_INFO_MINUTES; the first one with this on has every audio file to
read. Now the rebuild publishes first with what the cache already knows, and
the same process then reads the rest with no time limit, saving to the cache
in batches as it goes (read_pending()'s `save_every`), so a reading that is
stopped keeps what it read. update_list.py then rewrites the length and
quality into the published list once. A `stop` callable ends a reading early:
no new read starts, the ones in flight finish and are kept.
"""

import os
import sqlite3
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait

import defaults as config

# Extensions read at all. Everything else is size-only without being opened.
AUDIO_EXTENSIONS = (".mp3", ".flac")

# The first request's size: the ordinary file's ID3 header, first frame and
# Xing header, or FLAC's STREAMINFO, all fit.
FIRST_READ = 16 * 1024
# How far past the ID3v2 tag the first MP3 frame may start. Encoders pad; a
# file whose first frame is further out than this is size-only, not a stall.
MP3_SYNC_WINDOW = 64 * 1024
# How many FLAC metadata blocks are walked before giving up on a file.
FLAC_MAX_BLOCKS = 256

_BITRATES = {
    (1, 1): (0, 32, 64, 96, 128, 160, 192, 224, 256, 288, 320, 352, 384, 416, 448),
    (1, 2): (0, 32, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320, 384),
    (1, 3): (0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320),
    (2, 1): (0, 32, 48, 56, 64, 80, 96, 112, 128, 144, 160, 176, 192, 224, 256),
    (2, 2): (0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160),
    (2, 3): (0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160),
}
_SAMPLE_RATES = {1: (44100, 48000, 32000), 2: (22050, 24000, 16000), 25: (11025, 12000, 8000)}
_MP3_MODES = {0: "S", 1: "JS", 2: "DC", 3: "M"}


def is_audio(name):
    return name.lower().endswith(AUDIO_EXTENSIONS)


class _Window:
    """The bytes of one file, fetched in as few requests as possible.

    Every access asks for (offset, length); it is answered from the last read
    when that covers it, and otherwise costs ONE read of at least FIRST_READ
    from that offset. The file is opened unbuffered, so a read is a request
    and nothing is fetched behind it."""

    def __init__(self, handle, size=None):
        self.handle = handle
        self.size = size
        self.base = 0
        self.data = b""

    def get(self, offset, length, need=None):
        """Up to `length` bytes at `offset`; `need` of them must already be
        in the window for it to be enough, else they are fetched."""
        need = length if need is None else need
        start = offset - self.base
        if 0 <= start and start + need <= len(self.data):
            return self.data[start:start + length]
        self.handle.seek(offset)
        self.data = self._read(offset, max(length, FIRST_READ))
        self.base = offset
        return self.data[:length]

    def _read(self, offset, want):
        """`want` bytes from `offset`, or as many as the file has (#1189).

        One raw read() may return FEWER bytes than asked for - a network
        mount with cold caches does - and taken as the whole answer that
        handed the parser a truncated header: an MP3 whose Xing header it
        missed was read as CBR, and a cold rebuild's list came out a byte
        different from a warm one's. So read again until the bytes are there
        or the file ends. Knowing the size, an ordinary read stops at the end
        of the file without asking again to be told so: still one request."""
        if self.size is not None:
            want = max(0, min(want, self.size - offset))
        chunks = []
        got = 0
        while got < want:
            piece = self.handle.read(want - got)
            if not piece:
                break
            chunks.append(piece)
            got += len(piece)
        return b"".join(chunks)


def _id3v2_end(window, start=0):
    """The offset just past every ID3v2 tag at `start` (there may be more
    than one, back to back), or `start` when there is none."""
    offset = start
    for _ in range(8):
        head = window.get(offset, 10)
        if len(head) < 10 or head[:3] != b"ID3":
            break
        size = 0
        for byte in head[6:10]:
            if byte & 0x80:  # not a synchsafe integer: not a real tag
                return offset
            size = (size << 7) | byte
        offset += 10 + size + (10 if head[5] & 0x10 else 0)
    return offset


def _mp3_header(data, at):
    """The frame header at data[at:at+4], decoded, or None."""
    if at + 4 > len(data):
        return None
    word = int.from_bytes(data[at:at + 4], "big")
    if (word >> 21) & 0x7FF != 0x7FF:
        return None
    version = {0: 25, 2: 2, 3: 1}.get((word >> 19) & 3)
    layer = 4 - ((word >> 17) & 3)
    bitrate_index = (word >> 12) & 0xF
    rate_index = (word >> 10) & 3
    if version is None or layer == 4 or bitrate_index in (0, 15) or rate_index == 3:
        return None
    kbps = _BITRATES[(1 if version == 1 else 2, layer)][bitrate_index]
    rate = _SAMPLE_RATES[version][rate_index]
    padding = (word >> 9) & 1
    samples = 384 if layer == 1 else (1152 if layer == 2 or version == 1 else 576)
    if layer == 1:
        length = (12 * kbps * 1000 // rate + padding) * 4
    else:
        length = samples // 8 * kbps * 1000 // rate + padding
    return {"version": version, "layer": layer, "kbps": kbps, "rate": rate,
            "mode": (word >> 6) & 3, "samples": samples, "length": length}


def _first_frame(data):
    """(offset, header) of the first frame whose NEXT frame is where it says,
    or None. A lone 0xFFE bit pattern inside tag padding or cover art decodes
    as a header often enough that one match proves nothing."""
    at = data.find(b"\xff")
    while 0 <= at < len(data) - 4:
        header = _mp3_header(data, at)
        if header and header["length"] > 4:
            following = at + header["length"]
            if following + 4 > len(data):
                return at, header  # nothing to confirm against; take it
            nxt = _mp3_header(data, following)
            if nxt and (nxt["version"], nxt["layer"], nxt["rate"]) == \
                    (header["version"], header["layer"], header["rate"]):
                return at, header
        at = data.find(b"\xff", at + 1)
    return None


def _read_mp3(window, size):
    start = _id3v2_end(window)
    # What the first read already holds past the tag, then - only if no frame
    # is found in it - the whole search window in one more request.
    data = window.get(start, FIRST_READ, need=1)
    found = _first_frame(data)
    if not found and len(data) < MP3_SYNC_WINDOW and start + len(data) < size:
        data = window.get(start, MP3_SYNC_WINDOW)
        found = _first_frame(data)
    if not found:
        return None
    at, header = found
    audio_bytes = size - (start + at)
    if audio_bytes <= 0:
        return None

    frames = total_bytes = None
    vbr = False
    if header["version"] == 1:
        side = 17 if header["mode"] == 3 else 32
    else:
        side = 9 if header["mode"] == 3 else 17
    tag_at = at + 4 + side
    tag = data[tag_at:tag_at + 4]
    if tag in (b"Xing", b"Info") and len(data) >= tag_at + 16:
        flags = int.from_bytes(data[tag_at + 4:tag_at + 8], "big")
        cursor = tag_at + 8
        if flags & 1:
            frames = int.from_bytes(data[cursor:cursor + 4], "big")
            cursor += 4
        if flags & 2:
            total_bytes = int.from_bytes(data[cursor:cursor + 4], "big")
        vbr = tag == b"Xing"
    elif data[at + 36:at + 40] == b"VBRI" and len(data) >= at + 54:
        total_bytes = int.from_bytes(data[at + 46:at + 50], "big")
        frames = int.from_bytes(data[at + 50:at + 54], "big")
        vbr = True

    if frames:
        seconds = frames * header["samples"] / header["rate"]
    else:
        vbr = False  # a VBR header without a frame count says nothing usable
        seconds = audio_bytes * 8 / (header["kbps"] * 1000)
    if seconds <= 0:
        return None
    if vbr:
        kbps = round((total_bytes or audio_bytes) * 8 / seconds / 1000)
    else:
        kbps = header["kbps"]
    return {"seconds": seconds, "kbps": kbps, "rate": header["rate"],
            "channels": _MP3_MODES[header["mode"]], "vbr": vbr}


def _read_flac(window, size):
    start = _id3v2_end(window)
    if window.get(start, 4) != b"fLaC":
        return None
    offset = start + 4
    info = None
    for _ in range(FLAC_MAX_BLOCKS):
        head = window.get(offset, 4)
        if len(head) < 4:
            return None
        length = int.from_bytes(head[1:4], "big")
        if head[0] & 0x7F == 0:
            body = window.get(offset + 4, length)
            if length < 34 or len(body) < 34:
                return None
            word = int.from_bytes(body[10:18], "big")
            info = {"rate": word >> 44, "channels": ((word >> 41) & 7) + 1,
                    "samples": word & 0xFFFFFFFFF}
        # Any other block - a picture of megabytes - is stepped over: the next
        # header is fetched where it is, not read up to.
        offset += 4 + length
        if head[0] & 0x80:
            break
    else:
        return None
    if not info or not info["rate"] or not info["samples"] or offset >= size:
        return None
    seconds = info["samples"] / info["rate"]
    channels = {1: "M", 2: "S"}.get(info["channels"], f"{info['channels']}ch")
    return {"seconds": seconds, "kbps": round((size - offset) * 8 / seconds / 1000),
            "rate": info["rate"], "channels": channels, "vbr": False}


def read(path, size=None):
    """{"seconds", "kbps", "rate", "channels", "vbr"} for an MP3 or FLAC, or
    None - for any other file, and for any file that makes no sense.

    RAISES OSError when the file could not be read at all (#973): a network
    mount's transient EIO or timeout, a sharing violation while another
    program holds it on Windows. That is not an answer about the file, and
    remembered as one it stayed without length and quality until the file
    changed size - Cache.read_pending() reads it again next time instead.
    A file that is no longer there, or anything else that goes wrong reading
    it, None."""
    name = path.lower()
    try:
        if size is None:
            size = os.path.getsize(path)
        if not name.endswith(AUDIO_EXTENSIONS):
            return None
        # Unbuffered: a read is exactly one request, nothing fetched behind it.
        with open(path, "rb", buffering=0) as handle:
            window = _Window(handle, size)
            if name.endswith(".mp3"):
                return _read_mp3(window, size)
            return _read_flac(window, size)
    except FileNotFoundError:
        # Gone since the walk listed it: an answer - the next walk will not
        # list it either.
        return None
    except OSError:
        raise
    except Exception:
        return None
    return None


def describe(info):
    """"4m31s 320/44.1/JS" from what read() returned; "" for None."""
    if not info:
        return ""
    seconds = int(round(info["seconds"]))
    quality = f"{'~' if info.get('vbr') else ''}{info['kbps']}/{info['rate'] / 1000:g}/{info['channels']}"
    return f"{seconds // 60}m{seconds % 60}s {quality}"


def cache_path():
    return getattr(config, "LIST_AUDIO_INFO_CACHE", "./data/audio_info.db")


class Cache:
    """What a rebuild knows about each audio file, kept between rebuilds.

    note() is called from the walk for each listed audio file: a file whose
    size matches the stored row is answered from memory, anything else is put
    aside. read_pending() then reads what was put aside, many at once - since
    #1182 after the list is published, in the background. suffix() answers
    while the list is written. publish()
    stores what this rebuild knows and drops every row it did not see - a file
    removed from the library - and is called only when the rebuild publishes;
    close() without it keeps what was read and drops nothing, so an aborted
    scan that saw half the library does not forget the other half.

    Opening can fail (a read-only data directory, a damaged file); then open()
    returns None, the caller says so, and the list is written size-only.
    """

    def __init__(self, conn, reader=None, scope=""):
        self.conn = conn
        self.scope = scope
        self.reader = reader or read
        self.known = {key: (size, suffix or "") for key, size, suffix in conn.execute(
            "SELECT key, size, suffix FROM audio WHERE scope = ?", (scope,))}
        self.seen = {}       # key -> suffix, for every audio file this rebuild listed and knows
        self.sizes = {}      # key -> size, for the same
        self.fresh = {}      # key -> (size, suffix) read by this rebuild
        self.unread = set()  # keys whose read failed with an I/O error: not remembered (#973)
        # (key, path or (folder, name), size) still to read. The size may be
        # None for a file named by the published list rather than the walk
        # (#1182): it is then asked for when the file is read.
        self.pending = []
        self._unsaved = []   # keys in `fresh` not written to the database yet (#1182)
        self.read_count = 0
        self.reused_count = 0
        self.left_count = 0
        self.unreadable_count = 0  # read, and nothing came of it: size alone (#1182)
        self.workers = 0
        self.read_seconds = 0.0
        self.published = False

    @classmethod
    def open(cls, path=None, reader=None, log=print, scope="", formerly=None):
        """`scope` is the list being built: each list prunes only its own
        rows, so rebuilding one never empties another's. `formerly` is a scope
        whose rows are this list's now (#979): taken over - where this scope
        has no row for the same file - and the rest dropped."""
        path = path or cache_path()
        conn = None
        try:
            folder = os.path.dirname(path)
            if folder:
                os.makedirs(folder, exist_ok=True)
            conn = sqlite3.connect(path)
            # mtime and run are kept for a cache written before #914's rework;
            # nothing reads them now.
            conn.execute("CREATE TABLE IF NOT EXISTS audio (scope TEXT, key TEXT, size INTEGER, "
                         "mtime INTEGER, suffix TEXT, run INTEGER, PRIMARY KEY (scope, key))")
            if formerly is not None and formerly != (scope or ""):
                conn.execute("UPDATE OR IGNORE audio SET scope = ? WHERE scope = ?", (scope or "", formerly))
                conn.execute("DELETE FROM audio WHERE scope = ?", (formerly,))
            conn.commit()
            return cls(conn, reader, scope or "")
        except (sqlite3.Error, OSError) as err:
            if conn is not None:
                conn.close()
            log(f"[LIST-GEN] Could not open the audio info cache at {path!r} ({err}); "
                f"this list is written with sizes only.")
            return None

    def note(self, key, path, size):
        """One listed audio file. No request is made here: a file whose size
        has not changed is answered from the cache, anything else waits for
        read_pending().

        `path` may be a (folder, name) pair, joined only if the file is read:
        on an unchanged library almost every call is answered from the cache,
        and joining a path a million times for nothing cost a second or two
        on Windows (#1138)."""
        hit = self.known.get(key)
        if hit is not None and hit[0] == size:
            self.seen[key] = hit[1]
            self.sizes[key] = size
            self.reused_count += 1
        else:
            self.pending.append((key, path, size))

    def read_pending(self, workers=16, budget=None, clock=time.monotonic, progress=None,
                     stop=None, save_every=0):
        """Read what note() put aside, `workers` at a time. With `budget`
        (seconds), no read is STARTED after it runs out - the ones in flight
        finish - and the rest are left for the next rebuild. `stop()`, when it
        answers True, does the same (#1182): a new rebuild ending a reading
        that runs in the background. `progress(done, total)` is called once at
        the start and then only when a read has completed (#968).

        `save_every` > 0 writes what was read to the database every that many
        files (#1182), so a reading that is stopped, or killed, keeps what it
        read and not only what close() would have written at the end.

        ONLY REAL PROGRESS IS REPORTED. The rebuild's watchdog stops a child
        whose progress file has not moved for LIST_UPDATE_STALL_SECONDS.
        Called every second whether anything finished or not, this kept it
        moving while every read hung on a network mount that had stopped
        answering: the budget only stops NEW reads, the pool waits for the
        stuck ones, and the rebuild never ended - and every later !update
        was refused as already running, until a restart."""
        total = len(self.pending)
        if not total:
            return
        workers = max(1, int(workers))
        self.workers = workers
        # Wall time on the real clock, not `clock` - that one is the budget's,
        # and a test drives it by hand. perf_counter, not monotonic: on
        # Windows before 3.13 monotonic ticks every ~15.6 ms, and a short
        # batch read inside one tick measured 0 s and no rate at all.
        began = time.perf_counter()
        deadline = None if not budget else clock() + budget
        queue = iter(self.pending)
        running = {}
        done = 0

        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="audio-info") as pool:
            def top_up():
                while len(running) < workers * 2:
                    if deadline is not None and clock() >= deadline:
                        return
                    if stop is not None and stop():
                        return
                    item = next(queue, None)
                    if item is None:
                        return
                    key, path, size = item
                    if isinstance(path, tuple):
                        # (folder, name) from the walk, joined only now that
                        # the file is really read (#1138).
                        path = os.path.join(*path)
                    running[pool.submit(self._read_one, path, size)] = (key, size)

            top_up()
            reported = 0
            if progress is not None:
                progress(0, total)
            while running:
                finished, _ = wait(running, timeout=1.0, return_when=FIRST_COMPLETED)
                for future in finished:
                    key, size = running.pop(future)
                    try:
                        size, info = future.result()
                        suffix = describe(info)
                    except OSError:
                        # Size alone this time, and not remembered: the
                        # next rebuild reads it again (#973).
                        suffix = ""
                        self.unread.add(key)
                    except Exception:
                        suffix = ""
                    self.seen[key] = suffix
                    self.sizes[key] = size
                    if key not in self.unread:
                        self.fresh[key] = (size, suffix)
                        self._unsaved.append(key)
                    if not suffix:
                        self.unreadable_count += 1
                    done += 1
                if save_every and len(self._unsaved) >= save_every:
                    self.save_fresh()
                if progress is not None and done != reported:
                    reported = done
                    progress(done, total)
                top_up()

        self.read_count = done
        self.left_count = total - done
        self.read_seconds = time.perf_counter() - began
        # Read, or left for the next rebuild: either way nothing needs the
        # list now, and on a cold first run it is every audio file in the
        # library (#1137). This line used to sit after rate()'s return, where
        # it never ran.
        self.pending = []

    def _read_one(self, path, size):
        """(size, what the reader made of it), on a worker thread. A file the
        published list named has no size from a walk (#1182): it is asked
        for here, on the worker, so those round trips overlap like the reads,
        and an OSError doing it counts as the read's own (#973).
        """
        if size is None:
            size = os.path.getsize(path)
        return size, self.reader(path, size)

    def save_fresh(self):
        """Write what was read since the last save (#1182). True when it was
        written; False when the database refused, and the rows stay unsaved
        for the next save or close() to try again - a locked cache must not
        end a reading that has hours to go."""
        if not self._unsaved:
            return True
        rows = [(key, self.fresh[key]) for key in self._unsaved]
        try:
            with self.conn:
                self._save(rows)
        except sqlite3.Error:
            return False
        self._unsaved = []
        return True

    def rate(self):
        """Files read per second of reading, or None when nothing was read.
        What an operator compares to choose LIST_AUDIO_INFO_THREADS: on a
        network mount the ceiling is the server's, not the setting's."""
        if not self.read_count or self.read_seconds <= 0:
            return None
        return self.read_count / self.read_seconds

    def suffix(self, key):
        return self.seen.get(key, "")

    def _save(self, rows):
        self.conn.executemany(
            "INSERT OR REPLACE INTO audio (scope, key, size, mtime, suffix, run) VALUES (?, ?, ?, 0, ?, 0)",
            ((self.scope, key, size, suffix) for key, (size, suffix) in rows))

    def publish(self):
        """This rebuild published: keep what it saw, forget the rest.

        ONLY WHAT CHANGED IS WRITTEN (#1137). Deleting the whole scope and
        inserting every row again rewrote a million rows on an unchanged
        library - the ordinary weekly rebuild - while searches were still
        held. Dropped: every stored row this rebuild did not see (a file
        removed from the library), and every row whose read failed with an
        I/O error (#973) - its OLD row must go too, or a file that returns to
        its old size would be served the stale suffix without a read. Written:
        every seen and read row that differs from what is stored. The table
        ends up exactly as before; only the legacy mtime and run columns of
        an untouched row keep their old value, and nothing reads them."""
        known = self.known
        with self.conn:
            self.conn.executemany(
                "DELETE FROM audio WHERE scope = ? AND key = ?",
                ((self.scope, key) for key in known
                 if key not in self.seen or key in self.unread))
            self._save((key, (self.sizes[key], suffix)) for key, suffix in self.seen.items()
                       if key not in self.unread and known.get(key) != (self.sizes[key], suffix))
        self.published = True

    def close(self):
        """Without publish(): keep what was read, drop nothing. Only what no
        batch has saved yet is written (#1182); the rest is already there.
        """
        try:
            if not self.published and self._unsaved:
                with self.conn:
                    self._save((key, self.fresh[key]) for key in self._unsaved)
                self._unsaved = []
            self.conn.close()
        except sqlite3.Error:
            pass


def row_key(folder, filename):
    """The cache key for one list row: its folder heading and its name, which
    is what the list itself identifies a file by."""
    return f"{folder}\x00{filename}"
