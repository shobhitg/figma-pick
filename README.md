# figma-pick

Exact CSS for just the Figma elements you name. A Claude Code skill and a dependency-free CLI.

Ask an AI agent to fix one button from a Figma design and it usually pulls the whole frame. It gets either a raw JSON dump (Figma's REST API returns 1–1.5 KB per layer, so one app screen runs to hundreds of thousands of tokens) or a full-screen screenshot that can't tell `#3e63dd` from `#3b5bdb`. figma-pick works the way a developer does: look at the screen once, name the elements, read their exact CSS.

```text
$ figma-pick find "<figma link>" "Save changes" "Admin"
# Workspace settings > Header
FRAME "Button/Primary" 116x32 [1:102] { display:flex; justify-content:center; align-items:center; gap:6px; padding:0 12px; height:32px; background:#3e63dd; border-radius:6px; box-shadow:0 1px 2px rgba(0,0,0,0.05) }
  VECTOR "check" 16x16 { width:16px; height:16px; stroke:#ffffff; stroke-width:1.5px }
  TEXT "Save changes" 50x20 { font:500 14px/20px "Inter"; letter-spacing:-0.1px; color:#ffffff }
# Workspace settings > Members > Member row
FRAME "Badge" 52x22 [1:105] { display:flex; align-items:center; padding:0 8px; height:22px; background:#edf2fe; border:1px solid #c1d0ff; border-radius:11px }
  TEXT "Admin" 50x20 { font:500 12px/16px "Inter"; color:#3a5bc7 }
# +2 more with identical CSS: [1:109] [1:113]
# ~248 tokens, node JSON ~3,228 · figma api: none (cached)
```

## What it saves

Measured on a real 1440×1024 SaaS list screen with 1,297 layers. Text is counted at about 3 characters per token; images use Anthropic's `width × height / 750` rule.

| Approach | Tokens |
|---|---:|
| Raw REST JSON for the frame | ~520,000 |
| Framelink MCP `get_figma_data`, whole frame | ~22,000 |
| `figma-pick render`: the whole frame, as the model sees it | ~1,500 |
| `figma-pick find` for 4 elements | ~1,300 |
| `figma-pick find` for 1 element | ~260 |
| `figma-pick outline`: the layer map with node ids | ~180 |
| `figma-pick render --node`: one button at 4× | ~80 |

It is also fast after the first look. The first read of a frame took 3.6 s, all of it Figma's latency. Every later `find`, `outline`, `text` or `css` on that frame took 0.1 s and made no API calls.

## Install

**As a Claude Code plugin**, which installs the skill plus a guard hook (see below):

```bash
claude plugin marketplace add shobhitg/figma-pick
claude plugin install figma-pick@figma-pick
```

Then paste a Figma link, or type `/figma-pick <link> the Save button`.

**As a plain skill**, without the hook:

```bash
git clone https://github.com/shobhitg/figma-pick
cp -r figma-pick/skills/figma-pick ~/.claude/skills/
```

**As a CLI**, for any agent or for you: `python3 skills/figma-pick/scripts/figma_pick.py --help`. It needs Python 3.9+ and nothing else.

**Token.** In Figma, go to Settings > Security > Personal access tokens. Create a token with the scopes *File content: read* and *File metadata: read*, then `export FIGMA_TOKEN=figd_...`. figma-pick also accepts `FIGMA_API_KEY`, or the key of a Framelink `figma` MCP server already configured in `~/.claude.json`.

## Commands

Every command takes a Figma link. *Copy link to selection* gives you one with a `node-id`.

| Command | Prints |
|---|---|
| `render <link> [--node ID ...] [--format svg] [--out PATH]` | A PNG of the frame or of chosen elements, scaled so the model sees it sharp without downscaling. Several `--node` values cost one API call. Use SVG for icons. |
| `find <link> TEXT [TEXT ...]` | Literal CSS for the layers whose name or visible text contains TEXT, with 2 levels of children (`--depth`). |
| `outline <link>` | The layer tree with node ids, to choose what to `find` or `render`. |
| `css <link> --node ID` | Literal CSS for one subtree. |
| `text <link>` | Every visible string, deduplicated. |

A link without a `node-id` lists the file's pages and frames instead of fetching a page. Output over ~4,000 tokens is refused with advice on narrowing it. `--max-tokens` and `--out FILE` are the escape hatches.

## Reading the output

One line per layer, indented by depth:

```text
TYPE "label" WxH [id] (Component: Variant=Value) { literal css } /* what CSS can't carry */
```

- **The values are literal.** Colours are hex, or `rgba()` with the alpha Figma stored. Sizes, fonts, OpenType features, leading trim and shadows are exactly what the design says. Nothing is rounded to a "nearest" token.
- **A text match reports the styled element around it.** Searching for "Save changes" returns the button, not the glyph run.
- **Repeats are folded.** Matches that would print identically become `+N more with identical CSS: [ids]`, and look-alike siblings such as table rows become `+N more like this`.
- **Instances name their design-system component and variant**, so the agent can reuse the component in your codebase instead of rebuilding it.

`outline` on the same frame:

```text
FRAME "Workspace settings" 1440x900 [12:1]
  FRAME "Header" 1440x80 [12:2]
    TEXT "Workspace settings" 50x20 [1:123]
    FRAME "Button/Primary" 116x32 [1:122]  (+2 children)
  FRAME "Members" 1440x820 [12:9]
    FRAME "Member row" 1200x48 [1:103]  (+2 children)
    +4 more "Member row"
```

## Caching and Figma's rate limits

Since November 2025, Figma allows View and Collab seats [about 20 reads of file content a month](https://developers.figma.com/docs/rest-api/rate-limits). Every read counts, so figma-pick caches each frame per file version:

- **A cheap check comes first.** Before reusing a cached frame, figma-pick asks Figma's metadata endpoint (`/v1/files/:key/meta`, a cheap rate-limit tier) for the file's `version` and `last_touched_at`, at most once a minute. If neither changed, the cached frame is used and nothing is downloaded.
- **Any edit invalidates the whole file.** Figma offers no per-layer hash or ETag; a file-level version plus last-modified time is the finest change signal available. Re-reading one frame after an edit is a single call.
- **Elements come from the frame you already have.** `find`, `outline`, `css`, `text` and `render --node` on anything inside a cached frame make no file reads. Parallel calls, for example several sub-agents, share one read through a file lock.
- **When rate-limited, it serves the last cached version** with a warning, rather than failing.
- **The cache lives in `~/.cache/figma-pick`**, shared by every project. Set `FIGMA_PICK_CACHE` to move it, for example to a git-ignored folder inside a repo.

The last line of every command reports the API calls it made.

## The guard hook

The plugin's `PreToolUse` hook blocks the two most common context floods. It stops shell calls (`curl`) that fetch raw file JSON from `api.figma.com/v1/files/...`, and Framelink `get_figma_data` calls without both `nodeId` and `depth`. Claude is told to use figma-pick instead. The cheap endpoints (`/meta`, `/me`, `/images`) are never blocked. To turn the hook off, disable the plugin in `/plugin`, or install the plain skill instead.

## What's captured

| Area | Captured |
|---|---|
| Layout | Auto layout as flex (direction, wrap, gap, padding, alignment); fixed/hug/fill sizing as width, height, `flex` and `align-self`; min/max sizes; absolute positions in plain frames |
| Fills | Solid colours, and linear gradients (including ones stretched with their layer) and radial gradients as exact CSS |
| Strokes | Inside strokes as `border`, outside and centre strokes as `outline`; per-side widths; dashes |
| Shape and effects | Corner radii, drop and inner shadows, layer and background blur, opacity, blend modes, clipping |
| Text | The `font` shorthand, letter-spacing, alignment, case, decoration, OpenType features, leading trim, truncation and line clamps, and mixed-style runs as spans |
| Naming | Component and variant names, and named styles |

Not captured, and flagged inline in a `/* note */` so the agent knows to check the render: image fills, angular and diamond gradients, vector geometry (use `render --format svg`), rotation, grid tracks, constraints, and variable names (Figma's variables API is Enterprise-only).

## Development

```bash
python3 -m unittest discover -s tests
```

The tests use synthetic Figma JSON and a fake API, so they need no token or network access.

## License

MIT
