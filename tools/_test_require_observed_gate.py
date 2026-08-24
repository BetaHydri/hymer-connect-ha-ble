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

    # Thetford absorber select (HYMER-only overlay) must NOT be gated
    if "fridge_absorber_cooling_step" in pd.STEPPED_SELECT_DEFS:
        assert pd.STEPPED_SELECT_DEFS["fridge_absorber_cooling_step"].get(
            "require_observed") is not True
    print(f"Runtime gate flags survive loader for brand '{brand}'  OK")

print("ALL RUNTIME CHECKS PASSED")
