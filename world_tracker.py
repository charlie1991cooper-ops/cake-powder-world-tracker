import html, re, threading, time, urllib.request
from dataclasses import dataclass
from html.parser import HTMLParser
import tkinter as tk
from tkinter import ttk

SOURCE_URL = "https://oldschool.runescape.com/slu"
INTERVAL = 10
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
    """Parse world rows and read the real world from slu-world-XXX."""
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
            "User-Agent": "CakePowder-OSRS-World-Tracker/5.0"
        },
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
        raise RuntimeError(
            "No OSRS worlds found in the server list."
        )

    unique = {world.world: world for world in worlds}

    return sorted(
        unique.values(),
        key=lambda world: world.world
    )


def detect_hops(prev, cur, min_group):
    drops = []
    gains = []

    for world in set(prev) & set(cur):
        delta = cur[world].players - prev[world].players

        if delta <= -min_group:
            drops.append((world, -delta))
        elif delta >= min_group:
            gains.append((world, delta))

    if not drops or not gains:
        return []

    total_left = sum(x[1] for x in drops)
    total_appeared = sum(x[1] for x in gains)

    results = []
    used_destinations = set()

    for source, left in sorted(
        drops,
        key=lambda x: x[1],
        reverse=True
    ):
        best = None

        for destination, appeared in gains:
            if destination in used_destinations:
                continue

            ratio = min(left, appeared) / max(left, appeared)
            share = (
                left / total_left +
                appeared / total_appeared
            ) / 2

            score = 55 * ratio + 45 * share

            # Small proximity bonus only.
            # PK groups can hop to any world.
            distance = abs(source - destination)

            if distance == 1:
                score += 6
            elif distance <= 3:
                score += 4
            elif distance <= 10:
                score += 2

            if best is None or score > best[0]:
                best = (score, destination, appeared)

        if best:
            score, destination, appeared = best
            confidence = min(99, round(score))

            if confidence >= 50:
                results.append(
                    Hop(
                        source=source,
                        destination=destination,
                        left=left,
                        appeared=appeared,
                        moved=min(left, appeared),
                        confidence=confidence,
                        timestamp=time.time(),
                    )
                )

                used_destinations.add(destination)

    return results


class GroupChain:
    """Persistent inferred group identity.

    A chain can contain any number of hops. It remains alive for one hour
    after the most recent matching hop. Revisiting an earlier world is fine:
    world numbers are route history, not unique identifiers.
    """
    MAX_AGE = 60 * 60
    SIZE_TOLERANCE = 0.45

    def __init__(self, hop):
        self.hops = [hop]
        self.last_world = hop.destination
        self.last_time = hop.timestamp
        self.created_time = hop.timestamp

    @property
    def age(self):
        return time.time() - self.last_time

    @property
    def size(self):
        recent = self.hops[-8:]
        return max(1, round(sum(h.moved for h in recent) / len(recent)))

    @property
    def hop_count(self):
        return len(self.hops)

    @property
    def route(self):
        return [self.hops[0].source] + [h.destination for h in self.hops]

    @property
    def confidence(self):
        recent = self.hops[-8:]
        avg = sum(h.confidence for h in recent) / len(recent)

        values = [h.moved for h in recent]
        mean = sum(values) / len(values)

        if mean:
            deviation = sum(abs(v - mean) for v in values) / len(values)
            consistency = max(0.0, 1.0 - deviation / mean)
        else:
            consistency = 0.0

        # Repetition is powerful evidence, but confidence asymptotically
        # approaches 99 rather than claiming certainty.
        repeat_bonus = min(30, max(0, self.hop_count - 1) * 7)

        # Three or more similarly-sized hops receive a strong consistency bonus.
        consistency_bonus = round(consistency * 15)

        score = avg * 0.58 + consistency_bonus + repeat_bonus

        # A 3-hop chain is already very strong; 5+ is exceptionally strong.
        if self.hop_count >= 3 and consistency >= 0.75:
            score += 8
        if self.hop_count >= 5 and consistency >= 0.80:
            score += 7

        return min(99, max(0, round(score)))

    def is_alive(self):
        return self.age <= self.MAX_AGE

    def can_extend(self, hop):
        if not self.is_alive():
            return False

        # A group must currently be detected leaving the world it was last
        # seen in. It may return to any world it has visited before.
        if hop.source != self.last_world:
            return False

        estimated = self.size
        if estimated <= 0:
            return False

        difference = abs(hop.moved - estimated) / estimated
        return difference <= self.SIZE_TOLERANCE

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
        self.hops = []
        self.chains = []
        self.max_chain_history = 100

        self.f2p = tk.BooleanVar(value=False)
        self.min_group = tk.IntVar(value=10)
        self.min_conf = tk.IntVar(value=75)

        self.view = "hops"

        self.status = tk.StringVar(
            value="Starting…"
        )

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
        header = ttk.Frame(
            self.root,
            padding=16
        )
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
            text="10-second detection window"
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
                "Groups are remembered for\n"
                "1 hour after their last matching\n"
                "hop; route history is unlimited."
            ),
            justify="left"
        ).pack(
            anchor="w",
            side="bottom"
        )

        ttk.Label(
            settings,
            text=(
                "More consistent hops = higher\n"
                "same-group likelihood. Groups may\n"
                "return to worlds already visited."
            ),
            justify="left"
        ).pack(anchor="w", pady=(18, 0))

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
            "chains": "ACTIVE GROUPS / 1 HOUR MEMORY",
            "worlds": "OSRS WORLD POPULATIONS",
        }

        columns = {
            "hops": (
                "from",
                "to",
                "left",
                "app",
                "moved",
                "conf",
                "time"
            ),
            "chains": (
                "status",
                "route",
                "size",
                "hops",
                "conf",
                "time"
            ),
            "worlds": (
                "world",
                "players",
                "location",
                "type",
                "activity"
            ),
        }

        self.view_title.config(
            text=titles[view]
        )

        self.detail.set({
            "worlds": "Showing the current OSRS world population snapshot.",
            "hops": "Showing individual 10-second population changes that look like group hops.",
            "chains": "Showing persistent group identities remembered for up to 1 hour after their latest matching hop.",
        }[view])

        self.tree["columns"] = columns[view]

        heading_names = {
            "from": "FROM WORLD",
            "to": "TO WORLD",
            "left": "PLAYERS LEFT",
            "app": "PLAYERS APPEARED",
            "moved": "PLAYERS MOVED",
            "conf": "LIKELIHOOD",
            "time": "LAST DETECTED",
            "status": "STATUS",
            "route": "GROUP ROUTE",
            "size": "GROUP SIZE",
            "hops": "HOPS",
            "world": "WORLD",
            "players": "PLAYERS",
            "location": "LOCATION",
            "type": "TYPE",
            "activity": "ACTIVITY",
        }

        for column in columns[view]:
            self.tree.heading(
                column,
                text=heading_names.get(column, column.upper())
            )

            self.tree.column(
                column,
                width=120,
                anchor="center"
            )

        if view == "worlds":
            self.tree.column("world", width=90, anchor="center")
            self.tree.column("players", width=110, anchor="center")
            self.tree.column("location", width=180, anchor="center")
            self.tree.column("type", width=110, anchor="center")
            self.tree.column("activity", width=300, anchor="w")

        elif view == "hops":
            self.tree.column("from", width=105, anchor="center")
            self.tree.column("to", width=105, anchor="center")
            self.tree.column("left", width=125, anchor="center")
            self.tree.column("app", width=145, anchor="center")
            self.tree.column("moved", width=125, anchor="center")
            self.tree.column("conf", width=120, anchor="center")
            self.tree.column("time", width=125, anchor="center")

        elif view == "chains":
            self.tree.column("status", width=90, anchor="center")
            self.tree.column("route", width=360, anchor="w")
            self.tree.column("size", width=110, anchor="center")
            self.tree.column("hops", width=90, anchor="center")
            self.tree.column("conf", width=120, anchor="center")
            self.tree.column("time", width=100, anchor="center")

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
        self.status.set(
            "Update failed; retrying…"
        )

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

        if self.previous is None:
            self.previous = current

            self.detail.set(
                "Baseline captured. Waiting for the next 10-second snapshot."
            )

        else:
            for hop in detect_hops(
                self.previous,
                current,
                self.min_group.get()
            ):
                if hop.confidence >= self.min_conf.get():
                    self.hops.insert(0, hop)
                    self.update_chain(hop)

            self.previous = current

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
        """Attach a hop to an existing group where possible.

        Priority:
        1. Existing chain whose current world is the hop source.
        2. Closest estimated group size.
        3. Otherwise create a new group identity.

        This prevents two unrelated ~6-player groups from being merged just
        because their sizes happen to match.
        """
        # Remove expired chains first.
        self.chains = [
            chain for chain in self.chains
            if chain.is_alive()
        ]

        candidates = [
            chain for chain in self.chains
            if chain.can_extend(hop)
        ]

        if candidates:
            # Prefer the most recently active chain when size evidence is
            # comparable; this avoids accidentally joining stale identities.
            candidates.sort(
                key=lambda chain: (
                    abs(chain.size - hop.moved),
                    chain.age
                )
            )
            candidates[0].add(hop)
        else:
            self.chains.insert(0, GroupChain(hop))

        # Keep historical identities available while they're alive.
        self.chains = self.chains[:self.max_chain_history]


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
                                time.localtime(
                                    hop.timestamp
                                )
                            ),
                        )
                    )

        elif self.view == "chains":

            # Chains are intentionally retained for one hour after their
            # latest matching hop.
            self.chains = [
                c for c in self.chains
                if c.is_alive()
            ]

            for index, chain in enumerate(self.chains):
                status = "ACTIVE"

                self.tree.insert(
                    "",
                    "end",
                    iid=f"c{index}",
                    values=(
                        status,
                        " → ".join(map(str, chain.route)),
                        f"~{chain.size} players",
                        chain.hop_count,
                        f"{chain.confidence}%",
                        time.strftime(
                            "%H:%M:%S",
                            time.localtime(chain.last_time)
                        ),
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

            chain = self.chains[
                int(selection[0][1:])
            ]

            self.detail.set(
                f"ACTIVE GROUP • ~{chain.size} players • "
                f"{chain.hop_count} hops • "
                f"{chain.confidence}% same-group likelihood • "
                f"Last seen {int(chain.age)}s ago • "
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

        self.detail.set(
            "History cleared."
        )

        self.draw()


if __name__ == "__main__":
    root = tk.Tk()
    app = App(root)
    root.mainloop()
