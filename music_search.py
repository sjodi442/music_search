#!/usr/bin/env python3
"""
music_search.py — Cari musik hi-res (FLAC/lossless) dari berbagai sumber.
Mendukung direct download (server.elsci.one, Internet Archive) dan torrent (torrents-csv, RuTracker).
Otomatis mengaktifkan Cloudflare WARP saat dijalankan agar bypass sensor/blokir ISP.

Usage:
    python3 music_search.py                         # Mode interaktif penuh
    python3 music_search.py -i                      # Mode interaktif penuh
    python3 music_search.py -q "yorushika elma" -i  # Cari lalu buka menu aksi interaktif
    python3 music_search.py -q "yorushika elma"
    python3 music_search.py -q "aimer" -s elsci
    python3 music_search.py -q "yorushika elma" -s archive
    python3 music_search.py -q "yorushika elma" --sort seeds
    python3 music_search.py -q "yorushika" --type artist
    python3 music_search.py -q "yorushika elma" --json
    python3 music_search.py --sources

Opsi:
    -q, --query       Query pencarian (nama album, artis, atau track)
    -i, --interactive Mode interaktif: putar di VLC, lihat track, magnet, atau buka link
    -s, --source      Sumber: elsci, archive, torrent, rutracker (default: semua)
    --format        Filter format: flac, mp3, wav, aac (default: flac)
    --type          Hint: album, single, artist
    --sort          Urutan: seeds (default), size, date
    -p, --page      Halaman (default: 1)
    --limit         Batas hasil per sumber (default: 10)
    --json          Output JSON
    --sources       Tampilkan sumber aktif

Env (opsional):
    RUTRACKER_USER      Username RuTracker
    RUTRACKER_PASS      Password RuTracker
"""

import argparse
import concurrent.futures
import datetime
import email.utils
import hashlib
import json
import os
import re
import shutil
import socket
import ssl
import subprocess
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from http.cookiejar import CookieJar, Cookie

# ── Cross-Platform Setup (Linux, macOS, Windows) ──────────────────────────────
if sys.platform.startswith("win"):
    # Aktifkan VT100 / ANSI escape code di Windows Console
    os.system("")
    # Reconfigure UTF-8 encoding untuk console Windows
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    if hasattr(sys.stderr, "reconfigure"):
        try:
            sys.stderr.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
elif sys.platform == "darwin":
    # Pastikan binary Homebrew (Apple Silicon & Intel) tersedia di PATH
    for brew_dir in ["/opt/homebrew/bin", "/usr/local/bin"]:
        if os.path.exists(brew_dir) and brew_dir not in os.environ.get("PATH", ""):
            os.environ["PATH"] = f"{brew_dir}:{os.environ.get('PATH', '')}"

def open_system_target(target):
    """Buka URL, file, folder, atau magnet link di aplikasi default (Linux, macOS, Windows)."""
    if sys.platform.startswith("win"):
        try:
            os.startfile(target)
            return True
        except Exception:
            try:
                subprocess.Popen(["cmd", "/c", "start", "", target], shell=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                return True
            except Exception:
                pass
    elif sys.platform == "darwin":
        try:
            subprocess.Popen(["open", target], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return True
        except Exception:
            pass
    else:
        xdg = shutil.which("xdg-open")
        if xdg:
            try:
                subprocess.Popen([xdg, target], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                return True
            except Exception:
                pass
    if target.startswith("http://") or target.startswith("https://"):
        try:
            import webbrowser
            return webbrowser.open(target)
        except Exception:
            pass
    return False

# ── DNS-over-HTTPS (DoH) Resolver ─────────────────────────────────────────────
def doh_resolve(host: str):
    """Resolve hostname via Cloudflare/Google DoH untuk bypass sensor DNS/ISP."""
    for doh in ["https://1.1.1.1/dns-query", "https://8.8.8.8/resolve"]:
        try:
            req = urllib.request.Request(
                f"{doh}?name={host}&type=A",
                headers={"Accept": "application/dns-json", "User-Agent": "Mozilla/5.0"}
            )
            with urllib.request.urlopen(req, timeout=5) as r:
                data = json.loads(r.read().decode("utf-8"))
                for ans in data.get("Answer", []):
                    if ans.get("type") == 1:
                        return ans["data"]
        except Exception:
            continue
    return None

_orig_gai = socket.getaddrinfo
def _custom_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
    try:
        return _orig_gai(host, port, family, type, proto, flags)
    except socket.gaierror:
        ip = doh_resolve(host)
        if ip:
            return _orig_gai(ip, port, family, type, proto, flags)
        raise

socket.getaddrinfo = _custom_getaddrinfo

try:
    from bs4 import BeautifulSoup
except ImportError:
    BeautifulSoup = None

TIMEOUT     = 12
MAX_WORKERS = 8
UA          = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
HEADERS     = {
    "User-Agent":      UA,
    "Accept-Language": "en-US,en;q=0.9",
    "Accept":          "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}
SSL_CTX     = ssl._create_unverified_context()

# ── Network & WARP Integration ────────────────────────────────────────────────

def ensure_warp_service() -> None:
    """Otomatis sambungkan Cloudflare WARP client jika belum tersambung tanpa meminta root/sudo."""
    try:
        warp_cli = shutil.which("warp-cli")
        if not warp_cli:
            return
        res = subprocess.run([warp_cli, "--accept-tos", "status"], capture_output=True, text=True, timeout=2)
        if "Connected" in res.stdout:
            return
        subprocess.run([warp_cli, "--accept-tos", "connect"], capture_output=True, timeout=3)
    except Exception:
        pass

# ── Helpers ────────────────────────────────────────────────────────────────────

def _req(url, extra_headers=None, data=None, jar=None, timeout=TIMEOUT, use_ssl_ctx=False):
    h = {**HEADERS, **(extra_headers or {})}
    req = urllib.request.Request(url, headers=h, data=data)
    if jar:
        handler = urllib.request.HTTPCookieProcessor(jar)
        opener  = urllib.request.build_opener(handler)
    else:
        opener  = urllib.request.build_opener()
    ctx = SSL_CTX if use_ssl_ctx else None
    try:
        with opener.open(req, timeout=timeout, **({"context": ctx} if ctx else {})) as r:
            raw = r.read()
            try:
                return raw.decode("utf-8")
            except UnicodeDecodeError:
                return raw.decode("cp1251", errors="replace")
    except Exception:
        return None

def _bytes_human(b):
    if not b:
        return "?"
    try:
        b = int(b)
    except (ValueError, TypeError):
        return str(b)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if b < 1024:
            return f"{b:.1f} {unit}"
        b /= 1024
    return f"{b:.1f} PB"

def _color(text, code):
    if not sys.stdout.isatty():
        return text
    return f"\033[{code}m{text}\033[0m"

def _bold(t):   return _color(t, "1")
def _cyan(t):   return _color(t, "36")
def _green(t):  return _color(t, "32")
def _yellow(t): return _color(t, "33")
def _magenta(t): return _color(t, "35")
def _dim(t):    return _color(t, "2")

def _make_magnet(infohash, name):
    return (
        f"magnet:?xt=urn:btih:{infohash}"
        f"&dn={urllib.parse.quote_plus(name)}"
        "&tr=udp%3A%2F%2Ftracker.opentrackr.org%3A1337%2Fannounce"
        "&tr=udp%3A%2F%2Fopen.tracker.cl%3A1337%2Fannounce"
        "&tr=udp%3A%2F%2Ftracker.leechers-paradise.org%3A6969%2Fannounce"
        "&tr=udp%3A%2F%2Ftracker.coppersurfer.tk%3A6969%2Fannounce"
    )

# ── server.elsci.one/Music/ (Open Directory Lossless / FLAC) ───────────────────

ELSCI_BASE  = "https://server.elsci.one"
ELSCI_MUSIC = f"{ELSCI_BASE}/Music/"
CACHE_DIR   = os.environ.get("MUSIC_SEARCH_CACHE_DIR") or os.path.expanduser("~/.cache/music_search")
CACHE_FILE  = os.path.join(CACHE_DIR, "elsci_index.json")
COOKIE_FILE = os.path.join(CACHE_DIR, "cookie.json")

def _load_cached_cookie():
    """Ambil cookie Anubis dari disk jika masih valid (TTL: 2 jam)."""
    if os.path.exists(COOKIE_FILE):
        try:
            with open(COOKIE_FILE) as f:
                data = json.load(f)
                if time.time() - data.get("ts", 0) < 7200 and data.get("cookie"):
                    return data.get("cookie")
        except Exception:
            pass
    return ""

def _save_cached_cookie(cookie_str):
    """Simpan cookie sesi Anubis ke disk agar eksekusi berikutnya tidak perlu solve PoW."""
    try:
        os.makedirs(CACHE_DIR, exist_ok=True)
        with open(COOKIE_FILE, "w") as f:
            json.dump({"ts": time.time(), "cookie": cookie_str}, f)
    except Exception:
        pass

def _solve_anubis(html, opener):
    """Selesaikan PoW challenge Anubis (Techaro) secara instan dalam pure Python."""
    idx = html.find('id="anubis_challenge"')
    if idx == -1:
        return
    start = html.find(">", idx) + 1
    end = html.find("</script>", start)
    data = json.loads(html[start:end].strip())
    chall = data["challenge"]
    target = "0" * data["rules"].get("difficulty", 4)
    nonce = 0
    t0 = time.time()
    while True:
        h = hashlib.sha256((chall["randomData"] + str(nonce)).encode()).hexdigest()
        if h.startswith(target):
            break
        nonce += 1
    elapsed = int((time.time() - t0) * 1000)
    cid = chall["id"]
    pass_url = f"{ELSCI_BASE}/.within.website/x/cmd/anubis/api/pass-challenge?id={cid}&response={h}&nonce={nonce}&redir=/Music/&elapsedTime={elapsed}"
    opener.open(urllib.request.Request(pass_url, headers=HEADERS), timeout=TIMEOUT)

def _get_elsci_items():
    """Ambil daftar katalog album dari server.elsci.one/Music/ via _h5ai dengan caching 24 jam."""
    os.makedirs(CACHE_DIR, exist_ok=True)
    if os.path.exists(CACHE_FILE):
        try:
            with open(CACHE_FILE) as f:
                c = json.load(f)
                if time.time() - c.get("ts", 0) < 86400 and c.get("items"):
                    return c.get("items", [])
        except Exception:
            pass

    jar = CookieJar()
    cached_cookie = _load_cached_cookie()
    req_headers = dict(HEADERS)
    if cached_cookie:
        req_headers["Cookie"] = cached_cookie

    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    try:
        resp = opener.open(urllib.request.Request(ELSCI_MUSIC, headers=req_headers), timeout=TIMEOUT)
        html = resp.read().decode("utf-8", errors="replace")
        if 'id="anubis_challenge"' in html:
            _solve_anubis(html, opener)
            c_str = "; ".join([f"{c.name}={c.value}" for c in jar])
            if c_str:
                _save_cached_cookie(c_str)
        elif cached_cookie:
            c_str = cached_cookie
        else:
            c_str = "; ".join([f"{c.name}={c.value}" for c in jar])
            if c_str:
                _save_cached_cookie(c_str)

        global ELSCI_COOKIES
        ELSCI_COOKIES = c_str

        post_headers = {**HEADERS, "Content-Type": "application/json"}
        if ELSCI_COOKIES:
            post_headers["Cookie"] = ELSCI_COOKIES

        payload = {"action": "get", "items": {"href": "/Music/", "what": 1}}
        req = urllib.request.Request(
            ELSCI_MUSIC,
            data=json.dumps(payload).encode(),
            headers=post_headers
        )
        res_raw = opener.open(req, timeout=TIMEOUT).read().decode("utf-8")
        res = json.loads(res_raw)
        items = res.get("items", [])
        music_items = []
        for it in items:
            h = it.get("href", "")
            if h.startswith("/Music/") and h != "/Music/":
                name = urllib.parse.unquote(h[len("/Music/"):].rstrip("/"))
                music_items.append({
                    "name": name,
                    "href": h,
                    "time": it.get("time"),
                    "size": it.get("size")
                })

        with open(CACHE_FILE, "w") as f:
            json.dump({"ts": time.time(), "items": music_items}, f)
        return music_items
    except Exception:
        return []

def detect_audio_quality(text):
    """Deteksi bit-depth, sample rate, dan format dari nama file/folder."""
    t = text.lower()
    if any(x in t for x in ["24bit-192khz", "24-192", "24bit/192khz", "24bit_192khz", "192khz-24bit", "192khz/24bit"]):
        return "FLAC 24-bit / 192 kHz (Hi-Res Studio)"
    if any(x in t for x in ["24bit-96khz", "24-96", "24bit/96khz", "24bit_96khz", "24b-96k", "96khz-24bit", "96khz/24bit"]):
        return "FLAC 24-bit / 96 kHz (Hi-Res)"
    if any(x in t for x in ["24bit-48khz", "24-48", "24bit/48khz", "24bit_48khz", "48khz/24bit", "48khz／24bit", "48khz-24bit"]):
        return "FLAC 24-bit / 48 kHz (Hi-Res)"
    if any(x in t for x in ["24bit-44.1khz", "24-44.1", "24-44", "24bit/44.1khz", "44.1khz-24bit"]):
        return "FLAC 24-bit / 44.1 kHz (Hi-Res)"
    if "24bit" in t or "24-bit" in t or "24 bit" in t:
        return "FLAC 24-bit (Hi-Res)"
    if any(x in t for x in ["16bit-44.1khz", "16-44.1", "16-44", "16bit_44.1khz", "cd-flac", "cd flac", "44.1khz-16bit"]):
        return "FLAC 16-bit / 44.1 kHz (CD)"
    if any(x in t for x in ["16bit-48khz", "16-48", "48khz-16bit"]):
        return "FLAC 16-bit / 48 kHz"
    if any(x in t for x in ["dsd", "dsf", "dff"]):
        return "DSD Lossless (Direct Stream Digital)"
    if "mqa" in t:
        return "MQA Lossless"
    if any(x in t for x in ["hi-res", "hires"]):
        return "Hi-Res Lossless"
    if "flac" in t:
        return "FLAC Lossless"
    if "alac" in t:
        return "ALAC Lossless"
    if "wav" in t:
        return "WAV Lossless"
    if "320" in t and "mp3" in t:
        return "MP3 320 kbps"
    return "Lossless Audio"

def search_elsci(query, fmt="flac", sort="seeds", page=1, limit=10):
    """Cari album lossless di open directory server.elsci.one/Music/."""
    items = _get_elsci_items()
    if not items:
        return []

    words = query.lower().split()
    matched = [it for it in items if all(w in it["name"].lower() for w in words)]
    if not matched and len(words) > 1:
        matched = [it for it in items if any(w in it["name"].lower() for w in words)]

    if fmt.lower() == "flac":
        matched.sort(
            key=lambda x: (
                0 if "24-96" in x["name"] or "hi-res" in x["name"].lower()
                else (1 if "flac" in x["name"].lower() else 2)
            )
        )

    results = []
    for it in matched[:limit]:
        raw_href = it["href"]
        name = it["name"]
        folder_url = ELSCI_BASE + raw_href
        mtime = it.get("time")
        date_str = ""
        if mtime:
            try:
                date_str = datetime.datetime.fromtimestamp(mtime / 1000).strftime("%Y-%m-%d")
            except Exception:
                pass
        fmt_str = detect_audio_quality(name)
        results.append({
            "source":  "server.elsci.one",
            "title":   name,
            "size":    "Folder / Album",
            "seeds":   "Direct HTTP",
            "leeches": "",
            "date":    date_str,
            "format":  fmt_str,
            "url":     folder_url,
            "magnet":  folder_url,
            "type":    "direct",
        })
    return results

# ── Torrents-CSV (aggregasi torrent publik) ───────────────────────────────────

TCSV_API = "https://torrents-csv.com/service/search"

def search_torrent(query, fmt="flac", sort="seeds", page=1, limit=10):
    """Cari via torrents-csv.com — agregasi dari berbagai tracker."""
    candidates = [f"{query} {fmt}", query]
    words = query.split()
    if len(words) > 1:
        candidates.append(words[0])

    items = []
    for q_try in candidates:
        params = urllib.parse.urlencode({
            "q":    q_try,
            "size": str(min(limit * 2, 50)),
            "page": str(page),
        })
        url  = f"{TCSV_API}?{params}"
        text = _req(url, extra_headers={"Accept": "application/json"})
        if not text:
            continue
        try:
            data = json.loads(text)
            hits = data.get("torrents", [])
            if hits:
                items = hits
                break
        except Exception:
            continue

    if not items:
        return []

    if sort == "seeds":
        items.sort(key=lambda x: int(x.get("seeders", 0)), reverse=True)
    elif sort == "size":
        items.sort(key=lambda x: int(x.get("size_bytes", 0)), reverse=True)
    elif sort == "date":
        items.sort(key=lambda x: int(x.get("created_unix", 0)), reverse=True)

    results = []
    for item in items[:limit]:
        ih      = item.get("infohash", "")
        name    = item.get("name", "")
        seeds   = str(item.get("seeders", 0))
        leeches = str(item.get("leechers", 0))
        size    = _bytes_human(item.get("size_bytes", 0))
        ts      = item.get("created_unix", 0)
        date    = ""
        if ts:
            try:
                date = datetime.datetime.fromtimestamp(int(ts)).strftime("%Y-%m-%d")
            except Exception:
                pass
        magnet = _make_magnet(ih, name) if ih else ""
        results.append({
            "source":  "torrents-csv",
            "title":   name,
            "size":    size,
            "seeds":   seeds,
            "leeches": leeches,
            "date":    date,
            "url":     f"https://torrents-csv.com/torrent/{ih}",
            "magnet":  magnet,
            "type":    "torrent",
        })
    return results

# ── RuTracker (J-music FLAC paling lengkap) ────────────────────────────────────

RT_BASE = "https://rutracker.org"
RT_MUSIC_FORUMS = ["14", "1235", "266", "1267", "434", "816", "1350"]

def _rt_login(username, password, jar):
    login_url = f"{RT_BASE}/forum/login.php"
    data = urllib.parse.urlencode({
        "login_username": username,
        "login_password": password,
        "login":          "Вход",
    }).encode("cp1251", errors="replace")
    h = {"Content-Type": "application/x-www-form-urlencoded", "Referer": login_url}
    res = _req(login_url, extra_headers=h, data=data, jar=jar, use_ssl_ctx=True)
    return res is not None and "bb_data" in str(jar)

def search_rutracker(query, fmt="flac", sort="seeds", page=1, limit=10):
    user = os.environ.get("RUTRACKER_USER")
    pwd  = os.environ.get("RUTRACKER_PASS")
    if not user or not pwd:
        return [{
            "source": "RuTracker",
            "error":  "Set RUTRACKER_USER dan RUTRACKER_PASS untuk mengaktifkan RuTracker",
        }]

    if BeautifulSoup is None:
        return [{
            "source": "RuTracker",
            "error":  "bs4 belum terpasang di Python venv ini (jalankan: pip install beautifulsoup4)",
        }]

    jar = CookieJar()
    if not _rt_login(user, pwd, jar):
        return [{"source": "RuTracker", "error": "Login gagal — periksa username/password"}]

    q_full = f"{query} {fmt}"
    params = urllib.parse.urlencode({
        "nm": q_full,
        "f":  ",".join(RT_MUSIC_FORUMS),
        "start": str((page - 1) * 50)
    }).encode("cp1251", errors="replace")
    sort_order = "2" if sort == "seeds" else ("1" if sort == "date" else "4")
    url = f"{RT_BASE}/forum/tracker.php?{params.decode('ascii')}&s={sort_order}"

    html = _req(url, jar=jar, use_ssl_ctx=True)
    if not html:
        return [{"source": "RuTracker", "error": "Gagal membuka halaman pencarian"}]

    soup = BeautifulSoup(html, "html.parser")
    rows = soup.select("#tor-tbl tr.tCenter")
    if not rows:
        return []

    results = []
    for r in rows[:limit]:
        title_el = r.select_one("a.tt-text, a.tLink")
        dl_el    = r.select_one("a.tr-dl")
        seed_el  = r.select_one("b.seedmed, .seed")
        leech_el = r.select_one(".leech")
        size_el  = r.select_one("a.tor-size")
        if not title_el:
            continue
        title    = title_el.get_text(strip=True)
        t_href   = title_el.get("href", "")
        topic_id = None
        if dl_el:
            m = re.search(r"t=(\d+)", dl_el.get("href", ""))
            if m:
                topic_id = m.group(1)
        url    = RT_BASE + "/forum/" + t_href if t_href else ""
        dl_url = (RT_BASE + "/forum/dl.php?t=" + topic_id) if topic_id else ""
        seeds  = seed_el.get_text(strip=True) if seed_el else "?"
        leeches = leech_el.get_text(strip=True) if leech_el else "?"
        size   = size_el.get_text(strip=True) if size_el else "?"
        results.append({
            "source":  "RuTracker",
            "title":   title,
            "size":    size,
            "seeds":   seeds,
            "leeches": leeches,
            "date":    "",
            "url":     url,
            "magnet":  dl_url,
            "type":    "torrent (.torrent link)",
        })
    return results

# ── Internet Archive ───────────────────────────────────────────────────────────

IA_SEARCH = "https://archive.org/advancedsearch.php"
IA_DL     = "https://archive.org/download"

def search_archive(query, fmt="flac", sort="seeds", page=1, limit=10):
    sort_map = {"seeds": "downloads", "date": "publicdate", "size": "item_size"}
    so = sort_map.get(sort, "downloads")

    # Lucene query yang presisi: cegah noise dan pencarian token acak
    clean_q = re.sub(r'["\+\-\*\(\)\:\^\[\]\{\}\~\?\\]', ' ', query).strip()
    phrase_q = f'"{clean_q}"'
    terms_q = " AND ".join(clean_q.split()) if clean_q else phrase_q
    
    queries = [
        # 1. Exact phrase / title / creator match dengan format lossless
        f'(title:{phrase_q} OR creator:{phrase_q} OR {phrase_q}) AND (format:FLAC OR format:"24bit Flac") AND mediatype:audio',
        # 2. Exact phrase audio umum
        f'(title:{phrase_q} OR creator:{phrase_q} OR {phrase_q}) AND mediatype:audio',
        # 3. Semua kata wajib ada (AND bukan OR)
        f'({terms_q}) AND mediatype:audio',
    ]

    docs = []
    for q_try in queries:
        params = urllib.parse.urlencode({
            "q":      q_try,
            "fl[]":   ["identifier", "title", "creator", "date", "downloads", "item_size", "format"],
            "sort[]": f"{so} desc",
            "rows":   str(limit * 2),  # Ambil cadangan untuk filter presisi
            "page":   str(page),
            "output": "json",
        }, doseq=True)
        url  = IA_SEARCH + "?" + params
        h    = {"User-Agent": "MusicSearch/1.0 (personal)", "Accept": "application/json"}
        text = _req(url, extra_headers=h)
        if not text:
            continue
        try:
            data = json.loads(text)
            hits = data.get("response", {}).get("docs", [])
            if hits:
                docs = hits
                break
        except Exception:
            continue

    if not docs:
        return []

    # Relevance scoring & filtering
    q_words = [w.lower() for w in clean_q.split() if len(w) > 1]
    results = []
    for d in docs:
        ident   = d.get("identifier", "")
        title   = d.get("title", ident)
        if isinstance(title, list):
            title = title[0] if title else ident
        creator = d.get("creator", "")
        if isinstance(creator, list):
            creator = ", ".join(creator[:2])
        date    = (d.get("date") or "")[:10]
        dl_count = str(d.get("downloads", "?"))
        size    = _bytes_human(d.get("item_size", 0))
        fmts    = d.get("format", [])
        if isinstance(fmts, str):
            fmts = [fmts]
        fmt_tag = " | ".join(f for f in fmts if "flac" in f.lower() or "wav" in f.lower())[:40]
        dl_url  = f"{IA_DL}/{ident}"
        label   = f"{creator} — {title}" if creator else title

        # Cek relevansi: apakah judul/creator/ident mengandung query
        haystack = f"{title} {creator} {ident}".lower()
        if q_words and not any(w in haystack for w in q_words):
            continue

        results.append({
            "source":  "Archive.org",
            "title":   label,
            "size":    size,
            "seeds":   dl_count + " DL",
            "leeches": "",
            "date":    date,
            "format":  fmt_tag or fmt.upper(),
            "url":     f"https://archive.org/details/{ident}",
            "magnet":  dl_url,
            "type":    "direct",
        })
        if len(results) >= limit:
            break

    return results

# ── Nyaa.si (Anime OST, J-Pop/J-Rock, Mora 24-bit Hi-Res) ──────────────────────

def search_nyaa(query, fmt="flac", sort="seeds", page=1, limit=10):
    """
    Cari via Nyaa.si — indeks audio lossless, Anime OST, J-Pop/J-Rock, dan Mora Hi-Res terbesar.
    c=2_1: Audio - Lossless
    c=2_0: Audio - All
    """
    category = "2_1" if fmt in ("flac", "wav") else "2_0"
    params = urllib.parse.urlencode({
        "page": "rss",
        "q": query,
        "c": category,
        "p": str(page),
    })
    if sort == "seeds":
        params += "&s=seeders&o=desc"
    elif sort == "size":
        params += "&s=size&o=desc"

    url = f"https://nyaa.si/?{params}"
    text = _req(url)
    if not text and category == "2_1":
        params = urllib.parse.urlencode({
            "page": "rss",
            "q": query,
            "c": "2_0",
            "p": str(page),
        })
        if sort == "seeds":
            params += "&s=seeders&o=desc"
        url = f"https://nyaa.si/?{params}"
        text = _req(url)

    if not text:
        return []

    try:
        root = ET.fromstring(text.encode("utf-8", errors="replace"))
        items = root.findall("./channel/item")
    except Exception:
        return []

    results = []
    for item in items[:limit]:
        title = item.findtext("title") or ""
        link = item.findtext("link") or ""
        guid = item.findtext("guid") or ""
        pub_date = item.findtext("pubDate") or ""
        date = ""
        if pub_date:
            try:
                date = email.utils.parsedate_to_datetime(pub_date).strftime("%Y-%m-%d")
            except Exception:
                pass

        infohash = ""
        seeds = "0"
        leeches = "0"
        size = "?"
        for child in item:
            tag = child.tag
            if "infoHash" in tag:
                infohash = child.text or ""
            elif "seeders" in tag:
                seeds = child.text or "0"
            elif "leechers" in tag:
                leeches = child.text or "0"
            elif "size" in tag:
                size = child.text or "?"

        magnet = _make_magnet(infohash, title) if infohash else ""
        results.append({
            "source":  "nyaa",
            "title":   title,
            "size":    size,
            "seeds":   seeds,
            "leeches": leeches,
            "date":    date,
            "url":     guid or link,
            "magnet":  magnet,
            "type":    "torrent",
        })
    return results

# ── The Pirate Bay (Discography, Vinyl Rips, Western FLAC) ─────────────────────

def search_piratebay(query, fmt="flac", sort="seeds", page=1, limit=10):
    """
    Cari via The Pirate Bay (tpb.party) — discography lengkap, vinyl rips (PBTHAL), dan western albums.
    """
    q_str = f"{query} {fmt}" if fmt and fmt != "all" else query
    sort_code = "7" if sort in ("seeds", "relevance") else ("3" if sort == "date" else "5")
    url = f"https://tpb.party/search/{urllib.parse.quote(q_str)}/{page}/{sort_code}/100"
    html = _req(url)

    row_pattern = (
        r'<td class="vertTh"><a [^>]*>([^<]+)</a></td>\s*'
        r'<td><a href="([^"]+)"[^>]*>([^<]+)</a>\s*</td>\s*'
        r'<td>([^<]*)</td>\s*'
        r'<td>.*?href="(magnet:\?[^"]+)".*?</td>\s*'
        r'<td align="right">([^<]+)</td>\s*'
        r'<td align="right">(\d+)</td>\s*'
        r'<td align="right">(\d+)</td>'
    )

    rows = []
    if html:
        rows = re.findall(row_pattern, html, re.DOTALL)
    if not rows and fmt:
        url = f"https://tpb.party/search/{urllib.parse.quote(query)}/{page}/{sort_code}/100"
        html = _req(url)
        if html:
            rows = re.findall(row_pattern, html, re.DOTALL)

    results = []
    for cat, link, title, date, mag, size, seeds, leech in rows[:limit]:
        fmt_label = "FLAC" if "flac" in cat.lower() or "flac" in title.lower() else "MP3/Audio"
        results.append({
            "source":  "piratebay",
            "title":   title.strip(),
            "size":    size.replace("&nbsp;", " ").strip(),
            "seeds":   seeds,
            "leeches": leech,
            "date":    date.replace("&nbsp;", " ").strip(),
            "format":  fmt_label,
            "url":     link,
            "magnet":  mag,
            "type":    "torrent",
        })
    return results

# ── Archive.org Live Music Archive (etree - Soundboards) ───────────────────────

def search_archive_live(query, fmt="flac", sort="seeds", page=1, limit=10):
    """
    Cari via Archive.org Live Music Archive (etree) — 250k+ konser live soundboard FLAC legal.
    """
    sort_param = "downloads+desc" if sort in ("seeds", "relevance") else ("publicdate+desc" if sort == "date" else "item_size+desc")
    clean_q = re.sub(r'["\+\-\*\(\)\:\^\[\]\{\}\~\?\\]', ' ', query).strip()
    phrase_q = f'"{clean_q}"'
    terms_q = " AND ".join(clean_q.split()) if clean_q else phrase_q
    q_solr = urllib.parse.quote(f'(title:{phrase_q} OR creator:{phrase_q} OR {phrase_q} OR ({terms_q})) AND collection:etree AND mediatype:etree')

    url = (
        f"https://archive.org/advancedsearch.php?q={q_solr}"
        f"&fl[]=identifier,title,creator,date,downloads,item_size"
        f"&sort[]={sort_param}&rows={limit * 2}&page={page}&output=json"
    )
    headers = {"User-Agent": "MusicSearch/1.0", "Accept": "application/json"}
    text = _req(url, extra_headers=headers)
    if not text:
        return []

    try:
        data = json.loads(text)
        docs = data.get("response", {}).get("docs", [])
    except Exception:
        return []

    q_words = [w.lower() for w in clean_q.split() if len(w) > 1]
    results = []
    for doc in docs:
        ident = doc.get("identifier", "")
        title = doc.get("title") or ident
        creator = doc.get("creator", "")
        if creator and isinstance(creator, str) and creator not in title:
            title = f"{creator} — {title}"
        elif isinstance(creator, list) and creator:
            title = f"{creator[0]} — {title}"

        # Filter relevansi: pastikan tidak melenceng ke artis lain
        haystack = f"{title} {creator} {ident}".lower()
        if q_words and not any(w in haystack for w in q_words):
            continue

        size = _bytes_human(doc.get("item_size", 0)) if doc.get("item_size") else "?"
        date = (doc.get("date") or "")[:10]
        dl_count = str(doc.get("downloads", 0))
        results.append({
            "source":  "archive_live",
            "title":   title,
            "size":    size,
            "seeds":   dl_count + " DL",
            "leeches": "",
            "date":    date,
            "format":  "FLAC (Live SBD)",
            "url":     f"https://archive.org/details/{ident}",
            "magnet":  f"https://archive.org/download/{ident}",
            "type":    "direct",
            "download_url": f"https://archive.org/download/{ident}",
        })
        if len(results) >= limit:
            break
    return results

# ── Dispatcher ────────────────────────────────────────────────────────────────

SOURCES = {
    "elsci":        search_elsci,
    "nyaa":         search_nyaa,
    "piratebay":    search_piratebay,
    "archive":      search_archive,
    "archive_live": search_archive_live,
    "torrent":      search_torrent,
    "rutracker":    search_rutracker,
}

def search_all(query, fmt="flac", sort="seeds", page=1, limit=10, source=None):
    targets = {source: SOURCES[source]} if source and source in SOURCES else dict(SOURCES)
    # Jangan jalankan RuTracker saat mode pencarian semua sumber jika kredensial belum di-set
    if not source and "rutracker" in targets and not os.environ.get("RUTRACKER_USER"):
        targets.pop("rutracker", None)

    results = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        futures = {
            ex.submit(fn, query, fmt, sort, page, limit): name
            for name, fn in targets.items()
        }
        for fut in concurrent.futures.as_completed(futures):
            name = futures[fut]
            try:
                results[name] = fut.result()
            except Exception as e:
                results[name] = [{"source": name, "error": str(e)}]
    ordered = {}
    for k in ["elsci", "nyaa", "piratebay", "archive", "archive_live", "torrent", "rutracker"]:
        if k in results:
            ordered[k] = results[k]
    return ordered

# ── Output ─────────────────────────────────────────────────────────────────────

def print_results(all_results, as_json=False):
    flat = []
    for src, items in all_results.items():
        for item in items:
            flat.append(item)

    if as_json:
        print(json.dumps(flat, ensure_ascii=False, indent=2))
        return []

    if not flat:
        print("Tidak ada hasil ditemukan.")
        return []

    idx         = 1
    current_src = None
    valid_items = []
    for item in flat:
        src = item.get("source", "?")
        if "error" in item:
            print(f"\n{_yellow('[' + src + ']')} {item['error']}")
            continue
        valid_items.append(item)
        if src != current_src:
            current_src = src
            itype = item.get("type", "torrent")
            lbl   = "Direct Download" if itype == "direct" else "Torrent / Magnet"
            print(f"\n{'─'*62}")
            print(f"{_bold(_cyan('[ ' + src + ' ]'))} — {lbl}")
            print(f"{'─'*62}")
        seeds_txt   = item.get("seeds", "?")
        leeches_txt = item.get("leeches", "")
        size_txt    = item.get("size", "?")
        date_txt    = item.get("date", "")
        fmt_tag     = item.get("format", "")
        itype       = item.get("type", "torrent")
        magnet_val  = item.get("magnet") or "(tidak tersedia)"

        print(f"\n{_bold(str(idx) + '.')} {item['title']}")
        if fmt_tag:
            print(f"    Format  : {_green(fmt_tag)}")
        print(f"    Ukuran  : {_green(size_txt)}")
        if leeches_txt:
            print(f"    Seeds   : {_green(seeds_txt)}  |  Leeches: {leeches_txt}")
        else:
            print(f"    Unduhan : {_green(seeds_txt)}")
        if date_txt:
            print(f"    Tanggal : {date_txt}")
        print(f"    URL     : {_cyan(item['url'])}")
        if itype == "direct":
            print(f"    Download: {_dim(magnet_val)}")
        else:
            print(f"    Magnet  : {_dim(magnet_val[:120] + ('...' if len(magnet_val) > 120 else ''))}")
        idx += 1

    return valid_items

# ── Interactive Mode & Media Actions ──────────────────────────────────────────

def open_url(url):
    """Buka URL di browser default sistem (Linux, macOS, Windows)."""
    ok = open_system_target(url)
    return "Browser Default" if ok else None

ELSCI_COOKIES = ""

def _get_elsci_cookies():
    global ELSCI_COOKIES
    if ELSCI_COOKIES:
        return ELSCI_COOKIES
    cached = _load_cached_cookie()
    if cached:
        ELSCI_COOKIES = cached
        return ELSCI_COOKIES
    jar = CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    try:
        resp = opener.open(urllib.request.Request(ELSCI_MUSIC, headers=HEADERS), timeout=TIMEOUT)
        html = resp.read().decode("utf-8", errors="replace")
        if 'id="anubis_challenge"' in html:
            _solve_anubis(html, opener)
        ELSCI_COOKIES = "; ".join([f"{c.name}={c.value}" for c in jar])
        if ELSCI_COOKIES:
            _save_cached_cookie(ELSCI_COOKIES)
    except Exception:
        pass
    return ELSCI_COOKIES

def _get_elsci_folder_tracks(folder_url, max_depth=2):
    """Ambil daftar track audio di dalam folder server.elsci.one secara rekursif (multi-disc)."""
    global ELSCI_COOKIES
    jar = CookieJar()
    cached = _load_cached_cookie() or ELSCI_COOKIES
    if cached:
        for part in cached.split("; "):
            if "=" in part:
                k, v = part.split("=", 1)
                jar.set_cookie(Cookie(
                    version=0, name=k, value=v, port=None, port_specified=False,
                    domain="server.elsci.one", domain_specified=True, domain_initial_dot=False,
                    path="/", path_specified=True, secure=True, expires=None, discard=False,
                    comment=None, comment_url=None, rest={"HttpOnly": None}
                ))

    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    try:
        resp = opener.open(urllib.request.Request(ELSCI_MUSIC, headers=HEADERS), timeout=TIMEOUT)
        html = resp.read().decode("utf-8", errors="replace")
        if 'id="anubis_challenge"' in html:
            _solve_anubis(html, opener)
            ELSCI_COOKIES = "; ".join([f"{c.name}={c.value}" for c in jar])
            if ELSCI_COOKIES:
                _save_cached_cookie(ELSCI_COOKIES)
    except Exception:
        pass

    def _post_rpc(href_path):
        p_headers = {**HEADERS, "Content-Type": "application/json"}
        current_c = "; ".join([f"{c.name}={c.value}" for c in jar]) or ELSCI_COOKIES or _load_cached_cookie()
        if current_c:
            p_headers["Cookie"] = current_c
        payload = {"action": "get", "items": {"href": href_path, "what": 1}}
        req = urllib.request.Request(ELSCI_MUSIC, data=json.dumps(payload).encode(), headers=p_headers)
        try:
            resp = opener.open(req, timeout=TIMEOUT)
            body = resp.read().decode("utf-8", errors="replace")
            if 'id="anubis_challenge"' in body:
                _solve_anubis(body, opener)
                new_c = "; ".join([f"{c.name}={c.value}" for c in jar])
                if new_c:
                    _save_cached_cookie(new_c)
                    p_headers["Cookie"] = new_c
                req2 = urllib.request.Request(ELSCI_MUSIC, data=json.dumps(payload).encode(), headers=p_headers)
                resp = opener.open(req2, timeout=TIMEOUT)
                body = resp.read().decode("utf-8", errors="replace")
            res = json.loads(body)
            return res.get("items", [])
        except Exception:
            return []

    def _fetch_folder(curr_path, depth=0, prefix=""):
        if depth > max_depth:
            return []
        items = _post_rpc(curr_path)
        if not items and "%" in curr_path:
            items = _post_rpc(urllib.parse.unquote(curr_path))

        unquoted_curr = urllib.parse.unquote(curr_path)
        collected = []
        for it in items:
            h = it.get("href", "")
            unquoted_h = urllib.parse.unquote(h)
            if h == curr_path or unquoted_h == unquoted_curr:
                continue

            # Perbandingan path baik raw encoded maupun unquoted
            if h.startswith(curr_path) or unquoted_h.startswith(unquoted_curr):
                if h.endswith("/"):
                    sub_name = urllib.parse.unquote(h.rstrip("/").split("/")[-1])
                    sub_prefix = f"[{sub_name}] " if prefix == "" else f"{prefix}[{sub_name}] "
                    collected.extend(_fetch_folder(h, depth + 1, sub_prefix))
                else:
                    name = urllib.parse.unquote(h.split("/")[-1])
                    ext = name.split(".")[-1].lower() if "." in name else ""
                    if ext in ["flac", "wav", "mp3", "m4a", "ogg", "opus", "aac", "alac", "dsf", "dff", "cue", "log"]:
                        collected.append({
                            "name": f"{prefix}{name}",
                            "raw_name": name,
                            "url": ELSCI_BASE + h,
                            "size": it.get("size")
                        })
        return collected

    start_path = urllib.parse.urlparse(folder_url).path
    if not start_path.endswith("/"):
        start_path += "/"
    return _fetch_folder(start_path)

def _get_archive_tracks(archive_url):
    """Ambil daftar track audio dari Archive.org metadata API."""
    ident = archive_url.rstrip("/").split("/")[-1]
    url = f"https://archive.org/metadata/{ident}/files"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        data = json.loads(urllib.request.urlopen(req, timeout=TIMEOUT).read().decode("utf-8"))
        files = data.get("result", [])
        tracks = []
        for f in files:
            name = f.get("name", "")
            fmt = f.get("format", "").lower()
            if any(ext in name.lower() for ext in [".flac", ".mp3", ".wav", ".ogg", ".m4a"]):
                dl = f"https://archive.org/download/{ident}/{urllib.parse.quote(name)}"
                tracks.append({
                    "name": name,
                    "url":  dl,
                    "size": f.get("size"),
                    "format": f.get("format")
                })
        return tracks
    except Exception:
        return []

def sanitize_filename(name):
    """Bersihkan string agar aman sebagai nama file atau folder di Linux, macOS, dan Windows."""
    clean = re.sub(r'[\\/*?:"<>|\x00-\x1f]', "_", str(name)).strip()
    clean = clean.rstrip(". ")
    if not clean:
        clean = "unnamed"
    # Windows reserved filenames (CON, PRN, AUX, NUL, COM1-9, LPT1-9)
    stem = clean.split(".")[0].lower()
    reserved = {"con", "prn", "aux", "nul"} | {f"com{i}" for i in range(1, 10)} | {f"lpt{i}" for i in range(1, 10)}
    if stem in reserved:
        clean = f"_{clean}"
    return clean[:200]

def download_file(url, out_path, headers=None, title="", cookie_str=""):
    """Unduh file secara direct via HTTP dengan dukungan resume dan progress bar visual."""
    req_headers = dict(headers or HEADERS)
    if cookie_str:
        req_headers["Cookie"] = cookie_str

    dest_dir = os.path.dirname(out_path)
    if dest_dir:
        os.makedirs(dest_dir, exist_ok=True)

    part_path = out_path + ".part"
    existing_bytes = 0
    if os.path.exists(part_path):
        existing_bytes = os.path.getsize(part_path)
        req_headers["Range"] = f"bytes={existing_bytes}-"

    req = urllib.request.Request(url, headers=req_headers)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            status = getattr(resp, "status", 200)
            total_bytes = None
            crange = resp.headers.get("Content-Range")
            if crange and "/" in crange:
                try:
                    total_bytes = int(crange.split("/")[-1])
                except Exception:
                    pass
            if not total_bytes:
                clen = resp.headers.get("Content-Length")
                if clen:
                    total_bytes = int(clen) + (existing_bytes if status == 206 else 0)

            mode = "ab" if (status == 206 and existing_bytes > 0) else "wb"
            if mode == "wb":
                existing_bytes = 0

            downloaded = existing_bytes
            t_start = time.time()
            last_print = 0

            with open(part_path, mode) as f:
                while True:
                    chunk = resp.read(65536)
                    if not chunk:
                        break
                    f.write(chunk)
                    downloaded += len(chunk)
                    now = time.time()
                    if now - last_print >= 0.25:
                        last_print = now
                        elapsed = now - t_start
                        speed = (downloaded - existing_bytes) / (elapsed + 1e-6)
                        speed_str = f"{speed / (1024*1024):.1f} MB/s" if speed > 1024*1024 else f"{speed / 1024:.0f} KB/s"
                        
                        if total_bytes and total_bytes > 0:
                            pct = min(100.0, (downloaded / total_bytes) * 100)
                            eta = int((total_bytes - downloaded) / (speed + 1e-6))
                            eta_str = f"{eta}s" if eta < 60 else f"{eta//60}m{eta%60}s"
                            bar_len = 24
                            filled = int(bar_len * pct / 100)
                            bar = "█" * filled + "░" * (bar_len - filled)
                            sys.stdout.write(f"\r  \033[92m[{bar}]\033[0m \033[1m{pct:5.1f}%\033[0m ({_bytes_human(downloaded)}/{_bytes_human(total_bytes)}) \033[36m{speed_str}\033[0m ETA: {eta_str}   ")
                        else:
                            sys.stdout.write(f"\r  \033[92m[Unduh]\033[0m {_bytes_human(downloaded)} \033[36m{speed_str}\033[0m   ")
                        sys.stdout.flush()

        if os.path.exists(out_path):
            os.remove(out_path)
        os.rename(part_path, out_path)
        sys.stdout.write(f"\r  \033[92m[✓ Selesai]\033[0m {_bytes_human(downloaded)} tersimpan di: {os.path.basename(out_path)}                               \n")
        sys.stdout.flush()
        return True
    except (KeyboardInterrupt, SystemExit):
        sys.stdout.write(f"\n  \033[93m[!] Download dibatalkan.\033[0m\n")
        sys.stdout.flush()
        return False
    except Exception as e:
        sys.stdout.write(f"\n  \033[93m[!] Error download: {e}\033[0m\n")
        sys.stdout.flush()
        return False

def download_torrent(magnet, title, dest_dir=None):
    """Download torrent/magnet via webtorrent-cli (npx) atau aria2c ke direktori tujuan."""
    if not dest_dir:
        dest_dir = os.path.expanduser("~/Music")
    os.makedirs(dest_dir, exist_ok=True)

    webtorrent = shutil.which("webtorrent")
    npx = shutil.which("npx")
    aria2c = shutil.which("aria2c")

    print(f"\n{_bold(_cyan('════ DOWNLOAD TORRENT / MAGNET ════'))}")
    print(f"Judul : {_bold(title)}")
    print(f"Tujuan: {dest_dir}")

    if webtorrent:
        cmd = [webtorrent, "download", magnet, "-o", dest_dir]
        print(f"{_green('[>] Menjalankan webtorrent...')}")
        try:
            subprocess.run(cmd)
            return True
        except KeyboardInterrupt:
            print(_yellow("\n[!] Download torrent dibatalkan."))
            return False
    elif npx:
        cmd = [npx, "-y", "webtorrent-cli", "download", magnet, "-o", dest_dir]
        print(f"{_green('[>] Menjalankan webtorrent via npx...')}")
        try:
            subprocess.run(cmd)
            return True
        except KeyboardInterrupt:
            print(_yellow("\n[!] Download torrent dibatalkan."))
            return False
    elif aria2c:
        cmd = [aria2c, "--seed-time=0", "-d", dest_dir, magnet]
        print(f"{_green('[>] Menjalankan aria2c...')}")
        try:
            subprocess.run(cmd)
            return True
        except KeyboardInterrupt:
            print(_yellow("\n[!] Download torrent dibatalkan."))
            return False
    else:
        print(_yellow("[!] Downloader CLI (webtorrent / aria2c) tidak ditemukan."))
        print(_dim("    Membuka magnet link ke torrent client default..."))
        if open_system_target(magnet):
            print(_green("[✓] Magnet link dikirim ke torrent client."))
        else:
            print(f"Magnet: {magnet}")
        return False

def download_item(item, track_choice=None, auto_all=False, out_dir=None):
    """Download item (direct FLAC album/track atau torrent magnet) ke ~/Music atau out_dir."""
    source = item.get("source", "")
    title  = item.get("title", "")
    dest_base = out_dir or os.path.expanduser("~/Music")
    os.makedirs(dest_base, exist_ok=True)

    if source == "server.elsci.one":
        cookie_str = _get_elsci_cookies()
        print(f"\n{_bold(_cyan('[>] Mengambil daftar berkas album...'))}")
        tracks = _get_elsci_folder_tracks(item.get("url", ""))
        audio_tracks = [t for t in tracks if any(ext in t["name"].lower() for ext in [".flac", ".mp3", ".wav", ".m4a", ".ogg", ".opus", ".aac", ".alac", ".cue", ".log"])]
        if not audio_tracks:
            audio_tracks = tracks
        if not audio_tracks:
            print(_yellow("[!] Tidak ada file yang dapat diunduh dari album ini."))
            return

        album_folder = os.path.join(dest_base, sanitize_filename(title))
        os.makedirs(album_folder, exist_ok=True)

        if track_choice is not None:
            if 0 <= track_choice < len(audio_tracks):
                t = audio_tracks[track_choice]
                raw_filename = t["raw_name"] if "raw_name" in t else t["name"]
                out_file = os.path.join(album_folder, sanitize_filename(raw_filename))
                print(f"\n{_bold(_cyan(f'[>] Mengunduh track ke: {out_file}'))}")
                download_file(t["url"], out_file, cookie_str=cookie_str, title=t["name"])
            else:
                print(_yellow(f"[!] Nomor track tidak valid (1-{len(audio_tracks)})."))
            return

        if len(audio_tracks) == 1:
            t = audio_tracks[0]
            raw_filename = t["raw_name"] if "raw_name" in t else t["name"]
            out_file = os.path.join(album_folder, sanitize_filename(raw_filename))
            print(f"\n{_bold(_cyan('[>] Mengunduh: ' + t['name']))}")
            download_file(t["url"], out_file, cookie_str=cookie_str, title=t["name"])
            return

        if auto_all:
            p_in = "a"
        else:
            print(f"\n{_bold(_cyan(f'═══ PILIH TRACK DOWNLOAD: {title} ({len(audio_tracks)} file) ═══'))}")
            for i, t in enumerate(audio_tracks, 1):
                sz = _bytes_human(t.get("size"))
                print(f"  {_green(str(i) + '.')} {t['name']} {_dim('[' + sz + ']')}")

            try:
                p_in = input(f"\n{_bold(f'Pilih track untuk diunduh [1-{len(audio_tracks)} / a (Unduh Semua Album) / q (Batal)] > ')}").strip().lower()
            except (EOFError, KeyboardInterrupt):
                return

        if p_in in ["a", "all", "semua"]:
            print(f"\n{_bold(_green(f'[>] Mengunduh seluruh album ({len(audio_tracks)} berkas) ke: {album_folder}'))}")
            total_succ = 0
            for idx, t in enumerate(audio_tracks, 1):
                raw_filename = t["raw_name"] if "raw_name" in t else t["name"]
                out_file = os.path.join(album_folder, sanitize_filename(raw_filename))
                sz_str = _bytes_human(t.get("size"))
                print(f"\n{_bold(f'[{idx}/{len(audio_tracks)}]')} {t['name']} ({sz_str})")
                if os.path.exists(out_file) and t.get("size") and os.path.getsize(out_file) == t.get("size"):
                    print(f"  {_green('[✓] Berkas sudah ada (lengkap), melewati.')}")
                    total_succ += 1
                    continue
                if download_file(t["url"], out_file, cookie_str=cookie_str, title=t["name"]):
                    total_succ += 1
            print(f"\n{_bold(_green(f'[✓] Selesai! {total_succ}/{len(audio_tracks)} berkas tersimpan di: {album_folder}'))}")
        elif p_in.isdigit():
            t_idx = int(p_in) - 1
            if 0 <= t_idx < len(audio_tracks):
                t = audio_tracks[t_idx]
                raw_filename = t["raw_name"] if "raw_name" in t else t["name"]
                out_file = os.path.join(album_folder, sanitize_filename(raw_filename))
                print(f"\n{_bold(_cyan(f'[>] Mengunduh track ke: {out_file}'))}")
                download_file(t["url"], out_file, cookie_str=cookie_str, title=t["name"])
            else:
                print(_yellow(f"[!] Nomor track tidak valid (1-{len(audio_tracks)})."))

    elif source == "Archive.org":
        print(f"\n{_bold(_cyan('[>] Mengambil berkas track Archive.org...'))}")
        tracks = _get_archive_tracks(item.get("url", ""))
        audio_tracks = [t for t in tracks if any(ext in t["name"].lower() for ext in [".flac", ".mp3", ".wav", ".m4a", ".ogg"])]
        if not audio_tracks:
            audio_tracks = tracks
        if not audio_tracks:
            print(_yellow("[!] Tidak ada file audio di item Archive.org ini."))
            return

        album_folder = os.path.join(dest_base, sanitize_filename(title))
        os.makedirs(album_folder, exist_ok=True)

        if track_choice is not None:
            if 0 <= track_choice < len(audio_tracks):
                t = audio_tracks[track_choice]
                out_file = os.path.join(album_folder, sanitize_filename(t["name"]))
                print(f"\n{_bold(_cyan(f'[>] Mengunduh track ke: {out_file}'))}")
                download_file(t["url"], out_file, title=t["name"])
            else:
                print(_yellow(f"[!] Nomor track tidak valid (1-{len(audio_tracks)})."))
            return

        if len(audio_tracks) == 1:
            t = audio_tracks[0]
            out_file = os.path.join(album_folder, sanitize_filename(t["name"]))
            print(f"\n{_bold(_cyan('[>] Mengunduh: ' + t['name']))}")
            download_file(t["url"], out_file, title=t["name"])
            return

        if auto_all:
            p_in = "a"
        else:
            print(f"\n{_bold(_cyan(f'═══ PILIH TRACK DOWNLOAD: {title} ({len(audio_tracks)} file) ═══'))}")
            for i, t in enumerate(audio_tracks, 1):
                sz = _bytes_human(t.get("size"))
                print(f"  {_green(str(i) + '.')} {t['name']} {_dim('[' + sz + ']')}")

            try:
                p_in = input(f"\n{_bold(f'Pilih track untuk diunduh [1-{len(audio_tracks)} / a (Unduh Semua Album) / q (Batal)] > ')}").strip().lower()
            except (EOFError, KeyboardInterrupt):
                return

        if p_in in ["a", "all", "semua"]:
            print(f"\n{_bold(_green(f'[>] Mengunduh seluruh album ({len(audio_tracks)} berkas) ke: {album_folder}'))}")
            total_succ = 0
            for idx, t in enumerate(audio_tracks, 1):
                out_file = os.path.join(album_folder, sanitize_filename(t["name"]))
                sz_str = _bytes_human(t.get("size"))
                print(f"\n{_bold(f'[{idx}/{len(audio_tracks)}]')} {t['name']} ({sz_str})")
                if os.path.exists(out_file) and t.get("size") and os.path.getsize(out_file) == t.get("size"):
                    print(f"  {_green('[✓] Berkas sudah ada (lengkap), melewati.')}")
                    total_succ += 1
                    continue
                if download_file(t["url"], out_file, title=t["name"]):
                    total_succ += 1
            print(f"\n{_bold(_green(f'[✓] Selesai! {total_succ}/{len(audio_tracks)} berkas tersimpan di: {album_folder}'))}")
        elif p_in.isdigit():
            t_idx = int(p_in) - 1
            if 0 <= t_idx < len(audio_tracks):
                t = audio_tracks[t_idx]
                out_file = os.path.join(album_folder, sanitize_filename(t["name"]))
                print(f"\n{_bold(_cyan(f'[>] Mengunduh track ke: {out_file}'))}")
                download_file(t["url"], out_file, title=t["name"])
            else:
                print(_yellow(f"[!] Nomor track tidak valid (1-{len(audio_tracks)})."))

    else:
        # Torrent / Magnet
        magnet = item.get("magnet", "")
        if magnet:
            download_torrent(magnet, title, dest_base)
        else:
            print(_yellow("[!] Magnet link tidak tersedia untuk item ini."))

def get_terminal_player():
    """Cari media player CLI yang tersedia di terminal (cross-platform)."""
    for cmd in ["ffplay", "mpv", "cvlc"]:
        path = shutil.which(cmd)
        if path:
            return cmd, path
    if sys.platform == "darwin":
        for cmd in ["ffplay", "mpv"]:
            for base in ["/opt/homebrew/bin", "/usr/local/bin"]:
                p = os.path.join(base, cmd)
                if os.path.exists(p):
                    return cmd, p
    elif sys.platform.startswith("win"):
        for cmd in ["ffplay", "mpv"]:
            p = shutil.which(f"{cmd}.exe")
            if p:
                return cmd, p
    return None, None

def _play_single_track_terminal(player_cmd, track_name, track_url, album_title, source, cookie_str="", player_bin=None):
    """Putar satu track langsung di terminal menggunakan ffplay, mpv, atau cvlc."""
    bin_path = player_bin or player_cmd
    BOLD = "\033[1m"
    CYAN = "\033[96m"
    GREEN = "\033[92m"
    YELLOW = "\033[93m"
    RESET = "\033[0m"

    w = 74
    print(f"\n{BOLD}{CYAN}┌{'─' * (w-2)}┐{RESET}")
    head_txt = f"🎧 NOW PLAYING (Terminal Player: {player_cmd})"
    print(f"{BOLD}{CYAN}{_box_line(BOLD + head_txt + RESET, len(head_txt) + 1, w)}{RESET}")
    print(f"{BOLD}{CYAN}├{'─' * (w-2)}┤{RESET}")

    t_disp = track_name[:52] if len(track_name) > 52 else track_name
    print(f"{BOLD}{CYAN}{_box_line(f'Track : {GREEN}{t_disp}{RESET}', len(f'Track : {t_disp}'), w)}{RESET}")

    alb_disp = album_title[:52] if len(album_title) > 52 else album_title
    print(f"{BOLD}{CYAN}{_box_line(f'Album : {alb_disp}', len(f'Album : {alb_disp}'), w)}{RESET}")
    print(f"{BOLD}{CYAN}{_box_line(f'Sumber: {source}', len(f'Sumber: {source}'), w)}{RESET}")
    print(f"{BOLD}{CYAN}├{'─' * (w-2)}┤{RESET}")
    print(f"{BOLD}{CYAN}{_box_line(YELLOW + 'Kontrol: [q] Stop/Lanjut  [Spasi] Pause  [←/→] Seek 10s' + RESET, 54, w)}{RESET}")
    print(f"{BOLD}{CYAN}└{'─' * (w-2)}┘{RESET}\n")

    if player_cmd == "ffplay":
        cmd = [bin_path, "-nodisp", "-autoexit", "-hide_banner", "-loglevel", "info"]
        if cookie_str:
            cmd.extend(["-headers", f"User-Agent: Mozilla/5.0\r\nCookie: {cookie_str}\r\n"])
        cmd.append(track_url)
    elif player_cmd == "cvlc":
        cmd = [bin_path, "--play-and-exit"]
        if cookie_str:
            cmd.extend([f":http-header=Cookie: {cookie_str}"])
        cmd.append(track_url)
    elif player_cmd == "mpv":
        cmd = [bin_path, "--no-video"]
        if cookie_str:
            cmd.extend([f"--http-header-fields=Cookie: {cookie_str}"])
        cmd.append(track_url)
    else:
        print(_yellow("[!] Media player terminal tidak didukung."))
        return False

    try:
        subprocess.run(cmd)
        return True
    except KeyboardInterrupt:
        print(f"\n{YELLOW}[!] Pemutaran dihentikan oleh pengguna.{RESET}")
        return False
    except Exception as e:
        print(_yellow(f"[!] Gagal memutar audio: {e}"))
        return False

def play_terminal(item, track_choice=None):
    """Streaming audio langsung di dalam terminal (ffplay/cvlc/mpv)."""
    player_cmd, player_path = get_terminal_player()
    if not player_cmd:
        print(_yellow("[!] Media player terminal tidak ditemukan di sistem."))
        print(_dim("    Linux : sudo apt install ffmpeg mpv"))
        print(_dim("    macOS : brew install ffmpeg mpv"))
        print(_dim("    Win   : winget install Gyan.FFmpeg atau choco install ffmpeg"))
        print(_dim("    Alternatif: gunakan v<nomor> untuk VLC player GUI."))
        return

    source = item.get("source", "")
    title  = item.get("title", "")

    if source not in ["server.elsci.one", "Archive.org"] and item.get("type") != "direct":
        print(_yellow(f"[!] Item ini adalah Torrent/Magnet ({source})."))
        print(_dim("    Streaming langsung via terminal hanya mendukung sumber Direct Lossless (server.elsci.one & Archive.org)."))
        print(_dim("    Gunakan aksi m<nomor> untuk membuka di torrent client, atau download filenya terlebih dahulu."))
        return

    cookie_str = ""
    tracks = []

    if source == "server.elsci.one":
        print(f"\n{_cyan('[>] Menyiapkan daftar track dari server.elsci.one...')}")
        all_files = _get_elsci_folder_tracks(item.get("url", ""))
        cookie_str = _get_elsci_cookies()
        tracks = [t for t in all_files if any(ext in t["name"].lower() for ext in [".flac", ".mp3", ".wav", ".m4a", ".ogg", ".opus"])]
    elif source == "Archive.org":
        print(f"\n{_cyan('[>] Menyiapkan daftar track dari Archive.org...')}")
        tracks = _get_archive_tracks(item.get("url", ""))
    else:
        tracks = [{"name": title, "url": item.get("url") or item.get("magnet"), "size": item.get("size")}]

    if not tracks:
        print(_yellow("[!] Tidak ditemukan berkas audio yang dapat diputar."))
        return

    if len(tracks) == 1:
        _play_single_track_terminal(player_cmd, tracks[0]["name"], tracks[0]["url"], title, source, cookie_str, player_bin=player_path)
        return

    if track_choice is not None:
        if 0 <= track_choice < len(tracks):
            t = tracks[track_choice]
            _play_single_track_terminal(player_cmd, t["name"], t["url"], title, source, cookie_str, player_bin=player_path)
            return
        elif track_choice == -1:
            print(f"\n{_bold(_green(f'[>] Memulai Playlist Mode ({len(tracks)} track)...'))}")
            for idx, t in enumerate(tracks, 1):
                print(f"\n{_dim(f'--- Track {idx} dari {len(tracks)} ---')}")
                ok = _play_single_track_terminal(player_cmd, t["name"], t["url"], title, source, cookie_str, player_bin=player_path)
                if not ok:
                    try:
                        c = input(_yellow("\nLanjut ke track berikutnya? [y/n/q] > ")).strip().lower()
                        if c in ["n", "q", "quit"]:
                            break
                    except (EOFError, KeyboardInterrupt):
                        break
            return

    BOLD = "\033[1m"
    CYAN = "\033[96m"
    GREEN = "\033[92m"
    YELLOW = "\033[93m"
    RESET = "\033[0m"

    print(f"\n{BOLD}{CYAN}═══ DAFTAR TRACK: {title} ({len(tracks)} track) ═══{RESET}")
    for i, t in enumerate(tracks, 1):
        sz = _bytes_human(t.get("size"))
        print(f"  {GREEN}{i:2}.{RESET} {t['name']} {_dim('[' + sz + ']')}")

    while True:
        try:
            p_in = input(f"\n{BOLD}Pilih track [1-{len(tracks)} / a (Putar Semua Album) / q (Kembali)] > {RESET}").strip().lower()
        except (EOFError, KeyboardInterrupt):
            break

        if not p_in or p_in in ["q", "quit", "kembali"]:
            break

        if p_in in ["a", "all", "semua"]:
            print(f"\n{_bold(_green(f'[>] Memulai Playlist Mode ({len(tracks)} track)...'))}")
            for idx, t in enumerate(tracks, 1):
                print(f"\n{_dim(f'--- Track {idx} dari {len(tracks)} ---')}")
                ok = _play_single_track_terminal(player_cmd, t["name"], t["url"], title, source, cookie_str, player_bin=player_path)
                if not ok:
                    try:
                        c = input(_yellow("\nLanjut ke track berikutnya? [y/n/q] > ")).strip().lower()
                        if c in ["n", "q", "quit"]:
                            break
                    except (EOFError, KeyboardInterrupt):
                        break
            break

        if p_in.isdigit():
            t_idx = int(p_in) - 1
            if 0 <= t_idx < len(tracks):
                t = tracks[t_idx]
                _play_single_track_terminal(player_cmd, t["name"], t["url"], title, source, cookie_str, player_bin=player_path)
            else:
                print(f"{YELLOW}[!] Nomor track tidak valid (1-{len(tracks)}).{RESET}")

def _find_vlc():
    """Temukan lokasi executable VLC secara cross-platform (Linux, macOS, Windows)."""
    vlc = shutil.which("vlc")
    if vlc:
        return vlc
    if sys.platform == "darwin":
        mac_paths = [
            "/Applications/VLC.app/Contents/MacOS/VLC",
            os.path.expanduser("~/Applications/VLC.app/Contents/MacOS/VLC"),
            "/opt/homebrew/bin/vlc",
            "/usr/local/bin/vlc"
        ]
        for p in mac_paths:
            if os.path.exists(p):
                return p
    elif sys.platform.startswith("win"):
        win_paths = [
            os.path.expandvars(r"%ProgramFiles%\VideoLAN\VLC\vlc.exe"),
            os.path.expandvars(r"%ProgramFiles(x86)%\VideoLAN\VLC\vlc.exe"),
            os.path.expandvars(r"%LocalAppData%\Programs\VideoLAN\VLC\vlc.exe"),
        ]
        for p in win_paths:
            if os.path.exists(p):
                return p
    return None

def play_vlc(item):
    """Streaming audio langsung di VLC player."""
    vlc = _find_vlc()
    if not vlc:
        print(_yellow("[!] VLC player tidak terpasang di sistem."))
        print(_dim("    Linux : sudo apt install vlc"))
        print(_dim("    macOS : brew install --cask vlc atau unduh di videolan.org"))
        print(_dim("    Win   : winget install VideoLAN.VLC atau unduh di videolan.org"))
        return

    source = item.get("source", "")
    title  = item.get("title", "")
    print(f"\n{_cyan('[>] Menyiapkan pemutaran di VLC untuk:')} {_bold(title)}")

    play_urls = []
    if source == "server.elsci.one":
        tracks = _get_elsci_folder_tracks(item.get("url", ""))
        audio_tracks = [t for t in tracks if any(ext in t["name"].lower() for ext in [".flac", ".mp3", ".wav", ".m4a"])]
        if audio_tracks:
            play_urls = [t["url"] for t in audio_tracks]
            print(f"    Ditemukan {len(play_urls)} track audio — memuat ke playlist VLC...")
        else:
            play_urls = [item.get("url", "")]
    elif source == "Archive.org":
        tracks = _get_archive_tracks(item.get("url", ""))
        if tracks:
            play_urls = [t["url"] for t in tracks]
            print(f"    Ditemukan {len(play_urls)} track audio — memuat ke playlist VLC...")
        else:
            play_urls = [item.get("magnet") or item.get("url", "")]
    else:
        # Torrent magnet link
        magnet = item.get("magnet", "")
        if magnet:
            play_urls = [magnet]

    if not play_urls:
        print(_yellow("[!] Tidak ditemukan link streaming/audio untuk item ini."))
        return

    cookie_str = _get_elsci_cookies() if source == "server.elsci.one" else ""
    vlc_args = [vlc]
    if cookie_str:
        vlc_args.append(f":http-header=Cookie: {cookie_str}")
    vlc_args.extend(play_urls)

    try:
        subprocess.Popen(vlc_args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        print(_green(f"[✓] Berhasil diluncurkan di VLC player!"))
    except Exception as e:
        print(_yellow(f"[!] Gagal meluncurkan VLC: {e}"))

def show_tracks_or_details(item):
    """Tampilkan detail lengkap, daftar file track, atau info magnet link."""
    source = item.get("source", "")
    title  = item.get("title", "")
    print(f"\n{_bold(_cyan('═' * 62))}")
    print(f"{_bold('🎵 DETAIL:')} {_bold(title)}")
    print(f"Sumber  : {source} | Tipe: {item.get('type')}")
    print(f"URL     : {item.get('url')}")
    print(f"{_bold(_cyan('═' * 62))}")

    if source == "server.elsci.one":
        print("Mengambil daftar berkas album...")
        tracks = _get_elsci_folder_tracks(item.get("url", ""))
        if tracks:
            print(f"\nDaftar File ({len(tracks)} berkas):")
            for i, t in enumerate(tracks, 1):
                sz = _bytes_human(t.get("size"))
                print(f"  {_green(str(i) + '.')} {t['name']} [{sz}]")
                print(f"     Direct Download: {_dim(t['url'])}")
            try:
                sub = input(f"\n\033[1m\033[92mAksi Track [t<nomor> Putar | dl<nomor> Unduh Track | a Unduh Semua Album | v Buka di VLC | q Kembali] > \033[0m").strip()
                if (sub.lower().startswith("t") or sub.lower().startswith("p")) and sub[1:].isdigit():
                    t_no = int(sub[1:]) - 1
                    play_terminal(item, track_choice=t_no)
                elif sub.lower().startswith("dl") and sub[2:].isdigit():
                    t_no = int(sub[2:]) - 1
                    download_item(item, track_choice=t_no)
                elif sub.lower().startswith("u") and sub[1:].isdigit():
                    t_no = int(sub[1:]) - 1
                    download_item(item, track_choice=t_no)
                elif sub.lower() in ["a", "all", "semua"]:
                    download_item(item, auto_all=True)
                elif sub.lower() == "v":
                    play_vlc(item)
            except (EOFError, KeyboardInterrupt):
                pass
        else:
            print(f"Tautan Folder: {item.get('url')}")

    elif source == "Archive.org":
        print("Mengambil berkas track...")
        tracks = _get_archive_tracks(item.get("url", ""))
        if tracks:
            print(f"\nDaftar File Audio ({len(tracks)} track):")
            for i, t in enumerate(tracks, 1):
                sz = _bytes_human(t.get("size"))
                print(f"  {_green(str(i) + '.')} {t['name']} ({t.get('format', 'audio')}) [{sz}]")
                print(f"     Direct Download: {_dim(t['url'])}")
            try:
                sub = input(f"\n\033[1m\033[92mAksi Track [t<nomor> Putar | dl<nomor> Unduh Track | a Unduh Semua Album | v Buka di VLC | q Kembali] > \033[0m").strip()
                if (sub.lower().startswith("t") or sub.lower().startswith("p")) and sub[1:].isdigit():
                    t_no = int(sub[1:]) - 1
                    play_terminal(item, track_choice=t_no)
                elif sub.lower().startswith("dl") and sub[2:].isdigit():
                    t_no = int(sub[2:]) - 1
                    download_item(item, track_choice=t_no)
                elif sub.lower().startswith("u") and sub[1:].isdigit():
                    t_no = int(sub[1:]) - 1
                    download_item(item, track_choice=t_no)
                elif sub.lower() in ["a", "all", "semua"]:
                    download_item(item, auto_all=True)
                elif sub.lower() == "v":
                    play_vlc(item)
            except (EOFError, KeyboardInterrupt):
                pass
        else:
            print(f"Download URL: {item.get('magnet')}")

    else:
        magnet = item.get("magnet", "")
        print(f"Seeds   : {item.get('seeds')} | Leeches: {item.get('leeches')}")
        print(f"Ukuran  : {item.get('size')}")
        print(f"\n{_bold('Magnet Link:')}")
        print(f"{_dim(magnet)}")
        try:
            sub = input(f"\n\033[1m\033[92mAksi [dl Unduh Torrent/Magnet ke ~/Music | q Kembali] > \033[0m").strip().lower()
            if sub in ["dl", "d", "unduh", "download"]:
                download_torrent(magnet, title)
        except (EOFError, KeyboardInterrupt):
            pass

def _box_line(text, visual_len=None, width=74):
    vlen = visual_len if visual_len is not None else len(text)
    pad = width - 3 - vlen
    if pad < 0: pad = 0
    return f"│ {text}{' ' * pad}│"

def interactive_menu(flat_results):
    """Menu aksi interaktif ala Movie Search."""
    if not flat_results:
        return "quit"

    BOLD = "\033[1m"
    DIM  = "\033[2m"
    CYAN = "\033[96m"
    GREEN = "\033[92m"
    YELLOW = "\033[93m"
    MAGENTA = "\033[95m"
    RESET = "\033[0m"

    n = len(flat_results)
    header_title = f"🎧 AKSI CEPAT INTERAKTIF (Pilih musik 1-{n}):"

    w = 74
    print(f"\n{BOLD}{CYAN}┌{'─' * (w-2)}┐{RESET}")
    print(f"{BOLD}{CYAN}{_box_line(BOLD + header_title + RESET, len(header_title) + 1, w)}{RESET}")
    print(f"{BOLD}{CYAN}├{'─' * (w-2)}┤{RESET}")
    print(f"{BOLD}{CYAN}{_box_line(GREEN + 't<nomor>' + RESET + '   ' + BOLD + 'Streaming Langsung di Terminal (ffplay)' + RESET + '     (misal: ' + GREEN + 't1/p1' + RESET + ')', 69, w)}{RESET}")
    print(f"{BOLD}{CYAN}{_box_line(YELLOW + 'v<nomor>' + RESET + '   ' + BOLD + 'Streaming Audio di GUI VLC Player' + RESET + '           (misal: ' + YELLOW + 'v1' + RESET + ')', 66, w)}{RESET}")
    print(f"{BOLD}{CYAN}{_box_line(CYAN + 'dl<nomor>' + RESET + '  ' + BOLD + 'Unduh Track / Album / Torrent ke ~/Music' + RESET + '    (misal: ' + CYAN + 'dl1' + RESET + ')', 67, w)}{RESET}")
    print(f"{BOLD}{CYAN}{_box_line(CYAN + 'd<nomor>' + RESET + '   ' + BOLD + 'Lihat Daftar Track & Direct Link Download' + RESET + '   (misal: ' + CYAN + 'd1' + RESET + ')', 66, w)}{RESET}")
    print(f"{BOLD}{CYAN}{_box_line(MAGENTA + 'm<nomor>' + RESET + '   ' + BOLD + 'Buka Magnet Link di Torrent Client' + RESET + '          (misal: ' + MAGENTA + 'm1' + RESET + ')', 66, w)}{RESET}")
    print(f"{BOLD}{CYAN}{_box_line(BOLD + '<nomor>' + RESET + '    ' + BOLD + 'Buka Tautan / Album di Browser (Chrome)' + RESET, 50, w)}{RESET}")
    print(f"{BOLD}{CYAN}├{'─' * (w-2)}┤{RESET}")
    print(f"{BOLD}{CYAN}{_box_line(BOLD + '[c] Cari Musik Lain' + RESET + '  │  ' + BOLD + '[q] Keluar' + RESET, 34, w)}{RESET}")
    print(f"{BOLD}{CYAN}└{'─' * (w-2)}┘{RESET}")

    while True:
        try:
            choice = input(f"\n{BOLD}{GREEN}Aksi Anda [1-{n} / t1 / v1 / dl1 / d1 / m1 / c / q] > {RESET}").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nKeluar.")
            return "quit"

        if not choice or choice.lower() in ["q", "quit", "exit"]:
            print(f"{BOLD}Selesai.{RESET}")
            return "quit"

        if choice.lower() in ["c", "cari"]:
            return "search_again"

        # Download Track / Album / Torrent: dl<nomor>
        if choice.lower().startswith("dl") and choice[2:].isdigit():
            idx = int(choice[2:]) - 1
            if 0 <= idx < n:
                download_item(flat_results[idx])
            else:
                print(f"{YELLOW}[!] Nomor tidak valid. Pilih antara 1-{n}.{RESET}")
            continue

        # Streaming Terminal: t<nomor> atau p<nomor>
        if (choice.lower().startswith("t") or choice.lower().startswith("p")) and choice[1:].isdigit():
            idx = int(choice[1:]) - 1
            if 0 <= idx < n:
                play_terminal(flat_results[idx])
            else:
                print(f"{YELLOW}[!] Nomor tidak valid. Pilih antara 1-{n}.{RESET}")
            continue

        # Streaming VLC: v<nomor>
        if choice.lower().startswith("v") and choice[1:].isdigit():
            idx = int(choice[1:]) - 1
            if 0 <= idx < n:
                play_vlc(flat_results[idx])
            else:
                print(f"{YELLOW}[!] Nomor tidak valid. Pilih antara 1-{n}.{RESET}")
            continue

        # Detail track / link download: d<nomor>
        if choice.lower().startswith("d") and choice[1:].isdigit():
            idx = int(choice[1:]) - 1
            if 0 <= idx < n:
                show_tracks_or_details(flat_results[idx])
            else:
                print(f"{YELLOW}[!] Nomor tidak valid. Pilih antara 1-{n}.{RESET}")
            continue

        # Magnet link: m<nomor>
        if choice.lower().startswith("m") and choice[1:].isdigit():
            idx = int(choice[1:]) - 1
            if 0 <= idx < n:
                item = flat_results[idx]
                magnet = item.get("magnet", "")
                if magnet and magnet.startswith("magnet:"):
                    print(f"\n{BOLD}{MAGENTA}Magnet Link [{item.get('title')}]:{RESET}")
                    print(magnet)
                    if open_system_target(magnet):
                        print(f"{GREEN}[✓] Mengirim magnet link ke torrent client default...{RESET}")
                    else:
                        print(f"{YELLOW}[!] Tidak dapat membuka magnet link secara otomatis.{RESET}")
                else:
                    print(f"{YELLOW}[!] Item ini menggunakan direct link, bukan magnet link. Gunakan aksi nomor atau d{idx+1}.{RESET}")
            else:
                print(f"{YELLOW}[!] Nomor tidak valid. Pilih antara 1-{n}.{RESET}")
            continue

        # Nomor langsung -> buka di browser
        if choice.isdigit():
            idx = int(choice) - 1
            if 0 <= idx < n:
                item = flat_results[idx]
                target = item.get("magnet") if item.get("type") == "direct" else item.get("url")
                if not target:
                    target = item.get("url")
                print(f"\n{_cyan('[>] Membuka di browser:')} {target}")
                opened = open_url(target)
                if opened:
                    print(_green(f"[✓] Berhasil dibuka di {opened}!"))
                else:
                    print(_yellow(f"[!] Browser tidak ditemukan. Buka manual: {target}"))
            else:
                print(f"{YELLOW}[!] Nomor tidak valid. Pilih antara 1-{n}.{RESET}")
            continue

        print(f"{YELLOW}Perintah tidak dikenali. Ketik nomor (1), t1/p1 (Terminal), v1 (VLC), d1 (Detail Track), m1 (Magnet), c (Cari), atau q.{RESET}")

def full_interactive_prompt(args):
    """Pusat kontrol interaktif lengkap."""
    BOLD = "\033[1m"
    CYAN = "\033[96m"
    GREEN = "\033[92m"
    YELLOW = "\033[93m"
    MAGENTA = "\033[95m"
    RESET = "\033[0m"

    while True:
        print(f"\n{BOLD}{CYAN}┌{'─' * 72}┐{RESET}")
        print(f"{BOLD}{CYAN}{_box_line(BOLD + '🎧 MUSIC HI-RES SEARCH — PUSAT KONTROL INTERAKTIF' + RESET, 49)}{RESET}")
        print(f"{BOLD}{CYAN}├{'─' * 72}┤{RESET}")
        print(f"{BOLD}{CYAN}{_box_line(GREEN + '[1]' + RESET + '  ' + BOLD + 'Cari Judul Album / Artis (Semua 7 Sumber)' + RESET, 44)}{RESET}")
        print(f"{BOLD}{CYAN}{_box_line(GREEN + '[2]' + RESET + '  ' + BOLD + 'Cari Direct Download & Stream (elsci & archive.org)' + RESET, 51)}{RESET}")
        print(f"{BOLD}{CYAN}{_box_line(GREEN + '[3]' + RESET + '  ' + BOLD + 'Cari Konser Live Soundboard (Archive Live / etree)' + RESET, 50)}{RESET}")
        print(f"{BOLD}{CYAN}{_box_line(GREEN + '[4]' + RESET + '  ' + BOLD + 'Cari Anime OST, J-Pop & Mora Hi-Res (Nyaa.si)' + RESET, 45)}{RESET}")
        print(f"{BOLD}{CYAN}{_box_line(GREEN + '[5]' + RESET + '  ' + BOLD + 'Cari Discography & Vinyl Rips (The Pirate Bay)' + RESET, 46)}{RESET}")
        print(f"{BOLD}{CYAN}{_box_line(GREEN + '[6]' + RESET + '  ' + BOLD + 'Cari Format Tertentu (FLAC, Hi-Res, WAV, MP3)' + RESET, 48)}{RESET}")
        print(f"{BOLD}{CYAN}├{'─' * 72}┤{RESET}")
        print(f"{BOLD}{CYAN}{_box_line(YELLOW + '[q]' + RESET + '  ' + BOLD + 'Keluar' + RESET, 11)}{RESET}")
        print(f"{BOLD}{CYAN}└{'─' * 72}┘{RESET}")

        try:
            mode = input(f"  {BOLD}{GREEN}Pilih Menu [1-6 / q] > {RESET}").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print("\nKeluar.")
            break

        if mode in ["q", "quit", "exit"]:
            print(f"{BOLD}Sampai jumpa!{RESET}")
            break

        src_choice = None
        fmt_choice = args.format or "flac"

        if mode == "1":
            try:
                q_in = input(f"\n  {BOLD}Masukkan nama album atau artis > {RESET}").strip()
            except (EOFError, KeyboardInterrupt):
                break
            if not q_in:
                print(f"  {YELLOW}[!] Query tidak boleh kosong.{RESET}")
                continue

        elif mode == "2":
            try:
                q_in = input(f"\n  {BOLD}Masukkan nama album/artis (Direct Download) > {RESET}").strip()
            except (EOFError, KeyboardInterrupt):
                break
            if not q_in:
                print(f"  {YELLOW}[!] Query tidak boleh kosong.{RESET}")
                continue
            print(f"  {CYAN}Pilihan sumber direct:{RESET} [1] elsci (24-bit studio)  [2] archive  [Enter] Keduanya")
            try:
                s_pick = input("  Pilih sumber [1/2/Enter] > ").strip()
            except (EOFError, KeyboardInterrupt):
                break
            if s_pick == "1":
                src_choice = "elsci"
            elif s_pick == "2":
                src_choice = "archive"

        elif mode == "3":
            try:
                q_in = input(f"\n  {BOLD}Masukkan nama band / musisi (Live Soundboard) > {RESET}").strip()
            except (EOFError, KeyboardInterrupt):
                break
            if not q_in:
                print(f"  {YELLOW}[!] Query tidak boleh kosong.{RESET}")
                continue
            src_choice = "archive_live"

        elif mode == "4":
            try:
                q_in = input(f"\n  {BOLD}Masukkan judul anime / lagu / artis (Nyaa.si) > {RESET}").strip()
            except (EOFError, KeyboardInterrupt):
                break
            if not q_in:
                print(f"  {YELLOW}[!] Query tidak boleh kosong.{RESET}")
                continue
            src_choice = "nyaa"

        elif mode == "5":
            try:
                q_in = input(f"\n  {BOLD}Masukkan nama album/artis (The Pirate Bay) > {RESET}").strip()
            except (EOFError, KeyboardInterrupt):
                break
            if not q_in:
                print(f"  {YELLOW}[!] Query tidak boleh kosong.{RESET}")
                continue
            src_choice = "piratebay"

        elif mode == "6":
            try:
                q_in = input(f"\n  {BOLD}Masukkan nama album atau artis > {RESET}").strip()
                fmt_in = input(f"  {BOLD}Pilih format audio [flac / wav / mp3 / aac] (default: flac) > {RESET}").strip().lower()
            except (EOFError, KeyboardInterrupt):
                break
            if not q_in:
                print(f"  {YELLOW}[!] Query tidak boleh kosong.{RESET}")
                continue
            if fmt_in:
                fmt_choice = fmt_in
        else:
            print(f"  {YELLOW}[!] Pilihan tidak valid. Ketik 1, 2, 3, 4, 5, 6, atau q.{RESET}")
            continue

        print(f"\n{BOLD}Mencari :{RESET} {BOLD}{q_in}{RESET} [{fmt_choice.upper()}]  |  Sumber: {src_choice or 'semua'}")
        results = search_all(q_in, fmt_choice, args.sort, args.page, args.limit, src_choice)
        flat = print_results(results, as_json=False)
        if flat:
            act = interactive_menu(flat)
            if act == "quit":
                break

# ── CLI ───────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(
        description="Music Hi-Res Search — cari FLAC/lossless dari berbagai sumber.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    ap.add_argument("-q", "--query",  help="Query pencarian")
    ap.add_argument("-s", "--source", choices=list(SOURCES), help="Sumber spesifik (elsci, nyaa, piratebay, archive, archive_live, torrent, rutracker)")
    ap.add_argument("--format", default="flac", help="Format audio (default: flac)")
    ap.add_argument("--type",   choices=["album", "single", "artist"], help="Hint tipe")
    ap.add_argument("--sort",   choices=["seeds", "size", "date"], default="seeds")
    ap.add_argument("-p", "--page", type=int, default=1)
    ap.add_argument("--limit",  type=int, default=10)
    ap.add_argument("--web",    action="store_true", help="Jalankan antarmuka Web Player server")
    ap.add_argument("--port",   type=int, default=8080, help="Port untuk server Web (default: 8080)")
    ap.add_argument("-i", "--interactive", action="store_true", help="Jalankan dalam mode interaktif penuh")
    ap.add_argument("-t", "--stream", "--play", dest="play_idx", type=int, metavar="N", help="Streaming langsung audio nomor N di terminal player (ffplay)")
    ap.add_argument("--vlc", dest="vlc_idx", type=int, metavar="N", help="Streaming audio nomor N di VLC player GUI")
    ap.add_argument("-d", "--download", dest="dl_idx", type=int, metavar="N", help="Unduh audio/album/torrent nomor N ke ~/Music")
    ap.add_argument("--download-all", dest="dl_all_idx", type=int, metavar="N", help="Unduh seluruh file album nomor N ke ~/Music")
    ap.add_argument("--out-dir", dest="out_dir", default=None, help="Direktori penyimpanan unduhan (default: ~/Music)")
    ap.add_argument("--json",   action="store_true")
    ap.add_argument("--sources", action="store_true")
    args = ap.parse_args()

    # Sambungkan WARP secara otomatis jika tersedia di sistem
    ensure_warp_service()

    if args.web:
        try:
            import app as web_app
            host = os.environ.get("HOST", "0.0.0.0")
            port = int(os.environ.get("PORT", args.port))
            print(f"🎵 Music Search Web Server aktif di http://{host}:{port}")
            web_app.app.run(host=host, port=port, server=web_app.ThreadedServer, quiet=True)
        except Exception as e:
            print(f"Gagal menjalankan web server: {e}")
        return

    if args.sources:
        rt_ok = bool(os.environ.get("RUTRACKER_USER"))
        print("Sumber aktif:")
        notes = {
            "elsci":        "server.elsci.one/Music/ — direct FLAC/Hi-Res open directory",
            "nyaa":         "nyaa.si — anime OST, J-Pop/J-Rock, dan Mora 24-bit Hi-Res FLAC",
            "piratebay":    "thepiratebay (tpb.party) — discography lengkap, vinyl rips & western FLAC",
            "archive":      "archive.org — direct FLAC legal, netlabels & historical recordings",
            "archive_live": "archive.org Live (etree) — 250k+ soundboard live concert FLAC recordings",
            "torrent":      "torrents-csv.com — aggregasi multi-tracker global, tanpa login",
            "rutracker":    "rutracker.org — koleksi FLAC J-music terlengkap" + (
                            " [login OK]" if rt_ok else " [BUTUH: RUTRACKER_USER + RUTRACKER_PASS]"),
        }
        for k, note in notes.items():
            ok = k != "rutracker" or rt_ok
            print(f"  {_green('✓') if ok else _yellow('!')} {k:12}: {note}")
        return

    # Jika dipanggil tanpa query atau dengan flag -i tanpa query -> Masuk mode interaktif penuh
    if not args.query or (args.interactive and not args.query):
        full_interactive_prompt(args)
        return

    q = args.query
    if args.type == "album":
        q = f"{q} album"

    if not args.json:
        print(f"Mencari : {_bold(q)} [{args.format.upper()}]")
        print(f"Sort    : {args.sort}  |  Page: {args.page}  |  Sumber: {args.source or 'semua'}")
        print()

    results = search_all(q, args.format, args.sort, args.page, args.limit, args.source)
    valid_items = print_results(results, as_json=args.json)

    errors = sum(
        1 for items in results.values()
        for item in (items if isinstance(items, list) else [])
        if "error" in item
    )
    total = sum(
        len(items) for items in results.values()
        if isinstance(items, list)
    ) - errors

    if not args.json:
        print(f"\n{'─'*62}")
        print(f"Total: {total} hasil | {errors} sumber error/tidak terkonfigurasi")
        if not args.source:
            print(_dim("Tip: -s elsci untuk direct download FLAC/Hi-Res server.elsci.one"))
            print(_dim("Tip: -s archive untuk direct download Archive.org"))
            print(_dim("Tip: -i untuk masuk ke mode interaktif (streaming VLC / buka browser)"))

    if args.play_idx:
        if valid_items and 1 <= args.play_idx <= len(valid_items):
            play_terminal(valid_items[args.play_idx - 1])
        else:
            print(_yellow(f"[!] Nomor tidak valid. Tersedia 1 - {len(valid_items) if valid_items else 0}."))
    elif args.vlc_idx:
        if valid_items and 1 <= args.vlc_idx <= len(valid_items):
            play_vlc(valid_items[args.vlc_idx - 1])
        else:
            print(_yellow(f"[!] Nomor tidak valid. Tersedia 1 - {len(valid_items) if valid_items else 0}."))
    elif args.dl_idx:
        if valid_items and 1 <= args.dl_idx <= len(valid_items):
            download_item(valid_items[args.dl_idx - 1], out_dir=args.out_dir)
        else:
            print(_yellow(f"[!] Nomor tidak valid. Tersedia 1 - {len(valid_items) if valid_items else 0}."))
    elif args.dl_all_idx:
        if valid_items and 1 <= args.dl_all_idx <= len(valid_items):
            download_item(valid_items[args.dl_all_idx - 1], auto_all=True, out_dir=args.out_dir)
        else:
            print(_yellow(f"[!] Nomor tidak valid. Tersedia 1 - {len(valid_items) if valid_items else 0}."))
    elif args.interactive and valid_items:
        interactive_menu(valid_items)

if __name__ == "__main__":
    main()
