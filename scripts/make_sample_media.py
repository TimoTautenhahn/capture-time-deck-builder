"""Generate synthetic sample images with EXIF capture timestamps.

The composer orders media by capture time (EXIF DateTimeOriginal, falling back to
file mtime). These placeholders carry explicit, out-of-order EXIF timestamps so the
capture-time sorting is observable in the resulting deck. No real photos are shipped.

Usage:
    python scripts/make_sample_media.py            # writes ./sample_media
    python scripts/make_sample_media.py --out DIR
"""
from __future__ import annotations

import argparse
from datetime import datetime
from pathlib import Path

from PIL import Image, ImageDraw

# (filename, capture_time, rgb) -- filenames are deliberately NOT in capture order,
# so sorting by EXIF time reorders them.
SAMPLES = [
    ("img_a.jpg", datetime(2023, 4, 2, 9, 15, 0), (54, 79, 107)),
    ("img_b.jpg", datetime(2023, 1, 18, 14, 30, 0), (112, 68, 72)),
    ("img_c.jpg", datetime(2023, 7, 9, 18, 5, 0), (66, 104, 74)),
    ("img_d.jpg", datetime(2023, 2, 27, 11, 48, 0), (120, 96, 54)),
    ("img_e.jpg", datetime(2023, 9, 21, 7, 40, 0), (84, 74, 110)),
    ("img_f.jpg", datetime(2023, 5, 30, 20, 12, 0), (58, 96, 104)),
]

WIDTH, HEIGHT = 1280, 853


def build_image(label: str, capture: datetime, rgb: tuple[int, int, int]) -> Image.Image:
    img = Image.new("RGB", (WIDTH, HEIGHT), rgb)
    draw = ImageDraw.Draw(img)
    text = f"{label}\n{capture:%Y-%m-%d %H:%M}"
    draw.multiline_text((60, 60), text, fill=(235, 235, 235), spacing=16)
    return img


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default="sample_media", help="Output directory.")
    args = parser.parse_args()

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    for name, capture, rgb in SAMPLES:
        img = build_image(name, capture, rgb)
        exif = img.getexif()
        stamp = capture.strftime("%Y:%m:%d %H:%M:%S")
        exif[306] = stamp     # DateTime
        exif[36867] = stamp   # DateTimeOriginal
        exif[36868] = stamp   # DateTimeDigitized
        img.save(out / name, "JPEG", quality=85, exif=exif)
        print(f"wrote {out / name}  (capture {stamp})")


if __name__ == "__main__":
    main()
