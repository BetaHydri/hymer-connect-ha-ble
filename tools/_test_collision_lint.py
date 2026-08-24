"""Collision lint: canonicity guardrail for the brandless base.json goal.

Two invariants that must hold before components can be merged into the
universal ``base.json`` (the no-brand-picker end-state):

  A. Cross-file key consistency - the same ``bus,slot`` must never be defined
     with a DIFFERENT ``name`` in two map files. A HYMER-vs-Eriba divergence for
     the same slot means the slot is not canonical and cannot move to base.json;
     a base-vs-overlay divergence means an overlay is silently redefining a
     shared decode.

  B. Per-brand name uniqueness - within a resolved brand (base + one overlay) an
     entity ``name`` must map to exactly one ``bus,slot``. Duplicate names
     collide in ENTITY_DEFS / Home Assistant unique_ids.

Only the decode-carrying sections (``sensors`` + ``switches``, keyed by
``bus,slot``) are checked; those define what a slot *means*.
"""
import json
import sys
from collections import defaultdict
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
MAPS = REPO / "custom_components" / "hymer_connect" / "sensor_maps"
SECTIONS = ("sensors", "switches")
# Shared files load for every brand (base first, then lights); overlays are per-brand.
SHARED = ("base.json", "lights.json")
OVERLAYS = ("hymer.json", "eriba.json")


def _keyed_names(doc: dict) -> dict[str, str]:
    """Return {"bus,slot": name} for the decode-carrying sections of one file."""
    out: dict[str, str] = {}
    for section in SECTIONS:
        block = doc.get(section, {})
        if not isinstance(block, dict):
            continue
        for key, defn in block.items():
            if key.startswith("_") or not isinstance(defn, dict):
                continue
            if "," in key and isinstance(defn.get("name"), str):
                out[key] = defn["name"]
    return out


def main() -> int:
    files = {
        name: _keyed_names(json.loads((MAPS / name).read_text(encoding="utf-8")))
        for name in SHARED + OVERLAYS
        if (MAPS / name).exists()
    }

    violations: list[str] = []

    # --- Check A: same bus,slot -> different name across files ---
    names_per_key: dict[str, dict[str, str]] = defaultdict(dict)
    for fname, keyed in files.items():
        for key, name in keyed.items():
            names_per_key[key][fname] = name
    for key, per_file in sorted(names_per_key.items()):
        distinct = set(per_file.values())
        if len(distinct) > 1:
            detail = ", ".join(f"{f}={n}" for f, n in sorted(per_file.items()))
            violations.append(f"  [A] slot {key} has conflicting names: {detail}")

    # --- Check B: duplicate name within a resolved brand (shared + overlay) ---
    shared_resolved: dict[str, str] = {}
    for name in SHARED:
        shared_resolved.update(files.get(name, {}))
    for overlay in OVERLAYS:
        if overlay not in files:
            continue
        resolved: dict[str, str] = dict(shared_resolved)
        resolved.update(files[overlay])  # overlay wins, mirroring the loader
        by_name: dict[str, list[str]] = defaultdict(list)
        for key, name in resolved.items():
            by_name[name].append(key)
        for name, keys in sorted(by_name.items()):
            if len(keys) > 1:
                brand = overlay.replace(".json", "")
                violations.append(
                    f"  [B] {brand}: name '{name}' maps to multiple slots: {', '.join(sorted(keys))}"
                )

    if violations:
        print("COLLISIONS FOUND:")
        print("\n".join(violations))
        print(f"\nFAIL: {len(violations)} collision(s)")
        return 1

    print("No slot/name collisions across map files  OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
