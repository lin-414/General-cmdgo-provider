#!/usr/bin/env python3
"""应用图标绘制 — 托盘、窗口/任务栏、EXE 文件图标共用同一设计。

设计：透明底 + 绿色圆形 + 白色 G 弧（cmdgo = Command Code Go）。
颜色随代理状态变化：运行中绿色，已停止红色（托盘用）。
"""
from PIL import Image, ImageDraw

BASE_COLOR = "#4CAF50"
ERROR_COLOR = "#f44336"


def make_icon_image(color: str = BASE_COLOR, size: int = 64) -> Image.Image:
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    margin = max(size // 16, 2)
    d.ellipse([margin, margin, size - margin, size - margin], fill=color)
    d.arc([size * 0.25, size * 0.2, size * 0.75, size * 0.8],
          start=30, end=320, fill="white", width=max(size // 10, 3))
    return img
