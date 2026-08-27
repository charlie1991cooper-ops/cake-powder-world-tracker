import html
import re
import threading
import time
import urllib.request
from dataclasses import dataclass
from html.parser import HTMLParser
import tkinter as tk
from tkinter import Tk, StringVar, BooleanVar, IntVar
from tkinter import ttk

SOURCE_URL = "https://oldschool.runescape.com/slu"
APP_NAME = "Cake Powder / Faithful Few – OSRS World Tracker"
POLL_SECONDS = 10


@dataclass(frozen=True)
class World:
    world: int
    players: int
    location: str
    membership: str
    activity: str


@dataclass(frozen=True)
class Hop:
    source: int
    destination: int
    players_left: int
    players_appeared: int
    players_moved: int
    confidence: int
    timestamp: float


class TableParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.in_tr = False
        self.in_cell = False
        self.rows = []
        self.current = []
        self.buffer = []

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if tag == "tr":
            self.in_tr = True
            self.current = []
        elif tag in ("td", "th") and self.in_tr:
            self.in_cell = True
            self.buffer = []

    def handle_data(self, data):
        if self.in_cell:
            self.buffer.append(data)

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag in ("td", "th") and self.in_cell:
            text = re.sub(r"\s+", " ", html.unescape("".join(self.buffer))).strip()
            self.current.append(text)
            self.in_cell = False
            self.buffer = []
        elif tag == "tr" and self.in_tr:
            if self.current:
                self.rows.append(self.current)
            self.in_tr = False
            self.current = []


def fetch_worlds():
    req = urllib.request.Request(
        SOURCE_URL,
        headers={"User-Agent": "CakePowder-OSRS-World-Tracker/2.0"},
    )
    with urllib.request.urlopen(req, timeout=12) as response:
        raw = response.read().decode("utf-8", errors="replace")

    parser = TableParser()
    parser.feed(raw)

    worlds = {}
    for row in parser.rows:
        # The current OSRS page contains quick-select rows before the world table.
        # Only accept the exact five-column world-list shape and a numeric world
        # name plus a player-count cell (including blank/offline counts).
        if len(row) < 5:
            continue
        world_match = re.fullmatch(r"Old School\s+(\d+)", row[0], re.I)
        if not world_match:
            continue
        players_text = row[1].strip()
        if players_text:
            players_match = re.fullmatch(r"([\d,]+)\s+players?", players_text, re.I)
            if not players_match:
                continue
            players = int(players_match.group(1).replace(",", ""))
        else:
            # Offline/empty cells are still valid world rows; treat them as 0.
            players = 0
        membership = row[3].strip()
        if membership not in ("Free", "Members"):
            continue
        world = int(world_match.group(1))
        worlds[world] = World(world, players, row[2], membership, row[4])

    if not worlds:
        raise RuntimeError("No OSRS worlds were found in the response.")
    return worlds


def filtered_worlds(worlds, include_f2p):
    if include_f2p:
        return dict(worlds)
    return {wid: w for wid, w in worlds.items() if w.membership == "Members"}


def detect_hops(previous, current, minimum_group, minimum_confidence):
    drops = []
    gains = []
    for world_id in set(previous) & set(current):
        delta = current[world_id].players - previous[world_id].players
        if delta <= -minimum_group:
            drops.append((world_id, -delta))
        elif delta >= minimum_group:
            gains.append((world_id, delta))

    candidates = []
    for source, left in drops:
        for destination, appeared in gains:
            if source == destination:
                continue
            match = min(left, appeared) / max(left, appeared)
            size_score = min(1.0, min(left, appeared) / max(50, minimum_group * 8))
            confidence = round((match * 0.88 + size_score * 0.12) * 100)
            confidence = max(0, min(99, confidence))
            moved = min(left, appeared)
            candidates.append((confidence, source, destination, left, appeared, moved))

    candidates.sort(reverse=True)
    used_sources = set()
    used_destinations = set()
    results = []
    for confidence, source, destination, left, appeared, moved in candidates:
        if confidence < minimum_confidence:
            continue
        if source in used_sources or destination in used_destinations:
            continue
        used_sources.add(source)
        used_destinations.add(destination)
        results.append(Hop(source, destination, left, appeared, moved, confidence, time.time()))
    return results


class App:
    def __init__(self, root):
        self.root = root
        self.root.title(APP_NAME)
        self.root.geometry("1380x820")
        self.root.minsize(1050, 680)

        self.previous = None
        self.current = {}
        self.history = []
        self.running = True
        self.fetching = False

        self.mode = StringVar(value="hops")
        self.minimum_group = IntVar(value=10)
        self.minimum_confidence = IntVar(value=75)
        self.only_likely = BooleanVar(value=True)
        self.notifications = BooleanVar(value=True)
        self.keep_history = BooleanVar(value=True)
        self.include_f2p = BooleanVar(value=False)
        self.status = StringVar(value="Starting…")
        self.updated = StringVar(value="Waiting for first update")

        self._configure_style()
        self._build_ui()
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self.refresh_async()

    def _configure_style(self):
        style = ttk.Style(self.root)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure("TFrame", background="#080c12")
        style.configure("Panel.TFrame", background="#0d131c")
        style.configure("TLabel", background="#0d131c", foreground="#d9e1ec", font=("Segoe UI", 10))
        style.configure("Title.TLabel", background="#080c12", foreground="#ffffff", font=("Segoe UI", 18, "bold"))
        style.configure("Brand.TLabel", background="#080c12", foreground="#a970ff", font=("Segoe UI", 10, "bold"))
        style.configure("Heading.TLabel", background="#0d131c", foreground="#a970ff", font=("Segoe UI", 11, "bold"))
        style.configure("Subtle.TLabel", background="#0d131c", foreground="#8995a5", font=("Segoe UI", 9))
        style.configure("Status.TLabel", background="#080c12", foreground="#6fe58a", font=("Segoe UI", 9))
        style.configure("TButton", background="#151d28", foreground="#eaf0f7", borderwidth=0, padding=(12, 8))
        style.map("TButton", background=[("active", "#252f3d")])
        style.configure("Accent.TButton", background="#713bd4", foreground="#ffffff", padding=(12, 8))
        style.map("Accent.TButton", background=[("active", "#8b54ef")])
        style.configure("TCheckbutton", background="#0d131c", foreground="#d9e1ec", padding=(2, 3))
        style.map("TCheckbutton", background=[("active", "#0d131c")])
        style.configure("TSpinbox", fieldbackground="#101822", background="#101822", foreground="#ffffff", arrowcolor="#a970ff", padding=4)
        style.configure("Treeview", background="#0b1119", fieldbackground="#0b1119", foreground="#d9e1ec", rowheight=34, borderwidth=0, font=("Segoe UI", 9))
        style.configure("Treeview.Heading", background="#111923", foreground="#aeb9c8", font=("Segoe UI", 9, "bold"), padding=7)
        style.map("Treeview", background=[("selected", "#2b1d4d")], foreground=[("selected", "#ffffff")])

    def panel(self, parent, **kwargs):
        return ttk.Frame(parent, style="Panel.TFrame", padding=kwargs.get("padding", 14))

    def _build_ui(self):
        header = ttk.Frame(self.root, padding=(18, 12))
        header.pack(fill="x")
        ttk.Label(header, text="Cake Powder", style="Title.TLabel").pack(side="left")
        ttk.Label(header, text=" /  FAITHFUL FEW", style="Brand.TLabel").pack(side="left", padx=8)
        ttk.Label(header, textvariable=self.status, style="Status.TLabel").pack(side="right")

        toolbar = ttk.Frame(self.root, padding=(18, 0, 18, 12))
        toolbar.pack(fill="x")
        self.world_btn = ttk.Button(toolbar, text="Worlds", command=lambda: self.set_mode("worlds"))
        self.world_btn.pack(side="left")
        self.hop_btn = ttk.Button(toolbar, text="Group Hops", command=lambda: self.set_mode("hops"))
        self.hop_btn.pack(side="left", padx=(6, 16))
        ttk.Checkbutton(toolbar, text="Include F2P worlds", variable=self.include_f2p, command=self.toggle_f2p).pack(side="left")
        ttk.Button(toolbar, text="Refresh Now", command=self.refresh_async).pack(side="right")
        ttk.Label(toolbar, text="Live polling: 10s", foreground="#8995a5", background="#080c12").pack(side="right", padx=(0, 12))

        self.body = ttk.Frame(self.root, padding=(18, 0, 18, 18))
        self.body.pack(fill="both", expand=True)
        self._build_hop_view()
        self._update_mode_buttons()

    def _clear_body(self):
        for child in self.body.winfo_children():
            child.destroy()

    def _build_hop_view(self):
        self._clear_body()
        container = ttk.Frame(self.body)
        container.pack(fill="both", expand=True)
        container.columnconfigure(1, weight=1)
        container.rowconfigure(0, weight=1)

        left = self.panel(container)
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 10))
        left.columnconfigure(1, weight=1)
        ttk.Label(left, text="DETECTION SETTINGS", style="Heading.TLabel").grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 14))

        self._setting_grid(left, 1, "Detection window", ttk.Label(left, text="10 seconds", anchor="e"))
        self._setting_grid(left, 2, "Minimum group size", ttk.Spinbox(left, from_=2, to=5000, textvariable=self.minimum_group, width=7, command=self.render_hops))
        self._setting_grid(left, 3, "Minimum confidence", ttk.Spinbox(left, from_=1, to=99, textvariable=self.minimum_confidence, width=7, command=self.render_hops))

        ttk.Separator(left).grid(row=4, column=0, columnspan=2, sticky="ew", pady=12)
        ttk.Checkbutton(left, text="Only show likely hops", variable=self.only_likely, command=self.render_hops).grid(row=5, column=0, columnspan=2, sticky="w", pady=3)
        ttk.Checkbutton(left, text="Play notification", variable=self.notifications).grid(row=6, column=0, columnspan=2, sticky="w", pady=3)
        ttk.Checkbutton(left, text="Keep history", variable=self.keep_history, command=self.render_hops).grid(row=7, column=0, columnspan=2, sticky="w", pady=3)
        ttk.Button(left, text="Clear History", command=self.clear_history).grid(row=8, column=0, columnspan=2, sticky="ew", pady=(10, 18))

        ttk.Label(left, text="CONFIDENCE GUIDE", style="Heading.TLabel").grid(row=9, column=0, columnspan=2, sticky="w", pady=(0, 8))
        guide = [("90–100%", "Very Likely", "#63e56f"), ("75–89%", "Likely", "#ffb62e"), ("50–74%", "Possible", "#c9a4ff"), ("0–49%", "Unlikely", "#ff5252")]
        for i, (score, label, colour) in enumerate(guide, start=10):
            ttk.Label(left, text=score, foreground=colour).grid(row=i, column=0, sticky="w", pady=2)
            ttk.Label(left, text=label, foreground=colour).grid(row=i, column=1, sticky="w", padx=(10, 0), pady=2)

        ttk.Label(left, text="\nA likelihood estimate based on matching\npopulation drops and gains in the same\n10-second snapshot.", style="Subtle.TLabel", justify="left").grid(row=14, column=0, columnspan=2, sticky="w", pady=(10, 0))

        main = self.panel(container)
        main.grid(row=0, column=1, sticky="nsew")
        main.columnconfigure(0, weight=1)
        main.rowconfigure(1, weight=1)
        ttk.Label(main, text="GROUP HOP DETECTIONS", style="Heading.TLabel").grid(row=0, column=0, sticky="w", pady=(0, 10))

        table_frame = ttk.Frame(main)
        table_frame.grid(row=1, column=0, sticky="nsew")
        table_frame.columnconfigure(0, weight=1)
        table_frame.rowconfigure(0, weight=1)
        self.tree = ttk.Treeview(table_frame, columns=("from", "to", "left", "appeared", "moved", "confidence", "detected"), show="headings")
        headers = [("from", "FROM"), ("to", "TO"), ("left", "PLAYERS LEFT"), ("appeared", "PLAYERS APPEARED"), ("moved", "EST. MOVED"), ("confidence", "SAME GROUP"), ("detected", "TIME")]
        widths = {"from": 75, "to": 75, "left": 115, "appeared": 145, "moved": 110, "confidence": 115, "detected": 90}
        for key, title in headers:
            self.tree.heading(key, text=title)
            self.tree.column(key, width=widths[key], anchor="center", stretch=key in ("left", "appeared", "moved", "confidence"))
        self.tree.grid(row=0, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(table_frame, orient="vertical", command=self.tree.yview)
        scroll.grid(row=0, column=1, sticky="ns")
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.bind("<<TreeviewSelect>>", self.show_detail)
        self.tree.tag_configure("very", foreground="#68e27a")
        self.tree.tag_configure("likely", foreground="#ffb52e")
        self.tree.tag_configure("possible", foreground="#c9a4ff")
        self.detail = ttk.Label(main, text="Select a detection to see the exact population changes.", style="Subtle.TLabel", justify="left")
        self.detail.grid(row=2, column=0, sticky="ew", pady=(10, 0))

    def _setting_grid(self, parent, row, label, widget):
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", pady=5)
        widget.grid(row=row, column=1, sticky="e", pady=5)

    def set_mode(self, mode):
        self.mode.set(mode)
        if mode == "hops":
            self._build_hop_view()
        else:
            self._build_world_view()
        self._update_mode_buttons()

    def _update_mode_buttons(self):
        if self.mode.get() == "hops":
            self.hop_btn.configure(style="Accent.TButton")
            self.world_btn.configure(style="TButton")
        else:
            self.world_btn.configure(style="Accent.TButton")
            self.hop_btn.configure(style="TButton")

    def toggle_f2p(self):
        # Changing the population universe invalidates the previous baseline.
        # Reset it so toggling F2P cannot create a fake group hop.
        self.previous = None
        self.status.set("F2P filter changed — rebuilding baseline…")
        self.render_worlds()
        self.render_hops()
        self.refresh_async()

    def _build_world_view(self):
        self._clear_body()
        main = self.panel(self.body)
        main.pack(fill="both", expand=True)
        main.columnconfigure(0, weight=1)
        main.rowconfigure(1, weight=1)
        title = "WORLD POPULATIONS" + ("  •  F2P INCLUDED" if self.include_f2p.get() else "  •  MEMBERS ONLY")
        ttk.Label(main, text=title, style="Heading.TLabel").grid(row=0, column=0, sticky="w", pady=(0, 10))
        frame = ttk.Frame(main)
        frame.grid(row=1, column=0, sticky="nsew")
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(0, weight=1)
        self.world_tree = ttk.Treeview(frame, columns=("world", "players", "change", "location", "type", "activity"), show="headings")
        for key, title, width in [("world", "WORLD", 90), ("players", "PLAYERS", 120), ("change", "CHANGE", 110), ("location", "LOCATION", 170), ("type", "TYPE", 100), ("activity", "ACTIVITY", 400)]:
            self.world_tree.heading(key, text=title)
            self.world_tree.column(key, width=width, anchor="center", stretch=key == "activity")
        self.world_tree.grid(row=0, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(frame, orient="vertical", command=self.world_tree.yview)
        scroll.grid(row=0, column=1, sticky="ns")
        self.world_tree.configure(yscrollcommand=scroll.set)
        self.world_tree.tag_configure("up", foreground="#67df76")
        self.world_tree.tag_configure("down", foreground="#ff5757")
        self.render_worlds()

    def render_worlds(self):
        if not hasattr(self, "world_tree") or not self.world_tree.winfo_exists():
            return
        for item in self.world_tree.get_children():
            self.world_tree.delete(item)
        visible = filtered_worlds(self.current, self.include_f2p.get())
        for wid, world in sorted(visible.items()):
            old = self.previous.get(wid).players if self.previous and wid in self.previous else world.players
            delta = world.players - old
            tag = "up" if delta > 0 else "down" if delta < 0 else ""
            self.world_tree.insert("", "end", values=(wid, f"{world.players:,}", f"{delta:+,}", world.location, world.membership, world.activity or "-"), tags=(tag,))

    def render_hops(self):
        if not hasattr(self, "tree") or not self.tree.winfo_exists():
            return
        for item in self.tree.get_children():
            self.tree.delete(item)
        hops = self.history if self.keep_history.get() else self.history[-20:]
        for hop in reversed(hops):
            if self.only_likely.get() and hop.confidence < self.minimum_confidence.get():
                continue
            tag = "very" if hop.confidence >= 90 else "likely" if hop.confidence >= 75 else "possible"
            when = time.strftime("%H:%M:%S", time.localtime(hop.timestamp))
            self.tree.insert("", "end", values=(hop.source, hop.destination, f"-{hop.players_left}", f"+{hop.players_appeared}", f"~{hop.players_moved}", f"{hop.confidence}%", when), tags=(tag,))

    def show_detail(self, _event=None):
        if not hasattr(self, "tree"):
            return
        sel = self.tree.selection()
        if not sel:
            return
        vals = self.tree.item(sel[0], "values")
        self.detail.config(text=(f"World {vals[0]} → World {vals[1]}   |   {vals[2]} players left World {vals[0]}   |   "
                                f"{vals[3]} players appeared in World {vals[1]}   |   Estimated {vals[4].lstrip('~')} players moved   |   "
                                f"{vals[5]} likelihood that this is the same group."))

    def clear_history(self):
        self.history.clear()
        self.render_hops()
        if hasattr(self, "detail"):
            self.detail.config(text="Detection history cleared.")

    def refresh_async(self):
        if self.fetching:
            return
        self.fetching = True
        self.status.set("Updating worlds…")
        threading.Thread(target=self._fetch_worker, daemon=True).start()

    def _fetch_worker(self):
        try:
            worlds = fetch_worlds()
            self.root.after(0, lambda: self._apply_snapshot(worlds))
        except Exception as exc:
            self.root.after(0, lambda e=str(exc): self._fetch_failed(e))
        finally:
            self.root.after(0, self._fetch_finished)

    def _fetch_finished(self):
        self.fetching = False
        if self.running:
            self.root.after(POLL_SECONDS * 1000, self.refresh_async)

    def _apply_snapshot(self, worlds):
        current = filtered_worlds(worlds, self.include_f2p.get())
        if self.previous is not None:
            hops = detect_hops(self.previous, current, self.minimum_group.get(), self.minimum_confidence.get())
            if hops:
                self.history.extend(hops)
                if self.notifications.get():
                    try:
                        self.root.bell()
                    except tk.TclError:
                        pass
        self.current = current
        self.previous = dict(current)
        self.updated.set(time.strftime("Last update %H:%M:%S"))
        count = len(current)
        self.status.set(f"Tracking {count} worlds")
        self.render_worlds()
        self.render_hops()

    def _fetch_failed(self, error):
        self.status.set("World update failed — retrying…")
        if hasattr(self, "detail"):
            self.detail.config(text=f"Could not update the OSRS world list: {error}")

    def close(self):
        self.running = False
        self.root.destroy()


def main():
    root = Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
