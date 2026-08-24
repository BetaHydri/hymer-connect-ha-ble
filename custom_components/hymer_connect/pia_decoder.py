"""PIA Protobuf decoder/encoder for HYMER Connect sensor data.

Decodes Base64-encoded Protobuf payloads from SignalR PiaResponse messages.
Encodes PiaRequest subscription messages for sensor data streaming.

Sensor mappings and entity definitions are loaded at runtime from JSON files
in the ``sensor_maps/`` directory.  Call :func:`load_sensor_map` at startup
to populate :data:`SENSOR_MAP` and :data:`ENTITY_DEFS` from ``base.json``
and an optional brand-specific overlay (e.g. ``eriba.json``).
"""

from __future__ import annotations

import base64
import json
import logging
import struct
import time
from pathlib import Path
from typing import Any

_LOGGER = logging.getLogger(__name__)

# Directory containing JSON overlay files
_SENSOR_MAPS_DIR = Path(__file__).parent / "sensor_maps"

# Discovery mode: tracks all sensor value changes (mapped and unmapped)
# and logs them at INFO level. Helps identify what unknown bus/sensor
# slots actually report. Enabled via HA logger config:
#   logger:
#     logs:
#       custom_components.hymer_connect.pia_decoder: info
_discovery_previous: dict[str, Any] = {}

# Cache of CCValue.connectedComponentInstance (field 10, bytes) per bus_id.
# Seeded from EVERY inbound PIA payload (cloud SignalR + BLE) by
# ``_seed_instance_cache_walk`` and ``_parse_sensor_entry``.  Used by the
# command builders below to echo the instance back on setValues writes — the
# SCU silently drops writes that omit it for buses that report one.
# Confirmed v2.62.20 log: bus 99 BMS responses carry instance b"can2".
# v2.62.23: unconditional cloud-path seeding via ``_seed_instance_cache_walk``
# guarantees the cache is primed from the SignalR subscription response
# before the user issues their first BLE write — no extra BLE round-trip.
_BUS_INSTANCE_CACHE: dict[int, bytes] = {}


def get_bus_instance(bus_id: int) -> bytes | None:
    """Return the cached connectedComponentInstance bytes for a bus, or None."""
    return _BUS_INSTANCE_CACHE.get(bus_id)


# Sensor key map: (bus_id, sensor_id) → (name, unit, value_transform)
# value_transform: None=raw, "div10"=divide by 10, "div100"=divide by 100, "div1000"=divide by 1000, "div3600"=seconds to hours
# v2.43.0+: Populated at runtime from sensor_maps/base.json + brand overlay.
SENSOR_MAP: dict[tuple[int, int], tuple[str, str | None, str | None]] = {}

# Discriminator map for sensors that share (bus_id, sensor_id) but differ
# by bus_name (PIA field 10, connectedComponentInstance, e.g. "pin-6"/"pin-7",
# but also "can2"/"lin1"/… on other buses).
# Key: (bus_id, sensor_id, bus_name) → (name, unit, transform).
# Populated from JSON sensor entries that carry an optional "bus_name" field.
# Looked up BEFORE SENSOR_MAP in the decoder so that a (bus, slot) pair which
# would otherwise collide can be split apart by the third discriminator.
SENSOR_MAP_PINNED: dict[tuple[int, int, str], tuple[str, str | None, str | None]] = {}

# Entity metadata loaded from JSON overlays.  Populated by load_sensor_map().
# Key: sensor name (str).  Value: dict with platform, device_class, etc.
# Only entries with a ``"platform"`` field in the JSON appear here.
ENTITY_DEFS: dict[str, dict[str, Any]] = {}

# Optional JSON-defined label maps (per sensor-name) to minimize hardcoded
# decoding logic in Python. Loaded from sensor entry objects via
#   "value_labels": {"RAW": "Readable"}
#   "int_labels": {"0": "Off", "1": "On"}
# Keys in int_labels may be strings in JSON and are converted to int.
JSON_VALUE_LABELS: dict[str, dict[str, str]] = {}
JSON_INT_LABELS: dict[str, dict[int, str]] = {}

# Light definitions loaded from JSON ``"lights"`` section.
# Key: bus_id (int).  Value: dict with name, icon, brightness, color_temp.
LIGHT_DEFS: dict[int, dict[str, Any]] = {}

# Switch definitions loaded from JSON ``"switches"`` section.
# Key: ``"bus_id,sensor_id"`` string.  Value: dict with name, icon, write_type, etc.
SWITCH_DEFS: dict[str, dict[str, Any]] = {}

# Climate/control definitions loaded from JSON ``"climate"`` section.
# Contains bus/slot IDs for heater, fridge, and boiler — used by
# climate.py and select.py to parameterize write commands per brand.
CLIMATE_DEFS: dict[str, dict[str, Any]] = {}

# Generic stepped-switch select definitions (v2.63.0+) loaded from the
# ``"climate"."selects"`` JSON subsection.  Drives appliances exposing a
# small set of integer steps (fridge cooling 1-5, freezer 1-3, fan
# speed 1-3 …) — see :class:`select.HymerSteppedSelect` for the schema
# and ``docs/sensor-map.md`` for the user-facing JSON contract.
STEPPED_SELECT_DEFS: dict[str, dict[str, Any]] = {}

# Generic float/number control definitions (v2.65.5+) loaded from the
# ``"climate"."numbers"`` JSON subsection.  Drives appliances exposing a
# writable numeric slot (e.g. the Alde 3030 target temperature, bus 5 slot
# 3) — see :class:`number.HymerNumber` for the schema.
NUMBER_DEFS: dict[str, dict[str, Any]] = {}

# Track whether overlays have already been loaded (prevents re-loading on
# integration reload, since SENSOR_MAP is module-level and persists).
_overlays_loaded: set[str] = set()

# ---------------------------------------------------------------------------
# Auto-slot assignment (v2.64.0+) — universal multi-device sensor numbering.
#
# Some buses host several physically identical devices that share the same
# (bus_id, sensor_id) slots and are told apart ONLY by their binary
# connectedComponentInstance (PIA field 10, surfaced as a "hex:…" bus_name).
# The canonical example is the four HYMER Smart tyre-pressure sensors on
# bus 70 (slots 1=status, 2=pressure, 3=temperature, 4=battery).  Each owner's
# four sensors carry different, factory-assigned hex ids, so hard-coding them
# in the JSON is not portable.
#
# Instead, a JSON sensor entry may use a *template* discriminator of the form
#   "bus_name": "auto:<group>:{n}"   (preferred, one entry covers all devices)
#   "bus_name": "auto:<group>:1"    (legacy: concrete device #1 as template)
# where <group> ties the four slots of one logical device together (so they
# get the SAME number) and {n}/1 marks the entry as an auto-slot template.  At
# decode time, the decoder observes the distinct hex ids appearing on that bus,
# assigns each one the next free slot number in stable first-seen order, and
# resolves the "auto:…" entries accordingly.  The hex→number mapping is
# persisted to a small JSON file so the numbering survives restarts: tyre
# "sensor 1" stays the same wheel every time.
#
# Owners never edit hex ids.  They simply rename "Tyre sensor 1/2/3/4" in the
# HA UI to the real wheel positions once, and the persisted map keeps it stable.
#
# AUTO_SLOT_GROUPS: bus_id -> { "max": int, "groups": set[str] }
#   Derived from JSON entries whose bus_name matches "auto:<group>:<n>".
#   "max" is the highest <n> explicitly defined in the JSON; it is a soft
#   upper bound for pre-defined entries only.  When AUTO_SLOT_TEMPLATES has a
#   template for the bus, devices beyond "max" are still accepted and their
#   entries are generated on the fly (no hard limit).
AUTO_SLOT_GROUPS: dict[int, dict[str, Any]] = {}

# AUTO_SLOT_TEMPLATES: bus_id -> { sensor_id(int) -> template_dict }
#   Captured from the JSON's auto-slot template entry — either the "{n}" form
#   (name already holds the "{n}" placeholder) or the legacy "…:1" device #1
#   (name is turned into a "{n}" template) — so that any device number on the
#   same bus can have its name + entity metadata generated dynamically.  Each
#   template_dict holds:
#     "name_tmpl": str with "{n}" placeholder (e.g. "hss_gaslevel{n}_percent")
#     "group":     str (e.g. "gas")
#     "unit", "transform", "meta": as parsed from the JSON entry
#   This is what makes the auto-slot mechanism unbounded: the JSON only needs
#   one template entry per (bus, slot); devices #1, #2, #3, … are materialised
#   at runtime by _materialise_auto_slot() the first time they are observed.
AUTO_SLOT_TEMPLATES: dict[int, dict[int, dict[str, Any]]] = {}

# AUTO_SLOT_ASSIGN: bus_id -> { hex_id(str without "hex:") -> slot_number(int) }
#   Runtime + persisted assignment of observed device hex ids to slot numbers.
AUTO_SLOT_ASSIGN: dict[int, dict[str, int]] = {}

# Path of the persisted assignment file (JSON).  Lives next to the sensor maps
# so it travels with the integration's config, not the read-only source tree.
_AUTO_SLOT_STORE = _SENSOR_MAPS_DIR / "_auto_slots.json"


def _load_auto_slot_store() -> None:
    """Load the persisted hex→slot assignments, if any."""
    if not _AUTO_SLOT_STORE.is_file():
        return
    try:
        with open(_AUTO_SLOT_STORE, encoding="utf-8") as f:
            raw = json.load(f)
    except (json.JSONDecodeError, OSError) as exc:
        _LOGGER.warning("Could not read auto-slot store: %s", exc)
        return
    for bus_str, mapping in raw.items():
        try:
            bus_id = int(bus_str)
        except (TypeError, ValueError):
            continue
        if isinstance(mapping, dict):
            AUTO_SLOT_ASSIGN[bus_id] = {
                str(k): int(v) for k, v in mapping.items()
                if isinstance(v, int) or (isinstance(v, str) and v.isdigit())
            }


def _save_auto_slot_store() -> None:
    """Persist the current hex→slot assignments to disk (best-effort)."""
    try:
        serializable = {
            str(bus): dict(sorted(mapping.items(), key=lambda kv: kv[1]))
            for bus, mapping in AUTO_SLOT_ASSIGN.items()
            if mapping
        }
        tmp = _AUTO_SLOT_STORE.with_suffix(".json.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(serializable, f, indent=2)
        tmp.replace(_AUTO_SLOT_STORE)
    except OSError as exc:
        _LOGGER.warning("Could not write auto-slot store: %s", exc)


def _make_auto_name_template(name: str, group: str) -> str:
    """Turn a concrete device-#1 name into a "{n}" template.

    ``hss_gaslevel1_percent`` + group ``gas`` → ``hss_gaslevel{n}_percent``.
    We replace the first ``1`` that appears after the group stem so unrelated
    digits elsewhere are left intact.  Falls back to appending ``{n}`` only if
    no ``1`` is present (shouldn't happen for a well-formed template entry).
    """
    idx = name.find("1")
    if idx == -1:
        return name + "{n}"
    return name[:idx] + "{n}" + name[idx + 1:]


def _materialise_auto_slot(bus_id: int, sensor_id: int, n: int) -> tuple | None:
    """Generate (and register) the mapping for an auto-slot device number N.

    Uses the template captured from device #1 to build the concrete name and
    entity metadata for device N, registering them in SENSOR_MAP_PINNED and
    ENTITY_DEFS so subsequent lookups (and sensor.py's discovery listener)
    treat the new device exactly like a pre-defined one.  Returns the
    ``(name, unit, transform)`` tuple, or ``None`` if no template exists for
    this (bus, slot).
    """
    tmpl_bus = AUTO_SLOT_TEMPLATES.get(bus_id)
    if not tmpl_bus:
        return None
    tmpl = tmpl_bus.get(sensor_id)
    if not tmpl:
        return None
    name = tmpl["name_tmpl"].replace("{n}", str(n))
    unit = tmpl["unit"]
    transform = tmpl["transform"]
    grp = tmpl["group"]
    disc = f"auto:{grp}:{n}"
    SENSOR_MAP_PINNED[(bus_id, sensor_id, disc)] = (name, unit, transform)
    if tmpl["meta"] is not None and name not in ENTITY_DEFS:
        ENTITY_DEFS[name] = dict(tmpl["meta"])
    _LOGGER.info(
        "Auto-slot: materialised %s (bus=%d slot=%d, device #%d) from template",
        name, bus_id, sensor_id, n,
    )
    return (name, unit, transform)


def _assign_auto_slot(bus_id: int, hex_id: str) -> int | None:
    """Return the stable slot number for a device hex id on an auto bus.

    Assigns the next free number in first-seen order the first time a hex id
    is observed, persists the result, and returns it.  Returns ``None`` if the
    bus has no auto-slot group.

    Numbering is unbounded when a template exists for the bus (see
    AUTO_SLOT_TEMPLATES): a brand-new device simply takes the next free number
    and its entries are materialised on demand.  Without a template we keep the
    old behaviour and cap at the JSON-defined "max".
    """
    grp = AUTO_SLOT_GROUPS.get(bus_id)
    if not grp:
        return None
    assigned = AUTO_SLOT_ASSIGN.setdefault(bus_id, {})
    existing = assigned.get(hex_id)
    if existing is not None:
        return existing
    used = set(assigned.values())
    has_template = bool(AUTO_SLOT_TEMPLATES.get(bus_id))
    # With a template the cap is effectively unlimited; without one we keep the
    # JSON's "max" as a hard ceiling so unmapped buses don't sprawl.
    ceiling = 9999 if has_template else grp["max"]
    for n in range(1, ceiling + 1):
        if n not in used:
            assigned[hex_id] = n
            _LOGGER.info(
                "Auto-slot: bus=%d device hex:%s → sensor %d (newly assigned)",
                bus_id, hex_id, n,
            )
            _save_auto_slot_store()
            return n
    _LOGGER.warning(
        "Auto-slot: bus=%d has more devices than slots (max=%d); "
        "hex:%s left unassigned", bus_id, grp["max"], hex_id,
    )
    return None


def _load_json_overlay(filename: str) -> int:
    """Load a single JSON overlay file and merge into SENSOR_MAP / ENTITY_DEFS.

    Supports two value formats per sensor entry:

    * **Array** (v2.42 compat): ``["name", "unit", "transform"]``
    * **Object** (v2.43+): ``{"name": "…", "unit": "…", "platform": "sensor", …}``

    Object entries with a ``"platform"`` key are also stored in
    :data:`ENTITY_DEFS` so that ``sensor.py`` / ``binary_sensor.py`` can
    build HA entity descriptions at runtime.

    Object entries may carry an optional ``"bus_name"`` discriminator.  When
    present, the entry is stored in :data:`SENSOR_MAP_PINNED` keyed by
    ``(bus_id, sensor_id, bus_name)`` instead of :data:`SENSOR_MAP`.  This lets
    two sensors that share the same ``(bus_id, sensor_id)`` be told apart by
    the inbound ``bus_name`` (PIA field 10, e.g. ``"pin-6"`` / ``"pin-7"``).

    The JSON object key for such an entry may carry a ``"#<suffix>"`` so the
    same ``bus_id,sensor_id`` can appear more than once in one JSON object
    (e.g. ``"76,1#pin-6"`` and ``"76,1#pin-7"``).  The suffix is stripped before
    the key is parsed and is otherwise ignored — only the ``"bus_name"`` field
    matters for matching.

    Additionally loads ``"lights"`` and ``"switches"`` sections (v2.44.0+)
    into :data:`LIGHT_DEFS` and :data:`SWITCH_DEFS`.

    Overlay entries **override** existing entries for the same key.

    Returns:
        Number of sensor entries merged (lights/switches not counted).
    """
    path = _SENSOR_MAPS_DIR / filename
    if not path.is_file():
        return 0

    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError) as exc:
        _LOGGER.error("Failed to load sensor map %s: %s", filename, exc)
        return 0

    _ENTITY_FIELDS = (
        "platform", "device_class", "state_class", "icon",
        "on_value", "enabled", "entity_category", "require_observed",
    )

    sensors = data.get("sensors", {})
    count = 0
    for key_str, value in sensors.items():
        # Allow an optional "#<discriminator>" suffix on the JSON key so the
        # same (bus_id, sensor_id) can appear multiple times in one object.
        # The suffix is display-only; the actual discriminator is the "pin"
        # field inside the value object (read below).
        raw_key = key_str.split("#", 1)[0]
        parts = raw_key.split(",")
        if len(parts) != 2:
            continue
        bus_id, sensor_id = int(parts[0].strip()), int(parts[1].strip())

        if isinstance(value, list):
            # v2.42 backward compat: [name, unit, transform]
            name = value[0]
            unit = value[1] if len(value) > 1 else None
            transform = value[2] if len(value) > 2 else None
        elif isinstance(value, dict):
            name = value.get("name", f"bus{bus_id}_s{sensor_id}")
            unit = value.get("unit")
            transform = value.get("transform")
            # Optional JSON-driven decode label maps
            if isinstance(value.get("value_labels"), dict):
                JSON_VALUE_LABELS[name] = {
                    str(k): str(v) for k, v in value["value_labels"].items()
                }
            if isinstance(value.get("int_labels"), dict):
                int_map: dict[int, str] = {}
                for k, v in value["int_labels"].items():
                    try:
                        int_map[int(k)] = str(v)
                    except (TypeError, ValueError):
                        continue
                if int_map:
                    JSON_INT_LABELS[name] = int_map
            # Store entity metadata when a platform is declared.  Names that
            # still carry an unresolved "{n}" auto-slot placeholder are
            # *templates*, not concrete entities — never register them directly
            # (doing so created literal "…{n}…" ghost entities, the v2.64.2
            # regression).  Concrete devices #1..N are materialised later by the
            # decoder via _materialise_auto_slot().
            if "platform" in value and "{n}" not in name:
                meta = {k: value[k] for k in _ENTITY_FIELDS if k in value}
                if unit is not None:
                    meta["unit"] = unit
                ENTITY_DEFS[name] = meta
        else:
            continue

        # Optional bus_name discriminator: route to the pinned map when present
        # so that colliding (bus_id, sensor_id) pairs stay separable. These
        # entries are intentionally NOT placed in SENSOR_MAP so they cannot
        # satisfy the un-discriminated lookup.
        disc = value.get("bus_name") if isinstance(value, dict) else None
        if disc:
            # Auto-slot discriminators come in two forms:
            #   "auto:<group>:{n}"  — ONE template covering devices #1..N
            #                          (preferred; no per-device JSON entries)
            #   "auto:<group>:1"    — legacy concrete device #1 that also serves
            #                          as the template (kept for backward compat)
            # A literal "{n}" placeholder must NEVER be stored in the pinned map
            # (no inbound "hex:…" frame can ever match "auto:tyre:{n}"); it only
            # DEFINES the template.  Every other discriminator (e.g. "pin-6") is
            # an ordinary pinned entry.
            _parts = disc.split(":") if disc.startswith("auto:") else []
            _is_tmpl = len(_parts) == 3 and _parts[2] == "{n}"
            _is_num = len(_parts) == 3 and _parts[2].isdigit()

            if not _is_tmpl:
                SENSOR_MAP_PINNED[(bus_id, sensor_id, disc)] = (name, unit, transform)

            if _is_tmpl or _is_num:
                _grp = _parts[1]
                _n = 1 if _is_tmpl else int(_parts[2])
                bucket = AUTO_SLOT_GROUPS.setdefault(
                    bus_id, {"max": 0, "groups": set()}
                )
                bucket["groups"].add(_grp)
                if _n > bucket["max"]:
                    bucket["max"] = _n
                # Capture a template so any device number can be materialised on
                # the fly.  For the "{n}" form the name already IS the template;
                # for the legacy ":1" form derive it from the concrete device-#1
                # name (e.g. "hss_gaslevel1_percent" → "hss_gaslevel{n}_percent").
                if _is_tmpl or _n == 1:
                    _meta = None
                    if isinstance(value, dict) and "platform" in value:
                        _meta = {
                            k: value[k] for k in _ENTITY_FIELDS if k in value
                        }
                        if unit is not None:
                            _meta["unit"] = unit
                    _name_tmpl = (
                        name if _is_tmpl
                        else _make_auto_name_template(name, _grp)
                    )
                    AUTO_SLOT_TEMPLATES.setdefault(bus_id, {})[sensor_id] = {
                        "name_tmpl": _name_tmpl,
                        "group": _grp,
                        "unit": unit,
                        "transform": transform,
                        "meta": _meta,
                    }
        else:
            SENSOR_MAP[(bus_id, sensor_id)] = (name, unit, transform)
        count += 1

    # --- Lights section (v2.44.0+) ---
    # Keyed by bus_id string. Convention: sid 1=on/off, sid 2=brightness,
    # sid 3=color_temp. The JSON declares which capabilities the light has.
    lights = data.get("lights", {})
    for bus_str, light_def in lights.items():
        if not isinstance(light_def, dict):
            continue
        bus_id = int(bus_str.strip())
        LIGHT_DEFS[bus_id] = light_def
    if lights:
        _LOGGER.debug("Loaded %d light definitions from %s", len(lights), filename)

    # --- Switches section (v2.44.0+) ---
    # Keyed by "bus_id,sensor_id" string. Defines write-command metadata.
    switches = data.get("switches", {})
    for key_str, switch_def in switches.items():
        if not isinstance(switch_def, dict):
            continue
        SWITCH_DEFS[key_str] = switch_def
    if switches:
        _LOGGER.debug("Loaded %d switch definitions from %s", len(switches), filename)

    # --- Climate section (v2.45.0+) ---
    # Defines bus/slot IDs for heater, fridge, and boiler per brand.
    # Later entries override earlier ones (brand overrides base).
    # The reserved subkey ``"selects"`` is consumed separately and feeds
    # the generic stepped-switch driver (v2.63.0+).
    climate = data.get("climate", {})
    for key, climate_def in climate.items():
        if key.startswith("_"):
            continue
        if key == "selects" and isinstance(climate_def, dict):
            for sel_key, sel_def in climate_def.items():
                if sel_key.startswith("_") or not isinstance(sel_def, dict):
                    continue
                STEPPED_SELECT_DEFS[sel_key] = sel_def
            continue
        if key == "numbers" and isinstance(climate_def, dict):
            for num_key, num_def in climate_def.items():
                if num_key.startswith("_") or not isinstance(num_def, dict):
                    continue
                NUMBER_DEFS[num_key] = num_def
            continue
        if isinstance(climate_def, dict):
            CLIMATE_DEFS[key] = climate_def
    if climate:
        _LOGGER.debug("Loaded %d climate definitions from %s",
                       sum(1 for k in climate if not k.startswith("_")), filename)

    return count


def load_sensor_map(brand: str) -> None:
    """Load sensor map overlays for the given brand.

    Loads ``base.json`` (shared across all EHG brands) first, then the
    brand-specific overlay (e.g. ``eriba.json``).  Populates both
    :data:`SENSOR_MAP` (decode layer) and :data:`ENTITY_DEFS` (entity
    metadata for ``sensor.py`` / ``binary_sensor.py``).

    ``base.json`` is required — if missing, an error is logged and no
    sensor mappings will be available.  The brand overlay is optional.

    This function is idempotent: calling it multiple times with the same brand
    is safe and will not re-load files.

    Args:
        brand: The EHG brand key (e.g. ``"hymer"``, ``"eriba"``, ``"buerstner"``).
    """
    cache_key = f"brand:{brand}"
    if cache_key in _overlays_loaded:
        return

    base_count = 0
    brand_count = 0

    if "base" not in _overlays_loaded:
        base_count = _load_json_overlay("base.json")
        _overlays_loaded.add("base")
        if base_count:
            _LOGGER.info("Sensor map: loaded base.json (%d entries)", base_count)
        else:
            _LOGGER.error(
                "Sensor map: base.json missing or empty — no sensor mappings loaded! "
                "Ensure sensor_maps/base.json is present in custom_components/hymer_connect/."
            )

    brand_file = f"{brand}.json"
    brand_count = _load_json_overlay(brand_file)
    if brand_count:
        _LOGGER.info(
            "Sensor map: loaded %s (%d entries)",
            brand_file, brand_count,
        )
    else:
        _LOGGER.debug("Sensor map: no brand overlay for '%s' (using base only)", brand)

    _overlays_loaded.add(cache_key)

    # Load any persisted auto-slot assignments so multi-device buses (e.g. the
    # four tyre sensors on bus 70) keep their stable sensor1..N numbering across
    # restarts.  Safe to call repeatedly; it only reads.
    _load_auto_slot_store()

    _LOGGER.info(
        "Sensor map ready: %d total entries (base=%d, %s=%d, hardcoded=%d), "
        "%d pinned, %d lights, %d switches, %d climate defs, %d stepped selects",
        len(SENSOR_MAP), base_count, brand, brand_count,
        len(SENSOR_MAP) - base_count - brand_count,
        len(SENSOR_MAP_PINNED),
        len(LIGHT_DEFS), len(SWITCH_DEFS), len(CLIMATE_DEFS),
        len(STEPPED_SELECT_DEFS),
    )
    if AUTO_SLOT_GROUPS:
        _LOGGER.info(
            "Auto-slot buses active: %s",
            ", ".join(
                f"bus {b} (max {info['max']}, groups {sorted(info['groups'])})"
                for b, info in sorted(AUTO_SLOT_GROUPS.items())
            ),
        )


# Human-readable mappings for raw SCU string values
_VALUE_LABELS: dict[str, dict[str, str]] = {
    "door_driver": {"OFF": "Closed", "CLS": "Closed", "ON": "Open", "OPN": "Open", "SNA": "N/A"},
    "door_passenger": {"OFF": "Closed", "CLS": "Closed", "ON": "Open", "OPN": "Open", "SNA": "N/A"},
    "wiping_water_empty": {"OFF": "Off", "ON": "On"},
    "motor_oil_warning": {"OFF": "Off", "ON": "On"},
    "ignition_state": {
        "IGN_LOCK": "Off",
        "IGN_OFF": "Accessory",
        "IGN_ACC": "Accessory",
        "IGN_ON": "On",
        "IGN_START": "Starting",
    },
    "lock_status": {
        "Vehicle unlocked": "Unlocked",
        "Vehicle external locked": "Locked",
        "Vehicle internal locked": "Locked (inside)",
    },
    "headlamp": {"OFF": "Off", "ON": "On"},
    "fog_front": {"OFF": "Off", "ON": "On"},
    "fog_rear": {"OFF": "Off", "ON": "On"},
    "high_beam": {"OFF": "Off", "ON": "On"},
    "parking_light": {"OFF": "Off", "ON": "On"},
    "turn_signal": {"OFF": "Off", "ON": "On"},
    # Chassis state flags (bus 1, slots 17-22) — remapped from vehicle lights
    "parking_brake": {"OFF": "Off", "ON": "On"},
    "standheizung_available": {"OFF": "Off", "ON": "On"},
    "standheizung_state": {"OFF": "Off", "ON": "On"},
    "cruise_control_can": {"OFF": "Off", "ON": "On"},
    "downhill_assist": {"OFF": "Off", "ON": "On"},
    "coolant_warning": {"OFF": "Off", "ON": "On"},
    "heater_fan_speed": {"OFF": "Off", "ECO": "Eco", "HOT": "Hot", "HIGH": "High", "VENT": "Vent"},
    "heater_state": {"False": "Off", "True": "On"},
}

# Integer-to-string label maps for sensors that report numeric codes.
_INT_LABELS: dict[str, dict[int, str]] = {
    "dpf_status": {0: "Normal", 1: "Regeneration"},
    "fridge_mode": {0: "On", 1: "Eco", 2: "Boost", 8: "Off"},
    "fridge_status": {0: "Open", 1: "Closed"},  # Operating state labels from SCU, not physical door
    # Thetford N4000 absorber fridge (bus 34, slot 6) — generic error codes.
    # EHG app shows "check the manual, error code: N" for all codes 0-13.
    "fridge_warning": {
        0: "Error 0",
        1: "Error 1",
        2: "Error 2",
        3: "Error 3",
        4: "Error 4",
        5: "Error 5",
        6: "Error 6",
        7: "Error 7",
        8: "Error 8",
        9: "Error 9",
        10: "Error 10",
        11: "Error 11",
        12: "Error 12",
        13: "Error 13",
    },
}

# Sentinel float values that indicate "sensor unavailable / not connected".
# The SCU stores 32768 (0x8000) as CAN "no data" — scaled to float as 3276.8.
_FLOAT_SENTINELS: set[float] = {3276.8, 32768.0, 65535.0, 6553.5}

# Mercedes Sprinter 7G-TRONIC automatic transmission gear mapping.
_GEAR_MAP: dict[int, str] = {
    0: "N",
    1: "1",
    2: "2",
    3: "3",
    4: "4",
    5: "5",
    6: "6",
    7: "7",
    100: "P",
}

# All PiaRequest payloads captured from the Hymer Connect app.
_PIA_REQUESTS = (
    "EhcI/4kTEgd2MC4zMi4wGNr5ws4GIgIKAA==",
    "ErUKCMO2AhIHdjAuMzIuMBja+cLOBiKfChqcCgoKCAEQAVIEY2FuMAoKCAIQAVIEY2FuMAoKCAMQAVIEY2FuMAoKCAQQAVIEY2FuMAoKCAUQAVIEY2FuMAoKCAYQAVIEY2FuMAoKCAcQAVIEY2FuMAoKCAgQAVIEY2FuMAoKCAkQAVIEY2FuMAoKCAoQAVIEY2FuMAoKCAsQAVIEY2FuMAoKCAwQAVIEY2FuMAoKCA0QAVIEY2FuMAoKCA4QAVIEY2FuMAoKCA8QAVIEY2FuMAoKCBAQAVIEY2FuMAoKCBEQAVIEY2FuMAoKCBIQAVIEY2FuMAoKCBMQAVIEY2FuMAoKCBQQAVIEY2FuMAoKCBUQAVIEY2FuMAoKCBYQAVIEY2FuMAoKCBcQAVIEY2FuMAoKCAEQA1IEbGluMQoKCAIQA1IEbGluMQoKCAMQA1IEbGluMQoKCAQQA1IEbGluMQoKCAUQA1IEbGluMQoKCAYQA1IEbGluMQoKCAcQA1IEbGluMQoKCAgQA1IEbGluMQoKCAkQA1IEbGluMQoKCAoQA1IEbGluMQoKCAsQA1IEbGluMQoKCAwQA1IEbGluMQoKCA0QA1IEbGluMQoKCA4QA1IEbGluMQoKCA8QA1IEbGluMQoKCBAQA1IEbGluMQoKCBEQA1IEbGluMQoKCBIQA1IEbGluMQoKCBMQA1IEbGluMQoKCBQQA1IEbGluMQoKCBUQA1IEbGluMQoKCBYQA1IEbGluMQoKCAEQCFIEbGluMgoKCAIQCFIEbGluMgoKCAMQCFIEbGluMgoKCAQQCFIEbGluMgoKCAUQCFIEbGluMgoKCAYQCFIEbGluMgoKCAcQCFIEbGluMgoECAEQCwoECAIQCwoECAEQDAoECAIQDAoECAMQDAoECAEQDwoECAIQDwoECAMQDwoECAEQEAoECAIQEAoECAEQEwoECAIQEwoECAEQFQoECAIQFQoECAEQFgoECAIQFgoECAEQGAoECAIQGAoECAMQGAoECAEQGQoECAIQGQoECAEQGwoECAIQGwoECAMQGwoECAEQHgoECAIQHgoECAMQHgoECAQQHgoECAUQHgoECAYQHgoECAcQHgoECAgQHgoECAkQHgoECAoQHgoECAsQHgoECAwQHgoECA0QHgoECA4QHgoKCAEQIlIEbGluMQoKCAIQIlIEbGluMQoKCAMQIlIEbGluMQoKCAQQIlIEbGluMQoKCAUQIlIEbGluMQoKCAYQIlIEbGluMQoKCAcQIlIEbGluMQoECAEQJQoECAIQJQoECAEQKwoECAIQKwoECAEQLAoECAIQLAoKCAgQLVIEbGluMQoKCAkQLVIEbGluMQoKCAoQLVIEbGluMQoKCAsQLVIEbGluMQoKCAgQMVIEbGluMQoKCAoQMVIEbGluMQoKCAsQMVIEbGluMQoKCAQQOlIEbGluMQoKCAUQOlIEbGluMQoKCAYQOlIEbGluMQoKCAcQOlIEbGluMQoKCAgQOlIEbGluMQoKCAkQOlIEbGluMQoKCAoQOlIEbGluMQoKCAsQOlIEbGluMQoKCAwQOlIEbGluMQoKCA0QOlIEbGluMQoKCA4QOlIEbGluMQoKCAEQY1IEY2FuMgoKCAIQY1IEY2FuMgoKCAMQY1IEY2FuMgoKCAQQY1IEY2FuMgoKCAUQY1IEY2FuMgoKCAYQY1IEY2FuMgoKCAcQY1IEY2FuMgoKCAgQY1IEY2FuMgoKCAkQY1IEY2FuMgoKCAoQY1IEY2FuMg==",
    "EhsIqdQjEgd2MC4zMi4wGNr5ws4GIgZKBAoCCAA=",
    "EhcIn7UFEgd2MC4zMi4wGNv5ws4GKgIaAA==",
    "EhcItPYkEgd2MC4zMi4wGNv5ws4GYgIKAA==",
    "EhcIjI8GEgd2MC4zMi4wGNv5ws4GSgIKAA==",
    "EhUIjekiEgd2MC4zMi4wGNz5ws4GegA=",
)


def build_subscription_requests() -> list[str]:
    """Build PiaRequest payloads for sensor data subscription."""
    return list(_PIA_REQUESTS)


def build_refresh_command() -> str:
    """Build a PiaRequest poll/refresh command to force SCU to re-report all states."""
    import random

    msg_id = random.randint(1, 10_000_000)
    ts = int(time.time())

    wrapper = _encode_varint_field(1, msg_id)
    wrapper += _encode_bytes_field(2, b"v0.32.0")
    wrapper += _encode_varint_field(3, ts)
    wrapper += _encode_bytes_field(9, b"")

    payload = _encode_bytes_field(2, wrapper)
    return base64.b64encode(payload).decode("ascii")


def build_restart_system_request(*, cold: bool = True) -> str:
    """Build a Request.command.restart PIA request to reboot the SCU."""
    import random

    msg_id = random.randint(1, 10_000_000)
    ts = int(time.time())

    restart_cmd = _encode_varint_field(1, 1 if cold else 0)
    command_topic = _encode_bytes_field(2, restart_cmd)

    wrapper = _encode_varint_field(1, msg_id)
    wrapper += _encode_bytes_field(2, b"v0.32.0")
    wrapper += _encode_varint_field(3, ts)
    wrapper += _encode_bytes_field(9, command_topic)

    payload = _encode_bytes_field(2, wrapper)
    return base64.b64encode(payload).decode("ascii")


def _encode_varint(value: int) -> bytes:
    """Encode an integer as a protobuf varint."""
    result = bytearray()
    while value > 0x7F:
        result.append((value & 0x7F) | 0x80)
        value >>= 7
    result.append(value & 0x7F)
    return bytes(result)


def _encode_field(field_number: int, wire_type: int, data: bytes) -> bytes:
    """Encode a protobuf field with tag and data."""
    tag = _encode_varint((field_number << 3) | wire_type)
    return tag + data


def _encode_varint_field(field_number: int, value: int) -> bytes:
    """Encode a varint field."""
    return _encode_field(field_number, 0, _encode_varint(value))


def _encode_bytes_field(field_number: int, data: bytes) -> bytes:
    """Encode a length-delimited field."""
    return _encode_field(field_number, 2, _encode_varint(len(data)) + data)


def _encode_str_field(field_number: int, value: str) -> bytes:
    """Encode a string as a length-delimited field."""
    data = value.encode("utf-8")
    return _encode_bytes_field(field_number, data)


def _encode_float_field(field_number: int, value: float) -> bytes:
    """Encode a 32-bit float field (wire type 5)."""
    return _encode_field(field_number, 5, struct.pack("<f", value))


def build_light_command(
    bus_id: int,
    sensor_id: int,
    *,
    bool_value: bool | None = None,
    uint_value: int | None = None,
    str_value: str | None = None,
) -> str:
    """Build a PiaRequest payload to control a light or switch."""
    sensor_data = _encode_varint_field(1, sensor_id)
    sensor_data += _encode_varint_field(2, bus_id)
    if str_value is not None:
        sensor_data += _encode_str_field(4, str_value)
    elif bool_value is not None:
        sensor_data += _encode_varint_field(5, 1 if bool_value else 0)
    elif uint_value is not None:
        sensor_data += _encode_varint_field(3, uint_value)
    instance = _BUS_INSTANCE_CACHE.get(bus_id)
    if instance is not None:
        sensor_data += _encode_bytes_field(10, instance)
        _LOGGER.debug(
            "build_light_command bus=%d sid=%d: instance=%r",
            bus_id, sensor_id, instance,
        )
    else:
        _LOGGER.debug(
            "build_light_command bus=%d sid=%d: no cached instance (cloud accepts both)",
            bus_id, sensor_id,
        )

    sub2 = _encode_bytes_field(1, sensor_data)
    inner = _encode_bytes_field(2, sub2)

    import random

    msg_id = random.randint(1, 10_000_000)
    version_bytes = b"v0.32.0"
    ts = int(time.time())

    wrapper = _encode_varint_field(1, msg_id)
    wrapper += _encode_bytes_field(2, version_bytes)
    wrapper += _encode_varint_field(3, ts)
    wrapper += _encode_bytes_field(4, inner)

    payload = _encode_bytes_field(2, wrapper)

    return base64.b64encode(payload).decode("ascii")


def build_multi_sensor_command(
    sensors: list[dict],
) -> str:
    """Build a PiaRequest payload with multiple sensor entries."""
    import random

    entries = b""
    for s in sensors:
        bus_id = s["bus_id"]
        sensor_id = s["sensor_id"]
        sensor_data = _encode_varint_field(1, sensor_id)
        sensor_data += _encode_varint_field(2, bus_id)
        if "bool_value" in s:
            sensor_data += _encode_varint_field(5, 1 if s["bool_value"] else 0)
        elif "uint_value" in s:
            sensor_data += _encode_varint_field(3, s["uint_value"])
        elif "str_value" in s:
            sensor_data += _encode_str_field(4, s["str_value"])
        elif "float_value" in s:
            sensor_data += _encode_float_field(6, s["float_value"])
        instance = _BUS_INSTANCE_CACHE.get(bus_id)
        if instance is not None:
            sensor_data += _encode_bytes_field(10, instance)
            _LOGGER.debug(
                "build_multi_sensor_command bus=%d sid=%d: instance=%r",
                bus_id, sensor_id, instance,
            )
        else:
            _LOGGER.debug(
                "build_multi_sensor_command bus=%d sid=%d: no cached instance (cloud accepts both)",
                bus_id, sensor_id,
            )
        entries += _encode_bytes_field(1, sensor_data)

    inner = _encode_bytes_field(2, entries)

    msg_id = random.randint(1, 10_000_000)
    ts = int(time.time())

    wrapper = _encode_varint_field(1, msg_id)
    wrapper += _encode_bytes_field(2, b"v0.32.0")
    wrapper += _encode_varint_field(3, ts)
    wrapper += _encode_bytes_field(4, inner)

    payload = _encode_bytes_field(2, wrapper)
    return base64.b64encode(payload).decode("ascii")


def _decode_varint(data: bytes, pos: int) -> tuple[int, int]:
    """Decode a varint, return (value, new_pos)."""
    result = 0
    shift = 0
    while pos < len(data):
        b = data[pos]
        result |= (b & 0x7F) << shift
        pos += 1
        if not (b & 0x80):
            break
        shift += 7
    return result, pos


def _decode_protobuf(data: bytes) -> list[tuple[int, int, Any]]:
    """Decode raw protobuf into (field_number, wire_type, value) tuples."""
    fields: list[tuple[int, int, Any]] = []
    pos = 0
    while pos < len(data):
        try:
            tag, pos = _decode_varint(data, pos)
        except (IndexError, ValueError):
            break
        field_number = tag >> 3
        wire_type = tag & 0x07
        if wire_type == 0:
            value, pos = _decode_varint(data, pos)
            fields.append((field_number, 0, value))
        elif wire_type == 1:
            if pos + 8 > len(data):
                break
            value = struct.unpack_from("<d", data, pos)[0]
            pos += 8
            fields.append((field_number, 1, value))
        elif wire_type == 5:
            if pos + 4 > len(data):
                break
            value = struct.unpack_from("<f", data, pos)[0]
            pos += 4
            fields.append((field_number, 5, round(value, 2)))
        elif wire_type == 2:
            length, pos = _decode_varint(data, pos)
            if pos + length > len(data):
                break
            value = data[pos : pos + length]
            pos += length
            fields.append((field_number, 2, value))
        else:
            break
    return fields


def _try_string(data: bytes) -> str | None:
    """Try decoding bytes as UTF-8 printable string."""
    try:
        text = data.decode("utf-8")
        if text and all(c.isprintable() or c in "\r\n\t" for c in text):
            return text
    except (UnicodeDecodeError, ValueError):
        pass
    return None


def _parse_sensor_entry(data: bytes) -> dict[str, Any] | None:
    """Parse a single sensor entry from protobuf bytes."""
    fields = _decode_protobuf(data)
    sensor_id = 0
    bus_id = 0
    bus_name = ""
    instance_bytes: bytes | None = None
    values: dict[int, Any] = {}

    if _LOGGER.isEnabledFor(logging.DEBUG):
        try:
            _raw_sid = next((v for fn, wt, v in fields if fn == 1 and wt == 0), None)
            _raw_bus = next((v for fn, wt, v in fields if fn == 2 and wt == 0), None)
            _parts = []
            for _fn, _wt, _v in fields:
                if isinstance(_v, (bytes, bytearray)):
                    _hexs = bytes(_v).hex()
                    _txt = _try_string(bytes(_v))
                    if _txt is not None:
                        _vrepr = f'hex:{_hexs} ("{_txt}")'
                    else:
                        _vrepr = f"hex:{_hexs}"
                else:
                    _vrepr = repr(_v)
                _parts.append(f"f{_fn}/wt{_wt}={_vrepr}")
            _LOGGER.debug(
                "RAW PIA bus=%s | sid=%s | %s",
                _raw_bus, _raw_sid, " | ".join(_parts),
            )
        except Exception:
            pass

    for fn, wt, v in fields:
        if fn == 1 and wt == 0:
            sensor_id = v
        elif fn == 2 and wt == 0:
            bus_id = v
        elif fn == 3 and wt == 0:
            values[3] = v
        elif fn == 4 and wt == 2:
            s = _try_string(v)
            if s is not None:
                values[4] = s
        elif fn == 5 and wt == 0:
            values[5] = bool(v)
        elif fn == 6 and wt == 5:
            values[6] = v
        elif fn == 7 and wt == 0:
            values[7] = v
        elif fn == 10 and wt == 2:
            instance_bytes = v
            s = _try_string(v)
            if s:
                bus_name = s
            elif v:
                bus_name = "hex:" + bytes(v).hex()

    if bus_id and instance_bytes is not None:
        prev = _BUS_INSTANCE_CACHE.get(bus_id)
        if prev != instance_bytes:
            _BUS_INSTANCE_CACHE[bus_id] = instance_bytes
            _LOGGER.debug(
                "Instance cache: bus=%d instance=%r (was %r)",
                bus_id, instance_bytes, prev,
            )

    value: Any = None
    for candidate_field in (4, 6, 3, 7, 5):
        if candidate_field in values:
            value = values[candidate_field]
            break

    if not sensor_id and value is None:
        return None

    return {
        "sensor_id": sensor_id,
        "bus_id": bus_id,
        "bus_name": bus_name,
        "value": value,
    }


def decode_pia_payload(b64_payload: str) -> dict[str, Any]:
    """Decode a PiaResponse Base64 payload into named sensor values."""
    try:
        raw = base64.b64decode(b64_payload)
    except Exception:
        _LOGGER.warning("Failed to base64-decode PIA payload")
        return {}

    try:
        _seed_instance_cache_walk(raw, depth=0)
    except Exception:
        _LOGGER.debug("Instance-cache seeding failed", exc_info=True)

    sensors: dict[str, Any] = {}
    top_fields = _decode_protobuf(raw)

    for fn, wt, v in top_fields:
        if wt != 2 or not isinstance(v, bytes):
            continue
        _extract_sensors_recursive(v, sensors, depth=0)

    return sensors


def _seed_instance_cache_walk(data: bytes, depth: int) -> None:
    """Walk protobuf bytes at all depths, seeding _BUS_INSTANCE_CACHE."""
    if depth > 8:
        return
    try:
        fields = _decode_protobuf(data)
    except Exception:
        return

    sid = None
    bus = None
    instance: bytes | None = None
    for fn, wt, v in fields:
        if fn == 1 and wt == 0:
            sid = v
        elif fn == 2 and wt == 0:
            bus = v
        elif fn == 10 and wt == 2 and isinstance(v, (bytes, bytearray)):
            instance = bytes(v)

    if (
        isinstance(sid, int) and isinstance(bus, int) and instance is not None
        and 0 < bus < 1000 and sid < 1000 and 0 < len(instance) <= 32
    ):
        prev = _BUS_INSTANCE_CACHE.get(bus)
        if prev != instance:
            _BUS_INSTANCE_CACHE[bus] = instance
            _LOGGER.debug(
                "Instance cache seeded (cloud/BLE walk): bus=%d sid=%d "
                "instance=%r (was %r)",
                bus, sid, instance, prev,
            )

    for fn, wt, v in fields:
        if wt == 2 and isinstance(v, (bytes, bytearray)) and len(v) > 2:
            _seed_instance_cache_walk(bytes(v), depth + 1)


def _extract_sensors_recursive(
    data: bytes, sensors: dict[str, Any], depth: int
) -> None:
    """Recursively search for sensor entries in nested protobuf."""
    if depth > 5:
        return

    fields = _decode_protobuf(data)

    has_sid = any(fn == 1 and wt == 0 for fn, wt, _ in fields)
    has_bus = any(fn == 2 and wt == 0 for fn, wt, _ in fields)
    has_value = any(
        (fn in (3, 4, 5, 6, 7) and wt in (0, 2, 5))
        for fn, wt, _ in fields
    )

    if has_sid and has_bus and has_value:
        sid_val = next((v for fn, wt, v in fields if fn == 1 and wt == 0), 0)
        bus_val = next((v for fn, wt, v in fields if fn == 2 and wt == 0), 0)

        is_known = (bus_val, sid_val) in SENSOR_MAP or any(
            k[0] == bus_val and k[1] == sid_val for k in SENSOR_MAP_PINNED
        ) or bus_val in AUTO_SLOT_GROUPS
        if sid_val < 1000 and bus_val < 1000 and (depth <= 3 or (depth == 4 and is_known)):
            entry = _parse_sensor_entry(data)
            if entry and entry["value"] is not None:
                disc = entry.get("bus_name") or ""
                if disc.startswith("hex:") and entry["bus_id"] in AUTO_SLOT_GROUPS:
                    _hex = disc[4:]
                    _slot = _assign_auto_slot(entry["bus_id"], _hex)
                    if _slot is not None:
                        _grp_info = AUTO_SLOT_GROUPS[entry["bus_id"]]
                        _grp = sorted(_grp_info["groups"])[0]
                        disc = f"auto:{_grp}:{_slot}"
                mapped = SENSOR_MAP_PINNED.get(
                    (entry["bus_id"], entry["sensor_id"], disc)
                )
                if mapped is None and disc.startswith("auto:"):
                    _n_part = disc.rsplit(":", 1)[-1]
                    if _n_part.isdigit():
                        mapped = _materialise_auto_slot(
                            entry["bus_id"], entry["sensor_id"], int(_n_part)
                        )
                if mapped is None:
                    mapped = SENSOR_MAP.get((entry["bus_id"], entry["sensor_id"]))
                if mapped:
                    name, unit, transform = mapped
                    val = entry["value"]
                    if isinstance(val, (int, float)) and val in _FLOAT_SENTINELS:
                        return
                    if transform == "div10" and isinstance(val, (int, float)):
                        val = val / 10
                    elif transform == "div100" and isinstance(val, (int, float)):
                        val = val / 100
                    elif transform == "div1000" and isinstance(val, (int, float)):
                        val = val / 1000
                    elif transform == "div3600" and isinstance(val, (int, float)):
                        val = round(val / 3600, 1)
                    elif transform == "invert100" and isinstance(val, (int, float)):
                        val = 100 - val
                    if isinstance(val, str):
                        if name in JSON_VALUE_LABELS:
                            val = JSON_VALUE_LABELS[name].get(val, val)
                        elif name in _VALUE_LABELS:
                            val = _VALUE_LABELS[name].get(val, val)
                    if isinstance(val, int):
                        if name in JSON_INT_LABELS:
                            val = JSON_INT_LABELS[name].get(val, val)
                        elif name in _INT_LABELS:
                            val = _INT_LABELS[name].get(val, val)
                    if name == "current_gear" and isinstance(val, int):
                        val = _GEAR_MAP.get(val, str(val))
                    sensors[name] = val
                    prev = _discovery_previous.get(name)
                    if prev != val:
                        _discovery_previous[name] = val
                        _LOGGER.debug(
                            "DISCOVERY mapped (%d,%d) %s: %r → %r",
                            entry["bus_id"], entry["sensor_id"],
                            name, prev, val,
                        )
                        if name in ("fridge_status", "heater_diesel_safety", "main_switch", "scu_connected"):
                            _LOGGER.info(
                                "State change (%d,%d) %s: %r → %r (depth=%d)",
                                entry["bus_id"], entry["sensor_id"],
                                name, prev, val, depth,
                            )
                else:
                    fallback = f"bus{entry['bus_id']}_s{entry['sensor_id']}"
                    sensors[fallback] = entry["value"]
                    prev = _discovery_previous.get(fallback)
                    if prev != entry["value"]:
                        _discovery_previous[fallback] = entry["value"]
                        _LOGGER.info(
                            "DISCOVERY unmapped (%d,%d) %s: %r → %r",
                            entry["bus_id"], entry["sensor_id"],
                            fallback, prev, entry["value"],
                        )
            return

    for fn, wt, v in fields:
        if wt == 2 and isinstance(v, bytes) and len(v) > 2:
            _extract_sensors_recursive(v, sensors, depth + 1)
