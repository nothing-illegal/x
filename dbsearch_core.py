#!/usr/bin/env python3
"""
dbsearch_core - shared streaming search engine used by both the CLI
(dbsearch.py) and the GUI (dbsearch_gui.py).

Design notes:
  * Reads in fixed-size chunks; memory stays flat regardless of file size.
  * Uses bulk bytes.split(b"\\n") on the fast path. Do NOT re-slice the
    buffer per line -- that makes the scan O(n^2) (1.1GB went from 9s to
    >220s when this was done naively).
  * Operates on bytes, so binary formats (SQLite, etc.) work too.
  * Files are only ever opened read-only.
"""

import os
import re
import sys

CHUNK = 8 * 1024 * 1024        # 8 MiB read size
MAX_LINE = 16 * 1024 * 1024    # max bytes buffered for a single line

# ---------------------------------------------------------------- discovery
# Extensions treated as "database-ish". Lowercase, with leading dot.
DB_EXTENSIONS = {
    # dumps / text
    ".sql", ".dump", ".pgsql", ".psql", ".ddl",
    # delimited text
    ".csv", ".tsv", ".psv",
    # sqlite
    ".db", ".db3", ".sqlite", ".sqlite3", ".sqlitedb", ".s3db",
    # ms sql / access
    ".mdf", ".ndf", ".ldf", ".bak", ".mdb", ".accdb",
    # mysql innodb/myisam internals
    ".ibd", ".myd", ".myi", ".frm",
    # misc
    ".dbf", ".dat",
}

# Archives we can detect but cannot search as plain bytes without extracting.
COMPRESSED_EXTENSIONS = {".gz", ".bz2", ".xz", ".zip", ".7z", ".tgz", ".zst"}

# Directories never worth descending into. @eaDir/#recycle/#snapshot are
# NAS-specific (Synology/uGreen thumbnail + recycle stores) and can contain
# huge numbers of useless files.
SKIP_DIRS = {
    "@eadir", "#recycle", "#snapshot", "@tmp", ".@__thumb",
    "$recycle.bin", "system volume information", "recycler",
    ".git", ".svn", ".hg", "node_modules", "__pycache__",
    ".cache", ".thumbnails", "lost+found",
}

# (offset, magic bytes, label) sniffed from the first bytes of a file.
MAGIC = [
    (0, b"SQLite format 3\x00", "sqlite"),
    (0, b"-- MySQL dump", "mysqldump"),
    (0, b"-- MariaDB dump", "mysqldump"),
    (0, b"--\n-- PostgreSQL database dump", "pgdump"),
    (0, b"PGDMP", "pgdump-custom"),
    (0, b"\x1f\x8b", "gzip"),
    (0, b"PK\x03\x04", "zip"),
    (0, b"BZh", "bzip2"),
    (0, b"\xfd7zXZ\x00", "xz"),
    (0, b"\x53\x51\x4c\x69", "sqlite?"),
    (0, b"\x00\x01\x00\x00Standard Jet DB", "access"),
    (0, b"\x01\x0f\x00\x00", "mssql"),
]

COMPRESSED_KINDS = {"gzip", "zip", "bzip2", "xz"}

# Control chars -> visible middle dot (see decode(..., sanitize=True)).
_CTRL_MAP = {c: "\u00b7" for c in range(0x20)}
_CTRL_MAP.update({c: "\u00b7" for c in range(0x7F, 0xA0)})
_CTRL_MAP.pop(0x09, None)   # keep tabs


def sniff_kind(path, ext_hint=""):
    """Peek at the first bytes to classify a file. Returns a short label, or
    '' if nothing recognised. Never raises."""
    try:
        with open(path, "rb") as fh:
            head = fh.read(64)
    except OSError:
        return ""
    for off, magic, label in MAGIC:
        if head[off:off + len(magic)] == magic:
            return label
    if ext_hint in (".sql", ".dump", ".ddl"):
        sample = head.lstrip()[:16].upper()
        for kw in (b"--", b"/*", b"CREATE", b"INSERT", b"DROP", b"SET ",
                   b"BEGIN", b"COPY"):
            if sample.startswith(kw):
                return "sql-text"
    if ext_hint in (".csv", ".tsv", ".psv"):
        return "delimited"
    return ""


def find_db_files(roots, recursive=True, extensions=None, min_size=0,
                  max_size=0, sniff=True, include_compressed=False,
                  follow_symlinks=False, cancel=None, on_scan=None):
    """Walk `roots` and yield dicts describing candidate database files:

        {'path','size','ext','kind','searchable','reason'}

    A file qualifies if its extension is in `extensions` OR (when `sniff` is
    on) its magic bytes identify it as a database/dump. Compressed files are
    reported with searchable=False unless `include_compressed`.

    Unreadable directories are skipped silently rather than aborting the walk,
    which matters on a NAS where some shares deny access. `on_scan(path)` is
    called periodically with the directory being scanned, for progress UI.
    """
    exts = DB_EXTENSIONS if extensions is None else {
        e.lower() if e.startswith(".") else "." + e.lower() for e in extensions}
    if isinstance(roots, (str, bytes, os.PathLike)):
        roots = [roots]

    seen = set()        # resolved paths, so overlapping roots don't duplicate
    visited_dirs = set()

    def consider(path):
        try:
            st = os.stat(path)
        except OSError:
            return None
        size = st.st_size
        real = os.path.realpath(path)
        if real in seen:
            return None
        ext = os.path.splitext(path)[1].lower()
        kind = sniff_kind(path, ext) if sniff else ""
        is_comp = ext in COMPRESSED_EXTENSIONS or kind in COMPRESSED_KINDS
        # does it qualify at all?
        if ext not in exts and not kind:
            return None
        if is_comp and not include_compressed and ext not in exts:
            return None
        if min_size and size < min_size:
            return None
        if max_size and size > max_size:
            return None
        seen.add(real)
        searchable = not is_comp
        reason = "" if searchable else "compressed - extract before searching"
        return {"path": path, "size": size, "ext": ext,
                "kind": kind or (ext.lstrip(".") or "file"),
                "searchable": searchable, "reason": reason}

    for root in roots:
        if cancel is not None and cancel.is_set():
            return
        if os.path.isfile(root):
            # An explicit file path is always accepted, extension or not.
            try:
                st = os.stat(root)
                real = os.path.realpath(root)
                if real not in seen:
                    seen.add(real)
                    ext = os.path.splitext(root)[1].lower()
                    kind = sniff_kind(root, ext) if sniff else ""
                    comp = (ext in COMPRESSED_EXTENSIONS
                            or kind in COMPRESSED_KINDS)
                    yield {"path": root, "size": st.st_size, "ext": ext,
                           "kind": kind or (ext.lstrip(".") or "file"),
                           "searchable": not comp,
                           "reason": "" if not comp
                                     else "compressed - extract first"}
            except OSError:
                pass
            continue

        for dirpath, dirnames, filenames in os.walk(
                root, topdown=True, followlinks=follow_symlinks):
            if cancel is not None and cancel.is_set():
                return
            # prune junk dirs in place
            dirnames[:] = [d for d in dirnames
                           if d.lower() not in SKIP_DIRS
                           and not d.startswith("$")]
            if not follow_symlinks:
                real_dir = os.path.realpath(dirpath)
                if real_dir in visited_dirs:
                    dirnames[:] = []
                    continue
                visited_dirs.add(real_dir)
            if on_scan:
                on_scan(dirpath)
            for name in filenames:
                if cancel is not None and cancel.is_set():
                    return
                info = consider(os.path.join(dirpath, name))
                if info:
                    yield info
            if not recursive:
                dirnames[:] = []


def resource_path(relative):
    """Path to a bundled data file, working both from source and from a
    PyInstaller --onefile bundle (which unpacks into sys._MEIPASS)."""
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, relative)


def human(n):
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{int(n)}B" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}PB"


def iter_lines(path, chunk_size=CHUNK, max_line=MAX_LINE, overlap=0):
    """Yield (line_number, byte_offset_of_line_start, line_bytes, is_fragment).

    Lines longer than `max_line` bytes (mysqldump can emit one multi-GB INSERT
    line per table) are emitted as bounded fragments rather than buffered whole,
    so memory stays flat. Each fragment carries `overlap` trailing bytes forward
    so a match straddling a fragment boundary is still found.
    """
    lineno = 0
    offset = 0          # byte offset of the start of `rest`
    rest = b""          # bytes after the last newline seen so far
    frag = False        # currently mid-way through an over-long line
    with open(path, "rb") as fh:
        while True:
            chunk = fh.read(chunk_size)
            if not chunk:
                break
            # Fast path: bulk split, O(n) in C.
            parts = (rest + chunk).split(b"\n")
            rest = parts.pop()          # trailing, possibly incomplete line
            for part in parts:
                if not frag:
                    lineno += 1
                yield lineno, offset, part, frag
                offset += len(part) + 1
                frag = False
            # Slow path only for a genuinely over-long line (no newline yet).
            while len(rest) > max_line:
                cut = max_line - overlap if overlap < max_line else max_line
                piece = rest[:cut]
                rest = rest[cut - overlap:] if overlap else rest[cut:]
                if not frag:
                    lineno += 1
                yield lineno, offset, piece, frag
                offset += cut - overlap if overlap else cut
                frag = True
    if rest:
        if not frag:
            lineno += 1
        yield lineno, offset, rest, frag


def build_matcher(pattern, ignore_case=False, regex=False):
    """Return (test_fn, pattern_bytes). Raises re.error on a bad regex."""
    pat = pattern.encode("utf-8", errors="surrogateescape")
    if regex:
        rx = re.compile(pat, re.IGNORECASE if ignore_case else 0)
        return (lambda line: rx.search(line) is not None), pat
    if ignore_case:
        low = pat.lower()
        return (lambda line: low in line.lower()), pat
    return (lambda line: pat in line), pat


def decode(b, limit=0, sanitize=False):
    """Decode bytes for display.

    sanitize=True replaces control characters with a visible dot. This is
    REQUIRED before inserting into a Tk Text widget: Tcl strings are
    NUL-terminated, so a single 0x00 byte silently truncates the insert and
    swallows the rest of the line (including the newline), which otherwise
    makes binary files such as SQLite render as garbage.
    """
    s = b.decode("utf-8", errors="replace").rstrip("\r")
    if sanitize:
        s = s.translate(_CTRL_MAP)
    if limit and len(s) > limit:
        s = s[:limit] + f"  ... [line truncated, {len(s)} chars total]"
    return s


def search(path, pattern, ignore_case=False, regex=False, context=0,
           max_matches=0, max_line=MAX_LINE, cancel=None, progress_every=0):
    """Generator of result events. Each event is a dict with a 'type':

        {'type':'match',    'lineno','offset','text','fragment'}
        {'type':'context',  'lineno','offset','text','before'}
        {'type':'gap'}                       -- break between match groups
        {'type':'progress', 'scanned','total','matches'}
        {'type':'done',     'matches','scanned','total','cancelled','stopped'}

    `text` is raw bytes; the caller decides how to decode/truncate it.
    `cancel` may be a threading.Event; when set, the scan stops promptly.
    """
    from collections import deque

    total = os.path.getsize(path)
    test, pat = build_matcher(pattern, ignore_case, regex)
    overlap = 0 if regex else min(max(len(pat) - 1, 0), 1 << 20)

    before = deque(maxlen=context) if context else None
    after_left = 0
    matches = 0
    scanned = 0
    last_emitted = 0
    next_tick = progress_every
    stopped = False
    cancelled = False

    for lineno, offset, line, frag in iter_lines(
            path, max_line=max_line, overlap=overlap):
        if cancel is not None and cancel.is_set():
            cancelled = True
            break
        scanned = offset + len(line)

        if test(line):
            matches += 1
            if before:
                if last_emitted and before[0][0] > last_emitted + 1:
                    yield {"type": "gap"}
                for bn, bo, bl in before:
                    if bn > last_emitted:
                        yield {"type": "context", "lineno": bn, "offset": bo,
                               "text": bl, "before": True}
                        last_emitted = bn
                before.clear()
            elif context == 0 and last_emitted and lineno > last_emitted + 1:
                pass
            yield {"type": "match", "lineno": lineno, "offset": offset,
                   "text": line, "fragment": frag}
            last_emitted = lineno
            after_left = context
            if max_matches and matches >= max_matches:
                stopped = True
                break
        else:
            if after_left > 0:
                yield {"type": "context", "lineno": lineno, "offset": offset,
                       "text": line, "before": False}
                last_emitted = lineno
                after_left -= 1
            if before is not None:
                before.append((lineno, offset, line))

        if next_tick and scanned >= next_tick:
            yield {"type": "progress", "scanned": scanned, "total": total,
                   "matches": matches}
            next_tick += progress_every

    yield {"type": "done", "matches": matches, "scanned": scanned,
           "total": total, "cancelled": cancelled, "stopped": stopped}


def search_many(paths, pattern, ignore_case=False, regex=False, context=0,
                max_matches=0, max_total_matches=0, max_line=MAX_LINE,
                cancel=None, progress_every=0):
    """Search a list of files in sequence.

    `paths` may be plain path strings or dicts from find_db_files().
    Yields the same events as search(), with two extra wrappers and a
    'path' key added to every per-file event:

        {'type':'file_start','path','size','index','count'}
        {'type':'file_done', 'path','matches','scanned','error'}
        {'type':'all_done',  'files','files_with_hits','matches','scanned',
                             'cancelled','errors'}

    `max_matches` limits matches *per file*; `max_total_matches` stops the
    whole run. A file that cannot be read is reported via file_done['error']
    and does not abort the batch.
    """
    items = []
    for p in paths:
        if isinstance(p, dict):
            items.append((p["path"], p.get("size", 0)))
        else:
            try:
                items.append((p, os.path.getsize(p)))
            except OSError:
                items.append((p, 0))

    grand_total = sum(s for _, s in items) or 1
    done_bytes = 0
    total_matches = 0
    files_with_hits = 0
    errors = []
    cancelled = False

    for idx, (path, size) in enumerate(items, 1):
        if cancel is not None and cancel.is_set():
            cancelled = True
            break
        yield {"type": "file_start", "path": path, "size": size,
               "index": idx, "count": len(items)}

        file_matches = 0
        scanned = 0
        err = None
        try:
            remaining = (max_total_matches - total_matches
                         if max_total_matches else 0)
            if max_total_matches and remaining <= 0:
                break
            per_file_cap = max_matches
            if remaining:
                per_file_cap = (min(per_file_cap, remaining)
                                if per_file_cap else remaining)

            for ev in search(path, pattern, ignore_case=ignore_case,
                             regex=regex, context=context,
                             max_matches=per_file_cap, max_line=max_line,
                             cancel=cancel, progress_every=progress_every):
                t = ev["type"]
                if t == "done":
                    file_matches = ev["matches"]
                    scanned = ev["scanned"]
                    cancelled = cancelled or ev["cancelled"]
                    break
                if t == "progress":
                    # rescale per-file progress onto the whole batch
                    yield {"type": "progress",
                           "scanned": done_bytes + ev["scanned"],
                           "total": grand_total,
                           "matches": total_matches + ev["matches"],
                           "path": path}
                    continue
                ev = dict(ev, path=path)
                yield ev
        except (OSError, re.error) as e:
            err = str(e)
            errors.append((path, err))

        done_bytes += size or scanned
        total_matches += file_matches
        if file_matches:
            files_with_hits += 1
        yield {"type": "file_done", "path": path, "matches": file_matches,
               "scanned": scanned, "error": err}

        if max_total_matches and total_matches >= max_total_matches:
            break

    yield {"type": "all_done", "files": len(items),
           "files_with_hits": files_with_hits, "matches": total_matches,
           "scanned": done_bytes, "cancelled": cancelled, "errors": errors}
