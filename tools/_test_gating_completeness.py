"""Completeness lint: every mapped *optional appliance* entity must be gated.

Rationale (see docs/ehg-app-metadata.md): the EHG SCU exposes each appliance as
a fixed component bound to a fixed bus. Almost every appliance category has
several mutually-exclusive hardware variants (9 fridges, ~8 heaters, ~8 ACs,
3 BMS, 4 tank monitors, ...), yet a vehicle carries only ONE per category. So an
un-gated appliance entity is a guaranteed phantom on every vehicle that has a
different variant. This lint derives the appliance universe from the decompiled
EHG metadata table and asserts that every mapped appliance entity carries
``require_observed``. This is the enforcement that makes moving components into
the universal ``base.json`` (the brandless end-state) safe.

Universal always-present kinds (chassis / scu_platform / vehicle_info) and the
naming-variable kinds (light / lighting_module / habitation / generic component)
are intentionally NOT enforced here.
"""
import json
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
MAPS = REPO / "custom_components" / "hymer_connect" / "sensor_maps"
METADATA = REPO / "docs" / "ehg-app-metadata.md"

# Kinds that are optional, mutually-exclusive appliances -> MUST be gated.
APPLIANCE_KINDS = {
    "fridge", "heater", "heater_neo", "truma_heater", "air_conditioner",
    "sat_antenna", "toilet", "tank_monitor", "bms", "inverter",
    "power_system", "tpms", "roof", "ventilation", "awning", "switch_pad",
    "solar_charger",
}

# Kinds intentionally NOT enforced (universal, or naming-variable, or unknown).
# chassis / scu_platform / vehicle_info -> always present
# light / lighting_module -> per-floorplan naming, core UX (separate decision)
# habitation -> carries core water/tank/pump data (base.json); separate decision
# component -> generic/unclassified


def load_bus_kinds() -> dict[int, str]:
    """Parse the EHG metadata *component* table into {bus_id: kind}.

    Only the top-level component table is parsed; the per-slot detail tables
    further down the file reuse ``| <slot> | `name` | <datatype> |`` rows that
    would otherwise clobber low bus IDs (e.g. slot 8 -> bus 8).
    """
    row = re.compile(r"^\|\s*(\d+)\s*\|\s*`[^`]*`\s*\|\s*([a-z_]+)\s*\|")
    kinds: dict[int, str] = {}
    in_table = False
    for line in METADATA.read_text(encoding="utf-8").splitlines():
        if line.startswith("## Component Table"):
            in_table = True
            continue
        if in_table and line.startswith("## "):
            break  # reached the slot-detail section
        if in_table:
            m = row.match(line)
            if m:
                kinds[int(m.group(1))] = m.group(2)
    return kinds


def _bus_of(key: str) -> int | None:
    m = re.match(r"^\s*(\d+)\s*,", key)
    return int(m.group(1)) if m else None


def iter_entities(doc: dict):
    """Yield (bus_id, label, defn) for every entity in one map file."""
    sensors = doc.get("sensors", {})
    if isinstance(sensors, dict):
        for key, defn in sensors.items():
            if not isinstance(defn, dict) or "platform" not in defn:
                continue
            bus = _bus_of(key)
            if bus is not None:
                yield bus, f"sensor {key} ({defn.get('name')})", defn

    switches = doc.get("switches", {})
    if isinstance(switches, dict):
        for key, defn in switches.items():
            if not isinstance(defn, dict) or "name" not in defn:
                continue
            bus = _bus_of(key)
            if bus is not None:
                yield bus, f"switch {key} ({defn.get('name')})", defn

    climate = doc.get("climate", {})
    if isinstance(climate, dict):
        for kind_key in ("fridge", "truma_heater"):
            defn = climate.get(kind_key)
            if isinstance(defn, dict):
                bus = defn.get("control_bus") or defn.get("heater_bus")
                if isinstance(bus, int):
                    yield bus, f"climate.{kind_key}", defn
        for section in ("selects", "numbers"):
            block = climate.get(section, {})
            if isinstance(block, dict):
                for key, defn in block.items():
                    if not isinstance(defn, dict) or "control_bus" not in defn:
                        continue
                    bus = defn.get("control_bus")
                    if isinstance(bus, int):
                        yield bus, f"climate.{section}.{key}", defn


def main() -> int:
    bus_kinds = load_bus_kinds()
    if not bus_kinds:
        print("FAIL: could not parse EHG metadata table")
        return 1

    violations: list[str] = []
    for name in ("base.json", "hymer.json", "eriba.json"):
        path = MAPS / name
        if not path.exists():
            continue
        doc = json.loads(path.read_text(encoding="utf-8"))
        for bus, label, defn in iter_entities(doc):
            kind = bus_kinds.get(bus)
            if kind in APPLIANCE_KINDS and not defn.get("require_observed"):
                violations.append(f"  {name}: bus {bus} [{kind}] {label}")

    if violations:
        print("UN-GATED APPLIANCE ENTITIES (must set require_observed):")
        print("\n".join(sorted(violations)))
        print(f"\nFAIL: {len(violations)} un-gated appliance entities")
        return 1

    print("All mapped appliance entities are gated  OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
