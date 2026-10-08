"""Generate verified per-bus slot tables for sensor-map.md from the authoritative
map files (base.json + lights.json), add stable <a id="bus-N"> anchors to every
curated bus section, and rewrite the ehg-app-metadata.md component-table links to
the stable #bus-N anchors.

Run from repo root:  py -3 tools/_gen_bus_tables.py
"""

from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MAPS = ROOT / "custom_components" / "hymer_connect" / "sensor_maps"
SM_PATH = ROOT / "docs" / "sensor-map.md"
APP_PATH = ROOT / "docs" / "ehg-app-metadata.md"

APPENDIX_START = "<!-- AUTO-BUS-TABLES:START -->"
APPENDIX_END = "<!-- AUTO-BUS-TABLES:END -->"

CATEGORY_PLATFORM = {
    "sensors": "sensor",
    "switches": "switch",
    "covers": "cover",
    "climate": "climate",
    "lights": "light",
    "numbers": "number",
}


def load_slots() -> dict[int, dict[int, dict]]:
    buses: dict[int, dict[int, dict]] = {}
    for fname in ("base.json", "lights.json"):
        data = json.loads((MAPS / fname).read_text(encoding="utf-8"))
        for cat, entries in data.items():
            if cat.startswith("_") or not isinstance(entries, dict):
                continue
            platform = CATEGORY_PLATFORM.get(cat, "sensor")
            for key, val in entries.items():
                if "," not in key or not isinstance(val, dict):
                    continue
                try:
                    bus, slot = (int(x) for x in key.split(",", 1))
                except ValueError:
                    continue
                entry = dict(val)
                entry["_platform"] = val.get("platform", platform)
                buses.setdefault(bus, {})[slot] = entry
    return buses


def rw_flag(entry: dict) -> str:
    if any(k in entry for k in ("write_type", "write_on", "write_off", "on_value")):
        return "rw"
    if entry.get("_platform") in ("switch", "cover", "climate", "light", "number"):
        return "rw"
    return "r"


def render_bus_table(bus: int, slots: dict[int, dict]) -> str:
    out = [
        f'<a id="bus-{bus}"></a>',
        f"### Bus {bus}",
        "",
        "| Slot | Sensor Name | Platform | Unit | Mode |",
        "|------|------------|----------|------|------|",
    ]
    for slot in sorted(slots):
        e = slots[slot]
        name = str(e.get("name", "")).strip()
        unit = str(e.get("unit", "") or "").strip() or "\u2014"
        out.append(
            f"| ({bus}, {slot}) | `{name}` | {e['_platform']} | {unit} | {rw_flag(e)} |"
        )
    out.append("")
    return "\n".join(out)


def curated_bus_headings(sm_text: str) -> dict[int, int]:
    """bus -> line index of the curated '## Bus N' heading."""
    result: dict[int, int] = {}
    for i, line in enumerate(sm_text.splitlines()):
        if line.startswith("## ") and "Bus" in line:
            for b in re.findall(r"Bus\s+(\d+)", line):
                result[int(b)] = i
    return result


def curated_sections(sm_text: str) -> list[dict]:
    """List curated '## Bus ...' sections with line range, buses and has_table."""
    lines = sm_text.splitlines()
    heads = [
        i
        for i, ln in enumerate(lines)
        if ln.startswith("## ") and "Bus" in ln
    ]
    sections = []
    for n, start in enumerate(heads):
        end = len(lines)
        for j in range(start + 1, len(lines)):
            if lines[j].startswith("## "):
                end = j
                break
        buses = [int(x) for x in re.findall(r"Bus\s+(\d+)", lines[start])]
        has_table = any(
            lines[j].startswith("| Slot |") or re.match(r"^\|\s*\(\d+,", lines[j])
            for j in range(start + 1, end)
        )
        sections.append(
            {"start": start, "end": end, "buses": buses, "has_table": has_table}
        )
    return sections


def add_anchors(sm_text: str, curated: dict[int, int]) -> str:
    lines = sm_text.splitlines()
    heading_buses: dict[int, list[int]] = {}
    for b, idx in curated.items():
        heading_buses.setdefault(idx, []).append(b)
    out: list[str] = []
    for i, line in enumerate(lines):
        if i in heading_buses:
            prev = out[-1] if out else ""
            need = [b for b in sorted(heading_buses[i]) if f'id="bus-{b}"' not in prev]
            if need:
                out.append("".join(f'<a id="bus-{b}"></a>' for b in need))
        out.append(line)
    return "\n".join(out)


def mapped_buses(app_text: str) -> set[int]:
    result: set[int] = set()
    for m in re.finditer(
        r"^\|\s*(?:\[)?(\d+)(?:\])?[^|]*\|[^|]*\|[^|]*\|[^|]*\|[^|]*\|\s*([^|]+?)\s*\|\s*$",
        app_text,
        re.M,
    ):
        col = m.group(2).strip().lower()
        if col.startswith("yes") or col.startswith("**yes"):
            result.add(int(m.group(1)))
    return result


def main() -> int:
    buses = load_slots()

    sm_text = SM_PATH.read_text(encoding="utf-8")
    sm_text = (
        re.sub(
            re.escape(APPENDIX_START) + r".*?" + re.escape(APPENDIX_END),
            "",
            sm_text,
            flags=re.S,
        ).rstrip()
        + "\n"
    )

    curated = curated_bus_headings(sm_text)
    curated_set = set(curated)

    app_text = APP_PATH.read_text(encoding="utf-8")
    mapped = mapped_buses(app_text)

    # 1) Fill curated sections that have no slot table by inserting generated
    #    tables at the end of that section (anchor stays on the curated heading).
    lines = sm_text.splitlines()
    filled: list[int] = []
    for sec in sorted(curated_sections(sm_text), key=lambda s: s["start"], reverse=True):
        if sec["has_table"]:
            continue
        data_buses = [b for b in sec["buses"] if b in buses and b in mapped]
        if not data_buses:
            continue
        block: list[str] = [""]
        for b in data_buses:
            block.append("| Slot | Sensor Name | Platform | Unit | Mode |")
            block.append("|------|------------|----------|------|------|")
            for slot in sorted(buses[b]):
                e = buses[b][slot]
                name = str(e.get("name", "")).strip()
                unit = str(e.get("unit", "") or "").strip() or "\u2014"
                block.append(
                    f"| ({b}, {slot}) | `{name}` | {e['_platform']} | {unit} |"
                    f" {rw_flag(e)} |"
                )
            block.append("")
            filled.append(b)
        insert_at = sec["end"]
        lines[insert_at:insert_at] = block
    sm_text = "\n".join(lines)

    # Recompute curated headings after insertion (line numbers shifted).
    curated = curated_bus_headings(sm_text)
    curated_set = set(curated)

    to_gen = sorted(b for b in mapped if b not in curated_set and b in buses)
    no_data = sorted(b for b in mapped if b not in curated_set and b not in buses)

    appendix = [
        APPENDIX_START,
        "",
        "## Appendix \u2014 auto-generated bus tables",
        "",
        "Generated directly from the live integration map (`sensor_maps/base.json` + "
        "`lights.json`). These cover mapped buses without a hand-curated section above; "
        "they list exactly the slots the integration reads/writes. The EHG app may "
        "define additional slots \u2014 see the component table in `ehg-app-metadata.md`.",
        "",
    ]
    for bus in to_gen:
        appendix.append(render_bus_table(bus, buses[bus]))
    appendix += [APPENDIX_END, ""]

    sm_text = add_anchors(sm_text, curated)
    sm_text = sm_text.rstrip() + "\n\n" + "\n".join(appendix)
    SM_PATH.write_text(sm_text, encoding="utf-8")

    sections_after = curated_set | set(to_gen)

    def fix_row(m: re.Match) -> str:
        bus = int(m.group("bus") or m.group("bus2"))
        rest = m.group("rest")
        cell = f"[{bus}](sensor-map.md#bus-{bus})" if bus in sections_after else str(bus)
        return f"| {cell} |{rest}"

    app_text = re.sub(
        r"^\|\s*(?:\[(?P<bus>\d+)\]\(sensor-map\.md#[^)]+\)|(?P<bus2>\d+))\s*\|(?P<rest>.*)$",
        fix_row,
        app_text,
        flags=re.M,
    )
    APP_PATH.write_text(app_text, encoding="utf-8")

    print("curated sections:", len(curated_set))
    print("filled table-less curated sections:", sorted(set(filled)))
    print("generated from live map:", len(to_gen), to_gen)
    print("mapped but no slot data in JSON (left plain):", no_data)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
