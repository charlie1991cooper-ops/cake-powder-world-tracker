import html
import re
import threading
import time
import urllib.request
from dataclasses import dataclass
from html.parser import HTMLParser
import tkinter as tk
from tkinter import ttk, messagebox

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
    """Extracts only rows from the actual world-list table."""
    def __init__(self):
        super().__init__()
        self.in_tr = False
        self.in_cell = False
        self.current = []
        self.buffer = []
        self.rows = []

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if tag == "tr":
            self.in_tr = True
            self.in_cell = False
            self.current = []
            self.buffer = []
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
            self.buffer = []


def fetch_worlds():
    req = urllib.request.Request(
        SOURCE_URL,
        headers={
            "User-Agent": "CakePowder-OSRS-World-Tracker/2.0",
            "Accept": "text/html,application/xhtml+xml",
        },
    )
    with urllib.request.urlopen(req, timeout=12) as response:
        raw = response.read().decode("utf-8", errors="replace")

    parser = TableParser()
    parser.feed(raw)

    worlds = {}
    for row in parser.rows:
        if len(row) < 5:
            continue

        # Actual world-table rows have this exact structure:
        # Old School XXX | N players | Location | Members/Free | Activity
        m_world = re.fullmatch(r"Old School\s+(\d+)", row[0], re.I)
        m_players = re.fullmatch(r"([\d,]+)\s+players?", row[1], re.I)

        if not m_world or not m_players:
            continue

        membership = row[3].strip()
        if membership not in ("Members", "Free"):
            continue

        world_id = int(m_world.group(1))
        players = int(m_players.group(1).replace(",", ""))

        worlds[world_id] = World(
            world=world_id,
            players=players,
            location=row[2].strip(),
            membership=membership,
            activity=row[4].strip() or "-",
        )

    if not worlds:
        raise RuntimeError("No OSRS world rows were found.")

    return sorted(worlds.values(), key=lambda w: w.world)


def detect_hops(previous, current, min_group):
    """Find likely source->destination movements between two snapshots.

    Confidence is deliberately a likelihood estimate, not proof that the
    same accounts moved. It combines population matching and how dominant
    the source/destination changes are among all changes in the snapshot.
    """
    drops = []
    gains = []

    common = set(previous) & set(current)
    for wid in common:
        delta = current[wid].players - previous[wid].players
        if delta <= -min_group:
            drops.append([wid, -delta])
        elif delta >= min_group:
            gains.append([wid, delta])

    if not drops or not gains:
        return []

    total_drop = sum(x[1] for x in drops)
    total_gain = sum(x[1] for x in gains)

    # Largest/strongest pairings first.
    candidates = []
    for source, left in drops:
        for destination, appeared in gains:
            ratio = min(left, appeared) / max(left, appeared)
            source_share = left / total_drop if total_drop else 0
            dest_share = appeared / total_gain if total_gain else 0

            # 55% population match, 25% source dominance, 20% destination dominance.
            score = 55 * ratio + 25 * source_share + 20 * dest_share
            candidates.append((score, source, destination, left, appeared))

    candidates.sort(reverse=True)

    used_sources = set()
    used_destinations = set()
    result = []

    for score, source, destination, left, appeared in candidates:
        if source in used_sources or destination in used_destinations:
            continue

        moved = min(left, appeared)
        confidence = max(0, min(99, round(score)))

        # Don't report weak matches. The UI also has its own confidence filter.
        if confidence >= 50:
            result.append(
                Hop(
                    source=source,
                    destination=destination,
                    players_left=left,
                    players_appeared=appeared,
                    players_moved=moved,
                    confidence=confidence,
                    timestamp=time.time(),
                )
            )
            used_sources.add(source)
            used_destinations.add(destination)

    return result


class WorldTrackerApp:
    def __init__(self, root):
        self.root = root
        self.root.title(APP_NAME)
        self.root.geometry("1260x760")
        self.root.minsize(1000, 650)

        self.worlds = []
        self.previous_snapshot = None
        self.history = []
        self.running = True
        self.baseline_ready = False
        self.last_update = None

        self.f2p_var = tk.BooleanVar(value=False)
        self.min_group_var = tk.IntVar(value=10)
        self.min_conf_var = tk.IntVar(value=75)
        self.keep_history_var = tk.BooleanVar(value=True)
        self.notifications_var = tk.BooleanVar(value=True)
        self.status_var = tk.StringVar(value="Starting…")
        self.view_var = tk.StringVar(value="hops")

        self._build_styles()
        self._build_ui()

        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self.root.after(150, self.start_refresh)

    def _build_styles(self):
        style = ttk.Style(self.root)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass

        bg = "#080d16"
        panel = "#101722"
        panel2 = "#131d2a"
        fg = "#e8edf5"
        muted = "#9aa8ba"
        purple = "#9b5cff"
        purple2 = "#6f35d9"
        green = "#55d88a"

        self.colors = {
            "bg": bg, "panel": panel, "panel2": panel2,
            "fg": fg, "muted": muted, "purple": purple,
            "purple2": purple2, "green": green,
        }

        self.root.configure(bg=bg)

        style.configure("TFrame", background=bg)
        style.configure("Panel.TFrame", background=panel)
        style.configure("TLabel", background=bg, foreground=fg, font=("Segoe UI", 10))
        style.configure("Muted.TLabel", background=bg, foreground=muted, font=("Segoe UI", 9))
        style.configure("Title.TLabel", background=bg, foreground="white",
                        font=("Segoe UI", 20, "bold"))
        style.configure("Brand.TLabel", background=bg, foreground=purple,
                        font=("Segoe UI", 10, "bold"))
        style.configure("Section.TLabel", background=panel, foreground=purple,
                        font=("Segoe UI", 10, "bold"))
        style.configure("TButton", font=("Segoe UI", 9, "bold"), padding=(12, 7))
        style.map("TButton",
                  background=[("active", purple2)],
                  foreground=[("active", "white")])

        style.configure("View.TButton", background=panel2, foreground=fg,
                        padding=(14, 8), borderwidth=0)
        style.map("View.TButton",
                  background=[("active", purple2)])
        style.configure("Treeview",
                        background="#0b111b", fieldbackground="#0b111b",
                        foreground=fg, rowheight=32, borderwidth=0,
                        font=("Segoe UI", 9))
        style.configure("Treeview.Heading",
                        background=panel2, foreground="#dfe5ef",
                        font=("Segoe UI", 9, "bold"), padding=(8, 9))
        style.map("Treeview",
                  background=[("selected", "#43258a")],
                  foreground=[("selected", "white")])

        style.configure("Horizontal.TProgressbar", background=purple)

    def _build_ui(self):
        # Header
        header = ttk.Frame(self.root, padding=(18, 14, 18, 10))
        header.grid(row=0, column=0, sticky="ew")
        header.columnconfigure(1, weight=1)

        left = ttk.Frame(header)
        left.grid(row=0, column=0, sticky="w")
        ttk.Label(left, text="Cake Powder", style="Title.TLabel").pack(side="left")
        ttk.Label(left, text="  /  FAITHFUL FEW", style="Brand.TLabel").pack(side="left", padx=(4, 0))

        ttk.Label(header, textvariable=self.status_var,
                  style="Muted.TLabel").grid(row=0, column=1, sticky="e", padx=12)

        self.refresh_btn = ttk.Button(header, text="Refresh Now", command=self.manual_refresh)
        self.refresh_btn.grid(row=0, column=2, sticky="e")

        # Toolbar
        toolbar = ttk.Frame(self.root, padding=(18, 0, 18, 12))
        toolbar.grid(row=1, column=0, sticky="ew")
        toolbar.columnconfigure(3, weight=1)

        self.world_btn = ttk.Button(toolbar, text="◉  Worlds",
                                    style="View.TButton",
                                    command=lambda: self.set_view("worlds"))
        self.world_btn.grid(row=0, column=0, padx=(0, 6))

        self.hop_btn = ttk.Button(toolbar, text="◉  Group Hops",
                                  style="View.TButton",
                                  command=lambda: self.set_view("hops"))
        self.hop_btn.grid(row=0, column=1, padx=(0, 18))

        ttk.Separator(toolbar, orient="vertical").grid(row=0, column=2, sticky="ns", padx=6)

        ttk.Checkbutton(toolbar, text="Include F2P worlds",
                        variable=self.f2p_var,
                        command=self.on_filter_changed).grid(row=0, column=3, sticky="w")

        ttk.Label(toolbar, text="10-second detection window",
                  style="Muted.TLabel").grid(row=0, column=4, sticky="e")

        # Main content
        content = ttk.Frame(self.root, padding=(18, 0, 18, 18))
        content.grid(row=2, column=0, sticky="nsew")
        content.columnconfigure(1, weight=1)
        content.rowconfigure(0, weight=1)

        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(2, weight=1)

        # Settings panel
        settings = ttk.Frame(content, style="Panel.TFrame", padding=16)
        settings.grid(row=0, column=0, sticky="nsw", padx=(0, 12))
        settings.columnconfigure(1, weight=1)

        ttk.Label(settings, text="DETECTION SETTINGS",
                  style="Section.TLabel").grid(row=0, column=0, columnspan=2,
                                                sticky="w", pady=(0, 16))

        ttk.Label(settings, text="Detection window").grid(row=1, column=0, sticky="w", pady=6)
        ttk.Label(settings, text="10 seconds", style="Muted.TLabel").grid(
            row=1, column=1, sticky="e", pady=6)

        ttk.Label(settings, text="Minimum group size").grid(row=2, column=0, sticky="w", pady=6)
        spin = ttk.Spinbox(settings, from_=1, to=500, textvariable=self.min_group_var,
                           width=8, command=self.reset_baseline)
        spin.grid(row=2, column=1, sticky="e", pady=6)

        ttk.Label(settings, text="Minimum confidence").grid(row=3, column=0, sticky="w", pady=6)
        spin2 = ttk.Spinbox(settings, from_=0, to=99, textvariable=self.min_conf_var,
                            width=8)
        spin2.grid(row=3, column=1, sticky="e", pady=6)

        ttk.Separator(settings).grid(row=4, column=0, columnspan=2,
                                     sticky="ew", pady=12)

        ttk.Checkbutton(settings, text="Play notification",
                        variable=self.notifications_var).grid(
                            row=5, column=0, columnspan=2, sticky="w", pady=4)
        ttk.Checkbutton(settings, text="Keep history",
                        variable=self.keep_history_var).grid(
                            row=6, column=0, columnspan=2, sticky="w", pady=4)

        ttk.Button(settings, text="Clear Detection History",
                   command=self.clear_history).grid(
                       row=7, column=0, columnspan=2, sticky="ew", pady=(12, 4))

        ttk.Separator(settings).grid(row=8, column=0, columnspan=2,
                                     sticky="ew", pady=16)

        ttk.Label(settings, text="CONFIDENCE GUIDE",
                  style="Section.TLabel").grid(row=9, column=0, columnspan=2,
                                                sticky="w", pady=(0, 10))

        guide = [
            ("90–99%", "Very likely", "#55d88a"),
            ("75–89%", "Likely", "#f0c75e"),
            ("50–74%", "Possible", "#f09b32"),
            ("0–49%", "Unlikely", "#ff5d67"),
        ]
        for i, (pct, text, color) in enumerate(guide, start=10):
            ttk.Label(settings, text=pct,
                      foreground=color, background=self.colors["panel"],
                      font=("Segoe UI", 9, "bold")).grid(
                          row=i, column=0, sticky="w", pady=3)
            ttk.Label(settings, text=text,
                      foreground=color, background=self.colors["panel"]).grid(
                          row=i, column=1, sticky="w", pady=3)

        ttk.Label(
            settings,
            text="The likelihood estimates whether a population drop\n"
                 "and rise are consistent with the same group moving.\n"
                 "It is not proof that the same accounts moved.",
            foreground=self.colors["muted"],
            background=self.colors["panel"],
            justify="left",
            font=("Segoe UI", 9),
        ).grid(row=14, column=0, columnspan=2, sticky="sw", pady=(22, 0))

        # Main table panel
        self.table_panel = ttk.Frame(content, style="Panel.TFrame", padding=14)
        self.table_panel.grid(row=0, column=1, sticky="nsew")
        self.table_panel.columnconfigure(0, weight=1)
        self.table_panel.rowconfigure(1, weight=1)

        self.panel_title = ttk.Label(self.table_panel, text="GROUP HOP DETECTIONS",
                                     style="Section.TLabel")
        self.panel_title.grid(row=0, column=0, sticky="w", pady=(0, 10))

        self.tree = ttk.Treeview(self.table_panel, show="headings")
        self.tree.grid(row=1, column=0, sticky="nsew")

        scrollbar = ttk.Scrollbar(self.table_panel, orient="vertical",
                                  command=self.tree.yview)
        scrollbar.grid(row=1, column=1, sticky="ns")
        self.tree.configure(yscrollcommand=scrollbar.set)

        self.tree.bind("<<TreeviewSelect>>", self.on_tree_select)

        self.detail_var = tk.StringVar(value="Select a detection to see details.")
        ttk.Label(self.table_panel, textvariable=self.detail_var,
                  style="Muted.TLabel", justify="left").grid(
                      row=2, column=0, sticky="w", pady=(10, 0))

        self.set_view("hops")

    def set_view(self, view):
        self.view_var.set(view)
        if view == "hops":
            self.panel_title.configure(text="GROUP HOP DETECTIONS")
            self._configure_hop_tree()
            self.refresh_hop_table()
        else:
            self.panel_title.configure(text="OSRS WORLD POPULATIONS")
            self._configure_world_tree()
            self.refresh_world_table()

    def _configure_hop_tree(self):
        columns = ("from", "to", "left", "appeared", "moved", "confidence", "detected")
        self.tree["columns"] = columns
        headings = {
            "from": "FROM WORLD",
            "to": "TO WORLD",
            "left": "PLAYERS LEFT",
            "appeared": "PLAYERS APPEARED",
            "moved": "PLAYERS MOVED (EST.)",
            "confidence": "SAME-GROUP LIKELIHOOD",
            "detected": "DETECTED",
        }
        widths = {"from": 105, "to": 105, "left": 120, "appeared": 140,
                  "moved": 145, "confidence": 180, "detected": 145}
        for col in columns:
            self.tree.heading(col, text=headings[col])
            self.tree.column(col, width=widths[col], anchor="center", stretch=True)

    def _configure_world_tree(self):
        columns = ("world", "players", "location", "type", "activity")
        self.tree["columns"] = columns
        headings = {
            "world": "WORLD", "players": "PLAYERS", "location": "LOCATION",
            "type": "TYPE", "activity": "ACTIVITY"
        }
        widths = {"world": 90, "players": 120, "location": 180, "type": 110, "activity": 260}
        for col in columns:
            self.tree.heading(col, text=headings[col])
            self.tree.column(col, width=widths[col], anchor="center" if col != "activity" else "w",
                             stretch=True)

    def visible_worlds(self):
        if self.f2p_var.get():
            return self.worlds
        return [w for w in self.worlds if w.membership == "Members"]

    def on_filter_changed(self):
        self.reset_baseline()
        self.refresh_world_table()
        self.refresh_hop_table()

    def reset_baseline(self):
        self.previous_snapshot = None
        self.baseline_ready = False
        self.detail_var.set("Baseline reset. Waiting for the next world snapshot.")

    def manual_refresh(self):
        if not self.refresh_btn.instate(["disabled"]):
            self.start_refresh()

    def start_refresh(self):
        self.refresh_btn.state(["disabled"])
        self.status_var.set("Updating worlds…")
        threading.Thread(target=self._fetch_worker, daemon=True).start()

    def _fetch_worker(self):
        try:
            worlds = fetch_worlds()
            self.root.after(0, lambda: self._apply_worlds(worlds))
        except Exception as exc:
            self.root.after(0, lambda: self._fetch_failed(str(exc)))

    def _fetch_failed(self, error):
        self.refresh_btn.state(["!disabled"])
        self.status_var.set("Update failed — retrying in 10 seconds")
        if self.baseline_ready:
            self.detail_var.set("World-list update failed: " + error)

        self.root.after(POLL_SECONDS * 1000, self.start_refresh)

    def _apply_worlds(self, worlds):
        self.worlds = worlds
        now = time.time()

        visible = self.visible_worlds()
        current = {w.world: w for w in visible}

        if self.previous_snapshot is None:
            self.previous_snapshot = current
            self.baseline_ready = True
        else:
            hops = detect_hops(self.previous_snapshot, current, self.min_group_var.get())
            min_conf = self.min_conf_var.get()

            for hop in hops:
                if hop.confidence >= min_conf:
                    self.history.insert(0, hop)

            if len(self.history) > 250:
                self.history = self.history[:250]

            self.previous_snapshot = current

        self.last_update = now
        self.refresh_btn.state(["!disabled"])

        shown = "F2P + Members" if self.f2p_var.get() else "Members only"
        self.status_var.set(
            f"Updated {time.strftime('%H:%M:%S')}  •  {len(visible)} worlds  •  {shown}"
        )

        if self.view_var.get() == "hops":
            self.refresh_hop_table()
        else:
            self.refresh_world_table()

        self.root.after(POLL_SECONDS * 1000, self.start_refresh)

    def refresh_world_table(self):
        if self.view_var.get() != "worlds":
            return
        self.tree.delete(*self.tree.get_children())
        for world in self.visible_worlds():
            self.tree.insert(
                "", "end",
                values=(
                    world.world,
                    f"{world.players:,}",
                    world.location,
                    world.membership,
                    world.activity,
                )
            )

    def refresh_hop_table(self):
        if self.view_var.get() != "hops":
            return
        self.tree.delete(*self.tree.get_children())

        min_conf = self.min_conf_var.get()
        for hop in self.history:
            if hop.confidence < min_conf:
                continue
            self.tree.insert(
                "", "end",
                iid=f"{hop.timestamp}-{hop.source}-{hop.destination}",
                values=(
                    hop.source,
                    hop.destination,
                    f"{hop.players_left:,}",
                    f"{hop.players_appeared:,}",
                    f"{hop.players_moved:,}",
                    f"{hop.confidence}%",
                    time.strftime("%H:%M:%S", time.localtime(hop.timestamp)),
                )
            )

        if not self.history:
            self.detail_var.set(
                "Waiting for a population change large enough to produce a detection."
            )

    def on_tree_select(self, _event):
        if self.view_var.get() != "hops":
            return
        selected = self.tree.selection()
        if not selected:
            return

        try:
            ts, source, destination = selected[0].split("-")
            ts = float(ts)
            source = int(source)
            destination = int(destination)
        except ValueError:
            return

        hop = next(
            (h for h in self.history
             if h.timestamp == ts and h.source == source and h.destination == destination),
            None
        )
        if not hop:
            return

        label = (
            f"World {hop.source} → World {hop.destination}    •    "
            f"{hop.players_left:,} players left World {hop.source}    •    "
            f"{hop.players_appeared:,} appeared in World {hop.destination}    •    "
            f"Estimated {hop.players_moved:,} players moved    •    "
            f"{hop.confidence}% same-group likelihood"
        )
        self.detail_var.set(label)

    def clear_history(self):
        self.history.clear()
        self.detail_var.set("Detection history cleared.")
        self.refresh_hop_table()

    def close(self):
        self.running = False
        self.root.destroy()


def main():
    root = tk.Tk()
    WorldTrackerApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
