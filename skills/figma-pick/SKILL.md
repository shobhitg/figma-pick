---
name: figma-pick
description: Use whenever a Figma link (figma.com/design, /file, /proto, /board) appears, or when building, matching, fixing, or comparing UI against a Figma design, or when you need the exact CSS (colors, spacing, typography, shadows) of a design element. Fetches only the elements you name, never the whole page.
argument-hint: "[figma-url] [element names]"
---

# figma-pick

Never pull a whole Figma page or frame as JSON (raw REST is ~1.5 KB per layer; one screen is 100k–1M tokens), and never read a full-screen image to inspect one button. Look once, name the elements, pick their exact CSS.

```bash
python3 ${CLAUDE_SKILL_DIR}/scripts/figma_pick.py render "URL"                  # 1. see the frame (~1.5k tokens): Read the PNG path it prints
python3 ${CLAUDE_SKILL_DIR}/scripts/figma_pick.py find "URL" "Save" "Tabs"      # 2. exact CSS for just those (~100-300 tokens each)
python3 ${CLAUDE_SKILL_DIR}/scripts/figma_pick.py render "URL" --node 12:34     # 3. zoom on one element, icon or image
```

- Run 1 and 2 together when the request already names the elements.
- `find` matches layer names and visible text, case-insensitively. A text match reports the styled element around it (the button, not its label). Identical matches (table rows, repeated badges) print once, with the other ids.
- A link with no `node-id` prints the file's frames: re-run with `--node <id>`. Never fetch a page.
- `outline` maps a frame's layers with ids; `text` lists every visible string; `css --node <id>` prints one subtree. Output over ~4k tokens is refused: narrow it instead of raising `--max-tokens`.
- Icons and vectors: `render --node <id> --format svg --out <path>` is the exact asset. Save it; don't read it.

## Reading the output

Each line is `TYPE "label" WxH [id] (Component: variant) { css } /* notes */`, indented by depth.

- The CSS is literal. Copy hex, rgba, px, fonts and shadows verbatim. Use a design token only when it matches exactly; otherwise use the literal value and say so. Never substitute a "nearest" color.
- `(Set: Prop=Value)` marks an instance of a design-system component: find that component in the codebase by name before writing new markup.
- `/* notes */` name what CSS can't carry (image fills, angular gradients, rotation): check the render for those.
- Children of a frame without auto layout get `position:absolute` offsets; inside auto layout, sizing prints as flex rules (`flex:1 0 0` means fill).

## Budget

The last line of every run reports the Figma API calls it made. Each frame is read once per file version and cached, so repeats, `find` after `render`, and parallel sub-agents on one frame cost no reads (a cheap version check runs at most once a minute). View and Collab seats get only about 20 file reads a month, so keep using one frame link rather than fetching its neighbours. For sub-agents: render once yourself, then give each the link plus element names, one `find` call each.

If auth fails, ask the user for a Figma token (env `FIGMA_TOKEN`; scopes File content read and File metadata read). Don't search config files for one.
