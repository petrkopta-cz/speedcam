#!/usr/bin/env python3
"""speedcam live view: MJPEG stream of the camera with the ROI overlay, for positioning.

Port 8086. Open http://192.168.88.16:8086/ in a browser on the same network.
Only one process may hold the camera, so the tracker must be stopped while this runs.
It stops by itself after MAX_SECONDS (default 30 min).
"""
import http.server
import os
import socketserver
import sys
import threading
import time

import cv2

sys.path.insert(0, "/home/pi/speedcam")
import speed_tracker as st  # noqa: E402

PORT = 8086
MAX_SECONDS = int(os.environ.get("LIVE_MAX_SECONDS", "1800"))
START = time.time()
latest = {"jpg": None, "t": 0.0}
lock = threading.Lock()


def capture_loop():
    cfg = st.load_config()
    cam = cfg["camera"]
    cap = st.open_camera(cam)
    mtime = st.config_mtime()
    while time.time() - START < MAX_SECONDS:
        frame = st.read_frame(cap, cam)
        if frame is None:
            time.sleep(0.1)
            continue
        m = st.config_mtime()
        if m != mtime:  # margins changed on the setup page: reload
            try:
                cfg = st.load_config()
                mtime = m
            except Exception as e:
                print("config reload failed:", e, flush=True)
        try:
            g = st.geometry(cfg, frame.shape[1], frame.shape[0])
            img = st.draw_preview(frame, cfg, g)
            img = cv2.resize(img, (960, 540), interpolation=cv2.INTER_AREA)
            ok, jpg = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 70])
            if ok:
                with lock:
                    latest["jpg"] = jpg.tobytes()
                    latest["t"] = time.time()
        except Exception as e:
            print("frame error:", e, flush=True)
        time.sleep(0.15)  # about 6 fps is enough for positioning
    cap.release()
    print("live view stopped (time limit reached)", flush=True)
    os._exit(0)


PAGE = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>speedcam – živý náhled</title>
<style>body{font-family:system-ui,sans-serif;background:#111;color:#eee;margin:0;padding:10px}
img{max-width:100%;border:1px solid #444;border-radius:6px}</style></head>
<body><div>Živý náhled kamery (zelená = ROI, červené = spouštěcí čáry). Obnoví se samo.</div>
<img src="/stream.mjpg" alt="živý náhled"></body></html>"""


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.path.startswith("/stream.mjpg"):
            self.send_response(200)
            self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            try:
                while time.time() - START < MAX_SECONDS:
                    with lock:
                        jpg = latest["jpg"]
                    if jpg:
                        self.wfile.write(b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: "
                                         + str(len(jpg)).encode() + b"\r\n\r\n" + jpg + b"\r\n")
                    time.sleep(0.2)
            except (BrokenPipeError, ConnectionResetError):
                pass
        elif self.path in ("/", "/index.html"):
            body = PAGE.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path.startswith("/snapshot.jpg"):
            with lock:
                jpg = latest["jpg"]
            if not jpg:
                self.send_response(503)
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("Content-Type", "image/jpeg")
            self.send_header("Content-Length", str(len(jpg)))
            self.end_headers()
            self.wfile.write(jpg)
        else:
            self.send_response(404)
            self.end_headers()


class Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


if __name__ == "__main__":
    threading.Thread(target=capture_loop, daemon=True).start()
    print("live view on port %d for %d s" % (PORT, MAX_SECONDS), flush=True)
    with Server(("0.0.0.0", PORT), Handler) as srv:
        srv.serve_forever()
