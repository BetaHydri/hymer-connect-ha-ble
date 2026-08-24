"""Runtime check: require_observed survives the loader into runtime defs."""
import importlib.util
import sys

spec = importlib.util.spec_from_file_location(
    "pd", "custom_components/hymer_connect/pia_decoder.py"
)
pd = importlib.util.module_from_spec(spec)
sys.modules["pd"] = pd
spec.loader.exec_module(pd)

for brand in ("hymer", "eriba"):
    pd.ENTITY_DEFS.clear()
    pd.STEPPED_SELECT_DEFS.clear()
    pd.SENSOR_MAP.clear()
    pd._overlays_loaded.clear()
    pd.load_sensor_map(brand)

    for name in ("dometic_fridge_mode", "dometic_fridge_level",
                 "dometic_fridge_power", "dometic_fridge_warning"):
        assert name in pd.ENTITY_DEFS, f"{name} missing from ENTITY_DEFS ({brand})"
        assert pd.ENTITY_DEFS[name].get("require_observed") is True, \
            f"{name} not gated in ENTITY_DEFS ({brand})"

    assert "require_observed" not in pd.ENTITY_DEFS.get("odometer", {})

    for key in ("fridge_dometic_cooling_step", "fridge_dometic_mode"):
        defn = pd.STEPPED_SELECT_DEFS[key]
        assert defn.get("require_observed") is True, f"{key} not gated ({brand})"
        read = defn.get("read") or {}
        watch = [n for n in (read.get("step_sensor"), read.get("value_sensor"),
                             read.get("power_sensor")) if n]
        assert watch, f"{key} has no watchable read sensor ({brand})"

    print(f"Runtime gate flags survive loader for brand '{brand}'  OK")

# --- S600 Thetford fridge + Truma Combi (dedicated-class gating, v2.69.1) ---
pd.ENTITY_DEFS.clear()
pd.STEPPED_SELECT_DEFS.clear()
pd.SENSOR_MAP.clear()
pd.CLIMATE_DEFS.clear()
pd.SWITCH_DEFS.clear()
pd._overlays_loaded.clear()
pd.load_sensor_map("hymer")

assert pd.CLIMATE_DEFS["fridge"].get("require_observed") is True, "fridge climate not gated"
assert pd.CLIMATE_DEFS["truma_heater"].get("require_observed") is True, "truma_heater not gated"
assert pd.SWITCH_DEFS["34,2"].get("require_observed") is True, "fridge_eco switch not gated"
for name in ("fridge_door", "fridge_warning", "fridge_mode", "fridge_status",
             "heater_setpoint", "heater_state", "heater_fuel_type",
             "heater_electric_power", "truma_status"):
    assert name in pd.ENTITY_DEFS, f"{name} missing from ENTITY_DEFS (hymer)"
    assert pd.ENTITY_DEFS[name].get("require_observed") is True, \
        f"{name} not gated in ENTITY_DEFS (hymer)"
# A universal chassis sensor must stay ungated
assert "require_observed" not in pd.ENTITY_DEFS.get("outside_temperature", {})
print("S600 Thetford + Truma dedicated-class gating  OK")

# --- ML-T compressor fridge (bus 114) gating (v2.69.2) ---
for name in ("fridge_compressor_power", "fridge_compressor_silent",
             "fridge_compressor_door", "fridge_compressor_slot6",
             "fridge_compressor_supply_voltage"):
    assert pd.ENTITY_DEFS[name].get("require_observed") is True, \
        f"{name} not gated"
assert pd.SWITCH_DEFS["114,1"].get("require_observed") is True
assert pd.SWITCH_DEFS["114,2"].get("require_observed") is True
for key in ("fridge_compressor_freezer", "fridge_compressor_cooling_step"):
    assert pd.STEPPED_SELECT_DEFS[key].get("require_observed") is True, \
        f"{key} not gated"
print("ML-T compressor fridge (bus 114) gating  OK")

# --- BMC absorber fridge (bus 32) gating (v2.69.3) ---
for name in ("fridge_absorber_power", "fridge_absorber_power_mode",
             "fridge_absorber_cooling_step", "fridge_absorber_door"):
    assert pd.ENTITY_DEFS[name].get("require_observed") is True, \
        f"{name} not gated"
for key in ("fridge_absorber_cooling_step", "fridge_absorber_power_mode"):
    assert pd.STEPPED_SELECT_DEFS[key].get("require_observed") is True, \
        f"{key} not gated"
print("BMC absorber fridge (bus 32) gating  OK")

# --- Alde heater (bus 5) + TenHaaft satellite (bus 10) gating (v2.69.3) ---
for name in ("alde_inside_temp", "alde_setpoint", "alde_energy_priority",
             "alde_warning", "alde_heating_on", "alde_heating_active",
             "alde_outside_temp", "alde_zone2_temp", "alde_zone2_setpoint",
             "alde_hot_water_mode", "alde_electric_setting", "alde_gas_active",
             "alde_acc_setting", "alde_error",
             "sat_satellite", "sat_status", "sat_signal_strength",
             "sat_dish_moving", "sat_safe_position", "sat_standby"):
    assert pd.ENTITY_DEFS[name].get("require_observed") is True, \
        f"{name} not gated"
for sw in ("5,9", "5,10", "10,1"):
    assert pd.SWITCH_DEFS[sw].get("require_observed") is True, \
        f"switch {sw} not gated"
for num in ("alde_setpoint", "alde_zone2_setpoint"):
    assert pd.NUMBER_DEFS[num].get("require_observed") is True, \
        f"number {num} not gated"
for key in ("alde_energy_priority", "alde_electric_boost",
            "alde_hot_water", "sat_position"):
    assert pd.STEPPED_SELECT_DEFS[key].get("require_observed") is True, \
        f"{key} not gated"
print("Alde heater (bus 5) + TenHaaft satellite (bus 10) gating  OK")

# --- Truma Combi D (bus 57) second gated profile (v2.70.1) ---
assert pd.CLIMATE_DEFS["truma_heater_d"].get("require_observed") is True, \
    "truma_heater_d not gated"
assert pd.CLIMATE_DEFS["truma_heater_d"].get("heater_bus") == 57, \
    "truma_heater_d must be bus 57"
assert pd.CLIMATE_DEFS["truma_heater_d"].get("supports_energy_select") is False, \
    "truma_heater_d (diesel-only) must not expose the energy select"
# The legacy bus-58 profile stays first so its entity IDs never move.
_profile_keys = [k for k, _ in pd.get_truma_heater_defs()]
assert _profile_keys[:2] == ["truma_heater", "truma_heater_d"], \
    f"unexpected Truma profile order: {_profile_keys}"
for name in ("heater_d_fuel_type", "heater_d_water_mode", "heater_d_setpoint",
             "heater_d_operating_mode", "heater_d_panel_busy",
             "heater_d_combi_error", "heater_d_response_error",
             "heater_d_shoreline_connected", "heater_d_window_switch_closed"):
    assert pd.ENTITY_DEFS[name].get("require_observed") is True, \
        f"{name} not gated"
# The bus-57 names must never collide with the bus-58 heater_* names.
assert "heater_d_setpoint" != "heater_setpoint"
assert pd.ENTITY_DEFS["heater_setpoint"].get("require_observed") is True
print("Truma Combi D (bus 57) second gated profile  OK")

print("ALL RUNTIME CHECKS PASSED")
