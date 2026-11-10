#!/usr/bin/env python3
"""Render the animated README diagrams into docs/media/*.gif.

Maintainer tool, not part of the runtime: needs Pillow and ffmpeg on PATH.
    uv run --with pillow python tools/readme_gifs.py [architecture|learning|use-cases ...]
"""
import math
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

from PIL import Image, ImageDraw, ImageFilter, ImageFont

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / 'docs/media'
W, H, S, FPS = 960, 600, 2, 15

BG_TOP, BG_BOTTOM = (9, 13, 28), (18, 22, 46)
PANEL, PANEL_HI, EDGE = (22, 29, 52), (30, 39, 70), (52, 64, 100)
TEXT, MUTED, DIM = (232, 237, 248), (146, 158, 190), (92, 104, 138)
CYAN, VIOLET, AMBER = (34, 211, 238), (167, 139, 250), (251, 191, 36)
GREEN, PINK, BLUE, RED = (52, 211, 153), (244, 114, 182), (96, 165, 250), (248, 113, 113)

FONTS = {
    (False, False): ['/usr/share/fonts/truetype/noto/NotoSans-Regular.ttf', '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf'],
    (True, False): ['/usr/share/fonts/truetype/noto/NotoSans-Bold.ttf', '/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf'],
    (False, True): ['/usr/share/fonts/truetype/noto/NotoSansMono-Regular.ttf', '/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf'],
    (True, True): ['/usr/share/fonts/truetype/noto/NotoSansMono-Bold.ttf', '/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf'],
}
_font_cache = {}


def font(size, bold=False, mono=False):
    key = (size, bold, mono)
    if key not in _font_cache:
        for path in FONTS[(bold, mono)]:
            if Path(path).exists():
                _font_cache[key] = ImageFont.truetype(path, size * S)
                break
        else:
            _font_cache[key] = ImageFont.load_default(size * S)
    return _font_cache[key]


def clamp(v, lo=0.0, hi=1.0):
    return max(lo, min(hi, v))


def ease(t):
    t = clamp(t)
    return t * t * (3 - 2 * t)


def mix(a, b, t):
    t = clamp(t)
    return tuple(int(round(x + (y - x) * t)) for x, y in zip(a, b))


def phase(t, start, end):
    return clamp((t - start) / (end - start)) if end > start else float(t >= start)


_background = None


def background():
    global _background
    if _background is None:
        img = Image.new('RGB', (W * S, H * S))
        d = ImageDraw.Draw(img)
        for y in range(H * S):
            d.line([(0, y), (W * S, y)], fill=mix(BG_TOP, BG_BOTTOM, y / (H * S)))
        for gx in range(0, W, 24):
            for gy in range(0, H, 24):
                d.ellipse([gx * S - 1, gy * S - 1, gx * S + 1, gy * S + 1], fill=(28, 35, 62))
        _background = img
    return _background.copy()


class Canvas:
    def __init__(self):
        self.img = background()
        self.d = ImageDraw.Draw(self.img)

    @staticmethod
    def _s(v):
        return [int(round(x * S)) for x in v]

    def box(self, x, y, w, h, r=14, fill=PANEL, outline=EDGE, width=2):
        self.d.rounded_rectangle(self._s([x, y, x + w, y + h]), radius=r * S, fill=fill,
                                 outline=outline, width=int(width * S) if outline else 0)

    def glow(self, x, y, w, h, color, strength=1.0, r=14, spread=10):
        if strength <= 0.02:
            return
        pad = spread * 3
        layer = Image.new('RGBA', ((w + 2 * pad) * S, (h + 2 * pad) * S), (0, 0, 0, 0))
        ImageDraw.Draw(layer).rounded_rectangle([pad * S, pad * S, (pad + w) * S, (pad + h) * S], radius=r * S,
                                                outline=color + (int(200 * clamp(strength)),), width=spread * S)
        layer = layer.filter(ImageFilter.GaussianBlur(spread * S * 0.8))
        self.img.paste(layer, (int((x - pad) * S), int((y - pad) * S)), layer)

    def text(self, x, y, s, size=16, color=TEXT, bold=False, mono=False, anchor='la'):
        self.d.text((x * S, y * S), s, font=font(size, bold, mono), fill=color, anchor=anchor)

    def width(self, s, size=16, bold=False, mono=False):
        return self.d.textlength(s, font=font(size, bold, mono)) / S

    def wrap(self, x, y, s, maxw, size=16, color=TEXT, bold=False, mono=False, leading=1.35):
        line, lines = '', []
        for word in s.split():
            trial = (line + ' ' + word).strip()
            if self.width(trial, size, bold, mono) > maxw and line:
                lines.append(line)
                line = word
            else:
                line = trial
        lines.append(line)
        for i, ln in enumerate(lines):
            self.text(x, y + i * size * leading, ln, size, color, bold, mono)
        return y + len(lines) * size * leading

    def line(self, pts, color=EDGE, width=2):
        self.d.line([tuple(self._s(p)) for p in pts], fill=color, width=int(width * S), joint='curve')

    def arrow(self, pts, color=EDGE, width=2, head=8):
        self.line(pts, color, width)
        (x1, y1), (x2, y2) = pts[-2], pts[-1]
        a = math.atan2(y2 - y1, x2 - x1)
        tip = [(x2, y2), (x2 - head * math.cos(a - 0.45), y2 - head * math.sin(a - 0.45)),
               (x2 - head * math.cos(a + 0.45), y2 - head * math.sin(a + 0.45))]
        self.d.polygon([tuple(self._s(p)) for p in tip], fill=color)

    def dot(self, x, y, r, color, halo=True):
        if halo:
            for k, alpha in ((2.6, 0.18), (1.8, 0.35)):
                self.d.ellipse(self._s([x - r * k, y - r * k, x + r * k, y + r * k]), fill=mix(BG_BOTTOM, color, alpha))
        self.d.ellipse(self._s([x - r, y - r, x + r, y + r]), fill=color)

    def check(self, x, y, size=14, color=GREEN):
        self.line([(x, y + size * 0.55), (x + size * 0.38, y + size * 0.9), (x + size, y + size * 0.1)], color, 3)

    def cross(self, x, y, size=12, color=RED):
        self.line([(x, y), (x + size, y + size)], color, 3)
        self.line([(x + size, y), (x, y + size)], color, 3)

    def frame(self):
        return self.img.resize((W, H), Image.LANCZOS)


def along(pts, f):
    """Point at fraction f of a polyline's length."""
    seg = [math.dist(a, b) for a, b in zip(pts, pts[1:])]
    total = sum(seg) or 1
    want = clamp(f) * total
    for (a, b), length in zip(zip(pts, pts[1:]), seg):
        if want <= length:
            k = want / length if length else 0
            return a[0] + (b[0] - a[0]) * k, a[1] + (b[1] - a[1]) * k
        want -= length
    return pts[-1]


def packets(c, pts, t, color, count=3, r=4):
    for k in range(count):
        f = (t * 1.6 + k / count) % 1.0
        x, y = along(pts, f)
        c.dot(x, y, r, color)


# ---------------------------------------------------------------- icons
def icon(c, kind, x, y, color, s=26):
    if kind == 'browser':
        c.box(x, y + 2, s, s - 4, r=4, fill=None, outline=color, width=2)
        c.line([(x, y + 9), (x + s, y + 9)], color, 2)
        for i in range(3):
            c.dot(x + 5 + i * 5, y + 5.5, 1.3, color, halo=False)
    elif kind == 'file':
        c.line([(x + 4, y), (x + s - 9, y), (x + s - 3, y + 6), (x + s - 3, y + s), (x + 4, y + s), (x + 4, y)], color, 2)
        for i in range(3):
            c.line([(x + 9, y + 10 + i * 5), (x + s - 8, y + 10 + i * 5)], color, 2)
    elif kind == 'git':
        c.line([(x + 7, y + 3), (x + 7, y + s - 3)], color, 2)
        c.line([(x + 7, y + 15), (x + s - 6, y + 9)], color, 2)
        for px, py in ((x + 7, y + 4), (x + 7, y + s - 4), (x + s - 6, y + 8)):
            c.d.ellipse(c._s([px - 3.5, py - 3.5, px + 3.5, py + 3.5]), fill=PANEL, outline=color, width=2 * S)
    elif kind == 'chat':
        c.box(x, y + 2, s, s - 9, r=6, fill=None, outline=color, width=2)
        c.line([(x + 7, y + s - 7), (x + 5, y + s), (x + 13, y + s - 7)], color, 2)
    elif kind == 'terminal':
        c.box(x, y + 2, s, s - 4, r=4, fill=None, outline=color, width=2)
        c.line([(x + 5, y + 9), (x + 10, y + 13), (x + 5, y + 17)], color, 2)
        c.line([(x + 13, y + 18), (x + 20, y + 18)], color, 2)
    elif kind == 'script':
        c.text(x + s / 2, y + s / 2, '{ }', 16, color, bold=True, mono=True, anchor='mm')
    elif kind == 'clock':
        c.d.ellipse(c._s([x + 1, y + 1, x + s - 1, y + s - 1]), outline=color, width=2 * S)
        c.line([(x + s / 2, y + 6), (x + s / 2, y + s / 2), (x + s - 8, y + s / 2 + 3)], color, 2)
    elif kind == 'web':
        c.d.ellipse(c._s([x + 1, y + 1, x + s - 1, y + s - 1]), outline=color, width=2 * S)
        c.d.ellipse(c._s([x + s * 0.3, y + 1, x + s * 0.7, y + s - 1]), outline=color, width=2 * S)
        c.line([(x + 2, y + s / 2), (x + s - 2, y + s / 2)], color, 2)


def logo(c, x, y, t=0.0, size=30):
    """An eye that blinks now and then: the Web Blink mark."""
    lid = 1.0 - 0.9 * max(0.0, 1 - abs(((t * 0.35) % 1.0) - 0.5) * 14)
    h = size * 0.42 * lid
    pts_top = [(x + size * i / 20, y - h * math.sin(math.pi * i / 20)) for i in range(21)]
    pts_bot = [(x + size * i / 20, y + h * math.sin(math.pi * i / 20)) for i in range(21)]
    c.line(pts_top, CYAN, 2.4)
    c.line(pts_bot, CYAN, 2.4)
    if lid > 0.35:
        c.dot(x + size / 2, y, size * 0.17 * lid, VIOLET, halo=False)


def header(c, title, subtitle, t):
    logo(c, 28, 40, t)
    c.text(70, 40, title, 24, TEXT, bold=True, anchor='lm')
    c.text(W - 28, 40, subtitle, 14, MUTED, anchor='rm')


def caption(c, n, total, text, accent=CYAN, y=532):
    c.box(24, y, W - 48, 48, r=12, fill=PANEL, outline=EDGE, width=1.5)
    for i in range(total):
        cx = 50 + i * 16
        c.dot(cx, y + 24, 4 if i == n else 3, accent if i == n else DIM, halo=i == n)
    c.text(50 + total * 16 + 10, y + 24, text, 17, TEXT, bold=False, anchor='lm')


def node(c, x, y, w, h, kind, title, sub, accent, lit, mono_sub=False):
    c.glow(x, y, w, h, accent, lit * 0.8)
    c.box(x, y, w, h, r=12, fill=mix(PANEL, PANEL_HI, lit), outline=mix(EDGE, accent, lit), width=2)
    icon(c, kind, x + 14, y + h / 2 - 13, mix(MUTED, accent, lit))
    c.text(x + 52, y + h / 2 - 11, title, 16, mix(MUTED, TEXT, 0.4 + 0.6 * lit), bold=True, anchor='lm')
    c.text(x + 52, y + h / 2 + 12, sub, 12.5, mix(DIM, MUTED, 0.3 + 0.7 * lit), mono=mono_sub, anchor='lm')


# ---------------------------------------------------------------- 1. architecture
def architecture():
    steps = [
        ('Learn: Blink records a web app while you, or the agent, use it', [0], CYAN),
        ('Or import a saved HAR file, or Blinks from your own Git repository', [1, 2], VIOLET),
        ('Blink proposes a versioned Blink; it is reviewed and switched on locally', ['learn'], AMBER),
        ('Your logins, defaults and secrets stay in your private local vault', ['vault'], PINK),
        ('Use it from Claude Code or Codex sessions through MCP tools', ['mcp'], GREEN),
        ('…or as CLI commands, scripts, verified routines and schedules', ['cli'], BLUE),
    ]
    per, hold = 34, 30
    total = per * len(steps) + hold
    left = [(28, 112, 'browser', 'Your browser', 'record while you use the app'),
            (28, 204, 'file', 'HAR file', 'import a saved session'),
            (28, 296, 'git', 'Git repository', 'your own or shared Blinks')]
    right = [(696, 104, 'chat', 'Claude Code · Codex', 'MCP tools in your session', False),
             (696, 184, 'terminal', 'CLI', 'blink shop list-orders', True),
             (696, 264, 'script', 'Scripts & routines', 'repeatable, verified', False),
             (696, 344, 'clock', 'Schedules', 'jobs that run on time', False)]
    chips = [('Recorder', 'captures requests + screenshots', CYAN), ('Learner', 'proposes operations & auth', AMBER),
             ('Review & activate', 'you switch it on', AMBER)]
    frames = []
    for i in range(total):
        step = min(i // per, len(steps))
        t = (i % per) / per if step < len(steps) else 1.0
        c = Canvas()
        header(c, 'How Web Blink works', 'everything runs locally on your machine', i / FPS)
        c.text(28, 90, 'LEARN FROM', 12, DIM, bold=True)
        c.text(318, 90, 'LOCAL BLINK RUNTIME', 12, DIM, bold=True)
        c.text(696, 82, 'USE IT FROM', 12, DIM, bold=True)
        done = lambda k: step > k or step == len(steps)
        cur = lambda k: (step == k) * ease(t * 3)
        lit = lambda k: 1.0 if done(k) else cur(k)
        # center runtime
        c.glow(318, 104, 324, 318, CYAN, 0.25)
        c.box(318, 104, 324, 318, r=16, fill=(16, 22, 42), outline=(60, 76, 120), width=2)
        c.text(336, 124, 'Local Blink', 17, TEXT, bold=True, anchor='lm')
        c.text(624, 124, '~/.web-blink', 13, MUTED, mono=True, anchor='rm')
        ys = [146, 194, 242]
        for k, (title, sub, col) in enumerate(chips):
            a = lit(0) if k == 0 else lit(2)
            c.glow(334, ys[k], 292, 38, col, a * 0.6, r=10)
            c.box(334, ys[k], 292, 38, r=10, fill=mix(PANEL, PANEL_HI, a), outline=mix(EDGE, col, a), width=1.5)
            c.text(348, ys[k] + 19, title, 14, mix(MUTED, TEXT, 0.5 + a / 2), bold=True, anchor='lm')
            c.text(614, ys[k] + 19, sub, 11.5, mix(DIM, MUTED, a), anchor='rm')
            if k < 2:
                c.arrow([(480, ys[k] + 38), (480, ys[k + 1] - 1)], mix(EDGE, col, a), 1.5, 6)
        blink_lit = max(lit(1), lit(2))
        version = '0.1.0' if step < 3 else ('0.2.0' if step < 5 else '0.3.0')
        c.glow(334, 296, 292, 44, VIOLET, blink_lit * 0.7, r=10)
        c.box(334, 296, 292, 44, r=10, fill=mix(PANEL, (44, 34, 82), blink_lit), outline=mix(EDGE, VIOLET, blink_lit), width=2)
        c.text(348, 318, 'Blinks', 15, TEXT, bold=True, anchor='lm')
        c.text(410, 318, 'OpenAPI + x-blink, versioned', 11.5, MUTED, anchor='lm')
        c.text(614, 318, 'v' + version, 12, VIOLET, mono=True, bold=True, anchor='rm')
        c.arrow([(480, 280), (480, 295)], mix(EDGE, AMBER, lit(2)), 1.5, 6)
        va = lit(3)
        for k, label in enumerate(['logins', 'private Blink', 'journal']):
            vx = 334 + k * 99
            c.glow(vx, 356, 94, 50, PINK, va * 0.5, r=10)
            c.box(vx, 356, 94, 50, r=10, fill=mix(PANEL, (58, 30, 56), va), outline=mix(EDGE, PINK, va), width=1.5)
            # padlock
            px, py = vx + 47, 372
            c.d.arc(c._s([px - 5, py - 7, px + 5, py + 3]), 180, 360, fill=mix(DIM, PINK, va), width=2 * S)
            c.box(px - 7, py - 2, 14, 10, r=2, fill=mix(DIM, PINK, va), outline=None)
            c.text(px, 395, label, 11, mix(MUTED, TEXT, va), anchor='mm')
        # left nodes and links
        targets = [(334, 165), (334, 213), (334, 318)]
        for k, (x, y, kind, title, sub) in enumerate(left):
            a = lit(0) if k == 0 else lit(1)
            col = CYAN if k == 0 else VIOLET
            node(c, x, y, 236, 74, kind, title, sub, col, a)
            pts = [(x + 236, y + 37), (292, y + 37), (292, targets[k][1]), targets[k]]
            c.arrow(pts, mix(EDGE, col, a), 2, 7)
            if (k == 0 and step == 0) or (k > 0 and step == 1):
                packets(c, pts, t, col)
        # right nodes and links
        for k, (x, y, kind, title, sub, mono) in enumerate(right):
            a = lit(4) if k == 0 else lit(5)
            col = GREEN if k == 0 else BLUE
            node(c, x, y, 236, 66, kind, title, sub, col, a, mono_sub=mono)
            pts = [(626, 318), (660, 318), (660, y + 33), (x - 1, y + 33)]
            c.arrow(pts, mix(EDGE, col, a), 2, 7)
            if (k == 0 and step == 4) or (k > 0 and step == 5):
                packets(c, pts, t, col)
        # the web apps it talks to
        wa = max(lit(4), lit(5), lit(0))
        c.box(318, 452, 324, 52, r=14, fill=mix(PANEL, PANEL_HI, wa), outline=mix(EDGE, GREEN, wa * 0.8), width=2)
        icon(c, 'web', 334, 465, mix(MUTED, GREEN, wa))
        c.text(372, 470, 'Web apps & their APIs', 15, TEXT, bold=True, anchor='lm')
        c.text(372, 490, 'reviewed calls, as you, with your login', 11.5, MUTED, anchor='lm')
        link = [(480, 422), (480, 451)]
        c.line(link, mix(EDGE, GREEN, wa), 2)
        if step in (0, 4, 5):
            packets(c, link if step else link[::-1], t, GREEN if step else CYAN, count=2, r=3.5)
        n = min(step, len(steps) - 1)
        caption(c, n, len(steps), steps[n][0] if step < len(steps) else 'Learn once, use it everywhere: sessions, scripts and schedules',
                steps[n][2])
        frames.append(c.frame())
    return frames


# ---------------------------------------------------------------- 2. learning modes
TABS = ['On the way (default)', 'Guided recording', 'Assisted']


def tabs(c, active, accent):
    x = 28
    for k, label in enumerate(TABS):
        w = c.width(label, 14, bold=True) + 32
        on = k == active
        c.box(x, 74, w, 34, r=17, fill=mix(PANEL, accent, 0.18) if on else PANEL,
              outline=accent if on else EDGE, width=2 if on else 1.5)
        c.text(x + w / 2, 91, label, 14, TEXT if on else MUTED, bold=True, anchor='mm')
        x += w + 10


def on_the_way(c, t):
    cx, cy, rx, ry = 480, 312, 330, 150
    labels = [('1  Check the Blink', 'does it have this step?'),
              ('2  Missing? Record', 'the agent does the step'),
              ('3  Build / extend', 'propose + activate'),
              ('4  Test it', 'with your own task'),
              ('5  Continue', 'through the Blink'),
              ('6  Next gap?', 'repeat as often as needed')]
    angles = [-90 + k * 60 for k in range(6)]
    pos = [(cx + rx * math.cos(math.radians(a)), cy + ry * math.sin(math.radians(a))) for a in angles]
    # ring
    ring = [(cx + rx * math.cos(math.radians(a)), cy + ry * math.sin(math.radians(a))) for a in range(-90, 271, 4)]
    c.line(ring, EDGE, 2)
    # marker travels: loop 1 full (0..0.42), shortcut (0.42..0.58), loop 2 full (0.58..1)
    if t < 0.42:
        f, mode = t / 0.42, 'loop'
    elif t < 0.58:
        f, mode = (t - 0.42) / 0.16, 'short'
    else:
        f, mode = (t - 0.58) / 0.42, 'loop'
    active = int(f * 6) % 6 if mode == 'loop' else (0 if f < 0.5 else 4)
    version = 1 + (t > 0.42 * 2.5 / 6) + (t > 0.58 + 0.42 * 2.5 / 6)
    # shortcut chord 1 -> 5
    sa = ease(phase(t, 0.42, 0.47)) * (1 - ease(phase(t, 0.56, 0.6)))
    chord = [(pos[0][0] - 60, pos[0][1] + 28), (cx - 175, cy - 15), (pos[4][0] + 40, pos[4][1] - 28)]
    if sa > 0.02:
        c.arrow(chord, mix(BG_BOTTOM, GREEN, sa), 2.5, 8)
        c.text(cx - 140, cy - 52, 'step known? run it', 13, mix(BG_BOTTOM, GREEN, sa), bold=True, anchor='lm')
    # center
    c.box(cx - 120, cy - 2, 240, 76, r=16, fill=(16, 22, 42), outline=EDGE, width=1.5)
    c.text(cx, cy + 16, 'your task', 12, DIM, bold=True, anchor='mm')
    c.text(cx, cy + 38, '“archive my old orders”', 15, TEXT, anchor='mm')
    c.text(cx, cy + 59, f'Blink v0.{version}.0', 13, VIOLET, mono=True, bold=True, anchor='mm')
    cols = [CYAN, RED, AMBER, GREEN, GREEN, VIOLET]
    for k, (title, sub) in enumerate(labels):
        x, y = pos[k]
        on = 1.0 if k == active else 0.0
        w = 196
        c.glow(x - w / 2, y - 27, w, 54, cols[k], on * 0.9)
        c.box(x - w / 2, y - 27, w, 54, r=12, fill=mix(PANEL, PANEL_HI, on), outline=mix(EDGE, cols[k], 0.35 + 0.65 * on), width=2)
        c.text(x, y - 8, title, 15, mix(MUTED, TEXT, 0.5 + on / 2), bold=True, anchor='mm')
        c.text(x, y + 12, sub, 12, mix(DIM, MUTED, 0.4 + 0.6 * on), anchor='mm')
    if mode == 'short':
        x, y = along(chord, ease(f))
        c.dot(x, y, 7, GREEN)
    else:
        a = math.radians(-90 + f * 360)
        c.dot(cx + rx * math.cos(a), cy + ry * math.sin(a), 7, AMBER)
    texts = {0: 'The agent checks the Blink for each step of your task',
             1: 'Missing step: Blink records while the agent does it in the browser',
             2: 'The recording becomes a new Blink version, switched on at once',
             3: 'The new Blink is tested with your task: reads re-checked, writes never repeated',
             4: 'The rest of the task runs through the Blink',
             5: 'Another missing step? The cycle repeats, as often as needed'}
    if mode == 'short':
        return 'A step the Blink already knows simply runs through it'
    return texts[active]


def browser_mock(c, x, y, w, h, url, rec=0.0):
    c.box(x, y, w, h, r=12, fill=(240, 243, 250), outline=EDGE, width=1.5)
    c.d.rounded_rectangle(c._s([x, y, x + w, y + 30]), radius=12 * S, fill=(214, 220, 234))
    c.d.rectangle(c._s([x, y + 18, x + w, y + 30]), fill=(214, 220, 234))
    for k, col in enumerate([(248, 113, 113), (251, 191, 36), (52, 211, 153)]):
        c.dot(x + 16 + k * 14, y + 15, 4, col, halo=False)
    c.box(x + 64, y + 7, w - 80, 17, r=8, fill=(250, 251, 255), outline=None)
    c.text(x + 74, y + 15.5, url, 11, (90, 100, 125), mono=True, anchor='lm')
    if rec > 0:
        c.box(x + w - 66, y + 40, 54, 20, r=10, fill=mix((240, 243, 250), RED, rec), outline=None)
        c.dot(x + w - 53, y + 50, 3.5, (255, 255, 255), halo=False)
        c.text(x + w - 34, y + 50, 'REC', 10.5, (255, 255, 255), bold=True, anchor='mm')


def cursor(c, x, y, click=0.0):
    if click > 0:
        r = 6 + 14 * click
        c.d.ellipse(c._s([x - r, y - r, x + r, y + r]), outline=mix((240, 243, 250), CYAN, 1 - click), width=2 * S)
    pts = [(x, y), (x, y + 18), (x + 5, y + 13), (x + 9, y + 21), (x + 12, y + 19), (x + 8, y + 12), (x + 14, y + 12)]
    c.d.polygon([tuple(c._s(p)) for p in pts], fill=(20, 24, 40), outline=(255, 255, 255))


def orders_page(c, x, y, archived=0):
    rows = [('#1042', 'Desk lamp', 'delivered'), ('#1039', 'Headphones', 'delivered'),
            ('#1031', 'USB-C hub', 'delivered'), ('#1028', 'Notebook', 'delivered')]
    c.text(x, y, 'My orders', 15, (30, 36, 60), bold=True)
    for k, (no, item, state) in enumerate(rows):
        ry = y + 30 + k * 36
        gone = k < archived
        c.box(x, ry, 330, 30, r=6, fill=(226, 232, 244) if not gone else (236, 239, 246), outline=None)
        col = (40, 46, 70) if not gone else (160, 168, 188)
        c.text(x + 10, ry + 15, no, 12, col, mono=True, anchor='lm')
        c.text(x + 70, ry + 15, item, 12.5, col, anchor='lm')
        c.box(x + 252, ry + 5, 70, 20, r=10, fill=(99, 102, 241) if not gone else (196, 200, 214), outline=None)
        c.text(x + 287, ry + 15, 'archived' if gone else 'Archive', 11, (255, 255, 255), bold=True, anchor='mm')


def request_list(c, x, y, items, shown, accent=CYAN, title='captured by Blink'):
    c.box(x, y, 330, 300, r=14, fill=PANEL, outline=EDGE, width=1.5)
    c.text(x + 16, y + 22, title, 12, DIM, bold=True, anchor='lm')
    for k, (method, path) in enumerate(items[:shown]):
        ry = y + 44 + k * 34
        col = GREEN if method == 'GET' else AMBER
        c.box(x + 14, ry, 302, 28, r=8, fill=PANEL_HI, outline=None)
        c.text(x + 26, ry + 14, method, 11.5, col, bold=True, mono=True, anchor='lm')
        c.text(x + 76, ry + 14, path, 11.5, TEXT, mono=True, anchor='lm')


def guided(c, t):
    browser_mock(c, 40, 128, 420, 290, 'shop.example.net/orders', rec=0.6 + 0.4 * math.sin(t * 18))
    archived = int(clamp((t - 0.25) / 0.45) * 3)
    orders_page(c, 64, 182, archived)
    targets = [(64 + 287, 182 + 30 + k * 36 + 15) for k in range(3)]
    if t < 0.25:
        x, y = along([(300, 400), targets[0]], ease(t / 0.25))
        click = 0
    elif t < 0.7:
        k = min(2, int((t - 0.25) / 0.15))
        f = ((t - 0.25) % 0.15) / 0.15
        start = targets[k - 1] if k else targets[0]
        x, y = along([start, targets[k]], ease(f * 2)) if k else targets[0]
        click = clamp((f - 0.6) / 0.4)
    else:
        x, y = targets[2][0] + 40 * ease((t - 0.7) / 0.3), targets[2][1] + 60 * ease((t - 0.7) / 0.3)
        click = 0
    cursor(c, x, y, click)
    items = [('GET', '/api/me'), ('GET', '/api/orders?page=1'), ('POST', '/api/orders/1042/archive'),
             ('POST', '/api/orders/1039/archive'), ('POST', '/api/orders/1031/archive'), ('GET', '/api/orders/1031')]
    request_list(c, 500, 128, items, 1 + int(clamp(t / 0.75) * 5.99))
    done = ease(phase(t, 0.78, 0.9))
    if done:
        c.box(500, 438, 420, 60, r=14, fill=mix(PANEL, (24, 60, 52), done), outline=mix(EDGE, GREEN, done), width=2)
        c.check(518, 458, 18)
        c.text(546, 458, 'Blink “shop-example-net” v0.1.0', 15, TEXT, bold=True, anchor='lm')
        c.text(546, 480, 'listOrders · archiveOrder · getOrder · whoAmI', 12, MUTED, mono=True, anchor='lm')
    return ('You show the workflow once; Blink records the requests behind each click'
            if t < 0.78 else 'Say “finished”: Blink proposes and switches on the new Blink')


def assisted(c, t):
    steps = [('list my open orders', 'known', 0.05), ('archive the delivered ones', 'known', 0.18),
             ('export invoices as PDF', 'missing', 0.32), ('email the summary', 'known', 0.86)]
    c.box(40, 128, 400, 300, r=14, fill=PANEL, outline=EDGE, width=1.5)
    c.text(58, 150, 'AGENT · your task', 12, DIM, bold=True, anchor='lm')
    learned = t > 0.74
    for k, (label, state, at) in enumerate(steps):
        ry = 172 + k * 58
        a = ease(phase(t, at, at + 0.06))
        if a <= 0:
            continue
        missing = state == 'missing' and not learned
        col = RED if missing else GREEN
        c.box(56, ry, 368, 46, r=10, fill=mix(PANEL, PANEL_HI, a), outline=mix(EDGE, col, a * 0.8), width=1.5)
        c.text(76, ry + 23, label, 14, TEXT, anchor='lm')
        if missing:
            c.cross(394, ry + 17, 12)
            c.text(386, ry + 23, 'no operation', 11, RED, anchor='rm')
        else:
            c.check(392, ry + 15, 16)
            if state == 'missing':
                c.text(384, ry + 23, 'learned', 11, GREEN, anchor='rm')
    hand = ease(phase(t, 0.4, 0.46))
    if hand:
        browser_mock(c, 480, 128, 440, 300, 'shop.example.net/invoices', rec=hand)
        c.text(504, 186, 'Invoices', 15, (30, 36, 60), bold=True)
        for k in range(3):
            ry = 214 + k * 40
            c.box(504, ry, 392, 32, r=6, fill=(226, 232, 244), outline=None)
            c.text(516, ry + 16, f'INV-20{31 + k}', 12, (40, 46, 70), mono=True, anchor='lm')
            c.box(806, ry + 6, 80, 20, r=10, fill=(99, 102, 241), outline=None)
            c.text(846, ry + 16, 'PDF', 11, (255, 255, 255), bold=True, anchor='mm')
        f = phase(t, 0.46, 0.68)
        x, y = along([(640, 400), (846, 230)], ease(f * 1.4))
        cursor(c, x, y, clamp((f - 0.75) / 0.25))
        c.box(504, 356, 392, 54, r=10, fill=(255, 247, 225), outline=None)
        c.text(520, 374, 'Your turn: show this one step,', 13, (120, 80, 10), bold=True, anchor='lm')
        c.text(520, 394, 'then the agent carries on.', 13, (120, 80, 10), anchor='lm')
    if learned:
        a = ease(phase(t, 0.74, 0.8))
        c.box(480, 444, 440, 56, r=14, fill=mix(PANEL, (24, 60, 52), a), outline=mix(EDGE, GREEN, a), width=2)
        c.check(498, 463, 18)
        c.text(526, 472, 'exportInvoicePdf added · Blink v0.2.0', 14, TEXT, bold=True, anchor='lm')
    if t < 0.4:
        return 'The agent works through your task with the Blink until a step is missing'
    if t < 0.74:
        return 'Assisted: you demonstrate just the missing step in the recording tab'
    return 'Blink learns it, tests it, and the agent finishes the task'


def learning():
    scenes = [(on_the_way, 210, CYAN), (guided, 120, AMBER), (assisted, 140, PINK)]
    frames = []
    total_scenes = len(scenes)
    clock = 0
    for idx, (draw, n, accent) in enumerate(scenes):
        for i in range(n):
            t = i / (n - 1)
            c = Canvas()
            header(c, 'How Blink learns', 'record once, or learn on the way', clock / FPS)
            tabs(c, idx, accent)
            text = draw(c, t)
            caption(c, idx, total_scenes, text, accent)
            frames.append(c.frame())
            clock += 1
    return frames


# ---------------------------------------------------------------- 3. use cases
def case_panel(c, title, body, prompt, accent, a):
    c.text(40, 132, title, 24, TEXT, bold=True)
    c.wrap(40, 176, body, 330, 15, MUTED)
    c.box(40, 300, 340, 76, r=14, fill=mix(PANEL, accent, 0.12), outline=accent, width=1.5)
    c.text(58, 318, 'YOU', 11, accent, bold=True, anchor='lm')
    shown = prompt[:int(len(prompt) * clamp(a * 5))]
    c.wrap(58, 334, '“' + shown + ('”' if len(shown) == len(prompt) else ''), 300, 15, TEXT)


def timesheet(c, t):
    case_panel(c, 'Automate your business apps', 'Timesheets, CRM, ticketing and HR portals: the web apps you already sign in to every week.',
               'Fill in my week like last week.', CYAN, t)
    x, y = 420, 128
    c.box(x, y, 500, 330, r=14, fill=(240, 243, 250), outline=EDGE)
    c.text(x + 20, y + 26, 'Timesheet · week 46', 15, (30, 36, 60), bold=True, anchor='lm')
    days = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri']
    for k, day in enumerate(days):
        ry = y + 56 + k * 46
        c.text(x + 22, ry + 18, day, 13, (60, 68, 96), bold=True, anchor='lm')
        c.box(x + 80, ry, 260, 36, r=8, fill=(255, 255, 255), outline=(214, 220, 234), width=1)
        c.box(x + 352, ry, 64, 36, r=8, fill=(255, 255, 255), outline=(214, 220, 234), width=1)
        a = ease(phase(t, 0.25 + k * 0.1, 0.33 + k * 0.1))
        if a:
            c.text(x + 92, ry + 18, 'Development · Project Atlas', 12.5, mix((255, 255, 255), (40, 46, 70), a), anchor='lm')
            c.text(x + 384, ry + 18, '8:00', 12.5, mix((255, 255, 255), (40, 46, 70), a), mono=True, anchor='mm')
            c.check(x + 438, ry + 10, 16, mix((255, 255, 255), GREEN, a))
    a = ease(phase(t, 0.8, 0.9))
    if a:
        c.box(420, 474, 500, 44, r=12, fill=mix(PANEL, (24, 60, 52), a), outline=mix(EDGE, GREEN, a), width=2)
        c.check(438, 488, 16)
        c.text(464, 496, 'verified: 5 entries, 40:00 h — read back from the app', 13.5, TEXT, anchor='lm')


def no_api(c, t):
    case_panel(c, 'Apps without an API or MCP', 'No public API, no MCP server? Blink finds the calls behind the pages and turns them into tools.',
               'Approve the open invoices under 500 €.', VIOLET, t)
    browser_mock(c, 420, 128, 230, 200, 'erp.acme.example')
    for k in range(4):
        c.box(436, 178 + k * 34, 198, 24, r=5, fill=(226, 232, 244), outline=None)
    tools = [('listInvoices', 'read', GREEN), ('getInvoice', 'read', GREEN),
             ('approveInvoice', 'write', AMBER), ('exportReport', 'read', GREEN)]
    for k, (name, effect, col) in enumerate(tools):
        a = ease(phase(t, 0.25 + k * 0.12, 0.35 + k * 0.12))
        ry = 140 + k * 50
        pts = [(650, 228), (690, 228), (690, ry + 18), (712, ry + 18)]
        c.arrow(pts, mix(EDGE, VIOLET, a), 1.8, 6)
        if 0 < a < 1:
            packets(c, pts, t * 3, VIOLET, count=1)
        c.glow(714, ry, 206, 38, VIOLET, a * 0.5, r=10)
        c.box(714, ry, 206, 38, r=10, fill=mix(PANEL, PANEL_HI, a), outline=mix(EDGE, VIOLET, a), width=1.5)
        c.text(728, ry + 19, name, 13, mix(DIM, TEXT, a), mono=True, bold=True, anchor='lm')
        c.box(850, ry + 9, 58, 20, r=10, fill=mix(PANEL, col, a * 0.25), outline=None)
        c.text(879, ry + 19, effect, 11, mix(DIM, col, a), bold=True, anchor='mm')
    a = ease(phase(t, 0.8, 0.9))
    if a:
        c.box(420, 474, 500, 44, r=12, fill=mix(PANEL, (40, 32, 76), a), outline=mix(EDGE, VIOLET, a), width=2)
        c.check(438, 488, 16, VIOLET)
        c.text(464, 496, 'reviewed tools — usable from your session or the CLI', 13.5, TEXT, anchor='lm')


def extraction(c, t):
    case_panel(c, 'Collect web data for yourself', 'Turn pages you can already see into clean JSON or CSV for your own use, within each site’s terms.',
               'Track the prices on my watch list every morning.', AMBER, t)
    x, y = 420, 128
    c.box(x, y, 500, 156, r=14, fill=(240, 243, 250), outline=EDGE)
    for k, (name, price) in enumerate([('Trail shoes', '89.90'), ('Rain jacket', '129.00'), ('Head lamp', '34.50')]):
        cx = x + 18 + k * 162
        c.box(cx, y + 18, 140, 120, r=10, fill=(255, 255, 255), outline=(214, 220, 234), width=1)
        c.box(cx + 12, y + 30, 116, 50, r=6, fill=(226, 232, 244), outline=None)
        c.text(cx + 12, y + 96, name, 12.5, (40, 46, 70), bold=True, anchor='lm')
        c.text(cx + 12, y + 118, '€ ' + price, 12.5, (99, 102, 241), bold=True, anchor='lm')
    c.box(x, y + 172, 500, 200, r=14, fill=(12, 16, 30), outline=EDGE)
    cmd = '$ blink shop list-prices --output json'
    shown = cmd[:int(len(cmd) * clamp(t / 0.35))]
    c.text(x + 18, y + 198, shown, 13, GREEN, mono=True, anchor='lm')
    rows = ['{"item": "Trail shoes", "price": 89.90}', '{"item": "Rain jacket", "price": 129.00}',
            '{"item": "Head lamp", "price": 34.50}']
    for k, row in enumerate(rows):
        a = ease(phase(t, 0.42 + k * 0.08, 0.5 + k * 0.08))
        c.text(x + 18, y + 228 + k * 24, row, 13, mix((12, 16, 30), TEXT, a), mono=True, anchor='lm')
    a = ease(phase(t, 0.75, 0.85))
    c.text(x + 18, y + 320, 'saved routine “prices” · runs daily at 07:00', 13, mix((12, 16, 30), AMBER, a), mono=True, anchor='lm')


def to_script(c, t):
    case_panel(c, 'From a chat to a script', 'Do it once in Claude Code or Codex. Blink keeps the verified steps as a routine you can run from the CLI or a schedule.',
               'blink acme for me', GREEN, t)
    c.box(420, 128, 500, 140, r=14, fill=PANEL, outline=EDGE, width=1.5)
    icon(c, 'chat', 438, 142, GREEN)
    c.text(474, 155, 'Claude Code', 13, MUTED, bold=True, anchor='lm')
    a = ease(phase(t, 0.15, 0.25))
    c.box(438, 182, 300, 34, r=12, fill=mix(PANEL, (26, 46, 70), a), outline=None)
    c.text(452, 199, 'Should I do this by the last rules?', 13, mix(PANEL, TEXT, a), anchor='lm')
    a2 = ease(phase(t, 0.28, 0.36))
    c.box(760, 222, 140, 32, r=12, fill=mix(PANEL, (30, 70, 60), a2), outline=None)
    c.text(830, 238, 'Yes', 13, mix(PANEL, TEXT, a2), bold=True, anchor='mm')
    c.box(420, 284, 500, 172, r=14, fill=(12, 16, 30), outline=EDGE)
    icon(c, 'terminal', 438, 296, BLUE)
    c.text(474, 309, 'terminal · cron', 13, MUTED, bold=True, anchor='lm')
    cmd = '$ blink routine run weekly-report'
    shown = cmd[:int(len(cmd) * clamp((t - 0.4) / 0.2))]
    c.text(438, 344, shown, 13, GREEN, mono=True, anchor='lm')
    lines = [('step 1/3 listTickets', 0.62), ('step 2/3 exportReport', 0.68), ('step 3/3 sendSummary', 0.74)]
    for k, (ln, at) in enumerate(lines):
        a = ease(phase(t, at, at + 0.05))
        if a:
            c.text(438, 372 + k * 22, ln, 12.5, mix((12, 16, 30), TEXT, a), mono=True, anchor='lm')
            c.check(660, 364 + k * 22, 13, mix((12, 16, 30), GREEN, a))
    a = ease(phase(t, 0.82, 0.9))
    if a:
        c.box(420, 474, 500, 44, r=12, fill=mix(PANEL, (24, 60, 52), a), outline=mix(EDGE, GREEN, a), width=2)
        icon(c, 'clock', 436, 483, GREEN)
        c.text(472, 496, 'scheduled: every Monday 08:00 — same verified steps', 13.5, TEXT, anchor='lm')


def use_cases():
    scenes = [(timesheet, CYAN, 'Business apps: your weekly busywork, done and verified'),
              (no_api, VIOLET, 'No API or MCP needed: Blink learns the app itself'),
              (extraction, AMBER, 'Scrape for private use: pages become clean, structured data'),
              (to_script, GREEN, 'Chat once, then run it as a script or on a schedule')]
    frames, clock, n = [], 0, 96
    for idx, (draw, accent, text) in enumerate(scenes):
        for i in range(n):
            t = i / (n - 1)
            c = Canvas()
            header(c, 'What you can do with Web Blink', 'automate the web apps you use', clock / FPS)
            draw(c, t)
            caption(c, idx, len(scenes), text, accent)
            frames.append(c.frame())
            clock += 1
    return frames


# ---------------------------------------------------------------- 4. the budget example, from its real recording
STAGES = ['Record', 'Blink', 'Run', 'Script', 'Data']
SHOT = ROOT / 'docs/media/src/usaspending-agency.jpg'  # Blink's own screenshot from the recording
BUDGET_ROWS = [('Department of the Treasury', '$3,982,061,714,338', '24.61%'),
               ('Department of Health and Human Services', '$3,709,407,922,072', '22.92%'),
               ('Department of Defense', '$2,617,506,434,639', '16.18%'),
               ('Social Security Administration', '$1,725,080,621,637', '10.66%'),
               ('Department of Homeland Security', '$645,946,447,461', '3.99%')]
_shot = None


def stepper(c, active, accent):
    x0, gap = 40, 176
    for k, label in enumerate(STAGES):
        x = x0 + k * gap
        on, done = k == active, k < active
        col = accent if on else (GREEN if done else DIM)
        if k:
            start = x - gap + 30 + c.width(STAGES[k - 1], 15, bold=True) + 12
            c.line([(start, 96), (x - 6, 96)], GREEN if done or on else EDGE, 2)
        c.dot(x + 11, 96, 11 if on else 9, col, halo=on)
        if done:
            c.check(x + 5, 89, 12, (12, 20, 30))
        else:
            c.text(x + 11, 96, str(k + 1), 12, (12, 20, 30) if on else TEXT, bold=True, anchor='mm')
        c.text(x + 30, 96, label, 15, TEXT if on else MUTED, bold=True, anchor='lm')


def terminal(c, x, y, w, h, title):
    c.box(x, y, w, h, r=12, fill=(10, 14, 26), outline=EDGE, width=1.5)
    for k, col in enumerate([(248, 113, 113), (251, 191, 36), (52, 211, 153)]):
        c.dot(x + 16 + k * 14, y + 15, 4, col, halo=False)
    c.text(x + 64, y + 15, title, 11.5, MUTED, mono=True, anchor='lm')


def typed(text, t):
    return text[:int(len(text) * clamp(t))]


def budget_table(c, x, y, n, size=12.5):
    c.text(x, y, 'Agency', size, MUTED, mono=True, bold=True, anchor='lm')
    c.text(x + 610, y, 'Budgetary resources', size, MUTED, mono=True, bold=True, anchor='rm')
    c.text(x + 700, y, 'Share', size, MUTED, mono=True, bold=True, anchor='rm')
    for k, (agency, amount, share) in enumerate(BUDGET_ROWS[:n]):
        ry = y + 22 + k * 21
        c.text(x, ry, agency, size, TEXT, mono=True, anchor='lm')
        c.text(x + 610, ry, amount, size, GREEN, mono=True, anchor='rm')
        c.text(x + 700, ry, share, size, AMBER, mono=True, anchor='rm')


def stage_record(c, t):
    global _shot
    if _shot is None:
        from PIL import Image
        _shot = Image.open(SHOT).convert('RGB').resize((int(560 * S), int(560 * 469 / 960 * S)), Image.LANCZOS)
    browser_mock(c, 28, 128, 572, 330, 'www.usaspending.gov/agency', rec=0.6 + 0.4 * math.sin(t * 20))
    c.img.paste(_shot.crop((0, 0, _shot.width, min(_shot.height, int(292 * S)))), (int(34 * S), int(160 * S)))
    reqs = [('GET', 'www.usaspending.gov/agency', False), ('POST', 'google-analytics …/collect', None),
            ('GET', 'api …/references/glossary/', False), ('POST', 'crazyegg …/clock', None),
            ('GET', 'api …/references/toptier_agencies/', True)]
    c.box(616, 128, 316, 330, r=14, fill=PANEL, outline=EDGE, width=1.5)
    c.text(632, 150, 'CAPTURED BY BLINK', 12, DIM, bold=True, anchor='lm')
    shown = 1 + int(clamp(t / 0.6) * 4.99)
    for k, (method, path, key) in enumerate(reqs[:shown]):
        ry = 172 + k * 46
        a = 1.0 if key else (0.45 if key is None else 0.75)
        c.box(630, ry, 288, 38, r=8, fill=mix(PANEL, (24, 60, 52), 1 if key else 0), outline=GREEN if key else None,
              width=2)
        c.text(642, ry + 19, method, 11.5, mix(PANEL, GREEN if method == 'GET' else AMBER, a), bold=True, mono=True, anchor='lm')
        c.text(690, ry + 19, path, 11, mix(PANEL, TEXT, a), mono=True, anchor='lm')
    if shown == 5:
        c.arrow([(774, 428), (774, 416)], GREEN, 2, 6)
        c.text(774, 440, 'the data behind the table', 12.5, GREEN, bold=True, anchor='mm')
    return 'You (or the agent) open the Agency Profiles page; Blink records the calls behind it'


def stage_blink(c, t):
    terminal(c, 28, 128, 904, 330, 'learn_propose · activate')
    c.text(48, 168, typed('$ blink learn propose  # only the toptier_agencies endpoint', t / 0.3), 13, GREEN, mono=True, anchor='lm')
    a = ease(phase(t, 0.32, 0.45))
    if a:
        c.glow(70, 196, 820, 170, VIOLET, a * 0.6)
        c.box(70, 196, 820, 170, r=14, fill=mix((10, 14, 26), (40, 32, 76), a), outline=mix(EDGE, VIOLET, a), width=2)
        c.text(96, 226, 'Blink  usaspending-gov  v0.1.0', 18, TEXT, bold=True, anchor='lm')
        c.text(96, 262, 'agency-budgets', 14, VIOLET, mono=True, bold=True, anchor='lm')
        c.text(260, 262, 'read · GET api.usaspending.gov/api/v2/references/toptier_agencies/', 12.5, MUTED, mono=True, anchor='lm')
        c.text(96, 292, '--sort  --order   defaults from the page: share of total budget, largest first', 12.5, MUTED, mono=True, anchor='lm')
        for k, (label, at) in enumerate([('public: works without any login (checked)', 0.55), ('switched on', 0.7)]):
            b = ease(phase(t, at, at + 0.08))
            if b:
                c.check(96, 316 + k * 26, 14, mix((10, 14, 26), GREEN, b))
                c.text(122, 324 + k * 26, label, 13, mix((10, 14, 26), TEXT, b), anchor='lm')
    return 'Blink turns that one call into a reviewed, versioned Blink and switches it on'


def stage_run(c, t):
    terminal(c, 28, 128, 904, 330, 'terminal')
    c.text(48, 168, typed('$ blink usaspending-gov agency-budgets', t / 0.3), 14, GREEN, mono=True, anchor='lm')
    rows = int(clamp((t - 0.35) / 0.4) * 5.99)
    if t > 0.35:
        budget_table(c, 48, 206, rows)
    return 'You run one command and get every agency’s budget, exactly as the website shows it'


def stage_script(c, t):
    terminal(c, 28, 128, 904, 330, 'verified → routine → script')
    lines = [('verified: Department of the Treasury is first, as recorded', GREEN, 0.0, True),
             ('routine saved: budget  (phrase "federal budget by agency")', GREEN, 0.18, True),
             ('~/.web-blink/scripts/bundled--usaspending-gov/budget-14bd304b42a0.py', AMBER, 0.34, False)]
    for k, (text, col, at, tick) in enumerate(lines):
        a = ease(phase(t, at, at + 0.1))
        if a:
            if tick:
                c.check(48, 161 + k * 28, 13, mix((10, 14, 26), col, a))
            c.text(70 if tick else 48, 168 + k * 28, text, 13, mix((10, 14, 26), col, a), mono=True, anchor='lm')
    code = ["SERVICE = 'bundled:usaspending-gov'", "ROUTINE = 'budget'",
            "for index in range(COUNT):", "    blink routine step --input {service, name, runId, index}",
            "    # read-only; each run is journaled and verified"]
    a = ease(phase(t, 0.5, 0.62))
    if a:
        c.box(48, 262, 864, 176, r=10, fill=mix((10, 14, 26), (20, 26, 48), a), outline=None)
        for k, ln in enumerate(code):
            c.text(66, 286 + k * 30, ln, 13, mix((10, 14, 26), MUTED if ln.strip().startswith('#') else TEXT, a), mono=True, anchor='lm')
    return 'After a verified run, Blink saves the steps as a routine and writes the script for you'


def stage_data(c, t):
    terminal(c, 28, 128, 904, 330, 'terminal · any time, or from cron')
    c.text(48, 168, typed('$ blink routine run budget', t / 0.25), 14, GREEN, mono=True, anchor='lm')
    if t > 0.28:
        c.text(48, 196, 'Should I do this according to the last rules? [yes/no] yes', 12.5, MUTED, mono=True, anchor='lm')
    if t > 0.38:
        c.text(48, 222, 'Routine completed and verified (1 step)', 12.5, GREEN, mono=True, anchor='lm')
        budget_table(c, 48, 254, int(clamp((t - 0.42) / 0.3) * 5.99))
    a = ease(phase(t, 0.8, 0.9))
    if a:
        c.text(48, 440, '0 7 * * 1  blink routine run budget --confirmed --output json > budget.json',
               12, mix((10, 14, 26), AMBER, a), mono=True, anchor='lm')
    return 'Now the data is one command away, every time'


def budget():
    stages = [(stage_record, 105, CYAN), (stage_blink, 90, VIOLET), (stage_run, 90, BLUE),
              (stage_script, 90, AMBER), (stage_data, 120, GREEN)]
    frames, clock = [], 0
    for idx, (draw, n, accent) in enumerate(stages):
        for i in range(n):
            t = i / (n - 1)
            c = Canvas()
            header(c, 'How the budget Blink was made', 'a real recording of usaspending.gov', clock / FPS)
            stepper(c, idx, accent)
            text = draw(c, t)
            caption(c, idx, len(stages), text, accent)
            frames.append(c.frame())
            clock += 1
    return frames


# ---------------------------------------------------------------- encoding
def encode(frames, name):
    OUT.mkdir(parents=True, exist_ok=True)
    target = OUT / f'{name}.gif'
    with tempfile.TemporaryDirectory(prefix='blink-gif-') as tmp:
        for k, frame in enumerate(frames):
            frame.save(Path(tmp) / f'f{k:04d}.png')
        filters = ('split[a][b];[a]palettegen=max_colors=160:stats_mode=full[p];'
                   '[b][p]paletteuse=dither=sierra2_4a:diff_mode=rectangle')
        subprocess.run(['ffmpeg', '-loglevel', 'error', '-y', '-framerate', str(FPS), '-i', str(Path(tmp) / 'f%04d.png'),
                        '-vf', filters, '-loop', '0', str(target)], check=True)
        preview = Path(tempfile.gettempdir()) / 'blink-gif-preview'
        preview.mkdir(exist_ok=True)
        for k in range(0, len(frames), max(1, len(frames) // 8)):
            frames[k].save(preview / f'{name}-{k:04d}.png')
    print(f'{target.relative_to(ROOT)}: {len(frames)} frames, {target.stat().st_size / 1e6:.2f} MB')


BUILDERS = {'architecture': architecture, 'learning': learning, 'use-cases': use_cases, 'budget': budget}


def main(names):
    if not shutil.which('ffmpeg'):
        sys.exit('ffmpeg is required')
    for name in names or BUILDERS:
        encode(BUILDERS[name](), name)


if __name__ == '__main__':
    main(sys.argv[1:])
