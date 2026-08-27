import html
import re
import threading
import time
import urllib.request
from dataclasses import dataclass
from html.parser import HTMLParser
import tkinter as tk
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
    """Reads world rows and, importantly, the canonical slu-world-XXX id."""
    def __init__(self):
        super().__init__()
        self.in_tr = False
        self.in_cell = False
        self.current = []
        self.buffer = []
        self.current_world_id = None
        self.rows = []

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if tag == "tr":
            self.in_tr = True
            self.in_cell = False
            self.current = []
            self.buffer = []
            self.current_world_id = None
        elif tag in ("td", "th") and self.in_tr:
            self.in_cell = True
            self.buffer = []
        elif tag == "a" and self.in_tr:
            attrs = dict(attrs)
            anchor_id = attrs.get("id", "")
            match = re.fullmatch(r"slu-world-(\d+)", anchor_id, re.I)
            if match:
                self.current_world_id = int(match.group(1))

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
                self.rows.append((self.current, self.current_world_id))
            self.in_tr = False
            self.in_cell = False
            self.current = []
            self.buffer = []
            self.current_world_id = None

def fetch_worlds():
    req = urllib.request.Request(
        SOURCE_URL,
        headers={
            "User-Agent": "CakePowder-OSRS-World-Tracker/3.1",
            "Accept": "text/html,application/xhtml+xml",
        },
    )
    with urllib.request.urlopen(req, timeout=12) as response:
        raw = response.read().decode("utf-8", errors="replace")

    parser = TableParser()
    parser.feed(raw)
    worlds = {}

    for row, world_id in parser.rows:
        if len(row) < 5 or not isinstance(world_id, int):
            continue
        if not re.fullmatch(r"Old School\s+\d+", row[0], re.I):
            continue
        players_match = re.fullmatch(r"([\d,]+)\s+players?", row[1], re.I)
        if not players_match:
            continue
        membership = row[3].strip()
        if membership not in ("Members", "Free"):
            continue
        worlds[world_id] = World(
            world=world_id,
            players=int(players_match.group(1).replace(",", "")),
            location=row[2].strip(),
            membership=membership,
            activity=row[4].strip() or "-",
        )

    if not worlds:
        raise RuntimeError("No OSRS world rows were found.")
    return sorted(worlds.values(), key=lambda w: w.world)

def detect_hops(previous, current, min_group):
    drops, gains = [], []
    for wid in set(previous) & set(current):
        delta = current[wid].players - previous[wid].players
        if delta <= -min_group:
            drops.append((wid, -delta))
        elif delta >= min_group:
            gains.append((wid, delta))
    if not drops or not gains:
        return []

    total_drop = sum(x[1] for x in drops)
    total_gain = sum(x[1] for x in gains)
    candidates = []
    for source, left in drops:
        for destination, appeared in gains:
            ratio = min(left, appeared) / max(left, appeared)
            source_share = left / total_drop if total_drop else 0
            dest_share = appeared / total_gain if total_gain else 0
            score = 55 * ratio + 25 * source_share + 20 * dest_share
            candidates.append((score, source, destination, left, appeared))

    candidates.sort(reverse=True)
    used_sources, used_destinations, result = set(), set(), []
    for score, source, destination, left, appeared in candidates:
        if source in used_sources or destination in used_destinations:
            continue
        confidence = max(0, min(99, round(score)))
        if confidence < 50:
            continue
        result.append(Hop(source, destination, left, appeared, min(left, appeared), confidence, time.time()))
        used_sources.add(source)
        used_destinations.add(destination)
    return result

class App:
    def __init__(self, root):
        self.root = root
        self.root.title(APP_NAME)
        self.root.geometry("1260x760")
        self.root.minsize(1000, 650)
        self.worlds = []
        self.previous_snapshot = None
        self.history = []
        self.f2p_var = tk.BooleanVar(value=False)
        self.min_group_var = tk.IntVar(value=10)
        self.min_conf_var = tk.IntVar(value=75)
        self.notifications_var = tk.BooleanVar(value=True)
        self.keep_history_var = tk.BooleanVar(value=True)
        self.status_var = tk.StringVar(value="Starting…")
        self.view = "hops"
        self.build_ui()
        self.root.protocol("WM_DELETE_WINDOW", self.root.destroy)
        self.root.after(200, self.refresh)

    def styles(self):
        s = ttk.Style(self.root)
        try: s.theme_use("clam")
        except tk.TclError: pass
        bg, panel, panel2, fg, muted, purple = "#080d16", "#101722", "#131d2a", "#e8edf5", "#9aa8ba", "#9b5cff"
        self.colors = dict(bg=bg, panel=panel, panel2=panel2, fg=fg, muted=muted, purple=purple)
        self.root.configure(bg=bg)
        s.configure("TFrame", background=bg)
        s.configure("Panel.TFrame", background=panel)
        s.configure("TLabel", background=bg, foreground=fg, font=("Segoe UI", 10))
        s.configure("Muted.TLabel", background=bg, foreground=muted, font=("Segoe UI", 9))
        s.configure("Title.TLabel", background=bg, foreground="white", font=("Segoe UI", 20, "bold"))
        s.configure("Brand.TLabel", background=bg, foreground=purple, font=("Segoe UI", 10, "bold"))
        s.configure("Section.TLabel", background=panel, foreground=purple, font=("Segoe UI", 10, "bold"))
        s.configure("TButton", font=("Segoe UI", 9, "bold"), padding=(12, 7))
        s.configure("View.TButton", background=panel2, foreground=fg, padding=(14, 8), borderwidth=0)
        s.configure("Treeview", background="#0b111b", fieldbackground="#0b111b", foreground=fg, rowheight=32, font=("Segoe UI", 9))
        s.configure("Treeview.Heading", background=panel2, foreground="#dfe5ef", font=("Segoe UI", 9, "bold"), padding=(8, 9))
        s.map("Treeview", background=[("selected", "#43258a")], foreground=[("selected", "white")])

    def build_ui(self):
        self.styles()
        header = ttk.Frame(self.root, padding=(18, 14, 18, 10)); header.grid(row=0, column=0, sticky="ew"); header.columnconfigure(1, weight=1)
        left = ttk.Frame(header); left.grid(row=0, column=0, sticky="w")
        ttk.Label(left, text="Cake Powder", style="Title.TLabel").pack(side="left")
        ttk.Label(left, text="  /  FAITHFUL FEW", style="Brand.TLabel").pack(side="left", padx=4)
        ttk.Label(header, textvariable=self.status_var, style="Muted.TLabel").grid(row=0, column=1, sticky="e", padx=12)
        self.refresh_btn = ttk.Button(header, text="Refresh Now", command=self.refresh); self.refresh_btn.grid(row=0, column=2)

        bar = ttk.Frame(self.root, padding=(18, 0, 18, 12)); bar.grid(row=1, column=0, sticky="ew"); bar.columnconfigure(3, weight=1)
        ttk.Button(bar, text="◉  Worlds", style="View.TButton", command=lambda: self.set_view("worlds")).grid(row=0, column=0, padx=(0, 6))
        ttk.Button(bar, text="◉  Group Hops", style="View.TButton", command=lambda: self.set_view("hops")).grid(row=0, column=1, padx=(0, 18))
        ttk.Separator(bar, orient="vertical").grid(row=0, column=2, sticky="ns", padx=6)
        ttk.Checkbutton(bar, text="Include F2P worlds", variable=self.f2p_var, command=self.filter_changed).grid(row=0, column=3, sticky="w")
        ttk.Label(bar, text="10-second detection window", style="Muted.TLabel").grid(row=0, column=4, sticky="e")

        content = ttk.Frame(self.root, padding=(18, 0, 18, 18)); content.grid(row=2, column=0, sticky="nsew"); content.columnconfigure(1, weight=1); content.rowconfigure(0, weight=1)
        self.root.columnconfigure(0, weight=1); self.root.rowconfigure(2, weight=1)

        settings = ttk.Frame(content, style="Panel.TFrame", padding=16); settings.grid(row=0, column=0, sticky="nsw", padx=(0, 12)); settings.columnconfigure(1, weight=1)
        ttk.Label(settings, text="DETECTION SETTINGS", style="Section.TLabel").grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 16))
        ttk.Label(settings, text="Detection window").grid(row=1, column=0, sticky="w", pady=6); ttk.Label(settings, text="10 seconds", style="Muted.TLabel").grid(row=1, column=1, sticky="e", pady=6)
        ttk.Label(settings, text="Minimum group size").grid(row=2, column=0, sticky="w", pady=6); ttk.Spinbox(settings, from_=1, to=500, textvariable=self.min_group_var, width=8, command=self.reset_baseline).grid(row=2, column=1, sticky="e", pady=6)
        ttk.Label(settings, text="Minimum confidence").grid(row=3, column=0, sticky="w", pady=6); ttk.Spinbox(settings, from_=0, to=99, textvariable=self.min_conf_var, width=8).grid(row=3, column=1, sticky="e", pady=6)
        ttk.Separator(settings).grid(row=4, column=0, columnspan=2, sticky="ew", pady=12)
        ttk.Checkbutton(settings, text="Play notification", variable=self.notifications_var).grid(row=5, column=0, columnspan=2, sticky="w", pady=4)
        ttk.Checkbutton(settings, text="Keep history", variable=self.keep_history_var).grid(row=6, column=0, columnspan=2, sticky="w", pady=4)
        ttk.Button(settings, text="Clear Detection History", command=self.clear_history).grid(row=7, column=0, columnspan=2, sticky="ew", pady=(12, 4))
        ttk.Separator(settings).grid(row=8, column=0, columnspan=2, sticky="ew", pady=16)
        ttk.Label(settings, text="CONFIDENCE GUIDE", style="Section.TLabel").grid(row=9, column=0, columnspan=2, sticky="w", pady=(0, 10))
        for i, (pct, text, color) in enumerate((("90–99%", "Very likely", "#55d88a"), ("75–89%", "Likely", "#f0c75e"), ("50–74%", "Possible", "#f09b32"), ("0–49%", "Unlikely", "#ff5d67")), 10):
            ttk.Label(settings, text=pct, foreground=color, background=self.colors["panel"], font=("Segoe UI", 9, "bold")).grid(row=i, column=0, sticky="w", pady=3)
            ttk.Label(settings, text=text, foreground=color, background=self.colors["panel"]).grid(row=i, column=1, sticky="w", pady=3)
        ttk.Label(settings, text="The likelihood estimates whether a population drop\nand rise are consistent with the same group moving.\nIt is not proof that the same accounts moved.", foreground=self.colors["muted"], background=self.colors["panel"], justify="left", font=("Segoe UI", 9)).grid(row=14, column=0, columnspan=2, sticky="sw", pady=(22, 0))

        panel = ttk.Frame(content, style="Panel.TFrame", padding=14); panel.grid(row=0, column=1, sticky="nsew"); panel.columnconfigure(0, weight=1); panel.rowconfigure(1, weight=1)
        self.title = ttk.Label(panel, text="GROUP HOP DETECTIONS", style="Section.TLabel"); self.title.grid(row=0, column=0, sticky="w", pady=(0, 10))
        self.tree = ttk.Treeview(panel, show="headings"); self.tree.grid(row=1, column=0, sticky="nsew")
        sb = ttk.Scrollbar(panel, orient="vertical", command=self.tree.yview); sb.grid(row=1, column=1, sticky="ns"); self.tree.configure(yscrollcommand=sb.set)
        self.tree.bind("<<TreeviewSelect>>", self.select)
        self.detail = tk.StringVar(value="Waiting for a population change large enough to produce a detection.")
        ttk.Label(panel, textvariable=self.detail, style="Muted.TLabel", justify="left").grid(row=2, column=0, sticky="w", pady=(10, 0))
        self.set_view("hops")

    def set_view(self, view):
        self.view = view
        if view == "hops":
            self.title.configure(text="GROUP HOP DETECTIONS")
            cols = ("from", "to", "left", "appeared", "moved", "confidence", "detected")
            heads = ("FROM WORLD", "TO WORLD", "PLAYERS LEFT", "PLAYERS APPEARED", "PLAYERS MOVED (EST.)", "SAME-GROUP LIKELIHOOD", "DETECTED")
            widths = (105, 105, 120, 140, 145, 180, 145)
        else:
            self.title.configure(text="OSRS WORLD POPULATIONS")
            cols = ("world", "players", "location", "type", "activity")
            heads = ("WORLD", "PLAYERS", "LOCATION", "TYPE", "ACTIVITY")
            widths = (90, 120, 180, 110, 260)
        self.tree["columns"] = cols
        for c, h, w in zip(cols, heads, widths):
            self.tree.heading(c, text=h); self.tree.column(c, width=w, anchor="center" if c != "activity" else "w", stretch=True)
        self.render()

    def visible(self):
        return self.worlds if self.f2p_var.get() else [w for w in self.worlds if w.membership == "Members"]

    def filter_changed(self):
        self.reset_baseline(); self.render()

    def reset_baseline(self):
        self.previous_snapshot = None
        self.detail.set("Baseline reset. Waiting for the next world snapshot.")

    def refresh(self):
        if self.refresh_btn.instate(["disabled"]): return
        self.refresh_btn.state(["disabled"]); self.status_var.set("Updating worlds…")
        threading.Thread(target=self.worker, daemon=True).start()

    def worker(self):
        try:
            worlds = fetch_worlds(); self.root.after(0, lambda: self.apply(worlds))
        except Exception as exc:
            self.root.after(0, lambda: self.failed(str(exc)))

    def failed(self, error):
        self.refresh_btn.state(["!disabled"]); self.status_var.set("Update failed — retrying in 10 seconds")
        self.detail.set("World-list update failed: " + error)
        self.root.after(POLL_SECONDS * 1000, self.refresh)

    def apply(self, worlds):
        self.worlds = worlds
        visible = self.visible(); current = {w.world: w for w in visible}
        if self.previous_snapshot is None:
            self.previous_snapshot = current
            self.detail.set("Baseline captured. Watching for 10-second population changes…")
        else:
            for hop in detect_hops(self.previous_snapshot, current, self.min_group_var.get()):
                if hop.confidence >= self.min_conf_var.get():
                    self.history.insert(0, hop)
                    if self.notifications_var.get(): self.root.bell()
            if self.keep_history_var.get(): self.history = self.history[:250]
            else: self.history.clear()
            self.previous_snapshot = current
        self.status_var.set(f"Updated {time.strftime('%H:%M:%S')}  •  {len(visible)} worlds  •  {'F2P + Members' if self.f2p_var.get() else 'Members only'}")
        self.refresh_btn.state(["!disabled"]); self.render()
        self.root.after(POLL_SECONDS * 1000, self.refresh)

    def render(self):
        self.tree.delete(*self.tree.get_children())
        if self.view == "worlds":
            for w in self.visible():
                self.tree.insert("", "end", values=(w.world, f"{w.players:,}", w.location, w.membership, w.activity))
        else:
            for h in self.history:
                if h.confidence < self.min_conf_var.get(): continue
                iid = f"{h.timestamp}-{h.source}-{h.destination}"
                self.tree.insert("", "end", iid=iid, values=(h.source, h.destination, f"{h.players_left:,}", f"{h.players_appeared:,}", f"{h.players_moved:,}", f"{h.confidence}%", time.strftime("%H:%M:%S", time.localtime(h.timestamp))))
            if not self.history: self.detail.set("Waiting for a population change large enough to produce a detection.")

    def select(self, _event):
        if self.view != "hops": return
        selected = self.tree.selection()
        if not selected: return
        try: ts, source, destination = selected[0].split("-"); ts=float(ts); source=int(source); destination=int(destination)
        except ValueError: return
        h = next((x for x in self.history if x.timestamp == ts and x.source == source and x.destination == destination), None)
        if h:
            self.detail.set(f"World {h.source} → World {h.destination}  •  {h.players_left:,} players left World {h.source}  •  {h.players_appeared:,} appeared in World {h.destination}  •  Estimated {h.players_moved:,} players moved  •  {h.confidence}% same-group likelihood")

    def clear_history(self):
        self.history.clear(); self.detail.set("Detection history cleared."); self.render()

def main():
    root = tk.Tk(); App(root); root.mainloop()

if __name__ == "__main__": main()
