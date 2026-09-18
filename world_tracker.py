"""
Cake's OSRS World Tracker v22

Purpose:
    Infer PK-team movement from OSRS world population telemetry.

Core model:
    world snapshots -> movement episodes -> canonical movement events
    -> mass-hop matches / convergences / world alerts -> persistent teams

The UI is intentionally only a view of the canonical telemetry state.
It never runs its own movement calculations.
"""

from __future__ import annotations

import html
import queue
import re
import threading
import time
import urllib.request
from collections import deque
from dataclasses import dataclass, field
from html.parser import HTMLParser
import tkinter as tk
from tkinter import ttk, messagebox

try:
    import winsound
except ImportError:  # pragma: no cover - non-Windows development environments
    winsound = None


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

SOURCE_URL = "https://oldschool.runescape.com/slu"
APP_NAME = "Cake's OSRS World Tracker"
APP_PASSWORD = "1234"

POLL_INTERVAL = 2.0
NORMAL_HOP_WINDOW = 10.0
CONVERGENCE_WINDOW = 30.0
TEAM_HISTORY = 60 * 60
MAX_TRACKED_MOVEMENT = 400
EPISODE_MAX_SECONDS = 10.0
EPISODE_QUIET_SECONDS = 6.0
MAX_EVENTS = 600
MAX_TEAMS = 100
NETWORK_TIMEOUT = 8

DEFAULT_MIN_MOVEMENT = 10
DEFAULT_WATCH_THRESHOLD = 10
DEFAULT_MIN_HOP_SCORE = 45
DEFAULT_SOUND_ALERTS = False


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class World:
    world: int
    players: int
    location: str
    membership: str
    activity: str
    timestamp: float


@dataclass
class MovementEvent:
    """One authoritative movement episode.

    event_id remains stable while the episode grows, allowing every consumer
    (alerts, hops, convergence and team tracking) to refer to the same event.
    """

    event_id: str
    world: int
    amount: int
    start_pop: int
    end_pop: int
    start_time: float
    end_time: float
    active: bool = True

    @property
    def magnitude(self) -> int:
        return abs(self.amount)

    @property
    def is_outflow(self) -> bool:
        return self.amount < 0

    @property
    def is_inflow(self) -> bool:
        return self.amount > 0


@dataclass
class Hop:
    key: tuple
    source_event_id: str
    destination_event_id: str
    source: int
    destination: int
    left: int
    appeared: int
    score: int
    timestamp: float

    @property
    def moved(self) -> int:
        return max(self.left, self.appeared)


@dataclass
class Convergence:
    key: tuple
    destination_event_id: str
    source_event_ids: tuple[str, ...]
    destination: int
    sources: tuple[int, ...]
    source_amounts: tuple[int, ...]
    appeared: int
    score: int
    timestamp: float

    @property
    def source_count(self) -> int:
        return len(self.sources)

    @property
    def total_outflow(self) -> int:
        return sum(self.source_amounts)


@dataclass
class TeamTrack:
    team_id: str
    created_at: float
    last_activity: float
    last_world: int | None = None
    route: list[int] = field(default_factory=list)
    hop_keys: set[tuple] = field(default_factory=set)
    convergence_keys: set[tuple] = field(default_factory=set)
    hop_scores: list[int] = field(default_factory=list)
    hop_sizes: list[int] = field(default_factory=list)
    convergence_scores: list[int] = field(default_factory=list)
    convergence_sizes: list[int] = field(default_factory=list)
    support: int = 0

    @property
    def age(self) -> float:
        return max(0.0, time.time() - self.last_activity)

    @property
    def hop_count(self) -> int:
        return len(self.hop_keys)

    @property
    def convergence_count(self) -> int:
        return len(self.convergence_keys)

    @property
    def approx_size(self) -> int:
        values = self.hop_sizes[-8:] + self.convergence_sizes[-4:]
        return max(1, round(sum(values) / len(values))) if values else 1

    @property
    def confidence_score(self) -> int:
        hop_component = (
            sum(self.hop_scores[-8:]) / len(self.hop_scores[-8:])
            if self.hop_scores
            else 0.0
        )
        conv_component = (
            sum(self.convergence_scores[-4:]) / len(self.convergence_scores[-4:])
            if self.convergence_scores
            else 0.0
        )

        if hop_component and conv_component:
            score = hop_component * 0.68 + conv_component * 0.32
        else:
            score = max(hop_component, conv_component)

        sizes = self.hop_sizes[-8:] + self.convergence_sizes[-4:]
        consistency = 1.0
        if len(sizes) >= 2:
            mean = sum(sizes) / len(sizes)
            deviation = sum(abs(v - mean) for v in sizes) / len(sizes)
            consistency = max(0.0, 1.0 - deviation / max(1.0, mean))

        score += consistency * 12
        score += min(20, max(0, self.hop_count - 1) * 5)
        score += min(12, self.convergence_count * 4)
        score += min(10, self.support * 2)

        # Multiple independent pieces of evidence are required before very high
        # confidence is possible.
        if self.hop_count + self.convergence_count >= 4:
            score += 6
        if self.hop_count >= 2 and consistency >= 0.75:
            score += 5

        return min(99, max(0, round(score)))

    @property
    def likelihood(self) -> str:
        return likelihood_label(self.confidence_score)

    def is_expired(self, now: float) -> bool:
        return now - self.last_activity > TEAM_HISTORY

    def record_hop(self, hop: Hop, evidence_bonus: int = 0) -> None:
        if hop.key in self.hop_keys:
            self.last_activity = max(self.last_activity, hop.timestamp)
            self.support += max(0, evidence_bonus)
            return

        self.hop_keys.add(hop.key)
        self.hop_scores.append(hop.score)
        self.hop_sizes.append(hop.moved)
        self.support += max(1, evidence_bonus)
        self.last_activity = max(self.last_activity, hop.timestamp)

        if not self.route:
            self.route = [hop.source]
        elif self.route[-1] != hop.source:
            self.route.append(hop.source)
        self.route.append(hop.destination)
        self.last_world = hop.destination

    def update_existing_hop(self, hop: Hop) -> None:
        if hop.key not in self.hop_keys:
            return
        if self.hop_scores:
            self.hop_scores[-1] = max(self.hop_scores[-1], hop.score)
        if self.hop_sizes:
            self.hop_sizes[-1] = round((self.hop_sizes[-1] + hop.moved) / 2)
        self.last_activity = max(self.last_activity, hop.timestamp)

    def record_convergence(self, convergence: Convergence, evidence_bonus: int = 1) -> None:
        if convergence.key in self.convergence_keys:
            self.last_activity = max(self.last_activity, convergence.timestamp)
            return

        self.convergence_keys.add(convergence.key)
        self.convergence_scores.append(convergence.score)
        self.convergence_sizes.append(convergence.appeared)
        self.support += max(1, evidence_bonus)
        self.last_activity = max(self.last_activity, convergence.timestamp)

        if self.last_world != convergence.destination:
            self.route.append(convergence.destination)
            self.last_world = convergence.destination


@dataclass
class EpisodeState:
    direction: int
    anchor_pop: int
    start_pop: int
    current_pop: int
    start_time: float
    last_change_time: float
    event_id: str | None = None


# ---------------------------------------------------------------------------
# Utility helpers
# ---------------------------------------------------------------------------


def likelihood_label(score: int) -> str:
    if score >= 90:
        return "VERY LIKELY"
    if score >= 75:
        return "LIKELY"
    if score >= 50:
        return "POSSIBLE"
    return "UNLIKELY"


def likelihood_tag(score: int) -> str:
    if score >= 90:
        return "very"
    if score >= 75:
        return "likely"
    if score >= 50:
        return "possible"
    return "unlikely"


def ratio_score(a: int, b: int) -> float:
    if a <= 0 or b <= 0:
        return 0.0
    return min(a, b) / max(a, b)


def score_hop(source: MovementEvent, destination: MovementEvent) -> int:
    age = abs(source.end_time - destination.end_time)
    if age > NORMAL_HOP_WINDOW:
        return 0

    ratio = ratio_score(source.magnitude, destination.magnitude)
    timing = 1.0 - age / NORMAL_HOP_WINDOW
    size = max(source.magnitude, destination.magnitude)

    score = 47 * ratio + 35 * timing + min(18.0, size / 25.0 * 18.0)
    if ratio < 0.35:
        score -= 25
    elif ratio < 0.50:
        score -= 18
    elif ratio < 0.65:
        score -= 9
    elif ratio < 0.80:
        score -= 4

    if size < 8:
        score -= 4

    return min(99, max(0, round(score)))


# ---------------------------------------------------------------------------
# World list parser
# ---------------------------------------------------------------------------


class WorldParser(HTMLParser):
    """Parse the official world table using the actual slu-world-XXX ID."""

    def __init__(self, include_f2p: bool = False):
        super().__init__()
        self.include_f2p = include_f2p
        self.in_row = False
        self.in_cell = False
        self.row: list[str] = []
        self.cell: list[str] = []
        self.world_id: int | None = None
        self.rows: list[tuple[int, list[str]]] = []

    def handle_starttag(self, tag: str, attrs):
        tag = tag.lower()
        attr_map = dict(attrs)

        if tag == "tr":
            self.in_row = True
            self.in_cell = False
            self.row = []
            self.cell = []
            self.world_id = None
            return

        if not self.in_row:
            return

        if tag in ("td", "th"):
            self.in_cell = True
            self.cell = []
            return

        if tag == "a":
            ident = attr_map.get("id", "")
            match = re.fullmatch(r"slu-world-(\d+)", ident)
            if match:
                self.world_id = int(match.group(1))

    def handle_data(self, data: str):
        if self.in_cell:
            self.cell.append(data)

    def handle_endtag(self, tag: str):
        tag = tag.lower()
        if tag in ("td", "th") and self.in_cell:
            text = re.sub(r"\s+", " ", html.unescape("".join(self.cell))).strip()
            self.row.append(text)
            self.in_cell = False
            self.cell = []
            return

        if tag == "tr" and self.in_row:
            if self.world_id is not None and self.row:
                self.rows.append((self.world_id, list(self.row)))
            self.in_row = False
            self.in_cell = False
            self.row = []
            self.cell = []
            self.world_id = None


def parse_world_html(content: str, include_f2p: bool = False) -> list[World]:
    parser = WorldParser(include_f2p=include_f2p)
    parser.feed(content)
    now = time.time()
    worlds: dict[int, World] = {}

    for world_id, row in parser.rows:
        # The current OSRS table layout puts player count in the second cell,
        # membership in the fourth, and activity in the fifth. Search broadly
        # for player count so harmless table layout changes are less brittle.
        text = " | ".join(row)
        players_match = re.search(r"([\d,]+)\s+players?", text, re.IGNORECASE)
        if not players_match or len(row) < 4:
            continue

        players = int(players_match.group(1).replace(",", ""))
        membership = row[3].strip() if len(row) > 3 else ""
        if membership not in ("Members", "Free"):
            continue
        if membership == "Free" and not include_f2p:
            continue

        location = row[2].strip() if len(row) > 2 else "-"
        activity = row[4].strip() if len(row) > 4 and row[4].strip() else "-"

        worlds[world_id] = World(
            world=world_id,
            players=players,
            location=location,
            membership=membership,
            activity=activity,
            timestamp=now,
        )

    if not worlds:
        raise RuntimeError("No OSRS worlds could be parsed from the official world list.")

    return sorted(worlds.values(), key=lambda item: item.world)


def fetch_worlds(include_f2p: bool) -> list[World]:
    request = urllib.request.Request(
        SOURCE_URL,
        headers={"User-Agent": "Cakes-OSRS-World-Tracker/9.0"},
    )
    with urllib.request.urlopen(request, timeout=NETWORK_TIMEOUT) as response:
        content = response.read().decode("utf-8", "replace")
    return parse_world_html(content, include_f2p=include_f2p)


# ---------------------------------------------------------------------------
# Movement episode tracker
# ---------------------------------------------------------------------------


class MovementEpisodeTracker:
    """Turn noisy 2-second population changes into stable movement episodes."""

    def __init__(self):
        self.last_pop: dict[int, int] = {}
        self.episodes: dict[int, EpisodeState] = {}
        self.next_event_number = 1

    def reset(self) -> None:
        self.last_pop.clear()
        self.episodes.clear()
        self.next_event_number = 1

    def _new_event_id(self, world: int) -> str:
        value = f"M{world}-{self.next_event_number}"
        self.next_event_number += 1
        return value

    def update(
        self,
        current_worlds: dict[int, World],
        event_store: dict[str, MovementEvent],
        event_order: deque[str],
        min_threshold_for_world,
    ) -> list[str]:
        """Update all worlds and return event IDs whose state changed/appeared."""

        now = time.time()
        changed: list[str] = []

        # Handle worlds present in this snapshot.
        for world_id, snapshot in current_worlds.items():
            current = snapshot.players
            previous = self.last_pop.get(world_id)

            if previous is None:
                self.last_pop[world_id] = current
                continue

            delta = current - previous
            episode = self.episodes.get(world_id)
            threshold = min(MAX_TRACKED_MOVEMENT, max(1, int(min_threshold_for_world(world_id))))

            # Do not let a continuously changing population become one
            # unbounded episode. After the normal 10-second movement window,
            # close the old episode and let a fresh one begin.
            if episode and now - episode.start_time > EPISODE_MAX_SECONDS:
                if episode.event_id and episode.event_id in event_store:
                    event_store[episode.event_id].active = False
                    event_store[episode.event_id].end_time = episode.last_change_time
                    changed.append(episode.event_id)
                self.episodes.pop(world_id, None)
                episode = None

            # A very large swing is treated as an unreliable snapshot/reset.
            if abs(delta) > MAX_TRACKED_MOVEMENT:
                self.episodes.pop(world_id, None)
                self.last_pop[world_id] = current
                continue

            if delta == 0:
                if episode and now - episode.last_change_time >= EPISODE_QUIET_SECONDS:
                    if episode.event_id and episode.event_id in event_store:
                        event_store[episode.event_id].active = False
                        event_store[episode.event_id].end_time = episode.last_change_time
                        changed.append(episode.event_id)
                    self.episodes.pop(world_id, None)
                self.last_pop[world_id] = current
                continue

            direction = 1 if delta > 0 else -1

            # A direction reversal ends the old episode and starts a fresh one.
            if episode and direction != episode.direction:
                if episode.event_id and episode.event_id in event_store:
                    event_store[episode.event_id].active = False
                    event_store[episode.event_id].end_time = episode.last_change_time
                    changed.append(episode.event_id)
                episode = None
                self.episodes.pop(world_id, None)

            if episode is None:
                episode = EpisodeState(
                    direction=direction,
                    anchor_pop=previous,
                    start_pop=previous,
                    current_pop=current,
                    start_time=now,
                    last_change_time=now,
                )
                self.episodes[world_id] = episode
            else:
                episode.current_pop = current
                episode.last_change_time = now

            net = current - episode.anchor_pop

            if abs(net) > MAX_TRACKED_MOVEMENT:
                if episode.event_id and episode.event_id in event_store:
                    event_store.pop(episode.event_id, None)
                    try:
                        event_order.remove(episode.event_id)
                    except ValueError:
                        pass
                self.episodes.pop(world_id, None)
                self.last_pop[world_id] = current
                continue

            if abs(net) >= threshold:
                if episode.event_id is None:
                    event_id = self._new_event_id(world_id)
                    episode.event_id = event_id
                    event_store[event_id] = MovementEvent(
                        event_id=event_id,
                        world=world_id,
                        amount=net,
                        start_pop=episode.start_pop,
                        end_pop=current,
                        start_time=episode.start_time,
                        end_time=now,
                        active=True,
                    )
                    event_order.appendleft(event_id)
                    changed.append(event_id)
                else:
                    event = event_store.get(episode.event_id)
                    if event:
                        # Update the same canonical event as the movement grows.
                        event.amount = net
                        event.end_pop = current
                        event.end_time = now
                        event.active = True
                        changed.append(event.event_id)

            self.last_pop[world_id] = current

        # Finalize episodes for worlds that disappeared from a snapshot. We do
        # not manufacture movement because a missing world is ambiguous.
        missing = set(self.last_pop) - set(current_worlds)
        for world_id in missing:
            episode = self.episodes.get(world_id)
            if episode and episode.event_id and now - episode.last_change_time >= EPISODE_QUIET_SECONDS:
                event = event_store.get(episode.event_id)
                if event:
                    event.active = False
                    event.end_time = episode.last_change_time
                    changed.append(event.event_id)
                self.episodes.pop(world_id, None)
            self.last_pop.pop(world_id, None)

        return list(dict.fromkeys(changed))


# ---------------------------------------------------------------------------
# Central telemetry engine
# ---------------------------------------------------------------------------


class TelemetryEngine:
    """Single source of truth for movement, hops, convergences, alerts and teams."""

    def __init__(self):
        self.episode_tracker = MovementEpisodeTracker()
        self.events: dict[str, MovementEvent] = {}
        self.event_order: deque[str] = deque()
        self.hops: dict[tuple, Hop] = {}
        self.hop_order: deque[tuple] = deque()
        self.convergences: dict[tuple, Convergence] = {}
        self.convergence_order: deque[tuple] = deque()
        self.teams: dict[str, TeamTrack] = {}
        self.hop_team_map: dict[tuple, str] = {}
        self.convergence_team_map: dict[tuple, str] = {}
        self.next_team_id = 1
        self.alerted_event_ids: set[str] = set()
        self.sound_alerted_hop_keys: set[tuple] = set()
        self.sound_alerted_convergence_keys: set[tuple] = set()

        self.min_group = DEFAULT_MIN_MOVEMENT
        self.world_alert_threshold = DEFAULT_MIN_MOVEMENT
        self.watch_world: int | None = None
        self.watch_threshold = DEFAULT_WATCH_THRESHOLD
        self.min_hop_score = DEFAULT_MIN_HOP_SCORE

    def reset(self) -> None:
        self.episode_tracker.reset()
        self.events.clear()
        self.event_order.clear()
        self.hops.clear()
        self.hop_order.clear()
        self.convergences.clear()
        self.convergence_order.clear()
        self.teams.clear()
        self.hop_team_map.clear()
        self.convergence_team_map.clear()
        self.next_team_id = 1
        self.alerted_event_ids.clear()
        self.sound_alerted_hop_keys.clear()
        self.sound_alerted_convergence_keys.clear()

    def set_settings(
        self,
        min_group: int,
        world_alert_threshold: int,
        watch_world: int | None,
        watch_threshold: int,
        min_hop_score: int = DEFAULT_MIN_HOP_SCORE,
    ) -> None:
        self.min_group = max(1, min(MAX_TRACKED_MOVEMENT, int(min_group)))
        self.world_alert_threshold = max(1, min(MAX_TRACKED_MOVEMENT, int(world_alert_threshold)))
        self.watch_world = watch_world
        self.watch_threshold = max(1, min(MAX_TRACKED_MOVEMENT, int(watch_threshold)))
        self.min_hop_score = max(0, min(99, int(min_hop_score)))

    def threshold_for_world(self, world_id: int) -> int:
        if self.watch_world == world_id:
            return min(self.min_group, self.watch_threshold)
        return self.min_group

    def ingest(self, worlds: list[World]) -> dict:
        current = {item.world: item for item in worlds}
        changed_ids = self.episode_tracker.update(
            current,
            self.events,
            self.event_order,
            self.threshold_for_world,
        )

        self._prune_old_data()
        self._recompute_hops()
        self._recompute_convergences()
        self._update_teams()

        active_alerts = self._alerts()
        return {
            "worlds": worlds,
            "changed_events": changed_ids,
            "alerts": active_alerts,
            "hops": self._hops_list(),
            "convergences": self._convergence_list(),
            "teams": self._teams_list(),
        }

    def _prune_old_data(self) -> None:
        cutoff = time.time() - max(CONVERGENCE_WINDOW, NORMAL_HOP_WINDOW, TEAM_HISTORY)
        # Movement events are kept for up to one hour and bounded explicitly.
        stale_events = [event_id for event_id, event in self.events.items() if event.end_time < cutoff]
        for event_id in stale_events:
            self.events.pop(event_id, None)
            try:
                self.event_order.remove(event_id)
            except ValueError:
                pass

        while len(self.event_order) > MAX_EVENTS:
            event_id = self.event_order.pop()
            event = self.events.get(event_id)
            if event is not None and event.active:
                # Keep an active event even if the cap is reached; it is part of
                # the live canonical stream. Try the next oldest item instead.
                self.event_order.appendleft(event_id)
                break
            self.events.pop(event_id, None)

        stale_hops = [key for key, hop in self.hops.items() if hop.timestamp < cutoff]
        for key in stale_hops:
            self.hops.pop(key, None)

        stale_convergences = [key for key, item in self.convergences.items() if item.timestamp < cutoff]
        for key in stale_convergences:
            self.convergences.pop(key, None)

        now = time.time()
        expired = [team_id for team_id, team in self.teams.items() if team.is_expired(now)]
        for team_id in expired:
            self.teams.pop(team_id, None)

    def _recent_events(self, window: float) -> list[MovementEvent]:
        cutoff = time.time() - window
        result = []
        for event_id in self.event_order:
            event = self.events.get(event_id)
            if event and event.end_time >= cutoff:
                result.append(event)
        return result

    def _recompute_hops(self) -> None:
        recent = self._recent_events(NORMAL_HOP_WINDOW)
        drops = [event for event in recent if event.is_outflow and event.magnitude >= self.min_group]
        gains = [event for event in recent if event.is_inflow and event.magnitude >= self.min_group]

        candidates: list[tuple[int, MovementEvent, MovementEvent]] = []
        for source in drops:
            for destination in gains:
                if source.world == destination.world:
                    continue
                score = score_hop(source, destination)
                if score >= self.min_hop_score:
                    candidates.append((score, source, destination))

        # One source event and one destination event can only represent one hop.
        candidates.sort(
            key=lambda item: (
                item[0],
                min(item[1].magnitude, item[2].magnitude),
                -abs(item[1].end_time - item[2].end_time),
            ),
            reverse=True,
        )

        used_sources: set[str] = set()
        used_destinations: set[str] = set()
        seen_keys: set[tuple] = set()

        for score, source, destination in candidates:
            if source.event_id in used_sources or destination.event_id in used_destinations:
                continue

            key = (source.event_id, destination.event_id)
            seen_keys.add(key)
            hop = self.hops.get(key)
            timestamp = max(source.end_time, destination.end_time)
            if hop is None:
                hop = Hop(
                    key=key,
                    source_event_id=source.event_id,
                    destination_event_id=destination.event_id,
                    source=source.world,
                    destination=destination.world,
                    left=source.magnitude,
                    appeared=destination.magnitude,
                    score=score,
                    timestamp=timestamp,
                )
                self.hops[key] = hop
                self.hop_order.appendleft(key)
            else:
                hop.left = source.magnitude
                hop.appeared = destination.magnitude
                hop.score = score
                hop.timestamp = timestamp

            used_sources.add(source.event_id)
            used_destinations.add(destination.event_id)

        # Remove pairs that are no longer within the matching window.
        for key in list(self.hops):
            if key not in seen_keys:
                hop = self.hops[key]
                if hop.timestamp < time.time() - NORMAL_HOP_WINDOW:
                    self.hops.pop(key, None)

    def _recompute_convergences(self) -> None:
        recent = self._recent_events(CONVERGENCE_WINDOW)
        drops = [event for event in recent if event.is_outflow and event.magnitude >= self.min_group]
        gains = [event for event in recent if event.is_inflow and event.magnitude >= self.min_group]
        seen_keys: set[tuple] = set()

        for destination in gains:
            candidates = []
            for source in drops:
                if source.world == destination.world:
                    continue
                age = abs(source.end_time - destination.end_time)
                if age > CONVERGENCE_WINDOW:
                    continue
                ratio = ratio_score(source.magnitude, destination.magnitude)
                # A source that is less than roughly one third the observed
                # destination gain is too weak to be useful as convergence
                # evidence; excluding it prevents random world noise from
                # creating huge source lists.
                if ratio < 0.35:
                    continue
                timing = 1.0 - age / CONVERGENCE_WINDOW
                quality = ratio * 0.62 + timing * 0.38
                candidates.append((quality, source, ratio, timing))

            # At most one contribution per source world; choose the strongest.
            best_by_world: dict[int, tuple] = {}
            for candidate in candidates:
                _, source, _, _ = candidate
                old = best_by_world.get(source.world)
                if old is None or candidate[0] > old[0]:
                    best_by_world[source.world] = candidate

            chosen = sorted(
                best_by_world.values(),
                key=lambda item: (item[0], item[1].magnitude),
                reverse=True,
            )

            if len(chosen) < 2:
                continue

            # Avoid treating a tiny destination gain as strong confirmation of a
            # huge aggregate source outflow.
            sizes = [item[1].magnitude for item in chosen]
            median = sorted(sizes)[len(sizes) // 2]
            consistency = 1.0 - sum(abs(size - median) for size in sizes) / max(1, sum(sizes))
            avg_ratio = sum(item[2] for item in chosen) / len(chosen)
            avg_timing = sum(item[3] for item in chosen) / len(chosen)
            coverage = min(1.0, destination.magnitude / max(1, sum(sizes)))
            if coverage < 0.18:
                continue

            score = (
                35
                + 25 * avg_ratio
                + 18 * avg_timing
                + 12 * max(0.0, min(1.0, consistency))
                + min(18, (len(chosen) - 2) * 7)
                + 8 * coverage
            )
            if len(chosen) >= 3:
                score += 7

            score = min(99, max(0, round(score)))
            sources = tuple(item[1].world for item in chosen)
            source_event_ids = tuple(item[1].event_id for item in chosen)
            key = (destination.event_id, tuple(sorted(source_event_ids)))
            seen_keys.add(key)

            convergence = self.convergences.get(key)
            if convergence is None:
                convergence = Convergence(
                    key=key,
                    destination_event_id=destination.event_id,
                    source_event_ids=source_event_ids,
                    destination=destination.world,
                    sources=sources,
                    source_amounts=tuple(item[1].magnitude for item in chosen),
                    appeared=destination.magnitude,
                    score=score,
                    timestamp=max([destination.end_time] + [item[1].end_time for item in chosen]),
                )
                self.convergences[key] = convergence
                self.convergence_order.appendleft(key)
            else:
                convergence.sources = sources
                convergence.source_event_ids = source_event_ids
                convergence.source_amounts = tuple(item[1].magnitude for item in chosen)
                convergence.appeared = destination.magnitude
                convergence.score = score
                convergence.timestamp = max([destination.end_time] + [item[1].end_time for item in chosen])

        for key in list(self.convergences):
            if key not in seen_keys and self.convergences[key].timestamp < time.time() - CONVERGENCE_WINDOW:
                self.convergences.pop(key, None)

    def _update_teams(self) -> None:
        now = time.time()

        # First apply convergences. A convergence says that a group is likely at
        # the destination, so it should reinforce a compatible team already there
        # instead of creating a duplicate team every time the detector recomputes.
        for convergence in self._convergence_list():
            team_id = self.convergence_team_map.get(convergence.key)
            existing = self.teams.get(team_id) if team_id else None
            if existing is None or existing.is_expired(now):
                existing = self._best_team_for_location(
                    convergence.destination,
                    convergence.appeared,
                    convergence.timestamp,
                )
            if existing is None:
                existing = self._create_team(
                    initial_world=convergence.destination,
                    initial_size=max(1, convergence.appeared),
                    timestamp=convergence.timestamp,
                )
            self.convergence_team_map[convergence.key] = existing.team_id
            existing.record_convergence(
                convergence,
                evidence_bonus=max(1, convergence.source_count - 1),
            )

        # Then apply mass hops. If a recent convergence already identifies the
        # destination as an active team location, use that team as supporting
        # evidence rather than creating a second team whose route runs backward.
        for hop in self._hops_list():
            team_id = self.hop_team_map.get(hop.key)
            team = self.teams.get(team_id) if team_id else None
            if team is None or team.is_expired(now):
                team = self._best_team_for_hop(hop)

            if team is None:
                related = self._best_convergence_team_for_destination(hop.destination, hop.moved, hop.timestamp)
                if related is not None:
                    team = related
                    self.hop_team_map[hop.key] = team.team_id
                    self._record_hop_support(team, hop)
                    continue

            if team is None:
                team = self._create_team(
                    initial_world=hop.source,
                    initial_size=hop.moved,
                    timestamp=hop.timestamp,
                )
            self.hop_team_map[hop.key] = team.team_id
            if hop.key not in team.hop_keys:
                team.record_hop(hop, evidence_bonus=1 if hop.score >= 75 else 0)
            else:
                team.update_existing_hop(hop)

        for team_id, team in list(self.teams.items()):
            if team.is_expired(now):
                self.teams.pop(team_id, None)

    def _best_convergence_team_for_destination(self, destination: int, size: int, timestamp: float) -> TeamTrack | None:
        candidates: list[tuple[float, TeamTrack]] = []
        for convergence in self._convergence_list():
            if convergence.destination != destination:
                continue
            if abs(convergence.timestamp - timestamp) > CONVERGENCE_WINDOW:
                continue
            team_id = self.convergence_team_map.get(convergence.key)
            team = self.teams.get(team_id) if team_id else None
            if team is None or team.is_expired(time.time()):
                continue
            size_ratio = ratio_score(max(1, convergence.appeared), max(1, size))
            recency = max(0.0, 1.0 - abs(convergence.timestamp - timestamp) / CONVERGENCE_WINDOW)
            score = size_ratio * 70 + convergence.score * 0.2 + recency * 10
            candidates.append((score, team))
        return max(candidates, key=lambda item: item[0])[1] if candidates else None

    @staticmethod
    def _record_hop_support(team: TeamTrack, hop: Hop) -> None:
        if hop.key in team.hop_keys:
            team.update_existing_hop(hop)
            return
        team.hop_keys.add(hop.key)
        team.hop_scores.append(hop.score)
        team.hop_sizes.append(hop.moved)
        team.support += 1 if hop.score >= 75 else 0
        team.last_activity = max(team.last_activity, hop.timestamp)

    def _create_team(self, initial_world: int, initial_size: int, timestamp: float) -> TeamTrack:
        team_id = f"TEAM #{self.next_team_id}"
        self.next_team_id += 1
        team = TeamTrack(
            team_id=team_id,
            created_at=timestamp,
            last_activity=timestamp,
            last_world=initial_world,
            route=[initial_world],
        )
        team.hop_sizes.append(max(1, initial_size))
        self.teams[team_id] = team
        return team

    def _best_team_for_location(
        self,
        world: int,
        size: int,
        timestamp: float,
        allow_recent_sources: bool = False,
    ) -> TeamTrack | None:
        candidates: list[tuple[float, TeamTrack]] = []
        for team in self.teams.values():
            if team.last_world != world:
                continue
            if timestamp - team.last_activity > TEAM_HISTORY:
                continue
            size_ratio = ratio_score(team.approx_size, max(1, size))
            time_score = max(0.0, 1.0 - (timestamp - team.last_activity) / TEAM_HISTORY)
            score = size_ratio * 75 + time_score * 15 + min(10, team.support * 2)
            candidates.append((score, team))

        if not candidates:
            return None
        return max(candidates, key=lambda item: item[0])[1]

    def _best_team_for_hop(self, hop: Hop) -> TeamTrack | None:
        candidates: list[tuple[float, TeamTrack]] = []
        for team in self.teams.values():
            if team.last_world != hop.source:
                continue
            delta_t = max(0.0, hop.timestamp - team.last_activity)
            if delta_t > TEAM_HISTORY:
                continue
            size_ratio = ratio_score(team.approx_size, hop.moved)
            if size_ratio < 0.35:
                continue
            recency = max(0.0, 1.0 - delta_t / min(TEAM_HISTORY, 900.0))
            score = size_ratio * 60 + hop.score * 0.25 + recency * 15
            score += min(12, team.support * 2)
            if team.last_world == hop.source:
                score += 8
            candidates.append((score, team))

        if not candidates:
            return None
        return max(candidates, key=lambda item: item[0])[1]

    def _alerts(self) -> list[MovementEvent]:
        result = []
        for event_id in self.event_order:
            event = self.events.get(event_id)
            if not event:
                continue
            threshold = self.world_alert_threshold
            if self.watch_world == event.world:
                threshold = min(threshold, self.watch_threshold)
            if event.magnitude >= threshold:
                result.append(event)
        return result

    def _hops_list(self) -> list[Hop]:
        return sorted(self.hops.values(), key=lambda item: item.timestamp, reverse=True)

    def _convergence_list(self) -> list[Convergence]:
        return sorted(self.convergences.values(), key=lambda item: item.timestamp, reverse=True)

    def _teams_list(self) -> list[TeamTrack]:
        return sorted(
            self.teams.values(),
            key=lambda team: (team.confidence_score, team.last_activity),
            reverse=True,
        )[:MAX_TEAMS]

    def should_sound_for_event(self, event_id: str) -> bool:
        if event_id in self.alerted_event_ids:
            return False
        self.alerted_event_ids.add(event_id)
        return True

    def should_sound_for_hop(self, key: tuple) -> bool:
        if key in self.sound_alerted_hop_keys:
            return False
        self.sound_alerted_hop_keys.add(key)
        return True

    def should_sound_for_convergence(self, key: tuple) -> bool:
        if key in self.sound_alerted_convergence_keys:
            return False
        self.sound_alerted_convergence_keys.add(key)
        return True


# ---------------------------------------------------------------------------
# UI
# ---------------------------------------------------------------------------


class LoginWindow(tk.Toplevel):
    def __init__(self, parent, on_success):
        super().__init__(parent)
        self.parent = parent
        self.on_success = on_success
        self.title(f"Unlock - {APP_NAME}")
        self.geometry("420x255")
        self.resizable(False, False)
        self.configure(bg="#0b1018")
        self.protocol("WM_DELETE_WINDOW", parent.destroy)
        self.attributes("-topmost", True)

        frame = tk.Frame(self, bg="#0b1018", padx=24, pady=22)
        frame.pack(fill="both", expand=True)

        tk.Label(
            frame,
            text=APP_NAME,
            font=("Segoe UI", 16, "bold"),
            fg="#f4f7fb",
            bg="#0b1018",
        ).pack(anchor="w")
        tk.Label(
            frame,
            text="Enter the password to start telemetry tracking.",
            font=("Segoe UI", 9),
            fg="#9eabc0",
            bg="#0b1018",
        ).pack(anchor="w", pady=(5, 18))

        self.password = tk.StringVar()
        self.entry = tk.Entry(
            frame,
            textvariable=self.password,
            show="*",
            font=("Segoe UI", 12),
            bg="#182235",
            fg="#edf2f9",
            insertbackground="#edf2f9",
            relief="flat",
            justify="center",
        )
        self.entry.pack(fill="x", ipady=7)
        self.entry.bind("<Return>", lambda _event: self.verify())

        self.error = tk.StringVar(value="")
        tk.Label(
            frame,
            textvariable=self.error,
            font=("Segoe UI", 9, "bold"),
            fg="#ff6872",
            bg="#0b1018",
        ).pack(anchor="w", pady=(7, 0))

        row = tk.Frame(frame, bg="#0b1018")
        row.pack(fill="x", pady=(14, 0))
        tk.Button(
            row,
            text="UNLOCK",
            command=self.verify,
            font=("Segoe UI", 9, "bold"),
            bg="#7c4dff",
            fg="white",
            activebackground="#966eff",
            activeforeground="white",
            relief="flat",
            padx=18,
            pady=7,
        ).pack(side="right", padx=(7, 0))
        tk.Button(
            row,
            text="EXIT",
            command=self.parent.destroy,
            font=("Segoe UI", 9),
            bg="#182235",
            fg="#dfe7f3",
            activebackground="#293953",
            activeforeground="white",
            relief="flat",
            padx=18,
            pady=7,
        ).pack(side="right")

        self.update_idletasks()
        self.lift()
        self.focus_force()
        self.entry.focus_force()
        self.after(250, lambda: self.attributes("-topmost", False))

    def verify(self) -> None:
        if self.password.get() == APP_PASSWORD:
            self.on_success()
            self.destroy()
        else:
            self.password.set("")
            self.error.set("Incorrect password.")
            self.entry.focus_force()


class MainWindow(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(APP_NAME)
        self.geometry("1250x760")
        self.minsize(1000, 650)
        self.configure(bg="#0b1018")
        self.withdraw()

        self.engine = TelemetryEngine()
        self.include_f2p = tk.BooleanVar(value=False)
        self.min_group_var = tk.IntVar(value=DEFAULT_MIN_MOVEMENT)
        self.world_alert_var = tk.IntVar(value=DEFAULT_MIN_MOVEMENT)
        self.watch_world_var = tk.StringVar(value="")
        self.watch_threshold_var = tk.IntVar(value=DEFAULT_WATCH_THRESHOLD)
        self.sound_alerts_var = tk.BooleanVar(value=DEFAULT_SOUND_ALERTS)

        self.data_queue: queue.Queue[tuple[str, object]] = queue.Queue()
        self.stop_event = threading.Event()
        self.poll_thread: threading.Thread | None = None
        self.poll_include_f2p = False
        self.poll_in_progress = False
        self.last_worlds: list[World] = []
        self.last_state: dict | None = None
        self.snapshots_count = 0
        self.view = "teams"

        self._build_style()
        self._build_ui()
        self._show_login()
        self.after(100, self._drain_queue)
        self.after(1000, self._refresh_dynamic_labels)
        self.protocol("WM_DELETE_WINDOW", self.close)

    # -------------------------- styling / layout --------------------------

    def _build_style(self):
        style = ttk.Style(self)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure("TFrame", background="#0b1018")
        style.configure("TLabel", background="#0b1018", foreground="#e7edf6", font=("Segoe UI", 9))
        style.configure("Header.TLabel", background="#0b1018", foreground="#f6f8fb", font=("Segoe UI", 20, "bold"))
        style.configure("Subtitle.TLabel", background="#0b1018", foreground="#93a1b5", font=("Segoe UI", 9))
        style.configure("TButton", background="#182235", foreground="#e7edf6", bordercolor="#2a3951", padding=(10, 6), font=("Segoe UI", 9, "bold"))
        style.map("TButton", background=[("active", "#293953"), ("pressed", "#34476a")])
        style.configure("Accent.TButton", background="#7c4dff", foreground="white", bordercolor="#7c4dff", padding=(11, 6), font=("Segoe UI", 9, "bold"))
        style.map("Accent.TButton", background=[("active", "#966eff")])
        style.configure("TNotebook", background="#0b1018", borderwidth=0)
        style.configure("TNotebook.Tab", background="#182235", foreground="#aebbd0", padding=(13, 7), font=("Segoe UI", 9, "bold"))
        style.map("TNotebook.Tab", background=[("selected", "#7c4dff")], foreground=[("selected", "white")])
        style.configure("Modern.Treeview", background="#111927", fieldbackground="#111927", foreground="#e7edf6", rowheight=31, borderwidth=0, relief="flat", font=("Segoe UI", 9))
        style.configure("Modern.Treeview.Heading", background="#182235", foreground="#d7dfeb", relief="flat", padding=(8, 8), font=("Segoe UI", 9, "bold"))
        style.map("Modern.Treeview", background=[("selected", "#3e2a78")], foreground=[("selected", "white")])
        style.configure("TLabelframe", background="#111927", foreground="#a970ff", bordercolor="#263247")
        style.configure("TLabelframe.Label", background="#111927", foreground="#a970ff", font=("Segoe UI", 9, "bold"))

    def _build_ui(self):
        header = tk.Frame(self, bg="#0b1018", padx=18, pady=14)
        header.pack(fill="x")

        title_box = tk.Frame(header, bg="#0b1018")
        title_box.pack(side="left")
        tk.Label(title_box, text=APP_NAME, font=("Segoe UI", 20, "bold"), fg="#f5f7fb", bg="#0b1018").pack(anchor="w")
        self.header_status = tk.StringVar(value="Waiting for authentication")
        tk.Label(title_box, textvariable=self.header_status, font=("Segoe UI", 9), fg="#92a0b5", bg="#0b1018").pack(anchor="w", pady=(2, 0))

        controls = tk.Frame(header, bg="#0b1018")
        controls.pack(side="right", pady=2)

        self._add_labeled_spinbox(controls, "Min movement", self.min_group_var, 1, 400)
        self._add_labeled_spinbox(controls, "Alert", self.world_alert_var, 1, 400)

        tk.Label(controls, text="Watch world", font=("Segoe UI", 8, "bold"), fg="#aebbd0", bg="#0b1018").pack(side="left", padx=(12, 4))
        watch_entry = tk.Entry(controls, textvariable=self.watch_world_var, width=6, font=("Segoe UI", 9), bg="#182235", fg="#edf2f9", insertbackground="#edf2f9", relief="flat", justify="center")
        watch_entry.pack(side="left")
        watch_entry.bind("<Return>", lambda _event: self.apply_settings())

        self._add_labeled_spinbox(controls, "Watch", self.watch_threshold_var, 1, 400)

        tk.Checkbutton(
            controls,
            text="F2P",
            variable=self.include_f2p,
            command=self._reset_telemetry,
            font=("Segoe UI", 8, "bold"),
            fg="#aebbd0",
            bg="#0b1018",
            activebackground="#0b1018",
            activeforeground="#f0f4fa",
            selectcolor="#182235",
        ).pack(side="left", padx=(12, 3))
        tk.Checkbutton(
            controls,
            text="Sound",
            variable=self.sound_alerts_var,
            font=("Segoe UI", 8, "bold"),
            fg="#aebbd0",
            bg="#0b1018",
            activebackground="#0b1018",
            activeforeground="#f0f4fa",
            selectcolor="#182235",
        ).pack(side="left", padx=(3, 6))
        ttk.Button(controls, text="Apply", style="Accent.TButton", command=self.apply_settings).pack(side="left", padx=2)
        ttk.Button(controls, text="Clear", command=self._reset_telemetry).pack(side="left", padx=2)

        notebook = ttk.Notebook(self)
        notebook.pack(fill="both", expand=True, padx=18, pady=(0, 12))

        self.tabs = {}
        for key, title in (
            ("teams", "ACTIVE TEAMS"),
            ("hops", "MASS HOPS"),
            ("convergences", "CONVERGENCES"),
            ("alerts", "WORLD ALERTS"),
            ("worlds", "WORLDS"),
        ):
            frame = ttk.Frame(notebook)
            notebook.add(frame, text=title)
            self.tabs[key] = frame

        self.tree_teams = self._make_tree(self.tabs["teams"], [
            ("team", "TEAM", 100),
            ("size", "SIZE", 100),
            ("confidence", "CONFIDENCE", 130),
            ("last_world", "LAST WORLD", 110),
            ("hops", "HOPS", 70),
            ("route", "ROUTE", 470),
            ("seen", "LAST SEEN", 100),
        ])
        self.tree_hops = self._make_tree(self.tabs["hops"], [
            ("time", "TIME", 90),
            ("source", "FROM", 90),
            ("destination", "TO", 90),
            ("left", "LEFT", 90),
            ("appeared", "ARRIVED", 90),
            ("size", "EST. GROUP", 105),
            ("confidence", "CONFIDENCE", 130),
        ])
        self.tree_convergences = self._make_tree(self.tabs["convergences"], [
            ("time", "TIME", 90),
            ("destination", "DESTINATION", 105),
            ("gain", "DEST. GAIN", 105),
            ("sources", "SOURCE WORLDS", 420),
            ("outflow", "SOURCE OUTFLOW", 115),
            ("confidence", "CONFIDENCE", 130),
        ])
        self.tree_alerts = self._make_tree(self.tabs["alerts"], [
            ("time", "TIME", 90),
            ("world", "WORLD", 90),
            ("movement", "MOVEMENT", 110),
            ("start", "START POP", 105),
            ("end", "END POP", 105),
            ("status", "STATUS", 110),
        ])
        self.tree_worlds = self._make_tree(self.tabs["worlds"], [
            ("world", "WORLD", 80),
            ("players", "PLAYERS", 100),
            ("type", "TYPE", 100),
            ("location", "LOCATION", 180),
            ("activity", "ACTIVITY", 470),
        ])

        self._configure_tree_tags()

        footer = tk.Frame(self, bg="#111927", padx=14, pady=7)
        footer.pack(fill="x", side="bottom")
        self.footer_status = tk.StringVar(value="Not running")
        tk.Label(footer, textvariable=self.footer_status, font=("Segoe UI", 8), fg="#9cabc0", bg="#111927", anchor="w").pack(fill="x")

    def _add_labeled_spinbox(self, parent, label, variable, start, end):
        tk.Label(parent, text=label, font=("Segoe UI", 8, "bold"), fg="#aebbd0", bg="#0b1018").pack(side="left", padx=(10, 4))
        spin = tk.Spinbox(
            parent,
            from_=start,
            to=end,
            textvariable=variable,
            width=5,
            font=("Segoe UI", 9),
            bg="#182235",
            fg="#edf2f9",
            buttonbackground="#293953",
            insertbackground="#edf2f9",
            relief="flat",
        )
        spin.pack(side="left")

    def _make_tree(self, parent, columns):
        frame = ttk.Frame(parent)
        frame.pack(fill="both", expand=True)
        tree = ttk.Treeview(frame, columns=[item[0] for item in columns], show="headings", style="Modern.Treeview")
        for name, heading, width in columns:
            tree.heading(name, text=heading)
            tree.column(name, width=width, anchor="center")
        if "route" in [item[0] for item in columns]:
            tree.column("route", anchor="w")
        if "sources" in [item[0] for item in columns]:
            tree.column("sources", anchor="w")
        if "activity" in [item[0] for item in columns]:
            tree.column("activity", anchor="w")
        scrollbar = ttk.Scrollbar(frame, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=scrollbar.set)
        tree.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")
        return tree

    def _configure_tree_tags(self):
        for tree in (self.tree_teams, self.tree_hops, self.tree_convergences, self.tree_alerts, self.tree_worlds):
            tree.tag_configure("very", foreground="#39e58c")
            tree.tag_configure("likely", foreground="#e8d44d")
            tree.tag_configure("possible", foreground="#ff9d32")
            tree.tag_configure("unlikely", foreground="#ff5964")
            tree.tag_configure("watch", background="#241a3d")

    # ------------------------------- auth ---------------------------------

    def _show_login(self):
        LoginWindow(self, self._authenticated)

    def _authenticated(self):
        self.deiconify()
        self.header_status.set("Authenticated • waiting for first OSRS world snapshot")
        self.apply_settings()
        self._start_polling()

    # ---------------------------- settings --------------------------------

    def _parse_watch_world(self) -> int | None:
        text = self.watch_world_var.get().strip()
        if not text:
            return None
        try:
            value = int(text)
        except ValueError:
            return None
        if value < 1 or value > 9999:
            return None
        return value

    def apply_settings(self):
        self.min_group_var.set(min(MAX_TRACKED_MOVEMENT, max(1, self.min_group_var.get())))
        self.world_alert_var.set(min(MAX_TRACKED_MOVEMENT, max(1, self.world_alert_var.get())))
        self.watch_threshold_var.set(min(MAX_TRACKED_MOVEMENT, max(1, self.watch_threshold_var.get())))
        self.engine.set_settings(
            self.min_group_var.get(),
            self.world_alert_var.get(),
            self._parse_watch_world(),
            self.watch_threshold_var.get(),
        )
        self._reset_telemetry(preserve_settings=True)
        self.footer_status.set(
            f"Settings applied • movement {self.min_group_var.get()}+ • "
            f"alerts {self.world_alert_var.get()}+ • hop window {int(NORMAL_HOP_WINDOW)}s • convergence {int(CONVERGENCE_WINDOW)}s"
        )

    def _reset_telemetry(self, preserve_settings: bool = False):
        self.poll_include_f2p = bool(self.include_f2p.get())
        if not preserve_settings:
            self.apply_settings_silently()
        else:
            self.engine.reset()
        self.last_state = None
        self.last_worlds = []
        self.snapshots_count = 0
        self._clear_trees()
        self.header_status.set("Baseline reset • waiting for the next snapshot")

    def apply_settings_silently(self):
        self.engine.set_settings(
            self.min_group_var.get(),
            self.world_alert_var.get(),
            self._parse_watch_world(),
            self.watch_threshold_var.get(),
        )
        self.engine.reset()

    def _clear_trees(self):
        for tree in (
            self.tree_teams,
            self.tree_hops,
            self.tree_convergences,
            self.tree_alerts,
            self.tree_worlds,
        ):
            for item in tree.get_children():
                tree.delete(item)

    # ----------------------------- polling --------------------------------

    def _start_polling(self):
        if self.poll_thread and self.poll_thread.is_alive():
            return
        self.poll_include_f2p = bool(self.include_f2p.get())
        self.stop_event.clear()
        self.poll_thread = threading.Thread(target=self._poll_loop, daemon=True)
        self.poll_thread.start()

    def _poll_loop(self):
        while not self.stop_event.is_set():
            include_f2p = self.poll_include_f2p
            started = time.time()
            try:
                worlds = fetch_worlds(include_f2p)
                self.data_queue.put(("worlds", worlds))
            except Exception as exc:
                self.data_queue.put(("error", str(exc) or exc.__class__.__name__))

            elapsed = time.time() - started
            self.stop_event.wait(max(0.2, POLL_INTERVAL - elapsed))

    def _drain_queue(self):
        try:
            while True:
                kind, payload = self.data_queue.get_nowait()
                if kind == "worlds":
                    self._handle_worlds(payload)
                else:
                    self._handle_fetch_error(str(payload))
        except queue.Empty:
            pass
        if not self.stop_event.is_set():
            self.after(100, self._drain_queue)

    def _handle_fetch_error(self, message: str):
        self.header_status.set(f"World data warning • retrying • {message}")
        self.footer_status.set("Telemetry warning • the previous good state is retained")

    def _handle_worlds(self, worlds: list[World]):
        self.poll_in_progress = False
        self.last_worlds = worlds
        self.snapshots_count += 1

        self.engine.set_settings(
            self.min_group_var.get(),
            self.world_alert_var.get(),
            self._parse_watch_world(),
            self.watch_threshold_var.get(),
        )

        state = self.engine.ingest(worlds)
        self.last_state = state
        self._render_state(state)

        self.header_status.set(
            f"Live • {len(worlds)} worlds • {self.snapshots_count} snapshots • "
            f"updated {time.strftime('%H:%M:%S')}"
        )
        self.footer_status.set(
            f"Polling every {POLL_INTERVAL:g}s • mass-hop window {NORMAL_HOP_WINDOW:g}s • "
            f"convergence window {CONVERGENCE_WINDOW:g}s • max individual movement {MAX_TRACKED_MOVEMENT}"
        )

    # ------------------------------ render ---------------------------------

    def _render_state(self, state: dict):
        self._render_teams(state["teams"])
        self._render_hops(state["hops"])
        self._render_convergences(state["convergences"])
        self._render_alerts(state["alerts"])
        self._render_worlds(state["worlds"])

        # Sound alerts only on first appearance of an underlying canonical event,
        # hop or convergence. UI redraws cannot trigger duplicates.
        if self.sound_alerts_var.get() and winsound:
            for event_id in state["changed_events"]:
                if event_id in self.engine.events:
                    event = self.engine.events[event_id]
                    threshold = self.engine.world_alert_threshold
                    if event.world == self.engine.watch_world:
                        threshold = min(threshold, self.engine.watch_threshold)
                    if event.magnitude >= threshold and self.engine.should_sound_for_event(event_id):
                        self._beep()

            for hop in state["hops"]:
                if hop.score >= self.engine.min_hop_score and self.engine.should_sound_for_hop(hop.key):
                    self._beep()
                    break

            for convergence in state["convergences"]:
                if convergence.score >= self.engine.min_hop_score and self.engine.should_sound_for_convergence(convergence.key):
                    self._beep()
                    break

    def _render_teams(self, teams: list[TeamTrack]):
        self._delete_all(self.tree_teams)
        now = time.time()
        for team in teams:
            last_seen = f"{int(max(0, now - team.last_activity))}s"
            route = " → ".join(f"W{world}" for world in team.route)
            self.tree_teams.insert(
                "",
                "end",
                values=(
                    team.team_id,
                    f"~{team.approx_size}",
                    team.likelihood,
                    f"W{team.last_world}" if team.last_world else "-",
                    team.hop_count,
                    route,
                    last_seen,
                ),
                tags=(likelihood_tag(team.confidence_score),),
            )

    def _render_hops(self, hops: list[Hop]):
        self._delete_all(self.tree_hops)
        for hop in hops[:MAX_EVENTS]:
            self.tree_hops.insert(
                "",
                "end",
                values=(
                    time.strftime("%H:%M:%S", time.localtime(hop.timestamp)),
                    f"W{hop.source}",
                    f"W{hop.destination}",
                    f"-{hop.left}",
                    f"+{hop.appeared}",
                    f"~{hop.moved}",
                    likelihood_label(hop.score),
                ),
                tags=(likelihood_tag(hop.score),),
            )

    def _render_convergences(self, convergences: list[Convergence]):
        self._delete_all(self.tree_convergences)
        for item in convergences[:MAX_EVENTS]:
            source_text = " + ".join(
                f"W{world} (-{amount})"
                for world, amount in zip(item.sources, item.source_amounts)
            )
            self.tree_convergences.insert(
                "",
                "end",
                values=(
                    time.strftime("%H:%M:%S", time.localtime(item.timestamp)),
                    f"W{item.destination}",
                    f"+{item.appeared}",
                    source_text,
                    f"~{item.total_outflow}",
                    likelihood_label(item.score),
                ),
                tags=(likelihood_tag(item.score),),
            )

    def _render_alerts(self, alerts: list[MovementEvent]):
        self._delete_all(self.tree_alerts)
        for event in alerts[:MAX_EVENTS]:
            watched = event.world == self.engine.watch_world
            direction = "INFLUX" if event.is_inflow else "OUTFLOW"
            status = f"WATCHED • {direction}" if watched else direction
            tags = [likelihood_tag(self._alert_score(event))]
            if watched:
                tags.append("watch")
            self.tree_alerts.insert(
                "",
                "end",
                values=(
                    time.strftime("%H:%M:%S", time.localtime(event.end_time)),
                    f"W{event.world}",
                    f"{event.amount:+d}",
                    f"{event.start_pop}",
                    f"{event.end_pop}",
                    status,
                ),
                tags=tuple(tags),
            )

    def _render_worlds(self, worlds: list[World]):
        self._delete_all(self.tree_worlds)
        watch_world = self.engine.watch_world
        for world in worlds:
            tags = ("watch",) if world.world == watch_world else ()
            self.tree_worlds.insert(
                "",
                "end",
                values=(world.world, f"{world.players:,}", world.membership, world.location, world.activity),
                tags=tags,
            )

    def _alert_score(self, event: MovementEvent) -> int:
        # This is presentation-only. Movement qualification itself is canonical.
        magnitude = event.magnitude
        base = 40 + min(45, magnitude * 2)
        if event.world == self.engine.watch_world:
            base += 10
        return min(99, base)

    @staticmethod
    def _delete_all(tree):
        children = tree.get_children()
        if children:
            tree.delete(*children)

    @staticmethod
    def _beep():
        try:
            winsound.MessageBeep(winsound.MB_ICONEXCLAMATION)
        except Exception:
            pass

    def _refresh_dynamic_labels(self):
        if self.last_state:
            self._render_teams(self.last_state["teams"])
        if not self.stop_event.is_set():
            self.after(1000, self._refresh_dynamic_labels)

    def close(self):
        self.stop_event.set()
        self.destroy()


def run_app():
    app = MainWindow()
    try:
        app.mainloop()
    except Exception as exc:
        try:
            messagebox.showerror(APP_NAME, f"The application stopped unexpectedly:\n\n{type(exc).__name__}: {exc}")
        except Exception:
            pass


if __name__ == "__main__":
    run_app()
