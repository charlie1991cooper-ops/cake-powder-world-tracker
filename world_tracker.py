import html
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
POLL_INTERVAL = 2
NORMAL_MATCH_WINDOW = 10
CONVERGENCE_WINDOW = 30
TEAM_HISTORY_SECONDS = 60 * 60
MAX_TRACKED_MOVEMENT = 400
EPISODE_MAX_SECONDS = 10
EPISODE_QUIET_SECONDS = 4
MAX_HISTORY_EVENTS = 500
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

    @property
    def direction(self):
        return 1 if self.amount > 0 else -1

    @property
    def key(self):
        return (
            self.world,
            self.amount,
            round(self.start_time, 1),
            round(self.end_time, 1),
        )


@dataclass(frozen=True)
class Hop:
    source: int
    destination: int
    left: int
    appeared: int
    moved: int
    score: int
    timestamp: float
    source_event: tuple
    destination_event: tuple

    @property
    def key(self):
        return (self.source, self.destination, self.source_event, self.destination_event)


@dataclass(frozen=True)
class Convergence:
    destination: int
    sources: tuple
    source_amounts: tuple
    appeared: int
    score: int
    timestamp: float
    destination_event: tuple
    source_events: tuple

    @property
    def source_count(self):
        return len(self.sources)

    @property
    def total_outflow(self):
        return sum(self.source_amounts)

    @property
    def key(self):
        return (self.destination, tuple(sorted(self.sources)), self.destination_event)


@dataclass(frozen=True)
class WorldAlert:
    world: int
    delta: int
    score: int
    timestamp: float
    context: str
    watched: bool = False


class WorldParser(HTMLParser):
    """Parse official world data and use id='slu-world-XXX' for the real ID."""

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
            match = re.fullmatch(r"slu-world-(\d+)", attrs.get("id", ""))
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
        headers={"User-Agent": "Cakes-OSRS-World-Tracker/8.0"},
    )
    with urllib.request.urlopen(request, timeout=15) as response:
        raw = response.read().decode("utf-8", "replace")

    parser = WorldParser()
    parser.feed(raw)
    worlds = []
    for world_id, row in parser.rows:
        if len(row) < 5:
            continue
        match = re.search(r"([\d,]+)\s+players?", row[1], re.I)
        if not match:
            continue
        membership = row[3].strip()
        if membership not in ("Members", "Free"):
            continue
        worlds.append(
            World(
                world=world_id,
                players=int(match.group(1).replace(",", "")),
                location=row[2].strip(),
                membership=membership,
                activity=row[4].strip() or "-",
            )
        )

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


def _new_episode(previous_population, step, now):
    return {
        "direction": 1 if step > 0 else -1,
        "anchor": previous_population,
        "start_time": now,
        "last_nonzero": now,
        "triggered": False,
    }


def build_movement_episodes(episodes, previous, current, now, min_group):
    """Build one short movement episode at a time, instead of one event per poll.

    An episode may accumulate over up to 10 seconds. Once the population is quiet
    for four seconds, a new episode may begin even in the same direction. This
    avoids both repeated alerts and permanently merging separate team movements.
    """
    emitted = []
    min_group = max(1, min(MAX_TRACKED_MOVEMENT, int(min_group)))

    for world in set(previous) & set(current):
        prev_population = previous[world].players
        current_population = current[world].players
        step = current_population - prev_population
        state = episodes.get(world)

        if state is not None and now - state["start_time"] > EPISODE_MAX_SECONDS:
            state = None
            episodes.pop(world, None)

        if step == 0:
            if state is not None and now - state["last_nonzero"] >= EPISODE_QUIET_SECONDS:
                episodes.pop(world, None)
            continue

        if state is None:
            state = _new_episode(prev_population, step, now)
            episodes[world] = state
        elif step * state["direction"] < 0:
            state = _new_episode(prev_population, step, now)
            episodes[world] = state
        else:
            state["last_nonzero"] = now

        net = current_population - state["anchor"]
        if abs(net) > MAX_TRACKED_MOVEMENT:
            episodes.pop(world, None)
            continue

        if not state["triggered"] and abs(net) >= min_group:
            state["triggered"] = True
            emitted.append(
                Change(
                    world=world,
                    amount=net,
                    start_time=state["start_time"],
                    end_time=now,
                )
            )

    return emitted


def score_hop(source, destination):
    age = abs(source.end_time - destination.end_time)
    if age > NORMAL_MATCH_WINDOW:
        return 0

    ratio = ratio_score(source.magnitude, destination.magnitude)
    timing = 1.0 - age / NORMAL_MATCH_WINDOW
    size = max(source.magnitude, destination.magnitude)

    score = 46 * ratio + 34 * timing + min(20, size / 25 * 20)
    if ratio < 0.50:
        score -= 22
    elif ratio < 0.65:
        score -= 12
    elif ratio < 0.80:
        score -= 5
    if size <= 6:
        score -= 4
    return min(99, max(0, round(score)))


def match_movement_events(events, now, min_group):
    """Globally match unique drop/gain events inside the rolling 10-second window."""
    cutoff = now - NORMAL_MATCH_WINDOW
    min_group = max(1, min(MAX_TRACKED_MOVEMENT, int(min_group)))
    drops = [e for e in events if e.end_time >= cutoff and e.amount <= -min_group]
    gains = [e for e in events if e.end_time >= cutoff and e.amount >= min_group]

    candidates = []
    for source in drops:
        for destination in gains:
            if source.world == destination.world:
                continue
            score = score_hop(source, destination)
            if score >= 45:
                candidates.append((score, source, destination))

    candidates.sort(
        key=lambda item: (
            item[0],
            min(item[1].magnitude, item[2].magnitude),
            -abs(item[1].end_time - item[2].end_time),
        ),
        reverse=True,
    )

    used_sources = set()
    used_destinations = set()
    results = []
    for score, source, destination in candidates:
        if source.key in used_sources or destination.key in used_destinations:
            continue
        results.append(
            Hop(
                source=source.world,
                destination=destination.world,
                left=source.magnitude,
                appeared=destination.magnitude,
                moved=max(source.magnitude, destination.magnitude),
                score=score,
                timestamp=max(source.end_time, destination.end_time),
                source_event=source.key,
                destination_event=destination.key,
            )
        )
        used_sources.add(source.key)
        used_destinations.add(destination.key)
    return results


def detect_convergences(events, now, min_group):
    """Detect 2+ source worlds feeding one destination inside 30 seconds."""
    cutoff = now - CONVERGENCE_WINDOW
    min_group = max(1, min(MAX_TRACKED_MOVEMENT, int(min_group)))
    recent = [e for e in events if e.end_time >= cutoff and e.magnitude <= MAX_TRACKED_MOVEMENT]
    drops = [e for e in recent if e.amount <= -min_group]
    gains = [e for e in recent if e.amount >= min_group]
    results = []

    for destination in gains:
        candidates = []
        for source in drops:
            if source.world == destination.world:
                continue
            age = abs(source.end_time - destination.end_time)
            if age > CONVERGENCE_WINDOW:
                continue
            ratio = ratio_score(source.magnitude, destination.magnitude)
            timing = 1 - age / CONVERGENCE_WINDOW
            quality = ratio * 0.60 + timing * 0.40
            candidates.append((quality, source, ratio, timing))

        best_by_world = {}
        for candidate in candidates:
            quality, source, ratio, timing = candidate
            old = best_by_world.get(source.world)
            if old is None or quality > old[0]:
                best_by_world[source.world] = candidate

        chosen = sorted(
            best_by_world.values(),
            key=lambda item: (item[0], item[1].magnitude),
            reverse=True,
        )[:6]
        if len(chosen) < 2:
            continue

        sizes = [item[1].magnitude for item in chosen]
        median = sorted(sizes)[len(sizes) // 2]
        consistency = 1 - sum(abs(size - median) for size in sizes) / max(1, sum(sizes))
        avg_ratio = sum(item[2] for item in chosen) / len(chosen)
        avg_timing = sum(item[3] for item in chosen) / len(chosen)
        coverage = destination.magnitude / max(1, sum(sizes))

        score = (
            25
            + 25 * avg_ratio
            + 20 * avg_timing
            + 12 * max(0.0, min(1.0, consistency))
            + min(15, (len(chosen) - 1) * 7)
            + min(7, coverage * 7)
        )
        if len(chosen) >= 3:
            score += 6
        return_score = min(99, max(0, round(score)))

        results.append(
            Convergence(
                destination=destination.world,
                sources=tuple(item[1].world for item in chosen),
                source_amounts=tuple(item[1].magnitude for item in chosen),
                appeared=destination.magnitude,
                score=return_score,
                timestamp=max([destination.end_time] + [item[1].end_time for item in chosen]),
                destination_event=destination.key,
                source_events=tuple(item[1].key for item in chosen),
            )
        )

    best = {}
    for result in results:
        existing = best.get(result.destination)
        if existing is None or result.score > existing.score:
            best[result.destination] = result
    return sorted(best.values(), key=lambda item: (item.score, item.source_count), reverse=True)


class TeamTrack:
    """Persistent inferred team state remembered for up to one hour."""

    def __init__(self, *, first_hop=None, convergence=None):
        self.route_worlds = []
        self.hops = []
        self.last_world = None
        self.last_time = 0.0
        self.approx_size = 0
        self.seed_confidence = 0
        self.seed_sources = ()

        if first_hop is not None:
            self.route_worlds = [first_hop.source, first_hop.destination]
            self.hops = [first_hop]
            self.last_world = first_hop.destination
            self.last_time = first_hop.timestamp
            self.approx_size = first_hop.moved
        elif convergence is not None:
            self.route_worlds = [convergence.destination]
            self.last_world = convergence.destination
            self.last_time = convergence.timestamp
            self.approx_size = convergence.appeared
            self.seed_confidence = convergence.score
            self.seed_sources = convergence.sources

    @property
    def age(self):
        return max(0.0, time.time() - self.last_time)

    @property
    def hop_count(self):
        return len(self.hops)

    @property
    def route(self):
        return list(self.route_worlds)

    @property
    def size(self):
        if not self.hops:
            return max(1, self.approx_size)
        values = [hop.moved for hop in self.hops[-8:]]
        if self.approx_size:
            values.append(self.approx_size)
        return max(1, round(sum(values) / len(values)))

    @property
    def score(self):
        if not self.hops:
            return min(99, max(50, self.seed_confidence))
        scores = [hop.score for hop in self.hops[-8:]]
        base = sum(scores) / len(scores)
        values = [hop.moved for hop in self.hops[-8:]]
        consistency = 1.0
        if len(values) > 1:
            mean = sum(values) / len(values)
            deviation = sum(abs(value - mean) for value in values) / len(values)
            consistency = max(0.0, 1.0 - deviation / max(1, mean))
        repeat_bonus = min(28, max(0, len(self.hops) - 1) * 8)
        seed_bonus = min(15, max(0, self.seed_confidence - 70) // 2) if self.seed_confidence else 0
        return min(99, max(0, round(base * 0.70 + consistency * 14 + repeat_bonus + seed_bonus)))

    def is_alive(self):
        return self.last_time > 0 and time.time() - self.last_time <= TEAM_HISTORY_SECONDS

    def can_extend(self, hop):
        if not self.is_alive() or hop.source != self.last_world:
            return False
        expected = max(1, self.size)
        ratio = hop.moved / expected
        return 0.35 <= ratio <= 1.80

    def add(self, hop):
        self.hops.append(hop)
        self.last_world = hop.destination
        self.last_time = hop.timestamp
        self.approx_size = hop.moved
        if not self.route_worlds:
            self.route_worlds = [hop.source]
        if self.route_worlds[-1] != hop.destination:
            self.route_worlds.append(hop.destination)

    def reinforce(self, convergence):
        if convergence.destination != self.last_world:
            return False
        self.approx_size = round((self.size + convergence.appeared) / 2)
        self.seed_confidence = max(self.seed_confidence, convergence.score)
        self.seed_sources = convergence.sources
        self.last_time = max(self.last_time, convergence.timestamp)
        return True


class App:
    def __init__(self, root):
        self.root = root
        root.title(APP_NAME)
        root.geometry("1250x760")
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
        self.recent_events = deque(maxlen=MAX_HISTORY_EVENTS)
        self.movement_history = deque(maxlen=MAX_HISTORY_EVENTS)
        self.hops = deque(maxlen=250)
        self.convergences = deque(maxlen=150)
        self.teams = []
        self.alerts = deque(maxlen=500)
        self.delta_noise = defaultdict(lambda: deque(maxlen=24))
        self.reported_hops = {}
        self.reported_convergences = {}
        self.fetch_in_progress = False
        self.fetch_failures = 0
        self.last_fetch_started = 0.0

        self.f2p = tk.BooleanVar(value=False)
        self.min_group = tk.IntVar(value=10)
        self.min_conf = tk.IntVar(value=50)
        self.world_alert_threshold = tk.IntVar(value=10)
        self.watch_enabled = tk.BooleanVar(value=False)
        self.watch_world = tk.StringVar(value="")
        self.watch_threshold = tk.IntVar(value=10)
        self.sound_alerts = tk.BooleanVar(value=False)

        self.view = "teams"
        self.status = tk.StringVar(value="Starting…")
        self.detail = tk.StringVar(value="Waiting for the first world snapshot.")
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
        header = ttk.Frame(self.root, padding=(18, 14, 18, 7))
        header.pack(fill="x")
        ttk.Label(header, text=APP_NAME, style="Header.TLabel").pack(side="left")
        ttk.Label(header, textvariable=self.status, style="Status.TLabel").pack(side="right")

        controls = ttk.Frame(self.root, padding=(18, 0, 18, 9))
        controls.pack(fill="x")
        ttk.Label(controls, text="Group ≥").pack(side="left")
        ttk.Spinbox(controls, from_=1, to=400, textvariable=self.min_group, width=5).pack(side="left", padx=(5, 12))
        ttk.Label(controls, text="Show").pack(side="left")
        self.conf_combo = ttk.Combobox(controls, values=("Possible", "Likely", "Very likely"), state="readonly", width=11)
        self.conf_combo.current(0)
        self.conf_combo.bind("<<ComboboxSelected>>", self.conf_changed)
        self.conf_combo.pack(side="left", padx=(5, 12))
        ttk.Checkbutton(controls, text="F2P", variable=self.f2p, command=self.reset_baseline).pack(side="left", padx=(0, 12))
        ttk.Checkbutton(controls, text="Sound", variable=self.sound_alerts).pack(side="left")
        ttk.Button(controls, text="Settings", command=self.open_settings).pack(side="right", padx=(7, 0))
        ttk.Button(controls, text="Refresh", style="Accent.TButton", command=self.refresh).pack(side="right")

        body = ttk.Frame(self.root, padding=(18, 0, 18, 14))
        body.pack(fill="both", expand=True)

        nav = ttk.Frame(body)
        nav.pack(fill="x", pady=(0, 7))
        for text, view in (
            ("Active Teams", "teams"),
            ("Mass Hops", "hops"),
            ("Convergences", "convergences"),
            ("Worlds", "worlds"),
            ("World Alerts", "alerts"),
        ):
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
        self.tree.tag_configure("very", foreground="#39e58c")
        self.tree.tag_configure("likely", foreground="#e8d44d")
        self.tree.tag_configure("possible", foreground="#ff9d32")
        self.tree.tag_configure("unlikely", foreground="#ff5964")
        self.tree.tag_configure("watch", background="#241a3d")

        ttk.Label(body, textvariable=self.detail, wraplength=1120, foreground="#8d9ab0").pack(anchor="w", pady=(8, 0))
        self.set_view("teams")

    def open_settings(self):
        win = tk.Toplevel(self.root)
        win.title("Tracker Settings")
        win.geometry("390x455")
        win.resizable(False, False)
        win.configure(bg="#0b1018")
        win.transient(self.root)

        frame = ttk.Frame(win, padding=18)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text="TRACKER SETTINGS", style="Title.TLabel").pack(anchor="w")
        ttk.Label(frame, text="Detection thresholds are independent so the views remain consistent.", foreground="#8d9ab0", wraplength=340).pack(anchor="w", pady=(4, 14))

        ttk.Label(frame, text="Minimum group size").pack(anchor="w")
        ttk.Spinbox(frame, from_=1, to=400, textvariable=self.min_group, width=8).pack(anchor="w", pady=(3, 12))

        ttk.Label(frame, text="Minimum likelihood shown").pack(anchor="w")
        combo = ttk.Combobox(frame, values=("Possible", "Likely", "Very likely"), state="readonly", width=14)
        combo.set("Possible" if self.min_conf.get() == 50 else "Likely" if self.min_conf.get() == 75 else "Very likely")
        combo.bind("<<ComboboxSelected>>", lambda _e: self.conf_changed_from(combo))
        combo.pack(anchor="w", pady=(3, 12))

        ttk.Label(frame, text="World Alerts threshold").pack(anchor="w")
        ttk.Spinbox(frame, from_=1, to=400, textvariable=self.world_alert_threshold, width=8).pack(anchor="w", pady=(3, 12))
        ttk.Label(frame, text="Any qualifying movement appears here, including movements that were successfully matched to a hop.", foreground="#8d9ab0", wraplength=340).pack(anchor="w", pady=(0, 12))

        ttk.Checkbutton(frame, text="Watch a specific world", variable=self.watch_enabled, command=self.redraw).pack(anchor="w", pady=(2, 5))
        ttk.Label(frame, text="World").pack(anchor="w")
        self.settings_watch_combo = ttk.Combobox(frame, textvariable=self.watch_world, state="normal", width=12)
        self.settings_watch_combo.pack(anchor="w", pady=(3, 5))
        ttk.Label(frame, text="Watched-world threshold").pack(anchor="w")
        ttk.Spinbox(frame, from_=1, to=400, textvariable=self.watch_threshold, width=8).pack(anchor="w", pady=(3, 15))

        ttk.Button(frame, text="Clear history", command=lambda: (self.clear_history(), win.destroy())).pack(anchor="w", pady=(4, 8))
        ttk.Button(frame, text="Close", command=win.destroy).pack(anchor="e")
        self.update_watch_list()

    def conf_changed_from(self, combo):
        self.min_conf.set(50 if combo.get() == "Possible" else 75 if combo.get() == "Likely" else 90)
        self.redraw()

    def conf_changed(self, _event=None):
        self.min_conf.set(50 if self.conf_combo.get() == "Possible" else 75 if self.conf_combo.get() == "Likely" else 90)
        self.redraw()

    def visible_worlds(self):
        if self.f2p.get():
            return list(self.worlds)
        return [world for world in self.worlds if world.membership == "Members"]

    def update_watch_list(self):
        if not hasattr(self, "settings_watch_combo"):
            return
        self.settings_watch_combo["values"] = [str(world.world) for world in self.visible_worlds()]

    def reset_baseline(self):
        self.previous = None
        self.movement_episodes.clear()
        self.recent_events.clear()
        self.movement_history.clear()
        self.reported_hops.clear()
        self.reported_convergences.clear()
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
            "teams": ("status", "route", "group", "hops", "confidence", "last"),
            "hops": ("from", "to", "left", "appeared", "group", "confidence", "time"),
            "convergences": ("sources", "to", "appeared", "outflow", "confidence", "time"),
            "worlds": ("world", "players", "type", "location", "activity"),
            "alerts": ("world", "direction", "change", "context", "confidence", "time"),
        }
        names = {
            "status": "STATUS", "route": "ROUTE", "group": "GROUP", "hops": "HOPS", "confidence": "CONFIDENCE", "last": "LAST",
            "from": "FROM", "to": "TO", "left": "LEFT", "appeared": "APPEARED", "time": "TIME",
            "sources": "SOURCE WORLDS", "outflow": "SOURCE OUTFLOW", "world": "WORLD", "players": "PLAYERS", "type": "TYPE", "location": "LOCATION", "activity": "ACTIVITY",
            "direction": "DIRECTION", "change": "CHANGE", "context": "CONTEXT",
        }
        self.view_title.config(text=titles[view])
        self.tree["columns"] = columns[view]
        for col in columns[view]:
            self.tree.heading(col, text=names[col])
            self.tree.column(col, width=120, anchor="center")

        widths = {
            "teams": (("status", 80), ("route", 480), ("group", 90), ("hops", 70), ("confidence", 125), ("last", 90)),
            "hops": (("from", 75), ("to", 75), ("left", 90), ("appeared", 100), ("group", 100), ("confidence", 125), ("time", 90)),
            "convergences": (("sources", 390), ("to", 75), ("appeared", 105), ("outflow", 130), ("confidence", 125), ("time", 90)),
            "worlds": (("world", 80), ("players", 100), ("type", 90), ("location", 170), ("activity", 450)),
            "alerts": (("world", 75), ("direction", 95), ("change", 85), ("context", 290), ("confidence", 125), ("time", 90)),
        }
        for col, width in widths[view]:
            self.tree.column(col, width=width)

        explanations = {
            "teams": "Persistent inferred teams. A team can return to a previous world and is remembered for one hour after its latest evidence.",
            "hops": "Matched population movements. Source and destination events are drawn from the same underlying movement records used by World Alerts.",
            "convergences": "Two or more source worlds lose group-sized populations around the same time while one destination gains players.",
            "worlds": "Current official OSRS world population snapshot.",
            "alerts": "Every qualifying world movement appears here, whether or not it was matched to a hop or convergence.",
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
        self.fetch_failures = min(5, self.fetch_failures + 1)
        self.status.set("Update failed; retrying…")
        self.detail.set(f"Could not update world data: {message}")
        self.root.after(int(min(20, 2 ** self.fetch_failures) * 1000), self.refresh)

    def apply_worlds(self, worlds):
        self.fetch_in_progress = False
        self.fetch_failures = 0
        self.worlds = worlds
        current = {world.world: world for world in self.visible_worlds()}
        now = time.time()

        self.min_group.set(max(1, min(MAX_TRACKED_MOVEMENT, self.min_group.get())))
        self.world_alert_threshold.set(max(1, min(MAX_TRACKED_MOVEMENT, self.world_alert_threshold.get())))
        self.watch_threshold.set(max(1, min(MAX_TRACKED_MOVEMENT, self.watch_threshold.get())))

        if self.previous is None:
            self.previous = current
            self.update_watch_list()
            self.status.set(f"Baseline captured • {len(current)} worlds")
        else:
            min_group = self.min_group.get()
            new_events = build_movement_episodes(self.movement_episodes, self.previous, current, now, min_group)
            for event in new_events:
                self.recent_events.append(event)
                self.movement_history.append(event)

            self.recent_events = self.prune_events(self.recent_events, now, NORMAL_MATCH_WINDOW)
            self.movement_history = self.prune_events(self.movement_history, now, CONVERGENCE_WINDOW)

            hops = match_movement_events(self.recent_events, now, min_group)
            hops = [hop for hop in hops if not self.hop_recently_reported(hop, now)]
            convergences = detect_convergences(self.movement_history, now, min_group)
            convergences = [item for item in convergences if not self.convergence_recently_reported(item, now)]

            matched_event_keys = set()
            convergence_event_keys = set()
            hop_event_keys = set()

            for hop in hops:
                hop_event_keys.add(hop.source_event)
                hop_event_keys.add(hop.destination_event)
                matched_event_keys.add(hop.source_event)
                matched_event_keys.add(hop.destination_event)
                if hop.score >= self.min_conf.get():
                    self.hops.appendleft(hop)
                    self.attach_hop_to_team(hop)

            for convergence in convergences:
                convergence_event_keys.add(convergence.destination_event)
                convergence_event_keys.update(convergence.source_events)
                if convergence.score >= self.min_conf.get():
                    self.convergences.appendleft(convergence)
                    self.attach_convergence_to_team(convergence)
                    self.raise_banner(
                        f"{convergence.source_count} WORLDS → {convergence.destination} • +{convergence.appeared} • {likelihood_label(convergence.score)}"
                    )

            # Authoritative alert feed: every qualifying movement is represented,
            # even when it also participates in a successful hop/convergence.
            for event in new_events:
                context_parts = []
                if event.key in hop_event_keys:
                    context_parts.append("MASS HOP")
                if event.key in convergence_event_keys:
                    context_parts.append("CONVERGENCE")
                watched = self.is_watched(event.world) and event.magnitude >= self.watch_threshold.get()
                if watched:
                    context_parts.append("WATCHED WORLD")
                if not context_parts:
                    context_parts.append("UNMATCHED MOVEMENT")

                if event.magnitude >= self.world_alert_threshold.get() or watched:
                    alert = self.make_world_alert(event, now, " + ".join(context_parts), watched)
                    self.alerts.appendleft(alert)
                    if watched or not context_parts == ["UNMATCHED MOVEMENT"]:
                        self.raise_banner(
                            f"World {event.world} {'INFLUX' if event.amount > 0 else 'OUTFLOW'} {event.magnitude} • {' + '.join(context_parts)}"
                        )

            # Learn only from immediate polling noise, not from multi-second movement episodes.
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
        delay_ms = max(700, int(POLL_INTERVAL * 1000 - elapsed * 1000))
        self.root.after(delay_ms, self.refresh)

    @staticmethod
    def prune_events(events, now, window_seconds):
        cutoff = now - window_seconds
        return deque((event for event in events if event.end_time >= cutoff), maxlen=MAX_HISTORY_EVENTS)

    def hop_recently_reported(self, hop, now):
        key = (hop.source, hop.destination, hop.source_event, hop.destination_event)
        cutoff = now - NORMAL_MATCH_WINDOW
        for stored_key, stamp in list(self.reported_hops.items()):
            if stamp < cutoff:
                self.reported_hops.pop(stored_key, None)
        if key in self.reported_hops:
            return True
        self.reported_hops[key] = now
        return False

    def convergence_recently_reported(self, convergence, now):
        cutoff = now - CONVERGENCE_WINDOW
        for stored_key, stamp in list(self.reported_convergences.items()):
            if stamp < cutoff:
                self.reported_convergences.pop(stored_key, None)
        key = (convergence.destination, tuple(sorted(convergence.sources)), convergence.destination_event)
        if key in self.reported_convergences:
            return True
        self.reported_convergences[key] = now
        return False

    def attach_hop_to_team(self, hop):
        self.teams = [team for team in self.teams if team.is_alive()]
        candidates = [team for team in self.teams if team.can_extend(hop)]
        if candidates:
            candidates.sort(key=lambda team: (abs(team.size - hop.moved), team.age))
            candidates[0].add(hop)
        else:
            self.teams.insert(0, TeamTrack(first_hop=hop))
            self.teams = self.teams[:100]

    def attach_convergence_to_team(self, convergence):
        self.teams = [team for team in self.teams if team.is_alive()]
        candidates = []
        for team in self.teams:
            if team.last_world != convergence.destination:
                continue
            ratio = convergence.appeared / max(1, team.size)
            if 0.35 <= ratio <= 1.80:
                candidates.append((abs(team.size - convergence.appeared), team.age, team))
        if candidates:
            candidates.sort(key=lambda item: (item[0], item[1]))
            candidates[0][2].reinforce(convergence)
        else:
            self.teams.insert(0, TeamTrack(convergence=convergence))
            self.teams = self.teams[:100]

    def expire_teams(self):
        self.teams = [team for team in self.teams if team.is_alive()]

    def is_watched(self, world):
        if not self.watch_enabled.get():
            return False
        try:
            return int(self.watch_world.get()) == world
        except (ValueError, TypeError):
            return False

    def make_world_alert(self, event, now, context, watched):
        threshold = max(1, self.world_alert_threshold.get())
        noise = self.delta_noise[event.world]
        baseline = sum(noise) / len(noise) if noise else 0.0
        scale = max(float(threshold), baseline * 2.5, 3.0)
        score = 45 + (event.magnitude / scale) * 10
        if event.magnitude >= threshold * 2:
            score += 10
        if event.magnitude >= threshold * 3:
            score += 8
        return WorldAlert(
            world=event.world,
            delta=event.amount,
            score=min(99, max(0, round(score))),
            timestamp=now,
            context=context,
            watched=watched,
        )

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
            visible = [team for team in self.teams if team.score >= self.min_conf.get()]
            if idx < len(visible):
                team = visible[idx]
                seed = ""
                if team.seed_sources:
                    seed = f" • convergence sources {', '.join(map(str, team.seed_sources))}"
                self.detail.set(
                    f"TEAM • ~{team.size} players • {likelihood_label(team.score)} • last seen {int(team.age)}s ago • route {' → '.join(map(str, team.route))}{seed}"
                )
        elif self.view == "hops":
            self.detail.set(
                f"World {values[0]} → {values[1]} • {values[2]} left • {values[3]} appeared • estimated group {values[4]} • {values[5]}"
            )
        elif self.view == "convergences":
            self.detail.set(
                f"{values[0]} → World {values[1]} • destination gain {values[2]} • total source outflow {values[3]} • {values[4]}"
            )
        elif self.view == "worlds":
            self.detail.set(f"World {values[0]} • {values[1]} players • {values[2]} • {values[3]} • {values[4]}")
        elif self.view == "alerts":
            self.detail.set(
                f"World {values[0]} • {values[1].lower()} {abs(int(values[2]))} players • {values[3]} • {values[4]}"
            )

    def redraw(self):
        self.tree.delete(*self.tree.get_children())
        min_conf = self.min_conf.get()

        if self.view == "teams":
            self.teams = [team for team in self.teams if team.is_alive()]
            visible = []
            for team in self.teams:
                if team.score < min_conf:
                    continue
                visible.append(team)
                self.tree.insert(
                    "", "end", iid=f"t{len(visible) - 1}",
                    values=("ACTIVE", " → ".join(map(str, team.route)), f"~{team.size}", team.hop_count, likelihood_label(team.score), f"{int(team.age)}s"),
                    tags=(likelihood_tag(team.score),),
                )
        elif self.view == "hops":
            for hop in self.hops:
                if hop.score < min_conf:
                    continue
                self.tree.insert(
                    "", "end",
                    values=(hop.source, hop.destination, hop.left, hop.appeared, hop.moved, likelihood_label(hop.score), time.strftime("%H:%M:%S", time.localtime(hop.timestamp))),
                    tags=(likelihood_tag(hop.score),),
                )
        elif self.view == "convergences":
            for convergence in self.convergences:
                if convergence.score < min_conf:
                    continue
                sources = " + ".join(
                    f"{world} (-{amount})"
                    for world, amount in zip(convergence.sources, convergence.source_amounts)
                )
                self.tree.insert(
                    "", "end",
                    values=(sources, convergence.destination, f"+{convergence.appeared}", f"~{convergence.total_outflow}", likelihood_label(convergence.score), time.strftime("%H:%M:%S", time.localtime(convergence.timestamp))),
                    tags=(likelihood_tag(convergence.score),),
                )
        elif self.view == "worlds":
            try:
                watched = int(self.watch_world.get()) if self.watch_enabled.get() else None
            except ValueError:
                watched = None
            for world in self.visible_worlds():
                tags = ("watch",) if world.world == watched else ()
                self.tree.insert(
                    "", "end",
                    values=(world.world, f"{world.players:,}", world.membership, world.location, world.activity),
                    tags=tags,
                )
        else:
            # Alerts intentionally do not use min_conf as a visibility filter.
            # The alert threshold means “show movements >= X”, while confidence
            # is explanatory rather than a gate. This fixes the old cross-view mismatch.
            for alert in self.alerts:
                direction = "INFLUX" if alert.delta > 0 else "OUTFLOW"
                tags = [likelihood_tag(alert.score)]
                if alert.watched:
                    tags.append("watch")
                self.tree.insert(
                    "", "end",
                    values=(alert.world, direction, f"{alert.delta:+d}", alert.context, likelihood_label(alert.score), time.strftime("%H:%M:%S", time.localtime(alert.timestamp))),
                    tags=tuple(tags),
                )

    def clear_history(self):
        self.previous = None
        self.movement_episodes.clear()
        self.recent_events.clear()
        self.movement_history.clear()
        self.hops.clear()
        self.convergences.clear()
        self.teams.clear()
        self.alerts.clear()
        self.delta_noise.clear()
        self.reported_hops.clear()
        self.reported_convergences.clear()
        self.alert_banner.set("No alerts yet")
        self.detail.set("History cleared. Waiting for the next snapshot.")
        self.redraw()

    def close(self):
        self.root.destroy()


def log_startup_error(exc):
    try:
        with STARTUP_LOG.open("a", encoding="utf-8") as stream:
            stream.write(
                f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {type(exc).__name__}: {exc}\n"
            )
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
            messagebox.showerror(
                APP_NAME,
                f"The program could not start.\n\n{type(exc).__name__}: {exc}\n\nLog: {STARTUP_LOG}",
            )
            root.destroy()
        except Exception:
            pass


if __name__ == "__main__":
    run_app()
