"""Offline guard for the opt-in ``cloud_on_demand`` suppression gate.

Proves the two things that must never regress:

  1. The gate NEVER suppresses the cloud on a cloud-only enrollment — a config
     with ``ble_enabled`` false can never make ``_cloud_on_demand_active`` true,
     regardless of the option or any BLE state.
  2. On a BLE-capable install the gate only fires when ALL of: the option is on;
     BLE is enabled AND connected AND not degraded; and the link has held healthy
     for the stability grace window.

Rather than re-implement the predicate (which would drift from the code), this
extracts the REAL ``cloud_on_demand`` property and ``_cloud_on_demand_active``
method bodies from coordinator.py via AST and runs them against a lightweight
fake coordinator — no Home Assistant import required.

Run:  python tools/_test_cloud_on_demand.py
Exit code 0 = pass, 1 = fail.
"""

from __future__ import annotations

import ast
import importlib.util
import sys
import textwrap
import types
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_CC = _ROOT / "custom_components" / "hymer_connect"

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

_failures: list[str] = []


def check(cond: bool, msg: str) -> None:
    if cond:
        print(f"  PASS: {msg}")
    else:
        print(f"  FAIL: {msg}")
        _failures.append(msg)


# --- Load the real const constants (no HA dependency) ------------------------
def _load_const() -> types.ModuleType:
    spec = importlib.util.spec_from_file_location("hc_const", _CC / "const.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["hc_const"] = mod
    spec.loader.exec_module(mod)
    return mod


const = _load_const()

check(hasattr(const, "CONF_CLOUD_ON_DEMAND"), "const defines CONF_CLOUD_ON_DEMAND")
check(
    getattr(const, "DEFAULT_CLOUD_ON_DEMAND", None) is False,
    "DEFAULT_CLOUD_ON_DEMAND is False (opt-in off by default)",
)
check(
    isinstance(getattr(const, "CLOUD_ON_DEMAND_BLE_STABLE_SECONDS", None), (int, float)),
    "CLOUD_ON_DEMAND_BLE_STABLE_SECONDS is numeric",
)
GRACE = float(getattr(const, "CLOUD_ON_DEMAND_BLE_STABLE_SECONDS", 120))


# --- Extract the two functions from coordinator.py via AST -------------------
_src = (_CC / "coordinator.py").read_text(encoding="utf-8")
_tree = ast.parse(_src)
_funcs: dict[str, str] = {}
for node in ast.walk(_tree):
    if isinstance(node, ast.FunctionDef) and node.name in (
        "cloud_on_demand",
        "_cloud_on_demand_active",
    ):
        seg = ast.get_source_segment(_src, node)
        assert seg is not None
        # Strip the @property decorator line if present; we bind manually.
        body = textwrap.dedent(seg)
        body = "\n".join(
            ln for ln in body.splitlines() if not ln.strip().startswith("@property")
        )
        _funcs[node.name] = body

check("cloud_on_demand" in _funcs, "found cloud_on_demand in coordinator.py")
check("_cloud_on_demand_active" in _funcs, "found _cloud_on_demand_active in coordinator.py")

# Compile them in a namespace carrying exactly the globals they reference.
_fake_time = types.SimpleNamespace(monotonic=lambda: 1_000_000.0)
_ns: dict[str, object] = {
    "time": _fake_time,
    "CONF_CLOUD_ON_DEMAND": const.CONF_CLOUD_ON_DEMAND,
    "DEFAULT_CLOUD_ON_DEMAND": const.DEFAULT_CLOUD_ON_DEMAND,
    "CLOUD_ON_DEMAND_BLE_STABLE_SECONDS": const.CLOUD_ON_DEMAND_BLE_STABLE_SECONDS,
}
exec(_funcs["cloud_on_demand"], _ns)  # noqa: S102 — trusted repo source
exec(_funcs["_cloud_on_demand_active"], _ns)  # noqa: S102
_cloud_on_demand_fn = _ns["cloud_on_demand"]
_active_fn = _ns["_cloud_on_demand_active"]


class _Entry:
    def __init__(self, options: dict, data: dict) -> None:
        self.options = options
        self.data = data


class _Fake:
    """Minimal stand-in exposing just what the two functions read."""

    def __init__(
        self,
        *,
        option_on: bool,
        ble_enabled: bool,
        ble_connected: bool,
        degraded: bool,
        healthy_age: float,
    ) -> None:
        self.config_entry = _Entry({const.CONF_CLOUD_ON_DEMAND: option_on}, {})
        self._ble_enabled = ble_enabled
        self._ble_connected = ble_connected
        self._ble_write_degraded = degraded
        self._ble_healthy_since = (
            _fake_time.monotonic() - healthy_age if healthy_age >= 0 else 0.0
        )

    # Real property body is bound below; ble_enabled is a simple stand-in here.
    @property
    def ble_enabled(self) -> bool:
        return self._ble_enabled

    cloud_on_demand = property(_cloud_on_demand_fn)  # type: ignore[arg-type]
    _cloud_on_demand_active = _active_fn  # type: ignore[assignment]


def active(**kw) -> bool:
    return _Fake(**kw).__class__._cloud_on_demand_active(_Fake(**kw))


print("\n[1] cloud-only safety — ble_enabled=False can never suppress cloud")
check(
    active(option_on=True, ble_enabled=False, ble_connected=False, degraded=False, healthy_age=9999)
    is False,
    "cloud-only (option ON, no BLE) → gate INACTIVE (cloud stays always-on)",
)
check(
    active(option_on=True, ble_enabled=False, ble_connected=True, degraded=False, healthy_age=9999)
    is False,
    "cloud-only with stray ble_connected flag → still INACTIVE",
)

print("\n[2] opt-in off — never suppress even on a perfect BLE link")
check(
    active(option_on=False, ble_enabled=True, ble_connected=True, degraded=False, healthy_age=9999)
    is False,
    "option OFF → gate INACTIVE",
)

print("\n[3] dual-path happy path")
check(
    active(option_on=True, ble_enabled=True, ble_connected=True, degraded=False, healthy_age=GRACE + 5)
    is True,
    "option ON + BLE enabled/connected/healthy past grace → gate ACTIVE",
)

print("\n[4] negative gates on a BLE install")
check(
    active(option_on=True, ble_enabled=True, ble_connected=False, degraded=False, healthy_age=GRACE + 5)
    is False,
    "BLE not connected → INACTIVE",
)
check(
    active(option_on=True, ble_enabled=True, ble_connected=True, degraded=True, healthy_age=GRACE + 5)
    is False,
    "BLE degraded (write channel dead) → INACTIVE (cloud kept as fallback)",
)
check(
    active(option_on=True, ble_enabled=True, ble_connected=True, degraded=False, healthy_age=GRACE - 10)
    is False,
    "BLE healthy but within stability grace window → INACTIVE",
)
check(
    active(option_on=True, ble_enabled=True, ble_connected=True, degraded=False, healthy_age=-1)
    is False,
    "healthy_since unset (0) → INACTIVE",
)

print("\n[5] cloud_on_demand property reads option (default off)")
_off = _Fake(option_on=False, ble_enabled=True, ble_connected=True, degraded=False, healthy_age=0)
_on = _Fake(option_on=True, ble_enabled=True, ble_connected=True, degraded=False, healthy_age=0)
check(_Fake.cloud_on_demand.fget(_off) is False, "property False when option off")  # type: ignore[attr-defined]
check(_Fake.cloud_on_demand.fget(_on) is True, "property True when option on")  # type: ignore[attr-defined]

if _failures:
    print(f"\n{len(_failures)} FAILURE(S)")
    sys.exit(1)
print("\nAll cloud_on_demand gate checks passed.")
