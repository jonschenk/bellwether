"""Generate the Bellwether app icon: the Noto Emoji bell (U+1F514) centered on the app's dark
gradient squircle, with a soft glow and a top sheen so it reads as a polished app icon rather
than a floating emoji. Rendered at 2x and downscaled for crisp edges. Outputs icon_1024.png and
icon.ico.

The bell artwork is Google's Noto Emoji (Apache-2.0, github.com/googlefonts/noto-emoji), saved
locally as noto_bell_1f514.png so regenerating needs no network. We composite it, we don't ship
Apple's proprietary emoji."""

from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

SS = 2          # supersample factor for anti-aliasing
S = 1024        # final size
W = S * SS      # working size
RADIUS = int(W * 0.235)
EMOJI_FILE = Path(__file__).with_name("noto_bell_1f514.png")
EMOJI_FRAC = 0.62   # emoji width as a fraction of the icon (the rest is padding)


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


# ---- background squircle ----
bg = vgradient((W, W), (34, 46, 72), (9, 12, 19))
mask = Image.new("L", (W, W), 0)
ImageDraw.Draw(mask).rounded_rectangle([0, 0, W - 1, W - 1], radius=RADIUS, fill=255)
img = Image.new("RGBA", (W, W), (0, 0, 0, 0))
img.paste(bg, (0, 0), mask)

# ---- the bell emoji, scaled + centred ----
emoji = Image.open(EMOJI_FILE).convert("RGBA")
target_w = int(W * EMOJI_FRAC)
target_h = int(target_w * emoji.height / emoji.width)
emoji = emoji.resize((target_w, target_h), Image.LANCZOS)
ex = (W - target_w) // 2
ey = (W - target_h) // 2 - int(W * 0.012)  # nudge up a touch (optical centring)

emoji_layer = Image.new("RGBA", (W, W), (0, 0, 0, 0))
emoji_layer.paste(emoji, (ex, ey), emoji)

# soft glow (blurred copy underneath) for depth on the dark squircle
glow = emoji_layer.filter(ImageFilter.GaussianBlur(int(W * 0.013)))
img.alpha_composite(glow)
img.alpha_composite(emoji_layer)

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
