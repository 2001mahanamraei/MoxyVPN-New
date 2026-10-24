"""MoxyVPN - Firefox VPN client: SOCKS5 proxy, whole-system tunnel, usage tracking, tray."""
import ctypes
import http.server
import importlib
import json
import math
import os
import queue
import socket
import subprocess
import sys
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox

import pings
import settings_ui
import theme
import tunconf
import usage
import webbrowser
from theme import mix

try:                                  # Pillow draws the background / power button; optional at runtime
    import gfx
except Exception:                     # noqa: BLE001
    gfx = None

APP_NAME = "MoxyVPN"
HOST = "127.0.0.1"
APP_DIR = os.path.dirname(os.path.abspath(__file__))
ASSETS = os.path.join(APP_DIR, "assets")
TUN_DIR = os.path.join(os.path.dirname(APP_DIR), "tun")
MIHOMO = os.path.join(TUN_DIR, "mihomo.exe")
WINTUN = os.path.join(TUN_DIR, "wintun.dll")
TOKENS = os.path.join(os.path.expanduser("~"), ".foxyvpn-tokens.json")
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
NEW_CONSOLE = getattr(subprocess, "CREATE_NEW_CONSOLE", 0)
USAGE_REFRESH_SECONDS = 600
API_PORT = 47832

# ---- palette ---------------------------------------------------------------
BG = CARD = FIELD = BORDER = TEXT = MUTED = DIM = ACCENT = ACCENT2 = GREEN = RED = ""


def apply_palette(name):
    globals().update(theme.set_theme(name))


apply_palette("dark")
FONT = "Segoe UI"
VERSION = "1.4.0"
DEFAULT_SETTINGS = {
    "theme": "dark", "verify_exit": True, "dns_mode": "auto", "custom_dns_on": False, "custom_dns": "",
    "listen": "127.0.0.1", "http_on": False, "http_port": 8080, "edge": "", "upstream_on": False,
    "upstream": "", "api_proxy": "", "live_ping": True,
}

# (label, code). "rec" = Mozilla's "recommended" server: no --country flag is passed for it.
COUNTRIES = [
    ("Recommended", "rec"), ("Argentina", "ar"), ("Australia", "au"), ("Austria", "at"), ("Belgium", "be"),
    ("Brazil", "br"), ("Bulgaria", "bg"), ("Canada", "ca"), ("Chile", "cl"), ("Colombia", "co"),
    ("Denmark", "dk"), ("Finland", "fi"), ("France", "fr"), ("Germany", "de"), ("Ghana", "gh"),
    ("Ireland", "ie"), ("Italy", "it"), ("Japan", "jp"), ("Malaysia", "my"), ("Mexico", "mx"),
    ("Netherlands", "nl"), ("New Zealand", "nz"), ("Norway", "no"), ("Peru", "pe"),
    ("Philippines", "ph"), ("Portugal", "pt"), ("Singapore", "sg"), ("South Africa", "za"),
    ("South Korea", "kr"), ("Spain", "es"), ("Sweden", "se"), ("Thailand", "th"),
    ("United Kingdom", "gb"), ("United States", "us"),
]
W, H = 400, 758
LOGW = 360                              # width of the log side panel
HERO = (200, 176)                       # centre of the power button
# glass panels baked into the background image: connection card, usage card, ping pill
CARDS = [(20, 366, 380, 552, 16), (20, 568, 380, 728, 16), (112, 331, 288, 353, 11)]
PING_GOOD, PING_OK = 120, 250           # ms thresholds for green / amber


def ping_color(ms):
    if ms is None:
        return DIM
    return GREEN if ms <= PING_GOOD else (ACCENT2 if ms <= PING_OK else RED)


def country_name(code):
    for name, c in COUNTRIES:
        if c == (code or "").lower():
            return name
    return (code or "").upper() or "Recommended"


def rrect(c, x1, y1, x2, y2, r, **kw):
    pts = [x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r, x2, y2 - r, x2, y2, x2 - r, y2,
           x1 + r, y2, x1, y2, x1, y2 - r, x1, y1 + r, x1, y1]
    return c.create_polygon(pts, smooth=True, **kw)


def is_admin():
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def python_exe():
    exe = sys.executable
    if os.path.basename(exe).lower() == "pythonw.exe":
        alt = os.path.join(os.path.dirname(exe), "python.exe")
        if os.path.exists(alt):
            return alt
    return exe


def ensure_tray_deps():
    def ok():
        try:
            importlib.invalidate_caches()
            importlib.import_module("pystray")
            importlib.import_module("PIL.Image")
            return True
        except Exception:
            return False
    return ok()


class _ApiHandler(http.server.BaseHTTPRequestHandler):
    """Tiny local control API for the MoxyVPN Chrome extension (127.0.0.1 only)."""
    app = None

    def log_message(self, *args):
        pass

    def _host_ok(self):
        return self.headers.get("Host", "") in (f"127.0.0.1:{API_PORT}", f"localhost:{API_PORT}")

    def _origin(self):
        o = self.headers.get("Origin", "")
        return o if o.startswith("chrome-extension://") else ""

    def _send(self, code, obj=None):
        body = json.dumps(obj if obj is not None else {}).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        if self._origin():
            self.send_header("Access-Control-Allow-Origin", self._origin())
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        if not (self._host_ok() and self._origin()):
            return self._send(403, {"error": "forbidden"})
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", self._origin())
        self.send_header("Access-Control-Allow-Methods", "GET, POST")
        self.send_header("Access-Control-Allow-Headers", "X-Moxy-Client")
        self.send_header("Access-Control-Allow-Private-Network", "true")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        if not self._host_ok():
            return self._send(403, {"error": "forbidden"})
        if self.path.split("?")[0] == "/status":
            return self._send(200, self.app.api_status())
        self._send(404, {"error": "not found"})

    def do_POST(self):
        # Only the extension may control the app: browsers always send Origin on POST.
        if not (self._host_ok() and self._origin() and self.headers.get("X-Moxy-Client") == "1"):
            return self._send(403, {"error": "forbidden"})
        actions = {"/connect": self.app.start, "/disconnect": self.app.stop,
                   "/refresh": self.app.refresh_usage}
        fn = actions.get(self.path.split("?")[0])
        if not fn:
            return self._send(404, {"error": "not found"})
        self.app.ui(fn)
        self._send(200, {"ok": True})


HIDE_PROCS = {"system", "system idle process", "registry", "smss.exe", "csrss.exe", "wininit.exe",
              "services.exe", "lsass.exe", "svchost.exe", "fontdrvhost.exe", "dwm.exe",
              "conhost.exe", "winlogon.exe", "python.exe", "pythonw.exe", "mihomo.exe",
              "moxyvpn.exe", "tasklist.exe", "memory compression", "secure system"}


def running_processes():
    try:
        out = subprocess.run(["tasklist", "/fo", "csv", "/nh"], capture_output=True, text=True,
                             creationflags=NO_WINDOW, timeout=10).stdout
    except Exception:
        return []
    names = set()
    for line in out.splitlines():
        first = line.split('","')[0].strip('"').strip()
        if first and first.lower() not in HIDE_PROCS:
            names.add(first)
    return sorted(names, key=str.lower)


def _dark_button(parent, text, cmd, accent=False):
    return tk.Button(parent, text=text, command=cmd, relief="flat", bd=0, cursor="hand2",
                     bg=ACCENT if accent else BORDER, fg="#1B0F05" if accent else TEXT,
                     activebackground=ACCENT2 if accent else mix(BORDER, TEXT, 0.12),
                     activeforeground="#1B0F05" if accent else TEXT,
                     font=(FONT, 10, "bold" if accent else "normal"), padx=12, pady=6)


class ProcessPicker(tk.Toplevel):
    """Pick one or more running programs."""

    def __init__(self, parent, on_add):
        super().__init__(parent)
        self.on_add = on_add
        self.title("Running apps")
        self.configure(bg=BG)
        self.resizable(False, False)
        self.transient(parent)
        self.grab_set()
        self.all = running_processes()
        tk.Label(self, text="Pick the apps to add", bg=BG, fg=TEXT, font=(FONT, 12, "bold")).pack(
            anchor="w", padx=18, pady=(16, 6))
        self.q = tk.StringVar()
        e = tk.Entry(self, textvariable=self.q, bg=CARD, fg=TEXT, insertbackground=TEXT, relief="flat",
                     font=(FONT, 10), highlightthickness=1, highlightbackground=BORDER,
                     highlightcolor=ACCENT)
        e.pack(fill="x", padx=18, ipady=5)
        e.focus_set()
        self.q.trace_add("write", lambda *a: self._fill())
        box = tk.Frame(self, bg=BG)
        box.pack(fill="both", padx=18, pady=8)
        self.lb = tk.Listbox(box, selectmode="extended", height=12, width=34, bg=CARD, fg=TEXT,
                             selectbackground=ACCENT, selectforeground="#1B0F05", relief="flat",
                             highlightthickness=0, font=(FONT, 10), activestyle="none")
        sb = tk.Scrollbar(box, command=self.lb.yview)
        self.lb.config(yscrollcommand=sb.set)
        self.lb.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        row = tk.Frame(self, bg=BG)
        row.pack(fill="x", padx=18, pady=(0, 16))
        _dark_button(row, "Add selected", self._add, True).pack(side="right")
        _dark_button(row, "Cancel", self.destroy).pack(side="right", padx=8)
        self._fill()

    def _fill(self):
        t = self.q.get().strip().lower()
        self.lb.delete(0, "end")
        for n in self.all:
            if t in n.lower():
                self.lb.insert("end", n)

    def _add(self):
        self.on_add([self.lb.get(i) for i in self.lb.curselection()])
        self.destroy()


class SplitDialog(tk.Toplevel):
    MODES = (("off", "Off"), ("exclude", "Bypass selected apps"), ("include", "Only selected apps"))
    HELP = {
        "off": "Everything goes through the VPN.",
        "exclude": "Selected apps skip the VPN and use your normal connection. Everything else is protected.",
        "include": "Only the selected apps use the VPN. Everything else uses your normal connection.",
    }

    def __init__(self, app):
        super().__init__(app)
        self.app = app
        self.title("Split tunneling")
        self.configure(bg=BG)
        self.resizable(False, False)
        self.transient(app)
        self.grab_set()
        self.mode = tk.StringVar(value=app.split["mode"])
        self.apps = list(app.split["apps"])

        tk.Label(self, text="Split tunneling", bg=BG, fg=TEXT, font=(FONT, 15, "bold")).pack(
            anchor="w", padx=20, pady=(18, 2))
        tk.Label(self, text="Choose which programs use the VPN.", bg=BG, fg=MUTED,
                 font=(FONT, 10)).pack(anchor="w", padx=20)

        seg = tk.Frame(self, bg=CARD)
        seg.pack(fill="x", padx=20, pady=(14, 6))
        self.radios = {}
        for val, label in self.MODES:
            rb = tk.Radiobutton(seg, text=label, value=val, variable=self.mode, indicatoron=False,
                                command=self._mode_changed, bg=CARD, fg=MUTED, selectcolor=ACCENT,
                                activebackground=BORDER, activeforeground=TEXT, relief="flat", bd=0,
                                font=(FONT, 9, "bold"), padx=6, pady=7, cursor="hand2")
            rb.pack(side="left", fill="x", expand=True, padx=2, pady=2)
            self.radios[val] = rb
        self.help = tk.Label(self, text="", bg=BG, fg=MUTED, font=(FONT, 9), wraplength=340,
                             justify="left")
        self.help.pack(anchor="w", padx=20, pady=(0, 8))

        box = tk.Frame(self, bg=BG)
        box.pack(fill="both", padx=20)
        self.lb = tk.Listbox(box, selectmode="extended", height=9, width=38, bg=CARD, fg=TEXT,
                             selectbackground=ACCENT, selectforeground="#1B0F05", relief="flat",
                             highlightthickness=0, font=(FONT, 10), activestyle="none")
        sb = tk.Scrollbar(box, command=self.lb.yview)
        self.lb.config(yscrollcommand=sb.set)
        self.lb.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")

        row = tk.Frame(self, bg=BG)
        row.pack(fill="x", padx=20, pady=8)
        _dark_button(row, "Add running app", self._add_running).pack(side="left")
        _dark_button(row, "Browse .exe", self._browse).pack(side="left", padx=6)
        _dark_button(row, "Remove", self._remove).pack(side="right")

        self.note = tk.Label(self, text="", bg=BG, fg=ACCENT2, font=(FONT, 9), wraplength=340,
                             justify="left")
        self.note.pack(anchor="w", padx=20)
        if not app.use_tun.get():
            self.note.config(text="Turn on \u201cTunnel whole system\u201d to use split tunneling.")

        foot = tk.Frame(self, bg=BG)
        foot.pack(fill="x", padx=20, pady=16)
        _dark_button(foot, "Save", self._save, True).pack(side="right")
        _dark_button(foot, "Cancel", self.destroy).pack(side="right", padx=8)
        self._refresh()
        self._mode_changed()

    def _refresh(self):
        self.lb.delete(0, "end")
        for a in self.apps:
            self.lb.insert("end", a)

    def _mode_changed(self):
        self.help.config(text=self.HELP[self.mode.get()])
        for val, rb in self.radios.items():
            sel = val == self.mode.get()
            rb.config(fg="#1B0F05" if sel else MUTED, activeforeground="#1B0F05" if sel else TEXT)

    def _add_names(self, names):
        have = {a.lower() for a in self.apps}
        for n in names:
            n = n.strip()
            if n and "," not in n and n.lower() not in have:
                self.apps.append(n)
                have.add(n.lower())
        if self.mode.get() == "off" and self.apps:
            self.mode.set("exclude")
            self._mode_changed()
        self._refresh()

    def _add_running(self):
        ProcessPicker(self, self._add_names)

    def _browse(self):
        path = filedialog.askopenfilename(parent=self, title="Choose a program",
                                          filetypes=[("Programs", "*.exe"), ("All files", "*.*")])
        if path:
            self._add_names([os.path.basename(path)])

    def _remove(self):
        for i in reversed(self.lb.curselection()):
            del self.apps[i]
        self._refresh()

    def _save(self):
        self.app.set_split(self.mode.get(), self.apps)
        self.destroy()


class Art:
    """Pictures rendered by Pillow for one theme: a background per connection state, the power-button
    animation frames, header icons and switches. Rendering happens in a thread; PhotoImages are built
    lazily on the Tk thread."""

    def __init__(self, master, pal):
        self.master, self.pal = master, dict(pal)
        self.hero = gfx.HeroArt(self.pal)
        self.png, self.photos, self.stop = {}, {}, False

    def _render(self, key):
        if key not in self.png:
            if key[0] == "bg":
                img = gfx.background(W, H, self.pal, key[1], HERO, CARDS)
            else:
                img = self.hero.frame(key[1], key[2])
            self.png[key] = gfx.b64png(img).decode("ascii")
        return self.png[key]

    def render_rest(self):
        """Thread body: everything that was not rendered up front."""
        keys = [("bg", m) for m in ("connecting", "on", "off")]
        keys += [("hero", st, i) for st in ("connecting", "on") for i in range(gfx.FRAMES[st])]
        for key in keys:
            if self.stop:
                return
            try:
                self._render(key)
            except Exception:  # noqa: BLE001
                return

    def photo(self, key, render=False):
        ph = self.photos.get(key)
        if ph is None:
            data = self._render(key) if render else self.png.get(key)
            if data is None:
                return None
            ph = self.photos[key] = tk.PhotoImage(master=self.master, data=data)
        return ph

    def icon(self, name, color):
        key = ("icon", name, color)
        if key not in self.photos:
            self.photos[key] = tk.PhotoImage(master=self.master,
                                             data=gfx.b64png(gfx.icon(name, color)).decode("ascii"))
        return self.photos[key]

    def switch(self, on, busy):
        key = ("switch", bool(on), bool(busy))
        if key not in self.photos:
            self.photos[key] = tk.PhotoImage(master=self.master,
                                             data=gfx.b64png(gfx.switch(self.pal, on, busy)).decode("ascii"))
        return self.photos[key]


class CountryPicker(tk.Toplevel):
    """Drop-down list of countries with a live ping next to each one."""
    ROW, LW, LH = 36, 340, 352

    def __init__(self, app):
        super().__init__(app, bg=BORDER)
        self.app = app
        self.overrideredirect(True)
        self.sort_ping = False
        self.hover = None
        self.query = tk.StringVar()
        body = tk.Frame(self, bg=CARD)
        body.pack(padx=1, pady=1)
        top = tk.Frame(body, bg=CARD)
        top.pack(fill="x", padx=12, pady=(12, 6))
        self.entry = tk.Entry(top, textvariable=self.query, bg=FIELD, fg=TEXT, insertbackground=TEXT,
                              relief="flat", font=(FONT, 10), highlightthickness=1,
                              highlightbackground=BORDER, highlightcolor=ACCENT)
        self.entry.pack(side="left", fill="x", expand=True, ipady=5)
        self.ref = self._pill(top, "\u21bb Ping", lambda: app.run_pings(force=True))
        self.ref.pack(side="right", padx=(6, 0))
        self.srt = self._pill(top, "A\u2013Z", self._toggle_sort)
        self.srt.pack(side="right", padx=(6, 0))
        self.cv = tk.Canvas(body, width=self.LW, height=self.LH, bg=CARD, highlightthickness=0,
                            yscrollincrement=12)
        self.cv.pack()
        self.foot = tk.Label(body, text="", bg=CARD, fg=DIM, font=(FONT, 8), anchor="w")
        self.foot.pack(fill="x", padx=16, pady=(2, 8))
        self.query.trace_add("write", lambda *a: self.draw())
        self.cv.bind("<Motion>", self._motion)
        self.cv.bind("<Leave>", lambda e: self._set_hover(None))
        self.cv.bind("<Button-1>", self._click)
        self.cv.bind("<MouseWheel>", lambda e: self.cv.yview_scroll(int(-e.delta / 120) * 2, "units"))
        self.bind("<Escape>", lambda e: self.close())
        self.bind("<Return>", self._enter)
        self.bind("<FocusOut>", lambda e: self.after(90, self._check_focus))
        self.update_idletasks()
        w, h = self.winfo_reqwidth(), self.winfo_reqheight()
        x = app.winfo_rootx() + 362 - w
        y = app.winfo_rooty() + 452
        x = max(4, min(x, self.winfo_screenwidth() - w - 4))
        y = max(4, min(y, self.winfo_screenheight() - h - 48))
        self.geometry(f"+{x}+{y}")
        self.draw()
        self.focus_force()
        self.entry.focus_set()

    def _pill(self, parent, text, cmd):
        b = tk.Label(parent, text=text, bg=BORDER, fg=TEXT, font=(FONT, 9, "bold"), padx=9, pady=5,
                     cursor="hand2")
        b.bind("<Button-1>", lambda e: cmd())
        return b

    def close(self):
        if self.app.picker is self:
            self.app.picker = None
            self.app.picker_closed_at = time.time()
        self.destroy()

    def _check_focus(self):
        try:
            if self.focus_get() is None:
                self.close()
        except (tk.TclError, KeyError):
            self.close()

    def _toggle_sort(self):
        self.sort_ping = not self.sort_ping
        self.srt.config(text="Ping \u2191" if self.sort_ping else "A\u2013Z")
        self.draw()

    def rows(self):
        q = self.query.get().strip().lower()
        rows = [(n, c) for n, c in COUNTRIES if not q or q in n.lower() or q == c]
        if self.sort_ping:
            rows.sort(key=lambda r: (r[1] != "rec", self.app.ping_for(r[1]) is None,
                                     self.app.ping_for(r[1]) or 0))
        return rows

    def draw(self):
        cv, app, R = self.cv, self.app, self.ROW
        y0 = cv.yview()[0]
        cv.delete("all")
        rows = self._rows_cache = self.rows()
        cur = (app.country_code or "rec").lower()
        for i, (name, code) in enumerate(rows):
            y = i * R
            sel = code == cur
            if sel or i == self.hover:
                rrect(cv, 8, y + 2, self.LW - 8, y + R - 2, 9,
                      fill=mix(CARD, ACCENT, .17) if sel else mix(CARD, TEXT, .07), outline="")
            if sel:
                cv.create_rectangle(8, y + 9, 11, y + R - 9, fill=ACCENT, outline="")
            cv.create_text(22, y + R / 2, text=name, anchor="w", fill=TEXT,
                           font=(FONT, 11, "bold" if sel else "normal"))
            if code != "rec":
                cv.create_text(150 if len(name) < 12 else 168, y + R / 2, text=code.upper(), anchor="w",
                               fill=DIM, font=(FONT, 8))
            ms, wait = app.ping_for(code), app.ping_waiting(code)
            txt = "\u2026" if wait else ("\u2014" if ms is None else f"{ms} ms")
            col = DIM if (wait or ms is None) else ping_color(ms)
            cv.create_text(self.LW - 20, y + R / 2, text=txt, anchor="e", fill=col,
                           font=("Consolas", 10, "bold"))
            bx2 = self.LW - 82
            cv.create_rectangle(bx2 - 40, y + R / 2 - 2, bx2, y + R / 2 + 2, fill=BORDER, outline="")
            if ms is not None and not wait:
                frac = min(max(1 - ms / 400, 0.1), 1.0)
                cv.create_rectangle(bx2 - 40, y + R / 2 - 2, bx2 - 40 + 40 * frac, y + R / 2 + 2,
                                    fill=col, outline="")
        cv.config(scrollregion=(0, 0, self.LW, max(len(rows) * R, self.LH)))
        cv.yview_moveto(y0)
        self.foot.config(text=app.ping_status())

    def _row_at(self, e):
        i = int(self.cv.canvasy(e.y) // self.ROW)
        return i if 0 <= i < len(getattr(self, "_rows_cache", [])) else None

    def _set_hover(self, i):
        if i != self.hover:
            self.hover = i
            self.draw()

    def _motion(self, e):
        self._set_hover(self._row_at(e))

    def _pick(self, i):
        name, code = self._rows_cache[i]
        self.close()
        self.app.pick_country(name, code)

    def _click(self, e):
        i = self._row_at(e)
        if i is not None:
            self._pick(i)

    def _enter(self, _e=None):
        if getattr(self, "_rows_cache", None):
            self._pick(0)


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.state_data = usage.load_state()
        self.settings = {**DEFAULT_SETTINGS, **(self.state_data.get("settings") or {})}
        if not self.settings["api_proxy"]:
            self.settings["api_proxy"] = self.state_data.get("api_proxy", "")
        apply_palette(self.settings["theme"])
        self.VERSION = VERSION
        self.settings_page = None
        self.settings_open = False
        self.log_lines = []
        self._pending_email = None
        self.title(APP_NAME)
        self.configure(bg=BG)
        self.resizable(False, False)
        try:
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("MoxyVPN.App")
        except Exception:
            pass
        try:
            self._icon_img = tk.PhotoImage(file=os.path.join(ASSETS, "moxy_64.png"))
            self.iconphoto(True, self._icon_img)
        except tk.TclError:
            pass

        self.proc = None
        self.tun = None
        self.q = queue.Queue()
        self.vstate = "off"
        self.spin_angle = 0
        self.tray = None
        self.tray_imgs = None
        self.notified = False
        self.fetching = False
        self.last_fetch = 0
        self.log_visible = False
        self.login_ctx = None
        self.login_busy = False
        self.need_login = False
        self.api_port_cache = 1080
        self.api = None
        self.picker, self.picker_closed_at = None, 0
        self.pings, self.servers, self.ping_wait = {}, None, set()
        self.ping_busy, self.ping_total, self.ping_time, self.ping_err = False, 0, 0, ""
        self.live_ms, self.live_busy = None, False
        self.art, self.hits, self._log_token = None, [], 0

        self.port_var = tk.StringVar(value=str(self.state_data.get("port", "1080")))
        self.use_tun = tk.BooleanVar(value=("--tun" in sys.argv) or bool(self.state_data.get("tun")))
        self.country_code = (self.state_data.get("country", "us") or "rec").lower()
        sp = self.state_data.get("split") or {}
        self.split = {"mode": sp.get("mode", "off"), "apps": list(sp.get("apps", []))}
        self.api_proxy_var = tk.StringVar(value=self.settings["api_proxy"])
        try:
            self.api_port_cache = int(self.port_var.get())
        except ValueError:
            pass

        self._build()
        self._build_login()
        self._start_api()
        self.protocol("WM_DELETE_WINDOW", self.on_close)
        self.set_state("off")
        self.render_usage()
        self.update_split_chip()
        self.after(100, self._tick)
        self.after(40, self._anim)
        self.after(1500, self.run_pings)
        self.after(6000, self._live_loop)
        threading.Thread(target=self._tray_thread, daemon=True).start()
        if os.path.exists(TOKENS):
            self.after(1500, self.refresh_usage)
            if "--auto" in sys.argv:
                self.after(500, self.start)
        else:
            self.show_login()

    # ------------------------------------------------------------------ UI
    def _pal(self):
        return dict(BG=BG, CARD=CARD, FIELD=FIELD, BORDER=BORDER, TEXT=TEXT, MUTED=MUTED, DIM=DIM,
                    ACCENT=ACCENT, ACCENT2=ACCENT2, GREEN=GREEN, RED=RED)

    # The canvas has no per-item hit-testing for images / transparent areas, so clicks are routed by
    # rectangles (or one circle for the power button).
    def _hit(self, box, fn, circle=False):
        self.hits.append((box, fn, circle))

    def _hit_at(self, x, y):
        for (a, b, c2, d), fn, circle in reversed(self.hits):
            if circle:
                if (x - a) ** 2 + (y - b) ** 2 <= c2 ** 2:
                    return fn
            elif a <= x <= c2 and b <= y <= d:
                return fn
        return None

    def _on_click(self, e):
        fn = self._hit_at(e.x, e.y)
        if fn:
            fn()

    def _on_motion(self, e):
        self.c.config(cursor="hand2" if self._hit_at(e.x, e.y) else "")

    def _icon_item(self, x, y, name, color, fallback):
        if self.art:
            return self.c.create_image(x, y, image=self.art.icon(name, color))
        return self.c.create_text(x, y, text=fallback, fill=color, font=("Segoe UI Symbol", 15))

    def _build(self):
        self.hits = []
        c = self.c = tk.Canvas(self, width=W, height=H, bg=BG, highlightthickness=0)
        c.pack(side="left")
        c.bind("<Button-1>", self._on_click)
        c.bind("<Motion>", self._on_motion)
        c.bind("<Leave>", lambda e: c.config(cursor=""))
        self._bg_mode = self._hero_shown = None
        self._hero_i = 0
        cx, cy = HERO

        # background picture + power button picture (Pillow); plain canvas drawing if Pillow is missing
        self.art = None
        self.bg_item = c.create_image(0, 0, anchor="nw")
        self.hero_item = c.create_image(cx, cy)
        try:
            if not gfx:
                raise RuntimeError("Pillow not available")
            self.art = Art(self, self._pal())
            c.itemconfig(self.bg_item, image=self.art.photo(("bg", self.vstate), render=True))
            self._bg_mode = self.vstate
            c.itemconfig(self.hero_item, image=self.art.photo(("hero", self.vstate, 0), render=True))
            self._hero_shown = (self.vstate, 0)
            threading.Thread(target=self.art.render_rest, daemon=True).start()
        except Exception:  # noqa: BLE001
            self.art = None
            for x1, y1, x2, y2, r in CARDS:
                rrect(c, x1, y1, x2, y2, r, fill=CARD, outline=BORDER)
            self._build_hero_fallback()
        self._hit((cx, cy, 74, 0), self.toggle, circle=True)

        # header
        try:
            self._logo = tk.PhotoImage(file=os.path.join(ASSETS, "moxy_40.png"))
            c.create_image(22, 38, image=self._logo, anchor="w")
        except tk.TclError:
            pass
        c.create_text(72, 30, text="MoxyVPN", anchor="w", fill=TEXT, font=(FONT, 17, "bold"))
        self.hdr_sub = c.create_text(73, 52, text=self._email_text(), anchor="w", fill=MUTED,
                                     font=(FONT, 9))
        dark = theme.resolve(self.settings["theme"]) == "dark"
        self.icon_log = self._icon_item(268, 38, "log", ACCENT2 if self.log_visible else TEXT, "≡")
        self._icon_item(318, 38, "sun" if dark else "moon", ACCENT2 if dark else TEXT,
                        "☀" if dark else "☾")
        self._icon_item(367, 38, "menu", TEXT, "☰")
        self._hit((246, 16, 290, 60), self.toggle_log)
        self._hit((296, 16, 340, 60), self.toggle_theme)
        self._hit((346, 16, 390, 60), self.open_settings)

        self.s_title = c.create_text(200, 296, text="", fill=TEXT, font=(FONT, 17, "bold"))
        self.s_sub = c.create_text(200, 318, text="", fill=MUTED, font=(FONT, 10))
        self.pill_dot = c.create_oval(0, 0, 0, 0, fill=DIM, outline="")
        self.pill_txt = c.create_text(207, 342, text="", fill=MUTED, font=(FONT, 9, "bold"))
        self._hit((112, 331, 288, 353), self.pill_click)

        # connection card (glass panel is part of the background picture)
        for y in (413, 459, 505):
            c.create_line(38, y, 362, y, fill=BORDER)
        c.create_text(38, 389, text="SOCKS5 proxy", anchor="w", fill=MUTED, font=(FONT, 10))
        self.addr_item = c.create_text(150, 389, text=self.listen_host() + " :", anchor="w", fill=TEXT,
                                       font=("Consolas", 11))
        self.port_entry = tk.Entry(self, textvariable=self.port_var, width=6, justify="center",
                                   bg=FIELD, fg=TEXT, insertbackground=TEXT, relief="flat",
                                   font=("Consolas", 11), highlightthickness=1,
                                   highlightbackground=BORDER, highlightcolor=ACCENT,
                                   disabledbackground=FIELD, disabledforeground=DIM)
        c.create_window(280, 389, window=self.port_entry, width=50, height=26)
        rrect(c, 322, 377, 362, 401, 8, fill=BORDER)
        c.create_text(342, 389, text="Copy", fill=TEXT, font=(FONT, 9))
        self._hit((322, 377, 362, 401), self.copy_addr)

        c.create_text(38, 436, text="Country", anchor="w", fill=MUTED, font=(FONT, 10))
        rrect(c, 176, 424, 362, 448, 12, fill=BORDER)
        self.chip_name = c.create_text(190, 436, text="", anchor="w", fill=TEXT, font=(FONT, 10))
        self.chip_ping = c.create_text(334, 436, text="", anchor="e", fill=DIM, font=("Consolas", 9, "bold"))
        c.create_text(351, 437, text="▾", fill=MUTED, font=(FONT, 10))
        self._hit((176, 424, 362, 448), self.open_country)

        c.create_text(38, 482, text="Tunnel whole system", anchor="w", fill=MUTED, font=(FONT, 10))
        self.t_img = c.create_image(338, 482) if self.art else None
        if not self.art:
            self.t_track = rrect(c, 318, 472, 362, 492, 10, fill=BORDER)
            self.t_knob = c.create_oval(320, 474, 340, 490, fill=TEXT, outline="")
        self.draw_toggle()
        self._hit((312, 468, 364, 496), self.flip_tun)

        c.create_text(38, 528, text="Split tunneling", anchor="w", fill=MUTED, font=(FONT, 10))
        rrect(c, 232, 516, 362, 540, 8, fill=BORDER)
        self.split_txt = c.create_text(297, 528, text="", fill=TEXT, font=(FONT, 9))
        self._hit((232, 516, 362, 540), self.open_split)

        # usage card
        c.create_text(38, 590, text="DATA USAGE", anchor="w", fill=MUTED, font=(FONT, 8, "bold"))
        c.create_text(362, 590, text="↻  Refresh", anchor="e", fill=ACCENT2, font=(FONT, 9))
        self._hit((286, 578, 364, 602), lambda: self.refresh_usage(True))
        self.u_used = c.create_text(38, 632, text="-", anchor="w", fill=TEXT, font=(FONT, 28, "bold"))
        self.u_of = c.create_text(362, 642, text="", anchor="e", fill=MUTED, font=(FONT, 11))
        rrect(c, 38, 668, 362, 678, 5, fill=BORDER)
        self.u_bar = None
        self.u_line = c.create_text(38, 698, text="", anchor="w", fill=MUTED, font=(FONT, 9))
        self.u_upd = c.create_text(38, 716, text="", anchor="w", fill=DIM, font=(FONT, 8))

        self._build_side()
        self.update_country_chip()

    def _build_hero_fallback(self):
        """Plain canvas power button, used only when Pillow cannot be imported."""
        c = self.c
        cx, cy, R = HERO[0], HERO[1], 62
        self.glow = [c.create_oval(cx - R - i * 8, cy - R - i * 8, cx + R + i * 8, cy + R + i * 8,
                                   fill=BG, outline="") for i in range(5, 0, -1)]
        self.ring = c.create_oval(cx - R, cy - R, cx + R, cy + R, fill=CARD, outline=BORDER, width=5)
        self.spin = c.create_arc(cx - R, cy - R, cx + R, cy + R, start=0, extent=80, style="arc",
                                 width=5, outline=ACCENT, state="hidden")
        self.p_arc = c.create_arc(cx - 22, cy - 18, cx + 22, cy + 26, start=125, extent=290,
                                  style="arc", width=5, outline=DIM)
        self.p_line = c.create_line(cx, cy - 28, cx, cy - 4, width=5, fill=DIM, capstyle="round")

    # ---- log side panel
    def _build_side(self):
        bgc = mix(BG, CARD, .55)
        f = self.side = tk.Frame(self, bg=bgc, width=LOGW, height=H)
        f.pack_propagate(False)
        tk.Frame(f, bg=BORDER, width=1).pack(side="left", fill="y")
        inner = tk.Frame(f, bg=bgc)
        inner.pack(side="left", fill="both", expand=True)
        head = tk.Frame(inner, bg=bgc)
        head.pack(fill="x", padx=16, pady=(20, 0))
        tk.Label(head, text="Activity log", bg=bgc, fg=TEXT, font=(FONT, 14, "bold")).pack(side="left")
        if self.art:
            close = tk.Label(head, image=self.art.icon("close", MUTED), bg=bgc, cursor="hand2")
        else:
            close = tk.Label(head, text="✕", bg=bgc, fg=MUTED, font=(FONT, 12), cursor="hand2")
        close.pack(side="right")
        close.bind("<Button-1>", lambda e: self.toggle_log())
        for text, fn in (("Clear", self.clear_log), ("Copy", self.copy_log)):
            b = tk.Label(head, text=text, bg=BORDER, fg=TEXT, font=(FONT, 9), padx=9, pady=3, cursor="hand2")
            b.pack(side="right", padx=(0, 8))
            b.bind("<Button-1>", lambda e, fn=fn: fn())
        tk.Label(inner, text="Connection steps, errors and tunnel output", bg=bgc, fg=DIM,
                 font=(FONT, 9)).pack(anchor="w", padx=16, pady=(2, 0))
        box = tk.Frame(inner, bg=CARD, highlightthickness=1, highlightbackground=BORDER)
        box.pack(fill="both", expand=True, padx=16, pady=(12, 18))
        self.log = tk.Text(box, bg=CARD, fg=MUTED, relief="flat", font=("Consolas", 9), state="disabled",
                           highlightthickness=0, wrap="word", padx=10, pady=8, spacing1=2, width=10,
                           cursor="arrow", insertwidth=0)
        sb = tk.Scrollbar(box, command=self.log.yview, width=11)
        self.log.config(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.log.pack(side="left", fill="both", expand=True)
        self.log.tag_config("ts", foreground=DIM)
        self.log.tag_config("err", foreground=RED)
        self.log.tag_config("ok", foreground=GREEN)
        self.log.tag_config("tun", foreground=ACCENT2)
        self.log.tag_config("dim", foreground=DIM)
        self.log.tag_config("txt", foreground=MUTED)
        self._log_replay()

    def _update_log_icon(self):
        col = ACCENT2 if self.log_visible else TEXT
        if self.art:
            self.c.itemconfig(self.icon_log, image=self.art.icon("log", col))
        else:
            self.c.itemconfig(self.icon_log, fill=col)

    def toggle_log(self):
        self.log_visible = not self.log_visible
        self._update_log_icon()
        self._log_token += 1
        token = self._log_token
        target = W + LOGW if self.log_visible else W
        if not self.winfo_viewable():                # hidden in the tray: no animation needed
            if self.log_visible:
                self.side.pack(side="left", fill="y")
            else:
                self.side.pack_forget()
            self.geometry("")
            return
        cur = max(self.winfo_width(), W)
        self.geometry(f"{cur}x{H}")                  # freeze the size so the width can be animated
        if self.log_visible:
            self.side.pack(side="left", fill="y")
        steps = 9

        def step(i=1):
            if token != self._log_token:
                return
            t = i / steps
            self.geometry(f"{int(cur + (target - cur) * (1 - (1 - t) ** 3))}x{H}")
            if i < steps:
                self.after(14, lambda: step(i + 1))
            else:
                if not self.log_visible:
                    self.side.pack_forget()
                self.geometry("")
                self._keep_on_screen()
        step()

    def _keep_on_screen(self):
        self.update_idletasks()
        over = self.winfo_x() + self.winfo_reqwidth() - self.winfo_screenwidth()
        if over > 0:
            self.geometry(f"+{max(0, self.winfo_x() - over - 8)}+{self.winfo_y()}")

    def show_log(self):
        if not self.log_visible:
            self.toggle_log()

    @staticmethod
    def _log_tag(text):
        t = text.lower()
        if any(k in t for k in ("error", "fail", "refused", "denied", "exception", "traceback", "expired",
                                "unavailable", "could not")):
            return "err"
        if t.startswith("[tun]"):
            return "tun"
        if any(k in t for k in ("listening", "connected", "applied", "protected")):
            return "ok"
        if t.startswith("["):
            return "dim"
        return "txt"

    def _log_write(self, ts, text):
        self.log.config(state="normal")
        self.log.insert("end", ts + "  ", "ts")
        self.log.insert("end", text + "\n", self._log_tag(text))
        if int(self.log.index("end-1c").split(".")[0]) > 600:
            self.log.delete("1.0", "100.0")
        self.log.see("end")
        self.log.config(state="disabled")

    def _log(self, text):
        ts = time.strftime("%H:%M:%S")
        self.log_lines.append((ts, text))
        del self.log_lines[:-400]
        self._log_write(ts, text)

    def _log_replay(self):
        for ts, text in self.log_lines:
            self._log_write(ts, text)

    def clear_log(self):
        self.log_lines.clear()
        self.log.config(state="normal")
        self.log.delete("1.0", "end")
        self.log.config(state="disabled")

    def copy_log(self):
        self.clipboard_clear()
        self.clipboard_append("\n".join(f"{ts}  {t}" for ts, t in self.log_lines))

    # ---- toggles / small actions
    def draw_toggle(self):
        on = self.use_tun.get()
        busy = self.proc is not None
        if self.art:
            self.c.itemconfig(self.t_img, image=self.art.switch(on, busy))
            return
        col = (ACCENT if on else BORDER)
        if busy:
            col = mix(col, BG, 0.45)
        self.c.itemconfig(self.t_track, fill=col)
        x = 342 if on else 320
        self.c.coords(self.t_knob, x, 474, x + 20, 490)
        self.c.itemconfig(self.t_knob, fill="#FFFFFF" if not busy else mix("#FFFFFF", BG, 0.45))

    def flip_tun(self):
        if self.proc:
            return
        self.use_tun.set(not self.use_tun.get())
        self.draw_toggle()

    def open_country(self):
        if self.picker:
            self.picker.close()
            return
        if time.time() - self.picker_closed_at < 0.25:      # the click that just closed it
            return
        self.picker = CountryPicker(self)
        if not self.ping_busy and time.time() - self.ping_time > 300:
            self.run_pings()

    def pick_country(self, name, code):
        changed = code != (self.country_code or "rec")
        self.country_code = code
        self.state_data["country"] = code
        usage.save_state(self.state_data)
        self.update_country_chip()
        self.update_pill()
        if changed and self.proc:
            self._log(f"[country] switching to {name}")
            self.apply_changes("core")

    def copy_addr(self):
        self.clipboard_clear()
        self.clipboard_append(f"{self.listen_display()}:{self.port_var.get().strip()}")
        self.c.itemconfig(self.s_sub, text="Address copied")
        self.after(1200, self._refresh_sub)

    # ------------------------------------------------------------------ ping
    def ping_for(self, code):
        code = (code or "").upper()
        ms = self.pings.get(code)
        if ms is None and code == "REC":                  # no "REC" entry: best of all countries
            vals = [v for v in self.pings.values() if v is not None]
            return min(vals) if vals else None
        return ms

    def ping_waiting(self, code):
        if not self.ping_busy:
            return False
        code = (code or "").upper()
        if self.servers is None or code in self.ping_wait:
            return True
        return code == "REC" and "REC" not in self.servers and bool(self.ping_wait)

    def ping_status(self):
        if self.ping_busy:
            if self.servers is None:
                return "Loading the server list…"
            return f"Measuring… {self.ping_total - len(self.ping_wait)}/{self.ping_total}"
        if self.ping_err and not self.pings:
            return self.ping_err
        if self.ping_time:
            return f"Latency to Mozilla's servers  •  {usage.fmt_ago(self.ping_time)}"
        return ""

    def run_pings(self, force=False):
        """Measure TCP latency to every country's servers (works whether or not the VPN is on)."""
        if self.ping_busy:
            return
        self.ping_busy, self.ping_err = True, ""
        proxies = []
        if self.settings["api_proxy"].strip():
            proxies.append(self.settings["api_proxy"].strip())
        if self.vstate == "on":
            proxies.append(f"socks5://127.0.0.1:{self.api_port_cache}")

        def work():
            try:
                servers, src = pings.load_servers(proxies, force=force)
            except Exception as exc:  # noqa: BLE001
                self.q.put(("ping_err", str(exc)))
                return
            self.q.put(("servers", servers, src))
            pings.ping_all(servers, lambda code, ms: self.q.put(("ping", code, ms)))
            self.q.put(("ping_done",))
        threading.Thread(target=work, daemon=True).start()
        self._ping_changed()

    def _ping_changed(self):
        self.update_country_chip()
        self.update_pill()
        if self.picker:
            try:
                self.picker.draw()
            except tk.TclError:
                self.picker = None

    def live_measure(self):
        """Latency through the running VPN (local SOCKS5 -> Mozilla edge -> internet)."""
        if self.live_busy or self.vstate != "on":
            return
        self.live_busy = True
        host, port = "127.0.0.1", self.api_port_cache

        def work():
            self.q.put(("live", pings.tunnel_ping(host, port)))
        threading.Thread(target=work, daemon=True).start()

    def _live_loop(self):
        if self.vstate == "on" and self.settings["live_ping"]:
            self.live_measure()
        self.after(5000, self._live_loop)

    def pill_click(self):
        if self.vstate == "on":
            self.live_measure()
        elif self.vstate == "off":
            self.run_pings()

    def update_country_chip(self):
        code = (self.country_code or "rec").lower()
        ms, wait = self.ping_for(code), self.ping_waiting(code)
        self.c.itemconfig(self.chip_name, text=country_name(code))
        self.c.itemconfig(self.chip_ping, text="…" if wait else ("" if ms is None else f"{ms} ms"),
                          fill=DIM if wait else ping_color(ms))

    def update_pill(self):
        st = self.vstate
        if st == "on":
            ms = self.live_ms
            if self.settings["live_ping"]:
                txt, col = "Live ping  " + ("—" if ms is None else f"{ms} ms"), ping_color(ms)
            else:
                txt, col = ("Ping  tap to measure", DIM) if ms is None else (f"Ping  {ms} ms", ping_color(ms))
        elif st == "connecting":
            txt, col = "Connecting…", DIM
        else:
            code = (self.country_code or "rec").lower()
            ms = self.ping_for(code)
            if self.ping_waiting(code):
                txt, col = "Testing ping…", DIM
            elif ms is None:
                txt, col = "Tap to test ping", DIM
            else:
                txt, col = f"{country_name(code)}  {ms} ms", ping_color(ms)
        c = self.c
        c.itemconfig(self.pill_txt, text=txt, fill=MUTED if col == DIM else TEXT)
        c.itemconfig(self.pill_dot, fill=col)
        box = c.bbox(self.pill_txt)
        if box:
            c.coords(self.pill_dot, box[0] - 12, 339, box[0] - 6, 345)

    # ------------------------------------------------------------ appearance
    def set_state(self, vs):
        self.vstate = vs
        c = self.c
        title, tcol = {"off": ("Disconnected", TEXT), "connecting": ("Connecting...", TEXT),
                       "on": ("Protected", GREEN)}[vs]
        c.itemconfig(self.s_title, text=title, fill=tcol)
        if self.art:
            self._hero_i = 0
            self._apply_visuals()
        else:
            ring, glow, icon = {"off": (BORDER, None, DIM),
                                "connecting": (mix(CARD, ACCENT, 0.5), ACCENT, ACCENT2),
                                "on": (GREEN, GREEN, GREEN)}[vs]
            c.itemconfig(self.ring, outline=ring)
            n = len(self.glow)
            for i, item in enumerate(self.glow):
                t = 0 if glow is None else 0.05 + 0.11 * (i + 1) / n
                c.itemconfig(item, fill=mix(BG, glow, t) if glow else BG)
            c.itemconfig(self.p_arc, outline=icon)
            c.itemconfig(self.p_line, fill=icon)
            c.itemconfig(self.spin, state="normal" if vs == "connecting" else "hidden")
        if vs != "on":
            self.live_ms = None
        self._refresh_sub()
        busy = self.proc is not None
        self.port_entry.config(state="disabled" if busy else "normal")
        self.draw_toggle()
        self.update_pill()
        self.update_tray()

    def _apply_visuals(self):
        a, st = self.art, self.vstate
        if not a:
            return
        if self._bg_mode != st:
            ph = a.photo(("bg", st))
            if ph is not None:
                self.c.itemconfig(self.bg_item, image=ph)
                self._bg_mode = st
        key = (st, self._hero_i % gfx.FRAMES[st])
        if key != self._hero_shown:
            ph = a.photo(("hero",) + key)
            if ph is not None:
                self.c.itemconfig(self.hero_item, image=ph)
                self._hero_shown = key

    def _refresh_sub(self):
        if self.vstate == "off":
            txt = "Tap the power button to connect"
        elif self.vstate == "connecting":
            txt = "Signing in and picking a server"
        else:
            txt = f"socks5://{self.listen_display()}:{self.port_var.get().strip()}"
            if self.tun and self.use_tun.get():
                txt += "  \u2022  whole system"
            if self.settings["http_on"] and self.tun:
                txt += f"  \u2022  http :{self.settings['http_port']}"
        self.c.itemconfig(self.s_sub, text=txt)

    def _anim(self):
        if self.art:
            if self.vstate != "off":
                self._hero_i += 1
            self._apply_visuals()
        elif self.vstate == "connecting":
            self.spin_angle = (self.spin_angle - 16) % 360
            self.c.itemconfig(self.spin, start=self.spin_angle)
        elif self.vstate == "on":
            t = 0.5 + 0.5 * math.sin(time.time() * 2)
            n = len(self.glow)
            for i, item in enumerate(self.glow):
                self.c.itemconfig(item, fill=mix(BG, GREEN, (0.04 + 0.10 * (i + 1) / n) * (0.7 + 0.3 * t)))
        self.after(40, self._anim)

    # ----------------------------------------------------------------- usage
    def render_usage(self):
        q = self.state_data.get("quota") or {}
        used, limit, left, reset = q.get("used"), q.get("limit"), q.get("left"), q.get("reset")
        c = self.c
        c.itemconfig(self.u_used, text=usage.fmt_bytes(used) if used is not None else "-")
        c.itemconfig(self.u_of, text=f"of {usage.fmt_bytes(limit)}" if limit else "")
        if self.u_bar:
            c.delete(self.u_bar)
            self.u_bar = None
        frac = 0.0
        if used is not None and limit:
            frac = min(max(used / limit, 0.0), 1.0)
        if frac > 0:
            x2 = 38 + max(324 * frac, 10)
            self.u_bar = rrect(c, 38, 668, x2, 678, 5, fill=RED if frac >= 0.9 else ACCENT)
        parts = []
        if left is not None:
            parts.append(f"{usage.fmt_bytes(left)} left")
        if reset:
            if reset < time.time() and q.get("fetched_at", 0) < reset:
                parts.append("new cycle started - refresh")
            else:
                parts.append(usage.fmt_reset(reset))
        if not parts:
            parts.append("Tap Refresh to read your usage" if os.path.exists(TOKENS) else "Sign in first")
        c.itemconfig(self.u_line, text="  \u2022  ".join(parts))
        ago = usage.fmt_ago(q.get("fetched_at"))
        c.itemconfig(self.u_upd, text=f"Updated {ago}" if ago else "")
        self.update_header()
        self.update_tray()

    def usage_text(self):
        q = self.state_data.get("quota") or {}
        if q.get("used") is None:
            return "Usage: not read yet"
        return f"Used {usage.fmt_bytes(q['used'])} of {usage.fmt_bytes(q.get('limit'))}"

    def refresh_usage(self, manual=False):
        if self.fetching:
            return
        if not os.path.exists(TOKENS):
            if manual:
                messagebox.showinfo(APP_NAME, "Sign in first (press Connect, or run First login).")
            return
        self.fetching = True
        self.c.itemconfig(self.u_upd, text="Updating...")

        def work():
            try:
                self.q.put(("usage", usage.fetch_quota(self.api_proxy_var.get().strip() or None)))
            except Exception as exc:  # noqa: BLE001
                self.q.put(("usage_err", str(exc)))
        threading.Thread(target=work, daemon=True).start()

    # ------------------------------------------------------------------ tray
    def ui(self, fn):
        self.q.put(("ui", fn))

    def _tray_thread(self):
        if not ensure_tray_deps():
            self.q.put(("log", "[tray] could not install pystray/Pillow - running without tray"))
            return
        try:
            import pystray
            from PIL import Image
            on_img = Image.open(os.path.join(ASSETS, "moxy_256.png")).convert("RGBA")
            gray = Image.merge("LA", (on_img.convert("L"), on_img.split()[3])).convert("RGBA")
            self.tray_imgs = (gray, on_img)
            menu = pystray.Menu(
                pystray.MenuItem("Open MoxyVPN", lambda i, it: self.ui(self.show), default=True),
                pystray.MenuItem(lambda it: "Disconnect" if self.proc else "Connect",
                                 lambda i, it: self.ui(self.toggle)),
                pystray.MenuItem(lambda it: self.usage_text(), None, enabled=False),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("Quit", lambda i, it: self.ui(self.quit_app)),
            )
            self.tray = pystray.Icon(APP_NAME, gray, APP_NAME, menu)
            self.tray.run_detached()
            self.q.put(("ui", self.update_tray))
        except Exception as exc:  # noqa: BLE001
            self.q.put(("log", f"[tray] disabled: {exc}"))

    def update_tray(self):
        if not self.tray or not self.tray_imgs:
            return
        try:
            on = self.vstate == "on"
            self.tray.icon = self.tray_imgs[1 if self.vstate != "off" else 0]
            status = {"off": "Disconnected", "connecting": "Connecting...", "on": "Protected"}[self.vstate]
            self.tray.title = f"{APP_NAME} - {status}\n{self.usage_text()}"[:127]
            self.tray.update_menu()
        except Exception:
            pass

    def show(self):
        self.deiconify()
        self.lift()
        self.focus_force()

    def on_close(self):
        if self.tray:
            self.withdraw()
            if not self.notified:
                self.notified = True
                try:
                    self.tray.notify("MoxyVPN is still running here. Right-click to quit.", APP_NAME)
                except Exception:
                    pass
        else:
            self.quit_app()

    def quit_app(self):
        self.stop()
        if self.api:
            try:
                threading.Thread(target=self.api.shutdown, daemon=True).start()
            except Exception:
                pass
        if self.tray:
            try:
                self.tray.stop()
            except Exception:
                pass
        self.destroy()

    # ------------------------------------------------------------ connection
    def toggle(self):
        self.stop() if self.proc else self.start()

    def start(self):
        if self.proc:
            return
        if not os.path.exists(TOKENS):
            self.show_login()
            return
        port = self.port_var.get().strip()
        if not port.isdigit() or not 1 <= int(port) <= 65535:
            self.show()
            messagebox.showerror("Port", "Port must be a number between 1 and 65535.")
            return
        if self.use_tun.get() and not is_admin():
            self.show()
            messagebox.showerror("Administrator needed", "Open MoxyVPN.exe (it asks for Administrator) "
                                 "or turn on Proxy-only mode in Settings.")
            return
        if self.engine_needed():
            missing = not os.path.exists(MIHOMO) or (self.use_tun.get() and not os.path.exists(WINTUN))
            if missing:
                self.show()
                messagebox.showerror("Missing files", "tun\\mihomo.exe or tun\\wintun.dll not found.")
                return
        cmd = [python_exe(), "-m", "foxyvpn", "--listen", f"{self.listen_host()}:{port}"]
        if self.settings["edge"]:
            cmd += ["--proxy", self.settings["edge"]]
        elif self.country_code and self.country_code != "rec":      # "rec" = let Mozilla recommend
            cmd += ["--country", self.country_code]
        if not self.settings["verify_exit"]:
            cmd.append("--no-verify-exit")
        proxy = self.settings["api_proxy"].strip()
        if proxy:
            cmd += ["--api-proxy", proxy]
        self.need_login = False
        env = dict(os.environ, PYTHONUNBUFFERED="1", PYTHONIOENCODING="utf-8")
        try:
            self.proc = subprocess.Popen(
                cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                env=env, creationflags=NO_WINDOW, text=True, encoding="utf-8", errors="replace")
        except OSError as exc:
            self.show()
            messagebox.showerror("Error", str(exc))
            return
        self.api_port_cache = int(port)
        self.state_data.update(port=port, country=self.country_code, tun=self.use_tun.get())
        usage.save_state(self.state_data)
        threading.Thread(target=self._reader, args=("proxy", self.proc), daemon=True).start()
        self.set_state("connecting")

    def stop(self):
        self.stop_tun()
        if self.proc:
            p, self.proc = self.proc, None
            try:
                p.terminate()
            except OSError:
                pass
        self.set_state("off")

    def start_tun(self):
        if self.tun:
            return
        paths = {python_exe(), sys.executable}
        cfg = os.path.join(TUN_DIR, "config.yaml")
        with open(cfg, "w", encoding="utf-8") as f:
            S = self.settings
            lan = S["listen"] in ("0.0.0.0", "::")
            opts = {
                "tun": self.use_tun.get(), "lan": lan,
                "http_port": int(S["http_port"]) if S["http_on"] else 0,
                "socks_host": "127.0.0.1" if lan or S["listen"] in ("", "localhost") else S["listen"],
                "dns_mode": S["dns_mode"], "custom_dns_on": S["custom_dns_on"],
                "custom_dns": S["custom_dns"], "upstream": S["upstream"] if S["upstream_on"] else "",
            }
            f.write(tunconf.build(self.port_var.get().strip(), sorted(paths),
                                  self.split["mode"], self.split["apps"], opts))
        try:
            self.tun = subprocess.Popen(
                [MIHOMO, "-d", TUN_DIR, "-f", cfg], cwd=TUN_DIR, stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, creationflags=NO_WINDOW,
                text=True, encoding="utf-8", errors="replace")
        except OSError as exc:
            self._log(f"[tun] failed to start: {exc}")
            return
        threading.Thread(target=self._reader, args=("tun", self.tun), daemon=True).start()

    def stop_tun(self):
        if self.tun:
            p, self.tun = self.tun, None
            try:
                p.terminate()
                p.wait(timeout=3)
            except Exception:
                pass

    # --------------------------------------------------------------- sign in
    def _build_login(self):
        f = self.login_frame = tk.Frame(self, bg=BG)

        def entry(show=None, var=None):
            return tk.Entry(f, show=show, textvariable=var, bg=CARD, fg=TEXT, insertbackground=TEXT,
                            relief="flat", font=(FONT, 11), highlightthickness=1,
                            highlightbackground=BORDER, highlightcolor=ACCENT)

        def label(text, fg=MUTED, size=9, **kw):
            return tk.Label(f, text=text, bg=BG, fg=fg, font=(FONT, size), **kw)

        try:
            self._big = tk.PhotoImage(file=os.path.join(ASSETS, "moxy_256.png")).subsample(2, 2)
            tk.Label(f, image=self._big, bg=BG).pack(pady=(40, 4))
        except tk.TclError:
            pass
        tk.Label(f, text="MoxyVPN", bg=BG, fg=TEXT, font=(FONT, 22, "bold")).pack()
        label("Sign in with your Firefox / Mozilla VPN account", size=10).pack(pady=(2, 22))
        label("Email", anchor="w").pack(fill="x", padx=40)
        self.l_email = entry()
        self.l_email.pack(fill="x", padx=40, ipady=7, pady=(2, 12))
        label("Password", anchor="w").pack(fill="x", padx=40)
        self.l_pass = entry("\u2022")
        self.l_pass.pack(fill="x", padx=40, ipady=7, pady=(2, 12))
        self.l_code_label = label("", fg=ACCENT2, anchor="w", justify="left", wraplength=320)
        self.l_code = entry()
        self.l_btn = tk.Button(f, text="Sign in", command=self.do_login, bg=ACCENT, fg="#1B0F05",
                               activebackground=ACCENT2, activeforeground="#1B0F05", relief="flat",
                               bd=0, font=(FONT, 11, "bold"), cursor="hand2")
        self.l_btn.pack(fill="x", padx=40, ipady=8, pady=(6, 8))
        self.l_msg = label("", wraplength=320, justify="center")
        self.l_msg.pack(padx=40)
        self.l_adv = label("Can't connect? Set a sign-in proxy", fg=DIM)
        self.l_adv.config(font=(FONT, 9, "underline"), cursor="hand2")
        self.l_adv.pack(pady=(14, 0))
        self.l_adv.bind("<Button-1>", lambda e: self._show_proxy_field())
        self.l_proxy = entry(var=self.api_proxy_var)
        if self.api_proxy_var.get():
            self._show_proxy_field()
        label("Your password goes only to Firefox Accounts and is never saved. "
              "Only a sign-in token is kept on this PC.", fg=DIM, justify="center",
              wraplength=320).pack(side="bottom", pady=22)
        self.bind("<Return>", self._enter_key)

    def _show_proxy_field(self):
        self.l_adv.config(text="Sign-in proxy (e.g. socks5://127.0.0.1:10808)")
        self.l_proxy.pack(fill="x", padx=40, ipady=5, pady=(4, 0), after=self.l_adv)

    def _enter_key(self, _e=None):
        if self.login_frame.winfo_ismapped():
            self.do_verify() if self.login_ctx else self.do_login()

    def show_login(self, msg=""):
        self.show()
        self.login_ctx = None
        self.l_code_label.pack_forget()
        self.l_code.pack_forget()
        self.l_btn.config(text="Sign in", command=self.do_login)
        self._login_msg(msg, MUTED)
        self.login_frame.place(x=0, y=0, width=W, relheight=1)
        self.login_frame.lift()
        (self.l_pass if self.l_email.get() else self.l_email).focus_set()

    def hide_login(self):
        self.login_frame.place_forget()
        self.l_pass.delete(0, "end")
        self.l_code.delete(0, "end")
        self.login_ctx = None

    def _login_msg(self, text, color=MUTED):
        self.l_msg.config(text=text, fg=color)

    def _login_busy(self, busy, text=None):
        self.login_busy = busy
        if busy:
            self.l_btn.config(state="disabled", text=text or "Please wait...")
        else:
            label = "Sign in" if not self.login_ctx else (
                "Continue" if self.login_ctx["method"] == "email" else "Verify")
            self.l_btn.config(state="normal", text=label)

    def _explain(self, exc):
        try:
            from foxyvpn.exceptions import FxaApiError
        except Exception:
            FxaApiError = ()
        if FxaApiError and isinstance(exc, FxaApiError):
            nice = {102: "No Firefox account exists for this email.",
                    103: "Incorrect password.",
                    114: "Too many attempts. Wait a few minutes and try again.",
                    201: "Firefox Accounts is busy. Try again in a minute."}
            return nice.get(getattr(exc, "errno", None), str(exc))
        name = type(exc).__name__
        if isinstance(exc, OSError) or any(k in name for k in ("Connect", "Timeout", "Proxy", "Network")):
            return ("Can't reach Firefox Accounts. Check your internet connection, "
                    "or set a sign-in proxy below.")
        return f"{name}: {exc}"

    def do_login(self):
        if self.login_busy:
            return
        email, pw = self.l_email.get().strip(), self.l_pass.get()
        if "@" not in email or not pw:
            self._login_msg("Enter your email and password.", RED)
            return
        proxy = self.api_proxy_var.get().strip()
        self.state_data["api_proxy"] = proxy
        self.settings["api_proxy"] = proxy
        self._save_settings()
        self._pending_email = email
        self._login_msg("", MUTED)
        self._login_busy(True, "Signing in...")

        def work():
            try:
                from foxyvpn import fxa, http_client
                http_client.set_control_plane_proxy(proxy or None)
                resp = fxa.login(email, pw)
                if resp.verified:
                    self._finish_login(resp.session_token)
                else:
                    self.q.put(("login_code", {"session": resp.session_token, "email": email,
                                               "method": resp.verification_method or "email-2fa"}))
            except Exception as exc:  # noqa: BLE001
                self.q.put(("login_err", self._explain(exc)))
        threading.Thread(target=work, daemon=True).start()

    def _finish_login(self, session_token):
        from foxyvpn import fxa, tokens
        tokens.save_tokens(fxa.oauth_token(session_token))
        self.q.put(("login_ok", None))

    def _login_need_code(self, ctx):
        self.login_ctx = ctx
        method = ctx["method"]
        if method == "email":
            text = "Open the confirmation link we emailed you, then press Continue."
        elif method == "totp-2fa":
            text = "Enter the 6-digit code from your authenticator app."
        else:
            text = f"Enter the confirmation code we emailed to {ctx['email']}."
        self.l_code_label.config(text=text)
        self.l_code_label.pack(fill="x", padx=40, before=self.l_btn)
        if method != "email":
            self.l_code.pack(fill="x", padx=40, ipady=7, pady=(2, 8), before=self.l_btn)
            self.l_code.focus_set()
        self.l_btn.config(command=self.do_verify)
        self._login_busy(False)
        self._login_msg("", MUTED)

    def do_verify(self):
        ctx = self.login_ctx
        if self.login_busy or not ctx:
            return
        code = self.l_code.get().strip()
        if ctx["method"] != "email" and not code:
            self._login_msg("Enter the code first.", RED)
            return
        self._login_busy(True, "Verifying...")

        def work():
            try:
                from foxyvpn import fxa
                if ctx["method"] == "totp-2fa":
                    fxa._fxa_do("POST", "/session/verify/totp", session_token=ctx["session"],
                                json_body={"code": code})
                elif ctx["method"] != "email":
                    fxa.verify_session(ctx["session"], code)
                self._finish_login(ctx["session"])
            except Exception as exc:  # noqa: BLE001
                self.q.put(("login_err", self._explain(exc)))
        threading.Thread(target=work, daemon=True).start()

    def sign_out(self):
        if not messagebox.askyesno(APP_NAME, "Sign out of this account on this PC?"):
            return
        self.stop()
        try:
            from foxyvpn import tokens
            tokens.delete_tokens()
        except Exception:
            pass
        self.state_data.pop("quota", None)
        self.state_data.pop("history", None)
        self.state_data.pop("email", None)
        self.close_settings(False)
        self.update_header()
        usage.save_state(self.state_data)
        self.render_usage()
        self.l_email.delete(0, "end")
        self.show_login()

    # ------------------------------------------------------------- settings
    def _save_settings(self):
        self.state_data["settings"] = self.settings
        usage.save_state(self.state_data)

    def listen_host(self):
        return self.settings["listen"] or HOST

    def listen_display(self):
        return "127.0.0.1" if self.listen_host() in ("0.0.0.0", "::") else self.listen_host()

    def engine_needed(self):
        return bool(self.use_tun.get() or self.settings["http_on"])

    def set_setting(self, key, value, kind="none"):
        self.settings[key] = value
        self._save_settings()
        if key == "listen":
            self.c.itemconfig(self.addr_item, text=self.listen_host() + " :")
        if key == "api_proxy":
            self.api_proxy_var.set(value)
        self.apply_changes(kind)

    def apply_changes(self, kind):
        if not self.proc:
            return
        if kind == "engine":
            self.stop_tun()
            if self.engine_needed():
                self.start_tun()
            self._log("[settings] applied")
        elif kind == "core":
            self._log("[settings] reconnecting to apply changes")
            self.stop()
            self.after(800, self.start)

    def set_port(self, value):
        self.port_var.set(str(value))
        self.api_port_cache = int(value)
        self.state_data["port"] = str(value)
        self._save_settings()
        self.apply_changes("core")

    def set_proxy_only(self, on):
        self.use_tun.set(not on)
        self.state_data["tun"] = self.use_tun.get()
        self.draw_toggle()
        self._save_settings()
        self.apply_changes("core")

    # ----------------------------------------------------------------- theme
    def toggle_theme(self):
        cur = theme.resolve(self.settings["theme"])
        self.apply_theme("light" if cur == "dark" else "dark")

    def set_theme_pref(self, name):
        self.apply_theme(name)

    def apply_theme(self, pref=None):
        if pref:
            self.settings["theme"] = pref
            self._save_settings()
        apply_palette(self.settings["theme"])
        login_visible = self.login_frame.winfo_ismapped()
        reopen = self.settings_open
        self.settings_page, self.settings_open = None, False
        self.picker = None
        if self.art:
            self.art.stop = True
        for w in list(self.winfo_children()):
            w.destroy()
        self.configure(bg=BG)
        self._build()
        self._build_login()
        if self.log_visible:
            self.side.pack(side="left", fill="y")
        self.set_state(self.vstate)
        self.render_usage()
        self.update_split_chip()
        self.update_country_chip()
        if login_visible:
            self.show_login()
        if reopen:
            self.open_settings(animate=False)

    # ------------------------------------------------------ settings drawer
    def open_settings(self, animate=True):
        if self.settings_open:
            return
        self.settings_open = True
        page = self.settings_page = settings_ui.SettingsPage(self)
        w = W
        if not animate:
            page.place(x=0, y=0, width=w, relheight=1)
            page.lift()
            return
        steps = 9

        def step(i=0):
            if self.settings_page is not page:
                return
            t = (i + 1) / steps
            page.place(x=int(w * (1 - (1 - (1 - t) ** 3))), y=0, width=w, relheight=1)
            page.lift()
            if i + 1 < steps:
                self.after(14, lambda: step(i + 1))
        step()

    def close_settings(self, animate=True):
        page = self.settings_page
        if not page:
            return
        self.settings_page, self.settings_open = None, False
        if not animate:
            page.destroy()
            return
        w = W
        steps = 7

        def step(i=0):
            t = (i + 1) / steps
            try:
                page.place(x=int(w * t ** 2), y=0, width=w, relheight=1)
            except tk.TclError:
                return
            if i + 1 < steps:
                self.after(14, lambda: step(i + 1))
            else:
                page.destroy()
        step()

    def _email_text(self):
        e = self.state_data.get("email")
        if not e:
            return "Firefox VPN client"
        return e if len(e) <= 30 else e[:27] + "\u2026"

    def update_header(self):
        self.c.itemconfig(self.hdr_sub, text=self._email_text())

    # ------------------------------------------------------- split tunneling
    def split_summary(self):
        n = len(self.split["apps"])
        return {"off": "Off", "exclude": f"Bypass {n} app{'s' if n != 1 else ''}",
                "include": f"Only {n} app{'s' if n != 1 else ''}"}[self.split["mode"]] + "  \u203a"

    def update_split_chip(self):
        self.c.itemconfig(self.split_txt, text=self.split_summary())

    def open_split(self):
        SplitDialog(self)

    def set_split(self, mode, apps):
        self.split = {"mode": mode, "apps": apps}
        self.state_data["split"] = self.split
        usage.save_state(self.state_data)
        self.update_split_chip()
        if self.tun:                       # apply right away
            self.stop_tun()
            self.start_tun()
            self._log("[split] rules updated")

    # ------------------------------------------------------------ extension API
    def _start_api(self):
        try:
            _ApiHandler.app = self
            self.api = http.server.ThreadingHTTPServer(("127.0.0.1", API_PORT), _ApiHandler)
            threading.Thread(target=self.api.serve_forever, daemon=True).start()
        except OSError as exc:
            self.api = None
            self._log(f"[api] browser-extension API unavailable: {exc}")

    def api_status(self):
        q = self.state_data.get("quota") or {}
        return {
            "app": APP_NAME, "api": 1, "state": self.vstate, "port": self.api_port_cache,
            "country": self.country_code, "tun": bool(self.tun),
            "signed_in": os.path.exists(TOKENS),
            "usage": {k: q.get(k) for k in ("used", "limit", "left", "reset", "fetched_at")},
        }

    # ---------------------------------------------------------------- events
    def _reader(self, kind, proc):
        for line in proc.stdout:
            self.q.put((kind, proc, line.rstrip()))
        self.q.put((kind, proc, None))

    def _tick(self):
        dirty = False
        try:
            while True:
                item = self.q.get_nowait()
                kind = item[0]
                if kind == "ui":
                    item[1]()
                elif kind == "log":
                    self._log(item[1])
                elif kind == "login_code":
                    self._login_need_code(item[1])
                elif kind == "login_err":
                    self._login_busy(False)
                    self._login_msg(item[1], RED)
                elif kind == "login_ok":
                    self._login_busy(False)
                    self.hide_login()
                    if self._pending_email:
                        self.state_data["email"] = self._pending_email
                        usage.save_state(self.state_data)
                        self.update_header()
                    self.refresh_usage()
                    self.start()
                elif kind == "usage":
                    self.fetching = False
                    self.last_fetch = time.time()
                    usage.remember(self.state_data, item[1])
                    self.render_usage()
                elif kind == "usage_err":
                    self.fetching = False
                    self.last_fetch = time.time()
                    self._log("[usage] could not refresh: " + item[1])
                    self.render_usage()
                elif kind == "proxy":
                    _, proc, line = item
                    if proc is not self.proc:
                        continue
                    if line is None:
                        self.stop()
                        if self.need_login:
                            self.show_login("Your session expired - please sign in again.")
                        continue
                    self._log(line)
                    if "Firefox Accounts email" in line:
                        self.need_login = True
                    if "SOCKS5 proxy listening" in line:
                        if self.engine_needed():
                            self.start_tun()
                        self.set_state("on")
                        self.refresh_usage()
                        self.after(900, self.live_measure)
                elif kind == "servers":
                    self.servers, self.ping_wait = item[1], set(item[1])
                    self.ping_total = len(item[1])
                    self._log(f"[ping] server list: {len(item[1])} countries ({item[2]})")
                    dirty = True
                elif kind == "ping":
                    self.pings[item[1]] = item[2]
                    self.ping_wait.discard(item[1])
                    dirty = True
                elif kind == "ping_done":
                    self.ping_busy, self.ping_time = False, time.time()
                    ok = sum(1 for v in self.pings.values() if v is not None)
                    self._log(f"[ping] done: {ok} of {len(self.pings)} countries answered")
                    dirty = True
                elif kind == "ping_err":
                    self.ping_busy = False
                    self.ping_err = "Server list unavailable. Check your connection."
                    self._log("[ping] " + item[1])
                    dirty = True
                elif kind == "live":
                    self.live_busy, self.live_ms = False, item[1]
                    self.update_pill()
                elif kind == "tun":
                    _, proc, line = item
                    if proc is not self.tun:
                        continue
                    if line is None:
                        self.tun = None
                        self._log("[tun] stopped")
                        self._refresh_sub()
                        continue
                    self._log("[tun] " + line)
        except queue.Empty:
            pass
        if dirty:
            self._ping_changed()
        if time.time() - self.last_fetch > USAGE_REFRESH_SECONDS and self.proc and self.vstate == "on":
            self.refresh_usage()
        if int(time.time()) % 30 == 0:
            self.render_usage()
        self.after(250, self._tick)


def single_instance():
    s = socket.socket()
    try:
        s.bind(("127.0.0.1", 47831))
        s.listen(1)
        return s
    except OSError:
        return None


if __name__ == "__main__":
    lock = single_instance()
    if lock is None:
        r = tk.Tk()
        r.withdraw()
        messagebox.showinfo(APP_NAME, "MoxyVPN is already running - look for the fox in the system tray.")
        sys.exit(0)
    App().mainloop()
