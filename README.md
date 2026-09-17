# Cake's OSRS World Tracker

A specialized Windows desktop application designed to infer and track player group movements (such as PK teams) across Old School RuneScape (OSRS) worlds using population telemetry.

## How It Works

Instead of simple static population comparison, the application runs a continuous **Population Telemetry & Inference Engine**:

1. **2-Second Polling:** Parses official OSRS world list HTML (`https://oldschool.runescape.com/slu`) extracting exact world IDs directly from `id="slu-world-XXX"`.
2. **Movement Episodes:** Groups sequential 2-second deltas (e.g., `1000 -> 994 -> 979 -> 970`) into continuous movement episodes rather than spamming individual alerts.
3. **Canonical Event Stream:** All downstream detectors (Mass Hops, Convergences, World Alerts, Watched Worlds) consume a single unified `MovementEvent` lifecycle.
4. **Mass Hop Matching:** Matches source world outflows with destination world inflows within a ~10-second window while tolerating noisy group sizes.
5. **Multi-World Convergence Detection:** Detects when multiple distinct source worlds experience outflows within a 30-second window converging onto a single destination inflow.
6. **Persistent Team Tracking (`TeamTrack`):** Maintains inferred team identity for up to **1 hour** of inactivity, retaining approximate group size, route history, confidence levels, and convergence evidence.

## Configuration Defaults

- **Poll Interval:** 2 seconds
- **Normal Hop Match Window:** 10 seconds
- **Convergence Window:** 30 seconds
- **Team History Expiry:** 1 hour
- **Min Group Size Threshold:** 10 players
- **Max Tracked Movement Cap:** 400 players (prevents sorting/reset glitches)
- **Default Password:** `1234`
- **F2P Worlds:** OFF (Members worlds by default)

## Development & Building

### Requirements
- Python 3.12+
- `requests`, `beautifulsoup4`, `tkinter`

### Run locally
```bash
python world_tracker.py
