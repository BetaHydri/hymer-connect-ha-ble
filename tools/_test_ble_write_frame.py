"""Offline byte-level test for the opt-in BLE write path (field-1 rewrap).

Mirrors dan-simms1/hymer-connect-ha ``tests/test_token_tool_ble_writes.py``
(CloudEncodingAgreementTests). It proves the exact fix for the v2.62.24
"BLE writes silently dropped" bug:

  * ``build_light_command`` (the proven cloud encoder) wraps the PIA Request in
    top-level field 2 — correct for the cloud DataHub envelope, but on BLE
    field 2 is ``BleProtocol.response`` so the SCU discards it silently.
  * ``ble_client._rewrap_cloud_payload_as_ble_request`` re-tags the outer
    wrapper to field 1 (``BleProtocol.request``) WITHOUT touching the inner
    Request, and returns the Request's request_id for ACK correlation.

Also checks the response ACK parser used to correlate writes.

Run:  python tools/_test_ble_write_frame.py
Exit code 0 = pass, 1 = fail.
"""

from __future__ import annotations

import base64
import importlib.util
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_CC = _ROOT / "custom_components" / "hymer_connect"

# Windows consoles default to cp1252; force UTF-8 so the arrows/em-dashes in the
# diagnostic prints below don't raise UnicodeEncodeError.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, _CC / f"{name}.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


pia = _load("pia_decoder")
ble = _load("ble_client")


def _fields(buf: bytes) -> dict[int, list[bytes]]:
    """Collect fields by number: LEN as raw bytes, VARINT as int-in-bytes."""
    out: dict[int, list[bytes]] = {}
    offset = 0
    while offset < len(buf):
        key, offset = ble._decode_varint(buf, offset)
        fn, wt = key >> 3, key & 7
        if wt == ble._WIRE_LEN:
            val, offset = ble._decode_len_delimited(buf, offset)
            out.setdefault(fn, []).append(val)
        elif wt == ble._WIRE_VARINT:
            val, offset = ble._decode_varint(buf, offset)
            out.setdefault(fn, []).append(val)  # type: ignore[arg-type]
        else:
            offset = ble._skip_field(buf, offset, wt)
    return out


_failures: list[str] = []


def check(cond: bool, msg: str) -> None:
    if cond:
        print(f"  PASS: {msg}")
    else:
        print(f"  FAIL: {msg}")
        _failures.append(msg)


def _request_from_cloud(b64: str) -> bytes:
    raw = base64.b64decode(b64)
    # Cloud outer wrapper is field 2 (tag 0x12).
    check(raw[0] == 0x12, "cloud payload outer tag is field 2 (0x12)")
    return _fields(raw)[2][0]


def _request_from_ble(ble_payload: bytes) -> bytes:
    check(ble_payload[0] == 0x0A, "BLE payload outer tag is field 1 (0x0A)")
    return _fields(ble_payload)[ble._BLE_PROTOCOL_REQUEST_FIELD][0]


def test_agreement(kind: str, **kwargs) -> None:
    print(f"\n[topic agreement — {kind}]")
    cloud_b64 = pia.build_light_command(11, 1, **kwargs)
    ble_payload, request_id = ble._rewrap_cloud_payload_as_ble_request(
        base64.b64decode(cloud_b64)
    )

    cloud_request = _request_from_cloud(cloud_b64)
    ble_request = _request_from_ble(ble_payload)

    # The inner Request must be byte-identical — only the outer wrapper differs.
    check(ble_request == cloud_request, "inner Request is byte-identical")

    cloud_topic = _fields(cloud_request)[ble._REQUEST_CONNECTED_COMPONENT_FIELD][0]
    ble_topic = _fields(ble_request)[ble._REQUEST_CONNECTED_COMPONENT_FIELD][0]
    check(ble_topic == cloud_topic, "setValues topic (Request field 4) matches cloud")

    # request_id must be recovered for ACK correlation.
    req_field = _fields(cloud_request).get(ble._REQUEST_ID_FIELD, [None])[0]
    check(request_id == req_field and request_id is not None,
          f"request_id recovered ({request_id})")


def test_response_parser() -> None:
    print("\n[response ACK parser]")
    body = ble._encode_varint_field(ble._RESPONSE_ID_FIELD, 4242)
    body += ble._encode_varint_field(ble._RESPONSE_STATUS_FIELD, ble.PIA_STATUS_SUCCESS)
    payload = ble._encode_bytes_field(ble._BLE_PROTOCOL_RESPONSE_FIELD, body)
    rid, status = ble._extract_response_id_status(payload)
    check(rid == 4242, "response request_id parsed")
    check(status == ble.PIA_STATUS_SUCCESS, "response status parsed as SUCCESS")

    # A request frame must NOT be mistaken for a response.
    cloud_b64 = pia.build_light_command(11, 1, bool_value=True)
    ble_payload, _ = ble._rewrap_cloud_payload_as_ble_request(
        base64.b64decode(cloud_b64)
    )
    rid2, status2 = ble._extract_response_id_status(ble_payload)
    check(rid2 is None and status2 is None,
          "a request payload yields no response id/status")


def test_frame_roundtrip() -> None:
    print("\n[BLE PIA frame round-trip]")
    cloud_b64 = pia.build_light_command(3, 1, str_value="On")
    ble_payload, _ = ble._rewrap_cloud_payload_as_ble_request(
        base64.b64decode(cloud_b64)
    )
    frame = ble.encode_ble_pia_frame(ble_payload)
    check(frame.startswith(ble.BLE_PIA_MAGIC), "frame carries the A0CB magic")
    check(ble.decode_ble_pia_frame(frame) == ble_payload,
          "frame decodes back to the field-1 payload")


def test_subscription_payloads_rewrap() -> None:
    """The subscription/refresh path (send_pia_command) uses the same rewrap."""
    print("\n[subscription payloads — field-2 → field-1]")
    subs = pia.build_subscription_requests()
    check(len(subs) > 0, "subscription payloads present")
    for i, b64 in enumerate(subs):
        raw = base64.b64decode(b64)
        check(raw[0] == 0x12, f"subscription #{i} is field-2 wrapped (cloud envelope)")
        ble_payload, _ = ble._rewrap_cloud_payload_as_ble_request(raw)
        check(ble_payload[0] == 0x0A, f"subscription #{i} rewraps as BleProtocol.request (field 1)")
    refresh = base64.b64decode(pia.build_refresh_command())
    ref_payload, _ = ble._rewrap_cloud_payload_as_ble_request(refresh)
    check(ref_payload[0] == 0x0A, "refresh command rewraps as BleProtocol.request (field 1)")


if __name__ == "__main__":
    test_agreement("bool", bool_value=True)
    test_agreement("uint", uint_value=70)
    test_agreement("str", str_value="On")
    test_response_parser()
    test_frame_roundtrip()
    test_subscription_payloads_rewrap()

    print()
    if _failures:
        print(f"RESULT: {len(_failures)} FAILURE(S)")
        sys.exit(1)
    print("RESULT: all checks passed")
    sys.exit(0)
