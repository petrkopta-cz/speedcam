# speedcam

Raspberry Pi 4 traffic logger: counts vehicles with OpenCV, measures speed between two
calibrated lines, and serves a Czech web report (port 8085) plus a setup page and
manual live view (port 8086).

Files
- speed_tracker.py: camera loop, vehicle tracking, speed calculation (systemd: speedcam-tracker)
- report_server.py: web report and CSV download (systemd: speedcam-web)
- setup_page.py, live_view.py: calibration page and live view (systemd: speedcam-liveview)
- clip_recorder.py: short MP4 clip per pass, 4-hour rolling retention
- config.yaml: region, calibration distance, detection settings

Privacy: stores only aggregate pass records (time, direction, speed, length). No vehicle
images or plates are saved. Data, clips and databases are excluded by .gitignore.

Config is reloaded live by the tracker; only camera-device changes need a restart.
