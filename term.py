"""Colors for the demo's output: in a terminal, or when FORCE_COLOR is set (agent-runtime sets it
for lab actions, and the scimigo.com lab page renders the colors). NO_COLOR turns them off.
Piped output without FORCE_COLOR stays plain text, as recorded in observed/.
"""

import os
import sys

ENABLED = (sys.stdout.isatty() or bool(os.environ.get("FORCE_COLOR"))) and not os.environ.get("NO_COLOR")

_CODES = {
    "bold": "1",
    "dim": "2",
    "red": "31",
    "green": "32",
    "yellow": "33",
    "blue": "34",
    "magenta": "35",
    "cyan": "36",
}


def style(text: str, *names: str) -> str:
    """`text` wrapped in the ANSI codes for `names` (e.g. "bold", "red"), or unchanged."""
    if not ENABLED or not names or not text:
        return text
    return "".join(f"\033[{_CODES[name]}m" for name in names) + text + "\033[0m"
