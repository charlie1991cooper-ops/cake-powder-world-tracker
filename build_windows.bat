name: Build Windows Executable

on:
  push:
    branches: [ "main" ]
  pull_request:
    branches: [ "main" ]

jobs:
  build:
    runs-on: windows-latest

    steps:
    - name: Checkout repository
      uses: actions/checkout@v4

    - name: Set up Python 3.12
      uses: actions/setup-python@v5
      with:
        python-version: "3.12"

    - name: Install dependencies
      run: |
        python -m pip install --upgrade pip
        pip install pyinstaller beautifulsoup4 requests

    - name: Build Executable with PyInstaller
      run: |
        pyinstaller --noconfirm --clean --onefile --windowed --name "Cake's OSRS World Tracker" world_tracker.py

    - name: Upload Artifact
      uses: actions/upload-artifact@v4
      with:
        name: Cakes-OSRS-World-Tracker-Windows
        path: dist/Cake's OSRS World Tracker.exe
