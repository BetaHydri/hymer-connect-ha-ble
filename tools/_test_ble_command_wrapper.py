"""Offline regression test for the BLE command wrapper.

A setValues command sent over BLE must be wrapped as BleProtocol.request
(field 1). pia_decoder wraps command payloads in field 2 (the cloud DataHub
envelope); over BLE field 2 is BleProtocol.response, so a field-2-wrapped
command is parsed by the SCU as a response and silently discarded. This pins
the fix in ble_client.send_pia_command (via _unwrap_cloud_envelope).

ble_client imports bleak lazily and no Home Assistant modules at import time,
so it loads here without stubbing.

Run:  python tools/_test_ble_command_wrapper.py
Exit code 0 = pass, 1 = fail.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_BLE = _ROOT / "custom_components" / "hymer_connect" / "ble_client.py"

_spec = importlib.util.spec_from_file_location("_ble_client_under_test", _BLE)
_ble = importlib.util.module_from_spec(_spec)
# Register before exec so @dataclass (with `from __future__ import annotations`)
# can resolve the module during class processing.
sys.modules[_spec.name] = _ble
_spec.loader.exec_module(_ble)

_failures: list[str] = []


def check(cond: bool, msg: str) -> None:
    print(("PASS" if cond else "FAIL"), "-", msg)
    if not cond:
        _failures.append(msg)


# A synthetic bare Request (its content is irrelevant to the wrapping).
bare = _ble._encode_bytes_field(1, b"\x08\x2a")
# The cloud encoder wraps the Request in field 2 (tag 0x12) -- the DataHub envelope.
cloud = _ble._encode_bytes_field(2, bare)

check(cloud[0] == 0x12, "cloud command payload is field-2 wrapped (tag 0x12)")
check(_ble._unwrap_cloud_envelope(cloud) == bare,
      "_unwrap_cloud_envelope recovers the bare Request from the field-2 envelope")

# An already-bare payload (starts with field 1, tag 0x0a) must pass through unchanged.
already_bare = b"\x0a\x02\x08\x2a"
check(_ble._unwrap_cloud_envelope(already_bare) == already_bare,
      "already-bare payload passes through unchanged")

# The re-wrap: BleProtocol.request (field 1, tag 0x0a), never response (field 2).
request_msg = _ble._unwrap_cloud_envelope(cloud)
ble_protocol = _ble._encode_bytes_field(_ble._BLE_PROTOCOL_REQUEST_FIELD, request_msg)
check(ble_protocol[0] == 0x0a, "command wraps as BleProtocol.request (field 1, tag 0x0a)")
check(ble_protocol[0] != 0x12, "command is NOT wrapped as BleProtocol.response (field 2)")
check(ble_protocol == _ble._encode_bytes_field(1, bare),
      "the field-1 wrapper contains exactly the original Request")

if _failures:
    print(f"\n{len(_failures)} check(s) FAILED")
    sys.exit(1)
print("\nALL PASS")
sys.exit(0)
