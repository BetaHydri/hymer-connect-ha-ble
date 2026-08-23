"""Offline regression test for the JSON-driven stepped/string select driver.

Loads the real hymer.json ``climate.selects`` definitions and drives every one
through ``select.HymerSteppedSelect`` to prove that the ``option_values`` feature
added for the Alde electric booster (``1 kW / 2 kW / 3 kW``) did NOT change the
behaviour of the other selects that the S600 and ML-T vehicles rely on:

  * ``fridge_compressor_freezer``      (bus 114, int step)   — ML-T
  * ``fridge_compressor_cooling_step`` (bus 114, int step)   — ML-T
  * ``fridge_absorber_cooling_step``   (bus 32,  int step)   — BMC I 680
  * ``alde_energy_priority`` / ``alde_hot_water`` / ``fridge_absorber_power_mode``
    / ``sat_position``                 (string selects)
  * ``alde_electric_boost``            (bus 5,   int step + option_values) — NEW

Home Assistant is not installed in the dev venv, so the modules that ``select.py``
imports are stubbed before it is loaded (same approach as ``_test_auto_slot_n.py``).

Run:  python tools/_test_stepped_select.py
Exit code 0 = pass, 1 = fail.

NOTE: the dedicated Truma heater selects (``HymerBoilerSelect`` /
``HymerHeaterEnergySelect``) and the S600 fridge (``HymerFridgeSelect``) are
SEPARATE classes that were not touched by the change, so they are out of scope
for this driver-level test.
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


# --- Stub the Home Assistant modules that select.py imports ------------------
_mod("homeassistant")
_mod("homeassistant.components")
_sel = _mod("homeassistant.components.select")


class SelectEntity:  # minimal stand-in
    _attr_options: list[str] = []

    def async_write_ha_state(self) -> None:  # noqa: D401
        pass


class SelectEntityDescription:  # noqa: D401
    pass


_sel.SelectEntity = SelectEntity
_sel.SelectEntityDescription = SelectEntityDescription

_ce = _mod("homeassistant.config_entries")
_ce.ConfigEntry = type("ConfigEntry", (), {})

_core = _mod("homeassistant.core")
_core.HomeAssistant = type("HomeAssistant", (), {})

_exc = _mod("homeassistant.exceptions")
_exc.HomeAssistantError = type("HomeAssistantError", (Exception,), {})

_mod("homeassistant.helpers")
_ep = _mod("homeassistant.helpers.entity_platform")
_ep.AddEntitiesCallback = type("AddEntitiesCallback", (), {})

_uc = _mod("homeassistant.helpers.update_coordinator")


class CoordinatorEntity:  # minimal stand-in
    def __init__(self, coordinator: object) -> None:
        self.coordinator = coordinator

    def __class_getitem__(cls, _item: object) -> type:
        return cls

    def _handle_coordinator_update(self) -> None:
        pass


_uc.CoordinatorEntity = CoordinatorEntity

# --- Stub the intra-package modules select.py imports ------------------------
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

# --- Load the REAL select.py as hcpkg.select --------------------------------
_spec = importlib.util.spec_from_file_location("hcpkg.select", _CC / "select.py")
assert _spec and _spec.loader
select = importlib.util.module_from_spec(_spec)
sys.modules["hcpkg.select"] = select
_spec.loader.exec_module(select)

# --- Load pia_decoder.py directly to get the real hymer.json select defs -----
_spec2 = importlib.util.spec_from_file_location("hc_pia_decoder", _CC / "pia_decoder.py")
assert _spec2 and _spec2.loader
pd = importlib.util.module_from_spec(_spec2)
sys.modules["hc_pia_decoder"] = pd
_spec2.loader.exec_module(pd)


class FakeCoordinator:
    def __init__(self, sensors: dict) -> None:
        self.data = {"signalr_sensors": sensors}
        self.calls: list[tuple] = []

    async def async_send_light_command(
        self,
        bus: int,
        sid: int,
        *,
        bool_value: object = None,
        uint_value: object = None,
        str_value: object = None,
    ) -> None:
        self.calls.append((bus, sid, bool_value, uint_value, str_value))


class FakeEntry:
    entry_id = "e1"


def _make(defn_key: str, defn: dict, sensors: dict):
    coord = FakeCoordinator(sensors)
    ent = select.HymerSteppedSelect(coord, FakeEntry(), defn_key, defn)
    return ent, coord


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def main() -> int:
    pd.load_sensor_map("hymer")
    defs = pd.STEPPED_SELECT_DEFS
    ok = True

    def check(cond: bool, msg: str) -> None:
        nonlocal ok
        if cond:
            print(f"PASS: {msg}")
        else:
            print(f"FAIL: {msg}")
            ok = False

    # Every expected select must be present.
    expected = {
        "fridge_compressor_freezer",
        "fridge_compressor_cooling_step",
        "fridge_absorber_cooling_step",
        "fridge_dometic_cooling_step",
        "fridge_dometic_mode",
        "alde_energy_priority",
        "alde_electric_boost",
        "alde_hot_water",
        "fridge_absorber_power_mode",
        "sat_position",
    }
    check(expected.issubset(defs), f"all expected selects loaded ({sorted(defs)})")

    # -- ML-T compressor freezer (int step, no option_values) -----------------
    ent, coord = _make("fridge_compressor_freezer", defs["fridge_compressor_freezer"],
                        {"fridge_compressor_freezer": 2})
    check(ent._option_values is None, "compressor_freezer has no option_values (legacy)")
    check(ent.current_option == "2", f"compressor_freezer readback 2 -> '2' (got {ent.current_option!r})")
    coord.calls.clear()
    _run(ent.async_select_option("3"))
    check(coord.calls == [(114, 4, None, 3, None)], f"compressor_freezer select '3' -> uint 3 (got {coord.calls})")
    coord.calls.clear()
    _run(ent.async_select_option("Off"))
    check(coord.calls == [(114, 4, None, 0, None)], f"compressor_freezer select 'Off' -> uint 0 (got {coord.calls})")

    # -- ML-T compressor cooling step (power-gated int step) ------------------
    d = defs["fridge_compressor_cooling_step"]
    ent, coord = _make("fridge_compressor_cooling_step", d,
                       {"fridge_compressor_cooling_step": 4, "fridge_compressor_power": True})
    check(ent.current_option == "4", f"compressor_cooling readback 4 -> '4' (got {ent.current_option!r})")
    ent2, _ = _make("fridge_compressor_cooling_step", d,
                    {"fridge_compressor_cooling_step": 4, "fridge_compressor_power": False})
    check(ent2.current_option == "Off", f"compressor_cooling power=False -> 'Off' (got {ent2.current_option!r})")
    coord.calls.clear()
    _run(ent.async_select_option("5"))
    check(coord.calls == [(114, 1, True, None, None), (114, 3, None, 5, None)],
          f"compressor_cooling select '5' -> power on + uint 5 (got {coord.calls})")

    # -- BMC absorber cooling step (bus 32, same shape) -----------------------
    d = defs["fridge_absorber_cooling_step"]
    ent, coord = _make("fridge_absorber_cooling_step", d,
                       {"fridge_absorber_cooling_step": 3, "fridge_absorber_power": True})
    check(ent.current_option == "3", f"absorber_cooling readback 3 -> '3' (got {ent.current_option!r})")
    coord.calls.clear()
    _run(ent.async_select_option("Off"))
    check(coord.calls == [(32, 1, False, None, None)], f"absorber_cooling 'Off' -> power off (got {coord.calls})")

    # -- NEW: HYMER Dometic cooling step (bus 60, power sid 8 + level sid 2) ---
    d = defs["fridge_dometic_cooling_step"]
    ent, coord = _make("fridge_dometic_cooling_step", d,
                       {"dometic_fridge_level": 3, "dometic_fridge_power": True})
    check(ent.current_option == "3", f"dometic_cooling readback 3 -> '3' (got {ent.current_option!r})")
    ent2, _ = _make("fridge_dometic_cooling_step", d,
                    {"dometic_fridge_level": 3, "dometic_fridge_power": False})
    check(ent2.current_option == "Off", f"dometic_cooling power=False -> 'Off' (got {ent2.current_option!r})")
    coord.calls.clear()
    _run(ent.async_select_option("5"))
    check(coord.calls == [(60, 8, True, None, None), (60, 2, None, 5, None)],
          f"dometic_cooling select '5' -> power on + uint 5 (got {coord.calls})")
    coord.calls.clear()
    _run(ent.async_select_option("Off"))
    check(coord.calls == [(60, 8, False, None, None)], f"dometic_cooling 'Off' -> power off (got {coord.calls})")

    # -- NEW: HYMER Dometic user mode (bus 60 slot 1, string select) ----------
    d = defs["fridge_dometic_mode"]
    ent, coord = _make("fridge_dometic_mode", d, {"dometic_fridge_mode": "Silent Mode"})
    check(ent.current_option == "Silent Mode", f"dometic_mode string readback (got {ent.current_option!r})")
    coord.calls.clear()
    _run(ent.async_select_option("Turbo Mode"))
    check(coord.calls == [(60, 1, None, None, "Turbo Mode")], f"dometic_mode select -> str (got {coord.calls})")

    # -- NEW: Alde electric booster (int step WITH option_values) -------------
    d = defs["alde_electric_boost"]
    check(d.get("options") == ["Off", "1 kW", "2 kW", "3 kW"], f"alde booster options are kW labels ({d.get('options')})")
    ent, coord = _make("alde_electric_boost", d, {"alde_electric_setting": 2})
    check(ent._option_values == [0, 1, 2, 3], f"alde booster option_values=[0,1,2,3] (got {ent._option_values})")
    check(ent.current_option == "2 kW", f"alde booster readback 2 -> '2 kW' (got {ent.current_option!r})")
    ent0, _ = _make("alde_electric_boost", d, {"alde_electric_setting": 0})
    check(ent0.current_option == "Off", f"alde booster readback 0 -> 'Off' (got {ent0.current_option!r})")
    coord.calls.clear()
    _run(ent.async_select_option("3 kW"))
    check(coord.calls == [(5, 7, None, 3, None)], f"alde booster select '3 kW' -> uint 3 (got {coord.calls})")
    coord.calls.clear()
    _run(ent.async_select_option("Off"))
    check(coord.calls == [(5, 7, None, 0, None)], f"alde booster select 'Off' -> uint 0 (got {coord.calls})")

    # -- String selects unaffected -------------------------------------------
    d = defs["alde_energy_priority"]
    ent, coord = _make("alde_energy_priority", d, {"alde_energy_priority": "Prio EL"})
    check(ent.current_option == "Prio EL", f"energy_priority string readback (got {ent.current_option!r})")
    coord.calls.clear()
    _run(ent.async_select_option("Prio Gas"))
    check(coord.calls == [(5, 5, None, None, "Prio Gas")], f"energy_priority select -> str (got {coord.calls})")

    print("\n" + ("ALL PASS" if ok else "FAILURES DETECTED"))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
