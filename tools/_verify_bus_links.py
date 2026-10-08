"""Verify ehg-app-metadata.md component-table links against sensor-map.md.

A link [N](sensor-map.md#anchor) is valid when:
  1. `anchor` exists in sensor-map.md as an explicit <a id="anchor"></a> OR a heading slug.
  2. The section it points to contains a slot table within a few lines.
  3. The heading for that section references bus N (no cross-wiring like 3 -> 30).
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

DOCS = Path(__file__).resolve().parent.parent / "docs"
SM = (DOCS / "sensor-map.md").read_text(encoding="utf-8")
APP = (DOCS / "ehg-app-metadata.md").read_text(encoding="utf-8")
SM_LINES = SM.splitlines()


def gh_slug(text: str) -> str:
    s = text.strip().lower()
    s = re.sub(r"[^\w\- ]+", "", s)
    return s.replace(" ", "-")


anchors: dict[str, int] = {}
for i, line in enumerate(SM_LINES):
    for m in re.finditer(r'<a id="([^"]+)"></a>', line):
        anchors[m.group(1)] = i
    hm = re.match(r"^#{2,3}\s+(.*)$", line)
    if hm:
        anchors.setdefault(gh_slug(hm.group(1)), i)


def heading_after(idx: int) -> int:
    for j in range(idx, min(idx + 4, len(SM_LINES))):
        if re.match(r"^#{2,3}\s+", SM_LINES[j]):
            return j
    return idx


def section_has_table(idx: int) -> bool:
    for j in range(idx + 1, len(SM_LINES)):
        ln = SM_LINES[j]
        if re.match(r"^#{2,3}\s+", ln) and j > idx:
            break
        if ln.startswith("| Slot |") or re.match(r"^\|\s*\(\d+,", ln):
            return True
    return False


issues: list[str] = []
for m in re.finditer(r"\|\s*\[(\d+)\]\(sensor-map\.md#([^)]+)\)", APP):
    bus = int(m.group(1))
    anchor = m.group(2)
    if anchor not in anchors:
        issues.append(f"UNRESOLVED bus {bus} -> #{anchor}")
        continue
    hdr = heading_after(anchors[anchor])
    hdr_buses = [int(x) for x in re.findall(r"Bus\s+(\d+)", SM_LINES[hdr])]
    if hdr_buses and bus not in hdr_buses:
        issues.append(f"WRONG_SECTION bus {bus} -> '{SM_LINES[hdr].strip()}'")
    if not section_has_table(hdr):
        issues.append(f"NO_TABLE bus {bus} -> #{anchor}")

if issues:
    print("\n".join(sorted(set(issues))))
    sys.exit(1)
print("ALL_LINKS_VALID")
