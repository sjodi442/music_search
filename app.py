#!/usr/bin/env python3
"""
Web-based Lossless Music Search Server.
Features:
- Multi-source lossless audio search
- In-browser chunked streaming proxy (with HTTP Range seeking)
- On-the-fly streaming album ZIP downloader (zero disk overhead)
- RuTracker / Torrents-CSV / Elsci / Archive.org integration
"""

import os
import sys
import io
import re
import time
import json
import zipfile
import urllib.parse
import urllib.request
from socketserver import ThreadingMixIn
from wsgiref.simple_server import WSGIServer, WSGIRequestHandler, make_server
from bottle import Bottle, ServerAdapter, request, response, static_file

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

import music_search

app = Bottle()

# Caching in-memory (TTL 300s untuk tracks, 60s untuk pencarian)
_TRACKS_CACHE = {}
_SEARCH_CACHE = {}
TRACKS_CACHE_TTL = 300
SEARCH_CACHE_TTL = 60

class ThreadedWSGIServer(ThreadingMixIn, WSGIServer):
    daemon_threads = True

class ThreadedServer(ServerAdapter):
    def run(self, handler):
        class QuietHandler(WSGIRequestHandler):
            def log_message(self, format, *args):
                pass
        httpd = make_server(self.host, self.port, handler, server_class=ThreadedWSGIServer, handler_class=QuietHandler)
        httpd.serve_forever()

music_search.ensure_warp_service()

@app.route("/")
def index():
    return static_file("index.html", root=os.path.join(BASE_DIR, "static"))

@app.route("/static/<filename:path>")
def serve_static(filename):
    return static_file(filename, root=os.path.join(BASE_DIR, "static"))

@app.route("/api/search")
def api_search():
    response.content_type = "application/json"
    q = request.query.get("q", "").strip()
    fmt = request.query.get("format", "flac")
    sort = request.query.get("sort", "seeds")
    source = request.query.get("source", "").strip() or None
    try:
        limit = int(request.query.get("limit", 15))
    except Exception:
        limit = 15

    if not q:
        return json.dumps({"query": "", "items": []})

    cache_key = (q.lower(), fmt, sort, source, limit)
    now = time.time()
    if cache_key in _SEARCH_CACHE:
        ts, cached_res = _SEARCH_CACHE[cache_key]
        if now - ts < SEARCH_CACHE_TTL:
            return cached_res

    results = music_search.search_all(q, fmt=fmt, sort=sort, page=1, limit=limit, source=source)
    
    flattened = []
    for src, items in results.items():
        if isinstance(items, list):
            for it in items:
                if "error" not in it:
                    flattened.append(it)

    # Ranking relevansi: prioritaskan item yang judul/artisnya mengandung token query pengguna
    q_tokens = [t.lower() for t in re.sub(r'[^\w\s]', ' ', q).split() if len(t) > 1]
    def _rank_score(it):
        title = (it.get("title") or "").lower()
        score = 0
        if q.lower() in title:
            score += 100
        for token in q_tokens:
            if token in title:
                score += 20
        # Bobot tambahan untuk seeds / downloads
        seeds_raw = str(it.get("seeds", "0")).replace(" DL", "").replace(",", "")
        m_seed = re.search(r'\d+', seeds_raw)
        seeds_num = int(m_seed.group(0)) if m_seed else 0
        score += min(seeds_num, 50)
        return score

    if sort in ("seeds", "relevance", None, ""):
        flattened.sort(key=_rank_score, reverse=True)

    res_json = json.dumps({
        "query": q,
        "total": len(flattened),
        "items": flattened
    })

    if len(_SEARCH_CACHE) > 200:
        _SEARCH_CACHE.clear()
    _SEARCH_CACHE[cache_key] = (now, res_json)

    return res_json

@app.route("/api/tracks")
def api_tracks():
    response.content_type = "application/json"
    url = request.query.get("url", "").strip()
    source = request.query.get("source", "").strip()

    if not url:
        return json.dumps({"tracks": []})

    cache_key = (source, urllib.parse.unquote(url))
    now = time.time()
    if cache_key in _TRACKS_CACHE:
        ts, cached_tracks = _TRACKS_CACHE[cache_key]
        if now - ts < TRACKS_CACHE_TTL:
            return json.dumps({"url": url, "tracks": cached_tracks, "cached": True})

    tracks = []
    try:
        if "elsci" in source or "elsci.one" in url:
            tracks = music_search._get_elsci_folder_tracks(url)
        elif "archive" in source or "archive.org" in url:
            tracks = music_search._get_archive_tracks(url)
        
        if tracks:
            if len(_TRACKS_CACHE) > 300:
                _TRACKS_CACHE.clear()
            _TRACKS_CACHE[cache_key] = (now, tracks)
    except Exception as e:
        return json.dumps({"error": str(e), "tracks": []})

    return json.dumps({
        "url": url,
        "tracks": tracks
    })

@app.route("/api/stream")
def api_stream():
    """
    Proxy stream audio langsung ke browser.
    Meneruskan byte range, Content-Type, dan header cookie elsci agar browser bisa seek/play audio.
    """
    target_url = request.query.get("url", "").strip()
    is_download = request.query.get("download", "") == "1"

    if not target_url:
        response.status = 400
        return "Target URL missing"

    req_headers = dict(music_search.HEADERS)
    if "elsci.one" in target_url:
        cookie_str = music_search._get_elsci_cookies()
        if cookie_str:
            req_headers["Cookie"] = cookie_str

    range_header = request.headers.get("Range")
    if range_header:
        req_headers["Range"] = range_header

    try:
        req = urllib.request.Request(target_url, headers=req_headers)
        upstream = urllib.request.urlopen(req, timeout=30)
        
        status_code = getattr(upstream, "status", 200)
        response.status = status_code

        for h in ["Content-Type", "Content-Range", "Accept-Ranges", "Content-Length"]:
            val = upstream.headers.get(h)
            if val:
                response.set_header(h, val)

        if is_download:
            filename = target_url.split("/")[-1].split("?")[0]
            response.set_header("Content-Disposition", f'attachment; filename="{filename}"')

        def stream_generator():
            try:
                while True:
                    chunk = upstream.read(65536)
                    if not chunk:
                        break
                    yield chunk
            finally:
                upstream.close()

        return stream_generator()

    except Exception as e:
        response.status = 500
        return f"Streaming error: {e}"

@app.route("/api/download_album")
def api_download_album():
    """
    Download semua track dalam folder album sebagai file ZIP secara streaming (on-the-fly).
    Zero disk storage overhead — zip dibuat langsung di memori stream buffer.
    """
    album_url = request.query.get("url", "").strip()
    source = request.query.get("source", "").strip()
    album_name = request.query.get("name", "album").strip()

    if not album_url:
        response.status = 400
        return "Album URL missing"

    try:
        if "elsci" in source or "elsci.one" in album_url:
            tracks = music_search._get_elsci_folder_tracks(album_url)
        elif "archive" in source or "archive.org" in album_url:
            tracks = music_search._get_archive_tracks(album_url)
        else:
            tracks = []
    except Exception as e:
        response.status = 500
        return f"Error retrieving tracks: {e}"

    if not tracks:
        response.status = 404
        return "No tracks found to download"

    safe_title = music_search.sanitize_filename(album_name) or "album"
    response.set_header("Content-Type", "application/zip")
    response.set_header("Content-Disposition", f'attachment; filename="{safe_title}.zip"')

    def zip_stream():
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, mode="w", compression=zipfile.ZIP_STORED) as zf:
            for tr in tracks:
                tr_url = tr.get("url")
                tr_name = tr.get("name") or tr_url.split("/")[-1].split("?")[0]
                tr_name = music_search.sanitize_filename(tr_name)

                req_headers = dict(music_search.HEADERS)
                if "elsci.one" in tr_url:
                    cookie_str = music_search._get_elsci_cookies()
                    if cookie_str:
                        req_headers["Cookie"] = cookie_str

                try:
                    req = urllib.request.Request(tr_url, headers=req_headers)
                    with urllib.request.urlopen(req, timeout=30) as resp:
                        with zf.open(tr_name, mode="w") as dest:
                            while True:
                                data = resp.read(65536)
                                if not data:
                                    break
                                dest.write(data)
                                if buffer.tell() > 65536:
                                    chunk = buffer.getvalue()
                                    buffer.seek(0)
                                    buffer.truncate(0)
                                    yield chunk
                except Exception:
                    continue

        remainder = buffer.getvalue()
        if remainder:
            yield remainder

    return zip_stream()

if __name__ == "__main__":
    host = os.environ.get("HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", 8080))
    print(f"🎵 Music Search Web Server aktif di http://{host}:{port}")
    app.run(host=host, port=port, server=ThreadedServer, quiet=True)
