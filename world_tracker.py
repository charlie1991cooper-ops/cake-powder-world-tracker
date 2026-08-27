import html
import re
import threading
import time
import urllib.request
from dataclasses import dataclass
from html.parser import HTMLParser
import tkinter as tk
from tkinter import Tk, StringVar, BooleanVar, IntVar, messagebox
from tkinter import ttk

SOURCE_URL = "https://oldschool.runescape.com/slu"
APP_NAME = "Cake Powder / Faithful Few – OSRS World Tracker"


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
    """Small dependency-free parser for the RuneScape world-list table."""
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
        headers={"User-Agent": "CakePowder-OSRS-World-Tracker/1.0"},
    )
    with urllib.request.urlopen(req, timeout=12) as response:
        raw = response.read().decode("utf-8", errors="replace")

    parser = TableParser()
    parser.feed(raw)

    worlds = []
    for row in parser.rows:
        if len(row) < 5:
            continue
        world_match = re.search(r"Old School\s+(\d+)", row[0], re.I)
        players_match = re.search(r"([\d,]+)\s+players?", row[1], re.I)
        if not world_match or not players_match:
            continue
        worlds.append(
            World(
                world=int(world_match.group(1)),
                players=int(players_match.group(1).replace(",", "")),
                location=row[2],
                membership=row[3],
                activity=row[4],
            )
        )

    if not worlds:
        raise RuntimeError("No OSRS worlds were found in the response.")
    return {w.world: w for w in worlds}


def detect_hops(previous, current, minimum_group, minimum_confidence):
    """Estimate one-to-one world transfers from a 10-second snapshot delta.

    This is deliberately presented as a likelihood estimate, not proof that the
    same players moved. The strongest signal is matching loss/gain magnitude.
    """
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
            # Size score favours larger groups without making it dominate.
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
        self.interval = IntVar(value=10)
        self.minimum_group = IntVar(value=10)
        self.minimum_confidence = IntVar(value=75)
        self.only_likely = BooleanVar(value=True)
        self.notifications = BooleanVar(value=True)
        self.keep_history = BooleanVar(value=True)
        self.status = StringVar(value="Starting…")
        self.updated = StringVar(value="—")

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
        style.configure("TButton", background="#151d28", foreground="#eaf0f7", borderwidth=0, padding=(10, 7))
        style.map("TButton", background=[("active", "#252f3d")])
        style.configure("Accent.TButton", background="#713bd4", foreground="#ffffff", padding=(12, 8))
        style.map("Accent.TButton", background=[("active", "#8b54ef")])
        style.configure("TCheckbutton", background="#0d131c", foreground="#d9e1ec")
        style.configure("TCombobox", fieldbackground="#101822", background="#101822", foreground="#ffffff")
        style.configure("Treeview", background="#0b1119", fieldbackground="#0b1119", foreground="#d9e1ec", rowheight=42, borderwidth=0)
        style.configure("Treeview.Heading", background="#111923", foreground="#aeb9c8", font=("Segoe UI", 9, "bold"))
        style.map("Treeview", background=[("selected", "#2b1d4d")])

    def panel(self, parent):
        return ttk.Frame(parent, style="Panel.TFrame", padding=12)

    def _build_ui(self):
        header = ttk.Frame(self.root, padding=(18, 12))
        header.pack(fill="x")
        ttk.Label(header, text="Cake Powder", style="Title.TLabel").pack(side="left")
        ttk.Label(header, text="  /  FAITHFUL FEW", style="Brand.TLabel").pack(side="left", padx=8)
        ttk.Label(header, textvariable=self.status, background="#080c12", foreground="#6fe58a").pack(side="right")

        tabs = ttk.Frame(self.root, padding=(18, 0, 18, 10))
        tabs.pack(fill="x")
        self.world_btn = ttk.Button(tabs, text="◉  Worlds", command=lambda: self.set_mode("worlds"))
        self.world_btn.pack(side="left")
        self.hop_btn = ttk.Button(tabs, text="◉  Group Hops", style="Accent.TButton", command=lambda: self.set_mode("hops"))
        self.hop_btn.pack(side="left", padx=6)
        ttk.Button(tabs, text="Refresh Now", command=self.refresh_async).pack(side="right")

        self.body = ttk.Frame(self.root, padding=(18, 0, 18, 18))
        self.body.pack(fill="both", expand=True)
        self._build_hop_view()

    def _clear_body(self):
        for child in self.body.winfo_children():
            child.destroy()

    def _build_hop_view(self):
        self._clear_body()
        left = self.panel(self.body)
        left.pack(side="left", fill="y", padx=(0, 10))
        ttk.Label(left, text="DETECTION SETTINGS", style="Heading.TLabel").pack(anchor="w", pady=(0, 14))
        self._setting_row(left, "Detection window", ttk.Label(left, text="10 seconds"))
        self._setting_row(left, "Minimum group size", ttk.Spinbox(left, from_=2, to=5000, textvariable=self.minimum_group, width=8))
        self._setting_row(left, "Minimum confidence", ttk.Spinbox(left, from_=1, to=99, textvariable=self.minimum_confidence, width=8))
        ttk.Checkbutton(left, text="Only show likely hops", variable=self.only_likely, command=self.render_hops).pack(anchor="w", pady=8)
        ttk.Checkbutton(left, text="Play notification", variable=self.notifications).pack(anchor="w", pady=4)
        ttk.Checkbutton(left, text="Keep history", variable=self.keep_history).pack(anchor="w", pady=4)
        ttk.Button(left, text="Clear History", command=self.clear_history).pack(fill="x", pady=(12, 20))
        ttk.Label(left, text="CONFIDENCE GUIDE", style="Heading.TLabel").pack(anchor="w", pady=(4, 10))
        for label, colour in [("90–100%   Very Likely", "#63e56f"), ("75–89%     Likely", "#ffb62e"), ("50–74%     Possible", "#ffb62e"), ("0–49%      Unlikely", "#ff5252")]:
            ttk.Label(left, text=label, foreground=colour).pack(anchor="w", pady=3)
        ttk.Label(left, text="\nThe score estimates whether the\npopulation drop and rise are likely\nto represent the same group.", foreground="#8995a5", justify="left").pack(anchor="w", pady=12)

        main = self.panel(self.body)
        main.pack(side="left", fill="both", expand=True)
        ttk.Label(main, text="GROUP HOP DETECTIONS", style="Heading.TLabel").pack(anchor="w", pady=(0, 10))

        self.tree = ttk.Treeview(main, columns=("from", "to", "left", "appeared", "moved", "confidence", "detected"), show="headings")
        headers = [("from", "FROM WORLD"), ("to", "TO WORLD"), ("left", "PLAYERS LEFT"), ("appeared", "PLAYERS APPEARED"), ("moved", "PLAYERS MOVED (EST.)"), ("confidence", "LIKELIHOOD"), ("detected", "DETECTED")]
        widths = {"from": 100, "to": 100, "left": 125, "appeared": 145, "moved": 155, "confidence": 140, "detected": 120}
        for key, title in headers:
            self.tree.heading(key, text=title)
            self.tree.column(key, width=widths[key], anchor="center")
        self.tree.pack(fill="both", expand=True)
        self.tree.bind("<<TreeviewSelect>>", self.show_detail)
        self.tree.tag_configure("very", foreground="#68e27a")
        self.tree.tag_configure("likely", foreground="#ffb52e")
        self.tree.tag_configure("possible", foreground="#c9a4ff")
        self.detail = ttk.Label(main, text="Select a detection to see details.", foreground="#8995a5", justify="left")
        self.detail.pack(fill="x", pady=(12, 0))

    def _setting_row(self, parent, label, widget):
        row = ttk.Frame(parent, style="Panel.TFrame")
        row.pack(fill="x", pady=4)
        ttk.Label(row, text=label).pack(side="left")
        widget.pack(side="right")

    def set_mode(self, mode):
        self.mode.set(mode)
        if mode == "hops":
            self._build_hop_view()
        else:
            self._build_world_view()

    def _build_world_view(self):
        self._clear_body()
        main = self.panel(self.body)
        main.pack(fill="both", expand=True)
        ttk.Label(main, text="WORLD POPULATIONS", style="Heading.TLabel").pack(anchor="w", pady=(0, 10))
        self.world_tree = ttk.Treeview(main, columns=("world", "players", "change", "location", "membership", "activity"), show="headings")
        for key, title, width in [("world", "WORLD", 100), ("players", "PLAYERS", 130), ("change", "CHANGE", 120), ("location", "LOCATION", 180), ("membership", "TYPE", 120), ("activity", "ACTIVITY", 400)]:
            self.world_tree.heading(key, text=title)
            self.world_tree.column(key, width=width, anchor="center")
        self.world_tree.pack(fill="both", expand=True)
        self.world_tree.tag_configure("up", foreground="#67df76")
        self.world_tree.tag_configure("down", foreground="#ff5757")
        self.render_worlds()

    def render_worlds(self):
        if not hasattr(self, "world_tree") or not self.world_tree.winfo_exists():
            return
        for item in self.world_tree.get_children():
            self.world_tree.delete(item)
        for wid, world in sorted(self.current.items()):
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
        sel = self.tree.selection()
        if not sel:
            return
        vals = self.tree.item(sel[0], "values")
        self.detail.config(text=(
            f"World {vals[0]}  →  World {vals[1]}\n"
            f"{vals[2]} players left World {vals[0]} and {vals[3]} appeared in World {vals[1]} within 10 seconds.\n"
            f"Estimated same-group likelihood: {vals[5]}  |  Estimated players moved: {vals[4]}\n"
            f"This is a statistical estimate based on population changes; it does not prove the same accounts moved."
        ))

    def clear_history(self):
        self.history.clear()
        self.render_hops()

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
            self.root.after(0, lambda: self._fetch_failed(str(exc)))
        finally:
            self.root.after(0, lambda: setattr(self, "fetching", False))

    def _fetch_failed(self, error):
        self.status.set("Update failed – retrying")
        self.updated.set("—")
        if not self.current:
            self.detail = getattr(self, "detail", None)
        # Don't spam message boxes during background retries.
        if not self.current:
            self.root.after(0, lambda: messagebox.showwarning("OSRS World Tracker", f"Could not load the OSRS world list.\n\n{error}"))

    def _apply_snapshot(self, worlds):
        self.previous = self.current or None
        self.current = worlds
        now = time.time()
        self.updated.set(time.strftime("%H:%M:%S", time.localtime(now)))
        self.status.set(f"Tracking {len(worlds)} worlds • updated {self.updated.get()}")

        if self.previous:
            hops = detect_hops(self.previous, self.current, self.minimum_group.get(), self.minimum_confidence.get())
            if hops:
                if self.keep_history.get():
                    self.history.extend(hops)
                    self.history = self.history[-500:]
                else:
                    self.history = hops

        if self.mode.get() == "hops":
            self.render_hops()
        else:
            self.render_worlds()

        self.root.after(self.interval.get() * 1000, self.refresh_async)

    def close(self):
        self.running = False
        self.root.destroy()


def main():
    root = Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
