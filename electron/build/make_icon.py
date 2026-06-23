"""Generate the Bellwether app icon: a minimalist line-art bell (thin outline, no fill) centered on
the app's dark gradient squircle, with a faint glow so the stroke reads on the dark background and a
subtle top sheen. Rendered at 2x and downscaled for crisp edges. Outputs icon_1024.png and icon.ico.

The bell is drawn from scratch with Pillow primitives (a dome + flared skirt outline, a small top
knob, and a clapper dot) so there is no third-party artwork to ship. (The old Noto emoji bell PNG is
no longer used.)"""

import math
from PIL import Image, ImageDraw, ImageFilter

SS = 2          # supersample factor for anti-aliasing
S = 1024        # final size
W = S * SS      # working size
RADIUS = int(W * 0.235)
CX = W / 2

INK = (237, 241, 248, 255)   # near-white stroke
STROKE = int(W * 0.016)      # thin, minimalist line weight


def lerp(a, b, t):
    return tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3))


def vgradient(size, top, bottom):
    """Vertical gradient RGB image."""
    w, h = size
    g = Image.new("RGB", (w, h))
    d = ImageDraw.Draw(g)
    for y in range(h):
        d.line([(0, y), (w, y)], fill=lerp(top, bottom, y / max(1, h - 1)))
    return g


def bell_outline():
    """Closed point path for the bell body: dome over the top, sides flaring to the rim, rim arc."""
    y_dome_top, y_neck, y_rim = 0.31 * W, 0.37 * W, 0.655 * W
    hw_neck, hw_rim = 0.118 * W, 0.206 * W
    bow = 0.018 * W
    DN, SN, RN = 48, 40, 40
    pts = []
    # dome: left neck, over the top, to right neck
    for i in range(DN + 1):
        a = math.pi * (1 - i / DN)
        pts.append((CX + hw_neck * math.cos(a), y_neck - (y_neck - y_dome_top) * math.sin(a)))
    # right side: neck down to rim, flaring (power eases the flare toward the bottom)
    for i in range(1, SN + 1):
        t = i / SN
        pts.append((CX + hw_neck + (hw_rim - hw_neck) * (t ** 1.5), y_neck + (y_rim - y_neck) * t))
    # rim: right to left, bowed gently downward
    for i in range(1, RN + 1):
        t = i / RN
        pts.append(((CX + hw_rim) - 2 * hw_rim * t, y_rim + bow * math.sin(math.pi * t)))
    # left side: rim back up to neck
    for i in range(1, SN):
        t = 1 - i / SN
        pts.append((CX - (hw_neck + (hw_rim - hw_neck) * (t ** 1.5)), y_neck + (y_rim - y_neck) * t))
    pts.append(pts[0])
    return pts, y_dome_top, y_rim


# ---- background squircle ----
bg = vgradient((W, W), (34, 46, 72), (9, 12, 19))
mask = Image.new("L", (W, W), 0)
ImageDraw.Draw(mask).rounded_rectangle([0, 0, W - 1, W - 1], radius=RADIUS, fill=255)
img = Image.new("RGBA", (W, W), (0, 0, 0, 0))
img.paste(bg, (0, 0), mask)

# ---- the bell outline (own layer, so we can lay a soft glow under it) ----
pts, y_dome_top, y_rim = bell_outline()
bell = Image.new("RGBA", (W, W), (0, 0, 0, 0))
bd = ImageDraw.Draw(bell)
bd.line(pts, fill=INK, width=STROKE, joint="curve")
# small knob at the very top
kr = int(W * 0.026)
bd.ellipse([CX - kr, y_dome_top - kr * 1.4, CX + kr, y_dome_top + kr * 0.6], fill=INK)
# clapper dot below the rim
cr = int(W * 0.032)
ccy = y_rim + 0.052 * W
bd.ellipse([CX - cr, ccy - cr, CX + cr, ccy + cr], fill=INK)

# faint halo only (keeps the line crisp / minimalist, not neon) — just enough to lift it off the dark
glow = bell.filter(ImageFilter.GaussianBlur(int(W * 0.007)))
glow.putalpha(glow.split()[3].point(lambda a: int(a * 0.4)))
img.alpha_composite(glow)
img.alpha_composite(bell)

# ---- subtle top sheen for depth ----
sheen = Image.new("RGBA", (W, W), (0, 0, 0, 0))
ImageDraw.Draw(sheen).rounded_rectangle(
    [int(SS * 4), int(SS * 4), W - 1 - int(SS * 4), W - 1 - int(SS * 4)],
    radius=RADIUS, outline=(255, 255, 255, 38), width=int(SS * 3),
)
img.alpha_composite(sheen)

# downscale for anti-aliasing
final = img.resize((S, S), Image.LANCZOS)
final.save("icon_1024.png")

# Windows .ico (multi-resolution; electron-builder embeds it on --win builds)
final.resize((256, 256), Image.LANCZOS).save(
    "icon.ico", sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]
)
print("wrote icon_1024.png and icon.ico")
