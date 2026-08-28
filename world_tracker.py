import html, re, threading, time, urllib.request, statistics
from dataclasses import dataclass
from html.parser import HTMLParser
import tkinter as tk
from tkinter import ttk

SOURCE_URL = "https://oldschool.runescape.com/slu"
INTERVAL = 10
EVENT_WINDOW = 30          # allow the public world list to lag by a couple of polls
HISTORY_SIZE = 4
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
    left: int
    appeared: int
    moved: int
    confidence: int
    timestamp: float


class WorldParser(HTMLParser):
    """Read the actual game world number from the world-list link.

    Jagex's public HTML can display a short list number while the actual
    game-launch link contains world=301, world=302, etc. We prefer the
    href/query value, then fall back to the slu-world-XXX id.
    """
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
            return

        if not self.in_row:
            return

        if tag in ("td", "th"):
            self.in_cell = True
            self.cell_buf = []
            return

        if tag == "a":
            ident = attrs.get("id", "")
            href = attrs.get("href", "")

            # Most reliable: game?world=301
            match = re.search(r"(?:[?&]world=|slu-world-)(\d+)", href)
            if match:
                self.world_id = int(match.group(1))
                return

            match = re.fullmatch(r"slu-world-(\d+)", ident)
            if match:
                self.world_id = int(match.group(1))

    def handle_data(self, data):
        if self.in_cell:
            self.cell_buf.append(data)

    def handle_endtag(self, tag):
        tag = tag.lower()

        if tag in ("td", "th") and self.in_cell:
            text = re.sub(
                r"\s+",
                " ",
                html.unescape("".join(self.cell_buf))
            ).strip()
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
    req = urllib.request.Request(
        SOURCE_URL,
        headers={
            "User-Agent": "CakePowder-OSRS-World-Tracker/6.0"
        }
    )

    with urllib.request.urlopen(req, timeout=15) as response:
        raw = response.read().decode("utf-8", "replace")

    parser = WorldParser()
    parser.feed(raw)

    worlds = []

    for world_id, row in parser.rows:
        if len(row) < 5:
            continue

        players_match = re.search(
            r"([\d,]+)\s+players?",
            row[1],
            re.I
        )

        players = (
            int(players_match.group(1).replace(",", ""))
            if players_match else 0
        )

        membership = row[3]
        if membership not in ("Members", "Free"):
            continue

        worlds.append(
            World(
                world=world_id,
                players=players,
                location=row[2],
                membership=membership,
                activity=row[4] or "-",
            )
        )

    if not worlds:
        raise RuntimeError("No OSRS worlds found in the server list.")

    unique = {world.world: world for world in worlds}

    return sorted(
        unique.values(),
        key=lambda world: world.world
    )


def _deltas(old, new):
    """Return signed player changes for worlds present in both snapshots."""
    return {
        world: new[world].players - old[world].players
        for world in set(old) & set(new)
    }


def _event_candidates(history, current, min_group):
    """Build departure/arrival candidates using the newest snapshot plus
    one older snapshot.

    The important change from the old detector is that a group is not
    required to appear as a perfect +/-6 in one exact 10-second sample.
    The public world list can move in small increments or lag. We therefore
    also examine a 20-second window and keep the newest event timestamp.
    """
    if not history:
        return []

    newest = history[-1]
    newest_map = newest[1]

    candidates = []

    # Always inspect the normal 10-second delta.
    comparisons = [(history[-2] if len(history) >= 2 else None, newest)]

    # Also inspect up to 20 seconds. This catches a group whose population
    # change is split across two public-list updates.
    if len(history) >= 3:
        comparisons.append((history[-3], newest))

    for old_item, new_item in comparisons:
        if old_item is None:
            continue

        old_time, old_map = old_item
        new_time, new_map = new_item
        age = new_time - old_time

        if age > EVENT_WINDOW:
            continue

        deltas = _deltas(old_map, new_map)

        # Estimate ordinary world-list noise. A median is much less affected
        # by a handful of real group hops.
        abs_changes = [
            abs(v) for v in deltas.values()
            if abs(v) > 0
        ]
        noise = statistics.median(abs_changes) if abs_changes else 1.0

        drops = [
            (world, -delta)
            for world, delta in deltas.items()
            if delta <= -min_group
        ]
        gains = [
            (world, delta)
            for world, delta in deltas.items()
            if delta >= min_group
        ]

        if not drops or not gains:
            continue

        for source, left in drops:
            for destination, appeared in gains:
                moved = min(left, appeared)

                # Exact population matching is the strongest signal.
                ratio = min(left, appeared) / max(left, appeared)

                # How large is this movement compared with normal noise?
                signal = min(
                    1.0,
                    moved / max(float(min_group), noise * 2.0)
                )

                # If there are dozens of candidate movements, matching one
                # pair is less informative than when only a few exist.
                candidate_count = len(drops) + len(gains)
                uniqueness = 1.0 / (
                    1.0 + max(0, candidate_count - 2) * 0.18
                )

                # World-number proximity is deliberately only a small bonus.
                # PK groups can and do hop anywhere.
                distance = abs(source - destination)
                if distance == 1:
                    proximity = 1.0
                elif distance <= 3:
                    proximity = 0.75
                elif distance <= 10:
                    proximity = 0.45
                elif distance <= 25:
                    proximity = 0.20
                else:
                    proximity = 0.0

                # Longer comparison windows are useful for laggy public data,
                # but a direct 10-second match is slightly stronger.
                timing = 1.0 if age <= 11.5 else 0.65

                score = (
                    ratio * 48.0 +
                    signal * 22.0 +
                    uniqueness * 12.0 +
                    proximity * 8.0 +
                    timing * 10.0
                )

                confidence = min(99, round(score))

                candidates.append(
                    (
                        confidence,
                        source,
                        destination,
                        left,
                        appeared,
                        moved,
                        new_time
                    )
                )

    # Highest-confidence candidates first; one destination is only used once
    # per polling cycle.
    candidates.sort(reverse=True)

    results = []
    used_sources = set()
    used_destinations = set()

    # Avoid generating the same event twice from the 10s and 20s comparisons.
    recent_keys = set()

    for (
        confidence,
        source,
        destination,
        left,
        appeared,
        moved,
        timestamp
    ) in candidates:
        key = (source, destination, round(timestamp / INTERVAL))

        if key in recent_keys:
            continue

        if source in used_sources or destination in used_destinations:
            continue

        # Keep low-confidence observations out of the event stream. The UI's
        # minimum-confidence setting still controls what the user sees.
        if confidence < 50:
            continue

        results.append(
            Hop(
                source=source,
                destination=destination,
                left=left,
                appeared=appeared,
                moved=moved,
                confidence=confidence,
                timestamp=timestamp,
            )
        )

        used_sources.add(source)
        used_destinations.add(destination)
        recent_keys.add(key)

    return results


class GroupChain:
    def __init__(self, hop):
        self.hops = [hop]
        self.last_world = hop.destination
        self.last_time = hop.timestamp

    @property
    def size(self):
        recent = self.hops[-5:]
        return round(sum(h.moved for h in recent) / len(recent))

    @property
    def confidence(self):
        avg = (
            sum(h.confidence for h in self.hops)
            / len(self.hops)
        )

        values = [h.moved for h in self.hops[-5:]]
        mean = sum(values) / len(values)

        if mean:
            consistency = max(
                0,
                1 - max(
                    abs(v - mean) / mean
                    for v in values
                )
            )
        else:
            consistency = 0

        # Repeated hops are a strong signal. Three consistent hops should
        # become very hard to dismiss as random movement.
        repeat_bonus = min(
            18,
            (len(self.hops) - 1) * 6
        )

        return min(
            99,
            round(
                avg * 0.72 +
                consistency * 20 +
                repeat_bonus
            )
        )

    @property
    def route(self):
        return (
            [self.hops[0].source] +
            [h.destination for h in self.hops]
        )

    def can_extend(self, hop):
        if hop.source != self.last_world:
            return False

        if not self.size:
            return False

        # Allow modest real-world population noise. For a six-player group,
        # 4-8 is still a sensible continuation.
        tolerance = max(3, round(self.size * 0.45))

        if abs(hop.moved - self.size) > tolerance:
            return False

        return hop.timestamp - self.last_time <= 60

    def add(self, hop):
        self.hops.append(hop)
        self.last_world = hop.destination
        self.last_time = hop.timestamp


class App:
    def __init__(self, root):
        self.root = root
        root.title(APP_NAME)
        root.geometry("1250x760")
        root.minsize(1000, 650)

        self.worlds = []
        self.previous = None
        self.snapshot_history = []
        self.hops = []
        self.chains = []
        self.seen_events = {}

        self.f2p = tk.BooleanVar(value=False)
        self.min_group = tk.IntVar(value=5)
        self.min_conf = tk.IntVar(value=60)

        self.view = "hops"

        self.status = tk.StringVar(value="Starting…")
        self.detail = tk.StringVar(
            value="Waiting for first snapshot."
        )

        self.build_ui()

        root.protocol(
            "WM_DELETE_WINDOW",
            root.destroy
        )

        root.after(100, self.refresh)

    def build_ui(self):
        header = ttk.Frame(self.root, padding=16)
        header.pack(fill="x")

        ttk.Label(
            header,
            text="Cake Powder",
            font=("Segoe UI", 20, "bold")
        ).pack(side="left")

        ttk.Label(
            header,
            text="  /  FAITHFUL FEW",
            font=("Segoe UI", 10, "bold")
        ).pack(side="left")

        ttk.Label(
            header,
            textvariable=self.status
        ).pack(side="right")

        toolbar = ttk.Frame(
            self.root,
            padding=(16, 0, 16, 12)
        )
        toolbar.pack(fill="x")

        ttk.Button(
            toolbar,
            text="Worlds",
            command=lambda: self.set_view("worlds")
        ).pack(side="left", padx=3)

        ttk.Button(
            toolbar,
            text="Group Hops",
            command=lambda: self.set_view("hops")
        ).pack(side="left", padx=3)

        ttk.Button(
            toolbar,
            text="Active Groups",
            command=lambda: self.set_view("chains")
        ).pack(side="left", padx=3)

        ttk.Checkbutton(
            toolbar,
            text="Include F2P worlds",
            variable=self.f2p,
            command=self.reset_baseline
        ).pack(side="left", padx=20)

        ttk.Label(
            toolbar,
            text="10-second detection window • 20-second confirmation"
        ).pack(side="right")

        body = ttk.Frame(self.root)
        body.pack(
            fill="both",
            expand=True,
            padx=16,
            pady=4
        )

        settings = ttk.LabelFrame(
            body,
            text="Detection Settings",
            padding=14
        )
        settings.pack(
            side="left",
            fill="y",
            padx=(0, 12)
        )

        ttk.Label(
            settings,
            text="Minimum group size"
        ).pack(anchor="w", pady=4)

        ttk.Spinbox(
            settings,
            from_=1,
            to=500,
            textvariable=self.min_group,
            width=8
        ).pack(anchor="w")

        ttk.Label(
            settings,
            text="Minimum confidence"
        ).pack(anchor="w", pady=(14, 4))

        ttk.Spinbox(
            settings,
            from_=0,
            to=99,
            textvariable=self.min_conf,
            width=8
        ).pack(anchor="w")

        ttk.Button(
            settings,
            text="Clear history",
            command=self.clear_history
        ).pack(fill="x", pady=20)

        ttk.Label(
            settings,
            text=(
                "10 seconds: direct detection\n"
                "20 seconds: lag/partial-change check\n"
                "Repeated hops strengthen a group chain.\n"
                "World proximity is only a small bonus."
            ),
            justify="left"
        ).pack(
            anchor="w",
            side="bottom"
        )

        main = ttk.Frame(body)
        main.pack(
            side="left",
            fill="both",
            expand=True
        )

        self.view_title = ttk.Label(
            main,
            text="GROUP HOP DETECTIONS",
            font=("Segoe UI", 10, "bold")
        )
        self.view_title.pack(
            anchor="w",
            pady=(0, 8)
        )

        table_frame = ttk.Frame(main)
        table_frame.pack(
            fill="both",
            expand=True
        )

        self.tree = ttk.Treeview(
            table_frame,
            show="headings"
        )

        scrollbar = ttk.Scrollbar(
            table_frame,
            orient="vertical",
            command=self.tree.yview
        )

        self.tree.configure(
            yscrollcommand=scrollbar.set
        )

        self.tree.pack(
            side="left",
            fill="both",
            expand=True
        )

        scrollbar.pack(
            side="right",
            fill="y"
        )

        self.tree.bind(
            "<<TreeviewSelect>>",
            self.select_row
        )

        ttk.Label(
            main,
            textvariable=self.detail,
            wraplength=1000
        ).pack(
            anchor="w",
            pady=8
        )

        self.set_view("hops")

    def set_view(self, view):
        self.view = view

        titles = {
            "hops": "GROUP HOP DETECTIONS",
            "chains": "ACTIVE GROUP CHAINS",
            "worlds": "OSRS WORLD POPULATIONS",
        }

        columns = {
            "hops": (
                "from", "to", "left", "app",
                "moved", "conf", "time"
            ),
            "chains": (
                "route", "size", "hops", "conf", "time"
            ),
            "worlds": (
                "world", "players", "location",
                "type", "activity"
            ),
        }

        self.view_title.config(text=titles[view])
        self.tree["columns"] = columns[view]

        for column in columns[view]:
            self.tree.heading(
                column,
                text=column.upper()
            )
            self.tree.column(
                column,
                width=120,
                anchor="center"
            )

        if view == "worlds":
            self.tree.column("location", width=180)
            self.tree.column(
                "activity",
                width=260,
                anchor="w"
            )
        elif view == "chains":
            self.tree.column("route", width=280)

        self.draw()

    def visible_worlds(self):
        if self.f2p.get():
            return self.worlds

        return [
            w for w in self.worlds
            if w.membership == "Members"
        ]

    def reset_baseline(self):
        self.previous = None
        self.snapshot_history.clear()
        self.seen_events.clear()

        self.detail.set(
            "Baseline reset; waiting for the next 10-second snapshot."
        )

        self.draw()

    def refresh(self):
        threading.Thread(
            target=self.worker,
            daemon=True
        ).start()

    def worker(self):
        try:
            worlds = fetch_worlds()

            self.root.after(
                0,
                lambda: self.apply_worlds(worlds)
            )

        except Exception as exc:
            message = str(exc)

            self.root.after(
                0,
                lambda: self.fetch_failed(message)
            )

    def fetch_failed(self, message):
        self.status.set("Update failed; retrying…")
        self.detail.set(
            f"Could not update world data: {message}"
        )

        self.root.after(
            INTERVAL * 1000,
            self.refresh
        )

    def apply_worlds(self, worlds):
        self.worlds = worlds

        current = {
            w.world: w
            for w in self.visible_worlds()
        }

        now = time.time()

        self.snapshot_history.append((now, current))
        self.snapshot_history = self.snapshot_history[-HISTORY_SIZE:]

        if self.previous is None:
            self.previous = current
            self.detail.set(
                "Baseline captured. Waiting for the next 10-second snapshot."
            )
        else:
            detected = _event_candidates(
                self.snapshot_history,
                current,
                self.min_group.get()
            )

            added = 0

            for hop in detected:
                # De-duplicate events generated from overlapping windows.
                event_key = (
                    hop.source,
                    hop.destination,
                    round(hop.timestamp / INTERVAL)
                )

                if event_key in self.seen_events:
                    continue

                self.seen_events[event_key] = hop.timestamp

                if hop.confidence >= self.min_conf.get():
                    self.hops.insert(0, hop)
                    self.update_chain(hop)
                    added += 1

            # Keep the de-duplication table small.
            cutoff = now - 180
            self.seen_events = {
                k: v for k, v in self.seen_events.items()
                if v >= cutoff
            }

            self.previous = current

            if added:
                self.detail.set(
                    f"Detected {added} possible group hop"
                    f"{'s' if added != 1 else ''}."
                )

        self.status.set(
            f"Updated {time.strftime('%H:%M:%S')} • "
            f"{len(current)} worlds • "
            f"{'F2P + Members' if self.f2p.get() else 'Members only'}"
        )

        self.draw()

        self.root.after(
            INTERVAL * 1000,
            self.refresh
        )

    def update_chain(self, hop):
        candidates = [
            c for c in self.chains
            if c.can_extend(hop)
        ]

        if candidates:
            min(
                candidates,
                key=lambda c: abs(c.size - hop.moved)
            ).add(hop)
        else:
            self.chains.insert(
                0,
                GroupChain(hop)
            )
            self.chains = self.chains[:50]

    def draw(self):
        self.tree.delete(
            *self.tree.get_children()
        )

        if self.view == "hops":
            for hop in self.hops:
                if hop.confidence >= self.min_conf.get():
                    self.tree.insert(
                        "",
                        "end",
                        values=(
                            hop.source,
                            hop.destination,
                            hop.left,
                            hop.appeared,
                            hop.moved,
                            f"{hop.confidence}%",
                            time.strftime(
                                "%H:%M:%S",
                                time.localtime(hop.timestamp)
                            ),
                        )
                    )

        elif self.view == "chains":
            now = time.time()

            self.chains = [
                c for c in self.chains
                if now - c.last_time <= 60
            ]

            for index, chain in enumerate(self.chains):
                self.tree.insert(
                    "",
                    "end",
                    iid=f"c{index}",
                    values=(
                        " → ".join(map(str, chain.route)),
                        f"~{chain.size}",
                        len(chain.hops),
                        f"{chain.confidence}%",
                        time.strftime(
                            "%H:%M:%S",
                            time.localtime(chain.last_time)
                        )
                    )
                )

        else:
            for world in self.visible_worlds():
                self.tree.insert(
                    "",
                    "end",
                    values=(
                        world.world,
                        f"{world.players:,}",
                        world.location,
                        world.membership,
                        world.activity
                    )
                )

    def select_row(self, _event):
        selection = self.tree.selection()

        if not selection:
            return

        if self.view == "chains":
            chain = self.chains[int(selection[0][1:])]

            self.detail.set(
                f"Active group: ~{chain.size} players • "
                f"{len(chain.hops)} repeated hops • "
                f"{chain.confidence}% chain confidence • "
                f"Route: {' → '.join(map(str, chain.route))}"
            )

        elif self.view == "hops":
            values = self.tree.item(
                selection[0],
                "values"
            )

            self.detail.set(
                f"World {values[0]} → World {values[1]} • "
                f"{values[2]} left • {values[3]} appeared • "
                f"estimated {values[4]} moved • "
                f"{values[5]} same-group likelihood"
            )

    def clear_history(self):
        self.hops.clear()
        self.chains.clear()
        self.seen_events.clear()

        self.detail.set("History cleared.")
        self.draw()


if __name__ == "__main__":
    root = tk.Tk()
    app = App(root)
    root.mainloop()
