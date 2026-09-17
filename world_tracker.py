"""
Cake's OSRS World Tracker
-------------------------
A Windows/Python desktop application designed to detect, infer, and track
PK teams moving across Old School RuneScape (OSRS) worlds via population telemetry.
"""

import time
import re
import threading
import urllib.request
from html.parser import HTMLParser
import tkinter as tk
from tkinter import ttk, messagebox, font

# Global Configuration & Defaults
DEFAULT_PASSWORD = "1234"
DISCORD_CONTACT = "_____cooper_____"
POLL_INTERVAL_SEC = 2.0
NORMAL_HOP_WINDOW_SEC = 10.0
CONVERGENCE_WINDOW_SEC = 30.0
TEAM_EXPIRY_SEC = 3600.0  # 1 hour
MIN_MOVEMENT_THRESHOLD = 10
MAX_TRACKED_MOVEMENT = 400


class WorldSnapshot:
    """Represents a parsed snapshot of an OSRS world."""
    def __init__(self, world_id, players, location, activity, is_members):
        self.world_id = int(world_id)
        self.players = int(players)
        self.location = str(location)
        self.activity = str(activity)
        self.is_members = bool(is_members)
        self.timestamp = time.time()


class OSRSWorldHTMLParser(HTMLParser):
    """
    Standard library HTML parser to extract OSRS world telemetry.
    Extracts exact world ID directly from id="slu-world-XXX" attributes.
    """
    def __init__(self, include_f2p=False):
        super().__init__()
        self.include_f2p = include_f2p
        self.worlds = []
        self.current_world_id = None
        self.current_data = []

    def handle_starttag(self, tag, attrs):
        attr_dict = dict(attrs)
        element_id = attr_dict.get('id', '')
        match = re.search(r'slu-world-(\d+)', element_id)
        if match:
            self.current_world_id = int(match.group(1))
            self.current_data = []

    def handle_data(self, data):
        if self.current_world_id is not None:
            self.current_data.append(data)

    def handle_endtag(self, tag):
        if self.current_world_id is not None and tag in ['tr', 'div', 'li']:
            full_text = ' '.join(self.current_data)
            players_match = re.search(r'(\d+)\s+players', full_text, re.IGNORECASE)
            players = int(players_match.group(1)) if players_match else 0

            is_members = "Members" in full_text or "members" in full_text
            location = "US" if "United States" in full_text else ("UK" if "UK" in full_text else "Global")
            activity = "PVP" if "PVP" in full_text else ("Wilderness" if "Wilderness" in full_text else "Standard")

            if is_members or self.include_f2p:
                self.worlds.append(WorldSnapshot(self.current_world_id, players, location, activity, is_members))

            self.current_world_id = None
            self.current_data = []


def parse_osrs_world_list(html_content, include_f2p=False):
    """Parses OSRS world list HTML using regex and standard library parsing."""
    parser = OSRSWorldHTMLParser(include_f2p=include_f2p)
    parser.feed(html_content)
    
    # Fallback to direct Regex parsing if tag structure varies
    if not parser.worlds:
        pattern = r'id="slu-world-(\d+)"[^>]*>(.*?)</(?:tr|div|li)>'
        matches = re.findall(pattern, html_content, re.DOTALL | re.IGNORECASE)
        for world_str, content_str in matches:
            w_id = int(world_str)
            text = re.sub(r'<[^>]+>', ' ', content_str)
            p_match = re.search(r'(\d+)\s+players', text, re.IGNORECASE)
            players = int(p_match.group(1)) if p_match else 0
            is_mem = "Members" in text or "members" in text
            loc = "US" if "United States" in text else ("UK" if "UK" in text else "Global")
            act = "PVP" if "PVP" in text else ("Wilderness" if "Wilderness" in text else "Standard")
            
            if is_mem or include_f2p:
                parser.worlds.append(WorldSnapshot(w_id, players, loc, act, is_mem))

    return parser.worlds


class MovementEvent:
    """Canonical movement event emitted when a qualifying population change occurs."""
    def __init__(self, world_id, delta, start_pop, end_pop, start_time, end_time):
        self.event_id = f"{world_id}_{int(start_time)}_{delta}"
        self.world_id = world_id
        self.delta = delta
        self.start_pop = start_pop
        self.end_pop = end_pop
        self.start_time = start_time
        self.end_time = end_time

    @property
    def magnitude(self):
        return abs(self.delta)

    @property
    def is_outflow(self):
        return self.delta < 0

    @property
    def is_inflow(self):
        return self.delta > 0


class MovementEpisodeTracker:
    """Groups sequential 2-second deltas into continuous movement episodes."""
    def __init__(self, min_threshold=10, max_cap=400, quiet_reset_sec=6.0):
        self.min_threshold = min_threshold
        self.max_cap = max_cap
        self.quiet_reset_sec = quiet_reset_sec
        self.active_episodes = {}
        self.last_seen_pop = {}

    def process_snapshot(self, snapshot):
        w_id = snapshot.world_id
        curr_pop = snapshot.players
        now = snapshot.timestamp
        emitted_events = []

        if w_id not in self.last_seen_pop:
            self.last_seen_pop[w_id] = curr_pop
            return emitted_events

        prev_pop = self.last_seen_pop[w_id]
        delta = curr_pop - prev_pop

        if delta == 0:
            if w_id in self.active_episodes:
                ep = self.active_episodes[w_id]
                if now - ep['last_update'] >= self.quiet_reset_sec:
                    event = self._finalize_episode(w_id)
                    if event:
                        emitted_events.append(event)
            self.last_seen_pop[w_id] = curr_pop
            return emitted_events

        if abs(delta) > self.max_cap:
            self.last_seen_pop[w_id] = curr_pop
            return emitted_events

        if w_id not in self.active_episodes:
            self.active_episodes[w_id] = {
                'start_pop': prev_pop,
                'end_pop': curr_pop,
                'accumulated_delta': delta,
                'start_time': now,
                'last_update': now,
                'direction': 1 if delta > 0 else -1
            }
        else:
            ep = self.active_episodes[w_id]
            curr_dir = 1 if delta > 0 else -1

            if curr_dir != ep['direction']:
                event = self._finalize_episode(w_id)
                if event:
                    emitted_events.append(event)
                self.active_episodes[w_id] = {
                    'start_pop': prev_pop,
                    'end_pop': curr_pop,
                    'accumulated_delta': delta,
                    'start_time': now,
                    'last_update': now,
                    'direction': curr_dir
                }
            else:
                ep['end_pop'] = curr_pop
                ep['accumulated_delta'] += delta
                ep['last_update'] = now

                if abs(ep['accumulated_delta']) >= self.min_threshold:
                    event = self._finalize_episode(w_id)
                    if event:
                        emitted_events.append(event)

        self.last_seen_pop[w_id] = curr_pop
        return emitted_events

    def _finalize_episode(self, w_id):
        if w_id not in self.active_episodes:
            return None

        ep = self.active_episodes.pop(w_id)
        tot_delta = ep['accumulated_delta']

        if abs(tot_delta) >= self.min_threshold and abs(tot_delta) <= self.max_cap:
            return MovementEvent(
                world_id=w_id,
                delta=tot_delta,
                start_pop=ep['start_pop'],
                end_pop=ep['end_pop'],
                start_time=ep['start_time'],
                end_time=ep['last_update']
            )
        return None


class InferredTeam:
    """Represents a persistent inferred group/team tracked across OSRS worlds."""
    def __init__(self, team_id, initial_world, initial_size, confidence="LIKELY"):
        self.team_id = team_id
        self.last_known_world = initial_world
        self.approx_size = initial_size
        self.confidence = confidence
        self.route = [initial_world]
        self.hop_count = 0
        self.last_activity = time.time()
        self.convergences_linked = 0

    def record_hop(self, to_world, observed_size, hop_confidence):
        self.route.append(to_world)
        self.last_known_world = to_world
        self.approx_size = int((self.approx_size * 0.6) + (observed_size * 0.4))
        self.hop_count += 1
        self.last_activity = time.time()

        if self.hop_count >= 3 and self.confidence in ["POSSIBLE", "LIKELY"]:
            self.confidence = "VERY LIKELY"
        elif self.hop_count >= 1 and self.confidence == "POSSIBLE":
            self.confidence = "LIKELY"

    def record_convergence(self, target_world, size, confidence):
        if self.last_known_world != target_world:
            self.route.append(target_world)
            self.last_known_world = target_world
        self.approx_size = int((self.approx_size * 0.5) + (size * 0.5))
        self.convergences_linked += 1
        self.last_activity = time.time()
        self.confidence = "VERY LIKELY"

    def is_expired(self, current_time, expiry_sec=3600.0):
        return (current_time - self.last_activity) > expiry_sec


class HopMatcher:
    """Matches outflow movement events with destination inflow events within a timing window."""
    def __init__(self, window_sec=10.0):
        self.window_sec = window_sec

    def match(self, outflows, inflows):
        matches = []
        for out in outflows:
            for inf in inflows:
                if out.world_id == inf.world_id:
                    continue

                time_diff = abs(inf.start_time - out.start_time)
                if time_diff <= self.window_sec:
                    ratio = min(out.magnitude, inf.magnitude) / max(out.magnitude, inf.magnitude)
                    if ratio >= 0.35:
                        confidence = "VERY LIKELY" if ratio >= 0.75 else ("LIKELY" if ratio >= 0.5 else "POSSIBLE")
                        matches.append({
                            'source_world': out.world_id,
                            'dest_world': inf.world_id,
                            'outflow_size': out.magnitude,
                            'inflow_size': inf.magnitude,
                            'confidence': confidence,
                            'timestamp': max(out.end_time, inf.end_time)
                        })
        return matches


class ConvergenceDetector:
    """Detects multi-world convergence patterns (≥2 distinct sources outflowing into 1 destination)."""
    def __init__(self, window_sec=30.0):
        self.window_sec = window_sec

    def detect(self, outflows, inflows):
        convergences = []
        now = time.time()

        recent_outflows = [o for o in outflows if (now - o.start_time) <= self.window_sec]
        recent_inflows = [i for i in inflows if (now - i.start_time) <= self.window_sec]

        for inf in recent_inflows:
            matching_sources = []
            for out in recent_outflows:
                if out.world_id != inf.world_id:
                    matching_sources.append(out)

            if len(matching_sources) >= 2:
                total_outflow = sum(s.magnitude for s in matching_sources)
                dest_inflow = inf.magnitude
                confidence = "VERY LIKELY" if len(matching_sources) >= 3 else "LIKELY"

                convergences.append({
                    'dest_world': inf.world_id,
                    'dest_inflow': dest_inflow,
                    'sources': [(s.world_id, s.magnitude) for s in matching_sources],
                    'total_outflow': total_outflow,
                    'confidence': confidence,
                    'timestamp': inf.end_time
                })
        return convergences


class LoginWindow(tk.Toplevel):
    """Password authorization window requiring default passcode 1234 to unlock."""
    def __init__(self, parent, on_success):
        super().__init__(parent)
        self.parent = parent
        self.on_success = on_success
        self.title("Authentication - Cake's OSRS World Tracker")
        self.geometry("380x220")
        self.resizable(False, False)
        self.protocol("WM_DELETE_WINDOW", self.parent.destroy)

        self.attributes('-topmost', True)
        self.focus_force()

        ttk.Label(self, text="Cake's OSRS World Tracker", font=("Helvetica", 12, "bold")).pack(pady=10)
        ttk.Label(self, text="Enter Password to Unlock:").pack(pady=2)

        self.entry_pwd = ttk.Entry(self, show="*", width=20)
        self.entry_pwd.pack(pady=5)
        self.entry_pwd.focus_set()
        self.entry_pwd.bind("<Return>", lambda e: self.verify())

        self.lbl_error = ttk.Label(self, text="", foreground="red")
        self.lbl_error.pack(pady=2)

        btn_frame = ttk.Frame(self)
        btn_frame.pack(pady=5)
        ttk.Button(btn_frame, text="Unlock", command=self.verify).pack(side=tk.LEFT, padx=5)
        ttk.Button(btn_frame, text="Exit", command=self.parent.destroy).pack(side=tk.LEFT, padx=5)

        discord_frame = ttk.Frame(self)
        discord_frame.pack(pady=10)
        ttk.Label(discord_frame, text=f"Need password? Contact Discord: {DISCORD_CONTACT}", font=("Helvetica", 8)).pack(side=tk.LEFT)
        ttk.Button(discord_frame, text="Copy", width=5, command=self.copy_discord).pack(side=tk.LEFT, padx=5)

    def verify(self):
        if self.entry_pwd.get() == DEFAULT_PASSWORD:
            self.destroy()
            self.on_success()
        else:
            self.lbl_error.config(text="Incorrect password!")

    def copy_discord(self):
        self.clipboard_clear()
        self.clipboard_append(DISCORD_CONTACT)
        messagebox.showinfo("Copied", "Discord username copied to clipboard!")


class MainGUI(tk.Tk):
    """Primary Tkinter User Interface."""
    def __init__(self):
        super().__init__()
        self.title("Cake's OSRS World Tracker")
        self.geometry("900x600")
        self.withdraw()

        self.include_f2p = False
        self.watched_world_id = None
        self.watched_threshold = MIN_MOVEMENT_THRESHOLD

        self.episode_tracker = MovementEpisodeTracker(min_threshold=MIN_MOVEMENT_THRESHOLD, max_cap=MAX_TRACKED_MOVEMENT)
        self.hop_matcher = HopMatcher(window_sec=NORMAL_HOP_WINDOW_SEC)
        self.convergence_detector = ConvergenceDetector(window_sec=CONVERGENCE_WINDOW_SEC)
        
        self.outflow_history = []
        self.inflow_history = []
        self.active_teams = {}
        self.next_team_id = 1

        self.setup_ui()
        LoginWindow(self, self.on_authenticated)

    def on_authenticated(self):
        self.deiconify()
        self.start_polling_thread()

    def setup_ui(self):
        notebook = ttk.Notebook(self)
        notebook.pack(fill=tk.BOTH, expand=True)

        self.tab_teams = ttk.Frame(notebook)
        self.tab_hops = ttk.Frame(notebook)
        self.tab_convergences = ttk.Frame(notebook)
        self.tab_alerts = ttk.Frame(notebook)

        notebook.add(self.tab_teams, text="ACTIVE TEAMS")
        notebook.add(self.tab_hops, text="MASS HOPS")
        notebook.add(self.tab_convergences, text="CONVERGENCES")
        notebook.add(self.tab_alerts, text="WORLD ALERTS")

        self.tree_teams = ttk.Treeview(self.tab_teams, columns=("ID", "Size", "Confidence", "LastWorld", "Route"), show="headings")
        for col in ("ID", "Size", "Confidence", "LastWorld", "Route"):
            self.tree_teams.heading(col, text=col)
        self.tree_teams.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)

        self.tree_hops = ttk.Treeview(self.tab_hops, columns=("Time", "Source", "Dest", "Outflow", "Inflow", "Confidence"), show="headings")
        for col in ("Time", "Source", "Dest", "Outflow", "Inflow", "Confidence"):
            self.tree_hops.heading(col, text=col)
        self.tree_hops.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)

        self.tree_conv = ttk.Treeview(self.tab_convergences, columns=("Time", "DestWorld", "DestInflow", "Sources", "Confidence"), show="headings")
        for col in ("Time", "DestWorld", "DestInflow", "Sources", "Confidence"):
            self.tree_conv.heading(col, text=col)
        self.tree_conv.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)

        self.tree_alerts = ttk.Treeview(self.tab_alerts, columns=("Time", "World", "Delta", "StartPop", "EndPop"), show="headings")
        for col in ("Time", "World", "Delta", "StartPop", "EndPop"):
            self.tree_alerts.heading(col, text=col)
        self.tree_alerts.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)

    def start_polling_thread(self):
        t = threading.Thread(target=self.poll_loop, daemon=True)
        t.start()

    def poll_loop(self):
        while True:
            try:
                req = urllib.request.Request("https://oldschool.runescape.com/slu", headers={'User-Agent': 'Mozilla/5.0'})
                with urllib.request.urlopen(req, timeout=5) as resp:
                    html = resp.read().decode('utf-8')
                    snapshots = parse_osrs_world_list(html, include_f2p=self.include_f2p)
                    self.process_telemetry(snapshots)
            except Exception:
                pass
            time.sleep(POLL_INTERVAL_SEC)

    def process_telemetry(self, snapshots):
        now = time.time()
        new_events = []
        for s in snapshots:
            events = self.episode_tracker.process_snapshot(s)
            new_events.extend(events)

        if not new_events:
            return

        for ev in new_events:
            self.tree_alerts.insert("", 0, values=(time.strftime("%H:%M:%S"), ev.world_id, ev.delta, ev.start_pop, ev.end_pop))
            if ev.is_outflow:
                self.outflow_history.append(ev)
            else:
                self.inflow_history.append(ev)

        matches = self.hop_matcher.match(self.outflow_history, self.inflow_history)
        for m in matches:
            self.tree_hops.insert("", 0, values=(time.strftime("%H:%M:%S"), m['source_world'], m['dest_world'], f"-{m['outflow_size']}", f"+{m['inflow_size']}", m['confidence']))
            self.associate_hop_to_team(m['source_world'], m['dest_world'], m['inflow_size'], m['confidence'])

        convergences = self.convergence_detector.detect(self.outflow_history, self.inflow_history)
        for c in convergences:
            sources_str = ", ".join([f"W{s[0]}(-{s[1]})" for s in c['sources']])
            self.tree_conv.insert("", 0, values=(time.strftime("%H:%M:%S"), c['dest_world'], f"+{c['dest_inflow']}", sources_str, c['confidence']))
            self.associate_convergence_to_team(c['dest_world'], c['dest_inflow'], c['confidence'])

        expired_ids = [t_id for t_id, t in self.active_teams.items() if t.is_expired(now, TEAM_EXPIRY_SEC)]
        for t_id in expired_ids:
            del self.active_teams[t_id]

        self.refresh_teams_ui()

    def associate_hop_to_team(self, src_world, dest_world, size, confidence):
        matched_team = None
        for team in self.active_teams.values():
            if team.last_known_world == src_world:
                matched_team = team
                break

        if matched_team:
            matched_team.record_hop(dest_world, size, confidence)
        else:
            new_id = f"TEAM #{self.next_team_id}"
            self.next_team_id += 1
            t = InferredTeam(new_id, src_world, size, confidence)
            t.record_hop(dest_world, size, confidence)
            self.active_teams[new_id] = t

    def associate_convergence_to_team(self, dest_world, size, confidence):
        new_id = f"TEAM #{self.next_team_id}"
        self.next_team_id += 1
        t = InferredTeam(new_id, dest_world, size, confidence)
        t.record_convergence(dest_world, size, confidence)
        self.active_teams[new_id] = t

    def refresh_teams_ui(self):
        for item in self.tree_teams.get_children():
            self.tree_teams.delete(item)
        for team in self.active_teams.values():
            route_str = " -> ".join(map(str, team.route))
            self.tree_teams.insert("", tk.END, values=(team.team_id, f"~{team.approx_size}", team.confidence, team.last_known_world, route_str))


if __name__ == "__main__":
    app = MainGUI()
    app.mainloop()
