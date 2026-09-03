# =====================================================================
# DCC.PY - THE TRANSFER ENGINE
# =====================================================================
import socket
import threading
import time
import os
import sys
import re
import subprocess

import defaults as config
import platform_compat
import list as list_mod
import announce
import db
import runtime

# THE queue lock: bound to runtime.py's object, not constructed here - dcc.py is
# reloaded by !rehash (commands.CORE_MODULES), and importlib.reload() re-executing
# `queue_lock = threading.Lock()` would rebind this name to a brand-new lock every
# time, exactly the bug runtime.py's own module docstring exists to remove for
# containers. A thread already inside `with queue_lock:` when that happened would
# go on holding the old, now-invisible object while the next caller acquired the
# fresh one - two threads in the critical section at once. Binding to runtime.py's
# object instead means a reload re-runs this same line and picks the identical live
# lock back up, so `with queue_lock:` throughout this file always resolves to the
# one object every caller has ever held, and is what db.py's and announce.py's own
# comments mean when they say "queue_lock".
queue_lock = runtime.queue_lock

def is_safe_path(base_dir, path, follow_symlinks=True):
    """Safety filter: prevents directory traversal attacks.

    A falsy base_dir (#184's review: FILE_DIRECTORY is
    deliberately not in settings_file.REQUIRED any more, so callers that
    used to be able to assume it was always set can no longer do so) refuses
    rather than raising: os.path.realpath(None) is a TypeError, and every
    caller here is a security boundary, where "there is no base to check
    against" must read as "nothing is safe", not as an unhandled exception a
    caller's own except Exception can turn into a silent, unexplained
    failure.
    """
    if not base_dir:
        return False

    if follow_symlinks:
        matchpath = os.path.realpath(path)
    else:
        matchpath = os.path.abspath(path)

    base = os.path.realpath(base_dir)

    # FIXED: compares per directory step, rather than a plain startswith.
    # With startswith alone, "/srv/library-backup" would wrongly be accepted
    # as part of "/srv/library", because the string happens to begin the same way.
    return matchpath == base or matchpath.startswith(base + os.sep)

def download_count_identity(file_path, file_name):
    """(key, display name, kind) for one completed send, for db.record_download().

    Split out of start_dcc_send() so it can be tested without a socket: the
    rules below are the whole reason the "most downloaded" tables mean
    anything, and they were previously buried in the middle of a transfer.

    An ALBUM goes out as a packed archive from TMP_ZIP_DIR whose name this
    module built from the folder, so the archive's own name is already the
    readable one and identifies the album on its own.

    A FILE is keyed by its path relative to the library and only DISPLAYED by
    its basename. Two albums can hold a track with the same filename - #110 is
    the bug where exactly that ambiguity sent the wrong file - so keying on the
    basename would credit one track with another's downloads and report a
    popularity that belongs to neither.
    """
    if _is_temp_zip_cache_file(file_path):
        name = file_name[:-4] if str(file_name).lower().endswith(".rar") else file_name
        return file_name, name, "album"

    try:
        key = os.path.relpath(file_path, config.FILE_DIRECTORY)
    except ValueError:
        # A different drive on Windows: relpath refuses rather than returning
        # something wrong. The full path still identifies the file uniquely,
        # which is all a key has to do.
        key = file_path
    return key, file_name, "file"


def _sanitize_rar_leaf_name(folder_leaf):
    """The single, shared sanitiser for a packed album's LEAF folder name -
    used both to build the queue row's visible filename (what a user's
    client sees, and what AutoQ.mrc's own reconciliation matches) and the
    packer's own archive name. #162 finding #7: these used to be two
    independent regexes that disagreed - one deleted a disallowed
    character outright, the other replaced it with "_"; one kept square
    brackets, the other stripped them; one was ASCII-only (a-zA-Z0-9),
    silently mangling a non-ASCII album name the other left alone. The
    packer had grown a special-case "does the real folder name have an
    apostrophe the cleaned name lost? re-derive from scratch" workaround
    for exactly this kind of divergence - one shared function makes that
    workaround unnecessary, because the two call sites can no longer
    disagree in the first place.

    Preserves parentheses, brackets and apostrophes - real album tags use
    all three ("[WEB] [192K]", "A Winter's Tale") - and \\w (Unicode-aware
    in Python 3, unlike a literal a-zA-Z0-9 class) keeps real non-ASCII
    library names intact. Everything else, spaces included, becomes "_".
    """
    cleaned = re.sub(r"[^\w\-\.\(\)\[\]']", "_", str(folder_leaf))
    return cleaned.replace(" ", "_")


def _rar_archive_disk_name(source_dir):
    """Where a packed album's archive actually lives in TMP_ZIP_DIR - built
    from the path RELATIVE TO FILE_DIRECTORY (sanitised), not just the leaf
    folder name. #162 finding #7: two different artists' albums that
    happen to share a leaf name ("Greatest Hits") used to collide into the
    SAME disk filename - and `rar a` ADDS to an existing archive rather
    than replacing it, so the second requester silently received both
    albums packed together.

    Deliberately NOT the same name a user's client sees over DCC (see
    _sanitize_rar_leaf_name() for that, built from the leaf alone and
    unchanged by this fix) - AutoQ.mrc's own reconciliation compares the
    received (de-underscored) name against the QUEUED FOLDER'S OWN
    BASENAME, so changing what the user is offered would silently break
    that matching for every existing deployment. Only WHERE the bytes
    live on disk changes; what a user is offered does not.
    """
    try:
        rel = os.path.relpath(source_dir, config.FILE_DIRECTORY)
    except ValueError:
        rel = os.path.basename(str(source_dir).rstrip("/\\"))
    rel = rel.replace("\\", "/").strip("/")
    segments = [_sanitize_rar_leaf_name(part) for part in rel.split("/") if part]
    return f"{'_'.join(segments) or 'album'}.rar"


def _is_temp_zip_cache_file(path):
    """Is `path` one of the packed .rar archives in TMP_ZIP_DIR - eligible for
    the "delete once nothing else needs it" cleanup in start_dcc_send()?

    Checked against config.TMP_ZIP_DIR's actual configured value, not a
    hardcoded directory name: a literal "tmp_zips" substring check against
    the shipped default meant an operator who renamed the setting (via
    admin_config.py or settings.conf, both documented override points)
    would have every packed .rar sent successfully and never cleaned up
    afterward - this would simply never be true again, silently.

    The ".zip" exclusion is unchanged from before this fix: TMP_ZIP_DIR also
    holds the master list's own zip, which this cleanup must never touch.
    """
    path = str(path)
    return is_safe_path(config.TMP_ZIP_DIR, path) and ".zip" not in path


def user_is_present_in_ram(user_key):
    """Is this user still in ANY of the bot's live channel lists (synced from 353/JOIN)?"""
    u = str(user_key).lower()
    with runtime.channel_users_lock():
        for users_set in getattr(config, 'channel_users', {}).values():
            for known_user in users_set:
                if str(known_user).lower() == u:
                    return True
    return False

def discard_orphaned_temp_archives(user_key):
    """Delete the temp .rar files that only `user_key`'s queue rows still name.

    MUST be called with queue_lock held and BEFORE the rows are dropped: those
    rows are the only record that the archives exist, so once they are gone the
    files sit in TMP_ZIP_DIR forever.

    Four paths delete a whole queue - the freeze-timeout sweep, the per-user
    freeze timer, !clearqueue, and the user's own @<nick>-remove / CTCP REMOVE.
    Only the first three cleaned up; REMOVE, the one users actually type, did
    not. This is that cleanup, in one place.

    Two rows are deliberately skipped:

    * is_unpacked_rar_folder rows, whose "path" is the source album directory in
      the music library, not a temp file. Deleting that would delete the music.
    * archives another queue still points at, or that a transfer is streaming
      right now. Archive names come from the FOLDER, not the user
      (dcc.py builds "{clean_folder_name}.rar"), so two people who queued the
      same album share one file on disk.

    Returns the paths actually removed, for the caller to log.
    """
    removed = []
    queue = getattr(config, 'dcc_queue', {})
    if user_key not in queue:
        return removed

    for f_obj in queue[user_key]:
        if not isinstance(f_obj, dict):
            continue
        if f_obj.get('is_temporary_zip') is not True or f_obj.get('is_unpacked_rar_folder'):
            continue
        temp_path = f_obj.get('path')
        if not temp_path or not os.path.exists(temp_path):
            continue

        still_needed = any(
            isinstance(other, dict) and other.get('path') == temp_path
            for other_key, files in queue.items() if other_key != user_key
            for other in files
        )
        if not still_needed:
            still_needed = any(tx.get('file') == f_obj.get('file')
                               for tx in getattr(config, 'active_transfers', []))
        if still_needed:
            continue

        try:
            os.remove(temp_path)
            removed.append(temp_path)
        except OSError as rm_err:
            print(f"[TEMP CLEANUP] Could not remove {temp_path}: {rm_err}")

    return removed


def release_queue_entry(user, next_file, delivered, reason=""):
    """Settle the queue row for a finished attempt. Returns True if the row was kept.

    Removal is by IDENTITY, never by position. start_dcc_send's finally used to do
    config.dcc_queue[u_key].pop(0), which removes whatever is first at that instant rather
    than the entry actually sent. Two ways that lost files:

      * The direct-send fast path builds a synthetic next_file never inserted into the
        queue, so the row at position 0 on completion is by construction a DIFFERENT,
        unsent file. It was deleted anyway.
      * The queue can be appended to or promoted while a transfer runs.

    Identity removal fixes both and needs no special case: a synthetic entry is not in the
    list, so nothing is removed and the user's real queue is left intact.

    A failed attempt no longer consumes the row, but it must not retry forever, so the
    attempt count lives ON THE ROW as 'send_fails' and is capped at config.MAX_SEND_FAILS.
    Keeping it on the row rather than in a side dict means its lifetime is exactly the
    row's: it cannot leak, cannot collide between users or same-named files, and cannot be
    inherited by a later request for the same filename.

    Two kinds of row are deliberately NOT retryable, because retrying them can only fail:
      * a consumed temporary archive - the cleanup step deletes the .rar it points at, so
        every retry would abort immediately on file_size == 0 and emit a misleading error;
      * a legacy non-dict row, which has nowhere to store a counter.
    Both are settled on their first failure.
    """
    import defaults as config
    import db

    u_key = str(user).lower()

    def _remove_by_identity():
        rows = config.dcc_queue.get(u_key)
        if not rows:
            return 0
        kept = [row for row in rows if row is not next_file]
        removed = len(rows) - len(kept)
        if removed:
            config.dcc_queue[u_key] = kept
        return removed

    is_row = isinstance(next_file, dict)
    consumed_temp = bool(is_row and next_file.get("is_temporary_zip")
                         and not next_file.get("is_unpacked_rar_folder"))
    retryable = is_row and not consumed_temp

    retained = False
    gave_up = False
    budget = getattr(config, "MAX_SEND_FAILS", 3)

    with queue_lock:
        if delivered:
            removed = _remove_by_identity()
            outcome = "delivered, " + str(removed) + " row(s) removed"
        elif not retryable:
            removed = _remove_by_identity()
            why = "temporary archive already consumed" if consumed_temp else "row is not retryable"
            outcome = "failed (" + why + "), " + str(removed) + " row(s) removed"
        else:
            attempts = int(next_file.get("send_fails", 0)) + 1
            next_file["send_fails"] = attempts
            if attempts >= budget:
                removed = _remove_by_identity()
                gave_up = True
                outcome = "failed " + str(attempts) + "/" + str(budget) + " - giving up, " + str(removed) + " row(s) removed"
            else:
                retained = True
                outcome = "failed " + str(attempts) + "/" + str(budget) + " - kept for retry"

    try:
        db.save_dcc_queue()
    except Exception as save_err:
        print("[DCC QUEUE ERROR] Could not persist the queue: " + str(save_err))

    if gave_up or (not delivered and not retained):
        # Tell the user their file was dropped. Silently discarding it is how the old
        # positional pop hid this class of failure in the first place.
        try:
            oserve_mod = sys.modules.get("oserve")
            dropped = next_file.get("file", "your file") if is_row else str(next_file)
            if oserve_mod:
                oserve_mod.queue_message(
                    user,
                    "NOTICE " + str(user) + " :" + config.C_BOLD + "Error" + config.C_RESET +
                    ": Could not send " + str(dropped) + " (" + str(reason) + "). Removed from your queue.\r\n")
        except Exception as notify_err:
            print("[DCC QUEUE] Could not notify " + str(user) + ": " + str(notify_err))

    print("[DCC QUEUE] " + str(user) + ": " + outcome + ((" (" + reason + ")") if reason else "") + ".")
    return retained


def get_total_queued_count():
    """The total number of files sitting in every personal queue right now."""
    total = 0
    for user_key, files in config.dcc_queue.items():
        total += len(files)
    return total

def get_public_ip_long():
    """Convert config.MY_IP_OR_DOCK into the mIRC-compatible long format, or 0
    if it is blank or not a dotted-quad at all.

    Deliberately a pure converter, and it stays one. An earlier version of this
    fix folded the "is this address any use to a remote client?" question in
    here, which broke the admin console: adminchat.py's DCC CHAT listen-back
    uses the same value, and an operator connecting from the same machine or
    the same LAN has a loopback or private address that is entirely correct for
    that purpose. Only the file-transfer path needs the stricter rule, so the
    stricter rule lives there - see is_offerable_to_strangers() below.
    """
    text = str(getattr(config, "MY_IP_OR_DOCK", "") or "").strip()
    if not text:
        return 0
    try:
        parts = text.split('.')
        if len(parts) == 4:
            return (int(parts[0]) << 24) + (int(parts[1]) << 16) + (int(parts[2]) << 8) + int(parts[3])
    except Exception as e:
        print(f"[DCC IP ERROR] Could not convert the IP to long form: {e}")
    return 0


def is_offerable_to_strangers(ip_text=None):
    """Can a stranger on a public IRC network actually dial this address?

    #162 follow-up, found by the pre-publication audit. irc.py's connect used to
    fall back to "127.0.0.1" whenever the ipify lookup failed, and the only
    guard on the send path was `if ip_long == 0`. 127.0.0.1 converts to
    2130706433, which is not 0, so nothing caught it: the bot accepted every
    request, told each user "Active Transfer Started", held a DCC slot, and sent
    every leecher to their own loopback. One warning at boot was the only clue,
    and the queue drained into failed transfers.

    Loopback, private, link-local, multicast, reserved and unspecified are all
    unreachable from the far side of a public network, so an offer carrying one
    is an offer to nobody. Refusing with a message the operator can act on beats
    a transfer that silently never happens.

    An operator serving a LAN pins MY_IP_OR_DOCK, and irc.py now uses a pinned
    value verbatim without the lookup - but the offer still has to be dialable
    by whoever receives it, so this applies either way.
    """
    import ipaddress

    text = str(ip_text if ip_text is not None
               else getattr(config, "MY_IP_OR_DOCK", "") or "").strip()
    if not text:
        return False
    try:
        address = ipaddress.IPv4Address(text)
    except Exception:
        return False
    return not (address.is_loopback or address.is_private or address.is_link_local
                or address.is_multicast or address.is_reserved or address.is_unspecified)


def next_waiting_pack_owner(exclude_user=None):
    """The user whose queue HEAD is a folder pack, in arrival order, or None.

    check_queue_and_send() only ever looks at the queue of the user handed to
    it, and every caller hands it the user whose transfer just finished. A
    second user turned away at the [RAR-HOLD] branch - because someone else's
    pack held config.rar_inprogress - was therefore never revisited: their row
    stayed queued until they happened to complete some unrelated transfer of
    their own, which on a bot serving one album at a time may be never.

    Arrival order is dict insertion order on config.dcc_queue. Queue entries
    carry no timestamp, and insertion order is what the rest of the queue
    already treats as arrival order, so this matches rather than invents.

    HEAD only, not "anywhere in their queue": check_queue_and_send() dispatches
    entries[0] and nothing else, so a pack sitting behind a plain file is not
    dispatchable yet and waking that user would be a no-op that looks like a
    fix.
    """
    exclude = (exclude_user or "").lower()
    with queue_lock:
        for user_key, entries in config.dcc_queue.items():
            if user_key == exclude or not entries:
                continue
            head = entries[0]
            if isinstance(head, dict) and head.get('is_unpacked_rar_folder') is True:
                return head.get('user_raw') or user_key
    return None


def redispatch_waiting_pack(irc_sock, just_finished=None):
    """Wake the next queued folder pack, once rar_inprogress has been released.

    Called from every path that clears rar_inprogress. Returns the user woken,
    or None - which the tests assert on, and which is also the honest answer
    when there was nothing to wake.

    Re-checks rar_inprogress first: these callers clear it a line or two
    earlier, but a concurrent trigger may already have claimed it, and starting
    a second packer would defeat the interlock this whole branch exists to
    hold.

    Dispatched on a thread rather than called inline. check_queue_and_send()
    takes queue_lock, and threading.Lock is not reentrant - the scan above
    releases it before returning, but the send path this leads into is long
    (it packs an album), and running it inline would also make one completion
    recurse into the next.
    """
    if getattr(config, 'rar_inprogress', False):
        return None
    owner = next_waiting_pack_owner(exclude_user=just_finished)
    if not owner:
        return None
    print(f"[RAR-WAKE] A pack for {owner} was waiting on the packer lock; dispatching it now.")
    threading.Thread(target=check_queue_and_send, args=(irc_sock, owner),
                     daemon=True).start()
    return owner

def check_queue_and_send(irc_sock, completed_user):
    """Check the queues and run RAR packing one at a time, without flooding the server."""
    import announce as announce_mod
    import subprocess
    import threading
    import socket
    import sys
    import os
    import re
    import time
    import defaults as config
    import db
    
    user_key = completed_user.lower()
    oserve = sys.modules.get('oserve')
    
    # 1. Sweep away frozen queues older than five minutes
    # The sweep may ONLY run once the bot itself is fully channel-synced.
    # During a reconnect channel_users is empty, and the old sweep then deleted queues
    # belonging to users who had never left the channel.
    if getattr(config, 'bot_joined_channel', False):
        with queue_lock:
            current_time = time.time()
            for f_user, freeze_timestamp in list(config.frozen_queues.items()):
                # THAW: the user is back in memory - release the freeze instead of deleting
                if user_is_present_in_ram(f_user):
                    del config.frozen_queues[f_user]
                    print(f"[DCC FREEZE-THAW] {f_user} is back in the channel list. Their queue was saved.")
                    continue
                if (current_time - freeze_timestamp) > 300.0:
                    if f_user in config.dcc_queue:
                        for f_obj in config.dcc_queue[f_user]:
                            if isinstance(f_obj, dict) and f_obj.get('is_temporary_zip') is True and os.path.exists(f_obj['path']) and not f_obj.get('is_unpacked_rar_folder'):
                                try: os.remove(f_obj['path'])
                                except: pass
                        del config.dcc_queue[f_user]
                        db.save_dcc_queue()
                    if f_user in config.frozen_queues:
                        del config.frozen_queues[f_user]
                    print(f"[DCC QUEUE_CLEAN] {f_user} rensad permanent pga timeout.")

    if user_key == "system_next_trigger_fallback":
        user_key = ""

    next_file = None
    with queue_lock:
        if user_key and user_key in config.dcc_queue and config.dcc_queue[user_key]:
            if user_key not in config.frozen_queues:
                next_file = config.dcc_queue[user_key][0]  # FIXED: takes the top entry


    if next_file:
        if isinstance(next_file, dict):
            target_chan = next_file.get('channel', config.CHANNEL.split(','))
        else:
            target_chan = config.CHANNEL.split(',')
        
        user_is_actively_in_channel = False
        
        # Case-folding and system-trigger bypass:
        # if completed_user is the system trigger, OR the user was just rehashed, 
        # open the gate fully, to clear any silent case-sensitivity blocks.
        if "system_next_trigger_fallback" in [str(completed_user).lower(), str(user_key)]:
            user_is_actively_in_channel = True
        else:
            with runtime.channel_users_lock():
                if hasattr(config, 'channel_users'):
                    for chan_name, users_set in config.channel_users.items():
                        lowered_channel_users = [u.lower() for u in users_set]
                        if user_key in lowered_channel_users or str(completed_user).lower() in lowered_channel_users:
                            user_is_actively_in_channel = True
                            break
            
        if user_is_actively_in_channel is True:
            # ---------------------------------------------------------------------
            # The folder packer runs strictly one at a time, inside the send gate
            # ---------------------------------------------------------------------
            if isinstance(next_file, dict) and next_file.get('is_unpacked_rar_folder') is True:
                # FIXED (issue #27, RAR branch): this check-then-set had the same gap as
                # section A's plain-file dispatch before that fix - the user_processing_lock
                # check and the rar_inprogress check both ran, then BOTH interlocks got set,
                # all outside queue_lock. Two overlapping triggers could each read both flags
                # as clear before either claimed them, both start packing/sending the same
                # folder for the same user. Mirror the plain-file fix: check and claim both
                # interlocks atomically under queue_lock.
                with queue_lock:
                    # #162 finding #23: this branch had no capacity check at
                    # all, unlike the plain-file branch a few lines below,
                    # which already re-checks capacity inside this same
                    # lock. rar_inprogress bounds concurrent PACKS to one,
                    # but a pack's own SEND afterwards is a normal DCC slot
                    # like any other - with no check here, that one send
                    # could still push active_transfers one past
                    # MAX_DCC_SLOTS. Same check, same message, as the sibling
                    # plain-file branch below already has.
                    if len(config.active_transfers) >= config.MAX_DCC_SLOTS:
                        print(f"[DCC-BLOCK] {completed_user}: all {config.MAX_DCC_SLOTS} slot(s) busy, leaving queued for the next trigger.")
                        return

                    user_already_locked = (
                        hasattr(config, 'user_processing_lock')
                        and completed_user.lower() in config.user_processing_lock
                    )
                    pack_in_progress = getattr(config, 'rar_inprogress', False)

                    if user_already_locked:
                        print(f"[RAR-BLOCK] {completed_user} is already locked in memory; blocking a stale thread.")
                        return
                    if pack_in_progress:
                        print(f"[RAR-HOLD] {completed_user} waits in the queue while another packing run is in progress...")
                        return

                    config.rar_inprogress = True
                    if not hasattr(config, 'user_processing_lock'):
                        config.user_processing_lock = set()
                    config.user_processing_lock.add(completed_user.lower())

                def inline_rar_packer(sock):
                    # This runs with config.rar_inprogress already True and the user held in
                    # config.user_processing_lock. Both are PROCESS-WIDE interlocks, and
                    # nothing here released them on an unexpected failure - subprocess.run
                    # raising (rar missing, disk full, timeout), os.makedirs failing, or
                    # getsize on a vanished archive would all leave rar_inprogress latched
                    # True, silently disabling folder packing for every user until the
                    # daemon was restarted. Not even !rehash cleared it.
                    # handed_off: the body returns True once it has transferred ownership of
                    # config.rar_inprogress / user_processing_lock to another thread. Releasing
                    # them here in that case would un-serialise packing from sending - the very
                    # thing the interlocks exist to guarantee - so the finally only releases
                    # what this call still owns.
                    handed_off = False
                    try:
                        handed_off = _inline_rar_packer_body(sock)
                    except Exception as packer_err:
                        print("[LINJAR RAR ERROR] Packing failed for " + str(completed_user) + ": " + str(packer_err))
                        try:
                            announce_mod.send_pack_error_notice(sock, completed_user)
                        except Exception:
                            pass
                        release_queue_entry(completed_user, next_file, delivered=False,
                                            reason="pack failed: " + str(packer_err))
                    finally:
                        if not handed_off:
                            config.rar_inprogress = False
                            # #215: this release is the only moment another user's held pack can
                            # start. Nothing else revisits them - every check_queue_and_send()
                            # caller passes the user who just finished, never the one turned
                            # away at [RAR-HOLD].
                            redispatch_waiting_pack(irc_sock, just_finished=completed_user)
                            if hasattr(config, 'user_processing_lock'):
                                config.user_processing_lock.discard(completed_user.lower())

                def _inline_rar_packer_body(sock):
                    true_source_dir = next_file['path']

                    # SECOND LINE OF DEFENCE: queue entries survive restarts via dcc_queue.txt,
                    # so a poisoned row queued BEFORE the traversal guard existed would otherwise
                    # still be packed here. Re-verify the path immediately before calling rar.
                    if not is_safe_path(config.FILE_DIRECTORY, true_source_dir):
                        print(f"[SECURITY] Blocked a poisoned queue entry for {completed_user}: {true_source_dir}")
                        with queue_lock:
                            if completed_user.lower() in config.dcc_queue:
                                config.dcc_queue[completed_user.lower()] = [
                                    e for e in config.dcc_queue[completed_user.lower()] if e is not next_file
                                ]
                        db.save_dcc_queue()
                        config.rar_inprogress = False
                        # #215: this release is the only moment another user's held pack can
                        # start. Nothing else revisits them - every check_queue_and_send()
                        # caller passes the user who just finished, never the one turned
                        # away at [RAR-HOLD].
                        redispatch_waiting_pack(irc_sock, just_finished=completed_user)
                        if hasattr(config, 'user_processing_lock'):
                            config.user_processing_lock.discard(completed_user.lower())
                        announce_mod.send_debug(
                            f"Poisoned queue entry discarded for {config.C_BOLD}{completed_user}{config.C_RESET}: path outside the music root.",
                            category="HARDBAN")
                        return

                    # The DCC-visible name: recomputed fresh from the folder
                    # leaf on disk, with the SAME sanitiser
                    # handle_download_request() used when this row was
                    # queued (see _sanitize_rar_leaf_name()'s own docstring).
                    # One shared function is what makes the apostrophe-
                    # recovery special case this replaced unnecessary - the
                    # two call sites can no longer disagree in the first
                    # place, so there is nothing left to detect and patch
                    # over here.
                    folder_leaf = os.path.basename(true_source_dir.rstrip('/\\'))
                    rar_filename = f"{_sanitize_rar_leaf_name(folder_leaf)}.rar"

                    # #162 finding #7: the archive's DISK location is
                    # collision-resistant - built from the path RELATIVE TO
                    # FILE_DIRECTORY, not the leaf alone - so two different
                    # artists' albums sharing a leaf name ("Greatest Hits")
                    # can no longer collide into the same file. Deliberately
                    # NOT the same string as rar_filename above; see
                    # _rar_archive_disk_name()'s own docstring for why
                    # changing what the user is OFFERED would break
                    # AutoQ.mrc's reconciliation.
                    target_rar_path = os.path.normpath(
                        os.path.join(config.TMP_ZIP_DIR, _rar_archive_disk_name(true_source_dir)))

                    if not os.path.exists(config.TMP_ZIP_DIR):
                        os.makedirs(config.TMP_ZIP_DIR, exist_ok=True)

                    # Strip any hidden line breaks (\n) out of the path
                    if isinstance(true_source_dir, str):
                        true_source_dir = true_source_dir.strip()

                    # `rar a` ADDS to an existing archive rather than
                    # replacing it - a stale file left behind by an earlier
                    # crashed run would otherwise silently have the new
                    # album packed on TOP of whatever was already there.
                    # Removed first so a fresh pack always starts from
                    # nothing, regardless of what used to be at this path.
                    long_target = platform_compat.long_path(target_rar_path)
                    if os.path.exists(long_target):
                        try:
                            os.remove(long_target)
                        except OSError as unlink_err:
                            print(f"[LINEAR RAR] Could not remove a stale archive at "
                                  f"{target_rar_path}: {unlink_err}")

                    print(f"[LINEAR RAR] Starting to pack: {true_source_dir} -> {target_rar_path}")


                    # Arguments are passed as a list, never through a shell:
                    work_dir_switch = f"-w{os.path.abspath(config.TMP_ZIP_DIR)}"
                    # Resolve the binary rather than trusting a bare name on PATH:
                    # WinRAR installs rar.exe outside PATH entirely.
                    rar_bin = platform_compat.rar_command(getattr(config, 'RAR_BINARY', None))
                    if not rar_bin:
                        raise FileNotFoundError(
                            "rar executable not found - set config.RAR_BINARY or put rar on PATH")
                    cmd = [rar_bin, "a", "-ep1", work_dir_switch, os.path.abspath(target_rar_path), os.path.abspath(true_source_dir)]
                    # A timeout is essential: with timeout=None a hung rar blocks this
                    # thread forever while config.rar_inprogress stays True, wedging folder
                    # packing for EVERY user until the daemon is restarted.
                    rar_timeout = getattr(config, 'RAR_TIMEOUT', 1800)
                    process = subprocess.run(cmd, capture_output=True, text=True, timeout=rar_timeout)
                    
                    if process.returncode == 0 and os.path.exists(target_rar_path):
                        print(f"[LINEAR RAR] Compression succeeded. Waiting 2.0s for the disk to sync...")
                        time.sleep(2.0)
                        
                        final_size = os.path.getsize(target_rar_path)
                        print(f"[LINEAR RAR] The archive is settled on disk: {final_size:,} bytes")
                        
                        next_file['path'] = target_rar_path
                        next_file['file'] = rar_filename
                        next_file['is_unpacked_rar_folder'] = False
                        
                        config.active_transfers.append({"user": completed_user, "file": rar_filename, "bytes_sent": 0, "next_file_obj": rar_filename})
                        if oserve: oserve.active_downloads = len(config.active_transfers)
                        
                        announce_mod.send_dcc_sending_notice(completed_user, rar_filename)
                        
                        threading.Thread(
                            target=start_dcc_send, 
                            args=(sock, completed_user, target_rar_path, rar_filename, target_chan, next_file), 
                            daemon=True
                        ).start()
                        # Ownership of both interlocks now belongs to that send thread, which
                        # releases them in its own finally. Pack and send stay serialised.
                        return True
                    else:
                        error_msg = process.stderr.strip() if process.stderr else "Unknown RAR engine issue"
                        print(f"[LINJAR RAR ERROR] {error_msg}")
                        announce_mod.send_debug(f"Pack FAILED in queue slot for {completed_user}: {error_msg}", category="PART")
                        # Charge the failure to the retry budget instead of recursing. The old
                        # code cleared the interlocks and called check_queue_and_send inline,
                        # which re-selected the SAME row and started another packer thread from
                        # inside this one - unbudgeted, and the wrapper finally would then strip
                        # the interlocks that new thread had just claimed.
                        release_queue_entry(completed_user, next_file, delivered=False,
                                            reason="rar exited " + str(process.returncode))
                        return False

                # At exactly the right level, so it wakes the function above immediately
                threading.Thread(target=inline_rar_packer, args=(irc_sock,), daemon=True).start()
                return

             # Plain audio file (.mp3/.flac), not a RAR folder pack.
            else:
                # FIXED (issue #27): section B already has admission control for this exact
                # race (see the already_sending check below in section B), but section A's
                # plain-file branch never got it. check_queue_and_send() can be invoked for
                # the SAME user from multiple independent triggers close together - the 3s
                # delayed_queue_trigger_fallback after every completed send, a JOIN/353
                # thaw, and !rehash's system trigger - and the initial next_file read above
                # only holds queue_lock for that one read, not for the channel-membership
                # check and dispatch that follow. Two overlapping calls could both read the
                # same queue head, both pass the channel check, and both start an
                # independent start_dcc_send for the identical file to the identical user:
                # one DCC handshake completes, the other times out or arrives as 0 bytes on
                # the leech side. Claim the user atomically, inside the lock, before
                # dispatching - start_dcc_send's finally already discards this key on every
                # exit path.
                with queue_lock:
                    # FIXED (issue #33): section B already re-checks capacity inside the
                    # lock; section A never checked it at all. check_queue_and_send() is also
                    # invoked for users who are NOT the one who just finished - a JOIN/353
                    # thaw, the freeze-abort timer, !rehash's system trigger - so repeated
                    # triggers could push active_transfers past MAX_DCC_SLOTS with no ceiling,
                    # and oserve.active_downloads (the advertised slot count) followed it over.
                    if len(config.active_transfers) >= config.MAX_DCC_SLOTS:
                        print(f"[DCC-BLOCK] {completed_user}: all {config.MAX_DCC_SLOTS} slot(s) busy, leaving queued for the next trigger.")
                        return

                    already_claimed = (
                        hasattr(config, 'user_processing_lock')
                        and completed_user.lower() in config.user_processing_lock
                    ) or any(
                        str(tx.get('user', '')).lower() == user_key
                        for tx in config.active_transfers
                    )

                    if already_claimed:
                        print(f"[DCC-BLOCK] {completed_user} is already claimed elsewhere; skipping duplicate dispatch.")
                        return

                    if not hasattr(config, 'user_processing_lock'):
                        config.user_processing_lock = set()
                    config.user_processing_lock.add(completed_user.lower())

                    f_name = next_file['file'] if isinstance(next_file, dict) else os.path.basename(str(next_file))
                    f_path = next_file['path'] if isinstance(next_file, dict) else str(next_file)
                    config.active_transfers.append({"user": completed_user, "file": f_name, "bytes_sent": 0, "next_file_obj": f_name})

                print(f"[DCC QUEUE] Verified live in RAM for {target_chan}! Next file for {completed_user}: {f_name}")
                if oserve: oserve.active_downloads = len(config.active_transfers)

                announce_mod.send_dcc_sending_notice(completed_user, f_name)
                threading.Thread(target=start_dcc_send, args=(irc_sock, completed_user, f_path, f_name, target_chan, next_file), daemon=True).start()
                return
        else:
            # -----------------------------------------------------------------
            # NEVER freeze a queue while the bot itself is off the network.
            # On a netsplit or reconnect channel_users is empty or half-synced, so we do
            # not KNOW whether the user left. Leave the queue alone until NAMES has synced.
            # -----------------------------------------------------------------
            if not getattr(config, 'bot_joined_channel', False) or not getattr(config, 'channel_users', None):
                print(f"[DCC FREEZE-SKIP] The bot is not channel-synced yet. Leaving {completed_user}'s queue untouched.")
                return

            # A user may have exactly ONE countdown running at a time.
            if user_key in getattr(config, 'frozen_queues', {}):
                print(f"[DCC FREEZE-HOLD] {completed_user} already has a countdown running. Not starting another.")
                return

            with queue_lock:
                config.frozen_queues[user_key] = time.time()
            print(f"[DCC REACTIVE FREEZE] {completed_user} really has left {target_chan}. Starting the timer...")
            announce_mod.send_debug(f"DCC reactive freeze triggered for {completed_user} in {target_chan}. Initiating 5-minute cooldown timer.", category="QUIT")
            
            def user_queue_timer(sock, target_user, original_chan):
                """A verifying countdown, replacing the old blind 300-second sleep.
                The clock pauses entirely while the bot is disconnected - the bot's own
                downtime must NEVER count against a user's queue - and the countdown
                aborts as soon as the user reappears via JOIN or a NAMES sync."""
                t_key = target_user.lower()
                elapsed = 0
                
                while elapsed < 300:
                    time.sleep(10)
                    
                    # A) Something else already thawed the queue (JOIN / NAMES / !rehash)
                    if t_key not in getattr(config, 'frozen_queues', {}):
                        print(f"[DCC FREEZE-ABORT] {target_user} is already thawed. The countdown stops; the queue is safe.")
                        return
                        
                    # B) The bot itself is offline - freeze the clock, do NOT advance elapsed
                    if not getattr(config, 'bot_joined_channel', False):
                        print(f"[DCC FREEZE-PAUSE] The bot is off the network. Pausing {target_user}'s countdown at {elapsed}s.")
                        continue
                        
                    # C) The bot is back online - check against the fresh channel list
                    if user_is_present_in_ram(t_key):
                        with queue_lock:
                            config.frozen_queues.pop(t_key, None)
                        print(f"[DCC FREEZE-ABORT] {target_user} was found in the channel list. The queue is kept and woken.")
                        announce_mod.send_debug(f"Queue for {config.C_BOLD}{target_user}{config.C_RESET} preserved - user verified back in channel before timeout.", category="JOIN")
                        threading.Thread(target=check_queue_and_send, args=(sock, target_user), daemon=True).start()
                        return
                        
                    elapsed += 10
                
                if hasattr(config, 'frozen_queues') and t_key in config.frozen_queues:
                    with queue_lock:
                        if t_key in config.dcc_queue:
                            for f_obj in config.dcc_queue[t_key]:
                                if isinstance(f_obj, dict) and f_obj.get('is_temporary_zip') is True and os.path.exists(f_obj['path']) and not f_obj.get('is_unpacked_rar_folder'):
                                    try: os.remove(f_obj['path'])
                                    except: pass
                            del config.dcc_queue[t_key]
                            db.save_dcc_queue()
                        del config.frozen_queues[t_key]
                    announce_mod.send_debug(f"Timer expired for {target_user} in {original_chan}. Personal queue has been erased.", category="PART")
                    
            threading.Thread(target=user_queue_timer, args=(irc_sock, completed_user, target_chan), daemon=True).start()
            return

    # =====================================================================
    # B) Global queue handling for the next person in line, across all slots
    # =====================================================================
    if oserve:
        oserve.active_downloads = len(config.active_transfers)
        
    if len(config.active_transfers) < config.MAX_DCC_SLOTS:
        with queue_lock:
            # FIXED: re-check the slot count INSIDE the lock. The test above is already
            # stale by the time we acquire, so two concurrent callers could both pass it
            # and overshoot MAX_DCC_SLOTS.
            if len(config.active_transfers) >= config.MAX_DCC_SLOTS:
                return

            for waiting_user, user_files in list(config.dcc_queue.items()):
                # Use the dcc_queue dict key for every lock/queue operation. The old code
                # tested the guards with one key and then rebound w_key to the display
                # name further down, so the guard and the claim could disagree.
                queue_key = str(waiting_user).lower()

                if hasattr(config, 'user_processing_lock') and queue_key in config.user_processing_lock:
                    continue

                if not user_files or len(user_files) == 0 or queue_key in config.frozen_queues:
                    continue

                # FIXED (issue #4): user_files is the LIST of this user's queued files, not a
                # single file. Without [0] the isinstance test below was always true (a list is
                # never a dict), so every waiting user was skipped and section B was dead code.
                g_next = user_files[0]
                if not isinstance(g_next, dict):
                    continue

                # FIXED: section B had no admission control at all. It was dead code until the
                # [0] fix above woke it up, and nothing had ever audited what it does when it
                # actually runs. It never claimed the entry, so two overlapping triggers - the
                # 3s fallback fires after EVERY transfer, plus the 353 and JOIN thaws - both
                # promoted the same queue head. The user received two DCC offers for one file,
                # two slots were burned on it, and then both finally blocks popped position 0:
                # the first removed the file that was sent, the second removed the NEXT file,
                # which had never been sent. Silent loss, persisted straight to dcc_queue.txt.
                already_sending = any(
                    str(tx.get('user', '')).lower() == queue_key
                    for tx in config.active_transfers
                )
                if already_sending:
                    continue

                real_username = g_next.get('user_raw', waiting_user)

                g_chan = g_next.get('channel', config.CHANNEL.split(','))
                g_name = g_next.get('file', '')
                g_path = g_next.get('path', '')

                user_is_globally_active = False
                if isinstance(g_chan, str):
                    channels_to_check = [g_chan]
                elif isinstance(g_chan, list):
                    channels_to_check = g_chan
                else:
                    channels_to_check = config.CHANNEL.split(',')

                with runtime.channel_users_lock():
                    if hasattr(config, 'channel_users'):
                        for single_chan in channels_to_check:
                            n_chan = str(single_chan).strip().lower()
                            if n_chan in config.channel_users:
                                lowered_glob_users = [u.lower() for u in config.channel_users[n_chan]]
                                if queue_key in lowered_glob_users:
                                    user_is_globally_active = True
                                    break

                if user_is_globally_active is True:
                    if g_next.get('is_unpacked_rar_folder') is True:
                        # FIXED: this was `break`, which abandoned the whole scan. One user
                        # waiting on a RAR pack starved every other waiting user behind them
                        # for as long as the pack took. Skip this user and keep looking.
                        print(f"[DCC QUEUE] Folder pack already pending for {real_username}. Skipping to the next waiting user.")
                        continue

                    # CLAIM the user before releasing the lock, so a concurrent caller sees
                    # them as busy. start_dcc_send's finally already discards this key on
                    # every exit path, including the early aborts.
                    if not hasattr(config, 'user_processing_lock'):
                        config.user_processing_lock = set()
                    config.user_processing_lock.add(queue_key)

                    print(f"[DCC QUEUE] New user {real_username} verified live in RAM for {g_chan}. Got slot.")
                    config.active_transfers.append({"user": real_username, "file": g_name, "bytes_sent": 0, "next_file_obj": g_name})
                    if oserve: oserve.active_downloads = len(config.active_transfers)

                    announce_mod.send_dcc_sending_notice(real_username, g_name)
                    threading.Thread(target=start_dcc_send, args=(irc_sock, real_username, g_path, g_name, g_chan, g_next), daemon=True).start()
                    break


def handle_download_request(irc_sock, user, requested_file, target_chan):
    """Runs when somebody requests a file, or a whole folder via !rar."""
    # ---------------------------------------------------------------------
    # target_chan becomes this request's queue row "channel" a few lines
    # down, and that stored value is what announce.py later builds an
    # outbound "PRIVMSG {channel} :Sent: ..." line from on completion - so
    # it is re-validated here, at the one place it gets persisted, even
    # though irc.py's own PRIVMSG parser is already anchored so target_chan
    # can only ever be the literal wire target of this message. It must be
    # a channel we are actually configured to be in, or our own nick (a
    # private request); anything else can only mean a parsing bug
    # upstream, and is refused rather than trusted with a real advert line.
    configured_channels = {c.strip().lower() for c in str(getattr(config, 'CHANNEL', '')).split(',') if c.strip()}
    if (str(target_chan).lower() not in configured_channels
            and str(target_chan).lower() != str(getattr(config, 'NICKNAME', '')).lower()):
        print(f"[SECURITY] Refused a download request from {user}: "
              f"target_chan {target_chan!r} is not a channel this bot is in.")
        return
    # ---------------------------------------------------------------------
    # The global maintenance gate:
    # ---------------------------------------------------------------------
    # update_inprogress, NOT search_inprogress (#214). Both are set by !update,
    # but search_inprogress is ALSO set by every @find for the seconds it reads
    # the list - so any search refused every file request from everyone else,
    # and told them "MasterList is currently rebuilding", which was untrue. A
    # search only READS the list; it is the rebuild that replaces the files
    # underneath a transfer, and only the rebuild needs this gate.
    #
    # A real rebuild is unaffected: !update sets update_inprogress
    # unconditionally and search_inprogress only when PAUSE_ON_UPDATE is on,
    # so gating on update_inprogress behind the same switch refuses exactly
    # what it refused before.
    if getattr(config, 'PAUSE_ON_UPDATE', True) is True and getattr(config, 'update_inprogress', False) is True:
        oserve = sys.modules.get('oserve')
        if oserve:
            oserve.queue_message(user, f"NOTICE {user} :{config.C_BOLD}System Message{config.C_RESET}: MasterList is currently rebuilding. File requests temporarily paused. Please wait 1-2 minutes.\r\n")
        print(f"[MAINTENANCE BLOCK] Refused a file request from {user}: an !update is running.")
        return
    # ---------------------------------------------------------------------

    oserve = sys.modules.get('oserve')
    try:
        user_key = str(user).lower().strip()
        if oserve:
            oserve.active_downloads = len(config.active_transfers)
        print(f"[DCC] {user} requested: {requested_file}")

        # ---------------------------------------------------------------------
        # Asynchronous folder packing: the !rar gate, with root and NFS guards
        # ---------------------------------------------------------------------
        if requested_file.lower().startswith("!rar "):
            import announce as announce_mod

            if not getattr(config, 'RAR_ENABLED', True):
                announce_mod.send_dcc_error(user, "rar_disabled")
                return

            # FILE_DIRECTORY is deliberately not in settings_file.REQUIRED any
            # more (#184's review) - the daemon can be up and
            # answering requests before an operator has chosen a music
            # directory. Every album this branch packs lives under it, so
            # checked here, explicitly, rather than letting
            # list_mod.resolve_list_folder()/is_safe_path() below fail on a
            # None base and fall through to the bare except at the bottom of
            # this function, which told the requester nothing at all.
            if not config.FILE_DIRECTORY:
                announce_mod.send_dcc_error(user, "not_configured")
                return

            raw_win_path = requested_file[5:].strip()
            
            # Trim any leftovers, in case somebody pasted an old row
            if "::INFO::" in raw_win_path:
                raw_win_path = raw_win_path.split("::INFO::")[0].strip()
                
            win_path = re.sub(r'\s*\[[^\]]+\]$', '', raw_win_path).strip()

            # This used to be a third, differently-shaped copy of the same
            # "D:\MUSIC\<folder>\" prefix-stripping list.resolve_list_folder()
            # already does - non-anchored `.replace("D:/", "")` calls rather
            # than a startswith-anchored strip, which would have silently
            # matched "D:/" anywhere in the string, not just at the start.
            # os.path.normpath is kept even though resolve_list_folder()
            # itself doesn't call it: is_safe_path() below re-resolves the
            # path with os.path.realpath() regardless, but this is the
            # traversal guard's input and there is no reason to change its
            # exact shape while consolidating the prefix logic.
            true_source_dir = os.path.normpath(
                list_mod.resolve_list_folder(win_path, base=config.FILE_DIRECTORY))

            # ---------------------------------------------------------------------
            # THE TRAVERSAL GUARD - this one is critical:
            # without this check, anyone in the channel could type
            # "!rar ../../root/.ssh" and have that whole directory packed and sent
            # to them. os.path.normpath eats every "..", and the old root guard
            # below let through anything that contained a slash.
            # So the FINAL path is verified to still be inside the music directory
            # before anything else happens.
            # ---------------------------------------------------------------------
            if not is_safe_path(config.FILE_DIRECTORY, true_source_dir):
                print(f"[SECURITY] Blocked a traversal attempt from {user}: {raw_win_path!r} -> {true_source_dir}")
                announce_mod.send_pack_error_notice(irc_sock, user)
                announce_mod.send_debug(
                    f"Path traversal denied for {config.C_BOLD}{user}{config.C_RESET}: request resolved outside the music root.",
                    category="HARDBAN")
                return

            # An artist root (one path segment under FILE_DIRECTORY, no album
            # subfolder) rather than an actual album folder. relpath() rather
            # than the old hand-built linux_sub_path - same question, asked
            # of the path resolve_list_folder() already produced.
            relative_to_root = os.path.relpath(true_source_dir, config.FILE_DIRECTORY)
            if os.sep not in relative_to_root:
                print(f"[SECURITY] Blocked an attempt to pack the root folder from {user}: {relative_to_root}")
                announce_mod.send_pack_error_notice(irc_sock, user)
                announce_mod.send_debug(f"Pack denied for {user}: {config.C_BOLD}{relative_to_root}{config.C_RESET} is an artist root folder.", category="PART")
                return
            
            if not os.path.exists(platform_compat.long_path(true_source_dir)) or not os.path.isdir(platform_compat.long_path(true_source_dir)):
                announce_mod.send_debug(f"Pack error: Directory not found on disk storage for {user}.", category="PART")
                return

            with queue_lock:
                total_global_queued = get_total_queued_count()
                user_queued_count = len(config.dcc_queue.get(user_key, []))

                if total_global_queued >= config.MAX_GLOBAL_QUEUE:
                    announce_mod.send_dcc_error(user, "global_full")
                    return

                if user_queued_count >= config.MAX_USER_QUEUE:
                    announce_mod.send_dcc_error(user, "user_full")
                    return

                if user_key not in config.dcc_queue:
                    config.dcc_queue[user_key] = []

                # ---------------------------------------------------------------------
                # Album naming, kept AutoQ-compatible with the parentheses AND
                # brackets preserved
                # ---------------------------------------------------------------------
                folder_name = os.path.basename(true_source_dir.rstrip("/"))

                # Allows parentheses, square brackets and ordinary hyphens, so
                # AutoQ.mrc can match the filename. AutoQ's own filercvd handler
                # reconciles a completed .rar against the folder path it queued
                # by de-underscoring the received name (swapping "_" back to " ")
                # and comparing it to that path's basename - it never touches
                # brackets. A folder tagged "[WEB] [192K]" used to lose those
                # brackets here while AutoQ still expected them on the other
                # side of that comparison, so the two strings never matched and
                # the request stayed listed as outstanding in AutoQ's queue
                # window forever, even though the transfer itself completed
                # correctly every time.
                #
                # _sanitize_rar_leaf_name() is the SAME function the packer
                # itself now uses (see its own docstring) - one definition,
                # so the name queued here and the name the packer eventually
                # produces can no longer silently disagree.
                clean_folder_name = _sanitize_rar_leaf_name(folder_name)

                master_rar_filename = f"{clean_folder_name}.rar"


                config.dcc_queue[user_key].append({
                    "file": master_rar_filename, # The clean name is what gets written to dcc_queue.txt
                    "path": true_source_dir,
                    "channel": target_chan,
                    "user_raw": user,
                    "is_unpacked_rar_folder": True,
                    "is_temporary_zip": True
                })
                import db
                db.save_dcc_queue()  # Commit straight to dcc_queue.txt
                
                user_pos = len(config.dcc_queue[user_key])
                print(f"[RAR QUEUE] Added virtuell mapp {master_rar_filename} for {user} at position #{user_pos}.")
                
                # One single clean line to the debug channel, nothing more
                announce_mod.send_debug(f"{user} requested \"{clean_folder_name}\". Starting rar and sending when done.", category="INFO")
                
                announce_mod.send_dcc_queue_notice(user, folder_name, user_pos)
                threading.Thread(target=check_queue_and_send, args=(irc_sock, user), daemon=True).start()
            return


        # A request copied straight off a search result carries its own
        # "  ::INFO:: <size>" tail (list.py's own two-space convention, but
        # strip_info_suffix() tolerates any spacing and any cross-bot
        # branding after the marker too). That size is the one thing that
        # tells two identically-named copies apart, so it is kept as a hint
        # for the list scan below instead of being thrown away.
        requested_file, requested_size_hint = list_mod.strip_info_suffix(requested_file)
        requested_size_hint = requested_size_hint.lower().strip()

        requested_file = str(requested_file).lstrip("/")

        # The master list lives in LOCAL_LIST_DIR, everything else in the
        # music directory. Matched on the names the list builder writes rather
        # than on ".zip" plus the base name appearing anywhere: with .rar now
        # a list format too, a shared library file called
        # "Someone - DCCore Sessions.rar" would otherwise be looked for among
        # the lists and never found.
        if list_mod.is_list_artifact_name(requested_file):
            base_directory = os.path.abspath(config.LOCAL_LIST_DIR)
            full_path = os.path.join(base_directory, requested_file)
        else:
            # Same reasoning as the !rar branch above: FILE_DIRECTORY can
            # legitimately be unset (#184's review), and an
            # ordinary track request - unlike a list artifact request, which
            # never touches FILE_DIRECTORY at all - has nothing to look for
            # without it. Checked explicitly rather than letting
            # os.path.abspath(None) raise into the bare except below, which
            # left the requester with no response of any kind.
            if not config.FILE_DIRECTORY:
                announce.send_dcc_error(user, "not_configured")
                return
            base_directory = os.path.abspath(config.FILE_DIRECTORY)
            full_path = os.path.join(base_directory, requested_file)

        is_master_zip = list_mod.is_list_artifact_name(requested_file)
        if not is_master_zip and not os.path.exists(platform_compat.long_path(full_path)):
            latest_list_path = list_mod.find_latest_list()
            if latest_list_path and os.path.exists(latest_list_path):
                try:
                    with open(latest_list_path, "r", encoding="utf-8", errors="ignore") as lf:
                        lines = lf.readlines()
                    target_folder = None
                    fallback_folder = None
                    clean_req = str(requested_file).lower().strip()

                    for idx, line in enumerate(lines):
                        line_clean = line.strip()
                        if line_clean.startswith(f"!{config.NICKNAME} "):
                            # str.split() puts what came BEFORE the separator in
                            # [0], and the line starts with the separator - so
                            # [0] is the empty string on every line here, and
                            # this comparison never matched anything. The
                            # filename is in [1]; the whole list lookup was dead
                            # code without it, leaving the os.walk() below to
                            # answer every request.
                            parts_nick = line_clean.split(f"!{config.NICKNAME} ", 1)
                            rest_in_list = parts_nick[1].strip() if len(parts_nick) > 1 else ""

                            current_file_in_list, current_size_in_list = list_mod.strip_info_suffix(rest_in_list)

                            if clean_req == str(current_file_in_list).lower().strip():
                                found_folder = None
                                for back_idx in range(idx, -1, -1):
                                    back_line = lines[back_idx].strip()
                                    # The prefix-stripping itself is
                                    # list_mod.resolve_list_folder() - this used
                                    # to be a second, hand-written copy of it
                                    # (a hardcoded back_line[9:] rather than
                                    # len(LIST_FOLDER_PREFIX), and its own
                                    # trailing-backslash/separator handling),
                                    # which could silently drift from the
                                    # original if the list format ever changed.
                                    if back_line.upper().startswith(list_mod.LIST_FOLDER_PREFIX):
                                        found_folder = list_mod.resolve_list_folder(
                                            back_line, base=base_directory)
                                        break
                                if found_folder is None:
                                    continue

                                # Two or more copies can share this exact name
                                # and differ only in size. Without a size hint,
                                # or if it matches nothing, the first copy the
                                # list names wins - same as before this change,
                                # and pinned by
                                # test_no_error_is_reported_for_a_duplicate.
                                # With one, a copy whose own ::INFO:: size
                                # matches it wins instead, so a request built
                                # from a search result's exact line reaches the
                                # copy that result actually named. A bare
                                # request (no hint - AutoQ.mrc and every
                                # existing caller) still stops at this first
                                # match exactly as before; only a hinted
                                # request that has not matched yet pays for
                                # scanning on, since that is the one case
                                # where the answer isn't already known.
                                if fallback_folder is None:
                                    fallback_folder = found_folder
                                    if not requested_size_hint:
                                        break
                                if requested_size_hint and current_size_in_list.lower().strip() == requested_size_hint:
                                    target_folder = found_folder
                                    break

                    if target_folder is None:
                        target_folder = fallback_folder

                    if target_folder is not None:
                        test_path = os.path.join(target_folder, requested_file)
                        if os.path.exists(platform_compat.long_path(test_path)):
                            full_path = test_path
                except Exception as list_err:
                    print(f"[DCC-LOOKUP ERROR] {list_err}")
            if not os.path.exists(platform_compat.long_path(full_path)):
                for root, dirs, files in os.walk(base_directory):
                    if requested_file in files:
                        full_path = os.path.join(root, requested_file)
                        break

        if not is_safe_path(base_directory, full_path):
            announce.send_dcc_error(user, "invalid_path")
            return

        if not os.path.exists(platform_compat.long_path(full_path)) or os.path.isdir(platform_compat.long_path(full_path)):
            announce.send_dcc_error(user, "file_not_found")
            return

        file_name = os.path.basename(full_path)

        with queue_lock:
            total_global_queued = get_total_queued_count()
            user_queued_count = len(config.dcc_queue.get(user_key, []))

            if total_global_queued >= config.MAX_GLOBAL_QUEUE:
                announce.send_dcc_error(user, "global_full")
                return

            if user_queued_count >= config.MAX_USER_QUEUE:
                announce.send_dcc_error(user, "user_full")
                return

            # Check whether this nick ALREADY has a send running, in active_transfers
            user_already_transferring = any(str(tx['user']).lower() == user_key for tx in config.active_transfers)
            
            # Create the temporary send lock in config if it is missing
            if not hasattr(config, 'user_processing_lock'):
                config.user_processing_lock = set()
                
            # If the user just sent rows, check whether the nick is locked in memory
            user_is_processing = user_key in config.user_processing_lock
            user_has_queue = len(config.dcc_queue.get(user_key, [])) > 0

            # Only a user who is clear in transfers, the queue AND the memory lock sends immediately
            if not user_already_transferring and not user_is_processing and not user_has_queue and len(config.active_transfers) < config.MAX_DCC_SLOTS:
                # Lock the nick immediately, so the next row goes to the queue
                config.user_processing_lock.add(user_key)
                
                next_file_fake = {"path": full_path, "file": file_name, "channel": target_chan, "is_temporary_zip": False}
                config.active_transfers.append({"user": user, "file": file_name, "bytes_sent": 0, "next_file_obj": file_name})
                if oserve: oserve.active_downloads = len(config.active_transfers)
                announce.send_dcc_sending_notice(user, file_name)
                threading.Thread(target=start_dcc_send, args=(irc_sock, user, full_path, file_name, target_chan, next_file_fake), daemon=True).start()
                return
            else:
                # This user already has a track running; the row goes to dcc_queue.txt
                if user_key not in config.dcc_queue:
                    config.dcc_queue[user_key] = []
                config.dcc_queue[user_key].append({"file": file_name, "path": full_path, "channel": target_chan, "user_raw": user, "is_temporary_zip": False})
                
                # Save and update dcc_queue.txt on disk straight away
                import db
                db.save_dcc_queue()
                
                user_pos = len(config.dcc_queue[user_key])
                announce.send_dcc_queue_notice(user, file_name, user_pos)
                return

    except Exception as e:
        print(f"[DCC ERROR] {e}")
        oserve = sys.modules.get('oserve')
        if oserve: oserve.send_fails_count += 1

def start_dcc_send(irc_sock, user, file_path, file_name, channel, next_file):
    """Handle the network ports and the CTCP, and stream the bytes with accurate timing."""
    # No `global active_transfers` here: there is no module-level name of that
    # kind in this file, and there never was. Every real use below is
    # config.active_transfers, which needs no declaration. The statement was
    # inert, and inviting: it read as though this function mutated a module
    # global somebody could go looking for (#232).
    import time
    import os
    import socket
    import sys
    import threading
    
    # If file_path or file_name happen to be dictionaries, pull the real strings out
    if isinstance(next_file, dict):
        if not isinstance(file_path, str) or "{" in str(file_path):
            file_path = next_file.get('path', str(file_path))
        if not isinstance(file_name, str) or "{" in str(file_name):
            file_name = next_file.get('file', str(file_name))

    # Addressed through long_path so a deep library path reports its real
    # size instead of 0 - the DCC offer carries this number, and a 0 makes
    # the receiver close immediately.
    _size_probe = platform_compat.long_path(file_path) if isinstance(file_path, str) else None
    file_size = os.path.getsize(_size_probe) if (_size_probe and os.path.exists(_size_probe)) else 0
    ip_long = get_public_ip_long()
    start_time = time.time()
    bytes_sent = 0
    # Only a send loop that ran to completion counts as delivered. A socket timeout, a
    # refused connection or a half-finished stream must not consume the queue row.
    transfer_completed = False
    
    # is_offerable_to_strangers() is the address half; ip_long == 0 only catches
    # a blank or malformed value, and a loopback address passes it (127.0.0.1 is
    # 2130706433). See that function for what went wrong without it.
    if ip_long == 0 or file_size == 0 or not is_offerable_to_strangers():
        # Two very different failures used to share one message. "File access
        # issue or empty payload. Please try again." is actively misleading for
        # the address case: there is nothing wrong with the file, retrying
        # cannot help, and only the operator can fix it.
        if ip_long == 0 or not is_offerable_to_strangers():
            reason = ("this bot has no usable public address configured, so it "
                      "cannot offer a transfer")
            print(f"[DCC CRITICAL ABORT] No usable public address "
                  f"(MY_IP_OR_DOCK={getattr(config, 'MY_IP_OR_DOCK', '')!r}); refused "
                  f"the send for {user} rather than offering one nobody can dial. "
                  f"Set MY_IP_OR_DOCK in admin_config.py or settings.conf.")
        else:
            reason = "file access issue or empty payload. Please try again"
            print(f"[DCC CRITICAL ABORT] Aborted the send for {user}. "
                  f"Path: {file_path} (Size: {file_size})")
        try: 
            msg = f"NOTICE {user} :{config.C_BOLD}Error:{config.C_RESET} {reason}.\r\n"
            irc_sock.send(msg.encode('utf-8', errors='ignore'))
        except: 
            pass
            
        # Clear the locks and wait three seconds, to stay well clear of Excess Flood.
        # Only clear rar_inprogress if THIS send owns it - a plain audio file never
        # did, and clearing a flag another user's pack is holding is how the
        # interlock leaks (see the identical guard a few lines down, at the port-
        # exhaustion branch, which already got this right).
        if isinstance(next_file, dict) and next_file.get('is_temporary_zip'):
            config.rar_inprogress = False
            # #215: this release is the only moment another user's held pack can
            # start. Nothing else revisits them - every check_queue_and_send()
            # caller passes the user who just finished, never the one turned
            # away at [RAR-HOLD].
            redispatch_waiting_pack(irc_sock, just_finished=user)
        if hasattr(config, 'user_processing_lock'):
            config.user_processing_lock.discard(user.lower())
            
        with queue_lock:
            config.active_transfers[:] = [tx for tx in config.active_transfers if tx['user'].lower() != user.lower()]
            
        # This abort returns BEFORE the try/finally that settles the queue row, so without
        # this call the same unreadable entry was re-selected every ~3 seconds forever,
        # with no counter and no way out - a permanent hot loop injecting a NOTICE and a
        # VIP notice on every pass. Charging it to the retry budget bounds it.
        release_queue_entry(user, next_file, delivered=False,
                            reason="file missing, empty, or public IP unknown")

        time.sleep(3.0)
        check_queue_and_send(irc_sock, user)
        return


    dcc_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    # SO_REUSEADDR means the OPPOSITE thing on Windows - it lets another process
    # bind this same port and take the incoming connection, which on a DCC
    # listener is a hijack. platform_compat picks the right option per platform.
    platform_compat.prepare_listener(dcc_sock)
    
    assigned_port = None
    for port in range(config.DCC_PORT_START, config.DCC_PORT_END + 1):
        try:
            dcc_sock.bind(('0.0.0.0', port))
            assigned_port = port
            break
        except socket.error:
            continue

    if assigned_port is None:
        try: irc_sock.send(f"NOTICE {user} :{config.C_BOLD}Error:{config.C_RESET} No available DCC ports.\r\n".encode())
        except: pass
        with queue_lock:
            config.active_transfers[:] = [tx for tx in config.active_transfers if tx['user'].lower() != user.lower()]

        # Port exhaustion is TRANSIENT and is not this entry's fault, so it is deliberately
        # NOT charged to the retry budget - a busy spell must not discard good queued files.
        # The interlocks are released so the user is not left wedged, and the row stays
        # queued for the next completion trigger. Re-triggering here would spin: the ports
        # are still full one instruction later.
        # Only clear rar_inprogress if THIS send owns it - a plain audio file never did, and
        # clearing a flag another user's pack is holding is how the interlock leaks.
        if isinstance(next_file, dict) and next_file.get('is_temporary_zip'):
            config.rar_inprogress = False
            # #215: this release is the only moment another user's held pack can
            # start. Nothing else revisits them - every check_queue_and_send()
            # caller passes the user who just finished, never the one turned
            # away at [RAR-HOLD].
            redispatch_waiting_pack(irc_sock, just_finished=user)
        if hasattr(config, 'user_processing_lock'):
            config.user_processing_lock.discard(user.lower())
        # #162 finding #8: this branch's own comment above says the row
        # stays queued for the next completion trigger - deleting the
        # archive it still points at contradicted that in the same breath.
        # A second user's row pointing at the SAME shared archive (see
        # discard_orphaned_temp_archives()'s own comment on why two users
        # requesting the same real album share one file) was left dangling,
        # and the retry 45s later hit the file_size == 0 critical abort,
        # which classifies a consumed temporary archive as non-retryable
        # and drops the row - the album was lost by the one branch whose
        # stated purpose was not to lose it. Nothing is removed here now;
        # if the artifact is being preserved, the file backing it must be
        # too.

        # The row stays queued and is not charged a failure, but on an otherwise idle bot
        # nothing else would ever wake it. One bounded delayed retry, no tight spin.
        def delayed_port_retry():
            time.sleep(45)
            check_queue_and_send(irc_sock, user)
        threading.Thread(target=delayed_port_retry, daemon=True).start()

        print("[DCC PORTS] No free DCC port for " + str(user) + "; entry stays queued, retrying in 45s.")
        # The listener is closed HERE and not anywhere else. Every path from the
        # try: below runs through a finally: that closes it, but this branch
        # returns before reaching that try - so the socket created a few lines
        # above was leaked, once per refused request, on a path that retries
        # every 45 seconds. On a bot whose ports are genuinely exhausted that is
        # a steady file-descriptor leak for as long as the condition lasts,
        # which is exactly when the daemon can least afford one.
        try:
            dcc_sock.close()
        except Exception:
            pass
        return

    dcc_sock.settimeout(30.0)
    dcc_sock.listen(1)
    
    safe_file_name = file_name.replace(" ", "_")
    # The handshake is a PRIVMSG like any other: the server prepends our
    # ":nick!ident@host " when relaying it, and the whole thing has to fit
    # inside 512 bytes. A non-ASCII filename costs 2-3 bytes per character,
    # so a ~150-character Chinese or Japanese title overruns that on its own -
    # and the fields the transfer actually needs (address, port, size) sit
    # AFTER the name, so the server's cut takes THOSE and the receiver is
    # handed a handshake it cannot act on. Trimming the name ourselves costs a
    # shortened save-name; not trimming it costs the transfer.
    ctcp_handshake = announce.fit_irc_filename(
        lambda offered: (f"PRIVMSG {user} :\x01DCC SEND {offered} "
                         f"{ip_long} {assigned_port} {file_size}\x01\r\n"),
        safe_file_name)
    if safe_file_name not in ctcp_handshake:
        # Say so rather than letting the receiver silently save it under a
        # name the operator never chose and cannot find in their own library.
        print(f"[DCC] Offered filename shortened to fit the IRC line: {file_name!r}")
    
    try:
        irc_sock.send(ctcp_handshake.encode())
        print(f"[DCC-LISTEN] Listening on port {assigned_port} for {user} (Handshake sent directly).")
    except Exception as e:
        print(f"[DCC ERROR] Failed to send the handshake: {e}")

    conn = None
    try:
        conn, addr = dcc_sock.accept()
        conn.settimeout(60.0)
        dcc_sock.settimeout(None)
        
       # Push the packets out immediately rather than letting them coalesce
        conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        conn.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        print(f"[DCC-CONNECT] {user} connected from {addr}!")

        start_time = time.time()       

        with open(platform_compat.long_path(file_path), 'rb') as f:
            while True:
                # A 64 KB packet size, tuned for throughput
                chunk = f.read(65536)
                if not chunk: break
                try:
                    conn.sendall(chunk)
                    bytes_sent += len(chunk)
                except socket.error as e:
                    raise e
                for tx in config.active_transfers:
                    if tx['user'].lower() == user.lower():
                        tx['bytes_sent'] += len(chunk)
                oserve = sys.modules.get('oserve')
                if oserve: oserve.total_sent_bytes += len(chunk)
                
        transfer_completed = True
        print(f"[DCC-SUCCESS] Sent the whole file to {user} with no errors.")
        # The original pause: gives mIRC 1.5 seconds to close the file calmly
        try: time.sleep(1.5)
        except: pass
 
        # ---------------------------------------------------------------------
        # Update the statistics on disk
        # ---------------------------------------------------------------------
        try:
            import db
            # ONE locked read-modify-write inside db, instead of a load here, a
            # mutate here, and a separate save here. MAX_DCC_SLOTS transfers
            # finish concurrently, and the unsynchronised version let whichever
            # thread saved second discard the other's increment - permanently,
            # since nothing ever recomputes these counters. It also rotates the
            # day, so a transfer completing after midnight is counted correctly.
            stats = db.update_stats_on_complete(file_size)
            print(f"[DB COUNTER] Statistics updated on disk. (Files sent: {stats[0]})")

            # And count the item itself, for the Stats page's "Most
            # downloaded". A SEPARATE _disk_lock acquisition, after the one
            # above has been released: threading.Lock is not reentrant, and
            # this file is not the stats row - see db.py's note on why every
            # public entry point there takes the lock exactly once.
            #
            # An album goes out as a packed archive from TMP_ZIP_DIR whose
            # name dcc.py built from the folder, so the archive's own name is
            # already the readable one. A single file is keyed by its path
            # relative to the library and only displayed by its basename:
            # two albums can hold a track with the same filename (#110), and
            # collapsing them would credit one track with another's
            # downloads.
            db.record_download(*download_count_identity(file_path, file_name))
        except Exception as db_err:
            print(f"[DB ERROR] Could not increment the sharing statistics through the db module: {db_err}")
        # ---------------------------------------------------------------------

        try: conn.close()
        except: pass

    except socket.timeout:
        # FIXED (issue #30): previously silent. A handshake can succeed and the client can
        # connect, but if they never acknowledge fast enough the send loop times out here
        # with no log line at all - the only trace was a gap in the log between DCC-CONNECT
        # and the finally block's cleanup lines.
        print(f"[DCC-FAIL] Timeout sending to {user}: no data acknowledged within the socket timeout.")
        oserve = sys.modules.get('oserve')
        if oserve: oserve.send_fails_count += 1
    except Exception as e:
        # FIXED (issue #30): `e` was captured and never used. A connection reset, a broken
        # pipe, or any other mid-transfer failure produced the exact same silence - no way
        # to tell which one happened after the fact, especially once the temp archive is
        # already deleted and the queue row already gone.
        print(f"[DCC-FAIL] Transfer to {user} failed: {type(e).__name__}: {e}")
        oserve = sys.modules.get('oserve')
        if oserve: oserve.send_fails_count += 1
    finally:
        # Give the network buffer 0.5s to flush the final acknowledgement
        try:
            import time
            time.sleep(0.5)
        except:
            pass

        # The real-time speed counter
        acute_duration = time.time() - (start_time if 'start_time' in locals() else time.time())
        if acute_duration <= 0:
            acute_duration = 0.1
            
        acute_bytes = bytes_sent if 'bytes_sent' in locals() else 0
        final_calc_speed = int(acute_bytes / acute_duration)

        # The record the channel advert publishes. db has had
        # save_speed_record() from the start and announce.py has read it into
        # every advert since, but nothing ever sat between the two - the only
        # callers of the writer were tests. So the advert has shown
        # "Record: 0k/s" for the life of the feature, on every install.
        #
        # Only a transfer that actually completed counts: a send that failed
        # part-way has moved real bytes in real time, so its rate looks like a
        # legitimate sample and is not one.
        if transfer_completed:
            try:
                import stats_mgr as stats_mgr_mod
                stats_mgr_mod.update_speed_record(final_calc_speed, acute_duration)
            except Exception as record_err:
                print(f"[STATS ERROR] Could not update the speed record: {record_err}")

        # 1. Clean up the transfer and the slot immediately
        try:
            with queue_lock:
                config.active_transfers[:] = [tx for tx in config.active_transfers if tx['user'].lower() != user.lower()]
                oserve = sys.modules.get('oserve')
                if oserve: oserve.active_downloads = len(config.active_transfers)
        except Exception as trans_clean_err:
            print(f"[DCC CLEANUP ERROR] Could not clear active_transfers in memory: {trans_clean_err}")


        # 2. Get the channel notice out first of all
        try:
            import announce as announce_mod
            # Only announce a transfer that actually completed. A failed attempt now keeps
            # its queue row for retry, so announcing here would tell the channel "Sent" and
            # re-offer the same file on every attempt.
            if transfer_completed:
                announce_mod.send_transfer_complete(channel, user, file_name, file_size, start_time, final_calc_speed)
        except Exception as ann_chan_err:
            print(f"[ANNOUNCE CHANNEL ERROR] Could not send the channel notice: {ann_chan_err}")

        # 3. Close the network socket safely
        try: conn.close()
        except: pass
        try: dcc_sock.close()
        except: pass
        # 4. The RAR cache and the send lock
        try:
            file_still_needed = False
            safe_path = str(file_path)

            if _is_temp_zip_cache_file(safe_path):
                with queue_lock:
                    # A. Is the file still QUEUED for some OTHER user in dcc_queue.txt?
                    for q_user, q_files in getattr(config, 'dcc_queue', {}).items():
                        if q_user.lower() != user.lower():
                            for q_obj in q_files:
                                if isinstance(q_obj, dict) and (q_obj.get('file') == file_name or q_obj.get('path') == file_path):
                                    file_still_needed = True
                                    break
                    
                    # B. Is the file still being sent ACTIVELY to somebody in another slot?
                    active_matches = 0
                    for tx in getattr(config, 'active_transfers', []):
                        if tx.get('file') == file_name:
                            active_matches += 1
                    
                    if active_matches > 0:
                        file_still_needed = True

                # If no other user and no active slot still needs it, delete it from disk
                if not file_still_needed:
                    if os.path.exists(platform_compat.long_path(file_path)):
                        os.remove(file_path)
                        print(f"[DCC CLEANUP] Safely deleted the temporary archive from disk: {file_name}")
        except Exception as file_rm_err:
            print(f"[DCC CLEANUP ERROR] Could not run the disk cleanup: {file_rm_err}")
        
        # 5. SETTLE THE QUEUE ROW - by identity, with a retry budget. This replaces an
        #    unconditional pop(0), which removed whatever was first at that instant rather
        #    than the entry actually sent. See release_queue_entry.
        row_retained = False
        try:
            row_retained = release_queue_entry(
                user, next_file, delivered=transfer_completed,
                reason="transfer complete" if transfer_completed else "transfer did not complete")
        except Exception as pop_err:
            print("[DCC CLEANUP ERROR] Could not settle the queue row: " + str(pop_err))

        # 6. Release the memory lock and rule out duplicate threads.
        # #162 finding #6: a plain audio file never owned rar_inprogress - this ran
        # unconditionally on EVERY transfer's completion, so bob's ordinary MP3
        # finishing could clear the flag while alice's pack (queued as a separate
        # slot, holding the interlock for the whole duration of its own pack) was
        # still running, losing packer serialisation. Same guard as the critical-
        # abort and port-exhaustion branches above.
        if isinstance(next_file, dict) and next_file.get('is_temporary_zip'):
            config.rar_inprogress = False
            # #215: this release is the only moment another user's held pack can
            # start. Nothing else revisits them - every check_queue_and_send()
            # caller passes the user who just finished, never the one turned
            # away at [RAR-HOLD].
            redispatch_waiting_pack(irc_sock, just_finished=user)
        if hasattr(config, 'user_processing_lock'):
            config.user_processing_lock.discard(user.lower())

        # 7. Wake the queue automatically after three seconds, thread-safely
        # A retained row means the attempt FAILED and will be retried. Reusing the flat
        # 3-second completion timer as the retry timer would hammer a broken entry three
        # times in nine seconds, each pass emitting a NOTICE and a VIP notice while the VIP
        # queue drains slower than that. Back off instead.
        retry_delay = 3
        if row_retained and isinstance(next_file, dict):
            retry_delay = 15 * max(1, int(next_file.get('send_fails', 1)))

        def delayed_queue_trigger_fallback():
            time.sleep(retry_delay)
            check_queue_and_send(irc_sock, user)
        threading.Thread(target=delayed_queue_trigger_fallback, daemon=True).start()
