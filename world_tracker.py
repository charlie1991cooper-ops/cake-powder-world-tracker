import html
import json
import re
import threading
import time
import urllib.request
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from email.parser import BytesParser
from email.policy import default as email_default_policy
from collections import defaultdict, deque
from dataclasses import dataclass
from html.parser import HTMLParser
import tkinter as tk
from tkinter import ttk

try:
    import winsound
except ImportError:
    winsound = None

SOURCE_URL = "https://oldschool.runescape.com/slu"
INTERVAL = 2
MOVEMENT_WINDOW = 10
MAX_TRACKED_MOVEMENT = 400
APP_NAME = "Cake's OSRS World Tracker"
DINK_HOST = "127.0.0.1"
DINK_PORT = 8080
DINK_PATH = "/dink"
DINK_STALE_SECONDS = 120


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
    score: int
    timestamp: float


@dataclass(frozen=True)
class WorldMovement:
    world: int
    delta: int
    score: int
    baseline: float
    timestamp: float
    watched: bool = False


class WorldParser(HTMLParser):
    """Parse the official world list and use slu-world-XXX for the real ID."""

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
    req = urllib.request.Request(
        SOURCE_URL,
        headers={"User-Agent": "Cakes-OSRS-World-Tracker/6.0"},
    )
    with urllib.request.urlopen(req, timeout=15) as response:
        raw = response.read().decode("utf-8", "replace")

    parser = WorldParser()
    parser.feed(raw)
    worlds = []

    for world_id, row in parser.rows:
        if len(row) < 5:
            continue
        players_match = re.search(r"([\d,]+)\s+players?", row[1], re.I)
        players = int(players_match.group(1).replace(",", "")) if players_match else 0
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

    if not worlds:
        raise RuntimeError("No OSRS worlds found in the server list.")

    unique = {world.world: world for world in worlds}
    return sorted(unique.values(), key=lambda world: world.world)


def ratio_score(left, appeared):
    if left <= 0 or appeared <= 0:
        return 0
    return min(left, appeared) / max(left, appeared)


def detect_hops(prev, cur, min_group, delta_history=None):
    drops, gains = collect_changes(prev, cur, min_group)
    results, _, _ = match_changes(drops, gains, time.time(), delta_history)
    return results


def collect_changes(prev, cur, min_group):
    drops = []
    gains = []
    for world in set(prev) & set(cur):
        delta = cur[world].players - prev[world].players
        # Ignore very large population swings. These are more likely to be
        # world-list/data anomalies than a single moving group.
        magnitude = abs(delta)
        if magnitude > MAX_TRACKED_MOVEMENT:
            continue
        if delta <= -min_group:
            drops.append((world, -delta))
        elif delta >= min_group:
            gains.append((world, delta))
    return drops, gains


def match_changes(drops, gains, timestamp, delta_history=None):
    """Match population drops/gains, including a short cross-poll grace period."""
    delta_history = delta_history or {}
    pairs = []
    for source, left in drops:
        for destination, appeared in gains:
            ratio = ratio_score(left, appeared)
            share = 0.5
            score = 58 * ratio + 30 * share
            distance = abs(source - destination)
            if distance == 1:
                score += 8
            elif distance <= 3:
                score += 5
            elif distance <= 10:
                score += 2
            src_noise = sum(delta_history.get(source, ())) / max(1, len(delta_history.get(source, ())))
            dst_noise = sum(delta_history.get(destination, ())) / max(1, len(delta_history.get(destination, ())))
            if src_noise + dst_noise > 0:
                score += min(7, (left + appeared) / (src_noise + dst_noise + 1) * 0.7)
            pairs.append((score, source, destination, left, appeared))

    pairs.sort(reverse=True)
    used_sources = set()
    used_destinations = set()
    results = []
    for score, source, destination, left, appeared in pairs:
        if source in used_sources or destination in used_destinations or score < 35:
            continue
        results.append(Hop(source, destination, left, appeared, max(left, appeared), min(99, max(0, round(score))), timestamp))
        used_sources.add(source)
        used_destinations.add(destination)
    return results, used_sources, used_destinations


def movement_score(delta, noise_values, min_move):
    """Score a one-world anomaly without requiring a matching source/destination."""
    magnitude = abs(delta)
    if magnitude < min_move:
        return 0, 0.0

    vals = list(noise_values)
    baseline = sum(vals) / len(vals) if vals else 0.0
    # Population-list noise is usually small compared with a real group movement.
    scale = max(1.0, baseline * 2.5)
    score = 45 + min(45, (magnitude / scale) * 18)
    if magnitude >= min_move * 2:
        score += 8
    return min(99, round(score)), baseline


class GroupChain:
    MAX_AGE = 60 * 60
    SIZE_TOLERANCE = 0.65

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
        # Use the strongest side of each observation. This avoids systematically turning
        # a 20-person group into an 8-person group when only 8 show up on the destination.
        values = [max(h.left, h.appeared) for h in recent]
        return max(1, round(sum(values) / len(values)))

    @property
    def hop_count(self):
        return len(self.hops)

    @property
    def route(self):
        return [self.hops[0].source] + [h.destination for h in self.hops]

    @property
    def score(self):
        recent = self.hops[-8:]
        avg = sum(h.score for h in recent) / len(recent)
        values = [max(h.left, h.appeared) for h in recent]
        mean = sum(values) / len(values) if values else 0
        deviation = sum(abs(v - mean) for v in values) / len(values) if values else mean
        consistency = max(0.0, 1.0 - deviation / mean) if mean else 0.0

        repeat_bonus = min(32, max(0, self.hop_count - 1) * 8)
        consistency_bonus = round(consistency * 15)
        score = avg * 0.62 + repeat_bonus + consistency_bonus
        if self.hop_count >= 3 and consistency >= 0.65:
            score += 8
        if self.hop_count >= 5 and consistency >= 0.75:
            score += 9
        return min(99, max(0, round(score)))

    def is_alive(self):
        return self.age <= self.MAX_AGE

    def can_extend(self, hop):
        if not self.is_alive() or hop.source != self.last_world:
            return False
        estimated = self.size
        difference = abs(hop.moved - estimated) / max(1, estimated)
        return difference <= self.SIZE_TOLERANCE

    def add(self, hop):
        self.hops.append(hop)
        self.last_world = hop.destination
        self.last_time = hop.timestamp


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


class DinkRequestHandler(BaseHTTPRequestHandler):
    """Tiny local Dink webhook receiver. It accepts JSON and multipart payload_json."""

    server_version = "CakesOSRSTracker/1.0"

    def log_message(self, format, *args):
        # Keep the console quiet; the app reports Dink status in the UI.
        return

    def _send(self, code=200, body=b"OK"):
        self.send_response(code)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except Exception:
            pass

    def do_GET(self):
        if self.path.rstrip("/") == DINK_PATH:
            self._send(200, b"Cake's OSRS World Tracker Dink endpoint is running.")
        else:
            self._send(404, b"Not found")

    def do_POST(self):
        if self.path.rstrip("/") != DINK_PATH:
            self._send(404, b"Not found")
            return

        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > 10_000_000:
                self._send(400, b"Invalid body")
                return
            body = self.rfile.read(length)
            content_type = self.headers.get("Content-Type", "")
            event = None

            if content_type.lower().startswith("multipart/"):
                # email's MIME parser handles the multipart/form-data emitted by Dink.
                raw_headers = (
                    f"Content-Type: {content_type}\r\n"
                    "MIME-Version: 1.0\r\n\r\n"
                ).encode("utf-8")
                msg = BytesParser(policy=email_default_policy).parsebytes(raw_headers + body)
                for part in msg.iter_parts():
                    if part.get_param("name", header="content-disposition") == "payload_json":
                        payload = part.get_payload(decode=True) or b""
                        event = json.loads(payload.decode("utf-8", "replace"))
                        break
            else:
                event = json.loads(body.decode("utf-8", "replace"))

            if isinstance(event, dict):
                app = getattr(self.server, "app", None)
                if app is not None:
                    app.root.after(0, lambda e=event: app.process_dink_event(e))

            self._send(200)
        except Exception:
            self._send(400, b"Bad JSON")


class App:
    def __init__(self, root):
        self.root = root
        root.title(APP_NAME)
        root.geometry("1380x820")
        root.minsize(1120, 720)
        root.configure(bg="#0b1018")

        style = ttk.Style(root)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        self.configure_style(style)

        self.worlds = []
        self.previous = None
        self.hops = []
        self.movements = []
        self.chains = []
        self.alerts = deque(maxlen=250)
        self.max_chain_history = 150
        self.delta_history = defaultdict(lambda: deque(maxlen=12))
        # Short-lived unmatched changes let us catch a real hop when the official
        # world list reports the source drop and destination rise one poll apart.
        self.pending_drops = deque(maxlen=100)
        self.pending_gains = deque(maxlen=100)
        self.last_fetch_started = 0.0
        self.last_alert_signature = {}

        self.f2p = tk.BooleanVar(value=False)

        self.watched_players = []
        self.watched_player_worlds = {}
        self.watched_player_seen = {}
        self.watched_player_status = {}
        self.watched_player_events = {}
        self.dink_last_event = None
        self.dink_enabled = tk.BooleanVar(value=False)
        self.dink_status = tk.StringVar(value="Dink input disabled")
        self.load_watched_players()
        self.dink_server = None
        self.dink_thread = None
        self.min_group = tk.IntVar(value=5)
        self.min_conf = tk.IntVar(value=50)
        self.min_world_move = tk.IntVar(value=5)
        self.watch_enabled = tk.BooleanVar(value=False)
        self.watch_world = tk.StringVar(value="")
        self.watch_threshold = tk.IntVar(value=5)
        self.sound_alerts = tk.BooleanVar(value=True)

        self.view = "hops"
        self.status = tk.StringVar(value="Starting…")
        self.detail = tk.StringVar(value="Waiting for first snapshot.")
        self.alert_banner = tk.StringVar(value="No alerts yet")

        self.build_ui()
        self.set_dink_status()
        if self.dink_enabled.get():
            self.start_dink_server()
        root.protocol("WM_DELETE_WINDOW", self.close)
        root.after(100, self.refresh)

    def configure_style(self, style):
        style.configure("TFrame", background="#0b1018")
        style.configure("TLabel", background="#0b1018", foreground="#e7edf6", font=("Segoe UI", 9))
        style.configure("Header.TLabel", background="#0b1018", foreground="#f4f7fb", font=("Segoe UI", 23, "bold"))
        style.configure("Brand.TLabel", background="#0b1018", foreground="#a970ff", font=("Segoe UI", 10, "bold"))
        style.configure("Status.TLabel", background="#0b1018", foreground="#8d9ab0", font=("Segoe UI", 9))
        style.configure("Title.TLabel", background="#0b1018", foreground="#b477ff", font=("Segoe UI", 11, "bold"))
        style.configure("TCheckbutton", background="#111927", foreground="#dce4f0", font=("Segoe UI", 9))
        style.map("TCheckbutton", background=[("active", "#111927")])
        style.configure("TLabelframe", background="#111927", foreground="#a970ff", bordercolor="#263247")
        style.configure("TLabelframe.Label", background="#111927", foreground="#a970ff", font=("Segoe UI", 10, "bold"))
        style.configure("TButton", background="#182235", foreground="#e7edf6", bordercolor="#2a3951", padding=(13, 7), font=("Segoe UI", 9, "bold"))
        style.map("TButton", background=[("active", "#293953"), ("pressed", "#34476a")])
        style.configure("Accent.TButton", background="#7c4dff", foreground="white", bordercolor="#7c4dff", padding=(15, 8), font=("Segoe UI", 9, "bold"))
        style.map("Accent.TButton", background=[("active", "#966eff"), ("pressed", "#6938df")])
        style.configure("Modern.Treeview", background="#111927", fieldbackground="#111927", foreground="#e7edf6", rowheight=32, borderwidth=0, relief="flat", font=("Segoe UI", 9))
        style.configure("Modern.Treeview.Heading", background="#182235", foreground="#d5ddeb", relief="flat", borderwidth=0, padding=(9, 9), font=("Segoe UI", 9, "bold"))
        style.map("Modern.Treeview", background=[("selected", "#3d2875")], foreground=[("selected", "white")])
        style.configure("Modern.Vertical.TScrollbar", background="#182235", troughcolor="#0b1018", bordercolor="#0b1018", arrowcolor="#91a0b5")


    # ---------------- Experimental Dink / watched players ----------------

    def watch_file(self):
        return Path.home() / "cakes_osrs_tracker_watched_players.json"

    def load_watched_players(self):
        try:
            path = self.watch_file()
            if path.exists():
                data = json.loads(path.read_text(encoding="utf-8"))
                self.watched_players = [
                    str(x).strip()
                    for x in data.get("players", [])
                    if str(x).strip()
                ]
                self.dink_enabled.set(bool(data.get("dink_enabled", False)))
        except Exception:
            self.watched_players = []

    def save_watched_players(self):
        try:
            self.watch_file().write_text(
                json.dumps({
                    "players": self.watched_players,
                    "dink_enabled": bool(self.dink_enabled.get())
                }, indent=2),
                encoding="utf-8"
            )
        except Exception:
            pass

    def add_watched_player(self):
        name = self.watch_player_entry.get().strip()
        if not name:
            return
        if not any(p.lower() == name.lower() for p in self.watched_players):
            self.watched_players.append(name)
            self.save_watched_players()
            self.refresh_watched_players()
        self.watch_player_entry.delete(0, "end")

    def remove_watched_player(self):
        selected = self.watch_tree.selection()
        if not selected:
            return
        name = str(self.watch_tree.item(selected[0], "values")[0])
        self.watched_players = [
            p for p in self.watched_players
            if p.lower() != name.lower()
        ]
        key = name.lower()
        self.watched_player_worlds.pop(key, None)
        self.watched_player_seen.pop(key, None)
        self.save_watched_players()
        self.refresh_watched_players()

    def refresh_watched_players(self):
        if not hasattr(self, "watch_tree"):
            return

        self.watch_tree.delete(*self.watch_tree.get_children())
        now = time.time()
        for name in self.watched_players:
            key = name.lower()
            world = self.watched_player_worlds.get(key)
            seen = self.watched_player_seen.get(key)
            status = self.watched_player_status.get(key, "UNKNOWN")
            age = (now - seen) if seen else None

            if status == "ONLINE" and age is not None and age > DINK_STALE_SECONDS:
                status = "STALE"
                self.watched_player_status[key] = status
            elif status == "UNKNOWN":
                status = "UNKNOWN"

            world_text = str(world) if world else "—"
            last = f"{max(0, int(age))}s" if age is not None else "—"
            self.watch_tree.insert("", "end", values=(name, status, world_text, last))

    def set_dink_enabled(self):
        self.save_watched_players()
        if self.dink_enabled.get():
            self.start_dink_server()
        else:
            self.stop_dink_server()
        self.set_dink_status()

    def set_dink_status(self):
        if not self.dink_enabled.get():
            self.dink_status.set("Dink input disabled")
            return

        if self.dink_server is None:
            self.dink_status.set("Dink enabled • starting local listener…")
            return

        base = f"Listening on http://{DINK_HOST}:{DINK_PORT}{DINK_PATH}"
        if self.dink_last_event:
            e = self.dink_last_event
            world = f" • World {e['world']}" if e.get('world') else ""
            age = max(0, int(time.time() - e['time']))
            self.dink_status.set(
                f"{base}\nLast: {e['type']} • {e['player']}{world} • {age}s ago"
            )
        else:
            self.dink_status.set(base + "\nWaiting for Dink event…")

    def start_dink_server(self):
        if self.dink_server is not None:
            return
        try:
            server = ThreadingHTTPServer((DINK_HOST, DINK_PORT), DinkRequestHandler)
            server.daemon_threads = True
            server.app = self
            self.dink_server = server
            self.dink_thread = threading.Thread(target=server.serve_forever, daemon=True)
            self.dink_thread.start()
            self.set_dink_status()
            self.alert_banner.set(f"Dink listener ready • {DINK_HOST}:{DINK_PORT}{DINK_PATH}")
        except OSError as exc:
            self.dink_server = None
            self.dink_thread = None
            self.dink_status.set(f"Dink listener failed: {exc}")
            self.alert_banner.set("Dink could not bind to port 8080")

    def stop_dink_server(self):
        server = self.dink_server
        self.dink_server = None
        self.dink_thread = None
        if server is not None:
            try:
                server.shutdown()
                server.server_close()
            except Exception:
                pass

    def process_dink_event(self, event):
        """Process only events belonging to watched players."""
        if not isinstance(event, dict):
            return

        extra = event.get("extra") if isinstance(event.get("extra"), dict) else {}
        name = (
            event.get("playerName") or event.get("player") or event.get("username")
            or extra.get("playerName") or extra.get("username")
        )
        if not name:
            return

        watched_name = next((p for p in self.watched_players
                             if p.lower() == str(name).strip().lower()), None)
        if watched_name is None:
            return

        key = watched_name.lower()
        event_type = str(event.get("type") or "").upper()
        now = time.time()
        self.dink_last_event = {
            "type": event_type or "UNKNOWN",
            "player": watched_name,
            "time": now,
            "world": None,
        }

        world = event.get("world") or extra.get("world") or extra.get("worldId")
        try:
            world = int(world) if world is not None else None
        except (TypeError, ValueError):
            world = None

        old_world = self.watched_player_worlds.get(key)
        self.watched_player_seen[key] = now
        self.watched_player_events[key] = event_type or "UNKNOWN"

        if event_type in {"LOGOUT", "LOGGED_OUT"}:
            self.watched_player_status[key] = "OFFLINE"
            self.refresh_watched_players()
            self.set_dink_status()
            return

        if world is not None:
            self.watched_player_worlds[key] = world
            self.watched_player_status[key] = "ONLINE"
            self.dink_last_event["world"] = world

            if old_world and old_world != world:
                self.alert_banner.set(
                    f"ALERT • {watched_name} world change {old_world} → {world}"
                )
                if self.sound_alerts.get() and winsound:
                    try:
                        winsound.MessageBeep(winsound.MB_ICONEXCLAMATION)
                    except Exception:
                        pass
        elif event_type in {"LOGIN", "LOGGED_IN"}:
            # We know the player is online even if the world field was absent.
            self.watched_player_status[key] = "ONLINE"

        self.refresh_watched_players()
        self.set_dink_status()

    def close(self):
        self.stop_dink_server()
        self.root.destroy()

    def build_ui(self):
        header = ttk.Frame(self.root, padding=(18, 15, 18, 8))
        header.pack(fill="x")
        ttk.Label(header, text="Cake's OSRS World Tracker", style="Header.TLabel").pack(side="left")
        ttk.Label(header, textvariable=self.status, style="Status.TLabel").pack(side="right")

        toolbar = ttk.Frame(self.root, padding=(18, 0, 18, 12))
        toolbar.pack(fill="x")
        for text, view in (("Worlds", "worlds"), ("Group Hops", "hops"), ("Active Groups", "chains"), ("World Alerts", "alerts")):
            ttk.Button(toolbar, text=text, command=lambda v=view: self.set_view(v)).pack(side="left", padx=3)
        ttk.Checkbutton(toolbar, text="Include F2P worlds", variable=self.f2p, command=self.reset_baseline).pack(side="left", padx=18)
        ttk.Button(toolbar, text="Refresh Now", style="Accent.TButton", command=self.refresh).pack(side="right", padx=3)
        ttk.Label(toolbar, text="10-second movement matching window", style="Status.TLabel").pack(side="right", padx=12)

        body = ttk.Frame(self.root)
        body.pack(fill="both", expand=True, padx=18, pady=4)

        settings = ttk.LabelFrame(body, text="Detection & Watch", padding=14)
        settings.pack(side="left", fill="y", padx=(0, 14))
        settings.configure(width=245)

        ttk.Label(settings, text="Minimum group size").pack(anchor="w", pady=(2, 4))
        ttk.Spinbox(settings, from_=1, to=400, textvariable=self.min_group, width=9).pack(anchor="w")
        ttk.Label(settings, text="Minimum likelihood level").pack(anchor="w", pady=(13, 4))
        ttk.Combobox(settings, values=("Possible (50)", "Likely (75)", "Very likely (90)"), state="readonly", width=18).pack(anchor="w")
        # Keep the actual threshold simple and compatible with the existing variable.
        self.conf_combo = settings.winfo_children()[-1]
        self.conf_combo.current(0)
        self.conf_combo.bind("<<ComboboxSelected>>", self.conf_changed)

        ttk.Separator(settings).pack(fill="x", pady=14)
        ttk.Label(settings, text="Single-world movement threshold").pack(anchor="w", pady=(0, 4))
        ttk.Spinbox(settings, from_=1, to=400, textvariable=self.min_world_move, width=9).pack(anchor="w")
        ttk.Label(settings, text="This detects unusual + / − changes\neven without a matching world.", foreground="#8d9ab0").pack(anchor="w", pady=(4, 10))

        ttk.Separator(settings).pack(fill="x", pady=8)
        ttk.Checkbutton(settings, text="Watch a specific world", variable=self.watch_enabled, command=self.watch_changed).pack(anchor="w", pady=4)
        ttk.Label(settings, text="World to watch").pack(anchor="w", pady=(7, 4))
        self.watch_combo = ttk.Combobox(settings, textvariable=self.watch_world, width=12, state="normal")
        self.watch_combo.pack(anchor="w")
        ttk.Label(settings, text="Watch movement threshold").pack(anchor="w", pady=(9, 4))
        ttk.Spinbox(settings, from_=1, to=500, textvariable=self.watch_threshold, width=9).pack(anchor="w")
        ttk.Checkbutton(settings, text="Sound alerts", variable=self.sound_alerts).pack(anchor="w", pady=8)

        ttk.Separator(settings).pack(fill="x", pady=8)
        ttk.Button(settings, text="Clear history", command=self.clear_history).pack(fill="x", pady=6)
        ttk.Label(settings, textvariable=self.alert_banner, foreground="#b477ff", wraplength=210, justify="left").pack(anchor="w", side="bottom", pady=(12, 0))

        # Small optional Dink watch list.
        watch_panel = ttk.LabelFrame(
            body,
            text="  WATCHED PLAYERS  ",
            padding=(12, 12)
        )
        watch_panel.pack(side="left", fill="y", padx=(0, 14))
        watch_panel.configure(width=275)
        watch_panel.pack_propagate(False)

        ttk.Checkbutton(
            watch_panel,
            text="Enable Dink input",
            variable=self.dink_enabled,
            command=self.set_dink_enabled
        ).pack(anchor="w")

        ttk.Label(
            watch_panel,
            textvariable=self.dink_status,
            foreground="#8d9ab0",
            wraplength=235,
            justify="left"
        ).pack(anchor="w", pady=(4, 12))

        ttk.Label(
            watch_panel,
            text="PLAYER NAME",
            font=("Segoe UI", 9, "bold")
        ).pack(anchor="w", pady=(0, 5))

        add_row = ttk.Frame(watch_panel)
        add_row.pack(fill="x")

        self.watch_player_entry = ttk.Entry(add_row)
        self.watch_player_entry.pack(side="left", fill="x", expand=True)
        self.watch_player_entry.bind(
            "<Return>",
            lambda _event: self.add_watched_player()
        )

        ttk.Button(
            add_row,
            text="+",
            width=3,
            command=self.add_watched_player
        ).pack(side="left", padx=(6, 0))

        self.watch_tree = ttk.Treeview(
            watch_panel,
            columns=("player", "status", "world", "last"),
            show="headings",
            height=8
        )
        for col, heading, width in (
            ("player", "PLAYER", 100),
            ("status", "STATUS", 70),
            ("world", "WORLD", 55),
            ("last", "LAST", 50),
        ):
            self.watch_tree.heading(col, text=heading)
            self.watch_tree.column(col, width=width, anchor="center")
        self.watch_tree.column("player", anchor="w")
        self.watch_tree.pack(fill="both", expand=True, pady=(12, 8))

        ttk.Button(
            watch_panel,
            text="REMOVE SELECTED",
            command=self.remove_watched_player
        ).pack(fill="x")

        ttk.Label(
            watch_panel,
            text=(
                "Only names added here are processed.\n"
                "Dink sends login/event data to this app.\n"
                "World-hop updates depend on Dink sending an event with a world."
            ),
            foreground="#8d9ab0",
            justify="left"
        ).pack(anchor="w", pady=(12, 0))

        self.refresh_watched_players()

        main = ttk.Frame(body)
        main.pack(side="left", fill="both", expand=True)
        self.view_title = ttk.Label(main, text="GROUP HOP DETECTIONS", style="Title.TLabel")
        self.view_title.pack(anchor="w", pady=(0, 8))

        table_frame = ttk.Frame(main)
        table_frame.pack(fill="both", expand=True)
        self.tree = ttk.Treeview(table_frame, show="headings", style="Modern.Treeview")
        scrollbar = ttk.Scrollbar(table_frame, orient="vertical", command=self.tree.yview, style="Modern.Vertical.TScrollbar")
        self.tree.configure(yscrollcommand=scrollbar.set)
        self.tree.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")
        self.tree.bind("<<TreeviewSelect>>", self.select_row)

        # Likelihood colours instead of misleading percentages.
        self.tree.tag_configure("very", foreground="#39e58c")
        self.tree.tag_configure("likely", foreground="#e8d44d")
        self.tree.tag_configure("possible", foreground="#ff9d32")
        self.tree.tag_configure("unlikely", foreground="#ff5964")
        self.tree.tag_configure("watch", background="#241a3d")
        self.tree.tag_configure("surge", background="#162a2a")

        ttk.Label(main, textvariable=self.detail, wraplength=1050, foreground="#8d9ab0").pack(anchor="w", pady=8)
        self.set_view("hops")

    def conf_changed(self, _event=None):
        text = self.conf_combo.get()
        self.min_conf.set(50 if text.startswith("Possible") else 75 if text.startswith("Likely") else 90)
        self.draw()

    def watch_changed(self):
        self.draw()

    def visible_worlds(self):
        return self.worlds if self.f2p.get() else [w for w in self.worlds if w.membership == "Members"]

    def reset_baseline(self):
        self.previous = None
        self.detail.set("Baseline reset; waiting for the next 10-second snapshot.")
        self.draw()

    def set_view(self, view):
        self.view = view
        titles = {
            "hops": "GROUP HOP DETECTIONS",
            "chains": "ACTIVE GROUPS / 1 HOUR MEMORY",
            "worlds": "OSRS WORLD POPULATIONS",
            "alerts": "WORLD MOVEMENT ALERTS",
        }
        columns = {
            "hops": ("from", "to", "left", "app", "moved", "likelihood", "time"),
            "chains": ("status", "route", "size", "hops", "likelihood", "time"),
            "worlds": ("world", "players", "location", "type", "activity"),
            "alerts": ("world", "movement", "change", "likelihood", "time", "reason"),
        }
        self.view_title.config(text=titles[view])
        self.tree["columns"] = columns[view]
        names = {
            "from": "FROM", "to": "TO", "left": "LEFT", "app": "APPEARED", "moved": "EST. GROUP",
            "likelihood": "LIKELIHOOD", "time": "TIME", "status": "STATUS", "route": "GROUP ROUTE",
            "size": "GROUP SIZE", "hops": "HOPS", "world": "WORLD", "players": "PLAYERS",
            "location": "LOCATION", "type": "TYPE", "activity": "ACTIVITY", "movement": "MOVEMENT",
            "change": "CHANGE", "reason": "REASON",
        }
        for col in columns[view]:
            self.tree.heading(col, text=names[col])
            self.tree.column(col, width=120, anchor="center")
        if view == "hops":
            for col, width in (("from", 85), ("to", 85), ("left", 105), ("app", 105), ("moved", 115), ("likelihood", 125), ("time", 95)):
                self.tree.column(col, width=width)
        elif view == "chains":
            for col, width in (("status", 90), ("route", 420), ("size", 110), ("hops", 80), ("likelihood", 125), ("time", 95)):
                self.tree.column(col, width=width)
        elif view == "worlds":
            for col, width in (("world", 85), ("players", 110), ("location", 180), ("type", 100), ("activity", 360)):
                self.tree.column(col, width=width, anchor="center" if col != "activity" else "w")
        else:
            for col, width in (("world", 80), ("movement", 105), ("change", 90), ("likelihood", 125), ("time", 95), ("reason", 420)):
                self.tree.column(col, width=width)
        self.detail.set({
            "worlds": "Current official OSRS world population snapshot.",
            "hops": "Paired population drops and rises that are consistent with a group moving between worlds.",
            "chains": "Persistent inferred groups remembered for one hour after their latest matching hop. Routes may revisit worlds.",
            "alerts": "Unusual world-level population movements, including movements with no detectable source world.",
        }[view])
        self.draw()

    def refresh(self):
        self.last_fetch_started = time.time()
        threading.Thread(target=self.worker, daemon=True).start()

    def worker(self):
        try:
            worlds = fetch_worlds()
            self.root.after(0, lambda: self.apply_worlds(worlds))
        except Exception as exc:
            self.root.after(0, lambda: self.fetch_failed(str(exc)))

    def fetch_failed(self, message):
        self.status.set("Update failed; retrying…")
        self.detail.set(f"Could not update world data: {message}")
        self.root.after(int(INTERVAL * 1000), self.refresh)

    def apply_worlds(self, worlds):
        self.worlds = worlds
        current = {w.world: w for w in self.visible_worlds()}
        now = time.time()
        if self.previous is None:
            self.previous = current
            self.update_watch_list()
            self.detail.set("Baseline captured. Waiting for the next 10-second snapshot.")
        else:
            histories = self.delta_history
            # Use the previous noise history for scoring this snapshot; append the new
            # delta only after it has been classified. Otherwise a genuine surge would
            # partially train itself into looking normal.
            drops, gains = collect_changes(self.previous, current, self.min_group.get())
            hops = self.detect_with_pending(drops, gains, now, histories)
            for hop in hops:
                if hop.score >= self.min_conf.get():
                    self.hops.insert(0, hop)
                    self.update_chain(hop)

            # Detect unusual movement on a single world even when there is no matching source/destination.
            for world in set(self.previous) & set(current):
                delta = current[world].players - self.previous[world].players
                if abs(delta) < self.min_world_move.get():
                    continue
                if abs(delta) > MAX_TRACKED_MOVEMENT:
                    continue
                score, baseline = movement_score(delta, histories[world], self.min_world_move.get())
                watched = self.is_watched(world)
                watch_threshold = self.watch_threshold.get()
                if watched and abs(delta) >= watch_threshold:
                    score = max(score, 85)
                if score >= self.min_conf.get() or (watched and abs(delta) >= watch_threshold):
                    movement = WorldMovement(world, delta, score, baseline, now, watched)
                    self.movements.insert(0, movement)
                    self.add_alert(movement)
                histories[world].append(abs(delta))

            self.previous = current
            self.update_watch_list()

        self.status.set(
            f"Updated {time.strftime('%H:%M:%S')} • {len(current)} worlds • "
            f"{'F2P + Members' if self.f2p.get() else 'Members only'}"
        )
        self.refresh_watched_players()
        self.draw()
        elapsed = max(0.0, time.time() - self.last_fetch_started)
        delay = max(500, int(INTERVAL * 1000 - elapsed * 1000))
        self.root.after(delay, self.refresh)

    def detect_with_pending(self, drops, gains, now, histories):
        """Match same-snapshot changes first, then unmatched changes from the last 10s."""
        cutoff = now - MOVEMENT_WINDOW
        self.pending_drops = deque((x for x in self.pending_drops if x[2] >= cutoff), maxlen=100)
        self.pending_gains = deque((x for x in self.pending_gains if x[2] >= cutoff), maxlen=100)

        results = []
        same, used_sources, used_destinations = match_changes(drops, gains, now, histories)
        results.extend(same)

        rem_drops = [(w, n) for w, n in drops if w not in used_sources]
        rem_gains = [(w, n) for w, n in gains if w not in used_destinations]

        # Current gains can match a recent drop, and current drops can match a recent gain.
        recent_drops = list(self.pending_drops)
        recent_gains = list(self.pending_gains)
        cross_candidates = []
        for source, left, ts in recent_drops:
            for destination, appeared in rem_gains:
                age = max(0.0, now - ts)
                ratio = ratio_score(left, appeared)
                score = 45 * ratio + 35
                if age <= MOVEMENT_WINDOW:
                    score += 8
                if abs(source - destination) == 1:
                    score += 5
                cross_candidates.append((score, source, destination, left, appeared, ts))
        for source, left in rem_drops:
            for destination, appeared, ts in recent_gains:
                age = max(0.0, now - ts)
                ratio = ratio_score(left, appeared)
                score = 45 * ratio + 35
                if age <= MOVEMENT_WINDOW:
                    score += 8
                if abs(source - destination) == 1:
                    score += 5
                cross_candidates.append((score, source, destination, left, appeared, ts))

        used_pending_drops = set()
        used_pending_gains = set()
        used_current = set()
        for score, source, destination, left, appeared, ts in sorted(cross_candidates, reverse=True):
            key = (source, destination)
            if destination in used_current or source in used_current or score < 55:
                continue
            results.append(Hop(source, destination, left, appeared, max(left, appeared), min(99, round(score)), now))
            # Mark the relevant pending side as consumed.
            if any(x[0] == source and abs(x[2] - ts) < 0.01 for x in recent_drops):
                used_pending_drops.add((source, ts))
            if any(x[0] == destination and abs(x[2] - ts) < 0.01 for x in recent_gains):
                used_pending_gains.add((destination, ts))
            used_current.add(source)
            used_current.add(destination)

        # Keep only unmatched changes. They can be paired with the next poll.
        matched_sources = {h.source for h in results if h.timestamp == now}
        matched_destinations = {h.destination for h in results if h.timestamp == now}
        for source, left in rem_drops:
            if source not in matched_sources:
                self.pending_drops.append((source, left, now))
        for destination, appeared in rem_gains:
            if destination not in matched_destinations:
                self.pending_gains.append((destination, appeared, now))

        # Remove pending entries that were consumed by a cross-poll match.
        if used_pending_drops:
            self.pending_drops = deque((x for x in self.pending_drops if (x[0], x[2]) not in used_pending_drops), maxlen=100)
        if used_pending_gains:
            self.pending_gains = deque((x for x in self.pending_gains if (x[0], x[2]) not in used_pending_gains), maxlen=100)
        return results


    def is_watched(self, world):
        if not self.watch_enabled.get():
            return False
        try:
            return int(self.watch_world.get()) == world
        except (ValueError, TypeError):
            return False

    def update_watch_list(self):
        values = [str(w.world) for w in self.visible_worlds()]
        self.watch_combo["values"] = values

    def add_alert(self, movement):
        direction = "INFLUX" if movement.delta > 0 else "OUTFLOW"
        signature = (movement.world, direction)
        last = self.last_alert_signature.get(signature, 0)
        # Avoid repeatedly alerting on the same world every 10 seconds during a slow movement.
        if time.time() - last < 20:
            return
        self.last_alert_signature[signature] = time.time()
        reason = "WATCHED WORLD" if movement.watched else "UNUSUAL WORLD MOVEMENT"
        self.alerts.appendleft((movement, reason))
        self.alert_banner.set(f"ALERT • World {movement.world} {direction} {abs(movement.delta)} players")
        if self.sound_alerts.get() and winsound:
            try:
                winsound.MessageBeep(winsound.MB_ICONEXCLAMATION)
            except Exception:
                pass

    def update_chain(self, hop):
        self.chains = [c for c in self.chains if c.is_alive()]
        candidates = [c for c in self.chains if c.can_extend(hop)]
        if candidates:
            candidates.sort(key=lambda c: (abs(c.size - hop.moved), c.age))
            candidates[0].add(hop)
        else:
            self.chains.insert(0, GroupChain(hop))
        self.chains = self.chains[:self.max_chain_history]

    def draw(self):
        self.tree.delete(*self.tree.get_children())
        watch_world = None
        try:
            watch_world = int(self.watch_world.get()) if self.watch_enabled.get() else None
        except ValueError:
            pass

        if self.view == "hops":
            for hop in self.hops:
                if hop.score < self.min_conf.get():
                    continue
                self.tree.insert("", "end", values=(hop.source, hop.destination, hop.left, hop.appeared, hop.moved, likelihood_label(hop.score), time.strftime("%H:%M:%S", time.localtime(hop.timestamp))), tags=(likelihood_tag(hop.score),))
        elif self.view == "chains":
            self.chains = [c for c in self.chains if c.is_alive()]
            for index, chain in enumerate(self.chains):
                self.tree.insert("", "end", iid=f"c{index}", values=("ACTIVE", " → ".join(map(str, chain.route)), f"~{chain.size}", chain.hop_count, likelihood_label(chain.confidence), time.strftime("%H:%M:%S", time.localtime(chain.last_time))), tags=(likelihood_tag(chain.confidence),))
        elif self.view == "worlds":
            for world in self.visible_worlds():
                tags = ("watch",) if watch_world == world.world else ()
                self.tree.insert("", "end", values=(world.world, f"{world.players:,}", world.location, world.membership, world.activity), tags=tags)
        else:
            for movement, reason in list(self.alerts):
                direction = "INFLUX" if movement.delta > 0 else "OUTFLOW"
                tags = [likelihood_tag(movement.score)]
                if movement.watched:
                    tags.append("watch")
                self.tree.insert("", "end", values=(movement.world, direction, f"{movement.delta:+d}", likelihood_label(movement.score), time.strftime("%H:%M:%S", time.localtime(movement.timestamp)), reason), tags=tuple(tags))

    def select_row(self, _event=None):
        selection = self.tree.selection()
        if not selection:
            return
        values = self.tree.item(selection[0], "values")
        if self.view == "chains":
            idx = int(selection[0][1:])
            if idx < len(self.chains):
                chain = self.chains[idx]
                self.detail.set(f"ACTIVE GROUP • ~{chain.size} players • {chain.hop_count} hops • {likelihood_label(chain.confidence)} • last seen {int(chain.age)}s ago • Route: {' → '.join(map(str, chain.route))}")
        elif self.view == "hops":
            self.detail.set(f"World {values[0]} → {values[1]} • {values[2]} left • {values[3]} appeared • estimated group {values[4]} • {values[5]}")
        elif self.view == "alerts":
            self.detail.set(f"World {values[0]} • {values[1]} of {abs(int(values[2]))} players • {values[3]} • {values[5]}")

    def clear_history(self):
        self.hops.clear()
        self.movements.clear()
        self.chains.clear()
        self.alerts.clear()
        self.last_alert_signature.clear()
        self.alert_banner.set("No alerts yet")
        self.detail.set("History cleared.")
        self.draw()


if __name__ == "__main__":
    root = tk.Tk()
    App(root)
    root.mainloop()
