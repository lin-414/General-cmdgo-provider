#!/usr/bin/env python3
"""Generate a modern app icon (.ico + .png) for cmdgo-provider."""
from PIL import Image, ImageDraw, ImageFont

import os
OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "assets")

SIZE = 256
img = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
d = ImageDraw.Draw(img)

# Rounded-square green "tile"
d.rounded_rectangle([16, 16, SIZE - 16, SIZE - 16], radius=52, fill="#238636")
d.rounded_rectangle([30, 30, SIZE - 30, SIZE - 30], radius=42, fill="#3fb950")

# Letter "G" (Command Code Go) centered
try:
    font = ImageFont.truetype("arialbd.ttf", 150)
except Exception:
    font = ImageFont.load_default(size=150)
bbox = d.textbbox((0, 0), "G", font=font)
w, h = bbox[2] - bbox[0], bbox[3] - bbox[1]
d.text(((SIZE - w) / 2 - bbox[0], (SIZE - h) / 2 - bbox[1]), "G", font=font, fill="#ffffff")

img.save(OUT + "/icon.png")
# Square sizes for .ico
img.save(OUT + "/icon.ico", sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
print("icon written to", OUT)
