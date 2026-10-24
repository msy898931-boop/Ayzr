#!/usr/bin/env python3
"""Generate the legacy launcher PNGs for the Aurora Markets Android shell.

Kept as a script (not a checked-in binary blob) so the icon can be regenerated
if the brand colours change. Adaptive icons (Android 8+) come from the vector
drawable in res/drawable; these PNGs cover API 21-25.
"""
import os
from PIL import Image, ImageDraw

ROOT = os.path.dirname(os.path.abspath(__file__))
BG = (10, 11, 18, 255)
A1 = (124, 92, 255, 255)   # --accent
A2 = (34, 211, 238, 255)   # --accent2
DENSITIES = {"mdpi": 48, "hdpi": 72, "xhdpi": 96, "xxhdpi": 144, "xxxhdpi": 192}


def rounded_mask(size, radius):
    m = Image.new("L", (size, size), 0)
    d = ImageDraw.Draw(m)
    d.rounded_rectangle([0, 0, size - 1, size - 1], radius=radius, fill=255)
    return m


def make(size):
    # Gradient from accent to accent2, diagonal.
    grad = Image.new("RGBA", (size, size))
    px = grad.load()
    for y in range(size):
        for x in range(size):
            t = (x + y) / (2 * size - 2)
            px[x, y] = (
                int(A1[0] + (A2[0] - A1[0]) * t),
                int(A1[1] + (A2[1] - A1[1]) * t),
                int(A1[2] + (A2[2] - A1[2]) * t),
                255,
            )
    img = Image.new("RGBA", (size, size), BG)
    mask = rounded_mask(size, int(size * 0.22))
    # The gradient arc: a thick diagonal band behind the chart.
    band = Image.new("L", (size, size), 0)
    bd = ImageDraw.Draw(band)
    bd.polygon(
        [(0, int(size * 0.72)), (int(size * 0.55), 0), (int(size * 0.78), 0), (int(size * 0.18), size), (0, size)],
        fill=110,
    )
    img.paste(grad, (0, 0), Image.composite(band, Image.new("L", (size, size), 0), mask))
    # The chart line.
    d = ImageDraw.Draw(img)
    w = max(2, int(size * 0.055))
    pts = [
        (int(size * 0.16), int(size * 0.66)),
        (int(size * 0.34), int(size * 0.50)),
        (int(size * 0.48), int(size * 0.60)),
        (int(size * 0.66), int(size * 0.32)),
        (int(size * 0.84), int(size * 0.40)),
    ]
    d.line(pts, fill=A2, width=w, joint="curve")
    r = w * 1.15
    for (cx, cy) in (pts[0], pts[-1]):
        d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=(255, 255, 255, 255))
    img.putalpha(mask)
    return img


def main():
    for name, size in DENSITIES.items():
        out = os.path.join(ROOT, "app", "res", "mipmap-" + name)
        os.makedirs(out, exist_ok=True)
        make(size).save(os.path.join(out, "ic_launcher.png"))
    print("icons written:", ", ".join(DENSITIES))


if __name__ == "__main__":
    main()
