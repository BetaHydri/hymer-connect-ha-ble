"""Golden-vector regression test for the getPairedMobileDevices decoder.

The fixture is a real ``getPairedMobileDevices`` reply from a 7-entry pairing
table on a HYMER B-ML I 780 (SCU ASW 1.49.7), contributed by @stbcgn in
issue #26 as a follow-up to #25. The seven MACs and the two account UUIDs were
redacted with **same-length** placeholders, so every protobuf length prefix,
field boundary and offset is authentic — only the identifier bytes differ.

It locks in the exact wire structure Stefan documented:

* ``mobileDevices`` (Response field 10) is a WRAPPER message, not a repeated
  field at Response level — devices are ``repeated f1`` inside it.
* MAC (17 ch) and userUuid (36 ch) are ASCII strings, never byte-packed.
* Device entry field order is MAC(1), name(2), userUuid(3).
* The 6×UUID-A + 1×UUID-B account split from #25 (entry 2 = UUID-B).

Runs with no third-party dependencies:  ``python tests/test_decode_mobile_devices.py``
(also collected by pytest). ``ble_client`` is loaded by file path so importing
the HA package (which pulls in homeassistant) is not required.
"""

from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

_BLE_CLIENT_PATH = (
    Path(__file__).resolve().parent.parent
    / "custom_components"
    / "hymer_connect"
    / "ble_client.py"
)


def _load_ble_client():
    spec = importlib.util.spec_from_file_location("ble_client", _BLE_CLIENT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # Register before exec so @dataclass can resolve the module during class build.
    sys.modules["ble_client"] = module
    spec.loader.exec_module(module)
    return module


_BLE_CLIENT = _load_ble_client()

# issue #26: plaintext Response payload (BleProtocol.response wrapper included),
# 500 bytes, identifiers redacted with same-length placeholders.
GOLDEN_7_ENTRY_FRAME_HEX = (
    "12f10308a2a00c10011882f3c5d40652e2030a410a1161613a62623a63633a64643a65653a30"
    "3112066950686f6e651a2461616161616161612d616161612d346161612d616161612d616161"
    "6161616161616161610a410a1161613a62623a63633a64643a65653a303212066950686f6e65"
    "1a2462626262626262622d626262622d346262622d626262622d626262626262626262626262"
    "0a3f0a1161613a62623a63633a64643a65653a30331204695061641a2461616161616161612d"
    "616161612d346161612d616161612d6161616161616161616161610a410a1161613a62623a63"
    "633a64643a65653a303412066950686f6e651a2461616161616161612d616161612d34616161"
    "2d616161612d6161616161616161616161610a4e0a1161613a62623a63633a64643a65653a30"
    "3512136568672d746f6b656e2d657874726163746f721a2461616161616161612d616161612d"
    "346161612d616161612d6161616161616161616161610a410a1161613a62623a63633a64643a"
    "65653a303612066950686f6e651a2461616161616161612d616161612d346161612d61616161"
    "2d6161616161616161616161610a430a1161613a62623a63633a64643a65653a303712086861"
    "2d32373033351a2461616161616161612d616161612d346161612d616161612d616161616161"
    "616161616161"
)


class DecodeMobileDevicesGoldenVectorTest(unittest.TestCase):
    """Regression coverage for ``_decode_mobile_devices_from_payload``."""

    @classmethod
    def setUpClass(cls) -> None:
        payload = bytes.fromhex(GOLDEN_7_ENTRY_FRAME_HEX)
        cls.result = _BLE_CLIENT._decode_mobile_devices_from_payload(payload)
        cls.devices = cls.result["devices"]

    def test_response_envelope(self) -> None:
        self.assertEqual(self.result["request_id"], 200738)
        self.assertEqual(self.result["status"], _BLE_CLIENT.PIA_STATUS_SUCCESS)

    def test_seven_devices_decoded(self) -> None:
        self.assertEqual(len(self.devices), 7)

    def test_device_names_and_order(self) -> None:
        self.assertEqual(
            [d.name for d in self.devices],
            [
                "iPhone",
                "iPhone",
                "iPad",
                "iPhone",
                "ehg-token-extractor",
                "iPhone",
                "ha-27035",
            ],
        )

    def test_mac_and_uuid_are_fixed_width_ascii(self) -> None:
        for device in self.devices:
            self.assertEqual(len(device.mac), 17, device)
            self.assertRegex(device.mac, r"^[0-9a-f]{2}(:[0-9a-f]{2}){5}$")
            self.assertEqual(len(device.user_uuid), 36, device)

    def test_account_grouping_six_plus_one(self) -> None:
        uuid_a = self.devices[0].user_uuid
        uuid_b = self.devices[1].user_uuid
        self.assertNotEqual(uuid_a, uuid_b)
        # Entry 2 (index 1) is the only member of the second account.
        second_account = [i for i, d in enumerate(self.devices) if d.user_uuid == uuid_b]
        self.assertEqual(second_account, [1])
        self.assertEqual(
            sum(1 for d in self.devices if d.user_uuid == uuid_a), 6
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
