#!/usr/bin/env python3
"""生成应用图标(ui/icon.ico + ui/icon.png)。

纯 PIL 绘制:深色圆角底 + 冰蓝耳机图形。
深底保证在浅色/深色任务栏上都看得清,16px 尺寸下仍可辨认。
"""
from __future__ import annotations

import os

from PIL import Image, ImageDraw

HERE = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.path.join(HERE, "ui")

S = 1024                     # 先在大画布上画,再降采样得到抗锯齿
ICE = (143, 208, 255, 255)   # 与界面主色一致
ICE_PAD = (58, 118, 166, 255)  # 耳垫:同色系压暗,小尺寸下仍保持实心剪影
BG = (11, 13, 16, 255)

ICO_SIZES = [16, 20, 24, 32, 40, 48, 64, 96, 128, 256]


def draw_icon() -> Image.Image:
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    # 圆角方底(squircle 观感:半径约 22%)
    d.rounded_rectangle((0, 0, S - 1, S - 1), radius=228, fill=BG)
    d.rounded_rectangle((7, 7, S - 8, S - 8), radius=221,
                        outline=(255, 255, 255, 24), width=7)

    # 头梁:上半圆弧
    band_w = 76
    cx, cy, r = 512, 580, 280
    box = (cx - r, cy - r, cx + r, cy + r)
    d.arc(box, start=180, end=360, fill=ICE, width=band_w)
    # 两端补圆角(PIL 的 arc 没有圆头)
    for ex in (cx - r, cx + r):
        d.ellipse((ex - band_w // 2, cy - band_w // 2,
                   ex + band_w // 2, cy + band_w // 2), fill=ICE)

    # 左右耳罩
    cup_w, cup_top, cup_bot, cup_r = 142, 540, 852, 58
    for x0 in (232 - cup_w // 2, 792 - cup_w // 2):
        d.rounded_rectangle((x0, cup_top, x0 + cup_w, cup_bot),
                            radius=cup_r, fill=ICE)
        # 耳垫:内嵌一圈深色,让耳罩有层次
        inset = 30
        d.rounded_rectangle((x0 + inset, cup_top + inset,
                             x0 + cup_w - inset, cup_bot - inset),
                            radius=cup_r - inset, fill=ICE_PAD)

    return img


def main() -> None:
    os.makedirs(OUT_DIR, exist_ok=True)
    big = draw_icon()

    png_path = os.path.join(OUT_DIR, "icon.png")
    big.resize((256, 256), Image.LANCZOS).save(png_path)

    ico_path = os.path.join(OUT_DIR, "icon.ico")
    big.save(ico_path, format="ICO", sizes=[(s, s) for s in ICO_SIZES])

    print(f"写入 {png_path}")
    print(f"写入 {ico_path}  ({', '.join(str(s) for s in ICO_SIZES)})")


if __name__ == "__main__":
    main()
