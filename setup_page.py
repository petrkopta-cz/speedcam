"""speedcam setup page: stop tracking, show live stream with ROI margins,
adjust margins and the real-world distance between the red lines with +/- buttons.

Each change edits ONE value line in config.yaml (keeps comments), validates the
result, writes a rolling backup first and restores it on any failure.
The tracker reloads config.yaml by itself; the live view (port 8086) also reloads it.
"""
import html
import os
import re
import shutil
import subprocess

import yaml

BASE = "/home/pi/speedcam"
CONFIG_PATH = os.path.join(BASE, "config.yaml")
BACKUP_PATH = os.path.join(BASE, "config.yaml.bak-setup")
TRACKER_UNIT = "speedcam-tracker"
LIVE_UNIT = "speedcam-liveview"

# key -> (section, step, min, max)
MARGINS = {
    "x_start": ("roi_zone", 0.01, 0.0, 1.0, "Levá červená čára (x_start)"),
    "x_end": ("roi_zone", 0.01, 0.0, 1.0, "Pravá červená čára (x_end)"),
    "y_top": ("roi_zone", 0.01, 0.0, 1.0, "Horní okraj ROI (y_top)"),
    "y_bottom": ("roi_zone", 0.01, 0.0, 1.0, "Dolní okraj ROI (y_bottom)"),
}
LENGTH_KEY = ("calibration", "zone_length_meters", 1.0, 30.0)
LENGTH_STEP = 0.1
FMT_LEN = "%.2f"


# ---------------------------------------------------------------- config edit
def _read():
    with open(CONFIG_PATH, encoding="utf-8") as f:
        return f.read()


def _write_atomic(text):
    tmp = CONFIG_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, CONFIG_PATH)


def _replace_value(text, section, key, value):
    """Replace only the value of `key` inside `section:` keeping the trailing comment."""
    lines = text.split("\n")
    in_sec = False
    pat = re.compile(r"^(\s+%s:\s*)([^#\n]*?)(\s*(#.*)?)$" % re.escape(key))
    for i, line in enumerate(lines):
        if line and not line[0].isspace() and not line.startswith("#"):
            in_sec = line.rstrip() == section + ":"
            continue
        if in_sec:
            m = pat.match(line)
            if m:
                lines[i] = m.group(1) + "%.2f" % value + m.group(3)
                return "\n".join(lines)
    raise KeyError("%s.%s not found" % (section, key))


def _check(cfg):
    r = cfg["roi_zone"]
    if not (0.0 <= r["x_start"] < r["x_end"] <= 1.0):
        return "levá čára musí být vlevo od pravé"
    if r["x_end"] - r["x_start"] < 0.05:
        return "červené čáry jsou příliš blízko"
    if not (0.0 <= r["y_top"] < r["y_bottom"] <= 1.0):
        return "horní okraj musí být nad dolním"
    if r["y_bottom"] - r["y_top"] < 0.03:
        return "ROI je příliš úzké"
    return None


def apply_change(key, value=None, delta=None):
    """Change one margin (by delta) or the zone length (value). Returns (ok, message)."""
    text = _read()
    cfg = yaml.safe_load(text)
    if key == "zone_length_meters":
        section, lo, hi = LENGTH_KEY[0], LENGTH_KEY[2], LENGTH_KEY[3]
        cur = float(cfg["calibration"][key])
        new = value if value is not None else cur + delta * LENGTH_STEP
    else:
        section, step, lo, hi, _label = MARGINS[key]
        cur = float(cfg[section][key])
        new = value if value is not None else cur + delta * step
    new = round(min(hi, max(lo, new)), 2)
    if abs(new - cur) < 1e-9:
        return True, "beze změny"
    new_text = _replace_value(text, section, key, new)
    new_cfg = yaml.safe_load(new_text)
    err = _check(new_cfg)
    if err:
        return False, err
    shutil.copy2(CONFIG_PATH, BACKUP_PATH)
    try:
        _write_atomic(new_text)
        yaml.safe_load(_read())
    except Exception as e:  # restore on any failure
        shutil.copy2(BACKUP_PATH, CONFIG_PATH)
        return False, "uloženo zpět, chyba: %s" % e
    return True, "uloženo: %s = %.2f" % (key, new)


# ------------------------------------------------------------- service control
def _sudo(action, unit):
    r = subprocess.run(["sudo", "-n", "systemctl", action, unit],
                       capture_output=True, text=True, timeout=60)
    return r.returncode, r.stderr.strip()


def _active(unit):
    r = subprocess.run(["systemctl", "is-active", unit], capture_output=True, text=True, timeout=15)
    return r.stdout.strip() or "unknown"


def start_setup():
    """Stop tracking (frees the camera), then start the live view."""
    code, err = _sudo("stop", TRACKER_UNIT)
    if code != 0:
        return False, "nelze zastavit sledování: " + err
    code, err = _sudo("start", LIVE_UNIT)
    if code != 0:
        return False, "nelze spustit živý náhled: " + err
    return True, "sledování zastaveno, živý náhled běží"


def end_setup():
    """Stop the live view and resume tracking."""
    _sudo("stop", LIVE_UNIT)
    code, err = _sudo("start", TRACKER_UNIT)
    if code != 0:
        return False, "nelze spustit sledování: " + err
    return True, "náhled ukončen, sledování spuštěno"


# ------------------------------------------------------------------- rendering
CSS = """<style>
.row{display:flex;align-items:center;gap:10px;margin:6px 0;background:#fff;padding:8px 10px;border-radius:6px;box-shadow:0 1px 2px #0002}
.row .lbl{flex:1}
.row .val{font-weight:bold;min-width:70px;text-align:right;font-variant-numeric:tabular-nums}
button.pm{font-size:1.2em;width:44px;height:38px;border:0;border-radius:6px;background:#2b6cb0;color:#fff;cursor:pointer}
.msg{padding:8px 10px;border-radius:6px;background:#e6f4ea;margin:8px 0}
.msg.err{background:#fde8e8}
.stream{width:100%;border:1px solid #ccc;border-radius:6px;background:#000}
.tbtn{display:inline-block;padding:9px 14px;border-radius:6px;border:0;color:#fff;cursor:pointer;font-size:1em;margin:6px 6px 6px 0}
</style>"""


def _btn(action, key, delta):
    return ('<form method="post" action="/setup" style="margin:0">'
            '<input type="hidden" name="action" value="%s"><input type="hidden" name="key" value="%s">'
            '<input type="hidden" name="delta" value="%d">'
            '<button class="pm" type="submit">%s</button></form>') % (
        action, key, delta, "−" if delta < 0 else "+")


def render_setup(cfg, tracker_state, live_state, msg=None, ok=True):
    r = cfg["roi_zone"]
    length = float(cfg["calibration"]["zone_length_meters"])
    parts = ['<h2>Nastavení po přesunu kamery</h2>']
    parts.append('<div class="note">Sledování (tracker): <b>%s</b> · živý náhled: <b>%s</b></div>'
                 % (html.escape(tracker_state), html.escape(live_state)))
    if msg:
        parts.append('<div class="msg%s">%s</div>' % ("" if ok else " err", html.escape(msg)))

    if live_state != "active":
        parts.append('<form method="post" action="/setup" style="margin:10px 0">'
                     '<input type="hidden" name="action" value="start">'
                     '<button class="tbtn" style="background:#2b6cb0" type="submit">'
                     'Zastavit sledování a spustit živý náhled</button></form>')
    else:
        parts.append('<form method="post" action="/setup" style="margin:10px 0">'
                     '<input type="hidden" name="action" value="end">'
                     '<button class="tbtn" style="background:#2f855a" type="submit">'
                     'Ukončit náhled a znovu spustit sledování</button></form>')

    parts.append('<h2>Živý náhled</h2>')
    parts.append('<div class="note">Zelená = ROI, červené = spouštěcí čáry. Změny se projeví '
                 'v náhledu po několika vteřinách.</div>')
    parts.append('<img class="stream" id="stream" alt="živý náhled">')
    parts.append('<script>document.getElementById("stream").src="http://"+location.hostname+":8086/stream.mjpg";</script>')

    parts.append('<h2>Okraje ROI a červené čáry</h2>')
    for key, (_sec, step, lo, hi, label) in MARGINS.items():
        parts.append('<div class="row"><div class="lbl">%s</div><div class="val">%.2f</div>%s%s</div>'
                     % (html.escape(label), float(r[key]), _btn("adjust", key, -1), _btn("adjust", key, +1)))

    parts.append('<h2>Vzdálenost mezi červenými čárami</h2>')
    parts.append('<div class="row"><div class="lbl">Skutečná vzdálenost (m)</div>'
                 '<div class="val">%.2f m</div>%s%s</div>'
                 % (length, _btn("adjust", "zone_length_meters", -1), _btn("adjust", "zone_length_meters", +1)))
    parts.append('<form method="post" action="/setup" style="margin:6px 0">'
                 '<input type="hidden" name="action" value="set_length">'
                 'Přesná hodnota (m): <input name="value" type="number" step="0.1" min="1" max="30" value="%.2f" style="width:90px"> '
                 '<button class="tbtn" style="background:#2b6cb0" type="submit">Uložit</button></form>' % length)
    parts.append('<div class="note">Kroky: okraje po 0.01 (1 %%), vzdálenost po %.1f m (přesnou hodnotu lze zadat i s desetinami). Pravidla: levá čára vlevo od pravé, horní okraj nad dolním.</div>' % LENGTH_STEP)
    parts.append('<p><a href="/">← zpět na přehled</a></p>')
    return "\n".join(parts)


def handle_post(form):
    """form: dict of str->list (parse_qs). Returns (ok, message)."""
    action = form.get("action", [""])[0]
    if action == "start":
        return start_setup()
    if action == "end":
        return end_setup()
    if action == "adjust":
        key = form.get("key", [""])[0]
        try:
            delta = int(form.get("delta", ["0"])[0])
        except ValueError:
            return False, "neplatný krok"
        if key not in MARGINS and key != "zone_length_meters":
            return False, "neznámý parametr"
        if delta not in (-1, 1):
            return False, "neplatný krok"
        return apply_change(key, delta=delta)
    if action == "set_length":
        try:
            value = float(form.get("value", [""])[0].replace(",", "."))
        except ValueError:
            return False, "vzdálenost musí být číslo"
        if not (LENGTH_KEY[2] <= value <= LENGTH_KEY[3]):
            return False, "vzdálenost mimo rozsah 1-30 m"
        return apply_change("zone_length_meters", value=value)
    return False, "neplatná akce"
