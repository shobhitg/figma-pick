"""figma_pick.py against synthetic Figma JSON and a fake API; no network, no token."""

from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
import urllib.parse
from itertools import count
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "skills" / "figma-pick" / "scripts"))
import figma_pick as fp  # noqa: E402

_ids = count(100)


def rgb(hex6: str, a: float = 1.0) -> dict:
    return {
        "r": int(hex6[0:2], 16) / 255,
        "g": int(hex6[2:4], 16) / 255,
        "b": int(hex6[4:6], 16) / 255,
        "a": a,
    }


def solid(hex6: str, a: float = 1.0, **extra) -> dict:
    return {"type": "SOLID", "color": rgb(hex6, a), **extra}


def layer(kind: str, name: str, box=(0, 0, 10, 10), children=(), **props) -> dict:
    x, y, w, h = box
    node = {
        "type": kind,
        "name": name,
        "id": f"1:{next(_ids)}",
        "absoluteBoundingBox": {"x": x, "y": y, "width": w, "height": h},
        **props,
    }
    if children:
        node["children"] = list(children)
    return node


def text(chars: str, box=(0, 0, 50, 20), fills=None, **style) -> dict:
    base = {
        "fontFamily": "Inter",
        "fontWeight": 400,
        "fontSize": 14,
        "lineHeightPx": 20,
        "lineHeightUnit": "PIXELS",
        "textAutoResize": "WIDTH_AND_HEIGHT",
    }
    return layer(
        "TEXT", chars, box, characters=chars, style={**base, **style}, fills=fills or [solid("111111")]
    )


def gradient(kind: str, handles, stops) -> dict:
    return {
        "type": kind,
        "gradientHandlePositions": [{"x": x, "y": y} for x, y in handles],
        "gradientStops": [{"color": rgb(c, a), "position": p} for c, a, p in stops],
    }


def decls(node: dict, parent: dict | None = None, level: int = 0) -> list[str]:
    return fp.css(node, parent, level)[0]


def notes(node: dict) -> list[str]:
    return fp.css(node, None, 0)[1]


class Values(unittest.TestCase):
    def test_numbers_drop_trailing_zeros_and_negative_zero(self):
        self.assertEqual(
            [fp.num(12.0), fp.num(0.75), fp.num(-0.004), fp.num(-0.12)], ["12", "0.75", "0", "-0.12"]
        )
        self.assertEqual([fp.px(0), fp.px(6)], ["0", "6px"])

    def test_box_shorthand_collapses_like_css(self):
        self.assertEqual(fp.sides((8, 8, 8, 8)), "8px")
        self.assertEqual(fp.sides((0, 10, 0, 10)), "0 10px")
        self.assertEqual(fp.sides((4, 10, 6, 10)), "4px 10px 6px")
        self.assertEqual(fp.sides((1, 2, 3, 4)), "1px 2px 3px 4px")

    def test_colors_are_hex_when_opaque_and_rgba_when_not(self):
        self.assertEqual(fp.color(rgb("3e63dd")), "#3e63dd")
        self.assertEqual(fp.color(rgb("252500", 7 / 255)), "rgba(37,37,0,0.027)")  # 8-bit alpha survives
        self.assertEqual(fp.color(rgb("000000"), 0.5), "rgba(0,0,0,0.5)")  # paint opacity folds in


class Layout(unittest.TestCase):
    def test_horizontal_auto_layout(self):
        button = layer(
            "FRAME",
            "Button",
            (0, 0, 71, 26),
            layoutMode="HORIZONTAL",
            itemSpacing=6,
            paddingLeft=10,
            paddingRight=10,
            primaryAxisAlignItems="CENTER",
            counterAxisAlignItems="CENTER",
            layoutSizingHorizontal="HUG",
            layoutSizingVertical="FIXED",
        )
        self.assertEqual(
            decls(button),
            [
                "display:flex",
                "justify-content:center",
                "align-items:center",
                "gap:6px",
                "padding:0 10px",
                "height:26px",
            ],
        )

    def test_vertical_space_between_has_no_gap(self):
        column = layer(
            "FRAME", "Col", layoutMode="VERTICAL", itemSpacing=12, primaryAxisAlignItems="SPACE_BETWEEN"
        )
        self.assertIn("flex-direction:column", decls(column))
        self.assertIn("justify-content:space-between", decls(column))
        self.assertFalse([d for d in decls(column) if d.startswith("gap")])

    def test_wrap_uses_row_and_column_gaps(self):
        chips = layer(
            "FRAME", "Chips", layoutMode="HORIZONTAL", layoutWrap="WRAP", itemSpacing=8, counterAxisSpacing=12
        )
        self.assertIn("flex-wrap:wrap", decls(chips))
        self.assertIn("gap:12px 8px", decls(chips))

    def test_negative_spacing_becomes_a_note(self):
        avatars = layer("FRAME", "Avatars", layoutMode="HORIZONTAL", itemSpacing=-8)
        self.assertFalse([d for d in decls(avatars) if d.startswith("gap")])
        self.assertIn("children overlap by 8px (negative spacing)", notes(avatars))

    def test_fill_sizing_depends_on_the_parent_axis(self):
        row = layer("FRAME", "Row", layoutMode="HORIZONTAL")
        cell = layer("FRAME", "Cell", layoutSizingHorizontal="FILL", layoutSizingVertical="FILL")
        self.assertEqual(decls(cell, row, 1), ["flex:1 0 0", "align-self:stretch"])
        column = layer("FRAME", "Column", layoutMode="VERTICAL")
        self.assertEqual(decls(cell, column, 1), ["align-self:stretch", "flex:1 0 0"])

    def test_children_of_plain_frames_are_absolutely_positioned(self):
        child = layer("RECTANGLE", "Dot", (110, 205, 8, 8))
        card = layer("FRAME", "Card", (100, 200, 50, 50), children=[child])
        self.assertIn("position:relative", decls(card))
        self.assertEqual(decls(child, card, 1)[:3], ["position:absolute", "left:10px", "top:5px"])

    def test_absolute_child_inside_auto_layout(self):
        badge = layer("FRAME", "Badge", (40, 0, 8, 8), layoutPositioning="ABSOLUTE")
        icon = layer("FRAME", "Icon", (0, 0, 48, 48), children=[badge], layoutMode="HORIZONTAL")
        self.assertIn("position:relative", decls(icon))
        self.assertEqual(decls(badge, icon, 1)[:3], ["position:absolute", "left:40px", "top:0"])

    def test_text_sizing_follows_auto_resize(self):
        self.assertFalse([d for d in decls(text("Hug")) if d.startswith(("width", "height"))])
        wrapping = text("Wraps", (0, 0, 120, 40), textAutoResize="HEIGHT")
        self.assertIn("width:120px", decls(wrapping))
        self.assertFalse([d for d in decls(wrapping) if d.startswith("height")])


class Paint(unittest.TestCase):
    def test_paints_stack_top_first_with_bare_colour_last(self):
        card = layer("FRAME", "Card", fills=[solid("ffffff"), solid("000000", 0.5)])
        self.assertIn("background:linear-gradient(rgba(0,0,0,0.5), rgba(0,0,0,0.5)), #ffffff", decls(card))

    def test_hidden_paints_are_ignored(self):
        card = layer("FRAME", "Card", fills=[solid("ffffff"), solid("ff0000", visible=False)])
        self.assertIn("background:#ffffff", decls(card))

    def test_vertical_linear_gradient(self):
        paint = gradient(
            "GRADIENT_LINEAR", [(0.5, 0), (0.5, 1), (0, 0)], [("000000", 1, 0), ("ffffff", 1, 1)]
        )
        self.assertEqual(
            fp.linear_gradient(paint, 100, 50), "linear-gradient(180deg, #000000 0%, #ffffff 100%)"
        )

    def test_horizontal_fade_from_real_file(self):
        paint = gradient(
            "GRADIENT_LINEAR", [(0, 0.5), (1, 0.5), (0, 1)], [("d6d3d1", 0, 0), ("d6d3d1", 1, 0.03)]
        )
        self.assertEqual(
            fp.linear_gradient(paint, 100, 40), "linear-gradient(90deg, rgba(214,211,209,0) 0%, #d6d3d1 3%)"
        )

    def test_diagonal_gradient_on_a_wide_box(self):
        # handle 2 is perpendicular in pixel space: bands run at right angles to the diagonal
        paint = gradient(
            "GRADIENT_LINEAR", [(0, 0), (1, 1), (-0.25, 1)], [("000000", 1, 0), ("ffffff", 1, 1)]
        )
        self.assertEqual(
            fp.linear_gradient(paint, 200, 100), "linear-gradient(116.57deg, #000000 0%, #ffffff 100%)"
        )

    def test_gradient_stretched_with_its_layer(self):
        # perpendicular only in normalized space: on a 200x100 box the bands lean, so the
        # CSS angle follows the band normal, not the handle line
        paint = gradient(
            "GRADIENT_LINEAR", [(0, 0), (1, 1), (-0.5, 0.5)], [("000000", 1, 0), ("ffffff", 1, 1)]
        )
        self.assertEqual(
            fp.linear_gradient(paint, 200, 100), "linear-gradient(153.43deg, #000000 0%, #ffffff 100%)"
        )

    def test_radial_gradient(self):
        paint = gradient(
            "GRADIENT_RADIAL", [(0.5, 0.5), (1, 0.5), (0.5, 1)], [("ffffff", 1, 0), ("000000", 1, 1)]
        )
        self.assertEqual(
            fp.radial_gradient(paint, 200, 100, []),
            "radial-gradient(100px 50px at 100px 50px, #ffffff 0%, #000000 100%)",
        )

    def test_unsupported_fills_become_notes(self):
        photo = layer("RECTANGLE", "Photo", fills=[{"type": "IMAGE", "scaleMode": "FILL"}])
        self.assertEqual(decls(photo), ["width:10px", "height:10px"])
        self.assertIn("image fill: see render", notes(photo))
        wheel = layer(
            "ELLIPSE",
            "Wheel",
            fills=[gradient("GRADIENT_ANGULAR", [(0.5, 0.5), (1, 0.5), (0.5, 1)], [("ff0000", 1, 0)])],
        )
        self.assertTrue(notes(wheel)[0].startswith("angular gradient"))


class Strokes(unittest.TestCase):
    def test_stroke_alignment(self):
        base = {"strokes": [solid("191400", 0.21)], "strokeWeight": 0.5}
        self.assertIn(
            "border:0.5px solid rgba(25,20,0,0.21)", decls(layer("FRAME", "In", strokeAlign="INSIDE", **base))
        )
        self.assertIn(
            "outline:0.5px solid rgba(25,20,0,0.21)",
            decls(layer("FRAME", "Out", strokeAlign="OUTSIDE", **base)),
        )
        centered = decls(layer("FRAME", "Mid", strokeAlign="CENTER", **base))
        self.assertIn("outline-offset:-0.25px", centered)

    def test_single_side_border_from_real_file(self):
        divider = layer(
            "FRAME",
            "Divider",
            strokes=[solid("d6d3d1")],
            strokeWeight=1.0,
            strokeAlign="INSIDE",
            individualStrokeWeights={"top": 0.0, "right": 0.5, "bottom": 0.0, "left": 0.0},
        )
        self.assertIn("border-right:0.5px solid #d6d3d1", decls(divider))
        self.assertFalse([d for d in decls(divider) if d.startswith("border:")])

    def test_dashed_and_vector_strokes(self):
        self.assertIn(
            "border:1px dashed #000000",
            decls(layer("FRAME", "Drop", strokes=[solid("000000")], strokeWeight=1, strokeDashes=[4, 4])),
        )
        icon = layer("VECTOR", "Vector", strokes=[solid("21201c")], strokeWeight=0.75, fills=[])
        self.assertEqual(decls(icon), ["width:10px", "height:10px", "stroke:#21201c", "stroke-width:0.75px"])


class ShapeAndEffects(unittest.TestCase):
    def test_radius(self):
        self.assertIn("border-radius:6px", decls(layer("FRAME", "R", cornerRadius=6)))
        self.assertIn(
            "border-radius:8px 8px 0 0", decls(layer("FRAME", "R", rectangleCornerRadii=[8, 8, 0, 0]))
        )
        self.assertIn("border-radius:50%", decls(layer("ELLIPSE", "E")))
        self.assertFalse(
            [d for d in decls(layer("FRAME", "R", rectangleCornerRadii=[0, 0, 0, 0])) if "radius" in d]
        )

    def test_shadows_from_real_file(self):
        button = layer(
            "FRAME",
            "Button",
            effects=[
                {
                    "type": "DROP_SHADOW",
                    "color": rgb("252500", 7 / 255),
                    "offset": {"x": 0, "y": 2},
                    "radius": 3,
                    "spread": -2,
                },
                {
                    "type": "INNER_SHADOW",
                    "color": rgb("000000", 0.05),
                    "offset": {"x": -4, "y": 0},
                    "radius": 4,
                    "visible": False,
                },
                {
                    "type": "INNER_SHADOW",
                    "color": rgb("ffffff"),
                    "offset": {"x": 0, "y": 0},
                    "radius": 0,
                    "spread": 1.5,
                },
            ],
        )
        self.assertIn(
            "box-shadow:0 2px 3px -2px rgba(37,37,0,0.027), inset 0 0 0 1.5px #ffffff", decls(button)
        )

    def test_blurs_halve_figma_radius(self):
        glass = layer(
            "FRAME",
            "Glass",
            effects=[{"type": "BACKGROUND_BLUR", "radius": 20}, {"type": "LAYER_BLUR", "radius": 8}],
        )
        self.assertIn("backdrop-filter:blur(10px)", decls(glass))
        self.assertIn("filter:blur(4px)", decls(glass))

    def test_text_shadow_has_no_spread(self):
        glow = {
            **text("Glow"),
            "effects": [
                {
                    "type": "DROP_SHADOW",
                    "color": rgb("000000"),
                    "offset": {"x": 0, "y": 1},
                    "radius": 2,
                    "spread": 4,
                }
            ],
        }
        self.assertIn("text-shadow:0 1px 2px #000000", decls(glow))

    def test_opacity_blend_and_clipping(self):
        node = layer(
            "FRAME", "F", opacity=0.5, blendMode="MULTIPLY", clipsContent=True, children=[layer("FRAME", "c")]
        )
        self.assertTrue({"opacity:0.5", "mix-blend-mode:multiply", "overflow:hidden"} <= set(decls(node)))


class Text(unittest.TestCase):
    def test_font_shorthand_and_details(self):
        label = text(
            "New project",
            fontFamily="Inter Variable",
            fontWeight=450,
            fontSize=13,
            letterSpacing=-0.12,
            textAlignHorizontal="CENTER",
            opentypeFlags={"SS08": 1, "SS07": 1},
            fills=[solid("21201c")],
        )
        self.assertEqual(
            decls(label),
            [
                'font:450 13px/20px "Inter Variable"',
                "letter-spacing:-0.12px",
                "text-align:center",
                'font-feature-settings:"ss07","ss08"',
                "color:#21201c",
            ],
        )

    def test_auto_line_height_italic_case_decoration_trim(self):
        label = text(
            "x",
            lineHeightUnit="INTRINSIC_%",
            italic=True,
            textCase="UPPER",
            textDecoration="UNDERLINE",
            leadingTrim="CAP_HEIGHT",
        )
        out = decls(label)
        self.assertIn('font:italic 400 14px/normal "Inter"', out)
        self.assertTrue(
            {"text-transform:uppercase", "text-decoration:underline", "text-box:trim-both cap alphabetic"}
            <= set(out)
        )

    def test_truncation(self):
        one = text("Long", (0, 0, 80, 20), textAutoResize="NONE", textTruncation="ENDING")
        self.assertTrue(
            {"overflow:hidden", "text-overflow:ellipsis", "white-space:nowrap"} <= set(decls(one))
        )
        two = text("Long", (0, 0, 80, 40), textAutoResize="NONE", textTruncation="ENDING", maxLines=2)
        self.assertIn("-webkit-line-clamp:2", decls(two))

    def test_mixed_style_runs_print_only_the_difference(self):
        label = text("Hello world")
        label["characterStyleOverrides"] = [0] * 6 + [1] * 5
        label["styleOverrideTable"] = {"1": {"fontWeight": 700, "fills": [solid("e5484d")]}}
        lines = fp.Printer({}).lines(label)
        self.assertEqual(lines[1], '  span "world" { font:700 14px/20px "Inter"; color:#e5484d }')


def screen() -> dict:
    """Top bar with a button and tabs, a table of status badges, a hidden layer."""

    def badge(word: str, fill: str) -> dict:
        return layer(
            "FRAME",
            "Component 1",
            (0, 0, 70, 22),
            layoutMode="HORIZONTAL",
            cornerRadius=4,
            fills=[solid(fill)],
            children=[text(word, fills=[solid("218358")])],
        )

    rows = [
        layer(
            "FRAME",
            "Row",
            (0, 40 * i, 600, 40),
            layoutMode="HORIZONTAL",
            children=[
                text(f"Project {i}"),
                badge("Live", "c4e8d1" if i % 3 else "f4fbf6"),
            ],
        )
        for i in range(5)
    ]
    tab = {
        "componentId": "c1",
        "componentProperties": {
            "State": {"type": "VARIANT", "value": "Default"},
            "Label#3:0": {"type": "TEXT", "value": "x"},
        },
    }
    tabs = layer(
        "FRAME",
        "Tabs",
        layoutMode="HORIZONTAL",
        children=[
            layer("INSTANCE", "Tab", children=[text(word)], **tab)
            for word in ("Overview", "Activity", "Files")
        ],
    )
    button = layer(
        "FRAME",
        "Primary Button",
        (900, 10, 71, 26),
        layoutMode="HORIZONTAL",
        cornerRadius=6,
        fills=[solid("fbfbf9")],
        children=[layer("FRAME", "Label wrap", children=[text("New project")])],
    )
    hidden = {**text("New project (old)"), "visible": False}
    return layer(
        "FRAME",
        "Screen",
        (0, 0, 1440, 1024),
        id="1:1",
        children=[
            layer("FRAME", "Top Bar", (0, 0, 1440, 48), children=[tabs, button, hidden]),
            layer("FRAME", "Table", layoutMode="VERTICAL", children=rows),
        ],
    )


COMPONENTS = {"c1": {"name": "State=Default", "componentSetId": "s1"}}
SETS = {"s1": {"name": "Tab"}}
PAGES = [
    {
        "id": "0:1",
        "name": "Designs",
        "children": [
            {
                "id": "1:1",
                "type": "FRAME",
                "name": "Screen",
                "absoluteBoundingBox": {"x": 0, "y": 0, "width": 1440, "height": 1024},
            }
        ],
    }
]


class Finding(unittest.TestCase):
    def setUp(self):
        self.root = screen()
        self.printer = fp.Printer({"components": COMPONENTS, "componentSets": SETS})

    def test_text_hit_reports_the_styled_element_around_it(self):
        [(node, parent, path, exact, up)] = fp.find(self.root, "new project")
        self.assertEqual((node["name"], up, exact), ("Primary Button", 2, True))
        self.assertEqual(fp.short_path(path), "Screen > Top Bar")

    def test_hidden_layers_never_match(self):
        self.assertFalse([h for h in fp.find(self.root, "(old)")])

    def test_identical_matches_are_grouped(self):
        groups = fp.group(self.printer, fp.find(self.root, "live"), 2)
        self.assertEqual(sorted(len(g) for g in groups), [2, 3])  # two badge fills

    def test_instances_show_component_and_variant(self):
        line = self.printer.header(fp.find(self.root, "overview")[0][0], 0)
        self.assertIn("(Tab: State=Default)", line)

    def test_look_alike_siblings_print_once(self):
        tabs = self.root["children"][0]["children"][0]
        lines = self.printer.lines(tabs)
        self.assertEqual(lines[-1], '  +2 more like this: "Activity", "Files"')

    def test_depth_limit_is_announced(self):
        lines = self.printer.lines(self.root, depth=0)
        self.assertIn("2 children not shown: raise --depth", lines[0])


class FakeFigma:
    def __init__(self, tree: dict, version: str = "1"):
        self.tree, self.version, self.calls, self.fail = tree, version, [], {}
        self.touched = "2026-09-16T20:56:50.693Z"

    def __call__(self, url: str, token: str | None = None) -> bytes:
        path = url[len(fp.API) + 1 :] if url.startswith(fp.API) else url
        self.calls.append(path)
        for prefix, error in self.fail.items():
            if path.startswith(prefix):
                raise error
        if path.endswith("/meta"):
            return json.dumps({"file": {"version": self.version, "last_touched_at": self.touched}}).encode()
        if "/nodes?ids=" in path:
            node_id = urllib.parse.unquote(path.split("ids=")[1])
            node = fp.find_id(self.tree, node_id)
            entry = (
                {"document": node, "components": COMPONENTS, "componentSets": SETS, "styles": {}}
                if node
                else None
            )
            return json.dumps({"nodes": {node_id: entry}}).encode()
        if path.startswith("images/"):
            ids = urllib.parse.parse_qs(path.split("?", 1)[1])["ids"][0].split(",")
            return json.dumps({"images": {i: f"https://s3.test/{i}.png" for i in ids}}).encode()
        if path.startswith("https://s3.test/"):
            return b"\x89PNG"
        if "?depth=2" in path:
            return json.dumps({"document": {"children": PAGES}}).encode()
        raise AssertionError(f"unexpected request {path}")

    def api_calls(self) -> list[str]:
        calls = [c for c in self.calls if not c.startswith("https://")]
        self.calls.clear()
        return calls


KEY = "AbC123"
URL = f"https://www.figma.com/design/{KEY}/Demo?node-id=1-1"


class Cli(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.figma = FakeFigma(screen())
        self.saved = (fp.CACHE, fp.http_get, fp.META_FRESH_S, os.environ.get("FIGMA_TOKEN"))
        fp.CACHE, fp.http_get = Path(self.tmp.name), self.figma
        os.environ["FIGMA_TOKEN"] = "test"

    def tearDown(self):
        fp.CACHE, fp.http_get, fp.META_FRESH_S, token = self.saved
        if token is None:
            os.environ.pop("FIGMA_TOKEN", None)
        else:
            os.environ["FIGMA_TOKEN"] = token
        self.tmp.cleanup()

    def run_cli(self, *argv: str) -> tuple[int, str, str]:
        for kind in fp.STATS:
            fp.STATS[kind] = 0
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = fp.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def test_link_parsing(self):
        cases = {
            "https://www.figma.com/design/AbC123/Name?node-id=12-34&t=x": ("AbC123", "12:34"),
            "https://figma.com/file/AbC123/Name?node-id=I5-6%3B7-8": ("AbC123", "I5:6;7:8"),
            "https://www.figma.com/design/AbC123/branch/BrN456/Name?node-id=1-2": ("BrN456", "1:2"),
            "https://www.figma.com/proto/AbC123/Name?node-id=1-2": ("AbC123", "1:2"),
        }
        for url, expected in cases.items():
            self.assertEqual(fp.target(fp.argparse.Namespace(url=url, refresh=False)), expected)

    def test_find_prints_css_and_footer(self):
        code, out, _ = self.run_cli("find", URL, "New project")
        self.assertEqual(code, 0)
        self.assertIn('FRAME "Primary Button" 71x26 [', out)
        self.assertIn("background:#fbfbf9", out)
        self.assertIn("figma api: 1 file read, 1 version check", out.splitlines()[-1])

    def test_repeat_calls_are_free(self):
        self.run_cli("find", URL, "New project")
        self.assertEqual(self.figma.api_calls(), [f"files/{KEY}/meta", f"files/{KEY}/nodes?ids=1%3A1"])
        code, out, _ = self.run_cli("outline", URL)
        self.assertEqual((code, self.figma.api_calls()), (0, []))
        self.assertTrue(out.rstrip().endswith("figma api: none (cached)"))

    def test_a_child_is_served_from_its_cached_ancestor(self):
        self.run_cli("find", URL, "New project")
        self.figma.api_calls()
        button = fp.find(self.figma.tree, "new project")[0][0]["id"]
        code, out, _ = self.run_cli("css", URL, "--node", button)
        self.assertEqual((code, self.figma.api_calls()), (0, []))
        self.assertIn("Primary Button", out)

    def test_same_version_recheck_costs_one_cheap_call(self):
        fp.META_FRESH_S = 0
        self.run_cli("text", URL)
        self.figma.api_calls()
        self.run_cli("text", URL)
        self.assertEqual(self.figma.api_calls(), [f"files/{KEY}/meta"])

    def test_new_file_version_refetches_and_prunes(self):
        fp.META_FRESH_S = 0
        self.run_cli("text", URL)
        self.figma.version = "2"
        self.figma.api_calls()
        self.run_cli("text", URL)
        self.assertEqual(self.figma.api_calls(), [f"files/{KEY}/meta", f"files/{KEY}/nodes?ids=1%3A1"])
        self.assertEqual(
            sorted(p.name for p in (fp.CACHE / KEY).iterdir() if p.is_dir()), ["2-20260916205650693"]
        )

    def test_an_edit_without_a_new_version_still_refetches(self):
        fp.META_FRESH_S = 0
        self.run_cli("text", URL)
        self.figma.touched = "2026-09-16T21:03:12.001Z"  # same version number, later edit
        self.figma.api_calls()
        self.run_cli("text", URL)
        self.assertEqual(self.figma.api_calls(), [f"files/{KEY}/meta", f"files/{KEY}/nodes?ids=1%3A1"])

    def test_token_without_metadata_scope_falls_back_to_ttl(self):
        self.figma.fail[f"files/{KEY}/meta"] = fp.FigmaError("forbidden", 403)
        code, _, err = self.run_cli("text", URL)
        self.assertEqual(code, 0)
        self.assertIn("caching for 1h", err)
        self.figma.api_calls()
        self.run_cli("text", URL)
        self.assertEqual(self.figma.api_calls(), [])  # the 403 answer is cached too

    def test_rate_limit_serves_the_last_cached_version(self):
        fp.META_FRESH_S = 0
        self.run_cli("text", URL)
        self.figma.version = "2"
        self.figma.fail[f"files/{KEY}/nodes"] = fp.FigmaError("Figma rate limit; retry in 3d.", 429)
        code, out, err = self.run_cli("text", URL)
        self.assertEqual(code, 0)
        self.assertIn('"New project"', out)
        self.assertIn("earlier file version", err)

    def test_rate_limit_without_a_cache_is_an_error(self):
        self.figma.fail[f"files/{KEY}/nodes"] = fp.FigmaError("Figma rate limit; retry in 3d.", 429)
        code, _, err = self.run_cli("text", URL)
        self.assertEqual(code, 1)
        self.assertIn("rate limit", err)

    def test_rate_limit_message_names_the_seat_budget(self):
        error = fp.api_error(429, {"Retry-After": "400000", "X-Figma-Rate-Limit-Type": "low"}, b"{}")
        self.assertEqual(
            str(error),
            "Figma rate limit; retry in 4d. View and Collab seats get about 20 file reads a month.",
        )

    def test_render_batches_layers_into_one_call_and_caches(self):
        a, b = (fp.find(self.figma.tree, word)[0][0]["id"] for word in ("overview", "activity"))
        code, out, _ = self.run_cli("render", URL, "--node", a, "--node", b)
        self.assertEqual(code, 0)
        self.assertEqual(
            [c.split("?")[0] for c in self.figma.api_calls()],
            [f"files/{KEY}/meta", f"files/{KEY}/nodes", f"images/{KEY}"],
        )
        self.assertEqual(len([line for line in out.splitlines() if line.endswith("tokens")]), 2)
        self.run_cli("render", URL, "--node", a, "--node", b)
        self.assertEqual(self.figma.api_calls(), [])

    def test_link_without_node_id_lists_frames(self):
        code, out, err = self.run_cli("find", f"https://www.figma.com/design/{KEY}/Demo", "New project")
        self.assertEqual(code, 1)
        self.assertIn('FRAME "Screen" 1440x1024 [1:1]', out)
        self.assertIn("--node", err)

    def test_oversized_output_is_refused(self):
        code, out, err = self.run_cli("css", URL, "--max-tokens", "50")
        self.assertEqual((code, out), (1, ""))
        self.assertIn("Narrow it", err)

    def test_out_file_skips_the_limit(self):
        path = Path(self.tmp.name) / "screen.css"
        code, out, _ = self.run_cli("css", URL, "--max-tokens", "50", "--out", str(path))
        self.assertEqual(code, 0)
        self.assertIn("Primary Button", path.read_text())
        self.assertTrue(out.startswith(f"wrote {path}"))


class Packaging(unittest.TestCase):
    def test_names_agree_everywhere(self):
        plugin = json.loads((ROOT / ".claude-plugin" / "plugin.json").read_text())
        market = json.loads((ROOT / ".claude-plugin" / "marketplace.json").read_text())
        skill = (ROOT / "skills" / "figma-pick" / "SKILL.md").read_text()
        self.assertEqual(plugin["name"], "figma-pick")
        self.assertEqual(market["plugins"][0]["name"], "figma-pick")
        self.assertEqual(plugin["version"], fp.VERSION)
        self.assertIn("\nname: figma-pick\n", skill)


if __name__ == "__main__":
    unittest.main()
