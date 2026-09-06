"""Render the DS&A roadmap as a clean node-graph PNG (labuladong-style).

Design goals (from the product owner):
  * Clean connected node graph, NOT an emoji list.
  * Silver Wolf palette: violet = learned, cyan = ready/unlocked, dim grey = locked.
  * Don't re-render the whole map every call. Each NODE is a self-contained tile
    cached on disk by (pattern_key, state, version); only the tiles whose state
    changed for a given user re-render — everyone else's tiles come from cache.
    The composer then pastes cached tiles onto a tier-row canvas and draws the
    (cheap) prerequisite edges fresh.

PIL is imported lazily inside the render helpers so importing this module doesn't
drag Pillow into the always-on process until a roadmap is actually drawn.

Cache scope: process-lifetime on disk (Render's FS is ephemeral across deploys,
which is fine — tiles are tiny + deterministic and re-warm lazily). Bump
_TILE_VERSION to invalidate all tiles after a design change.
"""

from __future__ import annotations

import io
import os

# --- Silver Wolf palette ------------------------------------------------------
# Near-black backdrop (her UI aesthetic), electric violet + cyan accents.
_BG = (17, 18, 24)            # canvas background
_EDGE = (70, 74, 92)          # connector lines (core prereq — solid)
_EDGE_DONE = (124, 92, 220)   # edge into a learned node (violet)
_EDGE_SOFT = (56, 58, 74)     # recommended/'helps to know' edge — dashed, fainter
_EDGE_SPINE = (90, 84, 130)   # study-order 'next →' path link (violet-grey, dotted)

_VIOLET = (167, 139, 250)     # learned — SW violet
_VIOLET_DEEP = (124, 58, 237)
_CYAN = (34, 211, 238)        # unlocked / ready now
_LOCKED = (92, 96, 112)       # locked — muted grey
_START = (250, 204, 60)       # START-HERE accent (warm gold — pops off violet/cyan)
_NEXT = (120, 190, 255)       # 'next up' hint (soft blue)
_TEXT = (232, 234, 242)
_TEXT_DIM = (150, 154, 170)

_STATE_ACCENT = {
    "learned": _VIOLET,
    "unlocked": _CYAN,
    "locked": _LOCKED,
}
_STATE_TEXT = {
    "learned": _TEXT,
    "unlocked": _TEXT,
    "locked": _TEXT_DIM,
}

# Tile geometry (logical px; rendered at _SS supersample then downscaled).
# Roomier tiles so pattern names fit on ONE line (less cramped) and read clearly.
_TILE_W = 208
_TILE_H = 58
_SS = 3  # supersample factor for crisp text/corners

# Whole-composite upscale — the final PNG is rendered at this factor so text is
# crisp when Discord shows/zooms it (Discord downsizes to fit, keeping detail).
_CANVAS_SCALE = 1.5

_TILE_VERSION = 4  # bump to invalidate cached tiles after a design/font change

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_CACHE_DIR = os.path.join(_REPO_ROOT, "roadmap_cache")
_ASSET_FONT_DIR = os.path.join(_REPO_ROOT, "assets", "fonts")


def _font(size, bold=True):
    from PIL import ImageFont

    # BUNDLED font FIRST — DejaVu Sans is vendored in assets/fonts so the roadmap
    # renders IDENTICALLY on every host (local macOS, Render's Linux runtime, any
    # box). Without this, macOS paths vanish on Render and PIL falls back to a
    # tiny bitmap default → the image would look nothing like the preview. System
    # paths are kept only as a defensive fallback if the asset ever goes missing.
    bundled = os.path.join(_ASSET_FONT_DIR,
                           "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf")
    paths = (
        bundled,
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold
        else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/System/Library/Fonts/Supplemental/Arial Bold.ttf" if bold
        else "/System/Library/Fonts/Supplemental/Arial.ttf",
        "/Library/Fonts/Arial.ttf",
    )
    for p in paths:
        try:
            return ImageFont.truetype(p, size)
        except Exception:
            continue
    return ImageFont.load_default()


def _wrap(draw, text, font, max_w):
    """Wrap `text` to at most 2 lines that fit `max_w`; ellipsize if longer."""
    words = text.split()
    lines, cur = [], ""
    for w in words:
        trial = (cur + " " + w).strip()
        if draw.textlength(trial, font=font) <= max_w or not cur:
            cur = trial
        else:
            lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    if len(lines) > 2:
        lines = lines[:2]
        while lines[1] and draw.textlength(lines[1] + "…", font=font) > max_w:
            lines[1] = lines[1][:-1]
        lines[1] += "…"
    return lines[:2]


def _rounded(draw, box, radius, fill=None, outline=None, width=1):
    draw.rounded_rectangle(box, radius=radius, fill=fill, outline=outline, width=width)


def _pill(draw, x, y, text, color, dark_text=False):
    """A small filled rounded pill with a label — used for the START HERE / NEXT
    tags on the composed roadmap (drawn at composite scale, not on tiles)."""
    font = _font(11, bold=True)
    tw = draw.textlength(text, font=font)
    padx, h = 7, 17
    box = [x, y, x + tw + padx * 2, y + h]
    draw.rounded_rectangle(box, radius=h // 2, fill=color + (255,))
    txt_col = (20, 20, 24) if dark_text else (255, 255, 255)
    draw.text((x + padx, y + 2), text, font=font, fill=txt_col)


def _render_tile(name, state):
    """Draw ONE node tile (rounded box + name + status dot + accent underline)
    as RGBA PNG bytes. Pure function of (name, state) — safe to cache."""
    from PIL import Image, ImageDraw

    accent = _STATE_ACCENT.get(state, _LOCKED)
    txt = _STATE_TEXT.get(state, _TEXT_DIM)
    W, H = _TILE_W * _SS, _TILE_H * _SS
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    r = 9 * _SS
    pad = 2 * _SS
    box = [pad, pad, W - pad, H - pad]
    # Panel: locked is dimmer/flatter; learned/unlocked get a faint accent tint.
    if state == "learned":
        panel = (36, 31, 54)
    elif state == "unlocked":
        panel = (21, 37, 45)
    else:
        panel = (28, 30, 39)
    _rounded(d, box, r, fill=panel, outline=accent, width=2 * _SS if state != "locked" else 1 * _SS)

    # Status dot, top-right.
    dot_r = 4 * _SS
    cx, cy = W - pad - 9 * _SS, pad + 9 * _SS
    d.ellipse([cx - dot_r, cy - dot_r, cx + dot_r, cy + dot_r], fill=accent)

    # Name, centered, up to 2 lines. Leave room for the underline bar at bottom.
    name_font = _font(14 * _SS, bold=True)
    inner_w = W - 22 * _SS
    lines = _wrap(d, name, name_font, inner_w)
    line_h = (name_font.getbbox("Ay")[3] - name_font.getbbox("Ay")[1]) + 2 * _SS
    total_h = line_h * len(lines)
    y = (H - 8 * _SS - total_h) / 2
    for ln in lines:
        w = d.textlength(ln, font=name_font)
        d.text(((W - w) / 2, y), ln, font=name_font, fill=txt)
        y += line_h

    # Accent underline bar near bottom (like the ref's progress underline). Full
    # for learned, half for unlocked, short dim for locked.
    bar_y = H - pad - 8 * _SS
    bar_x0 = 14 * _SS
    bar_x1 = W - 14 * _SS
    frac = {"learned": 1.0, "unlocked": 0.5, "locked": 0.18}.get(state, 0.18)
    track = (52, 54, 66)
    _rounded(d, [bar_x0, bar_y, bar_x1, bar_y + 3 * _SS], 2 * _SS, fill=track)
    fill_x1 = bar_x0 + (bar_x1 - bar_x0) * frac
    _rounded(d, [bar_x0, bar_y, fill_x1, bar_y + 3 * _SS], 2 * _SS, fill=accent)

    img = img.resize((_TILE_W, _TILE_H), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _tile_path(key, state):
    return os.path.join(_CACHE_DIR, f"tile_{key}_{state}_v{_TILE_VERSION}.png")


def get_tile_image(key, name, state):
    """Return a PIL RGBA Image for a node tile, from disk cache if present else
    rendered + cached. Only tiles whose (key,state) isn't cached hit the renderer,
    so a user finishing one pattern regenerates at most that one tile."""
    from PIL import Image

    path = _tile_path(key, state)
    try:
        if os.path.exists(path):
            return Image.open(path).convert("RGBA")
    except Exception:
        pass  # corrupt/partial cache file — fall through to re-render

    data = _render_tile(name, state)
    try:
        os.makedirs(_CACHE_DIR, exist_ok=True)
        with open(path, "wb") as f:
            f.write(data)
    except Exception:
        pass  # read-only FS — still return the freshly rendered tile
    return Image.open(io.BytesIO(data)).convert("RGBA")


# --- Composer -----------------------------------------------------------------
# Layout: each TIER is a row (top → bottom). Tiles are horizontally centered per
# row. Prerequisite edges connect a prereq's bottom-center to the dependent's
# top-center with an elbow. Tiles come from cache; edges are drawn fresh (cheap).

_MARGIN = 44        # canvas margin
_HEADER_H = 132     # space at top for title + progress + guidance strip
_TILE_GAP = 22      # gap between tiles inside a panel

_G_TILE_GAP_X = 40   # gap between node tiles in a row
_G_ROW_GAP = 140     # vertical gap between tier rows (room for lanes + ghost labels)


def _round_corner(draw, cx, cy, r, quadrant, color, width):
    """A small rounded turn at an elbow corner, approximated by a short arc.
    quadrant picks which 90° the corner bends through."""
    import math
    a0, a1 = {"tl": (90, 180), "tr": (0, 90), "bl": (180, 270), "br": (270, 360)}[quadrant]
    pts = []
    for i in range(6):
        a = math.radians(a0 + (a1 - a0) * i / 5)
        pts.append((cx + r * math.cos(a), cy + r * math.sin(a)))
    draw.line(pts, fill=color, width=width, joint="curve")


def _arrowhead(draw, tip, direction, color, width, size=10):
    """Draw a filled-ish arrowhead at `tip` pointing along `direction` ('down',
    'up', 'left', 'right')."""
    x, y = tip
    if direction == "down":
        draw.polygon([(x, y), (x - size * 0.6, y - size), (x + size * 0.6, y - size)], fill=color)
    elif direction == "up":
        draw.polygon([(x, y), (x - size * 0.6, y + size), (x + size * 0.6, y + size)], fill=color)
    elif direction == "right":
        draw.polygon([(x, y), (x - size, y - size * 0.6), (x - size, y + size * 0.6)], fill=color)
    else:  # left
        draw.polygon([(x, y), (x + size, y - size * 0.6), (x + size, y + size * 0.6)], fill=color)


def _ortho_edge(draw, src_bottom, dst_top, color, width, dash=False, lane_y=None,
                radius=8):
    """Professional orthogonal connector: exits the SOURCE bottom-center going
    down, runs horizontally along a mid-lane in the inter-row gap, then drops into
    the DESTINATION top-center with an arrowhead. Right-angle turns are rounded.
    `src_bottom`/`dst_top` are (x, y) anchor points. `lane_y` overrides the
    horizontal-run height (for spreading parallel edges); defaults to the gap
    midpoint. Dashed for soft/spine edges."""
    x0, y0 = src_bottom
    x1, y1 = dst_top
    ly = lane_y if lane_y is not None else (y0 + y1) / 2

    def seg(p, q):
        if dash:
            # dashed straight segment
            import math
            dx, dy = q[0] - p[0], q[1] - p[1]
            dist = max(1.0, math.hypot(dx, dy))
            steps = max(1, int(dist / 9))
            for i in range(steps):
                if i % 2 == 0:
                    t0, t1 = i / steps, min(1.0, (i + 0.6) / steps)
                    draw.line([(p[0] + dx * t0, p[1] + dy * t0),
                               (p[0] + dx * t1, p[1] + dy * t1)], fill=color, width=width)
        else:
            draw.line([p, q], fill=color, width=width)

    if abs(x1 - x0) < 2:
        # straight vertical — no elbow
        seg((x0, y0), (x1, y1 - 6))
        _arrowhead(draw, (x1, y1), "down", color, width)
        return

    r = min(radius, abs(x1 - x0) / 2, (ly - y0) / 2 if ly > y0 else radius,
            (y1 - ly) / 2 if y1 > ly else radius)
    r = max(3, r)
    going_right = x1 > x0
    # down from source to lane (stop short for the corner)
    seg((x0, y0), (x0, ly - r))
    # corner 1
    _round_corner(draw, x0 + (r if going_right else -r), ly - r, r,
                  "bl" if going_right else "br", color, width)
    # horizontal run along the lane
    seg((x0 + (r if going_right else -r), ly),
        (x1 - (r if going_right else -r), ly))
    # corner 2
    _round_corner(draw, x1 - (r if going_right else -r), ly + r, r,
                  "tr" if going_right else "tl", color, width)
    # down into destination
    seg((x1, ly + r), (x1, y1 - 6))
    _arrowhead(draw, (x1, y1), "down", color, width)

def compose_track(track, overall, title=None):
    """Render ONE track (data structures OR algorithms) as its own readable node-
    graph. `track` = one entry from roadmap_tracks(...)['tracks']; `overall` = the
    top-level roadmap_tracks(...) dict (for the shared progress + NOW strip). Draws
    in-track prereq arrows between nodes, and a dim '↖ needs X' ghost label for any
    prerequisite that lives in the OTHER track (so each graph is self-contained and
    honest). The gold START-HERE glow + NOW strip appear only when this track holds
    the current pick (track['has_now']); otherwise a subtle track hint shows."""
    from PIL import Image, ImageDraw

    tiers = track["tiers"]
    ttl = title or track["title"]

    # Layout: tier rows, barycenter-ordered to reduce crossings (same as graph).
    placed = {}
    rows_nodes = []
    max_row_w = max((len(tb["patterns"]) * _TILE_W
                     + (len(tb["patterns"]) - 1) * _G_TILE_GAP_X)
                    for tb in tiers) if tiers else _TILE_W
    canvas_w = max(max_row_w + 2 * _MARGIN, 720)

    y = _HEADER_H + _MARGIN
    for tb in tiers:
        pats = list(tb["patterns"])
        def bary(p):
            # Order by the average x of ALL in-track dependencies (core + soft +
            # the study-order spine link) so a node sits under what it connects to
            # → fewer crossings, and the spine reads as a clean continuation.
            deps = (p.get("in_prereqs") or []) + (p.get("in_recommended") or [])
            if p.get("spine_parent"):
                deps = deps + [p["spine_parent"]]
            xs = [placed[q][0] for q in deps if q in placed]
            return sum(xs) / len(xs) if xs else canvas_w / 2
        pats.sort(key=bary)
        row_w = len(pats) * _TILE_W + (len(pats) - 1) * _G_TILE_GAP_X
        x = (canvas_w - row_w) / 2
        row = []
        for p in pats:
            placed[p["key"]] = (x + _TILE_W / 2, y, y + _TILE_H)
            row.append((p, int(x), int(y)))
            x += _TILE_W + _G_TILE_GAP_X
        rows_nodes.append(row)
        y += _TILE_H + _G_ROW_GAP

    canvas_h = y - _G_ROW_GAP + _MARGIN
    img = Image.new("RGBA", (canvas_w, canvas_h), _BG + (255,))
    d = ImageDraw.Draw(img)

    # --- Edges: collect, then route ORTHOGONALLY with lane-spreading --------
    # Each edge is (src_key, dst_key, kind). Kinds: 'prereq' (solid), 'rec'
    # (dashed), 'spine' (dotted). We route them as clean right-angle connectors
    # through the inter-row gaps and spread parallel edges onto separate lanes so
    # nothing overlaps — the professional graph look.
    state_of = {p["key"]: p["state"] for tb in tiers for p in tb["patterns"]}
    tier_of = {p["key"]: tb["tier"] for tb in tiers for p in tb["patterns"]}

    edges = []
    for tb in tiers:
        for p in tb["patterns"]:
            k = p["key"]
            if k not in placed:
                continue
            for q in p.get("in_prereqs") or []:
                if q in placed:
                    edges.append((q, k, "prereq"))
            for q in p.get("in_recommended") or []:
                # Only draw a recommended edge when the suggested topic comes
                # BEFORE (or same tier as) this one and is within ~1 tier. Skipping
                # upward/backward recommendations avoids stray up-pointing arrows
                # (e.g. Stacks & Queues 'recommends' Linked Lists, which sits lower).
                if not (q in placed):
                    continue
                tq, tk = tier_of.get(q, 0), tier_of.get(k, 0)
                if tq <= tk and (tk - tq) <= 1:
                    edges.append((q, k, "rec"))
            sp = p.get("spine_parent")
            if sp and sp in placed:
                edges.append((sp, k, "spine"))

    # Row index of each node (by its top-y) so we can tell same-row from down-row.
    row_idx = {}
    for ri, row in enumerate(rows_nodes):
        for p, tx, ty in row:
            row_idx[p["key"]] = ri

    # LANE + EXIT spreading. Edges that leave the SAME source node, and edges that
    # share the same inter-row gap, each get a distinct horizontal lane AND a
    # distinct exit-x offset from the source bottom — so parallel connectors never
    # stack on top of each other.
    #   lane_for[e]  → fraction (0..1) down the gap for the horizontal run
    #   exit_dx[e]   → x offset from source center where the edge drops out
    down_edges = [e for e in edges if row_idx.get(e[1], 0) > row_idx.get(e[0], -1)]
    # group by the (source-row) gap for lane spreading
    by_gap = {}
    for e in down_edges:
        by_gap.setdefault(row_idx.get(e[0], 0), []).append(e)
    lane_for, exit_dx = {}, {}
    for _gap, group in by_gap.items():
        group.sort(key=lambda e: placed[e[1]][0])  # left→right by target
        n = len(group)
        for i, e in enumerate(group):
            lane_for[e] = (i + 1) / (n + 1)
    # exit-x: fan multiple edges leaving the same source node
    by_src = {}
    for e in down_edges:
        by_src.setdefault(e[0], []).append(e)
    for src, group in by_src.items():
        group.sort(key=lambda e: placed[e[1]][0])
        n = len(group)
        span = min(_TILE_W * 0.5, 18 * (n - 1))
        for i, e in enumerate(group):
            exit_dx[e] = (-span / 2 + span * i / (n - 1)) if n > 1 else 0

    def edge_lane_y(e):
        s, t = placed[e[0]], placed[e[1]]
        gap_top, gap_bot = s[2], t[1]
        if gap_bot <= gap_top:
            return (s[2] + t[1]) / 2
        frac = lane_for.get(e, 0.5)
        return gap_top + (gap_bot - gap_top) * (0.28 + 0.44 * frac)

    style = {
        "spine": (_EDGE_SPINE, 2, True),
        "rec": (_EDGE_SOFT, 2, True),
        "prereq": (_EDGE, 3, False),
    }
    # Draw order: spine → rec → prereq so the solid primary arrows sit on top.
    for kind in ("spine", "rec", "prereq"):
        for e in edges:
            if e[2] != kind:
                continue
            s, t = placed[e[0]], placed[e[1]]
            if kind == "prereq":
                done = state_of.get(e[0]) == "learned" and state_of.get(e[1]) == "learned"
                col = (_EDGE_DONE if done else _EDGE)
            else:
                col = style[kind][0]
            _, w, dash = style[kind]
            r_src, r_dst = row_idx.get(e[0], -1), row_idx.get(e[1], -2)
            same_row = r_src == r_dst
            if same_row:
                # Side-by-side nodes → a direct horizontal edge-to-edge connector.
                sy = (s[1] + s[2]) / 2
                if t[0] > s[0]:
                    x_from, x_to, dirn = s[0] + _TILE_W / 2, t[0] - _TILE_W / 2, "right"
                else:
                    x_from, x_to, dirn = s[0] - _TILE_W / 2, t[0] + _TILE_W / 2, "left"
                d.line([(x_from, sy), (x_to, sy)], fill=col + (255,), width=w)
                _arrowhead(d, (x_to, sy), dirn, col + (255,), w)
            elif r_dst > r_src:
                # Normal downward edge — clean orthogonal routing.
                dx = exit_dx.get(e, 0)
                _ortho_edge(d, (s[0] + dx, s[2]), (t[0], t[1]), col + (255,), w,
                            dash=dash, lane_y=edge_lane_y(e))
            # else: upward edge — skip (defensive; would render as an up-arrow).

    # Tiles first, then overlays in layers so nothing collides:
    #   1) tiles  2) NEXT ring + START glow  3) NEXT / START pills (top-left)
    #   4) ghost 'needs X' labels — RIGHT-aligned, and lifted above a pill when the
    #      same node carries one, so the label and pill never overlap.
    ghost_font = _font(11, bold=False)
    start_box = None
    next_boxes = []
    ghosts = []  # (tx, ty, has_pill, label)
    for row in rows_nodes:
        for p, tx, ty in row:
            tile = get_tile_image(p["key"], p["name"], p["state"])
            img.paste(tile, (tx, ty), tile)
            box = (tx, ty, tx + _TILE_W, ty + _TILE_H)
            has_pill = bool(p.get("is_start") or p.get("is_next_up"))
            cross = p.get("cross_prereqs") or []
            if cross:
                first = cross[0].split(" (")[0]  # drop parenthetical, e.g. "(BST)"
                label = f"↖ needs {first}" + ("…" if len(cross) > 1 else "")
                while (d.textlength(label, font=ghost_font) > _TILE_W - 8
                       and len(label) > 10):
                    label = label[:-2] + "…"
                ghosts.append((tx, ty, has_pill, label))
            if p.get("is_start"):
                start_box = box
            elif p.get("is_next_up"):
                next_boxes.append(box)

    for nb in next_boxes:
        _rounded(d, [nb[0] - 2, nb[1] - 2, nb[2] + 2, nb[3] + 2], 11,
                 outline=_NEXT + (255,), width=2)
        _pill(d, nb[0] + 4, nb[1] - 9, "NEXT", _NEXT)
    if start_box:
        sb = start_box
        for grow, alpha in ((5, 90), (3, 160), (1, 255)):
            _rounded(d, [sb[0] - grow, sb[1] - grow, sb[2] + grow, sb[3] + grow],
                     12 + grow, outline=_START + (alpha,), width=3)
        _pill(d, sb[0] + 4, sb[1] - 11, "▶ START HERE", _START, dark_text=True)

    # Ghost labels last: right-aligned to clear the left-side pill; lifted higher
    # (above the pill) when the node has one.
    for tx, ty, has_pill, label in ghosts:
        lw = d.textlength(label, font=ghost_font)
        lx = tx + _TILE_W - lw  # right-aligned to the tile
        ly = ty - 26 if has_pill else ty - 14
        d.text((lx, ly), label, font=ghost_font, fill=_TEXT_DIM)

    # Header.
    tfont = _font(26, bold=True)
    d.text((_MARGIN, _MARGIN - 8), ttl, font=tfont, fill=_VIOLET)
    sub = f"{overall['learned_count']}/{overall['total']} overall · {overall['percent']}%"
    d.text((_MARGIN, _MARGIN + 24), sub, font=_font(14, bold=False), fill=_TEXT_DIM)

    now = overall.get("now")
    gy = _MARGIN + 64
    if track["has_now"] and now:
        _pill(d, _MARGIN, gy - 2, "▶ NOW", _START, dark_text=True)
        x = _MARGIN + d.textlength("▶ NOW", font=_font(11, bold=True)) + 22
        d.text((x, gy), now["name"], font=_font(15, bold=True), fill=_TEXT)
        x += d.textlength(now["name"], font=_font(15, bold=True)) + 18
        nxt = overall.get("next_up") or []
        if nxt:
            d.text((x, gy), "→  next:", font=_font(14, bold=False), fill=_TEXT_DIM)
            x += d.textlength("→  next:", font=_font(14, bold=False)) + 8
            d.text((x, gy), ", ".join(p["name"] for p in nxt[:3]),
                   font=_font(14, bold=False), fill=_NEXT)
    else:
        # Subtle hint: the next unlearned pattern IN THIS track.
        hint = None
        for tb in tiers:
            for p in tb["patterns"]:
                if p["state"] in ("unlocked",) and not hint:
                    hint = p["name"]
        if hint:
            d.text((_MARGIN, gy), f"Next in this track: {hint}",
                   font=_font(14, bold=False), fill=_CYAN)

    # Legend (compact).
    legend = [("done", _VIOLET), ("ready", _CYAN), ("now", _START),
              ("next", _NEXT), ("locked", _LOCKED)]
    lx = canvas_w - _MARGIN
    lfont = _font(12, bold=False)
    for label, col in reversed(legend):
        w = d.textlength(label, font=lfont)
        lx -= w
        d.text((lx, _MARGIN + 4), label, font=lfont, fill=_TEXT_DIM)
        lx -= 11
        d.ellipse([lx - 9, _MARGIN + 6, lx, _MARGIN + 15], fill=col)
        lx -= 18
    # Edge-type note under the legend.
    note = "solid = core prereq   ⇢ dashed = recommended   ⋯ dotted = study order"
    nw = d.textlength(note, font=lfont)
    d.text((canvas_w - _MARGIN - nw, _MARGIN + 24), note, font=lfont, fill=_TEXT_DIM)

    out = img.convert("RGB")
    if _CANVAS_SCALE and _CANVAS_SCALE != 1:
        out = out.resize((int(out.width * _CANVAS_SCALE), int(out.height * _CANVAS_SCALE)), Image.LANCZOS)
    buf = io.BytesIO()
    out.save(buf, format="PNG")
    return buf.getvalue()


def compose_tracks(tracks_data):
    """Render BOTH tracks. Returns [(name, png_bytes), ...] in a sensible order:
    the track holding the current NOW pick first."""
    tracks = tracks_data["tracks"]
    order = sorted(tracks, key=lambda k: (not tracks[k]["has_now"], k))
    out = []
    for tkey in order:
        png = compose_track(tracks[tkey], tracks_data)
        out.append((tracks[tkey]["name"], png))
    return out

