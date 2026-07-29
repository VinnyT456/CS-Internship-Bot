"""Render a circular match-score gauge (donut wheel) as PNG bytes.

Used by the Score button: a 0-100 number in the center, an arc filling
proportionally, color-graded red→amber→green by score.
"""

import io
import math

from PIL import Image, ImageDraw, ImageFont


def _color_for(score):
    """Five bands, red → orange → yellow → lime → green. Kept in sync with
    job_ai._score_color."""
    if score >= 80:
        return (67, 181, 129)   # green — Excellent
    if score >= 60:
        return (150, 200, 60)   # lime — Strong
    if score >= 40:
        return (250, 197, 40)   # yellow — Moderate
    if score >= 20:
        return (240, 138, 30)   # orange — Weak
    return (237, 66, 69)        # red — Very weak


def _load_font(size):
    for path in (
        "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
        "/System/Library/Fonts/Helvetica.ttc",
        "/Library/Fonts/Arial.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    ):
        try:
            return ImageFont.truetype(path, size)
        except Exception:
            continue
    return ImageFont.load_default()


def render(score, size=400, label="MATCH"):
    """PNG bytes of a donut gauge for `score` (0-100). Transparent background."""
    score = max(0, min(100, int(round(score))))
    scale = 4  # supersample for smooth arc, downscaled at the end
    S = size * scale
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    pad = int(S * 0.10)
    width = int(S * 0.11)
    box = [pad, pad, S - pad, S - pad]

    track = (60, 63, 69, 255)          # faint full-circle track
    fill = _color_for(score) + (255,)

    # Track ring.
    draw.arc(box, 0, 360, fill=track, width=width)

    # Filled arc: start at top (-90°), sweep clockwise by score%.
    start = -90
    end = start + (360 * score / 100)
    if score > 0:
        draw.arc(box, start, end, fill=fill, width=width)

    # Rounded cap at the arc end.
    if 0 < score < 100:
        cx, cy = S / 2, S / 2
        r = (box[2] - box[0]) / 2
        ang = math.radians(end)
        ex, ey = cx + r * math.cos(ang), cy + r * math.sin(ang)
        cap = width / 2
        draw.ellipse(
            [ex - cap, ey - cap, ex + cap, ey + cap], fill=fill
        )

    # Center number.
    num_font = _load_font(int(S * 0.30))
    num = str(score)
    tb = draw.textbbox((0, 0), num, font=num_font)
    tw, th = tb[2] - tb[0], tb[3] - tb[1]
    draw.text(
        ((S - tw) / 2 - tb[0], (S - th) / 2 - tb[1] - int(S * 0.03)),
        num,
        font=num_font,
        fill=fill,
    )

    # Label under the number.
    lbl_font = _load_font(int(S * 0.075))
    lb = draw.textbbox((0, 0), label, font=lbl_font)
    lw = lb[2] - lb[0]
    draw.text(
        ((S - lw) / 2 - lb[0], S / 2 + int(S * 0.12)),
        label,
        font=lbl_font,
        fill=(185, 187, 190, 255),
    )

    img = img.resize((size, size), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()
