"""Parse the committed EHG metadata Markdown into structured JSON.

`docs/ehg-app-metadata.md` is the human-readable reference extracted from the
decompiled EHG app. It holds:
  * a component table    (bus -> component_id, kind, name, slot count, mapped?)
  * per-component slot tables under "### Bus <n> - ..." headers
    (slot -> label, datatype, unit, min, max, mode)

This tool turns that committed Markdown into a machine-readable
`tools/ehg_metadata.json` so downstream tooling (the completeness lint and a
future base.json generator for the brandless goal) can consume structured data
instead of re-parsing Markdown. It only reads data already committed to the
repo - it does NOT touch any APK-derived extraction.

Run with --check to verify the JSON is up to date (CI-friendly, non-zero exit
if regeneration would change it).
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
METADATA_MD = REPO / "docs" / "ehg-app-metadata.md"
OUT_JSON = REPO / "tools" / "ehg_metadata.json"

# Component table row: | <bus> | `<component_id>` | <kind> | <name> | <slots> | <mapped> |
_COMPONENT_ROW = re.compile(
    r"^\|\s*(\d+)\s*\|\s*`([^`]+)`\s*\|\s*([a-z_]+)\s*\|\s*([^|]*?)\s*\|\s*([^|]*?)\s*\|\s*([^|]*?)\s*\|"
)
# Detailed slot table header: ### Bus <n> - <title>
_BUS_HEADER = re.compile(r"^###\s+Bus\s+(\d+)\b")
# A Markdown table row split into cells (leading/trailing pipes stripped).
_SLOT_LABEL = re.compile(r"^`?([^`|]+?)`?$")


def _cells(line: str) -> list[str]:
    return [c.strip() for c in line.strip().strip("|").split("|")]


def parse_components(lines: list[str]) -> dict[str, dict]:
    """Parse the top-level component table into {bus: {...}}."""
    out: dict[str, dict] = {}
    in_table = False
    for line in lines:
        if line.startswith("## Component Table"):
            in_table = True
            continue
        if in_table and line.startswith("## "):
            break
        if not in_table:
            continue
        m = _COMPONENT_ROW.match(line)
        if not m:
            continue
        bus, cid, kind, name, slots, mapped = m.groups()
        out[bus] = {
            "component_id": cid,
            "kind": kind,
            "name": name.strip(),
            "slot_count": slots.strip(),
            "mapped": mapped.strip(),
        }
    return out


def parse_slots(lines: list[str]) -> dict[str, dict]:
    """Parse each '### Bus <n>' detailed slot table into {bus: {slot: {...}}}."""
    out: dict[str, dict] = {}
    bus: str | None = None
    header_seen = False
    for line in lines:
        hm = _BUS_HEADER.match(line)
        if hm:
            bus = hm.group(1)
            out.setdefault(bus, {})
            header_seen = False
            continue
        if line.startswith("### "):  # a non-"Bus" subsection ends the current one
            bus = None
            continue
        if bus is None or not line.lstrip().startswith("|"):
            continue
        cells = _cells(line)
        if not cells or not cells[0].isdigit():
            # table header / separator / prose row
            header_seen = True
            continue
        slot = cells[0]
        label_m = _SLOT_LABEL.match(cells[1]) if len(cells) > 1 else None
        entry = {"label": label_m.group(1) if label_m else (cells[1] if len(cells) > 1 else "")}
        # Positional columns after label vary (7-col: datatype,unit,min,max,mode).
        keys = ["datatype", "unit", "min", "max", "mode"]
        for i, key in enumerate(keys, start=2):
            if i < len(cells) and cells[i] != "":
                entry[key] = cells[i]
        out[bus][slot] = entry
    # drop buses that ended up with no slots
    return {b: s for b, s in out.items() if s}


def build() -> dict:
    lines = METADATA_MD.read_text(encoding="utf-8").splitlines()
    components = parse_components(lines)
    slots = parse_slots(lines)
    return {
        "_source": "docs/ehg-app-metadata.md",
        "_generated_by": "tools/parse_ehg_metadata.py",
        "components": components,
        "slots": slots,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--check", action="store_true", help="verify JSON is current")
    args = ap.parse_args()

    data = build()
    rendered = json.dumps(data, ensure_ascii=False, indent=2) + "\n"

    if args.check:
        if not OUT_JSON.exists() or OUT_JSON.read_text(encoding="utf-8") != rendered:
            print("FAIL: tools/ehg_metadata.json is stale - run tools/parse_ehg_metadata.py")
            return 1
        print("ehg_metadata.json is up to date  OK")
        return 0

    OUT_JSON.write_text(rendered, encoding="utf-8")
    print(
        f"Wrote {OUT_JSON.relative_to(REPO)}: "
        f"{len(data['components'])} components, "
        f"{sum(len(s) for s in data['slots'].values())} detailed slots "
        f"across {len(data['slots'])} buses"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
