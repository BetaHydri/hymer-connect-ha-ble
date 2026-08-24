"""Generate gated base.json sensor candidates from tools/ehg_metadata.json.

Phase B (brandless auto-mapping): every EHG appliance is bound to a fixed bus,
so a component mapped once in the universal base.json appears on any brand that
reports it. This tool proposes `sensors` entries for the read-only slots of
documented components that are NOT yet mapped, applying the project's
conservative convention:

  * All emitted entries carry ``require_observed`` (optional components).
  * Writable slots (mode rw/write) are SKIPPED - controls need hand-porting.
  * bool                     -> binary_sensor (device_class inferred from label)
  * string                   -> sensor (diagnostic for status/mode/firmware)
  * float + unit             -> sensor, enabled (EHG floats are real units)
  * int/uint + '%'           -> sensor, enabled (direct 0-100)
  * int/uint + scaled unit   -> sensor, enabled:false + _unverified
    (V/A/W/psi/bar/... raw ints may be milli-/centi-scaled; confirm on-vehicle)

Output is a REVIEW ARTIFACT (tools/_generated_base_candidates.json), never an
automatic edit of base.json. A maintainer reviews and ports the wanted blocks.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
METADATA_JSON = REPO / "tools" / "ehg_metadata.json"
MAPS = REPO / "custom_components" / "hymer_connect" / "sensor_maps"
OUT = REPO / "tools" / "_generated_base_candidates.json"

# Universal / non-appliance kinds we never auto-generate.
SKIP_KINDS = {"chassis", "scu_platform", "vehicle_info", "light", "lighting_module"}

SCALED_UNITS = {"V", "A", "W", "psi", "bar", "kPa", "hPa", "mbar", "Ah", "kWh", "Wh"}
_DC_NUMERIC = {"V": "voltage", "A": "current", "W": "power", "Hz": "frequency",
               "°C": "temperature", "Ah": "battery", "kWh": "energy", "Wh": "energy",
               "psi": "pressure", "bar": "pressure", "kPa": "pressure"}


def _snake(text: str) -> str:
    return re.sub(r"_+", "_", re.sub(r"[^a-z0-9]+", "_", text.lower())).strip("_")


def _mapped_keys() -> set[str]:
    keys: set[str] = set()
    for name in ("base.json", "hymer.json", "eriba.json"):
        p = MAPS / name
        if not p.exists():
            continue
        doc = json.loads(p.read_text(encoding="utf-8"))
        for section in ("sensors", "switches"):
            for key in (doc.get(section) or {}):
                m = re.match(r"^(\d+),(\d+)", key)  # tolerate "b,s" and "b,s#disc"
                if m:
                    keys.add(f"{m.group(1)},{m.group(2)}")
    return keys


def _binary_device_class(label: str) -> str | None:
    L = label.lower()
    if any(w in L for w in ("error", "fault", "failure", "warning", "alarm")):
        return "problem"
    if "door" in L:
        return "door"
    if "moving" in L:
        return "moving"
    if any(w in L for w in ("running", "active", "heating")):
        return "running"
    if "connected" in L:
        return "connectivity"
    if any(w in L for w in ("power", "_on", "enable")):
        return "power"
    return None


def _make_entry(prefix: str, sid: str, meta: dict) -> dict:
    label = meta.get("label", f"slot{sid}")
    name = f"{prefix}_{label}"
    dt = (meta.get("datatype") or "").lower()
    unit = meta.get("unit")
    entry: dict = {"name": name, "require_observed": True}

    if dt == "bool":
        entry["platform"] = "binary_sensor"
        dc = _binary_device_class(label)
        if dc:
            entry["device_class"] = dc
        return entry

    entry["platform"] = "sensor"
    if dt == "string":
        if any(w in label.lower() for w in ("status", "mode", "firmware", "error", "state")):
            entry["entity_category"] = "diagnostic"
        return entry

    # numeric (int/uint/float)
    if unit:
        entry["unit"] = unit
        dc = _DC_NUMERIC.get(unit)
        if dc and not (unit == "Ah" and "batt" not in label.lower()):
            entry["device_class"] = dc
        entry["state_class"] = "measurement"
        is_scaled_suspect = dt in ("int", "uint", "uint8", "uint16", "int16") and unit in SCALED_UNITS
        if is_scaled_suspect:
            entry["enabled"] = False
            entry["_unverified"] = "raw int scale unconfirmed (may be milli-/centi-scaled)"
    else:
        entry["entity_category"] = "diagnostic"
    return entry


def main() -> int:
    meta = json.loads(METADATA_JSON.read_text(encoding="utf-8"))
    components = meta["components"]
    slots = meta["slots"]
    mapped = _mapped_keys()

    candidates: dict[str, dict] = {}
    summary: list[str] = []
    for bus in sorted(slots, key=int):
        comp = components.get(bus, {})
        kind = comp.get("kind", "component")
        if kind in SKIP_KINDS:
            continue
        prefix = _snake(comp.get("component_id", f"bus{bus}"))
        block: dict[str, dict] = {}
        skipped_write = skipped_mapped = 0
        for sid, sm in slots[bus].items():
            key = f"{bus},{sid}"
            if key in mapped:
                skipped_mapped += 1
                continue
            if (sm.get("mode") or "r").lower() not in ("r", "read"):
                skipped_write += 1
                continue
            block[key] = _make_entry(prefix, sid, sm)
        if block:
            candidates[bus] = {
                "_component": comp.get("component_id"),
                "_kind": kind,
                "_name": comp.get("name"),
                "entries": block,
            }
            n_dis = sum(1 for e in block.values() if e.get("enabled") is False)
            summary.append(
                f"  bus {bus:>3} [{kind}] {comp.get('component_id')}: "
                f"{len(block)} sensors ({n_dis} disabled/unverified), "
                f"skipped {skipped_mapped} mapped + {skipped_write} writable"
            )

    OUT.write_text(json.dumps(candidates, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    total = sum(len(c["entries"]) for c in candidates.values())
    print(f"Wrote {OUT.relative_to(REPO)}: {total} candidate sensors across {len(candidates)} buses")
    print("\n".join(summary))
    return 0


if __name__ == "__main__":
    sys.exit(main())
