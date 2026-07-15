"""Offline regression test for the JSON-driven number driver (Alde setpoint).

Loads the real hymer.json ``climate.numbers`` definitions and drives
``number.HymerNumber`` to prove:
  1. The Alde setpoint is loaded on bus 5 slot 3 with the expected range/unit.
  2. ``native_value`` reflects the backing sensor.
  3. ``async_set_native_value`` sends a single multi-sensor command carrying a
     ``float_value`` on (bus 5, sid 3).

Home Assistant is stubbed (same approach as ``_test_stepped_select.py``).

Run:  python tools/_test_number.py
Exit code 0 = pass, 1 = fail.
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
import types
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_CC = _ROOT / "custom_components" / "hymer_connect"


def _mod(name: str) -> types.ModuleType:
    m = types.ModuleType(name)
    sys.modules[name] = m
    return m


# --- Stub the Home Assistant modules that number.py imports ------------------
_mod("homeassistant")
_mod("homeassistant.components")
_num = _mod("homeassistant.components.number")


class NumberEntity:  # minimal stand-in
    def async_write_ha_state(self) -> None:
        pass


class NumberMode:  # minimal stand-in
    BOX = "box"
    SLIDER = "slider"
    AUTO = "auto"


_num.NumberEntity = NumberEntity
_num.NumberMode = NumberMode

_ce = _mod("homeassistant.config_entries")
_ce.ConfigEntry = type("ConfigEntry", (), {})

_core = _mod("homeassistant.core")
_core.HomeAssistant = type("HomeAssistant", (), {})

_mod("homeassistant.helpers")
_ep = _mod("homeassistant.helpers.entity_platform")
_ep.AddEntitiesCallback = type("AddEntitiesCallback", (), {})

_uc = _mod("homeassistant.helpers.update_coordinator")


class CoordinatorEntity:
    def __init__(self, coordinator: object) -> None:
        self.coordinator = coordinator

    def __class_getitem__(cls, _item: object) -> type:
        return cls

    def _handle_coordinator_update(self) -> None:
        pass


_uc.CoordinatorEntity = CoordinatorEntity

# --- Stub the intra-package modules number.py imports ------------------------
_pkg = _mod("hcpkg")
_pkg.__path__ = [str(_CC)]  # type: ignore[attr-defined]

_const = _mod("hcpkg.const")
_const.DOMAIN = "hymer_connect"
_const.MANUFACTURER = "EHG"

_coord = _mod("hcpkg.coordinator")
_coord.HymerConnectCoordinator = type("HymerConnectCoordinator", (), {})

_sensor = _mod("hcpkg.sensor")


def _resolve_path(data: object, path: str) -> object:
    cur: object = data
    for part in path.split("."):
        if isinstance(cur, dict):
            cur = cur.get(part)
        else:
            return None
    return cur


_sensor._resolve_path = _resolve_path

# --- Load the REAL number.py as hcpkg.number --------------------------------
_spec = importlib.util.spec_from_file_location("hcpkg.number", _CC / "number.py")
assert _spec and _spec.loader
number = importlib.util.module_from_spec(_spec)
sys.modules["hcpkg.number"] = number
_spec.loader.exec_module(number)

# --- Load pia_decoder.py directly to get the real hymer.json number defs -----
_spec2 = importlib.util.spec_from_file_location("hc_pia_decoder", _CC / "pia_decoder.py")
assert _spec2 and _spec2.loader
pd = importlib.util.module_from_spec(_spec2)
sys.modules["hc_pia_decoder"] = pd
_spec2.loader.exec_module(pd)


class FakeCoordinator:
    def __init__(self, sensors: dict) -> None:
        self.data = {"signalr_sensors": sensors}
        self.multi_calls: list[list[dict]] = []

    async def async_send_multi_sensor_command(self, sensors: list[dict]) -> None:
        self.multi_calls.append(sensors)


class FakeEntry:
    entry_id = "e1"


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def main() -> int:
    pd.load_sensor_map("hymer")
    defs = pd.NUMBER_DEFS
    ok = True

    def check(cond: bool, msg: str) -> None:
        nonlocal ok
        if cond:
            print(f"PASS: {msg}")
        else:
            print(f"FAIL: {msg}")
            ok = False

    check("alde_setpoint" in defs, f"alde_setpoint loaded ({list(defs)})")
    d = defs["alde_setpoint"]

    coord = FakeCoordinator({"alde_setpoint": 21.5})
    ent = number.HymerNumber(coord, FakeEntry(), "alde_setpoint", d)

    check(ent._bus == 5 and ent._sid == 3, f"alde_setpoint targets bus 5 slot 3 (got {ent._bus},{ent._sid})")
    check(ent._attr_native_min_value == 5.0 and ent._attr_native_max_value == 30.0,
          f"range 5-30 (got {ent._attr_native_min_value}-{ent._attr_native_max_value})")
    check(ent._attr_native_step == 0.5, f"step 0.5 (got {ent._attr_native_step})")
    check(ent.native_value == 21.5, f"native_value reads backing sensor (got {ent.native_value})")

    _run(ent.async_set_native_value(19.5))
    check(
        coord.multi_calls == [[{"bus_id": 5, "sensor_id": 3, "float_value": 19.5}]],
        f"set 19.5 -> float multi-sensor command (got {coord.multi_calls})",
    )
    check(ent.native_value == 19.5, f"optimistic value after write (got {ent.native_value})")

    print("\n" + ("ALL PASS" if ok else "FAILURES DETECTED"))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
