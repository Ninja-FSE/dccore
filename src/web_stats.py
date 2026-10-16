"""The dashboard's Stats page (#1318): the figures, importing totals from
another server's stats or a KeepTrack file, and the transfer record -
its periods, one nick's rows, forgetting a nick, and the CSV export.

Split out of webserver.py, step 4 of splitting it (docs/FUTURE.md); the
routes that call this are still there, and webserver imports every name
back.
"""

import time

from web_library import build_library_payload
import adminchat
import defaults as config


# The ceilings the import refuses beyond. A stray digit from a hand-edited
# text file is far likelier than a genuine value up here, and importing one
# silently is worse than refusing it - the operator can retype a number, but
# they cannot tell that a total is wrong once it looks plausible.
#
# 2^63 is where a 64-bit counter stops meaning anything; a byte total past a
# petabyte and a speed record past 10 GB/s are both past any real DCC link by
# orders of magnitude, and both are what a mis-parsed field looks like.
MAX_IMPORT_FILES = 2 ** 63 - 1


MAX_IMPORT_BYTES = 1 << 50           # 1 PiB


MAX_IMPORT_SPEED = 10 * 1000 ** 3    # 10 GB/s


_IMPORT_LIMITS = {
    "total_files": (MAX_IMPORT_FILES, "files sent"),
    "total_bytes": (MAX_IMPORT_BYTES, "bytes sent"),
    "speed_record": (MAX_IMPORT_SPEED, "speed record"),
    # KeepTrack's (#1062), into the transfer record's imported totals.
    "received_files": (MAX_IMPORT_FILES, "files received"),
    "received_bytes": (MAX_IMPORT_BYTES, "bytes received"),
}


_RECEIVED = ("received_files", "received_bytes")


def current_importable_stats():
    """What the three importable figures are right now.

    Read through the same accessors the Stats page uses, so the "before" column
    of the preview cannot disagree with the page the operator is looking at.
    """
    import db

    row = db.load_advanced_stats() or [0] * 7
    try:
        total_files = int(row[0])
    except (IndexError, TypeError, ValueError):
        total_files = 0
    try:
        total_bytes = int(row[1])
    except (IndexError, TypeError, ValueError):
        total_bytes = 0
    try:
        record = int(db.get_speed_record() or 0)
    except (TypeError, ValueError):
        record = 0
    # What an earlier KeepTrack import put in the record (#1062) - the figure
    # a new one replaces, so before and after compare like with like.
    received = {}
    try:
        import transfer_log
        received = transfer_log.imported_totals("keeptrack").get(transfer_log.RECEIVED) or {}
    except Exception as err:
        print(f"[STATS IMPORT] The imported received totals are unavailable: {err}")
    return {"total_files": total_files, "total_bytes": total_bytes,
            "speed_record": record,
            "received_files": int(received.get("files") or 0),
            "received_bytes": int(received.get("bytes") or 0)}


def validate_import_values(raw):
    """(clean, errors) for the three figures an import may carry.

    Every field optional - an operator missing an add-on imports what they
    have - but a field that IS present must be a whole number in range.
    Checked HERE and not only in the page: the page is convenience, this is
    the boundary, and everything arriving is from a text file somebody may
    have hand-edited.
    """
    clean = {}
    errors = []
    for name, (ceiling, label) in _IMPORT_LIMITS.items():
        if name not in (raw or {}):
            continue
        value = raw[name]
        if isinstance(value, bool) or not isinstance(value, (int, float, str)):
            errors.append(f"{label}: expected a whole number.")
            continue
        try:
            # str() first so "45902" works and 45902.0 does not silently
            # truncate a fraction somebody meant to be there.
            number = int(str(value).strip())
        except (TypeError, ValueError):
            errors.append(f"{label}: {value!r} is not a whole number.")
            continue
        if number < 0:
            errors.append(f"{label}: cannot be negative.")
            continue
        if number > ceiling:
            errors.append(f"{label}: {number:,} is beyond anything real "
                          f"(the limit is {ceiling:,}). A stray digit in a "
                          f"hand-edited file looks exactly like this.")
            continue
        clean[name] = number
    # When KeepTrack began counting, as the import found it (#1062). A date
    # or nothing: it is written into the record as it stands.
    if (raw or {}).get("received_since") not in (None, ""):
        since = str(raw["received_since"]).strip()
        import datetime
        try:
            datetime.date.fromisoformat(since)
            clean["received_since"] = since
        except ValueError:
            errors.append(f"received since: {since!r} is not a date (YYYY-MM-DD).")
    # A date with no received figure is not a figure to write. Kept when the
    # figure it dates was refused, it reached the apply on its own, nothing
    # wrote it, and the import answered 500 "could not be written" although
    # everything else had been (#1062 review).
    if not any(name in clean for name in _RECEIVED):
        clean.pop("received_since", None)
    return clean, errors


def build_stats_import_preview(text):
    """POST /api/stats/import/preview: what a vars.ini would do, changing nothing.

    Separate from the write for the reason the issue gives: on a fresh install
    nobody reads the sentence about overwriting, and on a used one it is the
    only thing that matters. The page shows before and after side by side and
    the operator confirms.
    """
    import omenserve_import

    found = omenserve_import.read_install(text)
    clean, errors = validate_import_values(found.get("values") or {})
    notes = list(found.get("notes", []))
    if any(name in clean for name in _RECEIVED):
        import transfer_log
        if not transfer_log._path():
            # Nowhere to put them: the record is turned off (#1068).
            for name in _RECEIVED + ("received_since",):
                clean.pop(name, None)
            notes.append("The received totals are not imported: the transfer record is off "
                         "(Settings > Advanced, TRANSFER_LOG_FILE is empty).")
    # Every source offered as a choice is checked as the default one is (#1062
    # review): only `values` was, so the other could show -5 in the preview and
    # then have the whole import refused when picked, here or in configure.py.
    # The default's faults are in `errors` already.
    sources = []
    for entry in found.get("sent_sources", []):
        figures = {k: v for k, v in entry.items() if k not in ("name", "label")}
        good, bad = validate_import_values(figures)
        sources.append(dict(good, name=entry["name"], label=entry["label"]))
        if entry["name"] != found.get("sent_source"):
            errors.extend(f"{entry['label']}: {fault}" for fault in bad)
    return {
        "rows": found.get("rows", []),
        "notes": notes + errors,
        "current": current_importable_stats(),
        "values": clean,
        # Where the sent totals can come from (#1062); the page offers a
        # choice when there are two, and posts back the one picked.
        "sent_sources": sources,
        "sent_source": found.get("sent_source"),
        # OVERWRITTEN, NOT COMBINED, and said where the page can put it in
        # front of the operator rather than buried in prose they will not read
        # on a fresh install and cannot miss on a used one.
        "replaces": {name: value for name, value in clean.items()
                     if current_importable_stats().get(name)},
    }


def apply_stats_import(raw):
    """(http_status, payload) for POST /api/stats/import.

    Writes through db.set_lifetime_totals() and db.save_speed_record(),
    which take the disk lock and write atomically - a caller, not a second
    implementation of either.

    Returns the before and after, so the page reports what actually happened
    rather than what it asked for.
    """
    import db

    clean, errors = validate_import_values(raw)
    if errors:
        return 400, {"error": errors[0], "errors": errors}
    if not clean:
        return 400, {"error": "Nothing to import - no recognised figure was "
                              "supplied."}

    before = current_importable_stats()

    written = None
    if "total_files" in clean or "total_bytes" in clean:
        # The 7-column row read-modify-written as a whole, under one lock
        # (#690): the day columns and the date belong to the daemon's own
        # rotation and are not this feature's to touch, and a transfer that
        # completes while this runs must not lose its count to a stale row.
        written = db.set_lifetime_totals(total_files=clean.get("total_files"),
                                         total_bytes=clean.get("total_bytes"))

    if "speed_record" in clean:
        db.save_speed_record(clean["speed_record"])

    # KeepTrack's received totals (#1062): into the transfer record's imported
    # totals, replacing an earlier KeepTrack import's. A half given keeps the
    # other half as it is.
    received_written = None
    if any(name in clean for name in _RECEIVED):
        import transfer_log
        received_written = transfer_log.import_totals(
            "keeptrack", transfer_log.RECEIVED,
            clean.get("received_files", before["received_files"]),
            clean.get("received_bytes", before["received_bytes"]),
            since=clean.get("received_since"))

    # WHAT ACTUALLY LANDED, compared against what was asked for. Both writers
    # swallow their own errors and return None - correct for them, since a
    # failed stats write must not take the daemon down - which meant this
    # returned 200 and "imported: [...]" for figures that never reached the
    # disk. The operator would have been told their history came across and
    # found half of it, with nothing to say which half.
    after = current_importable_stats()

    def landed_as_asked(name):
        # The totals are judged by the row the locked write produced, not by
        # a later read (#690): a transfer completing right after the import
        # legitimately moves them on, and that is not a failed write.
        if name == "total_files":
            return written is not None and written[0] == clean[name]
        if name == "total_bytes":
            return written is not None and written[1] == clean[name]
        if name == "received_since":
            return bool(received_written)
        return after.get(name) == clean[name]

    landed = [name for name in sorted(clean) if landed_as_asked(name)]
    missing = [name for name in sorted(clean) if name not in landed]
    if missing:
        return 500, {
            "error": "Some figures could not be written: "
                     + ", ".join(missing)
                     + ". Check the daemon log and the data directory.",
            "before": before, "after": after, "imported": landed,
            "failed": missing,
        }

    return 200, {"before": before, "after": after, "imported": landed}


# KeepTrack's per-nick history (#1064). A KTData.txt of a long-running bot is
# a few hundred KB. The app already refuses a body over 8 MB
# (MAX_CONTENT_LENGTH); this says the same about the text itself.
MAX_KTDATA_CHARS = 8 * 1024 * 1024


def _read_ktdata_text(text):
    """(parsed, error) for an uploaded KTData.txt's text."""
    import omenserve_import
    text = str(text or "")
    if len(text) > MAX_KTDATA_CHARS:
        return None, "That file is far larger than a KTData.txt; nothing was read."
    # Not MAX_IMPORT_FILES (2**63-1): a nick's figure is summed with its
    # recorded transfers in SQL, and at that size the sum overflows (#1064
    # review). read_ktdata()'s own limits, which leave that room.
    return omenserve_import.read_ktdata(text, max_bytes=MAX_IMPORT_BYTES), None


def build_ktdata_preview(text):
    """POST /api/stats/import-ktdata/preview: what a KTData.txt would add,
    writing nothing - the nicks per direction, their totals, the top ten, the
    lines skipped and why, and whether an earlier import would be replaced."""
    import transfer_log
    parsed, error = _read_ktdata_text(text)
    if error:
        return 400, {"error": error}
    if not transfer_log._path():
        return 409, {"error": "The transfer record is off (Settings > Advanced, TRANSFER_LOG_FILE "
                              "is empty), so there is nowhere to put the per-nick history."}
    rows = parsed["rows"]
    figures = {}
    for direction in (transfer_log.SENT, transfer_log.RECEIVED):
        mine = [r for r in rows if r[0] == direction]
        top = sorted(mine, key=lambda r: (-r[2], -r[3], r[1]))[:10]
        figures[direction] = {
            "nicks": len(mine),
            "files": sum(r[2] for r in mine),
            "bytes": sum(r[3] for r in mine),
            "top": [{"nick": r[1], "files": r[2], "bytes": r[3]} for r in top],
        }
    return 200, {"lines": parsed["lines"], "skipped": parsed["skipped"], "figures": figures,
                 "replaces": transfer_log.imported_nicks("keeptrack")}


def apply_ktdata_import(text):
    """POST /api/stats/import-ktdata: read the file again - the preview's
    answer is not trusted back - and write its per-nick totals into the
    transfer record, replacing an earlier KeepTrack per-nick import."""
    import transfer_log
    status, preview = build_ktdata_preview(text)
    if status != 200:
        return status, preview
    parsed, _error = _read_ktdata_text(text)
    if not parsed["rows"]:
        return 400, {"error": "Nothing in that file could be imported."}
    since = (transfer_log.imported_totals("keeptrack").get(transfer_log.RECEIVED) or {}).get("since")
    written = transfer_log.import_nicks("keeptrack", parsed["rows"], since=since)
    if written is None:
        return 500, {"error": "The per-nick history could not be written. Check the daemon log."}
    return 200, {"imported": written, "nicks": transfer_log.imported_nicks("keeptrack")}


# THE TRANSFER RECORD ON THE STATS PAGE (#1102). The record (#1068) has been
# written since #1069, but nothing read it. A period is one of these; "all"
# is the only one the imported KeepTrack figures count in, as everywhere in
# transfer_log.
RECORD_PERIODS = {"day": 86400, "week": 7 * 86400, "month": 30 * 86400, "all": None}


def _record_since(period):
    """(since, error) for a period name: a Unix time, or None for all of it."""
    if period not in RECORD_PERIODS:
        return None, f"Unknown period {str(period)[:20]!r}: use day, week, month or all."
    seconds = RECORD_PERIODS[period]
    return (int(time.time()) - seconds if seconds else None), None


def _record_off():
    import transfer_log
    if transfer_log._path():
        return None
    return {"enabled": False,
            "error": "The transfer record is off (Settings > Advanced, TRANSFER_LOG_FILE is empty)."}


def _wait_text(seconds):
    """A queue wait as the page shows it: format_uptime() reads anything under
    a minute as "0 Min", which for a wait is the wrong answer."""
    if seconds is None:
        return ""
    import adminchat
    return f"{int(seconds)} Sec" if seconds < 60 else adminchat.format_uptime(seconds)


def _nick_rows(rows):
    import stats_mgr
    return [{"nick": nick, "files": files, "bytes": size, "bytes_text": stats_mgr.format_size_human(size)}
            for nick, files, size in rows]


def _lifetime_sent():
    """(files, bytes) the bot has sent over its life, from stats.txt."""
    import db
    try:
        row = db.load_advanced_stats_rolled()
        return int(str(row[0]).strip()), int(str(row[1]).strip())
    except Exception:
        return 0, 0


def _record_tops(since):
    """The most-sent files and the most-sent album folders for a period, each as
    [{name, count}]. A period comes from the record. All time comes from the
    count file, which holds everything the bot has ever sent (the record only
    began with #1069); the record answers only when that file has nothing."""
    import db
    import transfer_log
    out = []
    for kind in (transfer_log.KIND_FILE, transfer_log.KIND_ALBUM):
        rows = []
        if since is None:
            try:
                rows = [{"name": row["name"], "count": row["count"]} for row in db.top_downloads(limit=10, kind=kind)]
            except Exception:
                rows = []
        if not rows:
            rows = [{"name": name, "count": count} for name, count in transfer_log.top_files(10, since, kind)]
        out.append(rows)
    return out[0], out[1]


def _record_outcomes(since):
    """How the period's transfers ended (#1203), as the Stats page's outcomes
    table shows them: sent files, albums and lists, and everything received.

    `failed` includes the albums whose folder could not be packed, which
    `pack_failed` also gives on its own. The success rate is completed out of
    completed and failed: a cancelled transfer was stopped by the operator,
    the user or the bot, not lost by the network, and counting it would
    blame the network for it. Rounded down, so 100% means nothing failed.
    "since" is set when the counting starts later than the period does,
    because failures were not written before then."""
    import transfer_log
    found = transfer_log.outcomes(since)
    lines = []
    for direction, kind in ((transfer_log.SENT, transfer_log.KIND_FILE),
                            (transfer_log.SENT, transfer_log.KIND_ALBUM),
                            (transfer_log.SENT, transfer_log.KIND_LIST),
                            (transfer_log.RECEIVED, None)):
        counts = {}
        for (row_direction, row_kind), by_status in found["rows"].items():
            if row_direction == direction and (kind is None or row_kind == kind):
                for status, count in by_status.items():
                    counts[status] = counts.get(status, 0) + count
        completed = counts.get(transfer_log.STATUS_COMPLETED, 0)
        pack_failed = counts.get(transfer_log.STATUS_PACK_FAILED, 0)
        failed = counts.get(transfer_log.STATUS_FAILED, 0) + pack_failed
        cancelled = counts.get(transfer_log.STATUS_CANCELLED, 0)
        decided = completed + failed
        rate = (completed * 100) // decided if decided else None
        lines.append({"direction": direction, "kind": kind or "all",
                      "attempts": completed + failed + cancelled, "completed": completed,
                      "failed": failed, "pack_failed": pack_failed, "cancelled": cancelled,
                      "success_rate": rate, "success_text": f"{rate}%" if rate is not None else ""})
    return {"since": found["since"], "rows": lines}


def build_record_payload(period="all"):
    """GET /api/stats/record?period=: the record's figures for a period - the
    totals, the most-sent files, the nicks sent to and received from most, and
    how the transfers ended (#1203). Every figure comes raw and as the page
    shows it, as in build_stats_payload."""
    import stats_mgr
    import transfer_log
    since, error = _record_since(period)
    if error:
        return 400, {"error": error}
    off = _record_off()
    if off:
        return 200, dict(off, period=period)
    figures = transfer_log.summary(since)
    top_files, top_albums = _record_tops(since)
    if since is None:
        # All time is never less than the lifetime counter: the record only
        # began with #1069, and what the bot sent before is in stats.txt only.
        files, size = _lifetime_sent()
        figures["files_sent"] = max(figures["files_sent"], files)
        figures["bytes_sent"] = max(figures["bytes_sent"], size)
    figures.update(
        bytes_sent_text=stats_mgr.format_size_human(figures["bytes_sent"]),
        bytes_received_text=stats_mgr.format_size_human(figures["bytes_received"]),
        top_speed_text=stats_mgr.format_speed(figures["top_speed"]),
        average_speed_text=stats_mgr.format_speed(figures["average_speed"]),
        queue_wait_text=_wait_text(figures["queue_wait_seconds"]))
    return 200, {
        "enabled": True,
        "period": period,
        "since": since,
        "summary": figures,
        "top_files": top_files,
        "top_albums": top_albums,
        "albums_enabled": bool(getattr(config, "RAR_ENABLED", True)),
        "top_sent": _nick_rows(transfer_log.top_nicks(transfer_log.SENT, 10, since)),
        "top_received": _nick_rows(transfer_log.top_nicks(transfer_log.RECEIVED, 10, since)),
        "outcomes": _record_outcomes(since),
        # Said on the page: all time holds figures from before the record began -
        # the bot's own totals, a nick's, or both (#1102 review: a per-nick import
        # alone left the note hidden while the tables counted it).
        "includes_imported": since is None and transfer_log.has_imported(),
    }


def build_record_nick_payload(nick, period="all"):
    """GET /api/stats/record/nick?nick=&period=: what one nick has had from
    this bot and what this bot has had from it."""
    import stats_mgr
    import transfer_log
    since, error = _record_since(period)
    if error:
        return 400, {"error": error}
    off = _record_off()
    if off:
        return 409, off
    wanted = transfer_log._nick(nick)
    if wanted is None:
        return 400, {"error": "Type a nick to look up."}
    figures = transfer_log.nick_summary(wanted, since)
    figures.update(bytes_sent_text=stats_mgr.format_size_human(figures["bytes_sent"]),
                   bytes_received_text=stats_mgr.format_size_human(figures["bytes_received"]))
    return 200, {"nick": wanted, "period": period, "figures": figures,
                 "found": any(figures[key] for key in ("files_sent", "lists_sent", "files_received"))}


def apply_record_forget(body):
    """POST /api/stats/record/forget: {"nick": "..."} takes one nick out of
    the record, imported figures included; {"everyone": true} empties it.
    Two separate words rather than an empty nick meaning everything, so a
    blank box can never wipe the record."""
    import transfer_log
    off = _record_off()
    if off:
        return 409, off
    try:
        if body.get("everyone") is True:
            removed = transfer_log.forget_all()
            print(f"[TRANSFER-LOG] The record was emptied from the dashboard ({removed} row(s)).")
            return 200, {"removed": removed, "everyone": True}
        nick = transfer_log._nick(body.get("nick"))
        if nick is None:
            return 400, {"error": "Say which nick to forget."}
        removed = transfer_log.forget_nick(nick)
    except Exception as err:
        return 500, {"error": f"The record could not be changed: {err}"}
    # The nick is not printed: the log would keep what the record just let go.
    print(f"[TRANSFER-LOG] A nick was forgotten from the dashboard ({removed} row(s)).")
    return 200, {"removed": removed, "nick": nick}


# A cell a spreadsheet would read as a formula. A nick or a file name is
# somebody else's text, and "=HYPERLINK(...)" in one runs when the export is
# opened; a leading apostrophe makes it text again.
_FORMULA_START = ("=", "+", "-", "@", "\t", "\r")


def _csv_cell(value):
    if value is None:
        return ""
    text = str(value)
    return "'" + text if text.startswith(_FORMULA_START) else text


def record_csv_lines(since=None):
    """The CSV export, one line at a time (#1102): a header, then every row
    from `since` on. The time is local and readable; the rest as stored."""
    import csv
    import io
    import transfer_log
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\r\n")

    def line(cells):
        buffer.seek(0)
        buffer.truncate()
        writer.writerow(cells)
        return buffer.getvalue()

    yield line(("time",) + transfer_log.EXPORT_COLUMNS[1:])
    for row in transfer_log.iter_rows(since):
        stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(row[0]))
        yield line([stamp] + [_csv_cell(value) for value in row[1:]])


STATS_PARTS = ("transfer", "sent", "library", "top")


def build_stats_payload(parts=None):
    """Everything the Stats view shows, in one request - or, with `parts`,
    only those (#1123): Live Transfers polls this every few seconds and shows
    the transfer figures alone, while "top" and "library" read and rank whole
    files. None is every part.

    Every figure comes twice: the raw number, and the daemon's own rendering of
    it. The raw one is for anything that is not this page - a script, a future
    CTCP reply - which should not have to parse "1.21TB" back into an integer.
    The rendered one uses stats_mgr.format_speed(), format_size_human() and
    adminchat.format_uptime(), which are what the channel advert and the admin
    console already print, so the dashboard cannot disagree with the advert
    about how the same number reads.

    Nothing here takes a lock, matching build_queue_payload(): a shallow copy of
    the live containers is good enough for a status display and cannot deadlock
    against the daemon's own queue processing.

    Every source is guarded individually. This is a read-only status page and a
    missing stats file, an unbuilt list or a permissions error on lists/ must
    cost the tile that needs it and nothing else - a dashboard that 500s
    because one counter is unreadable is worse than one showing a gap.
    """
    # Imported here, not at module scope. tests/test_import_graph.py pins that
    # importing webserver.py pulls in none of the daemon, so that
    # tests/test_webserver.py can exercise every route without one running -
    # and db/list/stats_mgr are named in that test explicitly. Same reason the
    # File Lists payload imports `list` inside its own function.
    import db
    import stats_mgr

    wanted = set(STATS_PARTS if parts is None else parts)
    active = list(getattr(config, "active_transfers", []))
    queue = dict(getattr(config, "dcc_queue", {}))

    try:
        speed_now = int(stats_mgr.live_speed())
    except Exception:
        speed_now = 0

    try:
        record = int(db.get_speed_record())
    except Exception:
        record = 0

    try:
        uptime = int(stats_mgr.get_uptime_seconds())
    except Exception:
        uptime = 0

    # When the first busy slot is likely to free up (#1207), from the same
    # copy of active_transfers the Sending card counts, so the two cannot
    # disagree about whether a slot is free. "now", whole minutes, or None.
    try:
        next_slot = stats_mgr.next_slot_estimate(transfers=active)
    except Exception:
        next_slot = None

    # The 7-column row: total files, total bytes, yesterday's pair, today's
    # pair, and the date the day last rolled over. Read through
    # load_advanced_stats_rolled(), which rolls a COPY, so a bot that has sent
    # nothing since midnight does not show yesterday's figures labelled Today -
    # and so answering a GET does not write to disk.
    sent = {"total_files": 0, "total_bytes": 0, "today_files": 0,
            "today_bytes": 0, "yesterday_files": 0, "yesterday_bytes": 0}
    try:
        if "sent" not in wanted:
            raise LookupError("not asked for")
        row = db.load_advanced_stats_rolled()
        names = ("total_files", "total_bytes", "yesterday_files",
                 "yesterday_bytes", "today_files", "today_bytes")
        for index, name in enumerate(names):
            try:
                sent[name] = int(str(row[index]).strip())
            except (IndexError, TypeError, ValueError):
                pass
    except Exception:
        pass

    # Every served list, not the primary alone (#952) - see the function.
    try:
        if "library" not in wanted:
            raise LookupError("not asked for")
        library = build_library_payload()
    except Exception:
        library = {"files": 0, "size": None, "raw_bytes": 0, "list_date": None,
                   "rar_folders": None, "lists": []}

    for name in ("total", "today", "yesterday"):
        sent[name + "_text"] = stats_mgr.format_size_human(sent[name + "_bytes"])

    # Counted together, reported apart. A 700 MB album and a 4 MB track are not
    # comparable, so one merged table would rank by whichever kind this bot
    # happens to send more of - a fact about the library, not about demand.
    #
    # albums_enabled is config.RAR_ENABLED (#140). With folder packing off no
    # album can ever be sent, so the page says that instead of showing a table
    # that would stay empty for ever with no explanation. Any counts from
    # before it was switched off are still returned - they are history, and
    # deciding they never happened would be the wrong kind of tidy.
    top = {"files": [], "albums": [],
           "albums_enabled": bool(getattr(config, "RAR_ENABLED", True))}
    if "top" in wanted:
        try:
            top["files"] = db.top_downloads(limit=10, kind="file")
            top["albums"] = db.top_downloads(limit=10, kind="album")
        except Exception:
            pass

    payload = {
        "top": top,
        "transfer": {
            "speed_now": speed_now,
            "speed_now_text": stats_mgr.format_speed(speed_now),
            "record": record,
            "record_text": stats_mgr.format_speed(record),
            "sending": len(active),
            "slots": int(getattr(config, "MAX_DCC_SLOTS", 0) or 0),
            "next_slot": next_slot,
            "next_slot_text": stats_mgr.format_next_slot(next_slot),
            "queued_files": sum(len(entries) for entries in queue.values()),
            "queued_users": len(queue),
            "uptime_seconds": uptime,
            "uptime_text": adminchat.format_uptime(uptime),
        },
        "sent": sent,
        "library": library,
        "version": str(getattr(config, "SCRIPT_VERSION", "")),
    }
    for part in STATS_PARTS:
        if part not in wanted:
            del payload[part]
    return payload
