import html
import json
import re
import threading
import time
import urllib.request
from collections import defaultdict, deque
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
import tkinter as tk
from tkinter import ttk

try:
    import winsound
except ImportError:
    winsound = None

SOURCE_URL = "https://oldschool.runescape.com/slu"
INTERVAL = 2
MOVEMENT_WINDOW = 10
CONVERGENCE_WINDOW = 30
TEAM_HISTORY_SECONDS = 60 * 60
MAX_TRACKED_MOVEMENT = 400
APP_NAME = "Cake's OSRS World Tracker"
APP_PASSWORD = "1234"
STARTUP_LOG = Path.home() / "cakes_osrs_tracker_startup.log"


@dataclass(frozen=True)
class World:
    world: int
    players: int
    location: str
    membership: str
    activity: str


@dataclass(frozen=True)
class Change:
    world: int
    amount: int
    start_time: float
    end_time: float

    @property
    def magnitude(self):
        return abs(self.amount)


@dataclass(frozen=True)
class Hop:
    source: int
    destination: int
    left: int
    appeared: int
    moved: int
    score: int
    timestamp: float


@dataclass(frozen=True)
class Convergence:
    destination: int
    sources: tuple
    source_amounts: tuple
    appeared: int
    score: int
    timestamp: float

    @property
    def source_count(self):
        return len(self.sources)

    @property
    def total_outflow(self):
        return sum(self.source_amounts)


@dataclass(frozen=True)
class WorldMovement:
    world: int
    delta: int
    score: int
    timestamp: float
    watched: bool = False


class WorldParser(HTMLParser):
    """Parse the official world list, using id=\"slu-world-XXX\" for real IDs."""

    def __init__(self):
        super().__init__()
        self.in_row = False
        self.in_cell = False
        self.cell_buf = []
        self.row = []
        self.world_id = None
        self.rows = []

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        attrs = dict(attrs)
        if tag == "tr":
            self.in_row = True
            self.in_cell = False
            self.cell_buf = []
            self.row = []
            self.world_id = None
        elif self.in_row and tag in ("td", "th"):
            self.in_cell = True
            self.cell_buf = []
        elif self.in_row and tag == "a":
            ident = attrs.get("id", "")
            match = re.fullmatch(r"slu-world-(\d+)", ident)
            if match:
                self.world_id = int(match.group(1))

    def handle_data(self, data):
        if self.in_cell:
            self.cell_buf.append(data)

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag in ("td", "th") and self.in_cell:
            text = re.sub(r"\s+", " ", html.unescape("".join(self.cell_buf))).strip()
            self.row.append(text)
            self.in_cell = False
        elif tag == "tr" and self.in_row:
            if self.world_id is not None and self.row:
                self.rows.append((self.world_id, self.row))
            self.in_row = False
            self.in_cell = False
            self.cell_buf = []
            self.row = []
            self.world_id = None


def fetch_worlds():
    request = urllib.request.Request(
        SOURCE_URL,
        headers={"User-Agent": "Cakes-OSRS-World-Tracker/7.0"},
    )
    with urllib.request.urlopen(request, timeout=15) as response:
        raw = response.read().decode("utf-8", "replace")

    parser = WorldParser()
    parser.feed(raw)
    worlds = []
    for world_id, row in parser.rows:
        if len(row) < 5:
            continue
        players_match = re.search(r"([\d,]+)\s+players?", row[1], re.I)
        if not players_match:
            continue
        players = int(players_match.group(1).replace(",", ""))
        membership = row[3]
        if membership not in ("Members", "Free"):
            continue
        worlds.append(World(
            world=world_id,
            players=players,
            location=row[2],
            membership=membership,
            activity=row[4] or "-",
        ))

    unique = {world.world: world for world in worlds}
    if not unique:
        raise RuntimeError("No OSRS worlds found in the server list.")
    return sorted(unique.values(), key=lambda world: world.world)


def ratio_score(left, appeared):
    if left <= 0 or appeared <= 0:
        return 0.0
    return min(left, appeared) / max(left, appeared)


def likelihood_label(score):
    if score >= 90:
        return "VERY LIKELY"
    if score >= 75:
        return "LIKELY"
    if score >= 50:
        return "POSSIBLE"
    return "UNLIKELY"


def likelihood_tag(score):
    if score >= 90:
        return "very"
    if score >= 75:
        return "likely"
    if score >= 50:
        return "possible"
    return "unlikely"


def build_movement_episode(episodes, previous, current, now, min_group):
    """Turn many small polls into one movement episode per world."""
    emitted = []
    min_group = max(1, min(MAX_TRACKED_MOVEMENT, int(min_group)))

    for world in set(previous) & set(current):
        prev_pop = previous[world].players
        cur_pop = current[world].players
        step = cur_pop - prev_pop
        state = episodes.get(world)

        if state is None:
            if step == 0:
                continue
            state = {
                "direction": 1 if step > 0 else -1,
                "anchor_population": prev_pop,
                "start_time": now,
                "triggered": False,
            }
            episodes[world] = state

        net = cur_pop - state["anchor_population"]
        direction = state["direction"]

        # Once an episode crosses back through its anchor, begin a new episode.
        if (direction > 0 and net < 0) or (direction < 0 and net > 0):
            if step == 0:
                episodes.pop(world, None)
                continue
            state = {
                "direction": 1 if step > 0 else -1,
                "anchor_population": prev_pop,
                "start_time": now,
                "triggered": False,
            }
            episodes[world] = state
            net = cur_pop - state["anchor_population"]

        if net == 0 and step == 0:
            episodes.pop(world, None)
            continue

        if abs(net) > MAX_TRACKED_MOVEMENT:
            episodes.pop(world, None)
            continue

        if abs(net) >= min_group and not state["triggered"]:
            state["triggered"] = True
            emitted.append(Change(world, net, state["start_time"], now))

    return emitted


def score_hop(source, destination, now):
    """Score one inferred source -> destination movement."""
    age = abs(source.end_time - destination.end_time)
    if age > MOVEMENT_WINDOW:
        return 0

    ratio = ratio_score(source.magnitude, destination.magnitude)
    timing = 1.0 - age / MOVEMENT_WINDOW
    size = max(source.magnitude, destination.magnitude)

    score = (
        44.0 * ratio
        + 34.0 * timing
        + min(22.0, size / 25.0 * 22.0)
    )
    if ratio < 0.50:
        score -= 20
    elif ratio < 0.65:
        score -= 12
    elif ratio < 0.80:
        score -= 6
    if size <= 6:
        score -= 5
    return min(99, max(0, round(score)))


def match_movement_events(drops, gains, now, min_group):
    """Globally match distinct movement episodes inside the 10-second window."""
    min_group = max(1, min(MAX_TRACKED_MOVEMENT, int(min_group)))
    drops = [d for d in drops if d.end_time >= now - MOVEMENT_WINDOW and d.magnitude >= min_group]
    gains = [g for g in gains if g.end_time >= now - MOVEMENT_WINDOW and g.magnitude >= min_group]

    candidates = []
    for source in drops:
        for destination in gains:
            if source.world == destination.world:
                continue
            score = score_hop(source, destination, now)
            if score >= 45:
                candidates.append((score, source, destination))

    candidates.sort(key=lambda item: (item[0], min(item[1].magnitude, item[2].magnitude)), reverse=True)
    used_sources = set()
    used_destinations = set()
    results = []

    for score, source, destination in candidates:
        if source.world in used_sources or destination.world in used_destinations:
            continue
        results.append(Hop(
            source=source.world,
            destination=destination.world,
            left=source.magnitude,
            appeared=destination.magnitude,
            moved=max(source.magnitude, destination.magnitude),
            score=score,
            timestamp=max(source.end_time, destination.end_time),
        ))
        used_sources.add(source.world)
        used_destinations.add(destination.world)

    return results


def detect_convergences(events, timestamp, min_group):
    """Find multiple source-world outflows converging on one destination."""
    cutoff = timestamp - CONVERGENCE_WINDOW
    min_group = max(1, min(MAX_TRACKED_MOVEMENT, int(min_group)))
    recent = [e for e in events if e.end_time >= cutoff and e.magnitude <= MAX_TRACKED_MOVEMENT]
    drops = [e for e in recent if e.amount <= -min_group]
    gains = [e for e in recent if e.amount >= min_group]
    results = []

    for destination in gains:
        choices = []
        for source in drops:
            if source.world == destination.world:
                continue
            age = abs(source.end_time - destination.end_time)
            if age > CONVERGENCE_WINDOW:
                continue
            ratio = ratio_score(source.magnitude, destination.magnitude)
            timing = 1.0 - age / CONVERGENCE_WINDOW
            quality = ratio * 0.65 + timing * 0.35
            choices.append((quality, source, ratio, timing))

        best_by_world = {}
        for quality, source, ratio, timing in choices:
            current = best_by_world.get(source.world)
            if current is None or quality > current[0]:
                best_by_world[source.world] = (quality, source, ratio, timing)

        chosen = sorted(best_by_world.values(), key=lambda item: (item[0], item[1].magnitude), reverse=True)[:6]
        if len(chosen) < 2:
            continue

        sizes = [item[1].magnitude for item in chosen]
        median_size = sorted(sizes)[len(sizes) // 2]
        consistency = 1.0 - sum(abs(size - median_size) for size in sizes) / max(1, sum(sizes))
        avg_ratio = sum(item[2] for item in chosen) / len(chosen)
        avg_timing = sum(item[3] for item in chosen) / len(chosen)
        score = (
            42
            + 23 * avg_ratio
            + 18 * avg_timing
            + 20 * max(0.0, min(1.0, consistency))
            + min(24, (len(chosen) - 2) * 12)
        )
        if len(chosen) >= 3:
            score += 8
        if len(chosen) >= 4:
            score += 6

        result = Convergence(
            destination=destination.world,
            sources=tuple(item[1].world for item in chosen),
            source_amounts=tuple(item[1].magnitude for item in chosen),
            appeared=destination.magnitude,
            score=min(99, max(0, round(score))),
            timestamp=max([destination.end_time] + [item[1].end_time for item in chosen]),
        )
        results.append(result)

    best = {}
    for result in results:
        existing = best.get(result.destination)
        if existing is None or result.score > existing.score:
            best[result.destination] = result
    return sorted(best.values(), key=lambda result: (result.score, result.source_count), reverse=True)


class TeamTrack:
    """A persistent inferred team remembered for up to one hour."""

    def __init__(self, *, first_hop=None, convergence=None):
        self.hops = []
        self.last_world = None
        self.last_time = 0.0
        self.created_time = time.time()
        self.seed_text = ""
        self.approx_size = 0
        if first_hop is not None:
            self.hops.append(first_hop)
            self.last_world = first_hop.destination
            self.last_time = first_hop.timestamp
            self.approx_size = first_hop.moved
        elif convergence is not None:
            self.last_world = convergence.destination
            self.last_time = convergence.timestamp
            self.approx_size = convergence.appeared
            self.seed_text = " + ".join(map(str, convergence.sources)) + f" → {convergence.destination}"

    @property
    def age(self):
        return max(0.0, time.time() - self.last_time)

    @property
    def hop_count(self):
        return len(self.hops) + (1 if self.seed_text else 0)

    @property
    def route(self):
        if self.seed_text:
            tail = [self.last_world]
            for hop in self.hops:
                tail.append(hop.destination)
            return [self.seed_text] if not self.hops else [self.seed_text] + tail[1:]
        if not self.hops:
            return []
        return [self.hops[0].source] + [hop.destination for hop in self.hops]

    @property
    def size(self):
        values = [hop.moved for hop in self.hops[-8:]]
        if self.seed_text and self.approx_size:
            values.append(self.approx_size)
        return max(1, round(sum(values) / len(values))) if values else max(1, self.approx_size)

    @property
    def score(self):
        hop_scores = [hop.score for hop in self.hops[-8:]]
        base = sum(hop_scores) / len(hop_scores) if hop_scores else 68
        consistency = 1.0
        values = [hop.moved for hop in self.hops[-8:]]
        if len(values) > 1:
            mean = sum(values) / len(values)
            deviation = sum(abs(v - mean) for v in values) / len(values)
            consistency = max(0.0, 1.0 - deviation / max(1, mean))
        repeat_bonus = min(30, max(0, len(self.hops) - 1) * 8)
        seed_bonus = 12 if self.seed_text else 0
        return min(99, max(0, round(base * 0.68 + consistency * 14 + repeat_bonus + seed_bonus)))

    def is_alive(self):
        return self.last_time > 0 and (time.time() - self.last_time) <= TEAM_HISTORY_SECONDS

    def can_extend(self, hop):
        if not self.is_alive() or hop.source != self.last_world:
            return False
        expected = max(1, self.size)
        ratio = hop.moved / expected
        return 0.35 <= ratio <= 1.65

    def add(self, hop):
        self.hops.append(hop)
        self.last_world = hop.destination
        self.last_time = hop.timestamp
        self.approx_size = hop.moved


class App:
    def __init__(self, root):
        self.root = root
        root.title(APP_NAME)
        root.geometry("1280x760")
        root.minsize(1050, 650)
        root.configure(bg="#0b1018")

        style = ttk.Style(root)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        self.configure_style(style)

        self.worlds = []
        self.previous = None
        self.movement_episodes = {}
        self.recent_events = deque(maxlen=500)
        self.movement_history = deque(maxlen=500)
        self.hops = deque(maxlen=250)
        self.convergences = deque(maxlen=150)
        self.teams = []
        self.alerts = deque(maxlen=250)
        self.delta_noise = defaultdict(lambda: deque(maxlen=24))
        self.last_hop_reported = {}
        self.last_convergence_reported = {}
        self.fetch_in_progress = False
        self.fetch_failures = 0
        self.last_fetch_started = 0.0

        self.f2p = tk.BooleanVar(value=False)
        self.min_group = tk.IntVar(value=10)
        self.min_conf = tk.IntVar(value=50)
        self.watch_enabled = tk.BooleanVar(value=False)
        self.watch_world = tk.StringVar(value="")
        self.watch_threshold = tk.IntVar(value=10)
        self.world_alert_threshold = tk.IntVar(value=10)
        self.sound_alerts = tk.BooleanVar(value=False)

        self.view = "teams"
        self.status = tk.StringVar(value="Starting…")
        self.detail = tk.StringVar(value="Waiting for first world snapshot.")
        self.alert_banner = tk.StringVar(value="No alerts yet")

        self.build_ui()
        root.protocol("WM_DELETE_WINDOW", self.close)
        root.after(100, self.refresh)

    def configure_style(self, style):
        style.configure("TFrame", background="#0b1018")
        style.configure("TLabel", background="#0b1018", foreground="#e7edf6", font=("Segoe UI", 9))
        style.configure("Header.TLabel", background="#0b1018", foreground="#f4f7fb", font=("Segoe UI", 22, "bold"))
        style.configure("Status.TLabel", background="#0b1018", foreground="#8d9ab0", font=("Segoe UI", 9))
        style.configure("Title.TLabel", background="#0b1018", foreground="#b477ff", font=("Segoe UI", 11, "bold"))
        style.configure("TCheckbutton", background="#111927", foreground="#dce4f0", font=("Segoe UI", 9))
        style.map("TCheckbutton", background=[("active", "#111927")])
        style.configure("TLabelframe", background="#111927", foreground="#a970ff", bordercolor="#263247")
        style.configure("TLabelframe.Label", background="#111927", foreground="#a970ff", font=("Segoe UI", 9, "bold"))
        style.configure("TButton", background="#182235", foreground="#e7edf6", bordercolor="#2a3951", padding=(11, 6), font=("Segoe UI", 9, "bold"))
        style.map("TButton", background=[("active", "#293953"), ("pressed", "#34476a")])
        style.configure("Accent.TButton", background="#7c4dff", foreground="white", bordercolor="#7c4dff", padding=(13, 7), font=("Segoe UI", 9, "bold"))
        style.map("Accent.TButton", background=[("active", "#966eff"), ("pressed", "#6938df")])
        style.configure("Modern.Treeview", background="#111927", fieldbackground="#111927", foreground="#e7edf6", rowheight=31, borderwidth=0, relief="flat", font=("Segoe UI", 9))
        style.configure("Modern.Treeview.Heading", background="#182235", foreground="#d5ddeb", relief="flat", borderwidth=0, padding=(8, 8), font=("Segoe UI", 9, "bold"))
        style.map("Modern.Treeview", background=[("selected", "#3d2875")], foreground=[("selected", "white")])
        style.configure("Modern.Vertical.TScrollbar", background="#182235", troughcolor="#0b1018", bordercolor="#0b1018", arrowcolor="#91a0b5")

    def build_ui(self):
        header = ttk.Frame(self.root, padding=(18, 14, 18, 8))
        header.pack(fill="x")
        ttk.Label(header, text=APP_NAME, style="Header.TLabel").pack(side="left")
        ttk.Label(header, textvariable=self.status, style="Status.TLabel").pack(side="right")

        controls = ttk.Frame(self.root, padding=(18, 2, 18, 10))
        controls.pack(fill="x")
        ttk.Label(controls, text="Group size ≥").pack(side="left")
        ttk.Spinbox(controls, from_=1, to=400, textvariable=self.min_group, width=5).pack(side="left", padx=(5, 14))
        ttk.Label(controls, text="Show").pack(side="left")
        self.conf_combo = ttk.Combobox(controls, values=("Possible", "Likely", "Very likely"), state="readonly", width=11)
        self.conf_combo.current(0)
        self.conf_combo.pack(side="left", padx=(5, 14))
        self.conf_combo.bind("<<ComboboxSelected>>", self.conf_changed)
        ttk.Checkbutton(controls, text="F2P", variable=self.f2p, command=self.reset_baseline).pack(side="left", padx=(0, 12))
        ttk.Checkbutton(controls, text="Sound", variable=self.sound_alerts).pack(side="left")
        ttk.Button(controls, text="Settings", command=self.open_settings).pack(side="right", padx=(7, 0))
        ttk.Button(controls, text="Refresh", style="Accent.TButton", command=self.refresh).pack(side="right")

        body = ttk.Frame(self.root, padding=(18, 0, 18, 14))
        body.pack(fill="both", expand=True)

        nav = ttk.Frame(body)
        nav.pack(fill="x", pady=(0, 8))
        for text, view in (("Active Teams", "teams"), ("Mass Hops", "hops"), ("Convergences", "convergences"), ("Worlds", "worlds"), ("Alerts", "alerts")):
            ttk.Button(nav, text=text, command=lambda v=view: self.set_view(v)).pack(side="left", padx=(0, 5))
        ttk.Label(nav, textvariable=self.alert_banner, foreground="#b477ff").pack(side="right")

        self.view_title = ttk.Label(body, text="ACTIVE TEAMS", style="Title.TLabel")
        self.view_title.pack(anchor="w", pady=(0, 6))

        table_frame = ttk.Frame(body)
        table_frame.pack(fill="both", expand=True)
        self.tree = ttk.Treeview(table_frame, show="headings", style="Modern.Treeview")
        scrollbar = ttk.Scrollbar(table_frame, orient="vertical", command=self.tree.yview, style="Modern.Vertical.TScrollbar")
        self.tree.configure(yscrollcommand=scrollbar.set)
        self.tree.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")
        self.tree.bind("<<TreeviewSelect>>", self.select_row)
        for tag, foreground in (("very", "#39e58c"), ("likely", "#e8d44d"), ("possible", "#ff9d32"), ("unlikely", "#ff5964")):
            self.tree.tag_configure(tag, foreground=foreground)
        self.tree.tag_configure("watch", background="#241a3d")

        ttk.Label(body, textvariable=self.detail, wraplength=1100, foreground="#8d9ab0").pack(anchor="w", pady=(8, 0))
        self.set_view("teams")

    def open_settings(self):
        win = tk.Toplevel(self.root)
        win.title("Tracker Settings")
        win.geometry("400x470")
        win.resizable(False, False)
        win.configure(bg="#0b1018")
        win.transient(self.root)

        frame = ttk.Frame(win, padding=18)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text="TRACKER SETTINGS", style="Title.TLabel").pack(anchor="w")
        ttk.Label(frame, text="These settings affect detection sensitivity only.", foreground="#8d9ab0").pack(anchor="w", pady=(4, 14))

        ttk.Label(frame, text="Minimum group size").pack(anchor="w")
        ttk.Spinbox(frame, from_=1, to=400, textvariable=self.min_group, width=8).pack(anchor="w", pady=(3, 12))

        ttk.Label(frame, text="Minimum likelihood shown").pack(anchor="w")
        combo = ttk.Combobox(frame, values=("Possible", "Likely", "Very likely"), state="readonly", width=14)
        combo.set("Possible" if self.min_conf.get() == 50 else "Likely" if self.min_conf.get() == 75 else "Very likely")
        combo.bind("<<ComboboxSelected>>", lambda _e: self.conf_changed_from(combo))
        combo.pack(anchor="w", pady=(3, 12))

        ttk.Label(frame, text="Unmatched world alert threshold").pack(anchor="w")
        ttk.Spinbox(frame, from_=1, to=400, textvariable=self.world_alert_threshold, width=8).pack(anchor="w", pady=(3, 12))

        ttk.Checkbutton(frame, text="Watch a specific world", variable=self.watch_enabled, command=self.redraw).pack(anchor="w", pady=(2, 5))
        ttk.Label(frame, text="World").pack(anchor="w")
        self.settings_watch_combo = ttk.Combobox(frame, textvariable=self.watch_world, state="normal", width=12)
        self.settings_watch_combo.pack(anchor="w", pady=(3, 5))
        ttk.Label(frame, text="Watched-world alert threshold").pack(anchor="w")
        ttk.Spinbox(frame, from_=1, to=400, textvariable=self.watch_threshold, width=8).pack(anchor="w", pady=(3, 15))

        ttk.Button(frame, text="Clear history", command=lambda: (self.clear_history(), win.destroy())).pack(anchor="w", pady=(4, 8))
        ttk.Button(frame, text="Close", command=win.destroy).pack(anchor="e")

        self.update_watch_list()

    def conf_changed_from(self, combo):
        text = combo.get()
        self.min_conf.set(50 if text == "Possible" else 75 if text == "Likely" else 90)
        self.redraw()

    def conf_changed(self, _event=None):
        text = self.conf_combo.get()
        self.min_conf.set(50 if text == "Possible" else 75 if text == "Likely" else 90)
        self.redraw()

    def visible_worlds(self):
        return self.worlds if self.f2p.get() else [w for w in self.worlds if w.membership == "Members"]

    def update_watch_list(self):
        if not hasattr(self, "settings_watch_combo"):
            return
        values = [str(world.world) for world in self.visible_worlds()]
        self.settings_watch_combo["values"] = values

    def reset_baseline(self):
        self.previous = None
        self.movement_episodes.clear()
        self.recent_events.clear()
        self.movement_history.clear()
        self.last_hop_reported.clear()
        self.last_convergence_reported.clear()
        self.detail.set("Baseline reset. Waiting for the next snapshot.")
        self.redraw()

    def set_view(self, view):
        self.view = view
        titles = {
            "teams": "ACTIVE TEAMS",
            "hops": "MASS HOPS",
            "convergences": "CONVERGENCES",
            "worlds": "WORLD POPULATIONS",
            "alerts": "WORLD ALERTS",
        }
        columns = {
            "teams": ("status", "route", "size", "hops", "confidence", "last"),
            "hops": ("from", "to", "left", "appeared", "group", "confidence", "time"),
            "convergences": ("sources", "to", "appeared", "outflow", "confidence", "time"),
            "worlds": ("world", "players", "type", "location", "activity"),
            "alerts": ("world", "direction", "change", "confidence", "time"),
        }
        names = {
            "status": "STATUS", "route": "ROUTE", "size": "GROUP", "hops": "HOPS", "confidence": "CONFIDENCE", "last": "LAST",
            "from": "FROM", "to": "TO", "left": "LEFT", "appeared": "APPEARED", "group": "EST. GROUP", "time": "TIME",
            "sources": "SOURCE WORLDS", "outflow": "SOURCE OUTFLOW", "world": "WORLD", "players": "PLAYERS", "type": "TYPE", "location": "LOCATION", "activity": "ACTIVITY",
            "direction": "DIRECTION", "change": "CHANGE",
        }
        self.view_title.config(text=titles[view])
        self.tree["columns"] = columns[view]
        for col in columns[view]:
            self.tree.heading(col, text=names[col])
            self.tree.column(col, width=120, anchor="center")
        widths = {
            "teams": (("status", 85), ("route", 500), ("size", 90), ("hops", 70), ("confidence", 120), ("last", 90)),
            "hops": (("from", 75), ("to", 75), ("left", 90), ("appeared", 100), ("group", 100), ("confidence", 120), ("time", 90)),
            "convergences": (("sources", 380), ("to", 75), ("appeared", 100), ("outflow", 125), ("confidence", 120), ("time", 90)),
            "worlds": (("world", 80), ("players", 100), ("type", 90), ("location", 170), ("activity", 450)),
            "alerts": (("world", 80), ("direction", 100), ("change", 90), ("confidence", 120), ("time", 90)),
        }
        for col, width in widths[view]:
            self.tree.column(col, width=width)
        explanations = {
            "teams": "Teams are inferred from repeated group-sized hops and convergence patterns, then remembered for one hour.",
            "hops": "A mass hop is a matched population drop and rise observed within 10 seconds.",
            "convergences": "Two or more source worlds lose group-sized populations toward the same destination within 30 seconds.",
            "worlds": "Current official OSRS world population snapshot.",
            "alerts": "Unmatched unusual population movements and watched-world changes.",
        }
        self.detail.set(explanations[view])
        self.redraw()

    def refresh(self):
        if self.fetch_in_progress:
            return
        self.fetch_in_progress = True
        self.last_fetch_started = time.time()
        threading.Thread(target=self.worker, daemon=True).start()

    def worker(self):
        try:
            worlds = fetch_worlds()
            self.root.after(0, lambda worlds=worlds: self.apply_worlds(worlds))
        except Exception as exc:
            message = str(exc) or exc.__class__.__name__
            self.root.after(0, lambda message=message: self.fetch_failed(message))

    def fetch_failed(self, message):
        self.fetch_in_progress = False
        self.fetch_failures = min(self.fetch_failures + 1, 5)
        self.status.set("Update failed; retrying…")
        self.detail.set(f"Could not update world data: {message}")
        delay = int(min(20, 2 ** self.fetch_failures) * 1000)
        self.root.after(delay, self.refresh)

    def apply_worlds(self, worlds):
        self.fetch_in_progress = False
        self.fetch_failures = 0
        self.worlds = worlds
        current = {world.world: world for world in self.visible_worlds()}
        now = time.time()
        min_group = max(1, min(MAX_TRACKED_MOVEMENT, self.min_group.get()))
        self.min_group.set(min_group)
        self.world_alert_threshold.set(max(1, min(MAX_TRACKED_MOVEMENT, self.world_alert_threshold.get())))
        self.watch_threshold.set(max(1, min(MAX_TRACKED_MOVEMENT, self.watch_threshold.get())))

        if self.previous is None:
            self.previous = current
            self.update_watch_list()
            self.status.set(f"Baseline captured • {len(current)} worlds")
        else:
            new_events = build_movement_episode(self.movement_episodes, self.previous, current, now, min_group)
            for event in new_events:
                self.recent_events.append(event)
                self.movement_history.append(event)
            self.recent_events = self.prune_events(self.recent_events, now, MOVEMENT_WINDOW)
            self.movement_history = self.prune_events(self.movement_history, now, CONVERGENCE_WINDOW)

            drops = [event for event in self.recent_events if event.amount < 0]
            gains = [event for event in self.recent_events if event.amount > 0]
            hops = match_movement_events(drops, gains, now, min_group)
            hops = [hop for hop in hops if not self.hop_recently_reported(hop.source, hop.destination, now)]
            convergences = detect_convergences(self.movement_history, now, min_group)

            matched_worlds = {hop.source for hop in hops} | {hop.destination for hop in hops}
            convergence_worlds = set()

            for hop in hops:
                if hop.score < self.min_conf.get():
                    continue
                self.hops.appendleft(hop)
                self.attach_hop_to_team(hop)
                matched_worlds.add(hop.source)
                matched_worlds.add(hop.destination)

            for convergence in convergences:
                if convergence.score < self.min_conf.get():
                    continue
                if self.convergence_recently_reported(convergence, now):
                    continue
                self.convergences.appendleft(convergence)
                convergence_worlds.update(convergence.sources)
                convergence_worlds.add(convergence.destination)
                self.attach_convergence_to_team(convergence)
                self.raise_banner(
                    f"{convergence.source_count} WORLDS → {convergence.destination} • +{convergence.appeared} • {likelihood_label(convergence.score)}"
                )

            for event in new_events:
                if event.world in matched_worlds or event.world in convergence_worlds:
                    continue
                movement = self.make_world_alert(event, now)
                if movement is not None:
                    self.alerts.appendleft(movement)

            for world in set(self.previous) & set(current):
                delta = current[world].players - self.previous[world].players
                if abs(delta) <= MAX_TRACKED_MOVEMENT:
                    self.delta_noise[world].append(abs(delta))

            self.previous = current
            self.update_watch_list()
            self.expire_teams()
            self.status.set(f"Updated {time.strftime('%H:%M:%S')} • {len(current)} worlds")

        self.redraw()
        elapsed = max(0.0, time.time() - self.last_fetch_started)
        self.root.after(max(700, int(INTERVAL * 1000 - elapsed * 1000)), self.refresh)

    @staticmethod
    def prune_events(events, now, window_seconds):
        cutoff = now - window_seconds
        return deque((event for event in events if event.end_time >= cutoff), maxlen=500)

    def hop_recently_reported(self, source, destination, now, window_seconds=20):
        cutoff = now - window_seconds
        for key, stamp in list(self.last_hop_reported.items()):
            if stamp < cutoff:
                self.last_hop_reported.pop(key, None)
        key = (source, destination)
        previous = self.last_hop_reported.get(key)
        if previous is not None and now - previous < window_seconds:
            return True
        self.last_hop_reported[key] = now
        return False

    def convergence_recently_reported(self, convergence, now):
        cutoff = now - CONVERGENCE_WINDOW
        for key, stamp in list(self.last_convergence_reported.items()):
            if stamp < cutoff:
                self.last_convergence_reported.pop(key, None)
        key = (convergence.destination, tuple(sorted(convergence.sources)))
        previous = self.last_convergence_reported.get(key)
        if previous is not None and now - previous < CONVERGENCE_WINDOW:
            return True
        self.last_convergence_reported[key] = now
        return False

    def attach_hop_to_team(self, hop):
        self.teams = [team for team in self.teams if team.is_alive()]
        candidates = [team for team in self.teams if team.can_extend(hop)]
        if candidates:
            candidates.sort(key=lambda team: (abs(team.size - hop.moved), team.age))
            candidates[0].add(hop)
            return
        self.teams.insert(0, TeamTrack(first_hop=hop))
        self.teams = self.teams[:100]

    def attach_convergence_to_team(self, convergence):
        self.teams = [team for team in self.teams if team.is_alive()]
        candidates = [team for team in self.teams if team.last_world == convergence.destination]
        if candidates:
            candidates.sort(key=lambda team: (abs(team.size - convergence.appeared), team.age))
            best = candidates[0]
            # A convergence onto the team's current world reinforces the same team rather than creating a duplicate.
            best.approx_size = round((best.size + convergence.appeared) / 2)
            best.last_time = convergence.timestamp
            return
        self.teams.insert(0, TeamTrack(convergence=convergence))
        self.teams = self.teams[:100]

    def expire_teams(self):
        self.teams = [team for team in self.teams if team.is_alive()]

    def make_world_alert(self, event, now):
        threshold = max(1, min(MAX_TRACKED_MOVEMENT, self.world_alert_threshold.get()))
        watched = False
        try:
            watched_world = int(self.watch_world.get()) if self.watch_enabled.get() else None
        except ValueError:
            watched_world = None
        if watched_world == event.world and event.magnitude >= self.watch_threshold.get():
            watched = True
        if event.magnitude < threshold and not watched:
            return None
        noise_values = self.delta_noise[event.world]
        baseline = sum(noise_values) / len(noise_values) if noise_values else 0.0
        scale = max(3.0, threshold, baseline * 2.5)
        score = 45 + min(45, event.magnitude / scale * 10)
        if event.magnitude >= threshold * 2:
            score += 5
        return WorldMovement(event.world, event.amount, min(99, round(score)), now, watched)

    def raise_banner(self, text):
        self.alert_banner.set("ALERT • " + text)
        if self.sound_alerts.get() and winsound:
            try:
                winsound.MessageBeep(winsound.MB_ICONEXCLAMATION)
            except Exception:
                pass

    def select_row(self, _event=None):
        selection = self.tree.selection()
        if not selection:
            return
        values = self.tree.item(selection[0], "values")
        if self.view == "teams":
            idx = int(selection[0][1:])
            if idx < len(self.teams):
                team = self.teams[idx]
                route = " → ".join(map(str, team.route))
                self.detail.set(f"TEAM • ~{team.size} players • {likelihood_label(team.score)} • last seen {int(team.age)}s ago • {route}")
        elif self.view == "hops":
            self.detail.set(f"World {values[0]} → {values[1]} • {values[2]} left • {values[3]} appeared • estimated group {values[4]} • {values[5]}")
        elif self.view == "convergences":
            self.detail.set(f"{values[0]} → World {values[1]} • destination gain {values[2]} • source outflow {values[3]} • {values[4]}")
        elif self.view == "worlds":
            self.detail.set(f"World {values[0]} • {values[1]} players • {values[2]} • {values[3]} • {values[4]}")
        else:
            self.detail.set(f"World {values[0]} • {values[1]} {abs(int(values[2]))} players • {values[3]}")

    def redraw(self):
        self.tree.delete(*self.tree.get_children())
        min_conf = self.min_conf.get()
        if self.view == "teams":
            self.teams = [team for team in self.teams if team.is_alive()]
            for index, team in enumerate(self.teams):
                if team.score < min_conf:
                    continue
                route = " → ".join(map(str, team.route))
                values = (
                    "ACTIVE",
                    route,
                    f"~{team.size}",
                    team.hop_count,
                    likelihood_label(team.score),
                    f"{int(team.age)}s",
                )
                self.tree.insert("", "end", iid=f"t{index}", values=values, tags=(likelihood_tag(team.score),))
        elif self.view == "hops":
            for hop in self.hops:
                if hop.score < min_conf:
                    continue
                self.tree.insert("", "end", values=(hop.source, hop.destination, hop.left, hop.appeared, hop.moved, likelihood_label(hop.score), time.strftime("%H:%M:%S", time.localtime(hop.timestamp))), tags=(likelihood_tag(hop.score),))
        elif self.view == "convergences":
            for convergence in self.convergences:
                if convergence.score < min_conf:
                    continue
                sources = " + ".join(f"{world} (-{amount})" for world, amount in zip(convergence.sources, convergence.source_amounts))
                self.tree.insert("", "end", values=(sources, convergence.destination, f"+{convergence.appeared}", f"~{convergence.total_outflow}", likelihood_label(convergence.score), time.strftime("%H:%M:%S", time.localtime(convergence.timestamp))), tags=(likelihood_tag(convergence.score),))
        elif self.view == "worlds":
            try:
                watched = int(self.watch_world.get()) if self.watch_enabled.get() else None
            except ValueError:
                watched = None
            for world in self.visible_worlds():
                tags = ("watch",) if world.world == watched else ()
                self.tree.insert("", "end", values=(world.world, f"{world.players:,}", world.membership, world.location, world.activity), tags=tags)
        else:
            for movement in self.alerts:
                if movement.score < min_conf and not movement.watched:
                    continue
                direction = "INFLUX" if movement.delta > 0 else "OUTFLOW"
                tags = [likelihood_tag(movement.score)]
                if movement.watched:
                    tags.append("watch")
                self.tree.insert("", "end", values=(movement.world, direction, f"{movement.delta:+d}", likelihood_label(movement.score), time.strftime("%H:%M:%S", time.localtime(movement.timestamp))), tags=tuple(tags))

    def clear_history(self):
        self.hops.clear()
        self.convergences.clear()
        self.teams.clear()
        self.alerts.clear()
        self.recent_events.clear()
        self.movement_history.clear()
        self.movement_episodes.clear()
        self.delta_noise.clear()
        self.last_hop_reported.clear()
        self.last_convergence_reported.clear()
        self.previous = None
        self.alert_banner.set("No alerts yet")
        self.detail.set("History cleared. Waiting for the next snapshot.")
        self.redraw()

    def close(self):
        self.root.destroy()


def log_startup_error(exc):
    try:
        with STARTUP_LOG.open("a", encoding="utf-8") as stream:
            stream.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {type(exc).__name__}: {exc}\n")
    except Exception:
        pass


class PasswordWindow(tk.Tk):
    def __init__(self):
        super().__init__()
        self.unlocked = False
        self.title(APP_NAME)
        self.geometry("460x285")
        self.resizable(False, False)
        self.configure(bg="#0b1018")

        style = ttk.Style(self)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure("Login.TFrame", background="#0b1018")
        style.configure("LoginTitle.TLabel", background="#0b1018", foreground="#f4f7fb", font=("Segoe UI", 17, "bold"))
        style.configure("LoginText.TLabel", background="#0b1018", foreground="#aeb9ca", font=("Segoe UI", 10))
        style.configure("Login.TButton", padding=(12, 8), font=("Segoe UI", 10, "bold"))
        style.configure("LoginError.TLabel", background="#0b1018", foreground="#ff5964", font=("Segoe UI", 9))

        frame = ttk.Frame(self, padding=24, style="Login.TFrame")
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text=APP_NAME, style="LoginTitle.TLabel").pack(anchor="w")
        ttk.Label(frame, text="Enter password to open the tracker", style="LoginText.TLabel").pack(anchor="w", pady=(7, 12))
        self.password = tk.StringVar()
        self.entry = ttk.Entry(frame, textvariable=self.password, show="*")
        self.entry.pack(fill="x")
        self.entry.bind("<Return>", lambda _e: self.unlock())

        contact = ttk.Frame(frame, style="Login.TFrame")
        contact.pack(fill="x", pady=(8, 0))
        ttk.Label(contact, text="Need the password? Discord:", style="LoginText.TLabel").pack(side="left")
        ttk.Label(contact, text="____cooper_____", style="LoginText.TLabel").pack(side="left", padx=(5, 8))
        ttk.Button(contact, text="Copy", width=7, command=self.copy_discord).pack(side="left")

        self.error = tk.StringVar()
        ttk.Label(frame, textvariable=self.error, style="LoginError.TLabel").pack(anchor="w", pady=(6, 0))
        row = ttk.Frame(frame, style="Login.TFrame")
        row.pack(fill="x", pady=(12, 0))
        ttk.Button(row, text="Exit", command=self.cancel, width=12, style="Login.TButton").pack(side="right", padx=(8, 0))
        ttk.Button(row, text="Unlock", command=self.unlock, width=12, style="Login.TButton").pack(side="right")

        self.protocol("WM_DELETE_WINDOW", self.cancel)
        self.update_idletasks()
        self.deiconify()
        self.lift()
        self.attributes("-topmost", True)
        self.after(250, lambda: self.attributes("-topmost", False))
        self.after(50, self.focus_entry)
        self.grab_set()

    def focus_entry(self):
        try:
            self.lift()
            self.focus_force()
            self.entry.focus_force()
        except tk.TclError:
            pass

    def copy_discord(self):
        try:
            self.clipboard_clear()
            self.clipboard_append("____cooper_____")
            self.error.set("Discord username copied.")
            self.after(1800, lambda: self.error.set(""))
        except tk.TclError:
            self.error.set("Could not copy Discord username.")

    def unlock(self):
        if self.password.get() == APP_PASSWORD:
            self.unlocked = True
            try:
                self.grab_release()
            except tk.TclError:
                pass
            self.destroy()
            return
        self.password.set("")
        self.error.set("Incorrect password.")
        self.focus_entry()

    def cancel(self):
        self.unlocked = False
        try:
            self.grab_release()
        except tk.TclError:
            pass
        self.destroy()


def run_app():
    try:
        login = PasswordWindow()
        login.mainloop()
        if not login.unlocked:
            return
        root = tk.Tk()
        App(root)
        root.mainloop()
    except Exception as exc:
        log_startup_error(exc)
        try:
            from tkinter import messagebox
            root = tk.Tk()
            root.withdraw()
            messagebox.showerror(APP_NAME, f"The program could not start.\n\n{type(exc).__name__}: {exc}\n\nLog: {STARTUP_LOG}")
            root.destroy()
        except Exception:
            pass


if __name__ == "__main__":
    run_app()
