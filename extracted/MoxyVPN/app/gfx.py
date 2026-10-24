"""Pillow-rendered artwork for MoxyVPN: aurora / glass background, power button, header icons.

Tk's canvas has no anti-aliasing and no transparency, so everything "pretty" is drawn here at a higher
resolution with Pillow, scaled down, and handed to Tk as PNG data.  Nothing in this file touches Tk, so
it can run in a background thread.
"""
import base64
import io
import math
import random

from PIL import Image, ImageChops, ImageDraw, ImageFilter

# ------------------------------------------------------------------ colour helpers


def _c(x):
    if isinstance(x, str):
        return tuple(int(x[i:i + 2], 16) for i in (1, 3, 5))
    return tuple(x)


def _m(a, b, t):
    a, b = _c(a), _c(b)
    return tuple(round(x + (y - x) * t) for x, y in zip(a, b))


def is_dark(pal):
    r, g, b = _c(pal["BG"])
    return (0.299 * r + 0.587 * g + 0.114 * b) < 128


def b64png(img):
    buf = io.BytesIO()
    img.save(buf, "PNG", compress_level=1)
    return base64.b64encode(buf.getvalue())


def _vgrad(size, c1, c2):
    """Vertical gradient, c1 at the top, c2 at the bottom."""
    m = Image.linear_gradient("L").resize(size, Image.BILINEAR)
    return Image.composite(Image.new("RGB", size, _c(c2)), Image.new("RGB", size, _c(c1)), m)


def _aa_mask(size, draw_fn, S=4):
    """Anti-aliased 'L' mask: draw_fn(draw, S) paints white on a S-times bigger canvas."""
    big = Image.new("L", (size[0] * S, size[1] * S), 0)
    draw_fn(ImageDraw.Draw(big), S)
    return big.resize(size, Image.LANCZOS)


def _round_mask(size, r):
    return _aa_mask(size, lambda d, S: d.rounded_rectangle(
        (0, 0, size[0] * S - 1, size[1] * S - 1), r * S, fill=255))


def _scale_alpha(img, k):
    r, g, b, a = img.split()
    return Image.merge("RGBA", (r, g, b, a.point(lambda v: int(v * k))))


# ------------------------------------------------------------------ background
ORANGE, AMBER, INDIGO, MAGENTA, TEAL = (255, 122, 26), (255, 179, 71), (86, 76, 220), (190, 60, 140), (24, 183, 201)


def _blobs(mode, pal, w, h, hx, hy, dark):
    green = _c(pal["GREEN"])
    k = 0.62 if dark else 0.42
    if mode == "on":
        spec = [(hx, hy, 230, 210, green, .55), (hx + 150, hy - 50, 200, 170, TEAL, .42),
                (70, h - 110, 210, 160, (70, 80, 220), .30)]
    elif mode == "connecting":
        spec = [(hx, hy, 220, 200, ORANGE, .55), (hx + 140, hy + 90, 180, 150, AMBER, .30),
                (80, h - 100, 200, 150, (190, 60, 100), .22)]
    else:
        spec = [(hx - 130, hy - 70, 170, 150, ORANGE, .32), (hx + 150, hy + 30, 190, 170, INDIGO, .38),
                (60, h - 120, 200, 160, MAGENTA, .18), (w - 40, h - 60, 170, 140, (40, 90, 200), .16)]
    return [(cx, cy, rx, ry, col, a * k) for cx, cy, rx, ry, col, a in spec]


def background(w, h, pal, mode, hero, cards):
    """Full-window background: gradient + aurora blobs + radar rings + constellation + glass cards.

    hero  = (x, y) centre of the power button; cards = [(x1, y1, x2, y2, radius), ...]
    """
    dark = is_dark(pal)
    hx, hy = hero
    bg = _c(pal["BG"])
    top = _m(bg, (44, 30, 74), .85) if dark else _m(bg, (255, 232, 214), .8)
    base = _vgrad((w, h), top, bg)

    # aurora blobs (computed at 1/4 size, then scaled up: smooth and fast)
    sw, sh = w // 4, h // 4
    small = base.resize((sw, sh), Image.BILINEAR)
    for cx, cy, rx, ry, col, a in _blobs(mode, pal, w, h, hx, hy, dark):
        m = Image.new("L", (sw, sh), 0)
        ImageDraw.Draw(m).ellipse(((cx - rx) / 4, (cy - ry) / 4, (cx + rx) / 4, (cy + ry) / 4),
                                  fill=min(255, int(255 * a)))
        m = m.filter(ImageFilter.GaussianBlur(max(rx, ry) / 4 * .55))
        small = Image.composite(Image.new("RGB", (sw, sh), col), small, m)
    base = small.resize((w, h), Image.BICUBIC)

    # fine grain, hides gradient banding
    noise = Image.effect_noise((w, h), 26).point(lambda v: 128 + (v - 128) // 4)
    base = Image.blend(base, Image.merge("RGB", (noise, noise, noise)), .035 if dark else .02)

    # radar rings + constellation (drawn at 2x for smooth lines)
    S = 2
    ov = Image.new("RGBA", (w * S, h * S), (0, 0, 0, 0))
    d = ImageDraw.Draw(ov)
    ink = (255, 255, 255) if dark else (70, 80, 130)
    tint = {"on": _c(pal["GREEN"]), "connecting": AMBER, "off": ink}[mode]
    ring_col = _m(ink, tint, .55)
    for i, r in enumerate((98, 138, 182, 232, 288)):
        a = int((46 - i * 7) * (1 if dark else .8))
        d.ellipse(((hx - r) * S, (hy - r) * S, (hx + r) * S, (hy + r) * S),
                  outline=ring_col + (max(a, 8),), width=S)
    rnd = random.Random(11)
    pts = []
    for _ in range(40):
        x, y = rnd.uniform(8, w - 8), rnd.uniform(6, hy + 130)
        dist = math.hypot(x - hx, y - hy)
        fade = min(1.0, max(0.0, (dist - 84) / 150)) * min(1.0, max(0.0, (hy + 150 - y) / 150))
        pts.append((x, y, fade))
    node_col = _m(ink, tint, .35)
    for i, (x1, y1, f1) in enumerate(pts):
        for x2, y2, f2 in pts[i + 1:]:
            dist = math.hypot(x1 - x2, y1 - y2)
            if dist < 92:
                a = int(46 * (1 - dist / 92) * min(f1, f2) * (1 if dark else .8))
                if a > 3:
                    d.line((x1 * S, y1 * S, x2 * S, y2 * S), fill=node_col + (a,), width=S)
    for x, y, f in pts:
        if f > .05:
            r = 1.6 + 1.6 * f
            d.ellipse(((x - r) * S, (y - r) * S, (x + r) * S, (y + r) * S),
                      fill=node_col + (int(150 * f),))
    ov = ov.resize((w, h), Image.LANCZOS)
    base = Image.alpha_composite(base.convert("RGBA"), ov).convert("RGB")

    # glass cards: blurred backdrop + tint + soft shadow + hairline border + top sheen
    card_col = _c(pal["CARD"])
    for x1, y1, x2, y2, r in cards:
        cw, ch = x2 - x1, y2 - y1
        mask = _round_mask((cw, ch), r)
        sh_col = (0, 0, 0) if dark else (60, 70, 130)
        pad = 36
        sm = Image.new("L", (cw + 2 * pad, ch + 2 * pad), 0)
        sm.paste(mask.point(lambda v: int(v * (.45 if dark else .16))), (pad, pad + 7))
        sm = sm.filter(ImageFilter.GaussianBlur(13))
        base.paste(Image.new("RGB", sm.size, sh_col), (x1 - pad, y1 - pad), sm)
        region = base.crop((x1, y1, x2, y2)).filter(ImageFilter.GaussianBlur(10))
        glass = Image.blend(region, Image.new("RGB", (cw, ch), card_col), .66 if dark else .74)
        base.paste(glass, (x1, y1), mask)
        S4 = 4
        edge = Image.new("L", (cw * S4, ch * S4), 0)
        ed = ImageDraw.Draw(edge)
        ed.rounded_rectangle((0, 0, cw * S4 - 1, ch * S4 - 1), r * S4, outline=255, width=S4)
        edge = edge.resize((cw, ch), Image.LANCZOS)
        border = _c(pal["BORDER"])
        base.paste(Image.new("RGB", (cw, ch), border), (x1, y1), edge.point(lambda v: int(v * .85)))
        sheen = Image.linear_gradient("L").transpose(Image.FLIP_TOP_BOTTOM).resize((cw, ch))
        sheen = ImageChops.multiply(edge, sheen.point(lambda v: int(v * (.30 if dark else .0))))
        base.paste(Image.new("RGB", (cw, ch), (255, 255, 255)), (x1, y1), sheen)
    return base


# ------------------------------------------------------------------ power button
SIZE = 300          # px of the finished hero image
FRAMES = {"off": 1, "connecting": 24, "on": 36}


class HeroArt:
    """Renders the big power button. Static parts are cached, so each animation frame is cheap."""

    def __init__(self, pal, S=3):
        self.pal, self.S = pal, S
        self.D = SIZE * S
        self.dark = is_dark(pal)
        self._body, self._glow = {}, {}
        self.R_o, self.R_i = 68 * S, 61 * S
        self.cx = self.cy = self.D // 2

    # ---- colours per state
    def colors(self, state):
        p = self.pal
        if state == "on":
            g = _c(p["GREEN"])
            icon = (_m(g, (255, 255, 255), .55), g) if self.dark else (g, _m(g, (0, 40, 25), .35))
            return dict(rim=(_m(g, (255, 255, 255), .45), _m(g, (0, 40, 30), .40)),
                        icon=icon, glow=g, halo=g)
        if state == "connecting":
            a, a2 = _c(p["ACCENT"]), _c(p["ACCENT2"])
            return dict(rim=(_m(p["BORDER"], a2, .40), _m(p["BORDER"], a, .25)),
                        icon=(a2, a), glow=a, halo=a)
        return dict(rim=(_m(p["BORDER"], p["TEXT"], .28), _m(p["BORDER"], p["BG"], .30)),
                    icon=(_m(p["MUTED"], p["TEXT"], .25), p["DIM"]), glow=None, halo=None)

    # ---- helpers
    def _disc_mask(self, r):
        D, cx, cy = self.D, self.cx, self.cy
        m = Image.new("L", (D, D), 0)
        ImageDraw.Draw(m).ellipse((cx - r, cy - r, cx + r, cy + r), fill=255)
        return m

    def _icon_mask(self):
        S, D, cx, cy = self.S, self.D, self.cx, self.cy
        m = Image.new("L", (D, D), 0)
        d = ImageDraw.Draw(m)
        r, w = 25 * S, 7 * S
        box = (cx - r, cy - r + 2 * S, cx + r, cy + r + 2 * S)
        gap = 40
        d.arc(box, 270 + gap, 360, fill=255, width=w)       # clockwise from the upper right ...
        d.arc(box, 0, 270 - gap, fill=255, width=w)         # ... round to the upper left
        # round caps at the two arc ends
        for ang in (270 + gap, 270 - gap):
            a = math.radians(ang)
            ex = cx + (r - w / 2) * math.cos(a)
            ey = cy + 2 * S + (r - w / 2) * math.sin(a)
            d.ellipse((ex - w / 2, ey - w / 2, ex + w / 2, ey + w / 2), fill=255)
        top, bot = cy - r - 4 * S + 2 * S, cy - 3 * S
        d.line((cx, top, cx, bot), fill=255, width=w)
        for y in (top, bot):
            d.ellipse((cx - w / 2, y - w / 2, cx + w / 2, y + w / 2), fill=255)
        return m

    def body(self, state):
        if state in self._body:
            return self._body[state]
        S, D, cx, cy, p = self.S, self.D, self.cx, self.cy, self.pal
        col = self.colors(state)
        out = Image.new("RGBA", (D, D), (0, 0, 0, 0))

        # 1) drop shadow
        sh = Image.new("L", (D, D), 0)
        ImageDraw.Draw(sh).ellipse((cx - self.R_o, cy - self.R_o + 10 * S, cx + self.R_o, cy + self.R_o + 10 * S),
                                   fill=int(255 * (.60 if self.dark else .28)))
        sh = sh.filter(ImageFilter.GaussianBlur(14 * S))
        shadow_col = (0, 0, 0) if self.dark else (50, 60, 120)
        out.paste(Image.new("RGBA", (D, D), shadow_col + (255,)), (0, 0), sh)

        # 2) metal rim (vertical gradient clipped to a circle)
        rim = _vgrad((D, D), *col["rim"]).convert("RGBA")
        out.paste(rim, (0, 0), self._disc_mask(self.R_o))

        # 3) glass dome: radial gradient with the light source up-left
        ri = self.R_i
        gs = int(2 * ri * 1.6)
        g = Image.radial_gradient("L").resize((gs, gs), Image.BILINEAR)
        off_x, off_y = int((gs - 2 * ri) / 2 + .16 * ri), int((gs - 2 * ri) / 2 + .22 * ri)
        g = g.crop((off_x, off_y, off_x + 2 * ri, off_y + 2 * ri))
        c_in = _m(p["CARD"], p["TEXT"], .10 if self.dark else -0.0)
        if not self.dark:
            c_in = (255, 255, 255)
        c_out = _m(p["CARD"], p["BG"], .65 if self.dark else .45)
        if col["glow"]:
            c_in = _m(c_in, col["glow"], .10)
            c_out = _m(c_out, col["glow"], .16)
        dome = Image.composite(Image.new("RGB", g.size, c_out), Image.new("RGB", g.size, c_in),
                               g.point(lambda v: min(255, int(v * 1.25)))).convert("RGBA")
        out.paste(dome, (cx - ri, cy - ri), self._disc_mask(ri).crop((cx - ri, cy - ri, cx + ri, cy + ri)))

        # 4) inner luminous edge (on / connecting)
        if col["halo"]:
            edge = Image.new("L", (D, D), 0)
            ImageDraw.Draw(edge).ellipse((cx - ri + 2 * S, cy - ri + 2 * S, cx + ri - 2 * S, cy + ri - 2 * S),
                                         outline=255, width=3 * S)
            blur = edge.filter(ImageFilter.GaussianBlur(7 * S)).point(lambda v: min(255, int(v * 1.5)))
            out.paste(Image.new("RGBA", (D, D), col["halo"] + (255,)), (0, 0),
                      blur.point(lambda v: int(v * .70)))
            out.paste(Image.new("RGBA", (D, D), _m(col["halo"], (255, 255, 255), .25) + (255,)), (0, 0),
                      edge.filter(ImageFilter.GaussianBlur(S)).point(lambda v: int(v * .55)))

        # 5) glossy sheen on the upper half
        sheen = Image.new("L", (D, D), 0)
        ImageDraw.Draw(sheen).ellipse((cx - .74 * ri, cy - .98 * ri, cx + .74 * ri, cy - .06 * ri),
                                      fill=int(255 * (.20 if self.dark else .55)))
        sheen = ImageChops.multiply(sheen.filter(ImageFilter.GaussianBlur(9 * S)),
                                    self._disc_mask(ri - 3 * S))
        out.paste(Image.new("RGBA", (D, D), (255, 255, 255, 255)), (0, 0), sheen)

        # 6) power symbol: soft glow, then a gradient-filled crisp glyph
        im = self._icon_mask()
        if col["glow"]:
            gl = im.filter(ImageFilter.GaussianBlur(9 * S)).point(lambda v: min(255, int(v * 2.0)))
            out.paste(Image.new("RGBA", (D, D), col["icon"][1] + (255,)), (0, 0),
                      gl.point(lambda v: int(v * .85)))
        glyph = _vgrad((D, D), *col["icon"]).convert("RGBA")
        out.paste(glyph, (0, 0), im)
        self._body[state] = out
        return out

    def glow(self, state):
        if state in self._glow:
            return self._glow[state]
        col = self.colors(state)["glow"]
        if not col:
            return None
        S, D, cx, cy = self.S, self.D, self.cx, self.cy
        m = Image.new("L", (D, D), 0)
        r = 80 * S
        ImageDraw.Draw(m).ellipse((cx - r, cy - r, cx + r, cy + r), fill=255)
        m = m.filter(ImageFilter.GaussianBlur(19 * S)).point(lambda v: int(v * (.62 if self.dark else .50)))
        g = Image.new("RGBA", (D, D), tuple(col) + (255,))
        g.putalpha(m)
        self._glow[state] = g
        return g

    # ---- animated overlays
    def _comet(self, theta):
        S, D, cx, cy = self.S, self.D, self.cx, self.cy
        ov = Image.new("RGBA", (D, D), (0, 0, 0, 0))
        d = ImageDraw.Draw(ov)
        col = self.colors("connecting")
        r = (self.R_o + self.R_i) / 2 + 0.5 * S
        box = (cx - r, cy - r, cx + r, cy + r)
        n, step = 44, 5.0
        wide = (box[0] - 3 * S, box[1] - 3 * S, box[2] + 3 * S, box[3] + 3 * S)
        for i in range(n, -1, -1):                       # soft halo first (drawn pixels replace, not blend)
            t = 1 - i / n
            ang = theta - i * step
            d.arc(wide, ang - 4, ang + 1, fill=_m(col["icon"][1], col["icon"][0], t) + (int(80 * t ** 2),),
                  width=int(13 * S))
        for i in range(n, -1, -1):                       # crisp tail, tail first so the head ends on top
            t = 1 - i / n
            ang = theta - i * step
            d.arc(box, ang - 4, ang + 1, fill=_m(col["icon"][1], col["icon"][0], t) + (int(255 * t ** 1.2),),
                  width=int(7 * S))
        a = math.radians(theta)                           # rounded head
        hx, hy = cx + r * math.cos(a), cy + r * math.sin(a)
        d.ellipse((hx - 3.5 * S, hy - 3.5 * S, hx + 3.5 * S, hy + 3.5 * S), fill=col["icon"][0] + (255,))
        return ov

    def _ripples(self, phase):
        S, D, cx, cy = self.S, self.D, self.cx, self.cy
        ov = Image.new("RGBA", (D, D), (0, 0, 0, 0))
        d = ImageDraw.Draw(ov)
        g = self.colors("on")["glow"]
        for off in (0.0, 0.5):
            p = (phase + off) % 1.0
            r = self.R_o + 4 * S + p * 50 * S
            a = int(170 * (1 - p) ** 1.7)
            d.ellipse((cx - r, cy - r, cx + r, cy + r), outline=g + (a,), width=int(2.4 * S))
        return ov

    def frame(self, state, i):
        n = FRAMES[state]
        D = self.D
        img = Image.new("RGBA", (D, D), (0, 0, 0, 0))
        gl = self.glow(state)
        if gl is not None:
            k = .72 + .28 * math.sin(2 * math.pi * i / n) if state == "on" else 1.0
            img = Image.alpha_composite(img, _scale_alpha(gl, k))
        img = Image.alpha_composite(img, self.body(state))
        if state == "connecting":
            img = Image.alpha_composite(img, self._comet(-90 + i * 360 / n))
        elif state == "on":
            img = Image.alpha_composite(img, self._ripples(i / n))
        return img.resize((SIZE, SIZE), Image.LANCZOS)


# ------------------------------------------------------------------ small header icons
def icon(name, color, size=28):
    S = 6
    D = size * S
    m = Image.new("L", (D, D), 0)
    d = ImageDraw.Draw(m)
    c, w = D / 2, 2.1 * S
    if name == "sun":
        r = 4.6 * S
        d.ellipse((c - r, c - r, c + r, c + r), fill=255)
        for k in range(8):
            a = math.radians(k * 45)
            d.line((c + 7.4 * S * math.cos(a), c + 7.4 * S * math.sin(a),
                    c + 10.2 * S * math.cos(a), c + 10.2 * S * math.sin(a)), fill=255, width=int(w))
            for e in (7.4, 10.2):
                x, y = c + e * S * math.cos(a), c + e * S * math.sin(a)
                d.ellipse((x - w / 2, y - w / 2, x + w / 2, y + w / 2), fill=255)
    elif name == "moon":
        r = 9 * S
        d.ellipse((c - r, c - r, c + r, c + r), fill=255)
        cut = Image.new("L", (D, D), 0)
        r2 = 8 * S
        ImageDraw.Draw(cut).ellipse((c - r2 + 6 * S, c - r2 - 5 * S, c + r2 + 6 * S, c + r2 - 5 * S), fill=255)
        m = ImageChops.subtract(m, cut)
    elif name == "menu":
        for dy in (-6, 0, 6):
            y = c + dy * S
            d.line((c - 9 * S, y, c + 9 * S, y), fill=255, width=int(w))
            for x in (c - 9 * S, c + 9 * S):
                d.ellipse((x - w / 2, y - w / 2, x + w / 2, y + w / 2), fill=255)
    elif name == "log":
        d.rounded_rectangle((c - 11 * S, c - 9 * S, c + 11 * S, c + 9 * S), 4 * S, outline=255, width=int(w))
        d.line((c - 6 * S, c - 3 * S, c - 2 * S, c), fill=255, width=int(w))
        d.line((c - 2 * S, c, c - 6 * S, c + 3 * S), fill=255, width=int(w))
        d.line((c + 1 * S, c + 4 * S, c + 6 * S, c + 4 * S), fill=255, width=int(w))
        for x, y in ((c - 6 * S, c - 3 * S), (c - 2 * S, c), (c - 6 * S, c + 3 * S),
                     (c + 1 * S, c + 4 * S), (c + 6 * S, c + 4 * S)):
            d.ellipse((x - w / 2, y - w / 2, x + w / 2, y + w / 2), fill=255)
    elif name == "close":
        d.line((c - 6 * S, c - 6 * S, c + 6 * S, c + 6 * S), fill=255, width=int(w))
        d.line((c - 6 * S, c + 6 * S, c + 6 * S, c - 6 * S), fill=255, width=int(w))
        for x, y in ((c - 6 * S, c - 6 * S), (c + 6 * S, c + 6 * S), (c - 6 * S, c + 6 * S), (c + 6 * S, c - 6 * S)):
            d.ellipse((x - w / 2, y - w / 2, x + w / 2, y + w / 2), fill=255)
    m = m.resize((size, size), Image.LANCZOS)
    img = Image.new("RGBA", (size, size), _c(color) + (255,))
    img.putalpha(m)
    return img


# ------------------------------------------------------------------ toggle switch
def switch(pal, on, busy=False, w=48, h=28):
    """Anti-aliased on/off switch (transparent corners)."""
    S = 6
    W, H = w * S, h * S
    out = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    pill = Image.new("L", (W, H), 0)
    ImageDraw.Draw(pill).rounded_rectangle((0, 0, W - 1, H - 1), H // 2, fill=255)
    if on:
        a, a2 = _c(pal["ACCENT"]), _c(pal["ACCENT2"])
        track = _vgrad((W, H), _m(a, a2, .45), a)
    else:
        track = _vgrad((W, H), _m(pal["BORDER"], pal["BG"], .25), _m(pal["BORDER"], pal["TEXT"], .06))
    if busy:
        track = Image.blend(track, Image.new("RGB", (W, H), _c(pal["BG"])), .45)
    out.paste(track.convert("RGBA"), (0, 0), pill)
    r = H / 2 - 3.2 * S
    cx = W - H / 2 if on else H / 2
    cy = H / 2
    sh = Image.new("L", (W, H), 0)
    ImageDraw.Draw(sh).ellipse((cx - r, cy - r + 1.6 * S, cx + r, cy + r + 1.6 * S), fill=120)
    sh = sh.filter(ImageFilter.GaussianBlur(1.6 * S))
    out.paste(Image.new("RGBA", (W, H), (0, 0, 0, 255)), (0, 0), ImageChops.multiply(sh, pill))
    knob = _vgrad((W, H), (255, 255, 255), (232, 235, 244) if on else _m(pal["MUTED"], (255, 255, 255), .55))
    km = Image.new("L", (W, H), 0)
    ImageDraw.Draw(km).ellipse((cx - r, cy - r, cx + r, cy + r), fill=255)
    if busy:
        km = km.point(lambda v: int(v * .6))
    out.paste(knob.convert("RGBA"), (0, 0), km)
    return out.resize((w, h), Image.LANCZOS)
