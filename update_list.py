# update_list.py - OmenServe-style layout, generating both lists (part 1 of 2)
import os
import io
import re
import sys
import shutil
import datetime
import subprocess
import tempfile
import zipfile
import time
import defaults as config
import platform_compat

# Multi-disc/box-set container names the !rar album list truncates at - see
# generate_master_list()'s own comment on the box-word block for why this
# has to match a whole PATH SEGMENT, not a substring anywhere in the path.
# An optional trailing number covers "CD1", "Disc 2", "Volume III" (digits
# only - "III" survives as part of the folder name, same as before this fix).
_BOX_WORD_RE = re.compile(
    r'^(cd|disc|disk|volume|digital media|media)\s*\d*$', re.IGNORECASE)

def format_size_human(bytes_size):
    for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
        if bytes_size < 1024.0:
            return f"{bytes_size:.1f}{unit}"
        bytes_size /= 1024.0
    return f"{bytes_size:.1f}PB"

def format_total_size(bytes_size):
    for unit in ['B', 'KiB', 'MiB', 'GiB', 'TiB']:
        if bytes_size < 1024.0:
            return f"{bytes_size:.1f}{unit}"
        bytes_size /= 1024.0
    return f"{bytes_size:.1f}PiB"

def _one_line(text):
    """Flatten anything that would break the one-entry-per-line format.

    POSIX filenames may contain newlines and other control characters - only "/"
    and NUL are forbidden - so a track called "evil\nname.flac" is perfectly
    legal on the Linux box this daemon runs on. Written straight into the list it
    splits one request entry into two lines, leaving a truncated entry and an
    orphan fragment, and the file every user downloads is malformed from there
    down. Windows refuses such names at creation, which is why CI caught this on
    the ubuntu jobs only.

    Nobody can plant one remotely - the library is the operator's own mount - so
    this is robustness rather than a security boundary. Replacing with a space
    keeps the entry visible and the file structurally sound; such a track is
    already unrequestable, because the request parser splits on whitespace too.

    Also sanitises non-UTF-8 bytes. os.walk() on POSIX decodes filenames with
    the "surrogateescape" error handler by default, so a name with bytes that
    are not valid UTF-8 (a CP1252 rip, a bad extraction, a FAT copy) comes back
    as a lone surrogate codepoint - not itself a control character, but not
    valid UTF-8 either, and this text is about to be written with a strict
    UTF-8 encoder. Sanitised here rather than left to fail at the write: one
    bad name in a library of thousands now costs a mangled-but-valid name in
    the list, not the entire rebuild.
    """
    text = str(text).encode("utf-8", "replace").decode("utf-8")
    return "".join(" " if ch < " " or ch == "\x7f" else ch for ch in text)


def _discard_temp_lists(*paths):
    """Remove half-written temporary lists so they cannot be mistaken for real ones."""
    for path in paths:
        try:
            if path and os.path.exists(path):
                os.remove(path)
        except OSError as err:
            print(f"[LIST-CLEAN ERROR] Could not remove {path}: {err}")


def _prune_superseded_lists(keep):
    """Delete older generated lists once the new ones are safely in place.

    Runs AFTER the swap, never before: the previous index has to stay usable for the whole
    scan, which can take minutes on a large NFS mount.
    """
    removed = 0
    try:
        entries = os.listdir(config.LOCAL_LIST_DIR)
    except OSError as err:
        print(f"[LIST-CLEAN ERROR] Could not read {config.LOCAL_LIST_DIR}: {err}")
        return

    for item in entries:
        if item in keep:
            continue
        if not item.startswith(config.LIST_BASE_NAME):
            continue
        # ".rar" is here because LIST_FORMAT can publish one. Only names that
        # also start with LIST_BASE_NAME are considered, and this is the lists
        # directory, so no album archive a user is waiting on is in reach.
        if not item.endswith((".txt", ".zip", ".rar")):
            continue
        try:
            os.remove(os.path.join(config.LOCAL_LIST_DIR, item))
            removed += 1
        except OSError as err:
            print(f"[LIST-CLEAN ERROR] Could not remove {item}: {err}")

    if removed:
        print(f"[LIST-CLEAN] Removed {removed} superseded list(s).")


# The literal shipped default defaults.py derives LIST_BASE_NAME away FROM -
# see its own comment on why the derivation compares against this same
# literal rather than a shared constant (a snapshot taken before overrides
# apply, the way SHIPPED_DEFAULTS does for settings_file.REQUIRED, would
# need LIST_BASE_NAME added to REQUIRED just to get one, which it deliberately
# is not).
_SHIPPED_LIST_BASE_NAME = "DCCore"


# The name the artifacts in LOCAL_LIST_DIR were last published under.
#
# #213: the migration below could only ever carry files across from the shipped
# default, so it worked exactly once. Rename the bot a second time - or from any
# value that was never "DCCore" - and the artifacts on disk keep the old name
# while the bot looks for the new one. A restart does not recover it: the list
# is right there and invisible, and the advert says 0 files.
#
# The previous name has to be remembered somewhere. A marker file in the lists
# directory rather than settings.conf/admin_config.py, because it travels with
# the thing it describes: a value in the config can be hand-edited, replaced
# wholesale on an upgrade, or restored from a backup taken before the rename,
# and each of those silently orphans the lists again - which is the exact
# failure being closed. A file in the directory cannot drift from the directory
# it names, because it is in it.
#
# Absent means an install from before this existed, which is precisely when
# falling back to _SHIPPED_LIST_BASE_NAME is the right guess.
#
# The leading dot and the lack of a .txt/.zip/.rar suffix keep it out of every
# artifact scan in list.py and out of the glob in find_latest_list().
_LIST_BASE_MARKER = ".dccore-list-base"


def list_base_marker_path(directory=None):
    directory = directory or getattr(config, "LOCAL_LIST_DIR", "./lists")
    return os.path.join(directory, _LIST_BASE_MARKER)


def read_list_base_marker(directory=None):
    """The LIST_BASE_NAME the artifacts on disk were published under, or None.

    None on any read problem, deliberately: an unreadable marker must fall back
    to the shipped-default guess, never raise into startup.
    """
    try:
        with io.open(list_base_marker_path(directory), encoding="utf-8") as handle:
            return handle.read().strip() or None
    except (OSError, UnicodeDecodeError):
        return None


def write_list_base_marker(name=None, directory=None, log=print):
    """Record the name the artifacts are now published under. Returns True on
    success.

    Written through db._atomic_write, like the advert side files: a marker
    truncated by a crash mid-write would read as absent, sending the next
    migration back to the shipped-default guess and stranding the artifacts
    this call exists to keep findable.
    """
    import db

    name = config.LIST_BASE_NAME if name is None else name
    try:
        db._atomic_write(list_base_marker_path(directory), str(name) + "\n")
        return True
    except Exception as err:
        log(f"[MIGRATE] Could not record the list base name: {err}. "
            f"A future rename may not find these files.")
        return False

def migrate_list_base_name(log=print):
    """Carry existing list files across when LIST_BASE_NAME changed out from
    under them - #184's review: defaults.py's LIST_BASE_NAME
    derivation (an untouched value takes NICKNAME's own value once NICKNAME
    is set) means every existing install's list files, generated before that
    derivation existed, are sitting on disk as "DCCore-<date>.*" while
    LIST_BASE_NAME now resolves to the operator's nickname instead.

    Without this, find_latest_list() globs for the NEW base name, finds
    nothing, and the daemon boots, joins its channels and advertises with no
    list at all - not because there is no list, but because the one on disk
    is filed under a name nothing is looking for any more. It stays that way
    until the next successful !update, which on a weekly rebuild schedule is
    up to a week of a bot that looks healthy and answers every request with
    "not found". db.migrate_legacy_side_files()'s own docstring describes
    this exact failure shape for the flac-serv-* rename; this is the same
    problem, for a prefix rather than a single filename.

    Deliberately narrow, matching that function's safety properties:

      * only when LIST_BASE_NAME no longer equals what defaults.py ships -
        an install that never had any "DCCore-*" files (fresh, or one that
        chose its own LIST_BASE_NAME from the very first run) has nothing to
        move, and this is a no-op for it.
      * only a file whose new name does not already exist is moved - a
        rebuild that has already happened under the new name wins over
        anything left behind from the old one.
      * os.replace, so an interrupted run leaves one intact file rather than
        two halves; a failure is logged and swallowed per file, because a
        daemon that will not start over a rename is a worse outcome than the
        rename not happening for one file.

    Returns the list of (old, new) basenames actually moved, for the tests
    and for the startup log.
    """
    directory = getattr(config, "LOCAL_LIST_DIR", "./lists")

    # What the files on disk are actually called, not what they were called
    # when the bot shipped. Absent means an install from before the marker
    # existed, where the shipped default is the right guess (#213).
    previous = read_list_base_marker(directory) or _SHIPPED_LIST_BASE_NAME

    if previous == config.LIST_BASE_NAME:
        # Nothing to move. Still record it: an install that has never been
        # renamed has no marker, and writing one now means its FIRST rename is
        # migrated from the right name rather than from the shipped guess.
        write_list_base_marker(config.LIST_BASE_NAME, directory, log=log)
        return []

    try:
        entries = os.listdir(directory)
    except OSError as err:
        log(f"[MIGRATE] Could not read {directory}: {err}")
        return []

    # The "-" is required, not just the bare prefix: every real artifact is
    # named "DCCore-<date>.ext" or "DCCore-RAR-<date>.ext", always with a
    # hyphen immediately after the base name. A bare startswith("DCCore")
    # would also match a file already renamed to the NEW LIST_BASE_NAME when
    # that name itself happens to start with "DCCore" - e.g. "DCCoreTest" -
    # corrupting an already-correct file instead of leaving it alone.
    old_prefix = previous + "-"

    moved = []
    for item in entries:
        if not item.startswith(old_prefix):
            continue
        if not item.endswith((".txt", ".zip", ".rar")):
            continue

        new_name = config.LIST_BASE_NAME + item[len(previous):]
        old_path = os.path.join(directory, item)
        new_path = os.path.join(directory, new_name)
        if os.path.exists(new_path):
            continue
        try:
            platform_compat.replace_with_retry(old_path, new_path)
            moved.append((item, new_name))
        except OSError as err:
            log(f"[MIGRATE] Could not rename {item} to {new_name}: {err}. "
                f"It will not be found until the next successful !update.")

    for old_name, new_name in moved:
        log(f"[MIGRATE] Renamed {old_name} to {new_name}.")

    # After the move, not before: if the renames failed the marker must still
    # say what the files on disk are really called, or the next startup would
    # look for them under a name nothing has.
    if moved or not read_list_base_marker(directory):
        write_list_base_marker(config.LIST_BASE_NAME, directory, log=log)
    return moved


def _artifact_paths(fmt, date_str):
    """Where the download artifact for `fmt` is published, and staged."""
    import list as list_mod
    final = os.path.join(config.LOCAL_LIST_DIR, list_mod.list_artifact_name(fmt, date_str))
    return final, final + ".new"


def _write_text_artifact(tmp_path, members):
    """Both lists as one text file.

    A plain .txt can only be one file where the .zip is two, so the album
    section is appended to the file list rather than dropped. The format is a
    choice about packaging; it is not a request to hand out less. This is a
    copy and not the master index itself precisely because the two must not be
    the same file - see list.FULL_LIST_MARKER.
    """
    with io.open(tmp_path, "w", encoding="utf-8", newline="\n") as out:
        for index, (source, _name) in enumerate(members):
            if index:
                out.write("\n\n")
            with io.open(source, encoding="utf-8") as handle:
                shutil.copyfileobj(handle, out)


def _write_zip_artifact(tmp_path, members):
    """Store the temp files under their FINAL names, so the archive users
    download is identical to what it always was."""
    with zipfile.ZipFile(tmp_path, "w", zipfile.ZIP_DEFLATED) as zipf:
        for source, name in members:
            zipf.write(source, arcname=name)


_STAGING_PREFIX = ".listpack-"


def _discard_stale_staging():
    """Remove staging directories a previous run was killed in the middle of."""
    try:
        entries = os.listdir(config.LOCAL_LIST_DIR)
    except OSError:
        return
    for name in entries:
        if not name.startswith(_STAGING_PREFIX):
            continue
        path = os.path.join(config.LOCAL_LIST_DIR, name)
        if os.path.isdir(path):
            shutil.rmtree(path, ignore_errors=True)


def _write_rar_artifact(tmp_path, members):
    """Pack with the rar binary. False if it could not be done, with the reason.

    rar has no equivalent of zipfile's arcname, so the members are copied to
    their published names in a scratch directory first - packing the temporary
    files directly would put "DCCore-2026-08-30.txt.new" inside the archive and
    every reader would see the name the build happened to use that run.

    The scratch directory sits inside LOCAL_LIST_DIR so the finished archive
    lands on the same filesystem: os.replace is only atomic within one.
    """
    rar_bin = platform_compat.rar_command(getattr(config, "RAR_BINARY", None))
    if not rar_bin:
        print("[LIST-GEN] LIST_FORMAT is 'rar' but no rar binary was found - "
              "set RAR_BINARY or put rar on PATH. Packing a .zip instead.")
        return False

    # The finally below removes this run's directory. A daemon killed between
    # mkdtemp and that finally leaves one behind, and nothing else in lists/
    # ever looks at directories, so it would sit there until somebody noticed.
    _discard_stale_staging()
    staging = tempfile.mkdtemp(prefix=_STAGING_PREFIX, dir=config.LOCAL_LIST_DIR)
    try:
        staged = []
        for source, name in members:
            target = os.path.join(staging, name)
            shutil.copyfile(source, target)
            staged.append(os.path.abspath(target))

        # rar names the archive itself when the given name has no extension,
        # so it is built inside the staging directory under a name it cannot
        # reinterpret and moved into place afterwards.
        built = os.path.join(staging, "list.rar")
        # -ep stores the names with no path at all, which is the arcname
        # behaviour above. A list of arguments and never a shell, and a
        # timeout, both for the same reasons dcc.py packs albums that way: a
        # hung rar here would wedge every list rebuild after it.
        cmd = [rar_bin, "a", "-ep", "-w" + os.path.abspath(config.LOCAL_LIST_DIR),
               os.path.abspath(built)] + staged
        result = subprocess.run(cmd, capture_output=True, text=True,
                                timeout=getattr(config, "RAR_TIMEOUT", 1800))
        if result.returncode == 0 and os.path.exists(built):
            platform_compat.replace_with_retry(built, tmp_path)
            return True
        detail = (result.stderr or result.stdout or "").strip().splitlines()
        print(f"[LIST-GEN] rar exited {result.returncode} packing the list - "
              f"packing a .zip instead. {detail[-1] if detail else ''}")
        return False
    except (OSError, ValueError, subprocess.SubprocessError) as err:
        print(f"[LIST-GEN] Could not pack the list with rar ({err}) - "
              f"packing a .zip instead.")
        return False
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def build_list_artifact(fmt, members, date_str):
    """Build what a user downloads. Returns (fmt_used, tmp_path, final_path).

    `fmt_used` is not always `fmt`: "rar" falls back to "zip" when the binary
    is missing or the pack fails. That is deliberate. The list is the whole
    point of the bot, and refusing to publish one over a packaging preference
    would take it off the air; the fallback is loud, and the operator still
    has something to serve while they sort the binary out.
    """
    final, tmp = _artifact_paths(fmt, date_str)
    if fmt == "rar":
        if _write_rar_artifact(tmp, members):
            return fmt, tmp, final
        fmt = "zip"
        final, tmp = _artifact_paths(fmt, date_str)

    if fmt == "txt":
        _write_text_artifact(tmp, members)
    else:
        _write_zip_artifact(tmp, members)
    return fmt, tmp, final


def generate_master_list():
    """Scan the music directory, clear the old files first, and build both lists."""
    import os
    import sys
    import time
    import datetime
    import zipfile
    import re
    import defaults as config
    import db
    import list as list_mod

    if not os.path.exists(config.LOCAL_LIST_DIR):
        os.makedirs(config.LOCAL_LIST_DIR)

    today = datetime.datetime.now().strftime("%Y-%m-%d")
    txt_filename = f"{config.LIST_BASE_NAME}-{today}.txt"
    rar_filename = f"{config.LIST_BASE_NAME}-RAR-{today}.txt"
    
    txt_path = os.path.join(config.LOCAL_LIST_DIR, txt_filename)
    rar_path = os.path.join(config.LOCAL_LIST_DIR, rar_filename)
    
    # config.RAR_ENABLED (#140) refuses every !rar request. Building an album
    # list anyway, and shipping it inside the zip every user downloads, hands
    # people a file whose every line is an instruction to use a command this
    # bot will refuse - they paste one, get "Folder packing is disabled", and
    # reasonably conclude the bot is broken rather than that the feature is
    # off. Raised on #140 and left as a follow-up there.
    serve_albums = bool(getattr(config, "RAR_ENABLED", True))
    if not serve_albums:
        print("[LIST-GEN] RAR_ENABLED is off - skipping the album list entirely.")

    SIZE_FILE_PATH = os.path.join(config.LOCAL_LIST_DIR, config.LIST_SIZE_FILE)
    RAWBYTES_FILE_PATH = os.path.join(config.LOCAL_LIST_DIR, config.LIST_RAWBYTES_FILE)

    scan_start = time.time()

    # The old lists are NOT deleted here any more. This ran before the scan, so an NFS mount
    # that went away, a disk that filled, or any exception below left the daemon with no index
    # at all: find_latest_list() returns None, @find answers "No MasterList found" and the
    # advert publicly announces 0 files, until someone notices and runs a successful !update.
    #
    # The new lists are written to temporary names alongside the real ones and swapped in only
    # once the whole generation has succeeded (see finalise below). A failed run now leaves the
    # previous index exactly as it was.
    tmp_txt_path = txt_path + ".new"
    tmp_rar_path = rar_path + ".new"
    # Every format's temporary, not just the configured one: a run that asked
    # for .rar and fell back to .zip has staged two, and the guards below have
    # no way of knowing which without repeating the decision.
    tmp_index_paths = (tmp_txt_path, tmp_rar_path)
    tmp_artifact_paths = tuple(_artifact_paths(f, today)[1] for f in list_mod.LIST_FORMATS)
    tmp_all_paths = tmp_index_paths + tmp_artifact_paths

    print(f"[LIST-GEN] Scanning the library in {config.FILE_DIRECTORY}...")

    all_files_data = []
    total_bytes = 0

    # long_path()-wrapped so a deeply-nested path (a real hazard in a music
    # library: "Artist\Album Name (Year)\CD2\12 - A Long Classical Track
    # Title.flac" nests past Windows' 260-char MAX_PATH without trying)
    # does not just vanish from the scan the way the per-file getsize() a
    # few lines down was already protected against. The SAME wrapped value
    # is used as os.path.relpath()'s base below - wrapping only the walk
    # root and comparing it against the unwrapped config.FILE_DIRECTORY
    # would be worse than not wrapping at all: mixing a "\\?\"-prefixed
    # root with an unprefixed base raises ValueError on Windows, turning a
    # silent omission into a hard crash of the entire !update.
    scan_root = platform_compat.long_path(config.FILE_DIRECTORY)

    # Every subtree os.walk() could not read (a stale NFS handle, EIO, a
    # revoked ACL) used to be skipped in total silence under the default
    # onerror=None - total_files_count came out non-zero, so the zero-files
    # guard further down never caught it, and a truncated index was
    # published over the previous good one. Collected here and checked
    # once the walk finishes, so one bad subtree costs the whole run
    # (keeping the previous index) rather than a silent partial one.
    walk_errors = []

    def _on_walk_error(err):
        walk_errors.append(err)
        print(f"[LIST-GEN ERROR] Could not read {err.filename!r} during the scan: {err}")

    for root, dirs, files in os.walk(scan_root, onerror=_on_walk_error):
        # Keep every track under its exact, complete path on disk
        for file in files:
            if file.lower().endswith(('.mp3', '.flac')):
                full_file_path = os.path.join(root, file)
                try:
                    file_bytes = os.path.getsize(platform_compat.long_path(full_file_path))
                except OSError as size_err:
                    # #228: a bare `except: pass` left file_bytes at 0 and the
                    # entry was published anyway - permission denied, a
                    # dangling symlink, or a file removed mid-scan all read as
                    # a legitimate 0-byte track. The list is the thing the
                    # bot HANDS OUT: publishing it meant offering a download
                    # that can only ever fail once someone actually requests
                    # it, with the library's reported total size quietly
                    # short and no log line saying why. Excluded and logged
                    # instead - one bad file costs itself, not the whole scan
                    # (walk_errors above is for a whole SUBTREE going
                    # unreadable, a more systemic failure than one file).
                    print(f"[LIST-GEN ERROR] Skipping {full_file_path!r}, "
                         f"could not read its size: {size_err}")
                    continue
                total_bytes += file_bytes
                rel_dir = os.path.relpath(root, scan_root)
                if rel_dir == ".":
                    rel_dir = ""
                all_files_data.append((rel_dir, file, file_bytes))

    if walk_errors:
        print(f"[LIST-GEN ERROR] {len(walk_errors)} part(s) of the library could not be "
              "read - keeping the previous index rather than publishing a truncated one.")
        return False

    # Sort by the real folder and file names
    all_files_data.sort(key=lambda x: (str(x[0]).lower(), str(x[1]).lower()))

    total_files_count = len(all_files_data)
    scan_end = time.time()
    elapsed_seconds = scan_end - scan_start
    if elapsed_seconds <= 0:
        elapsed_seconds = 0.1

    files_per_second = int(total_files_count / elapsed_seconds)
    def format_total_size(b):
        for unit in ['B','KB','MB','GB','TB']:
            if b < 1024.0: return f"{b:.2f}{unit}"
            b /= 1024.0
        return f"{b:.2f}PB"
    def format_size_human(b):
        return format_total_size(b)

    formatted_size = format_total_size(total_bytes)
    
    time_struct = time.gmtime(elapsed_seconds)
    duration_str = time.strftime("%H:%M:%S", time_struct)

    day = datetime.datetime.now().day
    suffix = "th" if 11 <= day <= 13 else {1: "st", 2: "nd", 3: "rd"}.get(day % 10, "th")
    date_header_str = datetime.datetime.now().strftime(f"{day}{suffix} %b %Y")
    
    try:
        with open(tmp_txt_path, "w", encoding="utf-8") as f, \
             open(tmp_rar_path, "w", encoding="utf-8") as f_rar:
                 
            f.write(f"List of {total_files_count:,} Files ({formatted_size}) generated on {date_header_str} in {duration_str} ( {files_per_second:,} Files Per Second )\n")
            f.write(f"To request a file, copy/paste to the channel... !{config.NICKNAME} FILENAME eg. !{config.NICKNAME} Songname.flac\n\n\n")

            if serve_albums:
                f_rar.write(f"List of Entire Album Folders (!rar) for !{config.NICKNAME} generated on {date_header_str}\n")
                f_rar.write(f"To request an entire album, copy/paste the line... eg. !{config.NICKNAME} !rar D:\\MUSIC\\Album\\\n")
                f_rar.write("="*90 + "\n\n")

            current_folder = None
            written_rar_folders = set()  # Keeps the !rar list free of duplicate rows

            for folder, filename, bytes_size in all_files_data:
                if folder != current_folder:
                    current_folder = folder
                    
                    # The text list gets the complete subfolder (e.g. \Digital Media 1\)
                    raw_folder_str = f"D:\\MUSIC\\{folder}\\" if folder else "D:\\MUSIC\\"
                    display_folder = raw_folder_str.replace("/", "\\")
                    
                    # The rule is drawn to the width of the folder line it wraps, not
                    # to a fixed 53 characters. Against the real library every one of
                    # the 4,107 folder headers is wider than 53 - they run 54 to 136,
                    # averaging 80 - so the fixed rule never once matched the line it
                    # was framing.
                    #
                    # Measured on _one_line()'s output rather than the raw string:
                    # that is what actually gets written, and it flattens control
                    # characters, which changes the length.
                    folder_line = _one_line(display_folder)
                    folder_rule = "=" * len(folder_line)
                    f.write(f"\n{folder_rule}\n")
                    f.write(f"{folder_line}\n")
                    f.write(f"{folder_rule}\n")
                    
                    # Strip multi-disc suffixes, for the !rar album list ONLY.
                    #
                    # Matched as a whole PATH SEGMENT (split on the same
                    # separators the folder can carry), not a substring
                    # anywhere in the path - a substring match let "\disc"
                    # fire on "\Discography" and "\media" fire on "\Media
                    # Markt Hits", collapsing a real album folder to the
                    # ARTIST root, which dcc.py refuses outright ("Artist
                    # root folders cannot be requested"). The album then had
                    # no requestable row at all: written_rar_folders
                    # deduplicates on the truncated (wrong) string.
                    #
                    # Also requires the truncation to leave at least two
                    # segments below FILE_DIRECTORY - i.e. never collapse to
                    # the artist root - and finds the EARLIEST matching
                    # segment by walking the path in order, rather than the
                    # old "first box word in LIST order" behaviour, which
                    # made the truncation point depend on the order this
                    # list happened to be written in.
                    if folder and serve_albums:
                        folder_segments = re.split(r'[\\/]', folder)
                        truncate_at = None
                        for seg_index, segment in enumerate(folder_segments):
                            if _BOX_WORD_RE.match(segment.strip()):
                                truncate_at = seg_index
                                break
                        if truncate_at is not None and truncate_at >= 2:
                            rar_folder_clean = "/".join(folder_segments[:truncate_at])
                        else:
                            # Truncating here would collapse to the artist
                            # root (or nothing) - offering the untruncated
                            # real path, box word and all, is still a
                            # request dcc.py will actually serve; the
                            # refused artist root is not.
                            rar_folder_clean = folder

                        raw_rar_str = f"D:\\MUSIC\\{rar_folder_clean}\\"
                        display_rar_folder = raw_rar_str.replace("/", "\\")
                        
                        # Write the row exactly once per album to the .rar text file
                        if display_rar_folder not in written_rar_folders:
                            f_rar.write(f"!{config.NICKNAME} !rar {_one_line(display_rar_folder)}\n")
                            written_rar_folders.add(display_rar_folder)
                single_file_size = format_size_human(bytes_size)
                f.write(f"!{config.NICKNAME} {_one_line(filename)}  ::INFO:: {single_file_size}\n")

        print(f"[LIST-GEN] Text list created: {tmp_txt_path}")
        if serve_albums:
            print(f"[LIST-GEN] RAR album list created: {tmp_rar_path}")
        
        # The size and rawbytes side files used to be published here. They are now
        # written with the lists in the finalise section below - see the comment
        # there for why writing them ahead of the guards was half a rollback.
            
        # A scan that found NOTHING is almost always an unavailable mount, not an empty
        # library - and publishing it would replace a good index with an empty one, which is
        # exactly the failure this rewrite exists to prevent. Size cannot be the test: the
        # file always carries two header lines, so it is never zero bytes.
        #
        # Accept an empty result only when there is no working index to lose, so a genuine
        # first run on an empty library still succeeds.
        if total_files_count == 0:
            try:
                existing = [f for f in os.listdir(config.LOCAL_LIST_DIR)
                            if f.startswith(config.LIST_BASE_NAME)
                            and f.endswith(".txt") and "-RAR-" not in f]
            except OSError:
                existing = []
            if existing:
                print("[LIST-GEN ERROR] Scan found 0 files but an index already exists "
                      "(mount unavailable?). Keeping the previous index.")
                _discard_temp_lists(*tmp_all_paths)
                return False
            print("[LIST-GEN] Scan found 0 files and there is no previous index; "
                  "publishing an empty list.")

        if not os.path.exists(tmp_txt_path):
            print("[LIST-GEN ERROR] No list was written. Keeping the previous index.")
            _discard_temp_lists(*tmp_all_paths)
            return False

        # ---- what a user actually downloads -----------------------------
        # config.LIST_FORMAT decides how the two text lists are handed over:
        # as one plain .txt, packed into a .zip, or packed into a .rar.
        # OmenServe has offered the same three for years, and which of them a
        # given person's client opens without complaint still differs.
        #
        # This is deliberately NOT tied to RAR_ENABLED. That switch governs
        # whether the bot will pack an ALBUM FOLDER for a stranger on demand -
        # minutes of CPU and a large temporary file, once per request. Packing
        # two text files once per rebuild, on the operator's own schedule, is
        # a different job and does not belong behind the same switch.
        members = [(tmp_txt_path, os.path.basename(txt_path))]
        if os.path.exists(tmp_rar_path) and os.path.getsize(tmp_rar_path) > 0:
            members.append((tmp_rar_path, os.path.basename(rar_path)))

        wanted_format = list_mod.list_format()
        print(f"[LIST-GEN] Packing the list for download as .{wanted_format}...")
        artifact_format, tmp_artifact_path, artifact_path = build_list_artifact(
            wanted_format, members, today)

        # Everything generated cleanly. Swap the new files in, THEN remove the superseded
        # ones. os.replace overwrites atomically on both POSIX and Windows, where os.rename
        # would raise because the destination already exists.
        #
        platform_compat.replace_with_retry(tmp_txt_path, txt_path)
        if serve_albums:
            platform_compat.replace_with_retry(tmp_rar_path, rar_path)
        else:
            # Not published, and not left behind either: an empty album list
            # in lists/ reads as "this bot offers no albums" to anything
            # counting the file, which is a different claim from "it does
            # not offer them at all".
            _discard_temp_lists(tmp_rar_path)
        platform_compat.replace_with_retry(tmp_artifact_path, artifact_path)

        # The two side files are published HERE, AFTER every swap above has
        # already succeeded, and atomically. They used to be written before
        # the swaps - so a failure partway through (a Windows os.replace can
        # raise PermissionError if something else has the destination open)
        # rolled the list itself back to the previous index while the size
        # and byte count it wears had already been overwritten with the new
        # scan's numbers: an old index publishing a new scan's size. And a
        # plain open(..., "w") truncates first, so an interruption left a
        # readable but EMPTY file, which is unparseable and used to cost the
        # caller the file count and the list date as well as the size.
        db._atomic_write(SIZE_FILE_PATH, formatted_size)
        db._atomic_write(RAWBYTES_FILE_PATH, str(total_bytes))
        # #213: the artifacts just published are named after the CURRENT
        # LIST_BASE_NAME, so this is the moment that fact becomes true. Written
        # here rather than only in the migration, so a rename between two
        # rebuilds is still migrated from the right name.
        write_list_base_marker(config.LIST_BASE_NAME, log=print)

        print(f"[LIST-GEN] New lists activated: {os.path.basename(txt_path)} "
              f"(download: {os.path.basename(artifact_path)})")

        keep = {os.path.basename(txt_path), os.path.basename(artifact_path)}
        if serve_albums:
            keep.add(os.path.basename(rar_path))
        # Yesterday's artifact in another format goes with the rest. It is not
        # only clutter: find_latest_list_file() falls back to whatever HAS been
        # built when the configured format has not been yet, so a stale .zip
        # left beside a fresh .rar would go on being handed out to somebody the
        # day the operator switched formats and the build failed.
        _prune_superseded_lists(keep=keep)
        return True
            
    except Exception as e:
        print(f"[LIST-GEN ERROR] Failed to generate the lists: {e}")
        print("[LIST-GEN] The previous list was left untouched and is still in use.")
        _discard_temp_lists(*tmp_all_paths)
        return False

if __name__ == "__main__":
    print("--- Starting the scheduled weekly file-list update ---")
    # FILE_DIRECTORY is not in settings_file.REQUIRED (see its own comment) -
    # a blank value is a supported "not chosen yet" state the daemon itself
    # boots fine with, so os.path.exists(None) here (a TypeError, not a
    # clean failure) must be guarded against explicitly rather than assuming
    # a real string ever reaches this point.
    if not config.FILE_DIRECTORY:
        print("[CRITICAL] No music directory configured yet - set FILE_DIRECTORY "
              "from the web dashboard's Settings page, settings.conf, or "
              "admin_config.py before running this.")
        sys.exit(1)
    if not os.path.exists(config.FILE_DIRECTORY):
        print(f"[CRITICAL] Missing music directory: {config.FILE_DIRECTORY}")
        sys.exit(1)
        
    success = generate_master_list()
    if success:
        print("--- The list was updated successfully. ---")
        sys.exit(0)
    else:
        print("--- ERROR: could not generate the list. ---")
        sys.exit(1)
