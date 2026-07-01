"""Offline smoke-test for auto-slot {n} template expansion.

Loads the real hymer.json overlay (which uses the "{n}" template form) and
verifies:
  1. Buses 70/71/73/74 are registered as auto-slot groups.
  2. No ENTITY_DEFS key contains a literal "{n}" placeholder (no ghosts).
  3. A synthetic bus-70 PIA frame carrying a binary connectedComponentInstance
     materialises hss_tyre1_* correctly, and a second instance materialises #2.

Run:  python tools/_test_auto_slot_n.py
Exit code 0 = pass, 1 = fail.  Deletes any _auto_slots.json it creates.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_PIA = _ROOT / "custom_components" / "hymer_connect" / "pia_decoder.py"

# Load pia_decoder.py directly by path so we don't trigger the package
# __init__.py (which imports Home Assistant, unavailable in this venv).
_spec = importlib.util.spec_from_file_location("hc_pia_decoder", _PIA)
pd = importlib.util.module_from_spec(_spec)
sys.modules["hc_pia_decoder"] = pd
_spec.loader.exec_module(pd)


def _build_frame(bus_id: int, sensor_id: int, *, float_val: float, instance: bytes) -> bytes:
    """Build a minimal CCValue protobuf: f1=sid, f2=bus, f6=float, f10=instance."""
    body = pd._encode_varint_field(1, sensor_id)
    body += pd._encode_varint_field(2, bus_id)
    body += pd._encode_float_field(6, float_val)
    body += pd._encode_bytes_field(10, instance)
    # Nest one level so _extract_sensors_recursive walks into it (depth 1).
    return pd._encode_bytes_field(2, body)


def main() -> int:
    store = pd._AUTO_SLOT_STORE
    pre_existing = store.exists()

    pd.load_sensor_map("hymer")

    ok = True

    # 1. Auto-slot groups registered.
    for bus in (70, 71, 73, 74):
        if bus not in pd.AUTO_SLOT_GROUPS:
            print(f"FAIL: bus {bus} not in AUTO_SLOT_GROUPS")
            ok = False
    if ok:
        print(f"PASS: auto-slot groups = {sorted(pd.AUTO_SLOT_GROUPS)}")

    # 2. No {n} ghost entities.
    ghosts = [k for k in pd.ENTITY_DEFS if "{n}" in k]
    if ghosts:
        print(f"FAIL: {len(ghosts)} ghost ENTITY_DEFS with '{{n}}': {ghosts[:5]}")
        ok = False
    else:
        print("PASS: no '{n}' ghost entities in ENTITY_DEFS")

    # 3. Templates captured for bus 70 slots 1-4.
    tmpl70 = pd.AUTO_SLOT_TEMPLATES.get(70, {})
    if set(tmpl70) != {1, 2, 3, 4}:
        print(f"FAIL: bus 70 templates = {sorted(tmpl70)} (expected 1-4)")
        ok = False
    else:
        print(f"PASS: bus 70 template name_tmpl(2) = {tmpl70[2]['name_tmpl']!r}")

    # 4. Decode two devices -> materialise hss_tyre1_* and hss_tyre2_*.
    dev_a = bytes.fromhex("03eded12d9d6")
    dev_b = bytes.fromhex("03eded12d7be")

    out: dict = {}
    pd._extract_sensors_recursive(_build_frame(70, 2, float_val=3.71, instance=dev_a), out, depth=0)
    pd._extract_sensors_recursive(_build_frame(70, 2, float_val=4.87, instance=dev_b), out, depth=0)

    if out.get("hss_tyre1_pressure") == 3.71 and out.get("hss_tyre2_pressure") == 4.87:
        print(f"PASS: materialised tyre1={out['hss_tyre1_pressure']} tyre2={out['hss_tyre2_pressure']}")
    else:
        print(f"FAIL: expected tyre1=3.71 tyre2=4.87, got {out}")
        ok = False

    # Both concrete names must now be real ENTITY_DEFS (for sensor.py pickup).
    for name in ("hss_tyre1_pressure", "hss_tyre2_pressure"):
        if name not in pd.ENTITY_DEFS:
            print(f"FAIL: {name} not registered in ENTITY_DEFS after materialise")
            ok = False

    # Cleanup: remove store file if the test created it.
    if not pre_existing and store.exists():
        store.unlink()

    print("\nRESULT:", "ALL PASS" if ok else "FAILURES")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
