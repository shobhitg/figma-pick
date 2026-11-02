"""The PreToolUse guard, run the way Claude Code runs it: JSON on stdin, exit code out."""

from __future__ import annotations

import json
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GUARD = ROOT / "hooks" / "guard.py"


def guard(tool: str, raw: str | None = None, **tool_input) -> int:
    event = raw if raw is not None else json.dumps({"tool_name": tool, "tool_input": tool_input})
    return subprocess.run(
        [sys.executable, "-I", "-S", str(GUARD)], input=event, capture_output=True, text=True
    ).returncode


class Guard(unittest.TestCase):
    def test_blocks_raw_file_json_from_the_shell(self):
        for command in (
            'curl -s -H "X-Figma-Token: $T" "https://api.figma.com/v1/files/AbC123/nodes?ids=1:2"',
            "curl https://api.figma.com/v1/files/AbC123?depth=2 | jq .",
            "curl https://api.figma.com/v1/files/AbC123",
        ):
            self.assertEqual(guard("Bash", command=command), 2, command)

    def test_allows_cheap_endpoints_and_figma_pick_itself(self):
        for command in (
            "curl https://api.figma.com/v1/files/AbC123/meta",
            "curl https://api.figma.com/v1/me",
            "curl 'https://api.figma.com/v1/images/AbC123?ids=1:2'",
            "python3 /x/figma_pick.py find https://www.figma.com/design/AbC123/N?node-id=1-2 Save",
            "ls -la",
        ):
            self.assertEqual(guard("Bash", command=command), 0, command)

    def test_framelink_needs_node_and_depth(self):
        tool = "mcp__figma__get_figma_data"
        self.assertEqual(guard(tool, fileKey="AbC123"), 2)
        self.assertEqual(guard(tool, fileKey="AbC123", nodeId="1:2"), 2)
        self.assertEqual(guard(tool, fileKey="AbC123", nodeId="1:2", depth=2), 0)

    def test_garbage_input_never_blocks(self):
        self.assertEqual(guard("Bash", raw="not json"), 0)

    def test_hook_config_points_at_the_guard(self):
        config = json.loads((ROOT / "hooks" / "hooks.json").read_text())
        [entry] = config["hooks"]["PreToolUse"]
        self.assertIn("hooks/guard.py", entry["hooks"][0]["command"])
        self.assertEqual(entry["matcher"], "Bash|mcp__.*get_figma_data")


if __name__ == "__main__":
    unittest.main()
