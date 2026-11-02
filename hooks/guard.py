#!/usr/bin/env python3
"""figma-pick guard: stop whole-file Figma JSON fetches before they flood the context.

A Claude Code PreToolUse hook. Exit code 2 blocks the tool call and shows Claude
the reason on stderr; exit 0 lets it through.
"""

import json
import re
import sys

RAW_FETCH = re.compile(r"api\.figma\.com/v1/files/[A-Za-z0-9]+(?:/nodes)?(?=[?\s\"'\\]|$)")
ADVICE = (
    "Blocked by figma-pick: this pulls raw Figma JSON (~1.5 KB per layer; one screen is"
    " 100k+ tokens). Use the figma-pick skill instead: `render` the frame to see it, then"
    " `find` the elements you need for their exact CSS."
)


def blocked(event: dict) -> bool:
    tool = event.get("tool_name", "")
    args = event.get("tool_input") or {}
    if tool == "Bash":
        command = args.get("command", "")
        return "figma_pick.py" not in command and bool(RAW_FETCH.search(command))
    # Framelink's get_figma_data returns a whole file or frame unless given nodeId and depth
    return tool.endswith("get_figma_data") and not (args.get("nodeId") and args.get("depth"))


def main() -> int:
    try:
        event = json.load(sys.stdin)
    except ValueError:
        return 0
    if blocked(event):
        print(ADVICE, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
