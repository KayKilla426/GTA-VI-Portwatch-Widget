#!/usr/bin/env python3
"""
PortWatch VI - Leader Widget
----------------------------
A small always-on-top desktop widget that shows the current leading project
from https://portwatch-vi.xyz/ (the same card the site shows), checks every
60 seconds, and notifies you when the leader changes.

Run:        python portwatch_widget.py
Optional:   pip install pillow winotify
            (pillow = project avatars, winotify = nicer Windows toasts)

The site publishes its data as one JSON file (data/tracker.json), so this
reads that file directly instead of scraping the page.
"""

import io
import json
import os
import re
import subprocess
import sys
import threading
import time
import tkinter as tk
import urllib.error
import urllib.request
import webbrowser
from datetime import datetime
from tkinter import font as tkfont

try:
    from PIL import Image, ImageDraw, ImageTk  # optional, for avatars
except Exception:  # pragma: no cover
    Image = None

# ----------------------------------------------------------------- settings
BASE = "https://portwatch-vi.xyz/"
DATA_URL = BASE + "data/tracker.json"
POLL_SECONDS = 60            # how often to check the site
RANKED_ONLY = False          # True = only projects with public code (what the site ranks)
NOTIFY_ON_SCORE_CHANGE = True  # also notify when the leader's % changes
ALWAYS_ON_TOP = True
_APP_DIR = (os.path.dirname(sys.executable) if getattr(sys, "frozen", False)
            else os.path.dirname(os.path.abspath(__file__)))
STATE_FILE = os.path.join(_APP_DIR, "portwatch_state.json")

# ------------------------------------------------------------------- colors
BG = "#0d0d12"
CARD = "#16161e"
TEXT = "#ececf4"
MUTED = "#9a9aae"
TRACK = "#2b2b38"
PINK = "#ff3d8b"
G1, G2 = (124, 92, 255), (255, 138, 61)   # bar gradient purple -> orange

STEPS = [("loads", "Exe runs"), ("boots", "First frame"), ("menu", "Main menu"),
         ("world", "In the world"), ("playable", "Playable")]


# ------------------------------------------------------------ scoring logic
def num(v, default=0.0):
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else default


def score(e):
    """Progress % using the site's published scale (ladder to 50%, then 4 tracks)."""
    g = e.get("gta") or {}
    steps = [min(num(g.get(k)), 1.0) for k, _ in STEPS]
    if any(s > 0 for s in steps):
        total = 0.0
        for s in steps:                      # each step needs the one before it
            total += 10 * s
            if s < 1:
                break
    else:                                    # groundwork only, up to 10%
        gw = e.get("groundwork") or {}
        vals = [num(gw.get(k)) for k in ("loader", "shaders", "graphics", "proven")]
        total = 10 * sum(vals) / 4
    if steps[-1] >= 1:                       # playable: the other half
        tracks = (35 * num(g.get("story")) + 25 * num(g.get("render"))
                  + 20 * min(num(g.get("fps")) / 30.0, 1.0) + 20 * num(g.get("stability")))
        total = 50 + tracks / 2
    return round(total, 1)


def is_public(e):
    return bool(e.get("repo"))


def stage(e):
    g = e.get("gta") or {}
    if num(g.get("playable")) >= 1:
        return "Playable"
    for k, name in reversed(STEPS):
        if num(g.get(k)) > 0:
            return name
    return "Groundwork"


def join_and(items):
    items = list(items)
    if len(items) <= 1:
        return "".join(items)
    return ", ".join(items[:-1]) + " and " + items[-1]


def gpu_vendor(e):
    text = (e.get("summary", "") + " " + " ".join(e.get("highlights", []))).lower()
    hits = []
    for name, pat in (("NVIDIA", r"nvidia|geforce|\brtx\b"),
                      ("AMD", r"\bamd\b|radeon|\brx \d{4}"),
                      ("Intel", r"\bintel\b|\barc\b")):
        m = re.search(pat, text)
        if m:
            hits.append((m.start(), name))
    return min(hits)[1] if hits else None


def stability_label(v):
    if not isinstance(v, (int, float)):
        return "Unknown"
    if v >= 0.8:
        return "Rarely crashes"
    if v >= 0.5:
        return "Crashes sometimes"
    return "Crashes often"


def analyze(data):
    efforts = data.get("efforts", [])
    names = {e["id"]: e.get("name", e["id"]) for e in efforts}
    live = [e for e in efforts
            if e.get("tier") == "tracked" and not e.get("nuked")
            and not (e.get("stats") or {}).get("missing")]
    pool = [e for e in live if is_public(e)] if RANKED_ONLY else live
    if not pool:
        raise ValueError("No tracked projects found in data")
    pool.sort(key=lambda e: (score(e), (e.get("stats") or {}).get("stars", 0)), reverse=True)
    leader = pool[0]
    public_sorted = sorted([e for e in live if is_public(e)], key=score, reverse=True)
    rank = None
    if is_public(leader):
        rank = [e["id"] for e in public_sorted].index(leader["id"]) + 1
    return {"leader": leader, "score": score(leader), "rank": rank, "names": names,
            "updated": data.get("updated", "")}


# ---------------------------------------------------------------- networking
def http_get(url, etag=None, modified=None, timeout=20):
    headers = {"User-Agent": "PortWatchLeaderWidget/1.0", "Cache-Control": "no-cache"}
    if etag:
        headers["If-None-Match"] = etag
    if modified:
        headers["If-Modified-Since"] = modified
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read(), r.headers.get("ETag"), r.headers.get("Last-Modified")
    except urllib.error.HTTPError as ex:
        if ex.code == 304:
            return None, etag, modified
        raise


# -------------------------------------------------------------- notifications
def notify(title, message):
    """Best-effort desktop notification. Returns True if something was shown."""
    try:
        if sys.platform.startswith("win"):
            try:
                from winotify import Notification, audio
                t = Notification(app_id="PortWatch Widget", title=title, msg=message, duration="long")
                t.set_audio(audio.Default, loop=False)
                t.add_actions(label="Open site", launch=BASE)
                t.show()
                return True
            except ImportError:
                pass
            esc = lambda s: (s.replace("&", "&amp;").replace("<", "&lt;")
                             .replace(">", "&gt;").replace('"', "'"))
            ps = (
                "[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] > $null;"
                "$t=[Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent([Windows.UI.Notifications.ToastTemplateType]::ToastText02);"
                "$n=$t.GetElementsByTagName('text');"
                f"$n.Item(0).AppendChild($t.CreateTextNode(\"{esc(title)}\")) > $null;"
                f"$n.Item(1).AppendChild($t.CreateTextNode(\"{esc(message)}\")) > $null;"
                "$toast=[Windows.UI.Notifications.ToastNotification]::new($t);"
                "[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier("
                "'{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\\WindowsPowerShell\\v1.0\\powershell.exe').Show($toast)"
            )
            subprocess.Popen(["powershell", "-NoProfile", "-WindowStyle", "Hidden", "-Command", ps],
                             creationflags=0x08000000)
            return True
        if sys.platform == "darwin":
            subprocess.Popen(["osascript", "-e",
                              f'display notification "{message}" with title "{title}"'])
            return True
        subprocess.Popen(["notify-send", title, message])
        return True
    except Exception:
        return False


def beep():
    try:
        if sys.platform.startswith("win"):
            import winsound
            winsound.MessageBeep(winsound.MB_ICONASTERISK)
    except Exception:
        pass


# ---------------------------------------------------------------- UI helpers
def rounded(c, x1, y1, x2, y2, r, **kw):
    r = max(0, min(r, (x2 - x1) / 2, (y2 - y1) / 2))
    pts = [x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r, x2, y2 - r, x2, y2, x2 - r, y2,
           x1 + r, y2, x1, y2, x1, y2 - r, x1, y1 + r, x1, y1]
    return c.create_polygon(pts, smooth=True, **kw)


def lerp(a, b, t):
    return "#%02x%02x%02x" % tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3))


def ellipsize(font, text, width):
    if font.measure(text) <= width:
        return text
    while text and font.measure(text + "…") > width:
        text = text[:-1]
    return text.rstrip() + "…"


def open_url(url):
    webbrowser.open(url)


class ChipRow(tk.Canvas):
    """Wrapping row of rounded pill labels; a chip with a url is clickable."""

    def __init__(self, master, chips, width, font, bg):
        super().__init__(master, width=width, height=10, highlightthickness=0, bg=bg)
        h, gap, x, y = font.metrics("linespace") + 10, 8, 0, 0
        for i, (text, fg, fill, outline, url) in enumerate(chips):
            w = font.measure(text) + 24
            if x + w > width and x > 0:
                x, y = 0, y + h + gap
            tag = f"chip{i}"
            rounded(self, x + 1, y + 1, x + w, y + h, h / 2, fill=fill, outline=outline, tags=(tag,))
            self.create_text(x + w / 2 + 0.5, y + h / 2 + 0.5, text=text, fill=fg, font=font, tags=(tag,))
            if url:
                self.tag_bind(tag, "<Button-1>", lambda _e, u=url: open_url(u))
                self.tag_bind(tag, "<Enter>", lambda _e: self.config(cursor="hand2"))
                self.tag_bind(tag, "<Leave>", lambda _e: self.config(cursor=""))
            x += w + gap
        self.config(height=y + h + 2)


# ----------------------------------------------------------------------- app
class App:
    CARD_W = 520

    def __init__(self):
        try:
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass
        self.root = tk.Tk()
        self.root.title("PortWatch · Leading project")
        self.root.configure(bg=BG)
        self.root.attributes("-topmost", ALWAYS_ON_TOP)
        self.root.resizable(False, False)
        fam = "Segoe UI" if sys.platform.startswith("win") else "Helvetica"
        mk = lambda size, bold=False: tkfont.Font(family=fam, size=size, weight="bold" if bold else "normal")
        self.f_name, self.f_score = mk(15, True), mk(21, True)
        self.f_body, self.f_small, self.f_chip = mk(10), mk(9), mk(9, True)
        self.f_tiny, self.f_tinyb = mk(8), mk(8, True)

        outer = tk.Frame(self.root, bg=BG, padx=14, pady=12)
        outer.pack()
        self.banner = tk.Label(outer, text="", bg=PINK, fg="white", font=self.f_small,
                               pady=5, wraplength=self.CARD_W + 30)
        self.card = tk.Frame(outer, bg=CARD, padx=18, pady=16, highlightthickness=1,
                             highlightbackground="#2a2a38")
        self.card.pack()
        self.placeholder = tk.Label(self.card, text="Loading…", bg=CARD, fg=MUTED,
                                    font=self.f_body, width=60, height=8)
        self.placeholder.pack()

        foot = tk.Frame(outer, bg=BG)
        foot.pack(fill="x", pady=(10, 0))
        self.status = tk.Label(foot, text="", bg=BG, fg=MUTED, font=self.f_tiny, anchor="w")
        self.status.pack(side="left")
        self.pin_var = tk.BooleanVar(value=ALWAYS_ON_TOP)
        for text, cmd in (("Refresh", self.refresh), ("Open site", lambda: open_url(BASE))):
            b = tk.Label(foot, text=text, bg=BG, fg=MUTED, font=self.f_tiny, cursor="hand2")
            b.pack(side="right", padx=(10, 0))
            b.bind("<Button-1>", lambda _e, c=cmd: c())
        pin = tk.Checkbutton(foot, text="Pin on top", variable=self.pin_var, bg=BG, fg=MUTED,
                             selectcolor=BG, activebackground=BG, activeforeground=TEXT,
                             font=self.f_tiny, command=self.toggle_pin, bd=0, highlightthickness=0)
        pin.pack(side="right", padx=(10, 0))

        self.state = self.load_state()
        self.result = None
        self.avatar_img = None
        self.avatar_cache = {}
        self.err = None
        self.last_check = None
        self.next_poll = time.time() + POLL_SECONDS
        self.refresh_evt = threading.Event()
        self.stop = False
        self.results = []          # messages from the worker thread
        self.lock = threading.Lock()
        threading.Thread(target=self.worker, daemon=True).start()
        self.root.protocol("WM_DELETE_WINDOW", self.quit)
        self.root.after(300, self.pump)
        self.root.after(1000, self.tick)

    # ---------------------------------------------------------- housekeeping
    def load_state(self):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}

    def save_state(self):
        try:
            with open(STATE_FILE, "w", encoding="utf-8") as f:
                json.dump(self.state, f)
        except Exception:
            pass

    def toggle_pin(self):
        self.root.attributes("-topmost", self.pin_var.get())

    def refresh(self):
        self.refresh_evt.set()

    def quit(self):
        self.stop = True
        self.refresh_evt.set()
        self.root.destroy()

    def run(self):
        self.root.mainloop()

    # ---------------------------------------------------------------- worker
    def worker(self):
        etag = modified = None
        while not self.stop:
            try:
                raw, etag, modified = http_get(DATA_URL, etag, modified)
                if raw is not None or self.result is None:
                    if raw is None:   # nothing cached yet, fetch fresh
                        raw, etag, modified = http_get(DATA_URL)
                    res = analyze(json.loads(raw.decode("utf-8")))
                    res["avatar_bytes"] = self.get_avatar(res["leader"])
                    self.post(("ok", res))
                else:
                    self.post(("same", None))
            except Exception as ex:
                self.post(("err", f"{type(ex).__name__}: {ex}"))
            self.refresh_evt.wait(POLL_SECONDS)
            self.refresh_evt.clear()

    def get_avatar(self, e):
        path = e.get("avatar")
        if not path or Image is None:
            return None
        if path not in self.avatar_cache:
            try:
                self.avatar_cache[path] = http_get(BASE + path)[0]
            except Exception:
                self.avatar_cache[path] = None
        return self.avatar_cache[path]

    def post(self, msg):
        with self.lock:
            self.results.append(msg)

    # ------------------------------------------------------------ UI updates
    def pump(self):
        with self.lock:
            msgs, self.results = self.results, []
        for kind, payload in msgs:
            self.last_check = datetime.now()
            self.next_poll = time.time() + POLL_SECONDS
            if kind == "ok":
                self.err = None
                self.on_result(payload)
            elif kind == "err":
                self.err = payload
        if not self.stop:
            self.root.after(300, self.pump)

    def tick(self):
        if self.stop:
            return
        left = max(0, int(self.next_poll - time.time()))
        if self.err:
            txt = f"Can't reach the site, retrying in {left}s"
        elif self.last_check:
            txt = f"Checked {self.last_check:%H:%M:%S} · next in {left}s"
        else:
            txt = "Checking…"
        self.status.config(text=txt, fg="#ff8a8a" if self.err else MUTED)
        self.root.after(1000, self.tick)

    def on_result(self, res):
        e = res["leader"]
        sc = res["score"]
        last_id, last_sc = self.state.get("leader_id"), self.state.get("score")
        msg = None
        if last_id is None:
            pass  # first run: just show, don't notify
        elif e["id"] != last_id:
            kind = "private" if not is_public(e) else "public"
            msg = (f"New leader: {e.get('name')}",
                   f"{sc:.1f}% · {stage(e)} · {kind} code")
        elif NOTIFY_ON_SCORE_CHANGE and last_sc is not None and abs(sc - last_sc) >= 0.05:
            msg = (f"{e.get('name')} is still leading",
                   f"Progress changed: {last_sc:.1f}% → {sc:.1f}%")
        self.state.update({"leader_id": e["id"], "score": sc})
        self.save_state()
        self.render(res)
        if msg:
            self.banner.config(text=f"🔔 {msg[0]}  —  {msg[1]}")
            self.banner.pack(before=self.card, fill="x", pady=(0, 8))
            notify(*msg)
            beep()
            self.flash()

    def flash(self):
        try:
            self.root.deiconify()
            self.root.lift()
            self.root.attributes("-topmost", True)
            self.root.after(2500, lambda: self.root.attributes("-topmost", self.pin_var.get()))
            self.root.bell()
        except Exception:
            pass

    # ---------------------------------------------------------- card render
    def render(self, res):
        e, W = res["leader"], self.CARD_W
        for w in self.card.winfo_children():
            w.destroy()
        g = e.get("gta") or {}
        sc = res["score"]
        public = is_public(e)

        # header: avatar, name, byline, score
        hd = tk.Canvas(self.card, width=W, height=64, bg=CARD, highlightthickness=0)
        hd.pack(anchor="w")
        self.draw_avatar(hd, e, res.get("avatar_bytes"))
        hd.create_text(70, 18, text=e.get("name", "?"), fill=TEXT, font=self.f_name, anchor="w")
        by = e.get("byline") or self.make_byline(e, res["names"])
        hd.create_text(70, 44, text=ellipsize(self.f_small, by, W - 70 - 130), fill=MUTED,
                       font=self.f_small, anchor="w")
        hd.create_text(W, 18, text=f"{sc:.1f}%", fill=TEXT, font=self.f_score, anchor="e")
        rank = f"#{res['rank']}" if res["rank"] else "not ranked"
        hd.create_text(W, 46, text=f"{stage(e)} · {rank}", fill=MUTED, font=self.f_small, anchor="e")

        # chips
        chips = []
        based = [res["names"].get(b, b) for b in e.get("basedOn", [])]
        if based:
            chips.append(("Built on " + join_and(based), "#bba9ff", "#231f45", "#5a4aa8", None))
        v = gpu_vendor(e)
        if v:
            col = {"NVIDIA": "#b6e35a", "AMD": "#ff7b6b", "Intel": "#6db4ff"}[v]
            chips.append((v, col, "#14151c", col, None))
        ev = e.get("evidence") or {"portwatch": "Verified by PortWatch",
                                   "self-reported": "Self-reported"}.get(e.get("verification"), "Unverified")
        chips.append(("Evidence: " + ev, "#f0c060", "#352b12", "#6b5622", None))
        if public:
            chips.append(("Public code · GitHub ↗", "#5fdc9a", "#12301f", "#2b7a50",
                          "https://github.com/" + e["repo"]))
        else:
            chips.append(("No public code", "#ff7a8a", "#3a1520", "#7a2a3c", None))
        proof = e.get("proof") or {}
        if proof.get("url"):
            chips.append((proof.get("label", "Watch video") + " ↗", "#ff7ab6", "#3a1530", "#7a2a5c", proof["url"]))
        ChipRow(self.card, chips, W, self.f_chip, CARD).pack(anchor="w", pady=(14, 0))

        # summary
        tk.Label(self.card, text=e.get("summary", ""), bg=CARD, fg="#d6d6e2", font=self.f_body,
                 wraplength=W, justify="left", anchor="w").pack(anchor="w", pady=(14, 0))

        # milestone dots
        ms = tk.Canvas(self.card, width=W, height=22, bg=CARD, highlightthickness=0)
        ms.pack(anchor="w", pady=(14, 0))
        gw = e.get("groundwork") or {}
        gw_avg = sum(num(gw.get(k)) for k in ("loader", "shaders", "graphics", "proven")) / 4
        any_step = any(num(g.get(k)) > 0 for k, _ in STEPS)
        ladder = [("Groundwork", 1.0 if any_step else (1.0 if gw_avg >= 0.5 else (0.5 if gw_avg > 0 else 0)))]
        ladder += [(name, num(g.get(k))) for k, name in STEPS]
        x = 0
        for name, val in ladder:
            if val >= 1:
                ms.create_oval(x, 6, x + 8, 14, fill=PINK, outline=PINK)
            elif val > 0:
                ms.create_oval(x, 6, x + 8, 14, fill="#7a2a55", outline=PINK)
            else:
                ms.create_oval(x, 6, x + 8, 14, fill=CARD, outline="#555566")
            ms.create_text(x + 13, 10, text=name, anchor="w", font=self.f_tinyb,
                           fill=TEXT if val > 0 else MUTED)
            x += 13 + self.f_tinyb.measure(name) + 12

        # four tracks
        playable = num(g.get("playable")) >= 1
        stab = g.get("stability")
        tracks = [
            ("Story progress", num(g.get("story")), f"{num(g.get('story')) * 100:.0f}%"),
            ("Rendering", num(g.get("render")), f"{num(g.get('render')):.2f}"),
            ("Performance", min(num(g.get("fps")) / 30.0, 1.0), f"{num(g.get('fps')):g} FPS"),
            ("Stability", num(stab), stability_label(stab)),
        ]
        bars = tk.Canvas(self.card, width=W, height=58, bg=CARD, highlightthickness=0)
        bars.pack(anchor="w", pady=(10, 0))
        gap = 14
        cw = (W - gap * 3) / 4
        for i, (label, frac, text) in enumerate(tracks):
            x0 = i * (cw + gap)
            bars.create_text(x0, 8, text=label, anchor="w", fill=TEXT, font=self.f_tinyb)
            bars.create_text(x0, 25, text=text if playable else "—", anchor="w", fill=MUTED, font=self.f_tiny)
            rounded(bars, x0, 40, x0 + cw, 46, 3, fill=TRACK, outline="")
            if playable and frac > 0:
                n = max(2, int(cw * min(frac, 1)))
                for px in range(n):
                    bars.create_line(x0 + px, 40, x0 + px, 46, fill=lerp(G1, G2, px / max(cw, 1)))

        # report link
        link = e.get("link")
        url = BASE + link if link else BASE + "#race"
        lk = tk.Label(self.card, text="Read our report →", bg=CARD, fg=TEXT, font=self.f_body, cursor="hand2")
        lk.pack(anchor="w", pady=(8, 0))
        lk.bind("<Button-1>", lambda _e: open_url(url))
        lk.bind("<Enter>", lambda _e: lk.config(fg=PINK))
        lk.bind("<Leave>", lambda _e: lk.config(fg=TEXT))

        self.root.update_idletasks()

    def make_byline(self, e, names):
        text = "Public code · " + e["repo"] if is_public(e) else "Private code"
        based = [names.get(b, b) for b in e.get("basedOn", [])]
        if based:
            text += " · based on " + join_and(based)
        return text

    def draw_avatar(self, canvas, e, raw):
        self.avatar_img = None
        if raw and Image is not None:
            try:
                im = Image.open(io.BytesIO(raw)).convert("RGBA").resize((56, 56))
                mask = Image.new("L", (56, 56), 0)
                ImageDraw.Draw(mask).rounded_rectangle((0, 0, 55, 55), radius=12, fill=255)
                im.putalpha(mask)
                self.avatar_img = ImageTk.PhotoImage(im)
                canvas.create_image(0, 4, image=self.avatar_img, anchor="nw")
                return
            except Exception:
                pass
        rounded(canvas, 0, 4, 56, 60, 12, fill="#2a2450", outline="#5a4aa8")
        initials = "".join(w[0] for w in re.findall(r"[A-Za-z0-9]+", e.get("name", "?"))[:2]).upper()[:3]
        canvas.create_text(28, 32, text=initials or "?", fill="#bba9ff", font=self.f_name)


if __name__ == "__main__":
    App().run()
