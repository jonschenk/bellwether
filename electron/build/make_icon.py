"""Generate the app icon: a dark rounded square with rising candlesticks and
an upward trend arrow. Outputs icon_1024.png."""

import math

from PIL import Image, ImageDraw

S = 1024
img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
d = ImageDraw.Draw(img)

# vertical gradient background
top, bot = (22, 27, 39), (11, 14, 20)
for y in range(S):
    t = y / S
    c = tuple(int(top[i] * (1 - t) + bot[i] * t) for i in range(3))
    d.line([(0, y), (S, y)], fill=c + (255,))

# round the corners (macOS squircle-ish)
mask = Image.new("L", (S, S), 0)
ImageDraw.Draw(mask).rounded_rectangle([0, 0, S - 1, S - 1], radius=int(S * 0.225), fill=255)
img.putalpha(mask)

d = ImageDraw.Draw(img)
green = (74, 222, 128, 255)
green_dim = (74, 222, 128, 90)
accent = (91, 140, 255, 255)

# rising candlesticks (wick + body), ascending left->right
candles = [
    # x, wick_top, wick_bot, body_top, body_bot
    (300, 600, 800, 660, 770),
    (470, 500, 720, 560, 690),
    (640, 410, 660, 470, 620),
    (790, 300, 580, 360, 540),
]
bw = 78
for x, wt, wb, bt, bb in candles:
    d.line([(x, wt), (x, wb)], fill=green_dim, width=14)
    d.rounded_rectangle([x - bw // 2, bt, x + bw // 2, bb], radius=14, fill=green)

# upward trend arrow over the candles
a, b = (235, 715), (815, 330)
d.line([a, b], fill=accent, width=30, joint="curve")

# arrowhead at b
ang = math.atan2(b[1] - a[1], b[0] - a[0])
L, spread = 135, math.radians(30)
left = (b[0] - L * math.cos(ang - spread), b[1] - L * math.sin(ang - spread))
right = (b[0] - L * math.cos(ang + spread), b[1] - L * math.sin(ang + spread))
d.polygon([b, left, right], fill=accent)

img.save("icon_1024.png")
print("wrote icon_1024.png")
