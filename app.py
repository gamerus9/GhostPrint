#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "Pillow>=10.0.0",
#   "requests>=2.28.0",
#   "tkinterdnd2>=0.3.0",
# ]
# ///
"""SHUI Print Manager — управление 3D-печатью для принтеров на прошивке SHUI."""

import base64
import io
import json
import re
import socket
import subprocess
import threading
import time
import tkinter as tk
from datetime import datetime
from pathlib import Path
from tkinter import messagebox, simpledialog, ttk

try:
    import requests
    HAS_REQUESTS = True
except ImportError:
    HAS_REQUESTS = False

try:
    from PIL import Image, ImageDraw, ImageTk
    HAS_PIL = True
except ImportError:
    HAS_PIL = False

try:
    from tkinterdnd2 import DND_FILES
    from tkinterdnd2.TkinterDnD import _require as _dnd_require
    HAS_DND = True
except ImportError:
    HAS_DND = False
    _dnd_require = None

# ── Settings ──────────────────────────────────────────────────────────────────

SETTINGS_FILE = Path(__file__).parent / "settings.json"
HISTORY_FILE  = Path(__file__).parent / "history.json"

_DEFAULTS = {
    "printer_ip":       "192.168.1.213",
    "upload_speed_kbs": 80,
    "default_cooling":  0,
    "projects_dir":     "projects",
}


def load_settings() -> dict:
    try:
        return {**_DEFAULTS, **json.loads(SETTINGS_FILE.read_text())}
    except Exception:
        return dict(_DEFAULTS)


def save_settings(s: dict):
    SETTINGS_FILE.write_text(json.dumps(s, indent=2, ensure_ascii=False))


_CFG = load_settings()

# ── Config (derived from _CFG) ────────────────────────────────────────────────

PRINTER_IP   = _CFG["printer_ip"]
UPLOAD_URL   = f"http://{PRINTER_IP}/upload"
PROJECTS_DIR = Path(__file__).parent / _CFG["projects_dir"]


def _reload_globals():
    global PRINTER_IP, UPLOAD_URL, PROJECTS_DIR
    PRINTER_IP   = _CFG["printer_ip"]
    UPLOAD_URL   = f"http://{PRINTER_IP}/upload"
    PROJECTS_DIR = Path(__file__).parent / _CFG["projects_dir"]


CARDS_PER_ROW = 3
THUMB_SIZE    = 160
CARD_W        = 210
CARD_PAD      = 14

COOLING_OPTIONS = [
    ("Нет",   0),
    ("1 мин", 60),
    ("2 мин", 120),
    ("3 мин", 180),
    ("5 мин", 300),
]

# ── Palette ───────────────────────────────────────────────────────────────────
# Dark theme inspired by Linear / Vercel — near-black base, clear hierarchy

BG      = "#0e0e0e"   # page background
BG_CARD = "#181818"   # card surface
BG_HDR  = "#111111"   # header / toolbar
BG_INP  = "#1e1e1e"   # input fields
BG_MENU = "#1c1c1c"   # context menus / popovers

FG      = "#efefef"   # primary text
FG2     = "#8a8a8a"   # secondary text
FG_DIM  = "#505050"   # placeholder / disabled

BLUE    = "#3b82f6"   # primary action
BLUE_S  = "#1e3a5f"   # subtle blue tint (status bg)

GREEN   = "#22c55e"   # online / success

RED     = "#ef4444"   # danger / stop / offline

SEP     = "#1e1e1e"   # separator lines
BDR     = "#262626"   # card borders

# Neutral buttons
BTN_N   = "#242424"   # neutral button bg
BTN_FG  = "#efefef"   # button text


# ── Color helpers ─────────────────────────────────────────────────────────────

def _shade(hex_color: str, delta: int) -> str:
    """Lighten (delta>0) or darken (delta<0) a hex RGB color."""
    r = max(0, min(255, int(hex_color[1:3], 16) + delta))
    g = max(0, min(255, int(hex_color[3:5], 16) + delta))
    b = max(0, min(255, int(hex_color[5:7], 16) + delta))
    return f"#{r:02x}{g:02x}{b:02x}"


# ── G-code templates ──────────────────────────────────────────────────────────

COOLING_BLOCK = """\
; --- Cooling ---
M106 S255
G4 S{sec}
M106 S0
; ---
"""

# ── History ───────────────────────────────────────────────────────────────────

def append_history(entry: dict):
    hist = []
    try:
        hist = json.loads(HISTORY_FILE.read_text())
    except Exception:
        pass
    hist.append(entry)
    HISTORY_FILE.write_text(json.dumps(hist[-200:], indent=2))

# ── Thumbnail helpers ─────────────────────────────────────────────────────────

def _extract_orca_thumbnail(text: str):
    """Extract largest OrcaSlicer/BambuStudio embedded PNG thumbnail."""
    if not HAS_PIL:
        return None
    best_img   = None
    best_px    = 0
    lines      = text.splitlines()
    i          = 0
    while i < len(lines):
        m = re.match(r'^; thumbnail begin (\d+)x(\d+)', lines[i])
        if m:
            w, h = int(m.group(1)), int(m.group(2))
            px   = w * h
            i   += 1
            b64  = ""
            while i < len(lines) and not lines[i].startswith('; thumbnail end'):
                ln = lines[i]
                if ln.startswith('; '):
                    b64 += ln[2:]
                elif ln.startswith(';'):
                    b64 += ln[1:]
                i += 1
            if px > best_px:
                try:
                    data    = base64.b64decode(b64)
                    img     = Image.open(io.BytesIO(data)).convert("RGBA")
                    best_px = px
                    best_img = img
                except Exception:
                    pass
        i += 1
    return best_img


def _load_companion_image(gcode_path: Path):
    """Look for .jpg/.png file alongside the .gcode."""
    if not HAS_PIL:
        return None
    for ext in (".jpg", ".jpeg", ".png"):
        p = gcode_path.with_suffix(ext)
        if p.exists():
            try:
                return Image.open(p).convert("RGBA")
            except Exception:
                pass
    return None


def _placeholder_photo():
    """Dark placeholder with a minimal printer icon."""
    if not HAS_PIL:
        return None
    img  = Image.new("RGBA", (THUMB_SIZE, THUMB_SIZE), "#1a1a1a")
    draw = ImageDraw.Draw(img)
    cx, cy = THUMB_SIZE // 2, THUMB_SIZE // 2
    # Printer body
    draw.rectangle([cx-28, cy-10, cx+28, cy+14],
                   fill="#2a2a2a", outline="#383838", width=1)
    # Paper slot top
    draw.rectangle([cx-16, cy-24, cx+16, cy-10], fill="#252525", outline="#383838", width=1)
    # Paper output bottom
    draw.rectangle([cx-18, cy+8,  cx+18, cy+26], fill="#252525")
    # Status LED
    draw.ellipse(  [cx+16, cy-6,  cx+23, cy+1],  fill="#22c55e")
    return ImageTk.PhotoImage(img)


def _make_thumb_photo(img):
    """Fit image inside THUMB_SIZE×THUMB_SIZE, letterbox on dark bg."""
    thumb = img.copy()
    thumb.thumbnail((THUMB_SIZE, THUMB_SIZE), Image.LANCZOS)
    bg  = Image.new("RGBA", (THUMB_SIZE, THUMB_SIZE), "#1a1a1a")
    ox  = (THUMB_SIZE - thumb.width)  // 2
    oy  = (THUMB_SIZE - thumb.height) // 2
    mask = thumb if thumb.mode == "RGBA" else None
    bg.paste(thumb, (ox, oy), mask)
    return ImageTk.PhotoImage(bg)

# ── G-code parsing ────────────────────────────────────────────────────────────

def _parse_print_time(text: str) -> str:
    m = re.search(r'; estimated printing time \(normal mode\) = (.+)', text)
    if m:
        return m.group(1).strip()
    m = re.search(r';TIME:(\d+)', text)
    if m:
        secs = int(m.group(1))
        h, r = divmod(secs, 3600)
        mn, s = divmod(r, 60)
        parts = []
        if h:  parts.append(f"{h}ч")
        if mn: parts.append(f"{mn}м")
        if s:  parts.append(f"{s}с")
        return " ".join(parts)
    m = re.search(r'; Build time: (.+)', text)
    if m:
        return m.group(1).strip()
    return ""


def process_gcode(text: str, cooling_secs: int) -> str:
    """Inject cooling block before every M84 if cooling_secs > 0."""
    if cooling_secs == 0:
        return text

    lines     = text.splitlines(keepends=True)
    out       = []
    found_m84 = False
    for ln in lines:
        if re.match(r'\s*M84\b', ln.strip()):
            out.append(COOLING_BLOCK.format(sec=cooling_secs))
            found_m84 = True
        out.append(ln)
    if not found_m84:
        out.append(COOLING_BLOCK.format(sec=cooling_secs))
        out.append("M84\n")
    return "".join(out)

# ── Printer API (TCP socket, port 8080) ───────────────────────────────────────

def _recv_line(s: socket.socket, timeout: float = 3.0) -> str:
    """Read from socket until '\\n' or timeout.

    SHUI firmware splits responses across multiple TCP packets
    (e.g. 'SHUI: 2025-07-13' arrives first, then the rest).
    A single recv() call only captures the first packet.
    """
    s.settimeout(timeout)
    buf = bytearray()
    while True:
        try:
            chunk = s.recv(256)
            if not chunk:
                break
            buf.extend(chunk)
            if b'\n' in buf:
                break
        except (socket.timeout, OSError):
            break
    return buf.decode(errors="ignore")


def _drain_banner(s: socket.socket):
    """Consume the SHUI welcome banner sent on every new connection."""
    s.settimeout(1.0)
    try:
        s.recv(512)
    except (socket.timeout, OSError):
        pass


def _tcp_command(ip: str, cmd: str, timeout: float = 3.0) -> str:
    """Send a Marlin command over a fresh TCP connection and return the response."""
    if not _tcp_lock.acquire(timeout=5.0):
        return ""
    try:
        with socket.create_connection((ip, 8080), timeout=timeout) as s:
            _drain_banner(s)
            s.settimeout(timeout)
            s.sendall((cmd + "\r\n").encode())
            return _recv_line(s, timeout)
    except (socket.timeout, OSError):
        return ""
    finally:
        _tcp_lock.release()


def get_printer_temps(ip: str) -> dict | None:
    """Return {"hotend": (cur, tgt), "bed": (cur, tgt)} or None.

    SHUI firmware responds with T0: (not T:), e.g.:
      ok T0:44.25 /0.00 B:40.27 /0.00 T0:44.25 /0.00 ...
    """
    try:
        resp = _tcp_command(ip, "M105")
        # T0? matches both "T:" (standard Marlin) and "T0:" (SHUI)
        m = re.search(
            r'T0?:(\d+\.?\d*)\s*/(\d+\.?\d*).*B:(\d+\.?\d*)\s*/(\d+\.?\d*)', resp)
        if m:
            return {
                "hotend": (float(m.group(1)), float(m.group(2))),
                "bed":    (float(m.group(3)), float(m.group(4))),
            }
    except Exception:
        pass
    return None


def get_printer_state(ip: str) -> str | None:
    """Return 'PRINTING' | 'PAUSED' | 'IDLE' or None.

    SHUI firmware does not implement M997. State is derived from M27:
      - 'SD printing byte X/Y'  → PRINTING
      - 'SD printing paused'    → PAUSED
      - 'Not SD printing'       → IDLE
    """
    try:
        resp = _tcp_command(ip, "M27")
        if re.search(r'SD printing byte \d+/\d+', resp):
            return "PRINTING"
        if re.search(r'paused', resp, re.IGNORECASE):
            return "PAUSED"
        if re.search(r'not sd printing', resp, re.IGNORECASE):
            return "IDLE"
        return None
    except Exception:
        return None


def get_print_progress(ip: str) -> tuple[int, int] | None:
    """Return (bytes_done, bytes_total) or None."""
    try:
        resp = _tcp_command(ip, "M27")
        m = re.search(r'SD printing byte (\d+)/(\d+)', resp)
        return (int(m.group(1)), int(m.group(2))) if m else None
    except Exception:
        return None


def check_printer_online(ip: str | None = None) -> bool:
    if ip is None:
        ip = _CFG["printer_ip"]
    return get_printer_temps(ip) is not None


def _query_printer_status(ip: str, timeout: float = 10.0, retries: int = 2) -> dict | None:
    """Single TCP session: M105 + M27 in one connection.

    SHUI firmware handles only one connection at a time.  Two back-to-back
    connections from _do_check_printer caused the second one to time out.
    Bundling both commands avoids the problem and halves TCP overhead.

    Args:
        ip: Printer IP address
        timeout: Connection timeout in seconds (default: 10.0)
        retries: Number of retry attempts (default: 2)

    Returns:
        {"hotend": (cur, tgt), "bed": (cur, tgt), "progress": (done, total)|None}
        or None if unreachable / parse failed.
    """
    if not _tcp_lock.acquire(timeout=5.0):
        return None
    try:
        for attempt in range(retries):
            try:
                with socket.create_connection((ip, 8080), timeout=timeout) as s:
                    _drain_banner(s)
                    s.settimeout(timeout)

                    # M105 — temperatures
                    s.sendall(b"M105\r\n")
                    r105 = _recv_line(s, timeout)
                    m = re.search(
                        r'T0?:(\d+\.?\d*)\s*/(\d+\.?\d*).*B:(\d+\.?\d*)\s*/(\d+\.?\d*)', r105)
                    if not m:
                        return None

                    # M27 — print progress (same open connection, no reconnect needed)
                    s.sendall(b"M27\r\n")
                    r27 = _recv_line(s, timeout)
                    pm = re.search(r'SD printing byte (\d+)/(\d+)', r27)

                    return {
                        "hotend":   (float(m.group(1)), float(m.group(2))),
                        "bed":      (float(m.group(3)), float(m.group(4))),
                        "progress": (int(pm.group(1)), int(pm.group(2))) if pm else None,
                    }
            except (socket.timeout, OSError) as e:
                if attempt < retries - 1:
                    continue
                return None
            except Exception:
                return None
        return None
    finally:
        _tcp_lock.release()


def pause_print(ip: str) -> bool:
    """Pause current print job using M25 (Pause SD print) command."""
    return bool(_tcp_command(ip, "M25").strip())


def resume_print(ip: str) -> bool:
    """Resume paused print job using M24 (Resume SD print) command."""
    return bool(_tcp_command(ip, "M24").strip())


def stop_print(ip: str) -> bool:
    """Stop current SD print job (M26)."""
    return bool(_tcp_command(ip, "M26").strip())


# ── TCP serialisation lock ────────────────────────────────────────────────────
_tcp_lock = threading.Lock()


class _ProgressReader(io.BytesIO):
    """BytesIO wrapper that calls a callback(sent, total) on each read."""

    def __init__(self, data: bytes, callback=None):
        super().__init__(data)
        self._cb  = callback
        self._len = len(data)

    def read(self, n=-1):
        chunk = super().read(n)
        if self._cb and chunk:
            self._cb(self.tell(), self._len)
        return chunk


def upload_gcode(name: str, content: bytes,
                 progress_cb=None) -> tuple[bool, str]:
    """Upload gcode to printer using MKS WiFi raw octet-stream protocol.

    MKS WiFi (ESP8266/SHUI) expects:
      POST /upload?X-Filename=<name>
      Content-Type: application/octet-stream
      (raw bytes body, no multipart)
    """
    if not HAS_REQUESTS:
        return False, "requests не установлен"
    try:
        url = f"{UPLOAD_URL}?X-Filename={name}"
        headers = {
            "Content-Type":   "application/octet-stream",
            "Connection":     "keep-alive",
            "Content-Length": str(len(content)),
        }
        read_timeout = max(180, len(content) // 10_240 + 120)
        body = _ProgressReader(content, progress_cb)
        t0   = time.monotonic()
        r    = requests.post(url, data=body, headers=headers,
                             timeout=(10, read_timeout))
        elapsed   = max(time.monotonic() - t0, 0.1)
        data      = r.json()
        if data.get("err", 1) == 0:
            speed_kbs = len(content) / (elapsed * 1024)
            return True, f"✓ Файл «{name}» отправлен  ({speed_kbs:.0f} КБ/с)"
        return False, f"Принтер вернул ошибку: {data}"
    except requests.Timeout:
        return False, "Таймаут загрузки — принтер не ответил вовремя"
    except Exception as e:
        return False, f"Ошибка: {e}"

# ── Custom widget: FlatBtn ────────────────────────────────────────────────────
# On macOS, tk.Button ignores bg color (Aqua theme overrides it).
# FlatBtn is a Frame+Label that actually respects the background.

class FlatBtn(tk.Frame):
    """Color-correct flat button for macOS/Tk."""

    def __init__(self, parent, text="", command=None,
                 bg=BTN_N, fg=BTN_FG, font=None,
                 padx=14, pady=6, **kw):
        active = _shade(bg, -10)
        super().__init__(parent, bg=bg, cursor="hand2", **kw)
        self._bg     = bg
        self._active = active
        self._cmd    = command

        self._lbl = tk.Label(
            self, text=text, bg=bg, fg=fg,
            font=font or ("Helvetica", 9),
            padx=padx, pady=pady, cursor="hand2")
        self._lbl.pack()

        for w in (self, self._lbl):
            w.bind("<Button-1>",        self._on_press)
            w.bind("<ButtonRelease-1>", self._on_release)

    def _set_bg(self, color: str):
        tk.Frame.configure(self, bg=color)
        self._lbl.configure(bg=color)

    def _on_press(self, _=None):
        self._set_bg(self._active)

    def _on_release(self, evt=None):
        self._set_bg(self._bg)
        if self._cmd:
            self._cmd()

    def configure(self, **kw):
        if "text"    in kw: self._lbl.configure(text=kw.pop("text"))
        if "command" in kw: self._cmd = kw.pop("command")
        if "fg"      in kw: self._lbl.configure(fg=kw.pop("fg"))
        if "font"    in kw: self._lbl.configure(font=kw.pop("font"))
        if kw:
            super().configure(**kw)

    config = configure

# ── GcodeProject ──────────────────────────────────────────────────────────────

class GcodeProject:
    __slots__ = ("path", "name", "size_str", "print_time", "thumb_img", "thumb_photo")

    def __init__(self, path: Path):
        self.path       = path
        self.name       = path.name
        sz              = path.stat().st_size
        if sz >= 1_048_576:
            self.size_str = f"{sz/1_048_576:.1f} МБ"
        elif sz >= 1024:
            self.size_str = f"{sz/1024:.0f} КБ"
        else:
            self.size_str = f"{sz} Б"
        self.print_time  = ""
        self.thumb_img   = None
        self.thumb_photo = None

    def load(self):
        try:
            text = self.path.read_text(encoding="utf-8", errors="replace")
            self.print_time = _parse_print_time(text)
            img = _extract_orca_thumbnail(text)
            if img is None:
                img = _load_companion_image(self.path)
            self.thumb_img = img
        except Exception:
            pass

# ── SendDialog ────────────────────────────────────────────────────────────────

class SendDialog(tk.Toplevel):
    """Modal: choose cooling time and action (send / save)."""

    def __init__(self, parent, project: GcodeProject):
        super().__init__(parent)
        self.project = project
        self.result  = None  # (cooling_secs, action)

        self.title("Отправить на принтер")
        self.configure(bg=BG)
        self.resizable(False, False)
        self.grab_set()
        self.transient(parent)

        w, h = 420, 220
        px = parent.winfo_rootx() + (parent.winfo_width()  - w) // 2
        py = parent.winfo_rooty() + (parent.winfo_height() - h) // 2
        self.geometry(f"{w}x{h}+{px}+{py}")
        self._build()
        self.wait_window()

    def _build(self):
        title_bar = tk.Frame(self, bg=BG_HDR)
        title_bar.pack(fill=tk.X)
        tk.Label(title_bar, text="📄  " + self.project.name,
                 bg=BG_HDR, fg=FG, font=("Helvetica", 11, "bold"),
                 wraplength=380, anchor="w",
                 padx=20, pady=14).pack(fill=tk.X)
        tk.Frame(self, bg=SEP, height=1).pack(fill=tk.X)

        body = tk.Frame(self, bg=BG)
        body.pack(fill=tk.BOTH, expand=True, padx=20, pady=14)

        # ── Cooling ───────────────────────────────────────────────────────────
        tk.Label(body, text="Охлаждение после печати",
                 bg=BG, fg=FG2, font=("Helvetica", 9)).pack(anchor="w", pady=(0, 6))

        self._cooling = tk.IntVar(value=_CFG["default_cooling"])
        radio_row = tk.Frame(body, bg=BG)
        radio_row.pack(anchor="w")
        for label, val in COOLING_OPTIONS:
            tk.Radiobutton(radio_row, text=label, variable=self._cooling, value=val,
                           bg=BG, fg=FG, selectcolor=BG_INP,
                           activebackground=BG, activeforeground=FG,
                           font=("Helvetica", 9)).pack(side=tk.LEFT, padx=(0, 6))

        # ── Buttons ───────────────────────────────────────────────────────────
        tk.Frame(self, bg=SEP, height=1).pack(fill=tk.X)
        btn_bar = tk.Frame(self, bg=BG_HDR)
        btn_bar.pack(fill=tk.X, padx=16, pady=12)

        FlatBtn(btn_bar, text="Отмена", command=self.destroy,
                bg=BTN_N, fg=BTN_FG, padx=16, pady=7
                ).pack(side=tk.RIGHT, padx=(6, 0))
        FlatBtn(btn_bar, text="  Отправить  ", command=self._send,
                bg=BLUE, fg="#ffffff", padx=16, pady=7,
                font=("Helvetica", 9, "bold")
                ).pack(side=tk.RIGHT)
        FlatBtn(btn_bar, text="💾 Сохранить", command=self._save,
                bg=BTN_N, fg=BTN_FG, padx=12, pady=7
                ).pack(side=tk.RIGHT, padx=(0, 8))

    def _send(self):
        self.result = (self._cooling.get(), "send")
        self.destroy()

    def _save(self):
        self.result = (self._cooling.get(), "save")
        self.destroy()

# ── SettingsDialog ────────────────────────────────────────────────────────────

class SettingsDialog(tk.Toplevel):

    def __init__(self, parent, on_save=None):
        super().__init__(parent)
        self.title("Настройки")
        self.configure(bg=BG)
        self.resizable(False, False)
        self.grab_set()
        self.transient(parent)
        self._on_save = on_save

        w, h = 380, 295
        px = parent.winfo_rootx() + (parent.winfo_width()  - w) // 2
        py = parent.winfo_rooty() + (parent.winfo_height() - h) // 2
        self.geometry(f"{w}x{h}+{px}+{py}")
        self._build()
        self.wait_window()

    def _build(self):
        tk.Label(self, text="Настройки", bg=BG_HDR, fg=FG,
                 font=("Helvetica", 12, "bold"),
                 padx=20, pady=14, anchor="w"
                 ).pack(fill=tk.X)
        tk.Frame(self, bg=SEP, height=1).pack(fill=tk.X)

        body = tk.Frame(self, bg=BG)
        body.pack(fill=tk.BOTH, expand=True, padx=20, pady=16)

        rows = [
            ("IP принтера",            "_ip_var",   _CFG["printer_ip"]),
            ("Скорость загрузки КБ/с", "_spd_var",  str(_CFG["upload_speed_kbs"])),
            ("Охлаждение по умолч. с", "_cool_var", str(_CFG["default_cooling"])),
        ]
        for row_i, (label, attr, default) in enumerate(rows):
            var = tk.StringVar(value=default)
            setattr(self, attr, var)
            tk.Label(body, text=label, bg=BG, fg=FG2,
                     font=("Helvetica", 9), anchor="w", width=22
                     ).grid(row=row_i, column=0, sticky="w", pady=5)
            e = tk.Entry(body, textvariable=var, width=18,
                         bg=BG_INP, fg=FG, insertbackground=FG,
                         relief=tk.FLAT, font=("Helvetica", 9),
                         highlightthickness=1,
                         highlightbackground=BDR, highlightcolor=BLUE)
            e.grid(row=row_i, column=1, sticky="ew", padx=(10, 0), ipady=4)

        body.columnconfigure(1, weight=1)

        tk.Frame(self, bg=SEP, height=1).pack(fill=tk.X)
        btn_bar = tk.Frame(self, bg=BG_HDR)
        btn_bar.pack(fill=tk.X, padx=16, pady=12)

        FlatBtn(btn_bar, text="Отмена", command=self.destroy,
                bg=BTN_N, fg=BTN_FG, padx=16, pady=7
                ).pack(side=tk.RIGHT, padx=(6, 0))
        FlatBtn(btn_bar, text="  Сохранить  ", command=self._save,
                bg=BLUE, fg="#ffffff", padx=16, pady=7,
                font=("Helvetica", 9, "bold")
                ).pack(side=tk.RIGHT)

    def _save(self):
        try:
            new_cfg = {
                "printer_ip":       self._ip_var.get().strip(),
                "upload_speed_kbs": int(self._spd_var.get()),
                "default_cooling":  int(self._cool_var.get()),
                "projects_dir":     _CFG.get("projects_dir", "projects"),
            }
        except ValueError as e:
            messagebox.showerror("Ошибка", f"Неверное значение: {e}", parent=self)
            return
        _CFG.update(new_cfg)
        save_settings(_CFG)
        _reload_globals()
        if self._on_save:
            self._on_save()
        self.destroy()

# ── HistoryDialog ─────────────────────────────────────────────────────────────

class HistoryDialog(tk.Toplevel):

    def __init__(self, parent):
        super().__init__(parent)
        self.title("История отправок")
        self.configure(bg=BG)
        self.geometry("660x420")
        self.grab_set()
        self.transient(parent)
        self._build()

    def _build(self):
        tk.Label(self, text="История отправок", bg=BG_HDR, fg=FG,
                 font=("Helvetica", 12, "bold"),
                 padx=20, pady=14, anchor="w"
                 ).pack(fill=tk.X)
        tk.Frame(self, bg=SEP, height=1).pack(fill=tk.X)

        style = ttk.Style(self)
        style.configure("Hist.Treeview",
                        background=BG_CARD, foreground=FG,
                        fieldbackground=BG_CARD, rowheight=26,
                        font=("Helvetica", 9))
        style.configure("Hist.Treeview.Heading",
                        background=BG_HDR, foreground=FG2,
                        font=("Helvetica", 8, "bold"))
        style.map("Hist.Treeview", background=[("selected", BLUE_S)],
                  foreground=[("selected", FG)])

        frame = tk.Frame(self, bg=BG)
        frame.pack(fill=tk.BOTH, expand=True, padx=12, pady=12)

        cols = ("ts", "file", "cooling", "ok")
        self._tree = ttk.Treeview(frame, columns=cols, show="headings",
                                  style="Hist.Treeview", height=13)
        self._tree.heading("ts",      text="Время")
        self._tree.heading("file",    text="Файл")
        self._tree.heading("cooling", text="Охлажд.")
        self._tree.heading("ok",      text="Итог")
        self._tree.column("ts",      width=150, anchor="w")
        self._tree.column("file",    width=270, anchor="w")
        self._tree.column("cooling", width=80,  anchor="center")
        self._tree.column("ok",      width=60,  anchor="center")

        vsb = ttk.Scrollbar(frame, orient=tk.VERTICAL, command=self._tree.yview)
        self._tree.configure(yscrollcommand=vsb.set)
        vsb.pack(side=tk.RIGHT, fill=tk.Y)
        self._tree.pack(fill=tk.BOTH, expand=True)

        self._load_entries()

        tk.Frame(self, bg=SEP, height=1).pack(fill=tk.X)
        btn_bar = tk.Frame(self, bg=BG_HDR)
        btn_bar.pack(fill=tk.X, padx=16, pady=10)
        FlatBtn(btn_bar, text="Очистить историю", command=self._clear,
                bg=BTN_N, fg=BTN_FG, padx=14, pady=6
                ).pack(side=tk.RIGHT)

    def _load_entries(self):
        for item in self._tree.get_children():
            self._tree.delete(item)
        hist = []
        try:
            hist = json.loads(HISTORY_FILE.read_text())
        except Exception:
            pass
        for entry in reversed(hist):
            self._tree.insert("", tk.END, values=(
                entry.get("ts", ""),
                entry.get("file", ""),
                f'{entry.get("cooling", 0)} с',
                "✓" if entry.get("success") else "✗",
            ))

    def _clear(self):
        if messagebox.askyesno("Очистить", "Удалить всю историю отправок?",
                               parent=self):
            try:
                HISTORY_FILE.write_text("[]")
            except Exception:
                pass
            self._load_entries()

# ── TerminalPanel ─────────────────────────────────────────────────────────────

class TerminalPanel(tk.Frame):
    """Embedded G-code terminal panel."""

    MAX_LINES  = 500
    QUICK_CMDS = [
        ("M105", "температуры"),
        ("M27",  "прогресс"),
        ("G28",  "парковка"),
        ("M114", "позиция"),
    ]

    def __init__(self, parent, app, **kw):
        super().__init__(parent, bg=BG_HDR, **kw)
        self._app      = app
        self._history: list[str] = []
        self._hist_pos = -1
        self._build()

    def _build(self):
        # ── Title bar ─────────────────────────────────────────────────────────
        title_bar = tk.Frame(self, bg=BG_HDR, height=28)
        title_bar.pack(fill=tk.X)
        title_bar.pack_propagate(False)
        tk.Label(title_bar, text=">_  Терминал",
                 bg=BG_HDR, fg=FG,
                 font=("Courier", 10, "bold"),
                 padx=10, pady=0).pack(side=tk.LEFT)
        FlatBtn(title_bar, text="✕", command=self._close,
                bg=BG_HDR, fg=FG2, padx=8, pady=2,
                font=("Helvetica", 10)).pack(side=tk.RIGHT, padx=4)

        # ── Quick commands bar ────────────────────────────────────────────────
        qbar = tk.Frame(self, bg=BG_HDR)
        qbar.pack(fill=tk.X, padx=6, pady=(0, 2))
        for cmd, desc in self.QUICK_CMDS:
            FlatBtn(qbar, text=f"{cmd}  {desc}",
                    command=lambda c=cmd: self._app._send_terminal_command(c),
                    bg=BTN_N, fg=FG2, padx=8, pady=2,
                    font=("Courier", 8)).pack(side=tk.LEFT, padx=2, pady=2)

        # ── Output area ───────────────────────────────────────────────────────
        out_frame = tk.Frame(self, bg=BG_INP)
        out_frame.pack(fill=tk.X)
        self._out = tk.Text(
            out_frame, height=8, state=tk.DISABLED,
            bg=BG_INP, fg=FG, font=("Courier", 9),
            relief=tk.FLAT, padx=6, pady=4,
            highlightthickness=0, wrap=tk.WORD)
        vsb = ttk.Scrollbar(out_frame, orient=tk.VERTICAL,
                            command=self._out.yview)
        self._out.configure(yscrollcommand=vsb.set)
        vsb.pack(side=tk.RIGHT, fill=tk.Y)
        self._out.pack(fill=tk.BOTH, expand=True)
        self._out.tag_config("sent",   foreground=BLUE)
        self._out.tag_config("recv",   foreground=FG)
        self._out.tag_config("error",  foreground=RED)
        self._out.tag_config("status", foreground=FG_DIM)

        # ── Input row ─────────────────────────────────────────────────────────
        in_row = tk.Frame(self, bg=BG_HDR)
        in_row.pack(fill=tk.X, padx=6, pady=4)
        tk.Label(in_row, text=">>", bg=BG_HDR, fg=FG2,
                 font=("Courier", 9)).pack(side=tk.LEFT, padx=(4, 4))
        self._entry_var = tk.StringVar()
        self._entry = tk.Entry(
            in_row, textvariable=self._entry_var,
            bg=BG_INP, fg=FG, insertbackground=FG,
            relief=tk.FLAT, font=("Courier", 9),
            highlightthickness=1,
            highlightbackground=BDR, highlightcolor=BLUE)
        self._entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 6), ipady=4)
        FlatBtn(in_row, text="Send", command=self._on_send,
                bg=BLUE, fg="#ffffff", padx=12, pady=4,
                font=("Helvetica", 9, "bold")).pack(side=tk.RIGHT)
        self._entry.bind("<Return>", lambda _: self._on_send())
        self._entry.bind("<Up>",     self._history_up)
        self._entry.bind("<Down>",   self._history_down)

    def _close(self):
        self._app._toggle_terminal()

    def _on_send(self):
        cmd = self._entry_var.get().strip()
        if not cmd:
            return
        if not self._history or self._history[-1] != cmd:
            self._history.append(cmd)
            if len(self._history) > 100:
                self._history.pop(0)
        self._hist_pos = -1
        self._entry_var.set("")
        self._app._send_terminal_command(cmd)

    def _history_up(self, _=None):
        if not self._history:
            return
        if self._hist_pos == -1:
            self._hist_pos = len(self._history) - 1
        elif self._hist_pos > 0:
            self._hist_pos -= 1
        self._entry_var.set(self._history[self._hist_pos])
        self._entry.icursor(tk.END)

    def _history_down(self, _=None):
        if self._hist_pos == -1:
            return
        if self._hist_pos < len(self._history) - 1:
            self._hist_pos += 1
            self._entry_var.set(self._history[self._hist_pos])
        else:
            self._hist_pos = -1
            self._entry_var.set("")
        self._entry.icursor(tk.END)

    def append(self, text: str, tag: str = "recv"):
        """Append a line to the output area (call from main thread only)."""
        self._out.configure(state=tk.NORMAL)
        self._out.insert(tk.END, text + "\n", tag)
        lines = int(self._out.index(tk.END).split(".")[0]) - 1
        if lines > self.MAX_LINES:
            self._out.delete("1.0", f"{lines - self.MAX_LINES}.0")
        self._out.configure(state=tk.DISABLED)
        self._out.see(tk.END)

    def focus_entry(self):
        """Focus the command input field."""
        self._entry.focus_set()


# ── ProjectCard ───────────────────────────────────────────────────────────────

class ProjectCard(tk.Frame):
    def __init__(self, parent, project: GcodeProject, on_send, on_reload=None, **kw):
        super().__init__(parent, bg=BG_CARD,
                         highlightbackground=BDR, highlightthickness=1, **kw)
        self.project    = project
        self._on_send   = on_send
        self._on_reload = on_reload
        self._build()

    def _build(self):
        # Thumbnail
        self._thumb = tk.Label(self, bg="#1a1a1a",
                               width=THUMB_SIZE, height=THUMB_SIZE)
        self._thumb.pack(pady=(12, 8), padx=12)

        # File name
        name = self.project.name
        if len(name) > 28:
            name = name[:25] + "…"
        self._name_lbl = tk.Label(
            self, text=name, bg=BG_CARD, fg=FG,
            font=("Helvetica", 9, "bold"),
            wraplength=CARD_W - 16)
        self._name_lbl.pack(padx=10)

        # Size + time badge
        meta = self.project.size_str
        if self.project.print_time:
            meta += f"  ·  {self.project.print_time}"
        self._meta_lbl = tk.Label(
            self, text=meta, bg=BG_CARD, fg=FG2,
            font=("Helvetica", 8))
        self._meta_lbl.pack(pady=(3, 10))

        # Send button
        self._send_btn = FlatBtn(
            self, text="Отправить на принтер",
            command=lambda: self._on_send(self.project),
            bg=BLUE, fg="#ffffff",
            font=("Helvetica", 9, "bold"), padx=12, pady=5)
        self._send_btn.pack(pady=(0, 14))

        # Context menu
        for w in [self] + list(self.winfo_children()):
            w.bind("<Button-2>", self._ctx_menu)
            w.bind("<Button-3>", self._ctx_menu)

    def set_thumb(self, photo):
        self._thumb.configure(image=photo,
                              width=THUMB_SIZE, height=THUMB_SIZE)
        self._thumb._photo = photo

    def _ctx_menu(self, event):
        menu = tk.Menu(self, tearoff=0,
                       bg=BG_MENU, fg=FG,
                       activebackground=BLUE_S, activeforeground=FG,
                       relief=tk.FLAT, bd=0,
                       font=("Helvetica", 10))
        menu.add_command(
            label="  Открыть в Finder",
            command=lambda: subprocess.run(["open", "-R", str(self.project.path)]))
        menu.add_command(
            label="  Открыть в OrcaSlicer",
            command=lambda: subprocess.run(
                ["open", "-a", "OrcaSlicer", str(self.project.path)]))
        menu.add_separator()
        menu.add_command(label="  Переименовать", command=self._rename)
        menu.add_command(label="  Удалить",        command=self._delete)
        menu.tk_popup(event.x_root, event.y_root)

    def _rename(self):
        new_name = simpledialog.askstring(
            "Переименовать", "Новое имя файла:",
            initialvalue=self.project.name, parent=self)
        if new_name and new_name != self.project.name:
            try:
                self.project.path.rename(self.project.path.parent / new_name)
                if self._on_reload:
                    self._on_reload()
            except Exception as e:
                messagebox.showerror("Ошибка", str(e), parent=self)

    def _delete(self):
        if messagebox.askyesno(
                "Удалить файл",
                f"Удалить «{self.project.name}»?\nЭто действие нельзя отменить.",
                parent=self):
            try:
                self.project.path.unlink()
                if self._on_reload:
                    self._on_reload()
            except Exception as e:
                messagebox.showerror("Ошибка", str(e), parent=self)

# ── Main App ──────────────────────────────────────────────────────────────────

class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("SHUI Print Manager")
        self.geometry("980x700")
        self.minsize(720, 520)
        self.configure(bg=BG)

        # Try to enable drag-and-drop (may fail if native lib is incompatible)
        self._dnd_enabled = False
        if HAS_DND:
            try:
                _dnd_require(self)
                self._dnd_enabled = True
            except RuntimeError:
                pass

        PROJECTS_DIR.mkdir(parents=True, exist_ok=True)

        self._projects: list[GcodeProject] = []
        self._loading  = False
        self._search_var = tk.StringVar()
        self._sort_var   = tk.StringVar(value="date")
        self._sort_btns: dict[str, FlatBtn] = {}
        self._terminal_visible = False

        self._build_ui()
        self._refresh()
        self._schedule_printer_check()

    # ── UI construction ───────────────────────────────────────────────────────

    def _build_ui(self):
        # ── Header ────────────────────────────────────────────────────────────
        hdr = tk.Frame(self, bg=BG_HDR, height=56)
        hdr.pack(fill=tk.X)
        hdr.pack_propagate(False)

        # Left: logo
        tk.Label(hdr, text="SHUI",
                 bg=BG_HDR, fg=FG,
                 font=("Helvetica", 14, "bold")).pack(side=tk.LEFT, padx=(20, 4))
        tk.Label(hdr, text="Print Manager",
                 bg=BG_HDR, fg=FG2,
                 font=("Helvetica", 14)).pack(side=tk.LEFT)

        # Printer status badge (LEFT side, after title)
        self._status_badge = tk.Frame(hdr, bg=BG_HDR)
        self._status_badge.pack(side=tk.LEFT, padx=(20, 0))

        self._dot = tk.Label(self._status_badge, text="●",
                             bg=BG_HDR, fg=RED, font=("Helvetica", 10))
        self._dot.pack(side=tk.LEFT)
        self._dot_lbl = tk.Label(self._status_badge, text="Offline",
                                 bg=BG_HDR, fg=FG2, font=("Helvetica", 9))
        self._dot_lbl.pack(side=tk.LEFT, padx=(3, 0))

        # Right: action buttons
        FlatBtn(hdr, text="↻ Обновить", command=self._refresh,
                bg=BTN_N, fg=BTN_FG, padx=12, pady=6,
                font=("Helvetica", 9)
                ).pack(side=tk.RIGHT, padx=(4, 16), pady=10)

        FlatBtn(hdr, text="⚙", command=self._open_settings,
                bg=BG_HDR, fg=FG2, padx=10, pady=6,
                font=("Helvetica", 13)
                ).pack(side=tk.RIGHT, padx=2, pady=10)

        FlatBtn(hdr, text="📋", command=self._open_history,
                bg=BG_HDR, fg=FG2, padx=10, pady=6,
                font=("Helvetica", 13)
                ).pack(side=tk.RIGHT, padx=2, pady=10)

        FlatBtn(hdr, text=">_", command=self._toggle_terminal,
                bg=BG_HDR, fg=FG2, padx=10, pady=6,
                font=("Courier", 11, "bold")
                ).pack(side=tk.RIGHT, padx=2, pady=10)

        # Pause/Resume button — hidden until printer is PRINTING or PAUSED
        self._pause_btn = FlatBtn(
            hdr, text="⏸  Пауза", command=self._pause_print_action,
            bg=BTN_N, fg=BTN_FG, padx=12, pady=6,
            font=("Helvetica", 9, "bold"))
        # Not packed initially
        
        # Stop button — hidden until printer is PRINTING or PAUSED
        self._stop_btn = FlatBtn(
            hdr, text="⏹  Стоп", command=self._stop_print_action,
            bg=RED, fg="#ffffff", padx=12, pady=6,
            font=("Helvetica", 9, "bold"))
        # Not packed initially

        tk.Frame(self, bg=SEP, height=1).pack(fill=tk.X)

        # ── Toolbar (search + sort) ────────────────────────────────────────────
        toolbar = tk.Frame(self, bg=BG_HDR, height=42)
        toolbar.pack(fill=tk.X)
        toolbar.pack_propagate(False)

        # Search field with inline icon
        search_wrap = tk.Frame(toolbar, bg=BG_INP,
                               highlightthickness=1,
                               highlightbackground=BDR,
                               highlightcolor=BLUE)
        search_wrap.pack(side=tk.LEFT, padx=(14, 0), pady=8)
        tk.Label(search_wrap, text="🔍", bg=BG_INP, fg=FG_DIM,
                 font=("Helvetica", 10)).pack(side=tk.LEFT, padx=(6, 2))
        self._search_var.trace_add("write", lambda *_: self._refresh())
        tk.Entry(search_wrap, textvariable=self._search_var,
                 bg=BG_INP, fg=FG, insertbackground=FG,
                 relief=tk.FLAT, font=("Helvetica", 9), width=22,
                 highlightthickness=0
                 ).pack(side=tk.LEFT, padx=(0, 6), ipady=4)

        # Sort buttons
        sort_wrap = tk.Frame(toolbar, bg=BG_HDR)
        sort_wrap.pack(side=tk.LEFT, padx=(12, 0), pady=8)
        tk.Label(sort_wrap, text="Сортировка:", bg=BG_HDR, fg=FG_DIM,
                 font=("Helvetica", 8)).pack(side=tk.LEFT, padx=(0, 6))

        for label, key in [("Дата", "date"), ("Имя", "name"), ("Размер", "size")]:
            is_active = (key == "date")
            btn = FlatBtn(
                sort_wrap, text=label,
                command=lambda k=key: self._set_sort(k),
                bg=BLUE if is_active else BTN_N,
                fg="#ffffff" if is_active else FG2,
                font=("Helvetica", 8), padx=10, pady=4)
            btn.pack(side=tk.LEFT, padx=2)
            self._sort_btns[key] = btn

        tk.Frame(self, bg=SEP, height=1).pack(fill=tk.X)

        # ── Status bar (packed first so it anchors at the very bottom) ──────────
        status_sep = tk.Frame(self, bg=SEP, height=1)
        status_sep.pack(fill=tk.X, side=tk.BOTTOM)
        status_bar = tk.Frame(self, bg=BG_HDR)
        status_bar.pack(side=tk.BOTTOM, fill=tk.X)
        self._status = tk.Label(status_bar, text="Готов",
                                bg=BG_HDR, fg=FG_DIM,
                                font=("Helvetica", 9), anchor="w")
        self._status.pack(side=tk.LEFT, padx=16, pady=6)

        if self._dnd_enabled:
            tk.Label(status_bar, text="Перетащите .gcode сюда",
                     bg=BG_HDR, fg=FG_DIM,
                     font=("Helvetica", 8)).pack(side=tk.RIGHT, padx=14)

        # ── Terminal (hidden by default) ───────────────────────────────────────
        self._terminal_sep = tk.Frame(self, bg=SEP, height=1)
        self._terminal     = TerminalPanel(self, app=self)
        # Not packed — _toggle_terminal manages visibility

        # ── Scrollable canvas ──────────────────────────────────────────────────
        canvas_frame = tk.Frame(self, bg=BG)

        self._canvas = tk.Canvas(canvas_frame, bg=BG, highlightthickness=0)
        vsb = ttk.Scrollbar(canvas_frame, orient=tk.VERTICAL,
                            command=self._canvas.yview)
        self._canvas.configure(yscrollcommand=vsb.set)
        vsb.pack(side=tk.RIGHT, fill=tk.Y)
        self._canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        self._inner = tk.Frame(self._canvas, bg=BG)
        self._win_id = self._canvas.create_window(
            (0, 0), window=self._inner, anchor="nw")

        self._inner.bind("<Configure>", self._on_inner_configure)
        self._canvas.bind("<Configure>", self._on_canvas_configure)
        for seq in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
            self._canvas.bind(seq, self._on_scroll)
        self.bind_all("<MouseWheel>", self._on_scroll)

        if self._dnd_enabled:
            self._canvas.drop_target_register(DND_FILES)
            self._canvas.dnd_bind("<<Drop>>", self._on_drop)

        canvas_frame.pack(fill=tk.BOTH, expand=True)

    def _on_inner_configure(self, _evt=None):
        self._canvas.configure(scrollregion=self._canvas.bbox("all"))

    def _on_canvas_configure(self, evt):
        self._canvas.itemconfig(self._win_id, width=evt.width)

    def _on_scroll(self, evt):
        if evt.num == 4:
            self._canvas.yview_scroll(-1, "units")
        elif evt.num == 5:
            self._canvas.yview_scroll(1, "units")
        elif evt.delta:
            self._canvas.yview_scroll(-1 * (evt.delta // 120), "units")

    def _set_sort(self, key: str):
        self._sort_var.set(key)
        for k, btn in self._sort_btns.items():
            is_active = (k == key)
            btn._bg = BLUE if is_active else BTN_N
            btn._lbl.configure(fg="#ffffff" if is_active else FG2)
            btn._set_bg(btn._bg)
        self._refresh()

    # ── Header actions ────────────────────────────────────────────────────────

    def _open_settings(self):
        SettingsDialog(self, on_save=self._refresh)

    def _open_history(self):
        HistoryDialog(self)

    def _toggle_terminal(self):
        if self._terminal_visible:
            self._terminal_sep.pack_forget()
            self._terminal.pack_forget()
            self._terminal_visible = False
        else:
            self._terminal_sep.pack(side=tk.BOTTOM, fill=tk.X)
            self._terminal.pack(side=tk.BOTTOM, fill=tk.X)
            self._terminal_visible = True
            self._terminal.focus_entry()

    def _send_terminal_command(self, cmd: str):
        ts = datetime.now().strftime("%H:%M:%S")
        self._terminal.append(f"[{ts}] >> {cmd}", "sent")
        threading.Thread(
            target=self._do_terminal_command, args=(_CFG["printer_ip"], cmd),
            daemon=True).start()

    def _do_terminal_command(self, ip: str, cmd: str):
        resp = _tcp_command(ip, cmd)
        ts   = datetime.now().strftime("%H:%M:%S")
        if resp.strip():
            self.after(0, self._terminal.append, f"[{ts}]    {resp.strip()}", "recv")
        else:
            if not _tcp_lock.acquire(blocking=False):
                self.after(0, self._terminal.append,
                           f"[{ts}]    [занято — другая команда в пути]", "error")
            else:
                _tcp_lock.release()
                self.after(0, self._terminal.append,
                           f"[{ts}]    [нет ответа]", "error")

    def _pause_print_action(self):
        """Handle pause/resume button click."""
        # Check current printer state to determine action
        state = get_printer_state(_CFG["printer_ip"])
        
        if state == "PRINTING":
            # Pause the print
            ok = pause_print(_CFG["printer_ip"])
            self._set_status("Печать на паузе" if ok else "Ошибка: не удалось поставить на паузу")
        elif state == "PAUSED":
            # Resume the print
            ok = resume_print(_CFG["printer_ip"])
            self._set_status("Печать возобновлена" if ok else "Ошибка: не удалось возобновить")
        else:
            self._set_status(f"Ошибка: неожиданное состояние принтера ({state})")

    def _stop_print_action(self):
        if messagebox.askyesno("Остановить печать",
                               "Остановить текущую печать?\n\nЭто аварийная остановка - все нагреватели будут выключены!", parent=self):
            ok = stop_print(_CFG["printer_ip"])
            self._set_status("Печать остановлена" if ok else "Ошибка: не удалось остановить")

    # ── Project loading ───────────────────────────────────────────────────────

    def _refresh(self):
        if self._loading:
            return
        self._loading = True
        search = self._search_var.get().lower().strip()
        sort   = self._sort_var.get()
        self._set_status("Сканирование…")
        threading.Thread(target=self._scan, args=(search, sort), daemon=True).start()

    def _scan(self, search: str, sort: str):
        try:
            all_files = list(PROJECTS_DIR.glob("*.gcode"))
            if search:
                all_files = [f for f in all_files if search in f.name.lower()]
            if sort == "date":
                files = sorted(all_files, key=lambda p: p.stat().st_mtime, reverse=True)
            elif sort == "name":
                files = sorted(all_files, key=lambda p: p.name.lower())
            elif sort == "size":
                files = sorted(all_files, key=lambda p: p.stat().st_size, reverse=True)
            else:
                files = all_files
            projects = [GcodeProject(f) for f in files]
            for p in projects:
                p.load()
            self.after(0, self._render_projects, projects)
        except Exception as e:
            self.after(0, self._set_status, f"Ошибка сканирования: {e}")
            self._loading = False

    def _render_projects(self, projects: list[GcodeProject]):
        for w in self._inner.winfo_children():
            w.destroy()
        self._projects = projects

        if not projects:
            empty = tk.Frame(self._inner, bg=BG)
            empty.pack(pady=100)
            tk.Label(empty, text="Нет файлов .gcode",
                     bg=BG, fg=FG2,
                     font=("Helvetica", 14)).pack()
            tk.Label(empty, text=f"Положите файлы в папку  {PROJECTS_DIR.name}/",
                     bg=BG, fg=FG_DIM,
                     font=("Helvetica", 10)).pack(pady=(8, 0))
            self._set_status("Папка projects/ пуста")
            self._loading = False
            return

        for c in range(CARDS_PER_ROW):
            self._inner.columnconfigure(c, weight=1)

        _ph = _placeholder_photo() if HAS_PIL else None

        for i, proj in enumerate(projects):
            row, col = divmod(i, CARDS_PER_ROW)
            card = ProjectCard(self._inner, proj,
                               on_send=self._send_project,
                               on_reload=self._refresh,
                               width=CARD_W)
            card.grid(row=row, column=col,
                      padx=CARD_PAD, pady=CARD_PAD, sticky="n")
            if proj.thumb_img is not None and HAS_PIL:
                photo = _make_thumb_photo(proj.thumb_img)
                card.set_thumb(photo)
                proj.thumb_photo = photo
            elif _ph:
                card.set_thumb(_ph)

        count = len(projects)
        self._set_status(f"Найдено: {count} {'файл' if count == 1 else 'файла' if 2 <= count <= 4 else 'файлов'}")
        self._loading = False

    # ── Drag & Drop ───────────────────────────────────────────────────────────

    def _on_drop(self, event):
        import shutil
        added = 0
        for p_str in self.tk.splitlist(event.data):
            src = Path(p_str)
            if src.suffix.lower() == ".gcode":
                dst = PROJECTS_DIR / src.name
                if not dst.exists():
                    shutil.copy2(src, dst)
                    added += 1
        self._refresh()
        self._set_status(f"Добавлено: {added} файл(ов)")

    # ── Printer status ────────────────────────────────────────────────────────

    def _schedule_printer_check(self):
        threading.Thread(target=self._do_check_printer, daemon=True).start()

    def _do_check_printer(self):
        ip     = _CFG["printer_ip"]
        status = _query_printer_status(ip)      # M105 + M27 in one TCP session
        if status:
            t_cur  = status["hotend"][0]
            t_tgt  = status["hotend"][1]
            b_cur  = status["bed"][0]
            b_tgt  = status["bed"][1]
            prog   = status["progress"]
            
            # Get printer state (PRINTING, PAUSED, IDLE)
            state = get_printer_state(ip) or "IDLE"
            
            # Format temperature display with target
            temp_str = f"T:{t_cur:.0f}°/{t_tgt:.0f}°  B:{b_cur:.0f}°/{b_tgt:.0f}°"
            
            if prog and prog[1] > 0:
                pct = int(100 * prog[0] / prog[1])
                # Calculate remaining time estimate
                remaining_bytes = prog[1] - prog[0]
                speed_kbs = _CFG["upload_speed_kbs"]
                remaining_secs = remaining_bytes / (speed_kbs * 1000)
                remaining_mins = remaining_secs / 60
                
                # Format time display - only show if >= 1 minute
                if remaining_mins >= 1:
                    if remaining_mins >= 60:
                        time_str = f"{remaining_mins/60:.0f}ч"
                    else:
                        time_str = f"{remaining_mins:.0f}м"
                else:
                    time_str = ""  # Don't show time if less than 1 minute
                
                if time_str:
                    lbl = f"{temp_str}  ·  {pct}%  ·  ~{time_str}"
                else:
                    lbl = f"{temp_str}  ·  {pct}%"
                self.after(0, self._apply_printer_status, True, lbl, state)
            else:
                # Only show temperatures when not printing
                self.after(0, self._apply_printer_status,
                           True, temp_str, state)
        else:
            self.after(0, self._apply_printer_status, False, "Offline", "IDLE")

        if status and self._terminal_visible:
            ts = datetime.now().strftime("%H:%M:%S")
            t_cur, t_tgt = status["hotend"]
            b_cur, b_tgt = status["bed"]
            prog = status["progress"]
            line = f"[{ts}] ~  T:{t_cur:.0f}/{t_tgt:.0f}  B:{b_cur:.0f}/{b_tgt:.0f}"
            if prog and prog[1] > 0:
                line += f"  {int(100 * prog[0] / prog[1])}%"
            self.after(0, self._terminal.append, line, "status")
        elif not status and self._terminal_visible:
            ts = datetime.now().strftime("%H:%M:%S")
            self.after(0, self._terminal.append, f"[{ts}] ~  offline", "status")

        self.after(15_000, self._schedule_printer_check)

    def _apply_printer_status(self, online: bool, text: str, state: str):
        """Apply printer status to UI.

        Args:
            online: True if printer is reachable, False otherwise
            text: Status text to display (temperatures, progress, etc.)
            state: Printer state: 'PRINTING', 'PAUSED', or 'IDLE'
        """
        if online:
            self._dot.configure(fg=GREEN)
            self._dot_lbl.configure(text=text, fg=FG2)
        else:
            self._dot.configure(fg=RED)
            self._dot_lbl.configure(text="Offline", fg=FG_DIM)
        
        # Show/hide buttons based on printer state
        if state == "PRINTING":
            self._pause_btn.configure(text="⏸  Пауза", bg=BTN_N, fg=BTN_FG)
            self._pause_btn.pack(side=tk.RIGHT, padx=(4, 4), pady=10)
            self._stop_btn.pack(side=tk.RIGHT, padx=(4, 8), pady=10)
        elif state == "PAUSED":
            self._pause_btn.configure(text="▶  Продолжить", bg=GREEN, fg="#ffffff")
            self._pause_btn.pack(side=tk.RIGHT, padx=(4, 4), pady=10)
            self._stop_btn.pack(side=tk.RIGHT, padx=(4, 8), pady=10)
        else:  # IDLE or offline
            self._pause_btn.pack_forget()
            self._stop_btn.pack_forget()

    # ── Send flow ─────────────────────────────────────────────────────────────

    def _send_project(self, project: GcodeProject):
        if self._dot.cget("fg") == RED:
            if not messagebox.askyesno(
                    "Принтер недоступен",
                    f"Принтер {_CFG['printer_ip']} не отвечает.\n\nПопробовать отправить файл?",
                    parent=self):
                return

        dlg = SendDialog(self, project)
        if dlg.result is None:
            return

        cooling_secs, action = dlg.result
        if action == "save":
            threading.Thread(target=self._do_save,
                             args=(project, cooling_secs),
                             daemon=True).start()
        else:
            threading.Thread(target=self._do_send,
                             args=(project, cooling_secs),
                             daemon=True).start()

    def _do_save(self, project: GcodeProject, cooling_secs: int):
        try:
            self.after(0, self._set_status, "Читаю файл…")
            text = project.path.read_text(encoding="utf-8", errors="replace")
            self.after(0, self._set_status, "Обрабатываю G-code…")
            processed = process_gcode(text, cooling_secs)
            out = project.path.with_stem(project.path.stem + "_processed")
            out.write_text(processed, encoding="utf-8")
            subprocess.run(["open", "-R", str(out)])
            self.after(0, self._set_status, f"Сохранено: {out.name}")
        except Exception as e:
            msg = f"Ошибка: {e}"
            self.after(0, self._set_status, msg)
            self.after(0, messagebox.showerror, "Ошибка", msg)

    def _do_send(self, project: GcodeProject, cooling_secs: int):
        try:
            self.after(0, self._set_status, "Читаю файл…")
            text = project.path.read_text(encoding="utf-8", errors="replace")
            self.after(0, self._set_status, "Обрабатываю G-code…")
            processed = process_gcode(text, cooling_secs)
            content = processed.encode("utf-8")
            size_kb = len(content) / 1024

            _last = [-1]  # throttle: update only when % changes

            def _progress(sent, total):
                pct = int(100 * sent / total) if total else 0
                if pct != _last[0]:
                    _last[0] = pct
                    self.after(0, self._set_status,
                               f"Отправляю «{project.name}»  "
                               f"{size_kb:.0f} КБ  {pct}%…")

            self.after(0, self._set_status,
                       f"Отправляю «{project.name}»  {size_kb:.0f} КБ…")
            ok, msg = upload_gcode(project.name, content,
                                   progress_cb=_progress)
            append_history({
                "ts":      datetime.now().isoformat(timespec="seconds"),
                "file":    project.name,
                "cooling": cooling_secs,
                "success": ok,
            })
            self.after(0, self._set_status, msg)
            if ok:
                self.after(0, messagebox.showinfo, "Успех", msg)
            else:
                self.after(0, messagebox.showerror, "Ошибка отправки", msg)
        except Exception as e:
            msg = f"Ошибка: {e}"
            self.after(0, self._set_status, msg)
            self.after(0, messagebox.showerror, "Ошибка", msg)

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _set_status(self, text: str):
        self._status.configure(text=text)


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    app = App()
    app.mainloop()
