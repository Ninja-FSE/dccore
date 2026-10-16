#!/usr/bin/env python3
"""Draws the DCCore icon set: rounded colour badge + white glyph, 16/20/24/32/48 px,
one .ico per icon, a .rc, and a preview sheet. Order of NAMES == resource order == icon index."""
import argparse, math, os, sys
from PIL import Image, ImageDraw, ImageFont

S = 512                      # supersampled canvas
SIZES = [16, 20, 24, 32, 48]
W = (255, 255, 255, 255)

# The bold font of the letter glyphs (help, ask, font, app, hash). The shipped
# icons were drawn with DejaVu Sans Bold, so it comes first wherever it is; the
# others draw those five letters a little differently.
DEJAVU = "DejaVuSans-Bold.ttf"

def font_dirs():
    # Linux and the BSDs: fonts/ under each XDG data dir (the spec's default
    # when XDG_DATA_DIRS is unset), and the user's own
    dirs = os.environ.get("XDG_DATA_DIRS")
    dirs = dirs.split(os.pathsep) if dirs else ["/usr/local/share", "/usr/share"]
    out = [os.path.join(d, "fonts") for d in dirs if d]
    out.append(os.path.join(os.path.expanduser("~"), ".local", "share", "fonts"))
    out.append(os.path.join(os.path.expanduser("~"), ".fonts"))
    return out

def font_candidates():
    windir = os.environ.get("WINDIR") or "C:\\Windows"
    return [
        os.path.join(windir, "Fonts", DEJAVU),
        os.path.join(windir, "Fonts", "arialbd.ttf"),
        os.path.join(windir, "Fonts", "segoeuib.ttf"),
        "/Library/Fonts/Arial Bold.ttf",
        "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    ]

def find_font():
    for top in font_dirs():
        for root, _dirs, files in os.walk(top):
            if DEJAVU in files:
                return os.path.join(root, DEJAVU)
    for path in font_candidates():
        if os.path.isfile(path):
            return path
    sys.exit("make_icons.py: no bold font found. Looked for %s under %s, then for %s. "
             "Name one with --font <path to a .ttf>."
             % (DEJAVU, ", ".join(font_dirs()), ", ".join(font_candidates())))

ap = argparse.ArgumentParser(description="Draws the DCCore icon set into a folder.")
ap.add_argument("out", nargs="?", default=".", help="the folder to write to (default: the current one)")
ap.add_argument("--font", help="the bold .ttf for the letter glyphs (default: DejaVu Sans Bold, else Arial Bold or Segoe UI Bold)")
ARGS = ap.parse_args()
OUT = ARGS.out
if ARGS.font and not os.path.isfile(ARGS.font):
    sys.exit("make_icons.py: --font %s: no such file" % ARGS.font)
BOLD = ARGS.font or find_font()

COL = dict(
    slate=(79, 107, 237), grey=(100, 116, 139), sky=(14, 165, 233), green=(22, 163, 74),
    amber=(217, 119, 6), purple=(139, 92, 246), red=(220, 38, 38), teal=(13, 148, 136),
    pink=(219, 39, 119), blue=(37, 99, 235), orange=(234, 88, 12),
)

def u(v):            # 64-grid -> canvas
    return v * S / 64.0

class G:
    def __init__(self, base):
        self.im = Image.new("RGBA", (S, S), (0, 0, 0, 0))
        self.d = ImageDraw.Draw(self.im)
        self.base = base + (255,)
        self.d.rounded_rectangle([u(2), u(2), u(62), u(62)], radius=u(13), fill=self.base)
        # subtle top highlight
        hl = Image.new("RGBA", (S, S), (0, 0, 0, 0))
        ImageDraw.Draw(hl).rounded_rectangle([u(2), u(2), u(62), u(34)], radius=u(13), fill=(255, 255, 255, 38))
        mask = Image.new("L", (S, S), 0)
        ImageDraw.Draw(mask).rounded_rectangle([u(2), u(2), u(62), u(62)], radius=u(13), fill=255)
        self.im = Image.composite(Image.alpha_composite(self.im, hl), self.im, mask)
        self.d = ImageDraw.Draw(self.im)
        self.lw = int(u(5.2))
    # primitives (64-grid coordinates)
    def line(self, pts, w=None):
        w = int(u(w)) if w else self.lw
        pts = [(u(x), u(y)) for x, y in pts]
        self.d.line(pts, fill=W, width=w, joint="curve")
        for p in (pts[0], pts[-1]):
            self.d.ellipse([p[0] - w / 2, p[1] - w / 2, p[0] + w / 2, p[1] + w / 2], fill=W)
    def poly(self, pts, fill=W):
        self.d.polygon([(u(x), u(y)) for x, y in pts], fill=fill)
    def rect(self, x0, y0, x1, y1, r=0, fill=W, outline=None, w=None):
        w = int(u(w)) if w else self.lw
        kw = dict(radius=u(r), fill=fill)
        if outline:
            kw.update(outline=outline, width=w)
        self.d.rounded_rectangle([u(x0), u(y0), u(x1), u(y1)], **kw)
    def ell(self, x0, y0, x1, y1, fill=W, outline=None, w=None):
        w = int(u(w)) if w else self.lw
        kw = dict(fill=fill)
        if outline:
            kw.update(outline=outline, width=w)
        self.d.ellipse([u(x0), u(y0), u(x1), u(y1)], **kw)
    def ring(self, cx, cy, r, w=None):
        self.ell(cx - r, cy - r, cx + r, cy + r, fill=None, outline=W, w=w)
    def arc(self, cx, cy, r, a0, a1, w=None):
        w = int(u(w)) if w else self.lw
        self.d.arc([u(cx - r), u(cy - r), u(cx + r), u(cy + r)], a0, a1, fill=W, width=w)
    def arrowhead(self, x, y, ang, size=9):
        a = math.radians(ang)
        p = [(x + size * math.cos(a), y + size * math.sin(a)),
             (x + size * 0.9 * math.cos(a + 2.5), y + size * 0.9 * math.sin(a + 2.5)),
             (x + size * 0.9 * math.cos(a - 2.5), y + size * 0.9 * math.sin(a - 2.5))]
        self.poly(p)
    def text(self, s, cx, cy, size):
        f = ImageFont.truetype(BOLD, int(u(size)))
        bb = self.d.textbbox((0, 0), s, font=f)
        self.d.text((u(cx) - (bb[0] + bb[2]) / 2, u(cy) - (bb[1] + bb[3]) / 2), s, font=f, fill=W)
    def hole(self, draw_fn):
        draw_fn()

def new(color):
    return G(COL[color])

# ---------------------------------------------------------------- glyphs
def console():
    g = new("slate"); g.line([(18, 22), (29, 32), (18, 42)], 6); g.line([(33, 43), (47, 43)], 6); return g
def settings():
    g = new("grey")
    for i in range(8):
        a = math.radians(i * 45)
        cx, cy = 32 + 17 * math.cos(a), 32 + 17 * math.sin(a)
        pts = []
        for dx, dy in [(-5, -5), (5, -5), (5, 5), (-5, 5)]:
            ca, sa = math.cos(a), math.sin(a)
            pts.append((cx + dx * ca - dy * sa, cy + dx * sa + dy * ca))
        g.poly(pts)
    g.ell(17, 17, 47, 47); g.ell(25, 25, 39, 39, fill=g.base); return g
def script_settings():
    g = new("grey")
    for y, k in [(20, 24), (32, 40), (44, 30)]:
        g.line([(14, y), (50, y)], 4); g.ell(k - 5, y - 5, k + 5, y + 5)
    return g
def status():
    g = new("sky")
    for i, h in enumerate([14, 24, 18, 32]):
        x = 15 + i * 10; g.rect(x, 48 - h, x + 6, 48, r=1.5)
    return g
def slots():
    g = new("green"); g.poly([(32, 12), (47, 29), (37, 29), (37, 42), (27, 42), (27, 29), (17, 29)]); g.line([(15, 52), (49, 52)], 5); return g
def queue():
    g = new("amber")
    for y in (20, 32, 44):
        g.ell(13, y - 3.5, 20, y + 3.5); g.line([(26, y), (51, y)], 4.5)
    return g
def dlqueue():
    g = new("amber"); g.line([(20, 14), (20, 38)], 5); g.poly([(20, 50), (9, 36), (31, 36)])
    for y in (18, 30, 42): g.line([(36, y), (52, y)], 4.5)
    return g
def uptime():
    g = new("sky"); g.ring(32, 32, 19, 5); g.line([(32, 32), (32, 20)], 5); g.line([(32, 32), (42, 38)], 5); return g
def version():
    g = new("sky"); g.ell(28, 14, 36, 22); g.rect(28, 26, 36, 50, r=2); g.rect(24, 26, 36, 31); g.rect(24, 45, 40, 50); return g
def helpi():
    g = new("sky"); g.text("?", 32, 33, 46); return g
def lists():
    g = new("purple"); g.rect(15, 11, 49, 53, r=5, fill=None, outline=W, w=4.5)
    for y in (22, 31, 40): g.line([(23, y), (41, y)], 4)
    return g
def fetch():
    g = new("blue"); g.line([(32, 11), (32, 36)], 6); g.poly([(32, 49), (19, 33), (45, 33)]); g.line([(13, 54), (51, 54)], 5); return g
def ask():
    g = new("blue"); g.rect(11, 12, 53, 42, r=8)
    g.poly([(20, 40), (20, 54), (34, 41)]); g.text("?", 32, 27, 30)
    # question mark in badge colour
    f = ImageFont.truetype(BOLD, int(u(30))); bb = g.d.textbbox((0, 0), "?", font=f)
    g.d.text((u(32) - (bb[0] + bb[2]) / 2, u(27) - (bb[1] + bb[3]) / 2), "?", font=f, fill=g.base); return g
def purge():
    g = new("red"); g.line([(14, 18), (50, 18)], 5); g.rect(26, 11, 38, 18, r=2)
    g.poly([(18, 24), (46, 24), (43, 53), (21, 53)])
    for x in (27, 32, 37): g.line([(x, 29), (x, 47)], 3)
    return g
def library():
    g = new("purple"); g.poly([(10, 18), (26, 18), (31, 24), (54, 24), (54, 50), (10, 50)]); return g
def rebuild():
    g = new("green")
    g.arc(32, 32, 16, 200, 330, 5.5); g.arrowhead(46, 22, 30, 10)
    g.arc(32, 32, 16, 20, 150, 5.5); g.arrowhead(18, 42, 210, 10); return g
def audio():
    g = new("purple"); g.ell(14, 38, 30, 52); g.line([(29, 44), (29, 15)], 5)
    g.poly([(29, 14), (47, 21), (47, 31), (29, 25)]); return g
def ban():
    g = new("red"); g.ring(32, 32, 19, 6); g.line([(19, 45), (45, 19)], 6); return g
def unban():
    g = new("green"); g.ring(32, 32, 19, 5.5); g.line([(22, 33), (29, 40), (43, 25)], 6); return g
def clear():
    g = new("grey"); g.line([(20, 20), (44, 44)], 7); g.line([(44, 20), (20, 44)], 7); return g
def ignore():
    g = new("grey"); g.ell(9, 20, 55, 44, fill=None, outline=W, w=4.5); g.ell(25, 24, 39, 40)
    g.line([(14, 52), (50, 12)], 5.5); return g
def moveup():
    g = new("amber"); g.poly([(32, 12), (50, 36), (38, 36), (38, 52), (26, 52), (26, 36), (14, 36)]); return g
def movedown():
    g = new("amber"); g.poly([(32, 52), (50, 28), (38, 28), (38, 12), (26, 12), (26, 28), (14, 28)]); return g
def checkupd():
    g = new("blue"); g.ell(10, 24, 34, 46); g.ell(22, 14, 46, 42); g.ell(32, 26, 54, 46); g.rect(21, 34, 44, 46)
    g.poly([(32, 51), (23, 41), (41, 41)], fill=g.base); g.d.line([(u(32), u(30)), (u(32), u(42))], fill=g.base, width=int(u(5.5)))  # arrow cut
    return g
def rehash():
    g = new("orange"); g.poly([(36, 8), (16, 36), (29, 36), (25, 56), (47, 26), (33, 26)]); return g
def stop():
    g = new("red"); g.arc(32, 34, 16, -60, 240, 6); g.line([(32, 10), (32, 32)], 6); return g
def connect():
    g = new("teal"); g.rect(18, 26, 46, 42, r=5); g.line([(26, 12), (26, 26)], 5); g.line([(38, 12), (38, 26)], 5); g.line([(32, 42), (32, 54)], 5); return g
def disconnect():
    g = connect(); g.line([(12, 52), (52, 12)], 6)
    # gap around slash in badge colour
    g.d.line([(u(15), u(55)), (u(55), u(15))], fill=g.base, width=int(u(2))); return g
def pair():
    g = new("teal"); g.rect(9, 22, 35, 42, r=10, fill=None, outline=W, w=5); g.rect(29, 22, 55, 42, r=10, fill=None, outline=W, w=5); return g
def unpair():
    g = new("teal"); g.rect(8, 24, 28, 40, r=8, fill=None, outline=W, w=5); g.rect(36, 24, 56, 40, r=8, fill=None, outline=W, w=5)
    g.line([(30, 34), (34, 30)], 4); return g
def trust():
    g = new("green"); g.poly([(32, 8), (52, 15), (52, 32), (32, 56), (12, 32), (12, 15)])
    g.line([(23, 31), (30, 38), (42, 24)], 5.5)
    g.d.line([(u(23), u(31)), (u(30), u(38)), (u(42), u(24))], fill=g.base, width=int(u(5.5))); return g
def chat():
    g = new("pink"); g.rect(8, 12, 40, 36, r=7); g.poly([(14, 34), (14, 46), (26, 35)])
    g.rect(28, 26, 56, 48, r=7, fill=W); g.poly([(48, 46), (48, 56), (38, 47)]);
    g.rect(31, 29, 53, 45, r=5, fill=None); return g
def downloads():
    g = new("green"); g.line([(32, 10), (32, 34)], 6); g.poly([(32, 47), (19, 32), (45, 32)]); g.rect(14, 50, 50, 55, r=2); return g
def panel():
    g = new("slate"); g.rect(10, 14, 54, 50, r=5, fill=None, outline=W, w=4.5); g.line([(40, 15), (40, 49)], 4)
    for y in (24, 32, 40): g.line([(44, y), (50, y)], 3)
    return g
def font():
    g = new("grey"); g.text("A", 32, 34, 44); return g
def dashboard():
    g = new("blue"); g.ring(32, 32, 20, 4.5); g.d.ellipse([u(22), u(12), u(42), u(52)], outline=W, width=int(u(3.5)))
    g.line([(12, 32), (52, 32)], 3.5); return g
def cancel():
    g = new("red"); g.ring(32, 32, 19, 5); g.line([(24, 24), (40, 40)], 5.5); g.line([(40, 24), (24, 40)], 5.5); return g
def again():
    g = new("blue"); g.arc(32, 32, 17, 40, 340, 6); g.arrowhead(44, 18, 130, 11); return g
def user():
    g = new("slate"); g.ell(22, 11, 42, 31); g.poly([(10, 56), (14, 40), (24, 35), (40, 35), (50, 40), (54, 56)]); return g
def speed():
    g = new("teal"); g.arc(32, 38, 21, 180, 360, 6); g.line([(32, 38), (43, 24)], 5.5); g.ell(27, 33, 37, 43); return g
def search():
    g = new("blue"); g.ring(27, 27, 14, 6); g.line([(37, 37), (52, 52)], 7); return g
def pack():
    g = new("amber"); g.rect(10, 14, 54, 26, r=3); g.rect(14, 29, 50, 52, r=3); g.rect(26, 35, 38, 40, r=2, fill=g.base); return g
def app():
    g = new("slate"); g.ell(8, 8, 56, 56, fill=None, outline=(255, 255, 255, 90), w=2); g.text("D", 32, 33, 46); return g
def folderopen():
    g = new("purple"); g.poly([(8, 20), (24, 20), (29, 26), (50, 26), (50, 32), (8, 32)])
    g.poly([(8, 52), (15, 32), (58, 32), (50, 52)]); return g
def shield():
    return trust()
def dccore():
    return app()

def hashtag():
    g = new("teal"); g.text("#", 32, 33, 44); return g
def palette():
    g = new("pink"); g.ell(9, 12, 55, 52)
    g.ell(34, 34, 46, 46, fill=g.base)
    for (x, y, c) in ((21, 24, (217, 119, 6)), (33, 19, (14, 165, 233)), (21, 37, (22, 163, 74)), (44, 25, (139, 92, 246))):
        g.ell(x - 4.5, y - 4.5, x + 4.5, y + 4.5, fill=c + (255,))
    return g
def megaphone():
    g = new("orange"); g.poly([(12, 26), (24, 26), (48, 13), (48, 51), (24, 38), (12, 38)])
    g.rect(21, 38, 29, 52, r=2); g.line([(52, 25), (52, 39)], 3.5); return g
def mail():
    g = new("sky"); g.rect(9, 17, 55, 47, r=4)
    g.d.line([(u(11), u(19)), (u(32), u(35)), (u(53), u(19))], fill=g.base, width=int(u(4.5)), joint="curve"); return g
def bug():
    g = new("red")
    for y in (28, 37, 46):
        g.line([(10, y - 3), (20, y)], 3.5); g.line([(54, y - 3), (44, y)], 3.5)
    g.ell(20, 20, 44, 54); g.ell(25, 10, 39, 24)
    g.d.line([(u(32), u(24)), (u(32), u(52))], fill=g.base, width=int(u(3))); return g
def key():
    g = new("amber"); g.ring(20, 32, 10, 6); g.line([(30, 32), (54, 32)], 6)
    g.line([(46, 32), (46, 42)], 5); g.line([(53, 32), (53, 40)], 5); return g
def home():
    g = new("slate"); g.poly([(32, 9), (56, 31), (49, 31), (49, 54), (15, 54), (15, 31), (8, 31)])
    g.rect(27, 38, 37, 54, r=2, fill=g.base); return g
def shieldalert():
    g = new("red"); g.poly([(32, 8), (52, 15), (52, 32), (32, 56), (12, 32), (12, 15)])
    g.rect(29, 18, 35, 37, r=2, fill=g.base); g.ell(28.5, 41, 35.5, 48, fill=g.base); return g

ICONS = [
    ("app", app), ("console", console), ("settings", settings), ("script", script_settings),
    ("status", status), ("slots", slots), ("queue", queue), ("dlqueue", dlqueue),
    ("uptime", uptime), ("version", version), ("help", helpi), ("lists", lists),
    ("fetch", fetch), ("ask", ask), ("purge", purge), ("library", library),
    ("rebuild", rebuild), ("audio", audio), ("ban", ban), ("unban", unban),
    ("clear", clear), ("ignore", ignore), ("moveup", moveup), ("movedown", movedown),
    ("update", checkupd), ("rehash", rehash), ("stop", stop), ("connect", connect),
    ("disconnect", disconnect), ("pair", pair), ("unpair", unpair), ("trust", trust),
    ("chat", chat), ("downloads", downloads), ("panel", panel), ("font", font),
    ("dashboard", dashboard), ("cancel", cancel), ("again", again), ("user", user),
    ("speed", speed), ("search", search), ("pack", pack), ("folder", folderopen),
    ("hash", hashtag), ("palette", palette), ("megaphone", megaphone), ("mail", mail),
    ("bug", bug), ("key", key), ("home", home), ("shield", shieldalert),
]

os.makedirs(OUT, exist_ok=True)
rc = []
rendered = []
for idx, (name, fn) in enumerate(ICONS):
    im = fn().im
    rendered.append((name, im))
    path = os.path.join(OUT, "i%02d_%s.ico" % (idx, name))
    imgs = [im.resize((s, s), Image.LANCZOS) for s in SIZES]
    imgs[-1].save(path, format="ICO", sizes=[(s, s) for s in SIZES], append_images=imgs[:-1], bitmap_format="bmp")
    rc.append('%d ICON "%s"' % (idx + 1, os.path.basename(path)))
open(os.path.join(OUT, "dccore_icons.rc"), "w").write("\n".join(rc) + "\n")
open(os.path.join(OUT, "names.txt"), "w").write("\n".join(n for n, _ in ICONS) + "\n")

# preview sheet: light | dark, each icon at 16 px (x4 zoom) and 32 px, with name+index
cols = 6; cell_w, cell_h = 168, 78
rows = math.ceil(len(ICONS) / cols)
sheet = Image.new("RGB", (cols * cell_w * 2, rows * cell_h + 28), (255, 255, 255))
d = ImageDraw.Draw(sheet)
f = ImageFont.load_default()
for half, bg, fg in ((0, (248, 248, 248), (40, 40, 40)), (1, (32, 32, 32), (230, 230, 230))):
    d.rectangle([half * cols * cell_w, 0, (half + 1) * cols * cell_w, sheet.height], fill=bg)
    d.text((half * cols * cell_w + 8, 8), "light" if half == 0 else "dark", fill=fg, font=f)
    for i, (name, im) in enumerate(rendered):
        cx = half * cols * cell_w + (i % cols) * cell_w + 8
        cy = 28 + (i // cols) * cell_h
        big = im.resize((48, 48), Image.LANCZOS)
        sheet.paste(big, (cx, cy), big)
        sm = im.resize((16, 16), Image.LANCZOS)
        sheet.paste(sm, (cx + 58, cy), sm)
        sm2 = im.resize((16, 16), Image.LANCZOS).resize((32, 32), Image.NEAREST)
        sheet.paste(sm2, (cx + 80, cy), sm2)
        d.text((cx, cy + 54), "%d %s" % (i, name), fill=fg, font=f)
sheet.save(os.path.join(OUT, "icons-preview.png"))
print(len(ICONS), "icons")
