"""Apply the missing gated READ sensors from tools/ehg_metadata.json into
sensor_maps/base.json -- add-only, format-preserving, collision-safe.

Checks base.json + lights.json + hymer.json + eriba.json for already-mapped
(bus,slot) keys AND existing entity names. Only emits read slots for appliance
kinds (skips light/lighting_module/chassis/scu_platform/vehicle_info). Writable
slots are skipped (controls hand-ported separately). Names are component-
prefixed and de-duplicated (append _b{bus} on a name clash). Units are cleaned
(drop bogus 'step'; mV->V/div1000, mA->A/div1000; other scaled ints shipped
enabled:false _unverified).

Usage:
  python tools/_apply_missing_sensors.py            # dry-run: report only
  python tools/_apply_missing_sensors.py --apply    # insert into base.json
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
META = REPO / "tools" / "ehg_metadata.json"
MAPS = REPO / "custom_components" / "hymer_connect" / "sensor_maps"
BASE = MAPS / "base.json"

SKIP_KINDS = {"chassis", "scu_platform", "vehicle_info", "light", "lighting_module"}
# Protect Martin's SIU smart-sensor range (70-77, templated smart_*/hss_*) and
# opaque unknown "Component##" buses (no meaningful labels -> no value).
SKIP_BUSES = {"68", "69", "70", "71", "72", "73", "74", "75", "76", "77", "115"}
# Clean, family-consistent prefixes (match existing entities on partial buses).
PREFIX_OVERRIDE = {
    "5": "alde", "7": "aventa", "10": "sat", "33": "sat_teleco",
    "65": "aventa_direct", "91": "seelevel", "123": "aventa_2g",
    "35": "tank_philippi", "53": "cbe_water", "54": "cbe_sens",
    "55": "cbe_batinfo", "87": "cbe_water_notank", "92": "inverter_pd1600",
    "98": "power_modulus", "101": "roof", "110": "ad100", "111": "battery_bos",
    "112": "shoreline", "122": "ad100_nopump", "126": "teb310d",
}
SCALED = {"V", "A", "W", "Ah", "kWh", "Wh", "psi", "bar", "kPa", "hPa", "mbar"}
DC = {"V": "voltage", "A": "current", "W": "power", "Hz": "frequency",
      "°C": "temperature", "Ah": "battery", "kWh": "energy", "Wh": "energy",
      "psi": "pressure", "bar": "pressure", "kPa": "pressure"}


def snake(s: str) -> str:
    return re.sub(r"_+", "_", re.sub(r"[^a-z0-9]+", "_", s.lower())).strip("_")


def _binary_dc(label: str) -> str | None:
    if any(w in label for w in ("error", "fault", "failure", "warning", "alarm")):
        return "problem"
    if "door" in label:
        return "door"
    if "moving" in label:
        return "moving"
    if any(w in label for w in ("running", "active")):
        return "running"
    if "connected" in label:
        return "connectivity"
    if label.endswith("_on") or label.endswith("power"):
        return "power"
    return None


def _collect() -> tuple[set[str], set[str]]:
    mapped: set[str] = set()
    names: set[str] = set()
    for fn in ("base.json", "lights.json", "hymer.json", "eriba.json"):
        p = MAPS / fn
        if not p.exists():
            continue
        doc = json.loads(p.read_text(encoding="utf-8"))
        for sect in ("sensors", "switches", "lights"):
            for k, v in (doc.get(sect) or {}).items():
                m = re.match(r"^(\d+),(\d+)", k)
                if m:
                    mapped.add(f"{m.group(1)},{m.group(2)}")
                if isinstance(v, dict) and v.get("name"):
                    names.add(v["name"])
        clim = doc.get("climate") or {}
        for sub in clim.values():
            if isinstance(sub, dict):
                for v in sub.values():
                    if isinstance(v, dict) and v.get("name"):
                        names.add(v["name"])
    return mapped, names


def _make(prefix: str, sid: str, sm: dict, names: set[str], bus: str) -> dict:
    label = sm.get("label", f"slot{sid}")
    dt = (sm.get("datatype") or "").lower()
    unit = sm.get("unit")
    name = f"{prefix}_{label}"
    if name in names:
        name = f"{name}_b{bus}"
    names.add(name)
    e: dict = {"name": name, "require_observed": True}
    if dt == "bool":
        e["platform"] = "binary_sensor"
        d = _binary_dc(label)
        if d:
            e["device_class"] = d
        return e
    e["platform"] = "sensor"
    if dt == "string":
        if any(w in label for w in ("status", "mode", "firmware", "error", "state", "type")):
            e["entity_category"] = "diagnostic"
        return e
    if unit and unit != "step":
        if unit == "mV":
            e.update({"unit": "V", "transform": "div1000", "device_class": "voltage", "state_class": "measurement"})
        elif unit == "mA":
            e.update({"unit": "A", "transform": "div1000", "device_class": "current", "state_class": "measurement"})
        else:
            e["unit"] = unit
            if unit in DC:
                e["device_class"] = DC[unit]
            e["state_class"] = "measurement"
            if dt in ("int", "uint", "uint8", "uint16", "int16") and unit in SCALED:
                e["enabled"] = False
                e["_unverified"] = "raw int scale unconfirmed (may be milli-/centi-scaled)"
    else:
        e["entity_category"] = "diagnostic"
    return e


def main() -> int:
    apply = "--apply" in sys.argv
    meta = json.loads(META.read_text(encoding="utf-8"))
    comps, slots = meta["components"], meta["slots"]
    mapped, names = _collect()

    new: dict[str, dict] = {}
    per_bus: list[str] = []
    for bus in sorted(slots, key=int):
        comp = comps.get(bus, {})
        kind = comp.get("kind", "component")
        if kind in SKIP_KINDS or bus in SKIP_BUSES:
            continue
        prefix = PREFIX_OVERRIDE.get(bus) or snake(comp.get("component_id", f"bus{bus}"))
        block = 0
        for sid, sm in slots[bus].items():
            key = f"{bus},{sid}"
            if key in mapped:
                continue
            if (sm.get("mode") or "r").lower() not in ("r", "read"):
                continue
            new[key] = _make(prefix, sid, sm, names, bus)
            block += 1
        if block:
            per_bus.append(f"  bus {bus:>3} [{kind}] {comp.get('component_id')}: +{block}")

    print(f"{len(new)} new gated read sensors across {len(per_bus)} buses")
    print("\n".join(per_bus))
    print("--- sample names ---")
    for k, v in list(new.items())[:25]:
        print(f"  {k}: {v['name']} ({v['platform']})")

    if not apply:
        print("\n(dry-run; pass --apply to insert into base.json)")
        return 0

    text = BASE.read_text(encoding="utf-8")
    anchor = '  "sensors": {\n'
    if anchor not in text:
        print("ERROR: sensors anchor not found")
        return 1
    lines = []
    for k, v in new.items():
        lines.append('    ' + json.dumps(k, ensure_ascii=False) + ": " + json.dumps(v, ensure_ascii=False) + ",")
    block_text = "\n".join(lines) + "\n"
    text = text.replace(anchor, anchor + block_text, 1)
    BASE.write_text(text, encoding="utf-8")
    print(f"\nINSERTED {len(new)} entries into base.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
