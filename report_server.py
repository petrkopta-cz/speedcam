#!/usr/bin/env python3
"""speedcam report server: live statistics + calibration preview on port 8085.

Standard library only. Read-only access to traffic.db and traffic_log.csv.
Serves no vehicle images: only the calibration preview and aggregate numbers.
"""
import html
import os
import sqlite3
import datetime
import http.server
import socketserver
import subprocess
import time
import urllib.parse
import yaml

import setup_page

BASE = "/home/pi/speedcam"
DB_PATH = os.path.join(BASE, "data", "traffic.db")
CSV_PATH = os.path.join(BASE, "data", "traffic_log.csv")
PREVIEW_PATH = os.path.join(BASE, "public", "calibration_preview.jpg")
SNAPSHOT_PATH = os.path.join(BASE, "public", "last_detection.jpg")
DEBUG_PATH = os.path.join(BASE, "data", "debug_tracks.log")
CLIP_DIR = os.path.join(BASE, "data", "clips")
CAMCHECK_PATH = os.path.join(BASE, "public", "camera_check.jpg")


def list_clips(page=1, per_page=25):
    """Clips from the last retention window, newest first, paginated.
    Returns (names_on_page, total, page, pages)."""
    if not os.path.isdir(CLIP_DIR):
        return [], 0, 1, 1
    cutoff = time.time() - retention_hours() * 3600
    items = []
    for f in os.listdir(CLIP_DIR):
        if not f.endswith(".mp4"):
            continue
        mt = os.path.getmtime(os.path.join(CLIP_DIR, f))
        if mt >= cutoff:
            items.append((mt, f))
    items.sort(reverse=True)
    total = len(items)
    pages = max(1, -(-total // per_page))
    page = min(max(1, page), pages)
    chunk = items[(page - 1) * per_page: page * per_page]
    return [f for _, f in chunk], total, page, pages


def retention_hours():
    try:
        with open(CONFIG_PATH) as f:
            return float((yaml.safe_load(f).get("video") or {}).get("retention_hours", 4))
    except Exception:
        return 4.0


TRACKER_UNIT = "speedcam-tracker"


def tracker_state():
    r = subprocess.run(["systemctl", "is-active", TRACKER_UNIT], capture_output=True, text=True, timeout=15)
    return r.stdout.strip() or "unknown"


LIVE_UNIT = "speedcam-liveview"


def tracker_control(action):
    """Only two fixed actions are allowed; the caller refuses anything else."""
    if action not in ("start", "stop"):
        raise ValueError("bad action")
    if action == "start":
        # The live view holds the camera; free it before the tracker needs it.
        subprocess.run(["sudo", "-n", "systemctl", "stop", LIVE_UNIT],
                       capture_output=True, text=True, timeout=60)
    r = subprocess.run(["sudo", "-n", "systemctl", action, TRACKER_UNIT],
                       capture_output=True, text=True, timeout=60)
    return r.returncode, r.stderr.strip()


def tail_lines(path, n):
    """Last n lines of a text file (reads only the end of the file)."""
    if not os.path.exists(path):
        return []
    with open(path, "rb") as f:
        f.seek(0, os.SEEK_END)
        size = f.tell()
        f.seek(max(0, size - 16384))
        data = f.read().decode("utf-8", errors="replace")
    return data.splitlines()[-n:]
CONFIG_PATH = os.path.join(BASE, "config.yaml")
PORT = 8085
BIND = "0.0.0.0"


def speed_limit():
    try:
        with open(CONFIG_PATH) as f:
            return float(yaml.safe_load(f)["calibration"]["speed_limit_kmh"])
    except Exception:
        return 30.0


def percentile85(values):
    """V85: the speed at or below which 85% of vehicles travel."""
    if not values:
        return None
    s = sorted(values)
    idx = max(0, min(len(s) - 1, int(-(-0.85 * len(s) // 1)) - 1))  # ceil(0.85*n)-1
    return s[idx]


def fmt(v, nd=1):
    return "–" if v is None else f"{v:.{nd}f}"


def query_stats():
    limit = speed_limit()
    if not os.path.exists(DB_PATH):
        return None, limit
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    try:
        rows = conn.execute(
            "SELECT timestamp, direction, speed_kmh FROM passes ORDER BY timestamp").fetchall()
    finally:
        conn.close()
    return rows, limit


def summarize(rows, limit):
    if not rows:
        return None
    speeds = [r[2] for r in rows]
    n = len(rows)
    by_dir = {"L2R": 0, "R2L": 0}
    for r in rows:
        by_dir[r[1]] = by_dir.get(r[1], 0) + 1
    over = sum(1 for s in speeds if s > limit)
    compliant = sum(1 for s in speeds if s <= limit)
    over40 = sum(1 for s in speeds if s > 40.0)
    over50 = sum(1 for s in speeds if s > 50.0)
    over60 = sum(1 for s in speeds if s > 60.0)
    hours = {}
    for ts, _d, s in rows:
        hours.setdefault((ts[:10], int(ts[11:13])), []).append(s)
    hourly = []
    for (day, h) in sorted(hours):
        sp = hours[(day, h)]
        hourly.append({
            "day": day, "hour": h, "count": len(sp),
            "avg": sum(sp) / len(sp), "v85": percentile85(sp),
            "speeders": sum(1 for s in sp if s > limit),
        })
    return {
        "n": n, "by_dir": by_dir,
        "v85": percentile85(speeds),
        "mean": sum(speeds) / n, "max": max(speeds),
        "pct_over": 100.0 * over / n, "pct_over40": 100.0 * over40 / n,
        "pct_compliant": 100.0 * compliant / n, "compliant": compliant,
        "pct_over50": 100.0 * over50 / n, "pct_over60": 100.0 * over60 / n,
        "over": over, "over40": over40, "over50": over50, "over60": over60,
        "hourly": hourly,
        "first": rows[0][0], "last": rows[-1][0],
    }


PAGE = """<!doctype html>
<html lang="cs"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="30">
<title>speedcam</title>
<style>
 body{font-family:system-ui,sans-serif;margin:0;background:#f4f6f8;color:#1d2530}
 header{background:#1d2530;color:#fff;padding:12px 18px}
 main{padding:14px 18px;max-width:1100px;margin:auto}
 .cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px;margin:12px 0}
 .card{background:#fff;border-radius:8px;padding:10px 12px;box-shadow:0 1px 2px #0002}
 .card b{display:block;font-size:1.5em}
 .card span{color:#5b6572;font-size:.85em}
 table{border-collapse:collapse;width:100%;background:#fff;font-size:.95em}
 th,td{padding:6px 8px;border-bottom:1px solid #e3e7ec;text-align:right}
 th:first-child,td:first-child{text-align:left}
 img{max-width:100%;border:1px solid #ccc;border-radius:6px;background:#000}
 .btn{display:inline-block;background:#2b6cb0;color:#fff;padding:9px 14px;border-radius:6px;text-decoration:none;margin:8px 0}
 .note{color:#5b6572;font-size:.85em}
 h2{margin-top:22px;font-size:1.15em}
</style></head><body>
<header><b>speedcam</b> &middot; živé statistiky provozu</header>
<main>
@@BODY@@
</main></body></html>"""


def render_index(page=1):
    rows, limit = query_stats()
    s = summarize(rows or [], limit)
    parts = []
    st = tracker_state()
    label = {"active": "běží", "inactive": "zastaveno"}.get(st, st)
    nxt = "stop" if st == "active" else "start"
    btn = "Zastavit sledování" if nxt == "stop" else "Spustit sledování"
    parts.append('<h2 id="sledovani">Sledování</h2>')
    parts.append('<div class="card" style="display:inline-block"><b>' + html.escape(label) + '</b><span>stav sledování</span></div>')
    parts.append('<form method="post" action="/tracker" style="margin:8px 0">'
                 '<input type="hidden" name="action" value="' + nxt + '">'
                 '<button class="btn" type="submit" style="border:0;cursor:pointer;font-size:1em">' + btn + '</button></form>')
    parts.append('<div class="note">Kamera nemůže být zároveň v živém náhledu (port 8086).</div>')
    parts.append('<p><a class="btn" href="/setup" style="display:inline-block;background:#2f855a;color:#fff;padding:9px 14px;border-radius:6px;text-decoration:none">Nastaveni</a></p>')
    parts.append("<h2>Kalibrační náhled</h2>")
    if os.path.exists(PREVIEW_PATH):
        ts = datetime.datetime.fromtimestamp(os.path.getmtime(PREVIEW_PATH)).strftime("%Y-%m-%d %H:%M:%S")
        parts.append(f'<img src="/calibration_preview.jpg?t={int(os.path.getmtime(PREVIEW_PATH))}" alt="kalibrační náhled">')
        parts.append(f'<div class="note">Vygenerováno {ts}. Zelená = ROI (sledovaná oblast), červená = spouštěcí čáry, žlutá čárkovaná = časové brány.</div>')
    else:
        parts.append('<div class="note">Kalibrační náhled zatím není k dispozici (sledování nebylo spuštěno).</div>')

    parts.append("<h2>Souhrn</h2>")
    if not s:
        parts.append('<div class="note">Zatím nebyla zaznamenána žádná projetí.</div>')
    else:
        parts.append('<div class="cards">')
        parts.append(f'<div class="card"><b>{s["n"]}</b><span>vozidel celkem</span></div>')
        parts.append(f'<div class="card"><b>{s["by_dir"].get("L2R",0)}</b><span>zleva doprava</span></div>')
        parts.append(f'<div class="card"><b>{s["by_dir"].get("R2L",0)}</b><span>zprava doleva</span></div>')
        parts.append(f'<div class="card"><b>{fmt(s["v85"])}</b><span>V85 km/h (všechna)</span></div>')
        parts.append(f'<div class="card"><b>{fmt(s["mean"])}</b><span>průměr km/h</span></div>')
        parts.append(f'<div class="card"><b>{fmt(s["max"])}</b><span>maximum km/h</span></div>')
        parts.append(f'<div class="card"><b>{fmt(s["pct_compliant"])}%</b><span>dodrželo limit (&le; {limit:g} km/h, {s["compliant"]})</span></div>')
        parts.append(f'<div class="card"><b>{fmt(s["pct_over"])}%</b><span>&gt; {limit:g} km/h ({s["over"]})</span></div>')
        parts.append(f'<div class="card"><b>{fmt(s["pct_over40"])}%</b><span>&gt; 40 km/h ({s["over40"]})</span></div>')
        parts.append(f'<div class="card"><b>{fmt(s["pct_over50"])}%</b><span>&gt; 50 km/h ({s["over50"]})</span></div>')
        parts.append(f'<div class="card"><b>{fmt(s["pct_over60"])}%</b><span>&gt; 60 km/h ({s["over60"]})</span></div>')
        parts.append("</div>")
        parts.append(f'<div class="note">Data od {html.escape(s["first"])} do {html.escape(s["last"])}. Stránka se obnovuje každých 30 s.</div>')

        parts.append("<h2>Přehled podle hodin</h2>")
        parts.append("<table><tr><th>Den a hodina</th><th>Vozidla</th><th>Průměr km/h</th>"
                     "<th>V85 km/h</th><th>Překročení (&gt;%g)</th></tr>" % limit)
        for h in s["hourly"]:
            parts.append(f"<tr><td>{h['day']} {h['hour']:02d}:00–{h['hour']:02d}:59</td><td>{h['count']}</td>"
                         f"<td>{fmt(h['avg'])}</td><td>{fmt(h['v85'])}</td><td>{h['speeders']}</td></tr>")
        parts.append("</table>")

    parts.append("<h2>Živá kontrola detekce</h2>")
    if os.path.exists(SNAPSHOT_PATH):
        mt = int(os.path.getmtime(SNAPSHOT_PATH))
        parts.append(f'<img src="/last_detection.jpg?t={mt}" alt="last detection">')
        parts.append('<div class="note">Vlevo: výřez ROI. Vpravo: maska popředí. Žluté rámečky = detekované objekty (plocha v px).</div>')
    else:
        parts.append('<div class="note">Zatím žádná detekce.</div>')
    parts.append("<h2>Posledních 15 rozhodnutí o sledování</h2>")
    lines = tail_lines(DEBUG_PATH, 15)
    if lines:
        parts.append('<pre style="background:#fff;padding:8px;overflow:auto;font-size:.8em">'
                     + html.escape("\n".join(lines)) + "</pre>")
    else:
        parts.append('<div class="note">Zatím žádné dokončené stopy.</div>')

    parts.append("<h2>Videa projetí</h2>")
    names, total, cur, pages = list_clips(page)
    hrs = retention_hours()
    parts.append(f'<div class="note">Zobrazena jsou videa z posledních {hrs:g} hodin (automaticky mazaná po {hrs:g} hodinách). Celkem: {total}.</div>')
    if names:
        parts.append("<table><tr><th>Soubor (čas, směr, rychlost)</th><th>Video</th></tr>")
        for name in names:
            safe = html.escape(name)
            parts.append(f'<tr><td>{safe}</td><td><a href="/clips/{safe}">přehrát / stáhnout</a></td></tr>')
        parts.append("</table>")
        nav = []
        if cur > 1:
            nav.append(f'<a class="btn" href="/?page={cur - 1}#videa">← Novější</a>')
        nav.append(f'<span class="note">Strana {cur} z {pages}</span>')
        if cur < pages:
            nav.append(f'<a class="btn" href="/?page={cur + 1}#videa">Starší →</a>')
        parts.append('<div id="videa">' + " &nbsp; ".join(nav) + "</div>")
    else:
        parts.append('<div class="note">Zatím žádná videa v posledních %g hodinách. Vytvoří se u každého zaznamenaného projetí.</div>' % hrs)

    parts.append("<h2>Surová data</h2>")
    if os.path.exists(CSV_PATH):
        parts.append('<a class="btn" href="/traffic_log.csv" download>Stáhnout traffic_log.csv</a>')
    else:
        parts.append('<div class="note">CSV se zobrazí po prvním zaznamenaném projetí.</div>')
    parts.append('<div class="note">Ochrana soukromí: ukládají se pouze souhrnné záznamy projetí (čas, směr, rychlost, délka, velikost objektu). Žádné snímky vozidel ani SPZ.</div>')
    return PAGE.replace("@@BODY@@", "\n".join(parts))


class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "speedcam"

    def _send(self, code, ctype, body, extra=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        if path in ("/", "/index.html"):
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            try:
                page = int(qs.get("page", ["1"])[0])
            except ValueError:
                page = 1
            self._send(200, "text/html; charset=utf-8", render_index(page).encode())
        elif path == "/calibration_preview.jpg":
            if os.path.exists(PREVIEW_PATH):
                with open(PREVIEW_PATH, "rb") as f:
                    self._send(200, "image/jpeg", f.read())
            else:
                self._send(404, "text/plain", b"no preview yet")
        elif path == "/camera_check.jpg":
            if os.path.exists(CAMCHECK_PATH):
                with open(CAMCHECK_PATH, "rb") as f:
                    self._send(200, "image/jpeg", f.read())
            else:
                self._send(404, "text/plain", b"no camera check image")
        elif path == "/last_detection.jpg":
            if os.path.exists(SNAPSHOT_PATH):
                with open(SNAPSHOT_PATH, "rb") as f:
                    self._send(200, "image/jpeg", f.read())
            else:
                self._send(404, "text/plain", b"no detection yet")
        elif path.startswith("/clips/"):
            name = os.path.basename(urllib.parse.unquote(path[len("/clips/"):]))
            fp = os.path.join(CLIP_DIR, name)
            if name.endswith(".mp4") and os.path.isfile(fp):
                with open(fp, "rb") as f:
                    self._send(200, "video/mp4", f.read())
            else:
                self._send(404, "text/plain", b"not found")
        elif path == "/traffic_log.csv":
            if os.path.exists(CSV_PATH):
                with open(CSV_PATH, "rb") as f:
                    self._send(200, "text/csv", f.read(),
                               {"Content-Disposition": 'attachment; filename="traffic_log.csv"'})
            else:
                self._send(404, "text/plain", b"no data yet")
        elif path == "/setup":
            try:
                cfg = yaml.safe_load(open(os.path.join(BASE, "config.yaml"), encoding="utf-8"))
                body = setup_page.render_setup(cfg, setup_page._active(setup_page.TRACKER_UNIT),
                                               setup_page._active(setup_page.LIVE_UNIT))
                self._send(200, "text/html; charset=utf-8", PAGE.replace("@@BODY@@", body).encode())
            except Exception as e:
                self._send(500, "text/plain; charset=utf-8", ("chyba: %s" % e).encode("utf-8"))
        elif path == "/healthz":
            self._send(200, "text/plain", b"ok")
        else:
            self._send(404, "text/plain", b"not found")

    do_HEAD = do_GET

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        if path == "/setup":
            length = min(int(self.headers.get("Content-Length", 0) or 0), 1024)
            form = urllib.parse.parse_qs(self.rfile.read(length).decode("utf-8", "replace"))
            ok, msg = setup_page.handle_post(form)
            cfg = yaml.safe_load(open(os.path.join(BASE, "config.yaml"), encoding="utf-8"))
            body = setup_page.render_setup(cfg, setup_page._active(setup_page.TRACKER_UNIT),
                                           setup_page._active(setup_page.LIVE_UNIT), msg=msg, ok=ok)
            self._send(200 if ok else 400, "text/html; charset=utf-8", PAGE.replace("@@BODY@@", body).encode())
            return
        if path != "/tracker":
            self._send(404, "text/plain", b"not found")
            return
        length = min(int(self.headers.get("Content-Length", 0) or 0), 1024)
        form = urllib.parse.parse_qs(self.rfile.read(length).decode("utf-8", "replace"))
        action = form.get("action", [""])[0]
        if action not in ("start", "stop"):
            self._send(400, "text/plain; charset=utf-8", "neplatna akce".encode("utf-8"))
            return
        try:
            code, err = tracker_control(action)
        except Exception as e:
            code, err = 1, str(e)
        if code != 0:
            self._send(500, "text/plain; charset=utf-8", ("chyba: " + err).encode("utf-8"))
            return
        self.send_response(303)
        self.send_header("Location", "/#sledovani")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, fmt_, *args):
        pass


class Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
    allow_reuse_address = True
    daemon_threads = True


if __name__ == "__main__":
    with Server((BIND, PORT), Handler) as srv:
        print(f"speedcam report server on http://{BIND}:{PORT}", flush=True)
        srv.serve_forever()
