#!/usr/bin/env python3
"""speedcam pass clips.

Keeps a short rolling buffer of low-resolution JPEG frames and, for every accepted
pass, writes one small H.264 MP4 clip (hardware encoder when available).

Recording stops by itself after `max_hours`, counted from the first start.
The start time is stored in data/video_started.txt so restarts do not extend it.
Delete that file to start a new recording period.
"""
import collections
import datetime
import logging
import os
import subprocess
import threading
import time

import cv2

BASE = "/home/pi/speedcam"
CLIP_DIR = os.path.join(BASE, "data", "clips")
START_FILE = os.path.join(BASE, "data", "video_started.txt")
log = logging.getLogger("speedcam.clips")


class ClipRecorder:
    def __init__(self, vcfg):
        vcfg = vcfg or {}
        self.enabled = bool(vcfg.get("enabled", False))
        self.fps = int(vcfg.get("fps", 10))
        self.w = int(vcfg.get("width", 640))
        self.h = int(vcfg.get("height", 360))
        self.window = float(vcfg.get("window_seconds", 12))
        self.bitrate = str(vcfg.get("bitrate", "1M"))
        self.deadline = None
        self.buf = collections.deque()
        self.last_push = 0.0
        self.lock = threading.Lock()
        if self.enabled:
            os.makedirs(CLIP_DIR, exist_ok=True)
            start = None
            if os.path.exists(START_FILE):
                try:
                    with open(START_FILE) as f:
                        start = float(f.read().strip())
                except (OSError, ValueError):
                    start = None
            if start is None:
                start = time.time()
                with open(START_FILE, "w") as f:
                    f.write(str(start))
            max_hours = vcfg.get("max_hours")
            if max_hours in (None, 0, "none", "unlimited"):
                self.deadline = float("inf")   # no recording limit: runs until stopped
                log.info("pass clips enabled with no time limit (rolling retention applies)")
            else:
                self.deadline = start + float(max_hours) * 3600
                log.info("pass clips enabled until %s",
                         datetime.datetime.fromtimestamp(self.deadline).isoformat(timespec="minutes"))

    @staticmethod
    def prune(retention_hours):
        """Delete clip files older than retention_hours (by file modification time).
        Runs regardless of whether recording is still active."""
        if not os.path.isdir(CLIP_DIR):
            return 0
        cutoff = time.time() - float(retention_hours) * 3600
        removed = 0
        for name in os.listdir(CLIP_DIR):
            if not name.endswith(".mp4"):
                continue
            p = os.path.join(CLIP_DIR, name)
            try:
                if os.path.getmtime(p) < cutoff:
                    os.remove(p)
                    removed += 1
            except OSError as e:
                log.error("clip prune failed for %s: %s", name, e)
        if removed:
            log.info("pruned %d clip(s) older than %s h", removed, retention_hours)
        return removed

    def active(self):
        return self.enabled and self.deadline is not None and time.time() < self.deadline

    def push(self, frame, t_mono):
        """Called for every camera frame. Keeps about `window` seconds at `fps`."""
        if not self.active() or t_mono - self.last_push < 1.0 / self.fps:
            return
        self.last_push = t_mono
        try:
            small = cv2.resize(frame, (self.w, self.h), interpolation=cv2.INTER_AREA)
            ok, jpg = cv2.imencode(".jpg", small, [cv2.IMWRITE_JPEG_QUALITY, 75])
        except Exception as e:
            log.error("clip frame encode failed: %s", e)
            return
        if not ok:
            return
        with self.lock:
            self.buf.append((t_mono, jpg.tobytes()))
            cutoff = t_mono - self.window
            while self.buf and self.buf[0][0] < cutoff:
                self.buf.popleft()

    def save_clip(self, direction, speed_kmh):
        """Start encoding the buffered frames into one clip. Returns the file name."""
        if not self.active():
            return None
        with self.lock:
            frames = [j for _, j in self.buf]
        if len(frames) < 5:
            return None
        stamp = datetime.datetime.now().strftime("%Y-%m-%dT%H-%M-%S")
        name = "%s_%s_%dkmh.mp4" % (stamp, direction, round(speed_kmh))
        path = os.path.join(CLIP_DIR, name)
        n = 2
        while os.path.exists(path):
            name = "%s_%s_%dkmh_%d.mp4" % (stamp, direction, round(speed_kmh), n)
            path = os.path.join(CLIP_DIR, name)
            n += 1
        threading.Thread(target=self._encode, args=(frames, path), daemon=True).start()
        return name

    def _encode(self, frames, path):
        data = b"".join(frames)
        base = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                "-f", "mjpeg", "-framerate", str(self.fps), "-i", "-"]
        tail = ["-pix_fmt", "yuv420p", "-movflags", "+faststart", path]
        attempts = [
            ["-c:v", "h264_v4l2m2m", "-b:v", self.bitrate] + tail,   # Pi hardware encoder
            ["-c:v", "libx264", "-preset", "ultrafast", "-crf", "30"] + tail,  # software fallback
        ]
        for extra in attempts:
            try:
                p = subprocess.run(base + extra, input=data, capture_output=True, timeout=120)
            except (subprocess.TimeoutExpired, OSError) as e:
                log.error("clip encode error: %s", e)
                continue
            if p.returncode == 0 and os.path.exists(path):
                log.info("clip saved: %s", os.path.basename(path))
                return
            log.warning("encoder attempt failed (rc=%s): %s", p.returncode,
                        p.stderr.decode(errors="replace")[-200:])
        log.error("clip not saved: %s", os.path.basename(path))
        if os.path.exists(path):
            os.remove(path)
