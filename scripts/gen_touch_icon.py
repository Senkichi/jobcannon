"""Generate jobcannon/web/static/apple-touch-icon.png (180x180) — the
cannon-firing-a-stick-figure illustration (spec §4; the 16px favicon.svg
keeps the simplified glyph, this concept does not survive 16x16).

One-off, run manually; the PNG is committed, so Pillow is NOT a project
dependency:

    uv run --no-sync --with pillow python scripts/gen_touch_icon.py

Colors are literal hexes sanctioned by living-journal.md rule 5's
icon-asset exemption, mirroring lj-tokens.css light values: --lj-page
#FAF6EF, --lj-ink #1E1611, --lj-green-text #1F7A40. (A PNG cannot switch
with the OS theme; iOS composites touch icons on light tiles, so the
light palette is the honest single choice.)
"""

from __future__ import annotations

import math
from pathlib import Path

from PIL import Image, ImageDraw

SIZE = 180
PAPER = "#FAF6EF"  # --lj-page (light)
INK = "#1E1611"  # --lj-ink (light)
GREEN = "#1F7A40"  # --lj-green-text (light)

OUT = Path(__file__).resolve().parents[1] / "jobcannon" / "web" / "static" / "apple-touch-icon.png"


def main() -> None:
    img = Image.new("RGBA", (SIZE, SIZE))
    d = ImageDraw.Draw(img)
    # iOS masks its own corner radius; fill the full square so no
    # transparent corners show through the mask.
    d.rectangle((0, 0, SIZE, SIZE), fill=PAPER)

    # Cannon, lower left: angled barrel + carriage wheel.
    d.line((36, 138, 92, 82), fill=INK, width=26)
    d.ellipse((22, 128, 62, 168), outline=INK, width=12)

    # Muzzle flash: two short green rays off the muzzle (the one green
    # accent — same ≤1-accent spirit as the page identity).
    d.line((100, 62, 112, 50), fill=GREEN, width=8)
    d.line((110, 78, 126, 74), fill=GREEN, width=8)

    # Stick figure mid-flight, upper right: head, torso, legs, and three
    # rotated arm strokes suggesting the whirl.
    cx, cy = 138, 44  # shoulder anchor
    d.ellipse((cx - 9, cy - 26, cx + 9, cy - 8), outline=INK, width=6)  # head
    d.line((cx, cy - 8, cx + 10, cy + 22), fill=INK, width=6)  # torso
    d.line((cx + 10, cy + 22, cx - 2, cy + 40), fill=INK, width=6)  # leg
    d.line((cx + 10, cy + 22, cx + 26, cy + 36), fill=INK, width=6)  # leg
    for angle in (20, 140, 260):  # whirling arms
        rad = math.radians(angle)
        d.line(
            (cx, cy, cx + 20 * math.cos(rad), cy + 20 * math.sin(rad)),
            fill=INK,
            width=5,
        )

    img.save(OUT)
    print(f"wrote {OUT}")


if __name__ == "__main__":
    main()
