#!/usr/bin/env python3
"""
dbsearch (CLI) - find database files and stream-search them for a text string.

TARGET may be a single file OR a folder. Given a folder, dbsearch discovers
database-ish files inside it (by extension and by magic bytes) and searches
every one of them.

For the graphical version, run dbsearch_gui.py (or DBSearch.exe).

Examples:
  dbsearch.exe D:\\data\\mydb.sql "john@example.com"
  dbsearch.exe \\\\NAS\\share "john@example.com" -i
  dbsearch.exe \\\\NAS\\share "ORDER_ID" --list-only
  dbsearch.exe D:\\dumps "^INSERT INTO .users." --regex -C 2 -o hits.txt
"""

import argparse
import os
import re
import sys

from dbsearch_core import (DB_EXTENSIONS, MAX_LINE, decode, find_db_files,
                           human, search_many)


def parse_size(text):
    """'10MB' / '500k' / '1024' -> bytes."""
    if not text:
        return 0
    t = str(text).strip().upper().replace("IB", "B")
    mult = 1
    for suf, m in (("TB", 1 << 40), ("GB", 1 << 30), ("MB", 1 << 20),
                   ("KB", 1 << 10), ("T", 1 << 40), ("G", 1 << 30),
                   ("M", 1 << 20), ("K", 1 << 10), ("B", 1)):
        if t.endswith(suf):
            t, mult = t[:-len(suf)], m
            break
    try:
        return int(float(t) * mult)
    except ValueError:
        raise argparse.ArgumentTypeError(f"bad size: {text!r}")


def main(argv=None):
    p = argparse.ArgumentParser(
        prog="dbsearch",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description="Find database files and search them for a text string.",
        epilog="TARGET may be a file or a folder. Folders are scanned for "
               "database files, which are then all searched.")
    p.add_argument("target", help="file OR folder to search (read-only)")
    p.add_argument("pattern", nargs="?",
                   help="text string (or regex with --regex) to find; "
                        "may be omitted with --list-only")
    p.add_argument("-i", "--ignore-case", action="store_true")
    p.add_argument("-r", "--regex", action="store_true",
                   help="treat pattern as a regular expression")
    p.add_argument("-C", "--context", type=int, default=0, metavar="N",
                   help="show N lines of context before and after each match")
    p.add_argument("-m", "--max-matches", type=int, default=0, metavar="N",
                   help="stop after N matches per file (0 = no limit)")
    p.add_argument("-M", "--max-total", type=int, default=0, metavar="N",
                   help="stop the whole run after N matches (0 = no limit)")
    p.add_argument("-w", "--max-width", type=int, default=400, metavar="N",
                   help="truncate displayed lines to N chars (0 = no limit)")
    p.add_argument("-o", "--output", metavar="FILE",
                   help="also write results to this file (UTF-8)")
    p.add_argument("-q", "--quiet", action="store_true",
                   help="print only the summary, not each match")
    p.add_argument("--max-line-bytes", type=int, default=MAX_LINE, metavar="N",
                   help="max bytes buffered per line (default 16MiB)")

    g = p.add_argument_group("file discovery (when TARGET is a folder)")
    g.add_argument("--no-recursive", action="store_true",
                   help="only the top folder, do not descend")
    g.add_argument("--ext", metavar="LIST",
                   help="comma-separated extensions to accept instead of the "
                        "built-in list (e.g. 'sql,csv,db')")
    g.add_argument("--no-sniff", action="store_true",
                   help="match by extension only; do not inspect magic bytes")
    g.add_argument("--min-size", type=parse_size, default=0, metavar="SZ",
                   help="ignore files smaller than this (e.g. 1MB)")
    g.add_argument("--max-size", type=parse_size, default=0, metavar="SZ",
                   help="ignore files larger than this")
    g.add_argument("--include-compressed", action="store_true",
                   help="list .gz/.zip etc too (they cannot be searched "
                        "without extracting)")
    g.add_argument("--follow-symlinks", action="store_true")
    g.add_argument("--list-only", action="store_true",
                   help="list the database files found, then exit")

    args = p.parse_args(argv)

    if not args.pattern and not args.list_only:
        p.error("a PATTERN is required unless --list-only is given")
    if not os.path.exists(args.target):
        p.error(f"path not found: {args.target}")
    if args.regex and args.pattern:
        try:
            re.compile(args.pattern)
        except re.error as e:
            p.error(f"invalid regex: {e}")

    exts = None
    if args.ext:
        exts = [e.strip() for e in args.ext.split(",") if e.strip()]

    out = open(args.output, "w", encoding="utf-8") if args.output else None

    def emit(text="", always=False):
        if always or not args.quiet:
            print(text)
        if out:
            out.write(text + "\n")

    # ---------------------------------------------------------- discovery
    is_dir = os.path.isdir(args.target)
    if is_dir:
        print(f"Scanning {args.target} for database files…", file=sys.stderr)
    found = list(find_db_files(
        args.target, recursive=not args.no_recursive, extensions=exts,
        min_size=args.min_size, max_size=args.max_size,
        sniff=not args.no_sniff,
        include_compressed=args.include_compressed or args.list_only,
        follow_symlinks=args.follow_symlinks))

    if not found:
        print(f"No database files found in {args.target}", file=sys.stderr)
        if out:
            out.close()
        return 1

    searchable = [f for f in found if f["searchable"]]
    skipped = [f for f in found if not f["searchable"]]
    total_bytes = sum(f["size"] for f in searchable)

    if is_dir or args.list_only:
        emit(f"Found {len(found)} database file(s) in {args.target} "
             f"({human(total_bytes)} to scan)", always=True)
        if args.list_only or not args.quiet:
            for f in found:
                flag = "  " if f["searchable"] else "! "
                emit(f"  {flag}{human(f['size']):>9}  {f['kind']:<12} "
                     f"{f['path']}"
                     f"{'   [' + f['reason'] + ']' if f['reason'] else ''}",
                     always=args.list_only)
        emit("", always=True)

    if args.list_only:
        if out:
            out.close()
        return 0 if found else 1

    if not searchable:
        print("Nothing searchable (all candidates are compressed).",
              file=sys.stderr)
        if out:
            out.close()
        return 1

    # ------------------------------------------------------------ search
    emit(f"Pattern  : {args.pattern!r}"
         f"{'  [regex]' if args.regex else ''}"
         f"{'  [ignore-case]' if args.ignore_case else ''}", always=True)
    emit("=" * 78, always=True)

    per_file = {}
    cur_hits = 0
    matches = 0
    try:
        for ev in search_many(
                searchable, args.pattern, ignore_case=args.ignore_case,
                regex=args.regex, context=args.context,
                max_matches=args.max_matches, max_total_matches=args.max_total,
                max_line=args.max_line_bytes,
                progress_every=256 * 1024 * 1024):
            t = ev["type"]
            if t == "file_start":
                cur_hits = 0
                if not args.quiet:
                    print(f"[{ev['index']}/{ev['count']}] {ev['path']} "
                          f"({human(ev['size'])})", file=sys.stderr)
            elif t == "match":
                if cur_hits == 0:
                    emit(f"\n--- {ev['path']}")
                cur_hits += 1
                tag = "~" if ev["fragment"] else ":"
                emit(f"  {ev['lineno']}{tag} (byte {ev['offset']}) "
                     f"{decode(ev['text'], args.max_width, sanitize=True)}")
            elif t == "context":
                emit(f"  {ev['lineno']}- (byte {ev['offset']}) "
                     f"{decode(ev['text'], args.max_width, sanitize=True)}")
            elif t == "gap":
                emit("  --")
            elif t == "progress":
                pct = 100.0 * ev["scanned"] / ev["total"] if ev["total"] else 0
                print(f"[... {pct:.0f}% of batch, {ev['matches']} match(es)]",
                      file=sys.stderr)
            elif t == "file_done":
                per_file[ev["path"]] = ev["matches"]
                if ev["error"]:
                    emit(f"  ! could not read: {ev['error']}", always=True)
            elif t == "all_done":
                matches = ev["matches"]
                emit("", always=True)
                emit("=" * 78, always=True)
                emit(f"Searched {ev['files']} file(s), "
                     f"{human(ev['scanned'])} total", always=True)
                emit(f"Files with matches: {ev['files_with_hits']}",
                     always=True)
                for path, n in sorted(per_file.items(), key=lambda kv: -kv[1]):
                    if n:
                        emit(f"  {n:>8,}  {path}", always=True)
                if skipped:
                    emit(f"Skipped {len(skipped)} compressed file(s); "
                         f"extract them to search.", always=True)
                if ev["errors"]:
                    emit(f"Unreadable: {len(ev['errors'])} file(s)",
                         always=True)
                emit(f"TOTAL MATCHES: {ev['matches']:,}", always=True)
    except KeyboardInterrupt:
        emit("\n[interrupted by user]", always=True)
        if out:
            out.close()
        return 130
    except re.error as e:
        print(f"invalid regex: {e}", file=sys.stderr)
        if out:
            out.close()
        return 2

    if args.output:
        print(f"Results written to {args.output}", file=sys.stderr)
    if out:
        out.close()
    return 0 if matches else 1


if __name__ == "__main__":
    sys.exit(main())
