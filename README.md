# Cake Powder / Faithful Few — OSRS World Tracker

Windows desktop prototype for tracking OSRS world populations and estimating possible group hops.

## What it does

- Polls the OSRS world list every **10 seconds** by default.
- Shows current world populations.
- Has a dedicated **Group Hops** view.
- Compares each 10-second population snapshot.
- Detects significant drops and gains between worlds.
- Displays explicitly:
  - players that left the source world
  - players that appeared in the destination world
  - estimated players moved
  - percentage likelihood that the changes represent the same group
- Minimum group size defaults to **10**.
- Minimum confidence defaults to **75%**.
- Keeps a detection history.

The percentage is a statistical estimate from population counts and timing. It does **not** prove that the same accounts moved.

## Run the finished app

The intended distribution is a standalone Windows EXE:

`Cake Powder OSRS World Tracker.exe`

The finished EXE does **not** require Python to be installed.

## Build the EXE locally

If Python is already installed, double-click:

`build_windows.bat`

The resulting EXE will be created at:

`dist\Cake Powder OSRS World Tracker.exe`

## Build without installing Python locally

This project includes a GitHub Actions workflow at:

`.github\workflows\build-windows.yml`

1. Create a GitHub repository.
2. Upload this folder to it.
3. Open **Actions**.
4. Select **Build Windows EXE**.
5. Click **Run workflow**.
6. When it finishes, download the artifact named `Cake-Powder-OSRS-World-Tracker-Windows`.

The GitHub runner supplies Windows and Python, so your own PC does not need Python just to produce the EXE.
