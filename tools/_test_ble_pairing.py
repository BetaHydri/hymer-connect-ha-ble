"""Local-only BLE pairing unit tests (NOT committed — dev harness).

Loads custom_components/hymer_connect/ble_client.py standalone (bleak is
try/except-guarded and there are no top-level homeassistant imports, so the
module imports on a plain Windows/dev Python) and exercises the pure,
hardware-independent pairing/protocol logic:

  1. PairMobileRequest is wrapped in BleProtocol.request (field 1) — the core
     "BLE writes silently dropped" fix. If this regresses, pairing AND writes
     break on the vehicle.
  2. decode_pair_mobile_response round-trips a synthetic SCU response (tokens,
     status, confirmation_required, request_id).
  3. _rewrap_cloud_payload_as_ble_request moves a cloud field-2 Request into
     field 1 and recovers the request_id; raises when there is no field-2.
  4. _extract_response_id_status parses a response and returns (None, None) for
     a non-response frame.
  5. encode/decode_ble_pia_frame round-trip (magic + length + CRC).
  6. SPEC MIRROR of the D-Bus pairing agent's device-lock + legacy PIN/passkey
     decision (v2.76.3). This mirrors the closure in
     ble_client._pair_via_bluetoothctl, which is not importable; KEEP IN SYNC
     if that logic changes.

Run:  python tools/_test_ble_pairing.py
Exit: 0 = all pass, 1 = at least one failure.
"""

from __future__ import annotations

import base64
import importlib.util
import json
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_BLE_CLIENT = os.path.join(
    _HERE, "..", "custom_components", "hymer_connect", "ble_client.py"
)


def _load_ble_client():
    spec = importlib.util.spec_from_file_location("ble_client_standalone", _BLE_CLIENT)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    # Register before exec so @dataclass type resolution (sys.modules lookup) works.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


_FAILURES: list[str] = []


def check(name: str, condition: bool) -> None:
    status = "PASS" if condition else "FAIL"
    print(f"  [{status}] {name}")
    if not condition:
        _FAILURES.append(name)


def _make_jwt(ett: str = "access-refresh") -> str:
    """A JWT-shaped string that passes ble_client._validate_refresh_token."""
    def b64(obj: dict) -> str:
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).rstrip(b"=").decode()

    header = b64({"alg": "none", "typ": "JWT"})
    payload = b64({"ett": ett, "urn": "urn:test:vehicle"})
    signature = "A" * 43  # arbitrary; keeps the token comfortably over 50 chars
    return f"{header}.{payload}.{signature}"


def test_pair_request_uses_field1(m) -> None:
    print("1) PairMobileRequest wraps in BleProtocol.request (field 1)")
    frame = m.build_pair_mobile_frame("activation-tok", "confirmation-tok", "ha-test")
    payload = m.decode_ble_pia_frame(frame)
    key, _ = m._decode_varint(payload, 0)
    check("outer field == BleProtocol.request (1)", (key >> 3) == m._BLE_PROTOCOL_REQUEST_FIELD)
    check("outer field is NOT response (2)", (key >> 3) != m._BLE_PROTOCOL_RESPONSE_FIELD)


def test_pair_response_roundtrip(m) -> None:
    print("2) decode_pair_mobile_response round-trip")
    refresh = _make_jwt()
    access = _make_jwt()
    mobile_pair = (
        m._encode_string_field(m._PAIR_RESP_ACCESS_TOKEN_FIELD, access)
        + m._encode_string_field(m._PAIR_RESP_REFRESH_TOKEN_FIELD, refresh)
        + m._encode_bool_field(m._PAIR_RESP_CONFIRMATION_REQUIRED_FIELD, False)
    )
    response_msg = (
        m._encode_varint_field(m._RESPONSE_ID_FIELD, 12345)
        + m._encode_varint_field(m._RESPONSE_STATUS_FIELD, 1)
        + m._encode_bytes_field(m._RESPONSE_MOBILE_PAIR_FIELD, mobile_pair)
    )
    ble = m._encode_bytes_field(m._BLE_PROTOCOL_RESPONSE_FIELD, response_msg)
    frame = m.encode_ble_pia_frame(ble)
    resp = m.decode_pair_mobile_response(frame)
    check("refresh token recovered", resp.remote_access_refresh_token == refresh)
    check("access token recovered", resp.remote_access_token == access)
    check("status == 1", resp.status == 1)
    check("request_id == 12345", resp.request_id == 12345)
    check("confirmation_required is False", resp.confirmation_required is False)


def test_rewrap_cloud_payload(m) -> None:
    print("3) _rewrap_cloud_payload_as_ble_request field-2 -> field-1")
    request_msg = (
        m._encode_varint_field(m._REQUEST_ID_FIELD, 777)
        + m._encode_string_field(m._REQUEST_VERSION_FIELD, m.APP_PIA_VERSION)
    )
    # Cloud encoder wraps the Request in top-level field 2.
    cloud_payload = m._encode_bytes_field(m._BLE_PROTOCOL_RESPONSE_FIELD, request_msg)
    ble_payload, request_id = m._rewrap_cloud_payload_as_ble_request(cloud_payload)
    key, _ = m._decode_varint(ble_payload, 0)
    check("rewrapped into field-1 request", (key >> 3) == m._BLE_PROTOCOL_REQUEST_FIELD)
    check("request_id extracted (777)", request_id == 777)

    # A payload without a field-2 Request must raise.
    only_field1 = m._encode_bytes_field(m._BLE_PROTOCOL_REQUEST_FIELD, request_msg)
    try:
        m._rewrap_cloud_payload_as_ble_request(only_field1)
        check("raises BleTransportError when no field-2", False)
    except m.BleTransportError:
        check("raises BleTransportError when no field-2", True)


def test_extract_response_id_status(m) -> None:
    print("4) _extract_response_id_status")
    resp_inner = (
        m._encode_varint_field(m._RESPONSE_ID_FIELD, 42)
        + m._encode_varint_field(m._RESPONSE_STATUS_FIELD, 5)
    )
    resp_payload = m._encode_bytes_field(m._BLE_PROTOCOL_RESPONSE_FIELD, resp_inner)
    rid, status = m._extract_response_id_status(resp_payload)
    check("request_id == 42", rid == 42)
    check("status == 5 (ACCESS_DENIED surfaces, caller must reject)", status == 5)

    # A field-1 (request) frame is not a response -> (None, None).
    req_payload = m._encode_bytes_field(m._BLE_PROTOCOL_REQUEST_FIELD, resp_inner)
    rid2, status2 = m._extract_response_id_status(req_payload)
    check("(None, None) for non-response frame", rid2 is None and status2 is None)


def test_frame_roundtrip(m) -> None:
    print("5) encode/decode_ble_pia_frame round-trip")
    data = b"hello-scu-\x00\xa0\xcb-embedded-magic-\xff"
    check("payload survives framing", m.decode_ble_pia_frame(m.encode_ble_pia_frame(data)) == data)


def _decide_agent_response(member: str, device_path, scu_address: str):
    """SPEC MIRROR of ble_client._pair_via_bluetoothctl's agent closure (v2.76.3).

    Returns ("reject", None) | ("return", <value>). KEEP IN SYNC with the real
    agent_handler if the device-lock / PIN / passkey rules change.
    """
    device_suffix = ("dev_" + scu_address.replace(":", "_")).upper()
    device_methods = {
        "RequestConfirmation",
        "RequestAuthorization",
        "AuthorizeService",
        "RequestPinCode",
        "RequestPasskey",
    }
    if member in device_methods:
        if device_path is None or not str(device_path).upper().endswith(device_suffix):
            return ("reject", None)
    if member == "RequestPinCode":
        return ("return", "0000")
    if member == "RequestPasskey":
        return ("return", 0)
    return ("return", None)


def test_agent_decision_spec() -> None:
    print("6) SPEC MIRROR: device-locked agent + legacy PIN/passkey (v2.76.3)")
    scu = "C5:D9:A0:14:C5:37"
    ours = "/org/bluez/hci0/dev_C5_D9_A0_14_C5_37"
    foreign = "/org/bluez/hci0/dev_11_22_33_44_55_66"
    check("our SCU confirmation accepted", _decide_agent_response("RequestConfirmation", ours, scu) == ("return", None))
    check("foreign confirmation rejected", _decide_agent_response("RequestConfirmation", foreign, scu) == ("reject", None))
    check("our SCU RequestPinCode -> '0000'", _decide_agent_response("RequestPinCode", ours, scu) == ("return", "0000"))
    check("our SCU RequestPasskey -> 0", _decide_agent_response("RequestPasskey", ours, scu) == ("return", 0))
    check("foreign RequestPinCode rejected", _decide_agent_response("RequestPinCode", foreign, scu) == ("reject", None))
    check("Release accepted (no device arg)", _decide_agent_response("Release", None, scu) == ("return", None))
    check("Cancel accepted (no device arg)", _decide_agent_response("Cancel", None, scu) == ("return", None))
    # Case-insensitive address match (BlueZ upper-cases the device path).
    check("lower-case SCU address still matches", _decide_agent_response("RequestConfirmation", ours, scu.lower()) == ("return", None))


def main() -> int:
    print(f"Loading {os.path.relpath(_BLE_CLIENT, _HERE)} ...")
    m = _load_ble_client()
    test_pair_request_uses_field1(m)
    test_pair_response_roundtrip(m)
    test_rewrap_cloud_payload(m)
    test_extract_response_id_status(m)
    test_frame_roundtrip(m)
    test_agent_decision_spec()
    print()
    if _FAILURES:
        print(f"RESULT: {len(_FAILURES)} FAILURE(S): {', '.join(_FAILURES)}")
        return 1
    print("RESULT: ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
