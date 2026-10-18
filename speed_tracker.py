#!/usr/bin/env python3
"""speedcam tracker v2: blob tracks + per-track position regression.

For every track we keep (time, x_min, x_max, centroid_x) on each frame. When a track
ends (it leaves the detection area or disappears) it is evaluated:
  - at least min_track_frames frames
  - net horizontal displacement >= min_track_displacement_ratio * ROI width
  - direction from first/last centroid
  - speed = slope of position vs time (leading edge where usable, else centroid)
Every finished track is written to data/debug_tracks.log with an accept/reject reason.

Privacy: aggregate pass records only. The optional last_detection.jpg is a small crop
of the ROI (road only, rate-limited, can be disabled with debug.save_snapshots).
"""
import csv
import datetime
import logging
import math
import os
import signal
import sqlite3
import time

import cv2
import numpy as np
import yaml

from clip_recorder import ClipRecorder

BASE = "/home/pi/speedcam"
CONFIG_PATH = os.path.join(BASE, "config.yaml")
DATA_DIR = os.path.join(BASE, "data")
PUBLIC_DIR = os.path.join(BASE, "public")
DB_PATH = os.path.join(DATA_DIR, "traffic.db")
CSV_PATH = os.path.join(DATA_DIR, "traffic_log.csv")
DEBUG_LOG_PATH = os.path.join(DATA_DIR, "debug_tracks.log")
PREVIEW_PATH = os.path.join(PUBLIC_DIR, "calibration_preview.jpg")
SNAPSHOT_PATH = os.path.join(PUBLIC_DIR, "last_detection.jpg")

WARMUP_FRAMES = 60
HEARTBEAT_SEC = 300
DEBUG_LOG_MAX_BYTES = 2_000_000
FONT = cv2.FONT_HERSHEY_SIMPLEX

log = logging.getLogger("speedcam")
running = True


def _stop(signum, frame):
    global running
    running = False


# ----------------------------------------------------------------------------
# Config and geometry
# ----------------------------------------------------------------------------
def load_config():
    with open(CONFIG_PATH) as f:
        return yaml.safe_load(f)


def config_mtime():
    try:
        return os.path.getmtime(CONFIG_PATH)
    except OSError:
        return None


def geometry(cfg, width, height):
    r = cfg["roi_zone"]
    det = cfg["detection"]
    cal = cfg["calibration"]
    m = float(det.get("gate_margin_fraction", 0.10))

    x0 = int(round(r["x_start"] * width))
    x1 = int(round(r["x_end"] * width))
    y0 = int(round(r["y_top"] * height))
    y1 = int(round(r["y_bottom"] * height))
    span = max(1, x1 - x0)
    pad = int(round(float(det.get("detection_pad_fraction", 0.0)) * span))
    return {
        "x0": x0, "x1": x1, "y0": y0, "y1": y1,
        "dx0": max(0, x0 - pad), "dx1": min(width, x1 + pad),
        "gateA": x0 + m * span,   # preview only
        "gateB": x1 - m * span,   # preview only
        "roi_w": span,            # ROI width in pixels
        "zone_m": float(cal["zone_length_meters"]),
        "width": width, "height": height,
    }


# ----------------------------------------------------------------------------
# Camera
# ----------------------------------------------------------------------------
def open_camera(cam):
    cap = cv2.VideoCapture(int(cam.get("device", 0)), cv2.CAP_V4L2)
    cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, int(cam["width"]))
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, int(cam["height"]))
    cap.set(cv2.CAP_PROP_FPS, int(cam["fps"]))
    cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
    if not cap.isOpened():
        raise RuntimeError("cannot open camera device %s" % cam.get("device", 0))
    return cap


def read_frame(cap, cam):
    ok, frame = cap.read()
    if not ok or frame is None:
        return None
    if cam.get("flip_horizontal"):
        frame = cv2.flip(frame, 1)
    if cam.get("flip_vertical"):
        frame = cv2.flip(frame, 0)
    return frame


# ----------------------------------------------------------------------------
# Calibration preview (regenerated on start and config change)
# ----------------------------------------------------------------------------
def _put(img, text, org, scale=0.45, color=(255, 255, 255)):
    cv2.putText(img, text, org, FONT, scale, (0, 0, 0), 3, cv2.LINE_AA)
    cv2.putText(img, text, org, FONT, scale, color, 1, cv2.LINE_AA)


def _dashed_vertical(img, x, y0, y1, color, dash=12, gap=8):
    y = y0
    while y < y1:
        cv2.line(img, (x, y), (x, min(y + dash, y1)), color, 1)
        y += dash + gap


def draw_preview(frame, cfg, g):
    img = frame.copy()
    h, w = img.shape[:2]
    cv2.rectangle(img, (g["x0"], g["y0"]), (g["x1"], g["y1"]), (0, 255, 0), 2)
    cv2.line(img, (g["x0"], 0), (g["x0"], h - 1), (0, 0, 255), 2)
    cv2.line(img, (g["x1"], 0), (g["x1"], h - 1), (0, 0, 255), 2)
    _dashed_vertical(img, int(round(g["gateA"])), 0, h - 1, (0, 255, 255))
    _dashed_vertical(img, int(round(g["gateB"])), 0, h - 1, (0, 255, 255))
    for i in range(21):
        f = i * 0.05
        major = (i % 2 == 0)
        tick = 14 if major else 7
        x = min(w - 1, int(round(f * w)))
        y = min(h - 1, int(round(f * h)))
        cv2.line(img, (x, 0), (x, tick), (255, 255, 255), 1)
        cv2.line(img, (x, h - 1), (x, h - 1 - tick), (255, 255, 255), 1)
        cv2.line(img, (0, y), (tick, y), (255, 255, 255), 1)
        cv2.line(img, (w - 1, y), (w - 1 - tick, y), (255, 255, 255), 1)
        if major:
            _put(img, "%.1f" % f, (min(x + 2, w - 40), 30))
            _put(img, "%.1f" % f, (4, min(max(y + 14, 14), h - 6)))
    r = cfg["roi_zone"]
    _put(img, "x_start %.2f" % r["x_start"], (g["x0"] + 4, g["y0"] - 8), 0.5, (0, 0, 255))
    _put(img, "x_end %.2f" % r["x_end"], (g["x1"] - 110, g["y0"] - 8), 0.5, (0, 0, 255))
    _put(img, "y_top %.2f" % r["y_top"], (g["x0"] + 4, g["y0"] + 18), 0.5, (0, 255, 0))
    _put(img, "y_bottom %.2f" % r["y_bottom"], (g["x0"] + 4, g["y1"] - 6), 0.5, (0, 255, 0))
    _put(img, "green=ROI  red=trigger lines  yellow dashed=timing gates (10%% inset)  ruler=5%%",
         (10, h - 12), 0.42)
    _put(img, "%dx%d  %s" % (w, h, datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
         (w - 230, h - 12), 0.42)
    return img


def write_preview(frame, cfg, g):
    os.makedirs(PUBLIC_DIR, exist_ok=True)
    tmp = PREVIEW_PATH + ".tmp.jpg"
    cv2.imwrite(tmp, draw_preview(frame, cfg, g), [cv2.IMWRITE_JPEG_QUALITY, 85])
    os.replace(tmp, PREVIEW_PATH)
    log.info("calibration preview written: %s", PREVIEW_PATH)


# ----------------------------------------------------------------------------
# Storage (aggregate pass records only)
# ----------------------------------------------------------------------------
def init_storage():
    os.makedirs(DATA_DIR, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(
        """CREATE TABLE IF NOT EXISTS passes (
               id INTEGER PRIMARY KEY AUTOINCREMENT,
               timestamp TEXT NOT NULL,
               direction TEXT NOT NULL,
               speed_kmh REAL NOT NULL,
               duration_sec REAL NOT NULL,
               bbox_area INTEGER)"""
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_passes_ts ON passes(timestamp)")
    conn.commit()
    return conn


def log_pass(conn, direction, speed, duration, area):
    ts = datetime.datetime.now().isoformat(timespec="seconds")
    conn.execute(
        "INSERT INTO passes (timestamp, direction, speed_kmh, duration_sec, bbox_area) "
        "VALUES (?, ?, ?, ?, ?)",
        (ts, direction, round(speed, 2), round(duration, 4), int(area)),
    )
    conn.commit()
    new = not os.path.exists(CSV_PATH) or os.path.getsize(CSV_PATH) == 0
    with open(CSV_PATH, "a", newline="") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["timestamp", "direction", "speed_kmh", "duration_sec", "bbox_area"])
        w.writerow([ts, direction, round(speed, 2), round(duration, 4), int(area)])


def debug_write(line):
    """Append one line to debug_tracks.log, rotating to .1 at ~2 MB."""
    try:
        if os.path.exists(DEBUG_LOG_PATH) and os.path.getsize(DEBUG_LOG_PATH) > DEBUG_LOG_MAX_BYTES:
            os.replace(DEBUG_LOG_PATH, DEBUG_LOG_PATH + ".1")
        with open(DEBUG_LOG_PATH, "a") as f:
            f.write(line + "\n")
    except OSError as e:
        log.error("debug log write failed: %s", e)


# ----------------------------------------------------------------------------
# Track evaluation (pure function, unit-tested)
# hist entries: (t, x_min, x_max, centroid_x, area)
# ----------------------------------------------------------------------------
def evaluate(hist, p):
    n = len(hist)
    out = {"frames": n}
    if n < p["min_frames"]:
        out["status"] = "too_few_frames"
        return out
    first, last = hist[0], hist[-1]
    disp = abs(last[3] - first[3])
    need = p["min_disp_ratio"] * p["roi_w"]
    out["disp_px"] = round(disp, 1)
    if disp < need:
        out["status"] = "too_short_displacement"
        out["need_px"] = round(need, 1)
        return out

    direction = "L2R" if last[3] > first[3] else "R2L"
    x0, rw = p["x0"], p["roi_w"]
    # leading edge where it is inside the first 75% of the ROI, else centroid
    lead_t, lead_x = [], []
    for t, xmin, xmax, cx, _a in hist:
        if direction == "L2R" and (xmax - x0) < 0.88 * rw:
            lead_t.append(t); lead_x.append(xmax)
        elif direction == "R2L" and (xmin - x0) > 0.12 * rw:
            lead_t.append(t); lead_x.append(xmin)
    if len(lead_t) >= 3:
        ts, xs, mode = np.array(lead_t), np.array(lead_x, dtype=float), "leading_edge"
    else:
        ts = np.array([h[0] for h in hist]); xs = np.array([h[3] for h in hist], dtype=float)
        mode = "centroid"
    dur = float(ts[-1] - ts[0])
    out.update({"direction": direction, "mode": mode, "duration_sec": round(dur, 4)})
    if dur <= 0:
        out["status"] = "bad_timing"
        return out
    slope = float(np.polyfit(ts - ts[0], xs, 1)[0])          # px per second
    speed = abs(slope) / rw * p["zone_m"] * 3.6 * p["mult"]  # km/h
    areas = sorted(h[4] for h in hist)
    out.update({"speed_kmh": round(speed, 2), "area": int(areas[len(areas) // 2])})
    if speed < p["min_v"] or speed > p["max_v"]:
        out["status"] = "speed_out_of_bounds"
        return out
    out["status"] = "accepted"
    return out


# ----------------------------------------------------------------------------
# Centroid tracker
# det tuple: (cx, cy, x_min, x_max, area, y_min, y_max) in full-frame pixels
# ----------------------------------------------------------------------------
class Track:
    __slots__ = ("id", "cx", "cy", "area", "disappeared", "hist")

    def __init__(self, tid, t, d):
        self.id = tid
        self.cx, self.cy, self.area = d[0], d[1], d[4]
        self.disappeared = 0
        self.hist = [(t, d[2], d[3], d[0], d[4])]


class CentroidTracker:
    def __init__(self, cfg, g):
        det = cfg["detection"]
        cal = cfg["calibration"]
        self.max_disappeared = int(det["max_disappeared_frames"])
        self.max_dist = float(det.get("max_centroid_distance_px", 70))
        self.dx0, self.dx1 = g["dx0"], g["dx1"]
        self.params = {
            "min_frames": int(det.get("min_track_frames", 4)),
            "min_disp_ratio": float(det.get("min_track_displacement_ratio", 0.45)),
            "roi_w": g["roi_w"], "x0": g["x0"], "zone_m": g["zone_m"],
            "mult": float(cal["calibration_multiplier"]),
            "min_v": float(cal["min_valid_speed_kmh"]),
            "max_v": float(cal["max_valid_speed_kmh"]),
        }
        self.tracks = {}
        self.next_id = 1
        self.finished = []

    def _finish(self, tr):
        res = evaluate(tr.hist, self.params)
        res["track_id"] = tr.id
        self.finished.append(res)

    def drain(self):
        out, self.finished = self.finished, []
        return out

    def _at_exit(self, tr):
        if len(tr.hist) < 2:
            return False
        first_cx = tr.hist[0][3]
        if tr.cx > first_cx:
            return tr.cx >= self.dx1 - 3
        if tr.cx < first_cx:
            return tr.cx <= self.dx0 + 3
        return False

    def update(self, dets, t):
        ids = list(self.tracks.keys())
        pairs = []
        for tid in ids:
            tr = self.tracks[tid]
            for di, d in enumerate(dets):
                dist = math.hypot(tr.cx - d[0], tr.cy - d[1])
                if dist <= self.max_dist:
                    pairs.append((dist, tid, di))
        pairs.sort()
        matched_t, matched_d = set(), set()
        for dist, tid, di in pairs:
            if tid in matched_t or di in matched_d:
                continue
            matched_t.add(tid); matched_d.add(di)
            tr = self.tracks[tid]
            d = dets[di]
            tr.cx, tr.cy, tr.area, tr.disappeared = d[0], d[1], d[4], 0
            tr.hist.append((t, d[2], d[3], d[0], d[4]))
            if len(tr.hist) > 400:
                tr.hist = tr.hist[-400:]

        for tid in ids:
            if tid in matched_t:
                continue
            tr = self.tracks[tid]
            tr.disappeared += 1
            if tr.disappeared > self.max_disappeared:
                self._finish(tr)
                del self.tracks[tid]

        for tid in list(self.tracks.keys()):
            tr = self.tracks[tid]
            if tr.disappeared == 0 and self._at_exit(tr):
                self._finish(tr)
                del self.tracks[tid]

        for di, d in enumerate(dets):
            if di not in matched_d:
                self.tracks[self.next_id] = Track(self.next_id, t, d)
                self.next_id += 1


# ----------------------------------------------------------------------------
# Snapshot (ROI crop + mask, rate-limited, optional)
# ----------------------------------------------------------------------------
def save_snapshot(frame, mask, g, dets):
    dx0, y0 = g["dx0"], g["y0"]
    crop = frame[y0:g["y1"], dx0:g["dx1"]].copy()
    m = cv2.cvtColor(mask, cv2.COLOR_GRAY2BGR)
    for panel in (crop, m):
        cv2.rectangle(panel, (g["x0"] - dx0, 0), (g["x1"] - dx0 - 1, panel.shape[0] - 1), (0, 255, 0), 1)
        for d in dets:
            cv2.rectangle(panel, (int(d[2] - dx0), int(d[5] - y0)), (int(d[3] - dx0), int(d[6] - y0)), (0, 255, 255), 1)
            _put(panel, "%d" % d[4], (int(d[2] - dx0), max(10, int(d[5] - y0) - 2)), 0.3, (0, 255, 255))
    both = cv2.resize(np.hstack([crop, m]), None, fx=2, fy=2, interpolation=cv2.INTER_NEAREST)
    _put(both, "ROI crop (left) | foreground mask (right) %s" % datetime.datetime.now().strftime("%H:%M:%S"),
         (6, both.shape[0] - 8), 0.45)
    tmp = SNAPSHOT_PATH + ".tmp.jpg"
    cv2.imwrite(tmp, both, [cv2.IMWRITE_JPEG_QUALITY, 80])
    os.replace(tmp, SNAPSHOT_PATH)


def fmt_debug(res, ts):
    parts = ["%s" % ts, "track=%s" % res.get("track_id"), "status=%s" % res["status"],
             "frames=%s" % res.get("frames")]
    for k in ("disp_px", "need_px", "direction", "mode", "speed_kmh", "duration_sec", "area"):
        if k in res:
            parts.append("%s=%s" % (k, res[k]))
    return " ".join(parts)


# ----------------------------------------------------------------------------
# Main loop
# ----------------------------------------------------------------------------
def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    os.makedirs(PUBLIC_DIR, exist_ok=True)
    os.makedirs(DATA_DIR, exist_ok=True)

    conn = init_storage()
    cfg = load_config()
    mtime = config_mtime()
    cam = cfg["camera"]
    cap = open_camera(cam)
    cam_sig = (cam.get("device"), cam.get("width"), cam.get("height"), cam.get("fps"))
    log.info("camera opened: device=%s requested=%sx%s@%s",
             cam.get("device"), cam.get("width"), cam.get("height"), cam.get("fps"))

    def build_state(cfg, frame):
        h, w = frame.shape[:2]
        g = geometry(cfg, w, h)
        bg = cv2.createBackgroundSubtractorMOG2(history=500, varThreshold=40, detectShadows=False)
        return g, bg, CentroidTracker(cfg, g)

    frame = None
    for _ in range(30):
        frame = read_frame(cap, cam)
        if frame is not None:
            break
    if frame is None:
        raise RuntimeError("no frames from camera")
    g, bg, tracker = build_state(cfg, frame)
    write_preview(frame, cfg, g)
    recorder = ClipRecorder(cfg.get("video"))

    frames_seen = 0
    fail_count = 0
    last_check = 0.0
    last_hb = time.monotonic()
    last_prune = 0.0
    last_snap = 0.0
    pass_count = 0
    rejected = 0
    k_open = cv2.getStructuringElement(cv2.MORPH_RECT, (3, 3))
    k_close = cv2.getStructuringElement(cv2.MORPH_RECT, (15, 9))
    log.info("tracking started (v2 regression): zone x %.3f-%.3f, ROI %d px, zone %.2f m, min_area %s",
             cfg["roi_zone"]["x_start"], cfg["roi_zone"]["x_end"], g["roi_w"], g["zone_m"],
             cfg["detection"]["min_contour_area"])

    while running:
        now_mono = time.monotonic()
        if now_mono - last_check >= 1.0:
            last_check = now_mono
            m = config_mtime()
            if m != mtime:
                mtime = m
                try:
                    new_cfg = load_config()
                    new_cam = new_cfg["camera"]
                    cfg = new_cfg
                    new_sig = (new_cam.get("device"), new_cam.get("width"),
                               new_cam.get("height"), new_cam.get("fps"))
                    if new_sig != cam_sig:
                        cap.release()
                        cam = new_cam
                        cap = open_camera(cam)
                        cam_sig = new_sig
                    cur = read_frame(cap, cam)
                    if cur is not None:
                        g, bg, tracker = build_state(cfg, cur)
                        write_preview(cur, cfg, g)
                        frames_seen = 0
                        log.info("config reloaded")
                except Exception as e:
                    log.error("config reload failed, keeping previous settings: %s", e)

        frame = read_frame(cap, cam)
        t_frame = time.monotonic()
        if frame is None:
            fail_count += 1
            if fail_count % 50 == 1:
                log.warning("camera read failed (%d), retrying", fail_count)
            if fail_count >= 50:
                try:
                    cap.release()
                    cap = open_camera(cam)
                    log.info("camera reopened")
                except Exception as e:
                    log.error("camera reopen failed: %s", e)
                fail_count = 0
            time.sleep(0.2)
            continue
        fail_count = 0

        recorder.push(frame, t_frame)
        if frame.shape[:2] != (g["height"], g["width"]):
            g, bg, tracker = build_state(cfg, frame)

        crop = frame[g["y0"]:g["y1"], g["dx0"]:g["dx1"]]
        fg = bg.apply(cv2.GaussianBlur(crop, (5, 5), 0), learningRate=-1)
        frames_seen += 1
        if frames_seen <= WARMUP_FRAMES:
            continue

        fg = cv2.morphologyEx(fg, cv2.MORPH_OPEN, k_open)
        fg = cv2.morphologyEx(fg, cv2.MORPH_CLOSE, k_close)

        min_area = float(cfg["detection"]["min_contour_area"])
        dets = []
        contours, _ = cv2.findContours(fg, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        for c in contours:
            area = cv2.contourArea(c)
            if area < min_area:
                continue
            x, y, w, h = cv2.boundingRect(c)
            fx0, fx1 = g["dx0"] + x, g["dx0"] + x + w
            fy0, fy1 = g["y0"] + y, g["y0"] + y + h
            dets.append(((fx0 + fx1) / 2.0, (fy0 + fy1) / 2.0, fx0, fx1, area, fy0, fy1))

        if dets:
            debug_cfg = cfg.get("debug", {}) or {}
            if debug_cfg.get("save_snapshots", True) and \
               t_frame - last_snap >= float(debug_cfg.get("snapshot_min_interval_sec", 1.0)):
                last_snap = t_frame
                try:
                    save_snapshot(frame, fg, g, dets)
                except Exception as e:
                    log.error("snapshot failed: %s", e)

        tracker.update(dets, t_frame)
        ts_now = datetime.datetime.now().isoformat(timespec="seconds")
        for res in tracker.drain():
            debug_write(fmt_debug(res, ts_now))
            if res["status"] == "accepted":
                clip = recorder.save_clip(res["direction"], res["speed_kmh"])
                try:
                    log_pass(conn, res["direction"], res["speed_kmh"], res["duration_sec"], res["area"])
                    pass_count += 1
                    log.info("pass %s %.1f km/h (%.3fs, %s)", res["direction"], res["speed_kmh"],
                             res["duration_sec"], res["mode"])
                except Exception as e:
                    log.error("failed to log pass: %s", e)
            else:
                rejected += 1

        if t_frame - last_prune >= 60:
            last_prune = t_frame
            try:
                ret_h = float((cfg.get("video") or {}).get("retention_hours", 4))
                recorder.prune(ret_h)
            except Exception as e:
                log.error("clip prune error: %s", e)

        if t_frame - last_hb >= HEARTBEAT_SEC:
            last_hb = t_frame
            log.info("heartbeat: passes logged=%d rejected=%d active tracks=%d",
                     pass_count, rejected, len(tracker.tracks))

    log.info("shutting down")
    conn.close()
    cap.release()


if __name__ == "__main__":
    main()
