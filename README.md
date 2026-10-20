# 🎵 Lossless Music Search & Web Streamer

[![Python Version](https://img.shields.io/badge/python-3.8%2B-blue.svg)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Dependencies](https://img.shields.io/badge/dependencies-0%20pip%20packages-brightgreen.svg)](#features)
[![Platform](https://img.shields.io/badge/platform-Linux%20%7C%20macOS%20%7C%20Windows-lightgrey.svg)](#installation)
[![Docker](https://img.shields.io/badge/docker-ready-2496ED.svg)](docker-compose.yml)

A blazing-fast, lightweight, **zero-dependency** music search engine and audio streamer for Hi-Res lossless music (FLAC, ALAC, WAV, MP3).

Available as both a **powerful CLI interactive tool** and a **modern, responsive Web Player** with native seeking, bit-perfect playback, queue management, and on-the-fly album ZIP downloading.

---

## 🌟 Key Highlights

- **⚡ Zero External Dependencies:** Built entirely on Python 3 standard libraries and a vendored single-file micro-framework (`bottle.py`). No `pip install`, no `node_modules`, no bloated virtual environments.
- **🎧 Bit-Perfect Streaming:** 1:1 binary passthrough proxy supporting HTTP `Range` requests for instant audio seeking. Web browser native decoding (FLAC/MP3) guarantees original master sound quality.
- **🚀 Multi-Threaded & Non-Blocking:** Concurrent chunked streaming ensures music playback never blocks background search queries or track inspections.
- **📦 On-The-Fly ZIP Generator:** Download entire multi-track albums in a single click via generator-based streaming ZIP — 0 MB disk space footprint on the server.
- **🔍 Multi-Source Aggregator:**
  - `server.elsci.one` (Direct lossless directory index with automated Anubis PoW solver)
  - `nyaa.si` (Anime OST, J-Pop/J-Rock, and Mora 24-bit Hi-Res FLAC catalog)
  - `thepiratebay` (Full discographies, PBTHAL vinyl rips, and western audiophile FLAC)
  - `archive.org` (Direct FLAC legal, netlabels & historical recordings)
  - `archive.org Live (etree)` (250,000+ live concert soundboard FLAC recordings)
  - `torrents-csv` (Global multi-tracker P2P torrent search)
  - `rutracker` (Hi-Res audiophile releases)
- **⚡ Two-Tier Caching:** Instant response times (<10ms) powered by in-memory backend TTL cache and client-side browser navigation cache.

---

## 📦 Installation & Setup

### Prerequisites
- Python 3.8 or newer (Linux, macOS, Windows).
- *(Optional for CLI playback)* `ffplay` (via `ffmpeg`), `mpv`, or `vlc`:
  - **macOS:** `brew install ffmpeg mpv` or install VLC via `brew install --cask vlc`.
  - **Windows:** `winget install Gyan.FFmpeg` or `winget install VideoLAN.VLC`.
  - **Linux:** `sudo apt install ffmpeg vlc` (or `mpv`).
- *(Optional)* `cloudflare-warp` if your ISP blocks upstream public directories or torrent trackers.

### 1. Clone the Repository
```bash
git clone https://github.com/sjodi442/music-search.git
cd music-search
```

That's it! There are **no pip requirements** to install. Works out-of-the-box on:
- 🐧 **Linux** (`python3`)
- 🍏 **macOS** (`python3`)
- 🪟 **Windows** (`python` in PowerShell or Command Prompt)

---

## 🚀 Usage

### 🌐 Option A: Run the Web Player

You can launch the web application directly:

```bash
python3 app.py
```
*or via the CLI flag:*
```bash
python3 music_search.py --web
```

Open your browser at:
```
http://localhost:8080
```

#### Custom Host & Port
```bash
PORT=3000 HOST=127.0.0.1 python3 app.py
```

#### Web Player Shortcuts
| Shortcut | Action |
| :--- | :--- |
| `Space` | Play / Pause |
| `←` / `→` | Seek backward / forward 5 seconds |
| `↑` / `↓` | Volume up / down 5% |
| `N` | Skip to next track in queue |
| `P` | Skip to previous track in queue |
| `M` | Mute / Unmute |

---

### 💻 Option B: Run the CLI Tool

#### Interactive Terminal Mode
```bash
python3 music_search.py -i
```
Enter an artist, album, or track name to open an interactive prompt where you can inspect track lists, stream directly into terminal audio players (`ffplay`/`mpv`/`vlc`), copy magnet links, or download individual tracks.

#### Direct Search Commands
```bash
# Search and list top results
python3 music_search.py -q "yorushika elma"

# Filter by specific source
python3 music_search.py -q "aimer" -s elsci
python3 music_search.py -q "radiohead" -s archive

# Stream result #1 directly in terminal (ffplay/mpv)
python3 music_search.py -q "yorushika" -t 1

# Open result #1 directly in VLC media player
python3 music_search.py -q "lisa crossing field" --vlc 1

# Download all tracks of an album to ~/Music
python3 music_search.py -q "yoasobi" --download-all 1

# Output structured JSON (for script pipelines)
python3 music_search.py -q "eve smile" --json
```

---

## 🐳 Docker Deployment

Run with Docker Compose:

```bash
docker compose up -d
```

Or build and run manually:
```bash
docker build -t music-search .
docker run -d -p 8080:8080 --name music-search music-search
```

---

## ⚙️ Optional Environment Variables

| Variable | Default | Description |
| :--- | :--- | :--- |
| `HOST` | `0.0.0.0` | Bind host for the web server |
| `PORT` | `8080` | Bind port for the web server |
| `RUTRACKER_USER` | *(empty)* | Optional RuTracker account username |
| `RUTRACKER_PASS` | *(empty)* | Optional RuTracker account password |

---

## 🛠️ Systemd Service (Linux Server)

To run the web app as a persistent background daemon:

Create `/etc/systemd/system/music-search.service`:
```ini
[Unit]
Description=Lossless Music Search Web Server
After=network.target

[Service]
Type=simple
User=youruser
WorkingDirectory=/path/to/music-search
ExecStart=/usr/bin/python3 /path/to/music-search/app.py
Restart=always
RestartSec=5
Environment=PORT=8080

[Install]
WantedBy=multi-user.target
```

Enable and start:
```bash
sudo systemctl daemon-reload
sudo systemctl enable --now music-search
```

---

## 📂 Project Architecture

```
music-search/
├── music_search.py    # Core search engine, scrapers, Anubis PoW solver & CLI interface
├── app.py             # Threaded HTTP server, REST endpoints & Range streaming proxy
├── bottle.py          # Vendored lightweight WSGI framework (0 external dependencies)
├── static/
│   └── index.html     # Responsive HTML5 web player with audio queue & caching
├── Dockerfile         # Lightweight Alpine-based container
├── docker-compose.yml # Compose manifest for 1-click self-hosting
├── requirements.txt   # Dependency manifest (pure standard library)
├── .gitignore         # Git exclusions
├── LICENSE            # MIT License
└── README.md          # Project documentation
```

---

## 📄 License

This project is licensed under the [MIT License](LICENSE) © 2026 sjodi442.

*Disclaimer: This tool searches and streams publicly available metadata and open web directories. Please respect artists and copyright holders in your local jurisdiction.*
