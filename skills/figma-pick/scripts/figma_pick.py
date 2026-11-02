#!/usr/bin/env python3
"""figma-pick: exact CSS for the Figma elements you name, and nothing else.

Figma's REST API returns ~1.5 KB of JSON per layer, so one app screen is
100k-1M tokens, nearly all of it bookkeeping. figma-pick reads a frame once,
caches it for that file version, and prints only what you ask for:

    render   PNG of a frame or element, sized to what the model actually sees
    find     literal CSS for the layers whose name or visible text matches
    outline  the layer tree with node ids, to choose what to find or render
    css      literal CSS for one subtree
    text     every visible string

Every command takes a Figma link ("Copy link to selection" adds the node-id).
A link without a node-id lists the file's frames instead. Auth: FIGMA_TOKEN.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import math
import os
import re
import shutil
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

try:
    import fcntl
except ImportError:  # Windows: no cross-process lock; at worst a duplicate read
    fcntl = None

VERSION = "0.1.1"
API = "https://api.figma.com/v1"
CACHE = Path(os.environ.get("FIGMA_PICK_CACHE", "~/.cache/figma-pick")).expanduser()
META_FRESH_S = 60  # version checks are cheap (rate-limit Tier 3) but not free
TTL_S = 3600  # cache lifetime when the token cannot read file versions
MAX_TOKENS = 4000
MAX_GROUPS = 8
FILE_FRAMES = 150  # a link without node-id lists frames up to this many, else pages
CHARS_PER_TOKEN = 3  # hex- and symbol-dense text tokenizes worse than prose
IMAGE_LONG_EDGE = 1568  # the model downscales larger images; ~w*h/750 tokens after
IMAGE_MAX_PIXELS = 1_150_000
SCALES = (4, 3, 2, 1.5, 1, 0.75, 0.5)
VECTORS = {"VECTOR", "BOOLEAN_OPERATION", "STAR", "REGULAR_POLYGON", "LINE"}
AUTO_NAME = re.compile(r"^(Frame|Group|Rectangle|Vector|Ellipse|Line|Component) \d+$")
LINK = re.compile(
    r"figma\.com/(?:file|design|proto|board|slides|make)/([A-Za-z0-9]+)" r"(?:/branch/([A-Za-z0-9]+))?"
)
JUSTIFY = {"CENTER": "center", "MAX": "flex-end", "SPACE_BETWEEN": "space-between"}
ALIGN = {"MIN": "flex-start", "CENTER": "center", "MAX": "flex-end", "BASELINE": "baseline"}
TEXT_ALIGN = {"CENTER": "center", "RIGHT": "right", "JUSTIFIED": "justify"}
TEXT_CASE = {
    "UPPER": "text-transform:uppercase",
    "LOWER": "text-transform:lowercase",
    "TITLE": "text-transform:capitalize",
    "SMALL_CAPS": "font-variant-caps:small-caps",
    "SMALL_CAPS_FORCED": "font-variant-caps:all-small-caps",
}
DECORATION = {"UNDERLINE": "underline", "STRIKETHROUGH": "line-through"}
BLEND = {"LINEAR_DODGE": "plus-lighter", "LINEAR_BURN": "plus-darker"}
STATS = {"file read": 0, "render": 0, "version check": 0}


class FigmaError(Exception):
    def __init__(self, message: str, status: int = 0):
        super().__init__(message)
        self.status = status


def warn(message: str) -> None:
    print(f"figma-pick: {message}", file=sys.stderr)


# --------------------------------------------------------------------------- figma api


def figma_token() -> str:
    for name in ("FIGMA_TOKEN", "FIGMA_API_KEY"):
        if value := os.environ.get(name, "").strip():
            return value
    try:  # reuse the token of a Framelink `figma` MCP server, if one is configured
        config = Path("~/.claude.json").expanduser().read_text()
    except OSError:
        config = ""
    if match := re.search(r'"FIGMA_API_KEY"\s*:\s*"([^"]+)"|--figma-api-key=([^"\s]+)', config):
        return match.group(1) or match.group(2)
    raise FigmaError(
        "no Figma token. Create one in Figma > Settings > Security > Personal access tokens"
        " (scopes: File content read, File metadata read), then export FIGMA_TOKEN=..."
    )


def http_get(url: str, token: str | None = None) -> bytes:
    """GET url; tests replace this function."""
    headers = {"User-Agent": f"figma-pick/{VERSION}"}
    if token:
        headers["X-Figma-Token"] = token
    request = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            return response.read()
    except urllib.error.HTTPError as error:
        raise api_error(error.code, error.headers, error.read()) from None
    except urllib.error.URLError as error:
        raise FigmaError(f"network error: {error.reason}") from None


def api_error(status: int, headers, body: bytes) -> FigmaError:
    try:
        detail = json.loads(body).get("err") or ""
    except (ValueError, AttributeError):
        detail = ""
    if status == 429:
        message = f"Figma rate limit; retry in {duration(int(headers.get('Retry-After') or 0))}."
        if headers.get("X-Figma-Rate-Limit-Type") == "low":
            message += " View and Collab seats get about 20 file reads a month."
        return FigmaError(message, status)
    hint = {
        403: "the token is invalid, expired, or cannot open this file",
        404: "file or node not found; copy the link from Figma again",
    }.get(status, "")
    return FigmaError(f"Figma API {status}: {'; '.join(p for p in (detail, hint) if p)}", status)


def duration(seconds: int) -> str:
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if seconds >= 2 * size:
            return f"{seconds // size}{unit}"
    return f"{seconds}s"


def api(path: str, kind: str) -> bytes:
    STATS[kind] += 1
    return http_get(f"{API}/{path}", figma_token())


# --------------------------------------------------------------------------- cache


def read_json(path: Path):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    temp.write_text(json.dumps(data))
    os.replace(temp, path)  # atomic: a parallel reader never sees half a file


def slug(node_id: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "-", node_id)


@contextlib.contextmanager
def locked(path: Path):
    """One fetch per file at a time, so parallel sub-agents share a single API read."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as handle:
        if fcntl:
            fcntl.flock(handle, fcntl.LOCK_EX)
        yield


def file_version(key: str, refresh: bool = False) -> str | None:
    """The file's current version, from the cheap metadata endpoint; None if unreadable.

    Figma has no per-node hash or ETag; a file-level version plus last-modified time is the
    finest change signal there is. Both go in the key, so any edit means a fresh read.
    """
    path = CACHE / key / "meta.json"
    meta = read_json(path)
    if meta and not refresh and time.time() - meta["checked"] < META_FRESH_S:
        return meta["version"]
    try:
        file = json.loads(api(f"files/{key}/meta", "version check"))["file"]
        touched = re.sub(r"\D", "", file.get("last_touched_at") or "")
        version = f"{file['version']}-{touched}" if touched else str(file["version"])
    except FigmaError as error:
        if error.status == 403:  # a token without the file_metadata:read scope
            warn("token can't read file versions (scope File metadata read); caching for 1h")
            version = None
        elif meta and (error.status in (0, 429) or error.status >= 500):
            warn(f"version check failed ({error}); using the cached copy")
            return meta["version"]
        else:
            raise
    write_json(path, {"version": version, "checked": time.time()})
    return version


def folder(key: str, version: str | None) -> Path:
    return CACHE / key / (version or "unversioned")


def fresh(path: Path, version: str | None) -> bool:
    return path.exists() and (bool(version) or time.time() - path.stat().st_mtime < TTL_S)


def find_id(root: dict, node_id: str) -> dict | None:
    stack = [root]
    while stack:
        node = stack.pop()
        if node.get("id") == node_id:
            return node
        stack.extend(node.get("children", []))
    return None


def cached_node(where: Path, node_id: str, version: str | None) -> dict | None:
    """node_id from any tree cached in `where`: its own fetch, or a fetched ancestor's."""
    if not where.is_dir():
        return None
    exact = where / f"{slug(node_id)}.json"
    for path in [exact, *sorted(p for p in where.glob("*.json") if p != exact)]:
        if not fresh(path, version):
            continue
        entry = read_json(path)
        node = entry and find_id(entry["document"], node_id)
        if node:
            return {**entry, "document": node}
    return None


def stale_node(key: str, node_id: str) -> dict | None:
    folders = [p for p in (CACHE / key).glob("*") if p.is_dir()]
    for where in sorted(folders, key=lambda p: p.stat().st_mtime, reverse=True):
        if entry := cached_node(where, node_id, "stale"):
            return entry
    return None


def load_node(key: str, node_id: str, refresh: bool = False) -> dict:
    """The node's whole subtree plus component and style names: one API read per file version."""
    version = file_version(key, refresh)
    where = folder(key, version)
    if not refresh and (entry := cached_node(where, node_id, version)):
        return entry
    with locked(CACHE / key / ".lock"):
        if not refresh and (entry := cached_node(where, node_id, version)):
            return entry  # a parallel call fetched it while this one waited
        try:
            raw = api(f"files/{key}/nodes?ids={urllib.parse.quote(node_id)}", "file read")
        except FigmaError as error:
            stale = stale_node(key, node_id) if error.status == 429 else None
            if not stale:
                raise
            warn(f"{error} Using the cached copy from an earlier file version.")
            return stale
        found = (json.loads(raw).get("nodes") or {}).get(node_id)
        if not found or not found.get("document"):
            raise FigmaError(f"node {node_id} not found in file {key}", 404)
        entry = {k: found.get(k) or {} for k in ("document", "components", "componentSets", "styles")}
        write_json(where / f"{slug(node_id)}.json", entry)
        if version:  # one version per file is enough; drop the rest
            for old in (CACHE / key).iterdir():
                if old.is_dir() and old != where:
                    shutil.rmtree(old, ignore_errors=True)
        return entry


def load_pages(key: str, refresh: bool = False) -> list[dict]:
    """Pages and their top-level frames: what to pick from when a link has no node-id."""
    version = file_version(key, refresh)
    path = folder(key, version) / "pages" / "file.json"
    pages = None if refresh or not fresh(path, version) else read_json(path)
    if pages is None:
        pages = json.loads(api(f"files/{key}?depth=2", "file read"))["document"].get("children", [])
        write_json(path, pages)
    return pages


# --------------------------------------------------------------------------- figma -> css


def num(value: float) -> str:
    return f"{round(value, 2) + 0.0:.2f}".rstrip("0").rstrip(".")


def px(value: float) -> str:
    text = num(value)
    return text if text == "0" else f"{text}px"


def sides(values) -> str:
    """CSS shorthand for top/right/bottom/left, or the four corners (same collapsing rules)."""
    top, right, bottom, left = (px(v) for v in values)
    if top == right == bottom == left:
        return top
    if top == bottom and right == left:
        return f"{top} {right}"
    if right == left:
        return f"{top} {right} {bottom}"
    return f"{top} {right} {bottom} {left}"


def color(rgba: dict, opacity: float = 1) -> str:
    r, g, b = (int(rgba[c] * 255 + 0.5) for c in "rgb")
    alpha = round(rgba.get("a", 1) * opacity, 3)  # 3 decimals reproduce Figma's 8-bit alpha
    return f"#{r:02x}{g:02x}{b:02x}" if alpha >= 1 else f"rgba({r},{g},{b},{alpha:g})"


def visible(paints) -> list[dict]:
    return [p for p in paints or [] if p.get("visible", True)]


def handles(paint: dict, w: float, h: float) -> list[tuple[float, float]]:
    return [(p["x"] * w, p["y"] * h) for p in paint["gradientHandlePositions"][:3]]


def stops(paint: dict, position) -> str:
    opacity = paint.get("opacity", 1)
    return ", ".join(
        f"{color(stop['color'], opacity)} {num(position(stop['position']) * 100)}%"
        for stop in paint.get("gradientStops", [])
    )


def linear_gradient(paint: dict, w: float, h: float) -> str:
    """Exact CSS for a Figma linear gradient, including ones stretched with their layer."""
    (x0, y0), (x1, y1), (x2, y2) = handles(paint, w, h)
    dx, dy = x1 - x0, y1 - y0
    nx, ny = y0 - y2, x2 - x0  # normal to the colour bands, which run along handle 0 -> 2
    if not (nx or ny):
        nx, ny = dx, dy
    if nx * dx + ny * dy < 0:
        nx, ny = -nx, -ny
    size = math.hypot(nx, ny) or 1
    ux, uy = nx / size, ny / size
    line = abs(w * ux) + abs(h * uy) or 1  # length of the CSS gradient line at this angle
    start = (w / 2 - ux * line / 2 - x0) * ux + (h / 2 - uy * line / 2 - y0) * uy
    span = dx * ux + dy * uy
    angle = math.degrees(math.atan2(ux, -uy)) % 360
    return f"linear-gradient({num(angle)}deg, {stops(paint, lambda s: (s * span - start) / line)})"


def radial_gradient(paint: dict, w: float, h: float, notes: list[str]) -> str:
    (x0, y0), (x1, y1), (x2, y2) = handles(paint, w, h)
    ax, ay, bx, by = x1 - x0, y1 - y0, x2 - x0, y2 - y0
    if abs(ay) < 0.01 and abs(bx) < 0.01:
        rx, ry = abs(ax), abs(by)
    elif abs(ax) < 0.01 and abs(by) < 0.01:
        rx, ry = abs(bx), abs(ay)
    else:
        rx, ry = math.hypot(ax, ay), math.hypot(bx, by)
        notes.append("radial gradient is rotated: compare with the render")
    return f"radial-gradient({px(rx)} {px(ry)} at {px(x0)} {px(y0)}, {stops(paint, lambda s: s)})"


def paint_css(paint: dict, w: float, h: float, notes: list[str]) -> str | None:
    kind = paint.get("type", "")
    if kind == "SOLID":
        return color(paint["color"], paint.get("opacity", 1))
    if kind == "GRADIENT_LINEAR":
        return linear_gradient(paint, w, h)
    if kind == "GRADIENT_RADIAL":
        return radial_gradient(paint, w, h, notes)
    if kind.startswith("GRADIENT_"):
        notes.append(f"{kind[9:].lower()} gradient ({stops(paint, lambda s: s)}): see render")
    else:
        notes.append(f"{kind.lower()} fill: see render")
    return None


def background(paints, w: float, h: float, notes: list[str]) -> str | None:
    """Figma lists paints bottom-up; CSS lists layers top-down and allows a bare colour last only."""
    layers = [css for paint in reversed(visible(paints)) if (css := paint_css(paint, w, h, notes))]
    return (
        ", ".join(
            f"linear-gradient({v}, {v})" if i < len(layers) - 1 and v.startswith(("#", "rgba(")) else v
            for i, v in enumerate(layers)
        )
        or None
    )


def layout(node: dict, notes: list[str]) -> list[str]:
    mode = node.get("layoutMode") or "NONE"
    kids = [c for c in node.get("children", []) if c.get("visible", True)]
    if mode == "NONE":
        return ["position:relative"] if kids and node["type"] not in VECTORS else []
    if mode == "GRID":
        notes.append("grid tracks not captured: see render")
        decls = ["display:grid"]
    else:
        decls = ["display:flex"]
        if mode == "VERTICAL":
            decls.append("flex-direction:column")
        wrap = node.get("layoutWrap") == "WRAP"
        if wrap:
            decls.append("flex-wrap:wrap")
        justify = node.get("primaryAxisAlignItems", "MIN")
        if justify in JUSTIFY:
            decls.append(f"justify-content:{JUSTIFY[justify]}")
        decls.append(f"align-items:{ALIGN.get(node.get('counterAxisAlignItems', 'MIN'), 'flex-start')}")
        gap = 0 if justify == "SPACE_BETWEEN" else node.get("itemSpacing", 0)
        rows = (node.get("counterAxisSpacing") or 0) if wrap else 0
        if gap < 0:
            notes.append(f"children overlap by {px(-gap)} (negative spacing)")
            gap = 0
        if rows:
            decls.append(f"gap:{px(rows)} {px(gap)}")
        elif gap:
            decls.append(f"gap:{px(gap)}")
        if wrap and node.get("counterAxisAlignContent") == "SPACE_BETWEEN":
            decls.append("align-content:space-between")
    if any(c.get("layoutPositioning") == "ABSOLUTE" for c in kids):
        decls.append("position:relative")
    pad = [node.get(f"padding{side}", 0) for side in ("Top", "Right", "Bottom", "Left")]
    if any(pad):
        decls.append(f"padding:{sides(pad)}")
    return decls


def sizing(node: dict, flow: str | None, w: float, h: float) -> list[str]:
    """width/height the way the layer is sized inside its parent's auto layout."""
    auto = (node.get("style") or {}).get("textAutoResize") if node["type"] == "TEXT" else None
    default = {"WIDTH_AND_HEIGHT": ("HUG", "HUG"), "HEIGHT": ("FIXED", "HUG")}.get(auto, ("FIXED", "FIXED"))
    decls = []
    for prop, axis, size, mode in (
        ("width", "HORIZONTAL", w, node.get("layoutSizingHorizontal") or default[0]),
        ("height", "VERTICAL", h, node.get("layoutSizingVertical") or default[1]),
    ):
        if mode == "FIXED":
            decls.append(f"{prop}:{px(size)}")
        elif mode == "FILL":
            if flow == axis:
                decls.append("flex:1 0 0")
            elif flow in ("HORIZONTAL", "VERTICAL"):
                decls.append("align-self:stretch")
            else:
                decls.append(f"{prop}:100%")  # parent not shown
    for key, prop in (
        ("minWidth", "min-width"),
        ("maxWidth", "max-width"),
        ("minHeight", "min-height"),
        ("maxHeight", "max-height"),
    ):
        if node.get(key) is not None:
            decls.append(f"{prop}:{px(node[key])}")
    return decls


def border(node: dict, notes: list[str]) -> list[str]:
    strokes = visible(node.get("strokes"))
    if not strokes:
        return []
    paint = strokes[-1]  # the topmost stroke
    if paint.get("type") != "SOLID":
        notes.append(f"{paint.get('type', '').lower()} stroke: see render")
        return []
    ink = color(paint["color"], paint.get("opacity", 1))
    weight = node.get("strokeWeight", 1)
    if node["type"] in VECTORS:
        return [f"stroke:{ink}", f"stroke-width:{px(weight)}"]
    if node["type"] == "TEXT":
        return [f"-webkit-text-stroke:{px(weight)} {ink}"]
    style = "dashed" if node.get("strokeDashes") else "solid"
    per_side = node.get("individualStrokeWeights")
    if per_side and len(set(per_side.values())) > 1:
        return [f"border-{side}:{px(width)} {style} {ink}" for side, width in per_side.items() if width]
    if per_side:
        weight = next(iter(per_side.values()))
    if not weight:
        return []
    align = node.get("strokeAlign", "INSIDE")
    if align == "INSIDE":
        return [f"border:{px(weight)} {style} {ink}"]
    offset = [f"outline-offset:{px(-weight / 2)}"] if align == "CENTER" else []
    return [f"outline:{px(weight)} {style} {ink}", *offset]


def radius(node: dict) -> list[str]:
    if node["type"] == "ELLIPSE":
        return ["border-radius:50%"]
    corners = node.get("rectangleCornerRadii")
    if corners and len(set(corners)) > 1:
        return [f"border-radius:{sides(corners)}"]
    if value := node.get("cornerRadius") or (corners or [0])[0]:
        return [f"border-radius:{px(value)}"]
    return []


def effects(node: dict, notes: list[str]) -> list[str]:
    text = node["type"] == "TEXT"
    shadows, decls = [], []
    for effect in node.get("effects") or []:
        if not effect.get("visible", True):
            continue
        kind = effect["type"]
        if kind in ("DROP_SHADOW", "INNER_SHADOW"):
            if text and kind == "INNER_SHADOW":
                notes.append("inner shadow on text: see render")
                continue
            offset = effect.get("offset", {})
            parts = [px(offset.get("x", 0)), px(offset.get("y", 0)), px(effect.get("radius", 0))]
            if effect.get("spread") and not text:
                parts.append(px(effect["spread"]))
            inset = "inset " if kind == "INNER_SHADOW" else ""
            shadows.append(f"{inset}{' '.join(parts)} {color(effect['color'])}")
        elif kind == "LAYER_BLUR":  # Figma's blur radius is twice CSS's standard deviation
            decls.append(f"filter:blur({px(effect.get('radius', 0) / 2)})")
        elif kind == "BACKGROUND_BLUR":
            decls.append(f"backdrop-filter:blur({px(effect.get('radius', 0) / 2)})")
        else:
            notes.append(f"{kind.lower()} effect: see render")
    if shadows:
        decls.insert(0, f"{'text-shadow' if text else 'box-shadow'}:{', '.join(shadows)}")
    return decls


def text_css(style: dict, fills, w: float, h: float, notes: list[str]) -> list[str]:
    line_px = style.get("lineHeightPx")
    height = "normal" if style.get("lineHeightUnit") == "INTRINSIC_%" or not line_px else px(line_px)
    italic = "italic " if style.get("italic") or "italic" in style.get("fontStyle", "").lower() else ""
    weight, size = num(style.get("fontWeight", 400)), px(style.get("fontSize", 16))
    decls = [f"font:{italic}{weight} {size}/{height} \"{style.get('fontFamily', '')}\""]
    if spacing := style.get("letterSpacing"):
        decls.append(f"letter-spacing:{px(spacing)}")
    if align := TEXT_ALIGN.get(style.get("textAlignHorizontal")):
        decls.append(f"text-align:{align}")
    if case := TEXT_CASE.get(style.get("textCase")):
        decls.append(case)
    if line := DECORATION.get(style.get("textDecoration")):
        decls.append(f"text-decoration:{line}")
    if flags := style.get("opentypeFlags"):
        features = ",".join(f'"{k.lower()}"' + ("" if v == 1 else f" {v}") for k, v in sorted(flags.items()))
        decls.append(f"font-feature-settings:{features}")
    if style.get("leadingTrim") == "CAP_HEIGHT":
        decls.append("text-box:trim-both cap alphabetic")
    if indent := style.get("paragraphIndent"):
        decls.append(f"text-indent:{px(indent)}")
    if style.get("textTruncation") == "ENDING":
        lines = style.get("maxLines") or (max(1, round(h / line_px)) if line_px else 1)
        if lines == 1:
            decls += ["overflow:hidden", "text-overflow:ellipsis", "white-space:nowrap"]
        else:
            decls += [
                "display:-webkit-box",
                f"-webkit-line-clamp:{lines}",
                "-webkit-box-orient:vertical",
                "overflow:hidden",
            ]
    if spacing := style.get("paragraphSpacing"):
        notes.append(f"paragraph spacing {px(spacing)}")
    if link := (style.get("hyperlink") or {}).get("url"):
        notes.append(f"links to {link}")
    paints = visible(fills)
    if len(paints) == 1 and paints[0].get("type") == "SOLID":
        decls.append(f"color:{color(paints[0]['color'], paints[0].get('opacity', 1))}")
    elif fill := background(paints, w, h, notes):
        decls += [f"background:{fill}", "-webkit-background-clip:text", "color:transparent"]
    return decls


def css(node: dict, parent: dict | None, level: int) -> tuple[list[str], list[str]]:
    """Literal CSS declarations for one layer, plus notes on what CSS can't carry."""
    decls: list[str] = []
    notes: list[str] = []
    kind = node["type"]
    box = node.get("absoluteBoundingBox") or {}
    w, h = box.get("width", 0), box.get("height", 0)
    flow = ((parent or {}).get("layoutMode") or "NONE") if parent is not None else None
    if level and flow is not None and (flow == "NONE" or node.get("layoutPositioning") == "ABSOLUTE"):
        origin = parent.get("absoluteBoundingBox") or {}
        decls += [
            "position:absolute",
            f"left:{px(box.get('x', 0) - origin.get('x', 0))}",
            f"top:{px(box.get('y', 0) - origin.get('y', 0))}",
        ]
    decls += layout(node, notes)
    decls += sizing(node, flow, w, h)
    if kind == "TEXT":
        decls += text_css(node.get("style") or {}, node.get("fills"), w, h, notes)
    elif kind in VECTORS:
        for paint in visible(node.get("fills"))[-1:]:
            if paint.get("type") == "SOLID":
                decls.append(f"fill:{color(paint['color'], paint.get('opacity', 1))}")
            else:
                notes.append(f"{paint.get('type', '').lower()} fill: see render")
    elif fill := background(node.get("fills"), w, h, notes):
        decls.append(f"background:{fill}")
    decls += border(node, notes)
    if kind not in VECTORS:
        decls += radius(node)
    decls += effects(node, notes)
    if (opacity := node.get("opacity", 1)) < 1:
        decls.append(f"opacity:{round(opacity, 3):g}")
    if (blend := node.get("blendMode", "PASS_THROUGH")) not in ("PASS_THROUGH", "NORMAL"):
        decls.append(f"mix-blend-mode:{BLEND.get(blend, blend.lower().replace('_', '-'))}")
    if node.get("clipsContent") and node.get("children"):
        decls.append("overflow:hidden")
    if abs(node.get("rotation") or 0) > 0.001:
        notes.append("rotated: see render")
    return decls, notes


def quoted(text: str, limit: int = 60) -> str:
    return json.dumps(text if len(text) <= limit else text[: limit - 1] + "…", ensure_ascii=False)


def first_text(node: dict) -> str:
    stack = [node]
    while stack:
        current = stack.pop(0)
        if current["type"] == "TEXT" and current.get("characters", "").strip():
            return current["characters"].strip()
        stack.extend(c for c in current.get("children", []) if c.get("visible", True))
    return ""


def text_runs(node: dict, level: int) -> list[str]:
    """Spans of a mixed-style text layer, each with only the declarations that differ."""
    table = node.get("styleOverrideTable") or {}
    marks = node.get("characterStyleOverrides") or []
    if node["type"] != "TEXT" or not table or not marks:
        return []
    text, style = node.get("characters", ""), node.get("style") or {}
    box = node.get("absoluteBoundingBox") or {}
    w, h = box.get("width", 0), box.get("height", 0)
    base = set(text_css(style, node.get("fills"), w, h, []))
    marks = [*marks, *[0] * (len(text) - len(marks))]
    out, start = [], 0
    for end in range(1, len(text) + 1):
        if end < len(text) and marks[end] == marks[start]:
            continue
        override = table.get(str(marks[start])) if marks[start] else None
        if override:
            fills = override.get("fills", node.get("fills"))
            decls = [d for d in text_css({**style, **override}, fills, w, h, []) if d not in base]
            if decls:
                out.append("  " * level + f"span {quoted(text[start:end])} {{ {'; '.join(decls)} }}")
        start = end
    return out


class Printer:
    """One line per layer: TYPE "label" WxH [id] (Component: props) { literal css } /* notes */"""

    def __init__(self, entry: dict, ids: bool = False):
        self.components = entry.get("components") or {}
        self.sets = entry.get("componentSets") or {}
        self.styles = entry.get("styles") or {}
        self.ids = ids

    def lines(
        self,
        node: dict,
        parent: dict | None = None,
        depth: int | None = None,
        level: int = 0,
        sig: bool = False,
    ) -> list[str]:
        """sig=True drops labels, sizes and ids: two layers with equal sig render identically."""
        decls, notes = css(node, parent, level)
        line = "  " * level + self.header(node, level, sig)
        if decls:
            line += " { " + "; ".join(decls) + " }"
        kids = [c for c in node.get("children", []) if c.get("visible", True)]
        if kids and depth is not None and level >= depth:
            notes.append(f"{len(kids)} children not shown: raise --depth")
        if notes:
            line += " /* " + "; ".join(notes) + " */"
        out = [line, *text_runs(node, level + 1)]
        if kids and (depth is None or level < depth):
            out += self.children(kids, node, depth, level + 1, sig)
        return out

    def header(self, node: dict, level: int, sig: bool = False) -> str:
        parts = [node["type"]]
        if not sig:
            parts.append(
                quoted(node.get("characters", "") if node["type"] == "TEXT" else node.get("name", ""))
            )
            if box := node.get("absoluteBoundingBox"):
                parts.append(f"{num(box['width'])}x{num(box['height'])}")
            if level == 0 or self.ids:
                parts.append(f"[{node['id']}]")
        if node["type"] == "INSTANCE":
            parts.append(self.component(node))
        refs = [
            f"{k}={self.styles[v]['name']}" for k, v in (node.get("styles") or {}).items() if v in self.styles
        ]
        if refs:
            parts.append(f"/* {' '.join(refs)} */")
        return " ".join(p for p in parts if p)

    def component(self, node: dict) -> str:
        part = self.components.get(node.get("componentId"), {})
        name = self.sets.get(part.get("componentSetId"), {}).get("name") or part.get("name", "")
        props = []
        for key, prop in (node.get("componentProperties") or {}).items():
            if prop.get("type") in ("VARIANT", "BOOLEAN"):
                value = prop.get("value")
                props.append(
                    f"{key.split('#')[0]}={str(value).lower() if isinstance(value, bool) else value}"
                )
        inner = ", ".join(props)
        if name and inner:
            return f"({name}: {inner})"
        return f"({name or inner})" if name or inner else ""

    def children(self, kids: list[dict], parent: dict, depth: int | None, level: int, sig: bool) -> list[str]:
        """Children, with runs of look-alike siblings (table rows, list items) printed once."""
        shapes = ["\n".join(self.lines(k, parent, depth, level, sig=True)) for k in kids]
        out, i = [], 0
        while i < len(kids):
            j = i + 1
            while j < len(kids) and shapes[j] == shapes[i]:
                j += 1
            out += self.lines(kids[i], parent, depth, level, sig)
            if j - i > 1:
                texts = list(dict.fromkeys(t for k in kids[i + 1 : j] if (t := first_text(k))))
                more = "" if sig or not texts else ": " + ", ".join(quoted(t, 30) for t in texts[:4])
                out.append(
                    "  " * level
                    + f"+{j - i - 1} more like this{more}{' …' if len(texts) > 4 and not sig else ''}"
                )
            i = j
        return out


# --------------------------------------------------------------------------- find


def styled(node: dict) -> bool:
    """Does the layer draw anything itself (vs. a bare layout wrapper)?"""
    return (
        node["type"] in ("INSTANCE", "COMPONENT")
        or bool(visible(node.get("fills")) or visible(node.get("strokes")))
        or any(e.get("visible", True) for e in node.get("effects") or [])
        or bool(node.get("cornerRadius"))
    )


def find(root: dict, needle: str) -> list[tuple]:
    """Visible layers under root whose name or text contains needle.

    Returns (target, parent, ancestors, exact, up). A text hit reports its nearest styled
    ancestor, up to 3 levels: naming visible text means you want the element around it
    (the button, the tab with its underline), not the glyph run. `exact` is True when the
    whole name or text equals needle, so a common word can fall back to exact hits.
    """
    hits: dict[str, tuple] = {}

    def visit(node: dict, ancestors: tuple) -> None:
        name, text = node.get("name", "").lower(), node.get("characters", "").lower()
        if ancestors and needle in f"{name}\n{text}":
            up = 0
            if node["type"] == "TEXT":  # climb toward a styled container, never onto the root
                up = next((i for i, a in enumerate(reversed(ancestors[1:][-3:]), 1) if styled(a)), 0)
            cut = len(ancestors) - up
            target = ancestors[cut] if up else node
            exact = needle in (name.strip(), text.strip())
            known = hits.get(target["id"])
            if known is None or (exact and not known[3]):
                hits[target["id"]] = (target, ancestors[cut - 1], ancestors[:cut], exact, up)
        for child in node.get("children", []):
            if child.get("visible", True):
                visit(child, (*ancestors, node))

    visit(root, ())
    return list(hits.values())


def group(printer: Printer, hits: list[tuple], depth: int) -> list[list[tuple]]:
    """Hits that would print identically (repeated cells, list items) share one entry."""
    groups: dict[str, list[tuple]] = {}
    for hit in hits:
        node, parent, _, _, up = hit
        shape = "\n".join(printer.lines(node, parent, max(depth, up), sig=True))
        groups.setdefault(shape, []).append(hit)
    return list(groups.values())


def short_path(ancestors) -> str:
    names = [a.get("name", "") for a in ancestors]
    return " > ".join([n for n in names if n and not AUTO_NAME.match(n)][-3:])


def label(node: dict) -> str:
    return node.get("characters", "") if node["type"] == "TEXT" else node.get("name", "")


# --------------------------------------------------------------------------- commands


def target(args: argparse.Namespace, required: bool = True) -> tuple[str, str | None]:
    """(file key, node id) from the link; --node overrides the link's node-id."""
    match = LINK.search(args.url)
    if not match:
        raise FigmaError(f"not a Figma file link: {args.url}")
    key = match.group(2) or match.group(1)
    node = getattr(args, "node", None)
    if isinstance(node, list):  # render takes several --node values and handles them itself
        node = None
    node = node or urllib.parse.parse_qs(urllib.parse.urlparse(args.url).query).get("node-id", [""])[0]
    node = node.replace("-", ":") or None
    if required and not node:
        print("\n".join(page_lines(load_pages(key, args.refresh))))
        raise FigmaError(
            "this link has no node-id. Pick a frame above and add --node <id>"
            " (or use 'Copy link to selection' in Figma)."
        )
    return key, node


def page_lines(pages: list[dict], limit: int = FILE_FRAMES) -> list[str]:
    total = sum(len(p.get("children", [])) for p in pages)
    out = []
    for page in pages:
        frames = [f for f in page.get("children", []) if f.get("visible", True)]
        if total > limit:
            out.append(f"PAGE {quoted(page.get('name', ''))} [{page['id']}]  {len(frames)} frames")
            continue
        out.append(f"PAGE {quoted(page.get('name', ''))} [{page['id']}]")
        for frame in frames:
            box = frame.get("absoluteBoundingBox") or {}
            size = f"{num(box.get('width', 0))}x{num(box.get('height', 0))}"
            out.append(f"  {frame['type']} {quoted(frame.get('name', ''))} {size} [{frame['id']}]")
    if total > limit:
        out.append("# too many frames to list; `outline <link> --node <page id>` lists one page")
    return out


def outline_lines(printer: Printer, node: dict, depth: int, level: int = 0) -> list[str]:
    out = ["  " * level + printer.header(node, 0)]
    kids = [c for c in node.get("children", []) if c.get("visible", True)]
    if level >= depth:
        if kids:
            out[-1] += f"  (+{len(kids)} children)"
        return out
    i = 0
    while i < len(kids):
        j = i + 1
        same = (kids[i]["type"], kids[i].get("name"))
        while kids[i]["type"] != "TEXT" and j < len(kids) and (kids[j]["type"], kids[j].get("name")) == same:
            j += 1
        out += outline_lines(printer, kids[i], depth, level + 1)
        if j - i > 1:
            out.append("  " * (level + 1) + f"+{j - i - 1} more {quoted(kids[i].get('name', ''))}")
        i = j
    return out


def estimate(text: str) -> int:
    return math.ceil(len(text) / CHARS_PER_TOKEN)


def api_calls() -> str:
    return ", ".join(f"{n} {kind}" for kind, n in STATS.items() if n) or "none (cached)"


def emit(lines: list[str], args: argparse.Namespace, root: dict | None = None) -> None:
    text = "\n".join(lines)
    tokens = estimate(text)
    raw = f", node JSON ~{estimate(json.dumps(root, separators=(',', ':'))):,}" if root else ""
    footer = f"# ~{tokens:,} tokens{raw} · figma api: {api_calls()}"
    if args.out:
        Path(args.out).write_text(text + "\n")
        print(f"wrote {args.out}\n{footer}")
        return
    if tokens > args.max_tokens:
        raise FigmaError(
            f"output would be ~{tokens:,} tokens (limit {args.max_tokens:,}). Narrow it: `find` the"
            " elements you need, `outline` then --node a smaller layer, or a lower --depth."
            " `render` anything you only need to see."
        )
    print(text)
    print(footer)


def cmd_find(args: argparse.Namespace) -> None:
    key, node_id = target(args)
    entry = load_node(key, node_id, args.refresh)
    printer = Printer(entry, args.ids)
    out: list[str] = []
    for term in args.terms:
        hits = find(entry["document"], term.lower())
        groups = group(printer, hits, args.depth)
        if len(groups) > MAX_GROUPS:
            exact = group(printer, [h for h in hits if h[3]], args.depth)
            if 0 < len(exact) <= MAX_GROUPS:
                out.append(f"# {term!r}: {len(hits)} layers match; showing the exact ones")
                groups = exact
            else:
                out.append(f"# {term!r}: {len(hits)} layers match. Be more specific, or add --node <id>:")
                out += [
                    f"#   [{n['id']}] {n['type']} {quoted(label(n), 40)} in {short_path(path[-2:]) or 'root'}"
                    for n, _, path, _, _ in hits[:12]
                ]
                continue
        if not groups:
            out.append(f"# {term!r}: no visible layer under {node_id} matches")
        for members in groups:
            node, parent, path, _, up = members[0]
            if where := short_path(path):
                out.append(f"# {where}")
            out += printer.lines(node, parent, max(args.depth, up))
            if len(members) > 1:
                ids = " ".join(f"[{m[0]['id']}]" for m in members[1:6])
                out.append(
                    f"# +{len(members) - 1} more with identical CSS: {ids}{' …' if len(members) > 6 else ''}"
                )
    emit(out, args, entry["document"])


def cmd_css(args: argparse.Namespace) -> None:
    key, node_id = target(args)
    entry = load_node(key, node_id, args.refresh)
    emit(Printer(entry, args.ids).lines(entry["document"], None, args.depth), args, entry["document"])


def cmd_outline(args: argparse.Namespace) -> None:
    key, node_id = target(args, required=False)
    if not node_id:
        emit(page_lines(load_pages(key, args.refresh)), args)
        return
    pages = read_json(folder(key, file_version(key)) / "pages" / "file.json") or []
    if page := next((p for p in pages if p["id"] == node_id), None):  # a page: list its frames, no fetch
        emit(page_lines([page], limit=10_000), args)
        return
    entry = load_node(key, node_id, args.refresh)
    emit(outline_lines(Printer(entry, ids=True), entry["document"], args.depth), args, entry["document"])


def cmd_text(args: argparse.Namespace) -> None:
    key, node_id = target(args)
    entry = load_node(key, node_id, args.refresh)
    counts: dict[str, int] = {}
    stack = [entry["document"]]
    while stack:
        node = stack.pop(0)
        if node["type"] == "TEXT" and (chars := node.get("characters", "").strip()):
            counts[chars] = counts.get(chars, 0) + 1
        stack.extend(c for c in node.get("children", []) if c.get("visible", True))
    emit([quoted(t, 200) + (f" x{n}" if n > 1 else "") for t, n in counts.items()], args, entry["document"])


def image_cost(width: float, height: float, scale: float) -> tuple[int, int, int]:
    """Pixels the model actually sees after its own downscale, and the ~token cost."""
    w, h = width * scale, height * scale
    fit = min(1.0, IMAGE_LONG_EDGE / max(w, h), (IMAGE_MAX_PIXELS / (w * h)) ** 0.5)
    w, h = int(w * fit), int(h * fit)
    return w, h, round(w * h / 750)


def cmd_render(args: argparse.Namespace) -> None:
    key, root_id = target(args, required=not args.node)
    ids = [i.replace("-", ":") for i in args.node] if args.node else [root_id]
    root = load_node(key, root_id, args.refresh)["document"] if root_id else None
    where = folder(key, file_version(key)) / "renders"
    version = None if where.parent.name == "unversioned" else where.parent.name
    shots, missing = [], {}
    for node_id in ids:
        node = (root and find_id(root, node_id)) or load_node(key, node_id, args.refresh)["document"]
        box = node.get("absoluteBoundingBox") or {}
        w, h = box.get("width") or 1, box.get("height") or 1
        scale = args.scale or next((s for s in SCALES if s <= IMAGE_LONG_EDGE / max(w, h)), SCALES[-1])
        path = where / f"{slug(node_id)}@{num(scale)}x.{args.format}"
        shots.append((node_id, path, w, h, scale))
        if args.refresh or not fresh(path, version):
            missing.setdefault(scale, []).append((node_id, path))
    for scale, todo in missing.items():  # one API call per scale, however many layers
        query = urllib.parse.urlencode(
            {"ids": ",".join(i for i, _ in todo), "scale": num(scale), "format": args.format}
        )
        urls = json.loads(api(f"images/{key}?{query}", "render")).get("images") or {}
        for node_id, path in todo:
            if not urls.get(node_id):
                raise FigmaError(f"Figma returned no image for {node_id} (empty or invisible layer?)")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(http_get(urls[node_id]))
    lines = []
    for _, path, w, h, scale in shots:
        if args.out:
            out = Path(args.out)
            dest = out / path.name if len(shots) > 1 or out.is_dir() or args.out.endswith("/") else out
            dest.parent.mkdir(parents=True, exist_ok=True)
            path = Path(shutil.copyfile(path, dest))
        if args.format == "svg":
            lines.append(f"{path}  (SVG asset: copy it into the project, don't read it)")
        else:
            seen_w, seen_h, tokens = image_cost(w, h, scale)
            lines.append(f"{path}  {seen_w}x{seen_h} as seen, ~{tokens:,} tokens")
    print("\n".join(lines))
    print(f"# figma api: {api_calls()}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="figma-pick", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--version", action="version", version=f"figma-pick {VERSION}")
    sub = parser.add_subparsers(dest="command", required=True)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("url", help="Figma link; 'Copy link to selection' adds the node-id")
    common.add_argument("--refresh", action="store_true", help="ignore the cache")
    printed = argparse.ArgumentParser(add_help=False)
    printed.add_argument("--node", help="node id to use instead of the link's (e.g. 12:34)")
    printed.add_argument("--out", help="write to FILE instead of stdout (no token limit)")
    printed.add_argument(
        "--max-tokens", type=int, default=MAX_TOKENS, help=f"refuse bigger output (default {MAX_TOKENS})"
    )

    render = sub.add_parser("render", parents=[common], help="PNG of the frame, or of --node elements")
    render.add_argument(
        "--node", action="append", help="element to render; repeat for several (one API call)"
    )
    render.add_argument("--scale", type=float, help="0.5-4; default: sharpest the model won't downscale")
    render.add_argument("--format", choices=("png", "jpg", "svg"), default="png")
    render.add_argument("--out", help="copy to this file, or into this directory")
    render.set_defaults(func=cmd_render)

    find_cmd = sub.add_parser(
        "find", parents=[common, printed], help="literal CSS for layers whose name or text matches"
    )
    find_cmd.add_argument(
        "terms", nargs="+", metavar="TEXT", help="case-insensitive; several terms share one fetch"
    )
    find_cmd.add_argument("--depth", type=int, default=2, help="levels of children to show (default 2)")
    find_cmd.add_argument("--ids", action="store_true", help="node ids on every line")
    find_cmd.set_defaults(func=cmd_find)

    css_cmd = sub.add_parser("css", parents=[common, printed], help="literal CSS for one subtree")
    css_cmd.add_argument("--depth", type=int, help="levels of children to show (default all)")
    css_cmd.add_argument("--ids", action="store_true", help="node ids on every line")
    css_cmd.set_defaults(func=cmd_css)

    outline = sub.add_parser("outline", parents=[common, printed], help="layer tree with node ids")
    outline.add_argument("--depth", type=int, default=2, help="levels to show (default 2)")
    outline.set_defaults(func=cmd_outline)

    text = sub.add_parser("text", parents=[common, printed], help="every visible string, deduplicated")
    text.set_defaults(func=cmd_text)

    args = parser.parse_args(argv)
    try:
        args.func(args)
    except FigmaError as error:
        print(f"figma-pick: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
