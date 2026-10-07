#!/usr/bin/env python3
"""
dbsearch_gui - Material-dark GUI front-end for dbsearch_core.

Workflow: pick a folder (or a file) -> "Find database files" discovers every
database/dump inside it -> tick the ones you want -> "Search" scans them all,
streaming results as they are found.

Fonts: bundles Roboto / Roboto Mono (Apache 2.0) and loads them at runtime
*without installing them system-wide*. Falls back to Segoe UI / system fonts
if the private-font load fails, so the app always starts.

Threading: discovery and scanning both run on worker threads and stream
batched events to the UI through a Queue, so the window stays responsive and
cancellable even across a multi-gigabyte NAS share.
"""

import os
import queue
import re
import sys
import threading
import time
import traceback

import tkinter as tk
from tkinter import filedialog, font as tkfont, messagebox, ttk

from dbsearch_core import (MAX_LINE, decode, find_db_files, human,
                           resource_path, search_many)

APP_TITLE = "DB Search"

# ---------------------------------------------------------------- palette
# Material Design dark-theme surfaces + accents.
C = {
    "bg":         "#121212",   # app background
    "surface":    "#1E1E1E",   # cards / inputs
    "surface2":   "#252525",   # elevated (results area)
    "hover":      "#2F2F2F",
    "border":     "#373737",
    "primary":    "#BB86FC",   # Material purple 200
    "primary_d":  "#8E5BD0",
    "secondary":  "#03DAC6",   # Material teal 200
    "error":      "#CF6679",
    "on_bg":      "#E6E1E5",   # high-emphasis text
    "muted":      "#9E9E9E",   # medium-emphasis text
    "disabled":   "#5F5F5F",
    "hit_bg":     "#4A3A68",   # highlighted match substring
    "on_primary": "#1A0033",
    "row_alt":    "#1C1C1C",
}

FONT_FILES = ["Roboto-Regular.ttf", "Roboto-Medium.ttf", "RobotoMono-Regular.ttf"]
UI_STACK = ["Roboto", "Segoe UI", "Helvetica Neue", "DejaVu Sans", "Arial"]
MONO_STACK = ["Roboto Mono", "Consolas", "DejaVu Sans Mono", "Courier New"]

DISPLAY_CAP = 5000       # max result rows rendered (scan still counts all)
BATCH_MS = 60            # UI drain interval
MAX_EVENTS_PER_TICK = 400    # bound work per UI callback to stay responsive
CHECK_ON, CHECK_OFF = "\u2611", "\u2610"


# ------------------------------------------------------------ font loading
def load_bundled_fonts():
    """Register bundled TTFs with the OS for this process only.
    Returns notes describing what happened (shown in the log pane)."""
    notes = []
    paths = []
    for name in FONT_FILES:
        p = resource_path(os.path.join("fonts", name))
        if os.path.isfile(p):
            paths.append(p)
        else:
            notes.append(f"bundled font not found: {name}")
    if not paths:
        return notes

    if sys.platform == "win32":
        try:
            import ctypes
            FR_PRIVATE = 0x10
            gdi32 = ctypes.WinDLL("gdi32")
            added = sum(1 for p in paths
                        if gdi32.AddFontResourceExW(ctypes.c_wchar_p(p),
                                                    FR_PRIVATE, 0))
            notes.append(f"loaded {added}/{len(paths)} bundled fonts")
        except Exception as e:                                 # pragma: no cover
            notes.append(f"private font load failed ({e}); using system fonts")
    else:
        try:
            import shutil
            import subprocess
            dest = os.path.expanduser("~/.local/share/fonts/dbsearch")
            os.makedirs(dest, exist_ok=True)
            for p in paths:
                tgt = os.path.join(dest, os.path.basename(p))
                if not os.path.exists(tgt):
                    shutil.copy2(p, tgt)
            subprocess.run(["fc-cache", "-f", dest], timeout=30,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            notes.append("registered bundled fonts via fontconfig")
        except Exception as e:                                 # pragma: no cover
            notes.append(f"fontconfig registration failed ({e})")
    return notes


def pick_family(stack, available):
    for fam in stack:
        if fam.lower() in available:
            return fam
    return stack[-1]


# --------------------------------------------------------- match highlight
def highlight_spans(text, pattern, ignore_case, regex, limit=200):
    """(start,end) char spans of `pattern` inside a displayed line.
    Purely cosmetic and best-effort; never allowed to break rendering."""
    if not pattern:
        return []
    spans = []
    try:
        if regex:
            rx = re.compile(pattern, re.IGNORECASE if ignore_case else 0)
            for m in rx.finditer(text):
                if m.end() > m.start():
                    spans.append((m.start(), m.end()))
                if len(spans) >= limit:
                    break
        else:
            hay = text.lower() if ignore_case else text
            needle = pattern.lower() if ignore_case else pattern
            i = hay.find(needle)
            while i >= 0 and len(spans) < limit:
                spans.append((i, i + len(needle)))
                i = hay.find(needle, i + len(needle))
    except re.error:
        return []
    return spans


# ------------------------------------------------------------------- app
class App:
    def __init__(self, root):
        self.root = root
        self.worker = None
        self.cancel_evt = None
        self.q = queue.Queue()
        self.mode = None            # 'scan' or 'search'
        self.files = []             # discovered file dicts
        self.checked = {}           # path -> bool
        self.rows_shown = 0
        self.capped = False
        self.total_matches = 0
        self.per_file = {}
        self.start_time = 0
        self.results_plain = []
        self.cur_pattern = ""
        self.cur_icase = False
        self.cur_regex = False
        self.cur_file_hits = 0
        self.cur_header_path = None

        self.font_notes = load_bundled_fonts()
        self._build_fonts()
        self._build_style()
        self._build_ui()

    # -- fonts / theme ----------------------------------------------------
    def _build_fonts(self):
        avail = {f.lower() for f in tkfont.families(self.root)}
        self.ui_family = pick_family(UI_STACK, avail)
        self.mono_family = pick_family(MONO_STACK, avail)
        self.f_body = tkfont.Font(family=self.ui_family, size=10)
        self.f_med = tkfont.Font(family=self.ui_family, size=10, weight="bold")
        self.f_title = tkfont.Font(family=self.ui_family, size=17, weight="bold")
        self.f_small = tkfont.Font(family=self.ui_family, size=9)
        self.f_mono = tkfont.Font(family=self.mono_family, size=10)

    def _build_style(self):
        s = ttk.Style()
        try:
            s.theme_use("clam")      # the theme that honours custom colours
        except tk.TclError:
            pass
        s.configure(".", background=C["bg"], foreground=C["on_bg"],
                    fieldbackground=C["surface"], borderwidth=0,
                    font=self.f_body)
        s.configure("TFrame", background=C["bg"])
        s.configure("TLabel", background=C["bg"], foreground=C["on_bg"])
        s.configure("Muted.TLabel", background=C["bg"], foreground=C["muted"],
                    font=self.f_small)
        s.configure("Title.TLabel", background=C["bg"], foreground=C["primary"],
                    font=self.f_title)
        s.configure("Head.TLabel", background=C["bg"], foreground=C["secondary"],
                    font=self.f_med)

        s.configure("TButton", background=C["surface2"], foreground=C["on_bg"],
                    borderwidth=0, focusthickness=0, padding=(13, 7),
                    font=self.f_med)
        s.map("TButton",
              background=[("active", C["hover"]), ("disabled", C["surface"])],
              foreground=[("disabled", C["disabled"])])
        s.configure("Accent.TButton", background=C["primary"],
                    foreground=C["on_primary"], padding=(18, 7))
        s.map("Accent.TButton",
              background=[("active", C["primary_d"]),
                          ("disabled", C["surface2"])],
              foreground=[("disabled", C["disabled"])])
        s.configure("Small.TButton", padding=(9, 4), font=self.f_small)

        s.configure("TCheckbutton", background=C["bg"], foreground=C["on_bg"],
                    focuscolor=C["bg"])
        s.map("TCheckbutton", background=[("active", C["bg"])],
              indicatorcolor=[("selected", C["primary"]),
                              ("!selected", C["surface2"])])
        s.configure("TSpinbox", fieldbackground=C["surface"],
                    background=C["surface2"], foreground=C["on_bg"],
                    arrowcolor=C["on_bg"], borderwidth=0)
        s.configure("TProgressbar", background=C["primary"],
                    troughcolor=C["surface"], borderwidth=0, thickness=4)
        s.configure("Vertical.TScrollbar", background=C["surface2"],
                    troughcolor=C["bg"], borderwidth=0, arrowcolor=C["muted"])
        s.configure("Horizontal.TScrollbar", background=C["surface2"],
                    troughcolor=C["bg"], borderwidth=0, arrowcolor=C["muted"])
        s.map("Vertical.TScrollbar", background=[("active", C["hover"])])

        # file list
        s.configure("Treeview", background=C["surface"],
                    fieldbackground=C["surface"], foreground=C["on_bg"],
                    borderwidth=0, rowheight=24, font=self.f_body)
        s.map("Treeview", background=[("selected", C["primary_d"])],
              foreground=[("selected", "#FFFFFF")])
        s.configure("Treeview.Heading", background=C["surface2"],
                    foreground=C["muted"], borderwidth=0, font=self.f_small,
                    padding=(6, 5))
        s.map("Treeview.Heading", background=[("active", C["hover"])])
        s.configure("TPanedwindow", background=C["bg"])
        s.configure("Sash", sashthickness=8, gripcount=0)

    def _entry(self, parent, textvar=None):
        return tk.Entry(parent, textvariable=textvar, bg=C["surface"],
                        fg=C["on_bg"], insertbackground=C["primary"],
                        relief="flat", font=self.f_body, highlightthickness=1,
                        highlightbackground=C["border"],
                        highlightcolor=C["primary"])

    # -- layout -----------------------------------------------------------
    def _build_ui(self):
        r = self.root
        r.title(APP_TITLE)
        r.configure(bg=C["bg"])
        r.geometry("1180x820")
        r.minsize(900, 620)

        outer = ttk.Frame(r, padding=(18, 14, 18, 10))
        outer.pack(fill="both", expand=True)
        outer.columnconfigure(0, weight=1)
        outer.rowconfigure(4, weight=1)

        # ---- header
        head = ttk.Frame(outer)
        head.grid(row=0, column=0, sticky="ew", pady=(0, 12))
        ttk.Label(head, text="DB Search", style="Title.TLabel").pack(side="left")
        ttk.Label(head, style="Muted.TLabel",
                  text="   find database files, then search them all"
                  ).pack(side="left", padx=(10, 0), pady=(9, 0))

        # ---- location row
        frow = ttk.Frame(outer)
        frow.grid(row=1, column=0, sticky="ew", pady=(0, 8))
        frow.columnconfigure(1, weight=1)
        ttk.Label(frow, text="Look in").grid(row=0, column=0, padx=(0, 10))
        self.var_root = tk.StringVar()
        e = self._entry(frow, self.var_root)
        e.grid(row=0, column=1, sticky="ew", ipady=5)
        ttk.Button(frow, text="Folder…", command=self.on_browse_folder)\
            .grid(row=0, column=2, padx=(8, 0))
        ttk.Button(frow, text="File…", command=self.on_browse_file)\
            .grid(row=0, column=3, padx=(6, 0))
        self.btn_scan = ttk.Button(frow, text="Find database files",
                                   command=self.on_scan)
        self.btn_scan.grid(row=0, column=4, padx=(10, 0))

        # ---- pattern row
        prow = ttk.Frame(outer)
        prow.grid(row=2, column=0, sticky="ew", pady=(0, 8))
        prow.columnconfigure(1, weight=1)
        ttk.Label(prow, text="Find").grid(row=0, column=0, padx=(0, 10))
        self.var_pat = tk.StringVar()
        self.e_pat = self._entry(prow, self.var_pat)
        self.e_pat.grid(row=0, column=1, sticky="ew", ipady=5)
        self.btn_search = ttk.Button(prow, text="Search", style="Accent.TButton",
                                     command=self.on_search)
        self.btn_search.grid(row=0, column=2, padx=(8, 0))
        self.btn_cancel = ttk.Button(prow, text="Cancel", command=self.on_cancel,
                                     state="disabled")
        self.btn_cancel.grid(row=0, column=3, padx=(8, 0))

        # ---- options row
        orow = ttk.Frame(outer)
        orow.grid(row=3, column=0, sticky="ew", pady=(0, 10))
        self.var_icase = tk.BooleanVar(value=True)
        self.var_regex = tk.BooleanVar(value=False)
        self.var_recurse = tk.BooleanVar(value=True)
        self.var_ctx = tk.IntVar(value=0)
        self.var_max = tk.IntVar(value=0)
        ttk.Checkbutton(orow, text="Ignore case", variable=self.var_icase)\
            .pack(side="left", padx=(0, 14))
        ttk.Checkbutton(orow, text="Regex", variable=self.var_regex)\
            .pack(side="left", padx=(0, 14))
        ttk.Checkbutton(orow, text="Include subfolders",
                        variable=self.var_recurse).pack(side="left", padx=(0, 14))
        ttk.Label(orow, text="Context").pack(side="left")
        ttk.Spinbox(orow, from_=0, to=20, width=4, textvariable=self.var_ctx)\
            .pack(side="left", padx=(6, 14))
        ttk.Label(orow, text="Stop after").pack(side="left")
        ttk.Spinbox(orow, from_=0, to=10000000, width=8,
                    textvariable=self.var_max).pack(side="left", padx=(6, 4))
        ttk.Label(orow, text="(0 = all)", style="Muted.TLabel")\
            .pack(side="left", padx=(0, 14))
        self.btn_export = ttk.Button(orow, text="Export results…",
                                     command=self.on_export, state="disabled")
        self.btn_export.pack(side="right")
        ttk.Button(orow, text="Clear results", command=self.on_clear)\
            .pack(side="right", padx=(0, 8))

        # ---- split: file list over results
        pane = ttk.Panedwindow(outer, orient="vertical")
        pane.grid(row=4, column=0, sticky="nsew")

        # file list panel
        top = ttk.Frame(pane)
        top.columnconfigure(0, weight=1)
        top.rowconfigure(1, weight=1)
        bar = ttk.Frame(top)
        bar.grid(row=0, column=0, sticky="ew", pady=(0, 6))
        self.var_files_hdr = tk.StringVar(value="Database files  —  none yet")
        ttk.Label(bar, textvariable=self.var_files_hdr, style="Head.TLabel")\
            .pack(side="left")
        ttk.Button(bar, text="None", style="Small.TButton",
                   command=lambda: self.set_all_checked(False)).pack(side="right")
        ttk.Button(bar, text="All", style="Small.TButton",
                   command=lambda: self.set_all_checked(True))\
            .pack(side="right", padx=(0, 6))

        wrap = tk.Frame(top, bg=C["border"], highlightthickness=0)
        wrap.grid(row=1, column=0, sticky="nsew")
        wrap.rowconfigure(0, weight=1)
        wrap.columnconfigure(0, weight=1)
        cols = ("size", "kind", "path")
        self.tree = ttk.Treeview(wrap, columns=cols, show="tree headings",
                                 selectmode="browse")
        self.tree.heading("#0", text="")
        self.tree.heading("size", text="SIZE")
        self.tree.heading("kind", text="TYPE")
        self.tree.heading("path", text="PATH")
        self.tree.column("#0", width=42, stretch=False, anchor="center")
        self.tree.column("size", width=100, stretch=False, anchor="e")
        self.tree.column("kind", width=110, stretch=False, anchor="w")
        self.tree.column("path", width=700, anchor="w")
        self.tree.grid(row=0, column=0, sticky="nsew")
        tvs = ttk.Scrollbar(wrap, orient="vertical", command=self.tree.yview)
        tvs.grid(row=0, column=1, sticky="ns")
        self.tree.configure(yscrollcommand=tvs.set)
        self.tree.tag_configure("odd", background=C["row_alt"])
        self.tree.tag_configure("skip", foreground=C["disabled"])
        self.tree.tag_configure("hit", foreground=C["secondary"])
        self.tree.bind("<Button-1>", self.on_tree_click)
        self.tree.bind("<space>", self.on_tree_space)
        pane.add(top, weight=1)

        # results panel
        bot = ttk.Frame(pane)
        bot.columnconfigure(0, weight=1)
        bot.rowconfigure(1, weight=1)
        ttk.Label(bot, text="Results", style="Head.TLabel")\
            .grid(row=0, column=0, sticky="w", pady=(8, 6))
        res = tk.Frame(bot, bg=C["surface2"], highlightthickness=1,
                       highlightbackground=C["border"])
        res.grid(row=1, column=0, sticky="nsew")
        res.rowconfigure(0, weight=1)
        res.columnconfigure(0, weight=1)
        self.txt = tk.Text(res, bg=C["surface2"], fg=C["on_bg"],
                           insertbackground=C["primary"], relief="flat",
                           font=self.f_mono, wrap="none", padx=12, pady=10,
                           selectbackground=C["primary_d"],
                           selectforeground="#FFFFFF")
        self.txt.grid(row=0, column=0, sticky="nsew")
        vs = ttk.Scrollbar(res, orient="vertical", command=self.txt.yview)
        vs.grid(row=0, column=1, sticky="ns")
        hs = ttk.Scrollbar(res, orient="horizontal", command=self.txt.xview)
        hs.grid(row=1, column=0, sticky="ew")
        self.txt.configure(yscrollcommand=vs.set, xscrollcommand=hs.set)
        self.txt.tag_configure("ln", foreground=C["muted"])
        self.txt.tag_configure("off", foreground=C["disabled"])
        self.txt.tag_configure("ctx", foreground=C["muted"])
        self.txt.tag_configure("hit", background=C["hit_bg"],
                               foreground=C["secondary"])
        self.txt.tag_configure("info", foreground=C["secondary"])
        self.txt.tag_configure("warn", foreground=C["error"])
        self.txt.tag_configure("file", foreground=C["primary"],
                               font=self.f_mono, spacing1=8)
        self.txt.configure(state="disabled")
        pane.add(bot, weight=2)

        # ---- status bar
        srow = ttk.Frame(outer)
        srow.grid(row=5, column=0, sticky="ew", pady=(10, 0))
        srow.columnconfigure(1, weight=1)
        self.var_status = tk.StringVar(value="Ready.")
        ttk.Label(srow, textvariable=self.var_status, style="Muted.TLabel")\
            .grid(row=0, column=0, sticky="w")
        self.prog = ttk.Progressbar(srow, mode="determinate", maximum=1000)
        self.prog.grid(row=0, column=1, sticky="ew", padx=14)
        self.var_count = tk.StringVar(value="")
        ttk.Label(srow, textvariable=self.var_count, style="Muted.TLabel")\
            .grid(row=0, column=2, sticky="e")

        r.bind("<Return>", lambda e: self.on_search())
        r.bind("<Escape>", lambda e: self.on_cancel())

        self._write(f"UI font: {self.ui_family}  ·  results: {self.mono_family}\n",
                    "ctx")
        for n in self.font_notes:
            self._write(f"{n}\n", "ctx")
        self._write("\nPick a folder (or a single file), press "
                    "\u201cFind database files\u201d, then search.\n", "ctx")

    # -- small helpers ----------------------------------------------------
    def _write(self, text, tag="ctx"):
        self.txt.configure(state="normal")
        self.txt.insert("end", text, tag)
        self.txt.configure(state="disabled")

    def busy(self, on, cancellable=True):
        st = "disabled" if on else "normal"
        self.btn_search.configure(state=st)
        self.btn_scan.configure(state=st)
        self.btn_cancel.configure(state="normal" if (on and cancellable)
                                  else "disabled")

    # -- browsing / discovery --------------------------------------------
    def on_browse_folder(self):
        d = filedialog.askdirectory(title="Select folder or NAS share")
        if d:
            self.var_root.set(d)
            self.on_scan()

    def on_browse_file(self):
        f = filedialog.askopenfilename(
            title="Select a database file",
            filetypes=[("Database / dump files",
                        "*.sql *.db *.sqlite *.sqlite3 *.csv *.tsv *.dump *.bak"),
                       ("All files", "*.*")])
        if f:
            self.var_root.set(f)
            self.on_scan()

    def on_scan(self):
        if self.worker and self.worker.is_alive():
            return
        root = self.var_root.get().strip().strip('"')
        if not root:
            messagebox.showwarning(APP_TITLE, "Choose a folder or file first.")
            return
        if not os.path.exists(root):
            messagebox.showerror(APP_TITLE, f"Path not found:\n{root}")
            return

        self.tree.delete(*self.tree.get_children())
        self.files, self.checked = [], {}
        self.var_files_hdr.set("Database files  —  scanning…")
        self.var_status.set("Scanning for database files…")
        self.prog.configure(mode="indeterminate")
        self.prog.start(12)
        self.busy(True)
        self.mode = "scan"
        self.cancel_evt = threading.Event()
        self.q = queue.Queue()
        self.worker = threading.Thread(target=self._scan_worker, daemon=True,
                                       args=(root, self.var_recurse.get()))
        self.worker.start()
        self.root.after(BATCH_MS, self._drain)

    def _scan_worker(self, root, recursive):
        batch = []
        try:
            for info in find_db_files(root, recursive=recursive,
                                      include_compressed=True,
                                      cancel=self.cancel_evt,
                                      on_scan=None):
                batch.append({"type": "found", "info": info})
                if len(batch) >= 50:
                    self.q.put(batch)
                    batch = []
        except Exception:
            batch.append({"type": "error", "msg": traceback.format_exc(limit=3)})
        batch.append({"type": "scan_done"})
        self.q.put(batch)

    def _add_file_row(self, info):
        self.files.append(info)
        self.checked[info["path"]] = info["searchable"]
        tags = []
        if len(self.files) % 2 == 0:
            tags.append("odd")
        if not info["searchable"]:
            tags.append("skip")
        kind = info["kind"] if info["searchable"] else info["kind"] + " (zip)"
        self.tree.insert(
            "", "end", iid=info["path"],
            text=CHECK_ON if info["searchable"] else CHECK_OFF,
            values=(human(info["size"]), kind, info["path"]), tags=tuple(tags))

    def _scan_finished(self):
        self.prog.stop()
        self.prog.configure(mode="determinate")
        self.prog["value"] = 0
        n = len(self.files)
        sz = sum(f["size"] for f in self.files if self.checked.get(f["path"]))
        comp = sum(1 for f in self.files if not f["searchable"])
        self.var_files_hdr.set(
            f"Database files  —  {n} found, {human(sz)} selected"
            + (f", {comp} compressed (skipped)" if comp else ""))
        self.var_status.set(f"Found {n} database file(s)." if n
                            else "No database files found here.")
        self.busy(False)
        if not n:
            self._write("\nNo database files found. Try another folder, or "
                        "turn on \u201cInclude subfolders\u201d.\n", "warn")

    # -- file list checkboxes --------------------------------------------
    def on_tree_click(self, event):
        if self.tree.identify_region(event.x, event.y) != "tree":
            return
        iid = self.tree.identify_row(event.y)
        if iid:
            self.toggle(iid)
            return "break"

    def on_tree_space(self, event):
        for iid in self.tree.selection():
            self.toggle(iid)
        return "break"

    def toggle(self, iid):
        new = not self.checked.get(iid, False)
        self.checked[iid] = new
        self.tree.item(iid, text=CHECK_ON if new else CHECK_OFF)
        self._refresh_selection_label()

    def set_all_checked(self, value):
        for f in self.files:
            # never auto-tick a compressed file; it cannot be searched
            v = value and f["searchable"]
            self.checked[f["path"]] = v
            self.tree.item(f["path"], text=CHECK_ON if v else CHECK_OFF)
        self._refresh_selection_label()

    def _refresh_selection_label(self):
        sel = [f for f in self.files if self.checked.get(f["path"])]
        sz = sum(f["size"] for f in sel)
        self.var_files_hdr.set(
            f"Database files  —  {len(self.files)} found, "
            f"{len(sel)} selected ({human(sz)})")

    # -- results ----------------------------------------------------------
    def on_clear(self):
        self.txt.configure(state="normal")
        self.txt.delete("1.0", "end")
        self.txt.configure(state="disabled")
        self.results_plain = []
        self.rows_shown = 0
        self.capped = False
        self.per_file = {}
        self.cur_header_path = None
        self.btn_export.configure(state="disabled")
        self.var_count.set("")
        self.prog["value"] = 0
        self.var_status.set("Ready.")
        for f in self.files:
            tags = tuple(t for t in self.tree.item(f["path"], "tags")
                         if t != "hit")
            self.tree.item(f["path"], tags=tags)

    def on_export(self):
        if not self.results_plain:
            return
        path = filedialog.asksaveasfilename(
            title="Save results", defaultextension=".txt",
            initialfile="dbsearch-results.txt",
            filetypes=[("Text file", "*.txt"), ("All files", "*.*")])
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(f"Searched : {self.var_root.get()}\n")
                fh.write(f"Pattern  : {self.cur_pattern!r}\n")
                fh.write("=" * 78 + "\n")
                fh.write("\n".join(self.results_plain))
                fh.write("\n" + "=" * 78 + "\n")
                for p, n in sorted(self.per_file.items(), key=lambda kv: -kv[1]):
                    if n:
                        fh.write(f"{n:>8,}  {p}\n")
                fh.write(f"TOTAL MATCHES: {self.total_matches:,}\n")
            self.var_status.set(f"Saved to {path}")
        except OSError as e:
            messagebox.showerror(APP_TITLE, f"Could not save:\n{e}")

    # -- search lifecycle -------------------------------------------------
    def on_search(self):
        if self.worker and self.worker.is_alive():
            return
        pattern = self.var_pat.get()
        if not pattern:
            messagebox.showwarning(APP_TITLE, "Enter text to search for.")
            return
        if self.var_regex.get():
            try:
                re.compile(pattern)
            except re.error as e:
                messagebox.showerror(APP_TITLE,
                                     f"Invalid regular expression:\n{e}")
                return
        if not self.files:
            # convenience: let Search imply a scan first
            root = self.var_root.get().strip().strip('"')
            if not root:
                messagebox.showwarning(APP_TITLE,
                                       "Choose a folder or file first.")
                return
            messagebox.showinfo(APP_TITLE,
                                "Press \u201cFind database files\u201d first "
                                "to see what will be searched.")
            return

        targets = [f for f in self.files if self.checked.get(f["path"])]
        if not targets:
            messagebox.showwarning(APP_TITLE, "No files are ticked.")
            return

        self.on_clear()
        self.cur_pattern = pattern
        self.cur_icase = self.var_icase.get()
        self.cur_regex = self.var_regex.get()
        total_bytes = sum(f["size"] for f in targets)
        self._write(
            f"Searching {len(targets)} file(s), {human(total_bytes)} "
            f"for {pattern!r}"
            f"{'  [regex]' if self.cur_regex else ''}"
            f"{'  [ignore-case]' if self.cur_icase else ''}\n", "info")

        self.busy(True)
        self.prog["value"] = 0
        self.total_matches = 0
        self.start_time = time.time()
        self.var_status.set("Scanning…")
        self.mode = "search"
        self.cancel_evt = threading.Event()
        self.q = queue.Queue()
        self.worker = threading.Thread(
            target=self._search_worker, daemon=True,
            args=(targets, pattern, self.cur_icase, self.cur_regex,
                  max(0, self.var_ctx.get()), max(0, self.var_max.get()),
                  total_bytes))
        self.worker.start()
        self.root.after(BATCH_MS, self._drain)

    def _search_worker(self, targets, pattern, icase, regex, ctx, maxtotal,
                       total_bytes):
        batch = []
        rows_sent = 0
        try:
            tick = max(total_bytes // 400, 1 << 20) if total_bytes else 1 << 20
            for ev in search_many(targets, pattern, ignore_case=icase,
                                  regex=regex, context=ctx,
                                  max_total_matches=maxtotal,
                                  cancel=self.cancel_evt,
                                  progress_every=tick):
                t = ev["type"]
                if t in ("match", "context"):
                    # Past the display cap there is nothing to render, so do
                    # not decode or queue the row at all. Totals still come
                    # from the authoritative file_done/all_done events, so a
                    # 400k-match scan stays fast and the UI stays responsive.
                    if rows_sent >= DISPLAY_CAP:
                        if rows_sent == DISPLAY_CAP:
                            rows_sent += 1
                            batch.append({"type": "capped"})
                        continue
                    rows_sent += 1
                    ev = dict(ev, text=decode(ev["text"], 2000, sanitize=True))
                batch.append(ev)
                if len(batch) >= 300 or t in ("progress", "all_done",
                                              "file_start", "file_done"):
                    self.q.put(batch)
                    batch = []
        except Exception:
            batch.append({"type": "error", "msg": traceback.format_exc(limit=3)})
        if batch:
            self.q.put(batch)

    def _drain(self):
        finished = False
        processed = 0
        try:
            while processed < MAX_EVENTS_PER_TICK:
                batch = self.q.get_nowait()
                processed += len(batch)
                for ev in batch:
                    t = ev["type"]
                    if t == "found":
                        self._add_file_row(ev["info"])
                    elif t == "scan_done":
                        self._scan_finished()
                        finished = True
                    elif t == "file_start":
                        self.cur_file_hits = 0
                        self.cur_header_path = None
                        self.var_status.set(
                            f"[{ev['index']}/{ev['count']}] "
                            f"{os.path.basename(ev['path'])}")
                    elif t in ("match", "context"):
                        self._ensure_header(ev.get("path"))
                        if t == "match":
                            self.cur_file_hits += 1
                        self._add_row(ev, t == "match")
                    elif t == "gap":
                        self._write("  \u22ef\n", "off")
                    elif t == "progress":
                        self._progress(ev)
                    elif t == "file_done":
                        self.per_file[ev["path"]] = ev["matches"]
                        if ev["matches"] and ev["path"] in self.checked:
                            tags = tuple(self.tree.item(ev["path"], "tags")) \
                                   + ("hit",)
                            self.tree.item(ev["path"], tags=tags)
                        if ev["error"]:
                            self._write(f"  ! {ev['path']}: {ev['error']}\n",
                                        "warn")
                    elif t == "error":
                        self._write(f"\nError:\n{ev['msg']}\n", "warn")
                        self.var_status.set("Failed.")
                        self.busy(False)
                        finished = True
                    elif t == "capped":
                        self._show_cap_notice()
                    elif t == "all_done":
                        self._finish(ev)
                        finished = True
        except queue.Empty:
            pass
        if not finished:
            self.root.after(BATCH_MS, self._drain)

    def _ensure_header(self, path):
        """Write the per-file heading once, on whichever row comes first --
        a leading context line can precede the first match."""
        if path and path != self.cur_header_path:
            self.cur_header_path = path
            self._write(f"\n\u25b8 {path}\n", "file")
            self.results_plain.append(f"\n--- {path}")

    def _show_cap_notice(self):
        if not self.capped:
            self.capped = True
            self._write(
                f"\n[display limit reached: showing the first "
                f"{DISPLAY_CAP:,} rows. The scan continues and the totals "
                f"below are complete \u2014 use Export for the full list.]\n",
                "warn")

    def _add_row(self, ev, is_match):
        if self.rows_shown >= DISPLAY_CAP:
            self._show_cap_notice()
            return
        self.rows_shown += 1

        marker = ("~" if ev.get("fragment") else ":") if is_match else "-"
        prefix = f"{ev['lineno']}{marker} "
        offs = f"(byte {ev['offset']}) "
        body = ev["text"]

        self.txt.configure(state="normal")
        self.txt.insert("end", prefix, "ln")
        self.txt.insert("end", offs, "off")
        start = self.txt.index("end-1c")
        self.txt.insert("end", body + "\n", () if is_match else ("ctx",))
        if is_match:
            for a, b in highlight_spans(body, self.cur_pattern,
                                        self.cur_icase, self.cur_regex):
                self.txt.tag_add("hit", f"{start}+{a}c", f"{start}+{b}c")
        self.txt.configure(state="disabled")
        self.results_plain.append(f"  {prefix}{offs}{body}")

    def _progress(self, ev):
        total = ev.get("total") or 0
        if total:
            frac = min(ev["scanned"] / total, 1.0)
            self.prog["value"] = frac * 1000
            elapsed = max(time.time() - self.start_time, 1e-6)
            rate = ev["scanned"] / elapsed
            eta = (total - ev["scanned"]) / rate if rate > 0 else 0
            self.var_status.set(f"Scanning… {frac*100:.0f}%  ·  "
                                f"{human(rate)}/s  ·  ETA {eta:.0f}s")
        self.var_count.set(f"{ev['matches']:,} matches")

    def _finish(self, ev):
        self.prog["value"] = 1000
        n = ev["matches"]
        self.total_matches = n
        elapsed = time.time() - self.start_time
        if ev["cancelled"]:
            msg = f"Cancelled · {n:,} matches so far"
            tag = "warn"
        elif n:
            msg = (f"Done · {n:,} match{'' if n == 1 else 'es'} in "
                   f"{ev['files_with_hits']} of {ev['files']} file(s) · "
                   f"{human(ev['scanned'])} in {elapsed:.1f}s")
            tag = "info"
        else:
            msg = (f"No matches in {ev['files']} file(s) · "
                   f"{human(ev['scanned'])} in {elapsed:.1f}s")
            tag = "warn"
        self._write(f"\n{'=' * 60}\n{msg}\n", tag)
        for p, c in sorted(self.per_file.items(), key=lambda kv: -kv[1]):
            if c:
                self._write(f"  {c:>8,}  {p}\n", "ctx")
        if ev.get("errors"):
            self._write(f"  {len(ev['errors'])} file(s) could not be read\n",
                        "warn")
        self.var_status.set(msg)
        self.var_count.set(f"{n:,} matches")
        self.busy(False)
        if self.results_plain:
            self.btn_export.configure(state="normal")

    def on_cancel(self):
        if self.cancel_evt and self.worker and self.worker.is_alive():
            self.cancel_evt.set()
            self.var_status.set("Cancelling…")


def main():
    root = tk.Tk()
    try:
        root.call("tk", "scaling", 1.3)
    except tk.TclError:
        pass
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
