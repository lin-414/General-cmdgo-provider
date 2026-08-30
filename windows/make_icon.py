#!/usr/bin/env python3
"""Generate the app icon (.ico + .png) — same design as the tray icon (see appicon.py)."""
import os
import sys

from PIL import Image  # noqa: F401  (re-exported for callers that inspect assets)

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import appicon  # noqa: E402  # 需先把项目根目录加入 sys.path

OUT = os.path.join(ROOT, "assets")
img = appicon.make_icon_image(size=256)
img.save(OUT + "/icon.png")
img.save(OUT + "/icon.ico", sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
print("icon written to", OUT)
