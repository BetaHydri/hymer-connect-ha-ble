# BLE Setup & Troubleshooting

> **Audience:** Users setting up **Path A — BLE + Cloud** (a Home Assistant host
> with Bluetooth, physically in the vehicle), and anyone who first set up
> **cloud-only** (Path B/C) and now wants to **add BLE later**. For the protocol
> internals behind these symptoms, see [`ble-communication.md`](ble-communication.md).

## What BLE does (and does not) do

- **BLE is a read-only sensor mirror.** Since v2.62.24 the SCU firmware (1.12.0.0)
  silently drops every BLE write, so **all commands always go via cloud / SignalR**.
  BLE only gives you **local, low-latency sensor reads**.
- **BLE is optional.** A pure cloud-only setup (Path B/C) is fully functional on
  its own. You never *need* BLE — it is a latency/offline-reads bonus for hosts
  that sit in the vehicle.
- **Every BLE pairing mints its own EHG refresh token**, bound to the pairing
  device's BLE identity. A token extracted with the Android APK (Path C) is bound
  to the **phone** and **cannot be reused by the Home Assistant host** for BLE —
  the HA host must do its **own** pairing to bond and mint its **own** token. See
  [Add BLE to an existing cloud-only setup](#add-ble-to-an-existing-cloud-only-setup).

## Does BLE keep delivering data when the vehicle's LTE is offline?

Short answer: **yes — if the HA host is the in-vehicle BLE host (Path A).** BLE is
a **direct local link** between the HA host and the SCU (TLS over Nordic UART); it
does **not** travel over the vehicle's LTE modem or the EHG cloud at all. As long
as the host is in Bluetooth range and the SCU is awake (**12 V on**), the SCU keeps
pushing **all subscribed sensor buses over BLE at ~50 ms**, with no internet
involved. **No client-side buffering is required** — it is a live push, not a poll.

What changes when the vehicle's LTE is down:

| | BLE + Cloud host in the vehicle (Path A) | Cloud-only host (Path B/C) |
| --- | --- | --- |
| **Sensor reads** | ✅ Fresh, live over BLE (~130 subscribed sensors) | ❌ Stale — the SCU can't reach Azure, so SignalR receives nothing; entities keep their last-known value |
| **Commands (writes)** | ❌ Not possible — the only write path is cloud / SignalR, which needs LTE | ❌ Not possible |
| **Cloud-derived values** | A few (e.g. GPS via *Find-My-RV*) may stop updating | ❌ |

Two clarifications that matter here:

- **LTE offline ≠ SCU offline.** On 12 V the SCU is still fully awake and still
  pushing its bus data — just not to the cloud. BLE taps that local push directly.
  (If **12 V is off**, the SCU drops to standby and stops pushing passive sensor
  data over *any* path, BLE included — that is a power state, not an LTE state.)
- **The integration does not — and cannot — buffer cloud data on the HA side.**
  When a cloud-only host loses the SCU's LTE feed, no new data is arriving to
  buffer; Home Assistant already retains each entity's last-known state until data
  resumes. Any store-and-forward while offline happens **inside the SCU firmware**,
  which re-syncs when LTE returns — it is not something this integration adds. The
  correct answer to "fresh data while LTE is down" is **BLE (Path A)**, not
  client-side buffering.

## Where each setting lives: Configure vs Reconfigure

Both menus sit under **Settings → Devices & Services → HYMER Connect BLE**, but
they do **different jobs**. The short rule:

- **⚙️ Configure** (the gear / ⋮ → *Configure*) = **tweak an already-running
  setup**. It only writes flags — it **never pairs**.
- **⋮ → Reconfigure** = **add or redo the BLE pairing**, or paste new tokens. This
  is the **only** place that actually bonds to the SCU and mints a token.

### ⚙️ Configure (Options) — `async_step_init`

| Field | What it does |
| --- | --- |
| **Fresh/grey water tank capacity** (30–200 L) | Tank size used to convert the raw level into a percentage. Pure display — nothing to do with BLE. |
| **Enable BLE direct path (sensor reads only)** | **On/off switch for the read path only.** It assumes a bond + token already exist. Ticking it on a never-paired host does **nothing useful** — it does **not** pair. Pair first via Reconfigure. |
| **Send commands over BLE (experimental)** | **Off by default.** When on, write commands (lights, switches, heater, fridge, …) are tried over the local BLE link first and **fall back to the cloud automatically** if the SCU doesn't acknowledge — so enabling it is low-risk. Requires *Enable BLE direct path* + a completed pairing. Added in **v2.66.0** (subscription path also corrected in **v2.66.2**). ⚠️ **Experimental / still being verified on real vehicles** — if you just installed and are unsure, leave it **off** for the proven cloud-write behaviour. |
| **SCU Bluetooth address** | Optional. Pin the SCU MAC so the host skips the auto-scan. Leave empty to auto-discover. |
| **OAuth Basic auth header** | Advanced/optional. Override the OAuth client credentials (rarely needed). |
| **Clear BLE bond (unpair from SCU)** | Removes the BlueZ bond **and** wipes the stored token + BLE address, so the next Reconfigure re-pairs cleanly. See [Clear a stale BLE bond](#clear-a-stale-ble-bond). |

> **Key point:** the *Enable BLE direct path* checkbox here is just a toggle. It
> flips a flag in the entry options — it **cannot** create a bond. A fresh
> cloud-only host that ticks the box (even with the SCU MAC filled in) will **not**
> start reading over BLE until it has been paired via Reconfigure.

### ⋮ → Reconfigure — `async_step_reconfigure`

| Field | What it does |
| --- | --- |
| **QR code activation token** | The dealer QR token from your handover paperwork. Entering it **triggers the BLE pairing** (bond + confirmation-token exchange + mint of the host's own refresh token) and auto-sets the BLE-enabled flag. This is the field that makes BLE actually happen. |
| **SCU Bluetooth address** | Optional. Same meaning as above — pin the MAC or leave empty to auto-scan. Setting it also auto-enables BLE. |
| **EHG Remote Access Refresh Token** | Paste an existing cloud token here to **skip pairing** (cloud-only path). Leave it **empty** to force a fresh BLE pairing. |
| **OAuth Basic auth header** | Advanced/optional. Same override as in Configure. |

> **Rule of thumb:** if you want the host to bond to the SCU and read over BLE, use
> **Reconfigure** with the **QR token** and leave the EHG token empty. Use
> **⚙️ Configure** afterwards only to turn that read path off/on, pin the MAC, or
> clear the bond.

## Verified hardware

The BLE dual-path has so far been confirmed working on **Raspberry Pi 4** hosts
(by the maintainer and a handful of users), using the Pi's **built-in Bluetooth
adapter** — no external USB BLE dongle needed. Other BLE-capable HA hosts
(BlueZ-based) should work but are **unverified**; if you get BLE running on
different hardware, a short report is very welcome.

## What to watch out for on the BLE-direct path

BLE pairing on the HA host is more environment-sensitive than the Android APK,
because the APK uses the phone's native Bluetooth + TLS stack while the HA host
depends on **BlueZ + `bluetoothctl` + the host's OpenSSL**. Check these before
and during pairing:

- **The host must be in Bluetooth range of the SCU and the vehicle's 12 V must be
  on.** Out of range or SCU asleep → no bonding.
- **The Home Assistant `Bluetooth` integration must be enabled** and own a working
  adapter (**Settings → Devices & Services → Bluetooth**). On HA OS the RPi4's
  built-in adapter works out of the box.
- **`bluetoothctl` must be available on the host.** The integration performs the
  JustWorks bond via a `bluetoothctl` agent (bleak's own `pair()` registers no
  agent, so BlueZ would otherwise cancel the pairing). HA OS and most container
  images ship it; a stripped-down host may not.
- **Press CONNECTION at the right time — the window can be short.** The SCU only
  accepts the bond while it is in pairing mode. On some vehicles this window is
  **~2 minutes**, but on others (e.g. B-MC I 680, SCU 1.13.0.0) it closes after
  only **~30 seconds** and a second CONNECTION press is ignored until it lapses.
  The most reliable sequence: **press CONNECTION, then submit the Reconfigure
  form within ~25 seconds** — the actual `Device1.Pair()` fires a couple of
  seconds after you submit, which lands it safely inside even a 30-second window.
  The integration then keeps retrying the bond, but the first attempt right after
  your press is the one most likely to succeed.
- **Do not pair while the phone/EHG app is actively connected over BLE** to the
  same SCU — a competing active connection can block the host's bond.
- **Legacy TLS 1.0/1.1 must be permitted by the host's OpenSSL.** The SCU only
  speaks legacy TLS; the integration clears `OP_NO_TLSv1`/`OP_NO_TLSv1_1` and sets
  `@SECLEVEL=0`, but a host whose OpenSSL was compiled **without** TLS 1.0/1.1
  cannot complete the handshake. This is the same legacy-TLS hurdle the Android
  extractor solved on-device (v2.65.9–v2.65.14).

> **Why did the Android APK work instantly but the HA host did not?** The APK
> uses the phone's native BLE bonding and TLS stack, so it sidesteps all three
> host dependencies above (`bluetoothctl` agent, host OpenSSL legacy-TLS support,
> BLE contention). A failed HA-host pairing is almost always one of those
> environmental factors — **not** a problem with your token or account. Cloud-only
> (Path C via the APK) remains the reliable route; BLE is the optional add-on.

## Add BLE to an existing cloud-only setup

If you already run cloud-only (token from the APK or mitmproxy) and later move the
HA host into the vehicle, add BLE via **Reconfigure** — the host will do its own
pairing and mint its **own** Pi-bound token. (For how this differs from the
⚙️ Configure page, see
[Where each setting lives](#where-each-setting-lives-configure-vs-reconfigure).)

1. Make sure the HA host is **in BLE range** of the vehicle and **12 V is on**.
2. Enable the **Bluetooth** integration in Home Assistant (if not already).
3. Go to **Settings → Devices & Services → HYMER Connect BLE → ⋮ → Reconfigure**.
4. In the **Reconfigure HYMER Connect** form:
   - **QR code activation token** — paste your dealer QR activation token (from the
     handover paperwork). This is required to enable BLE pairing.
   - **SCU Bluetooth address** — optional; leave empty to **auto-scan** for the SCU.
   - Leave **EHG Remote Access Refresh Token** empty so a **fresh BLE pairing is
     triggered** (if you paste a token here, pairing is skipped).
5. **Press CONNECTION** on the SCU touch panel, then **submit the form within
   ~25 seconds** and **do not close the dialog**. The pairing window can be as
   short as ~30 seconds on some SCUs, so submit promptly after the press rather
   than pressing CONNECTION only once the dialog is already waiting.
6. On success you see **BLE Pairing Complete** — the host has bonded and stored its
   own refresh token.

> **The phone/APK token does not work for the host's BLE path.** The APK token is
> bound to the phone's BLE identity; the host must mint its own during the pairing
> above. If a previously stored token is blocking a fresh pairing, clear it first —
> see [Clear a stale BLE bond](#clear-a-stale-ble-bond).

## Enable debug logging

You can turn on debug logging **directly from the integration** — no YAML and no
restart needed:

1. Go to **Settings → Devices & Services → HYMER Connect BLE**.
2. Open the **⋮ (three-dot) menu** and choose **"Enable debug logging"**.
3. A yellow **"Debug logging enabled"** banner appears. Reproduce the pairing
   attempt (run Reconfigure and press CONNECTION).
4. Open the same ⋮ menu and choose **"Disable debug logging"** — Home Assistant
   then **automatically downloads** the captured log for you.

This one-click toggle raises all `custom_components.hymer_connect.*` loggers to
`debug` for the duration and reverts them afterwards. For **fine-grained control**
(e.g. to also see the low-level BlueZ D-Bus layer), add this to
`configuration.yaml` and restart Home Assistant instead:

```yaml
logger:
  default: warning
  logs:
    # --- HYMER Connect integration ---
    custom_components.hymer_connect.config_flow: debug   # pairing ceremony progress
    custom_components.hymer_connect.ble_client: debug      # bonding, TLS, GATT writes
    custom_components.hymer_connect.api: debug             # confirmation/refresh token exchange
    custom_components.hymer_connect.coordinator: debug      # BLE start, path decisions
    # --- BLE stack (bleak / BlueZ) ---
    bleak: warning
    bleak.backends.bluezdbus.client: info                  # D-Bus calls, MTU, adapter errors
```

> **Tip:** Revert to the production logger profile (see
> [README → Logger reference](../README.md#logger-reference)) once you are done —
> `ble_client: debug` and the BlueZ logger are very verbose.

## Reading the log — which stage failed?

Pairing runs **bond → TLS → confirmation token → PairMobileRequest → refresh
token**. The message that stops tells you which host dependency to fix:

| Log message | Stage | What it means / what to check |
| --- | --- | --- |
| `🟢 BLE bonding SUCCESSFUL on attempt N` | Bond | CONNECTION was pressed, JustWorks bond worked |
| `BLE bonding rejected by SCU — press CONNECTION …` | Bond | Button not pressed in time, out of range, `bluetoothctl` missing, or phone holds the BLE link. Retry near the vehicle; close the EHG app. |
| `No SCU found via Bluetooth scan` (`ble_no_scu_found`) | Scan | Out of range, 12 V off, or Bluetooth integration/adapter not working |
| `BLE TLS session established with SCU …` | TLS | Legacy-TLS handshake OK |
| *(pairing hangs, then times out after TLS starts)* | TLS | Host OpenSSL likely rejects TLS 1.0/1.1 — see [What to watch out for](#what-to-watch-out-for-on-the-ble-direct-path) |
| `Could not obtain confirmation token …` (`ble_no_confirmation_token`) | Cloud | Cloud API did not return a confirmation token — check account/network |
| `BLE pairing response received from SCU …` | Pair | SCU answered the pairing request |
| `BLE pairing … did not contain a refresh token` (`ble_no_refresh_token`) | Pair | SCU answered but returned no token (pairing slot rejected) — clear the bond and retry |
| `BLE pairing failed …` (`ble_pairing_failed`) | Any | Generic failure — read the lines above it to find the real stage |

The GATT-level chatter (MTU negotiation, `ATT error: 0x0e`, write pacing) is
explained in [`ble-communication.md`](ble-communication.md#gatt-write-pacing-v2610).

## What a healthy BLE session looks like

Once pairing is done, a normal startup on a **BLE + Cloud** host produces the trace
below (SCU address and vehicle id anonymised). If your log has this shape, BLE is
working correctly — even if you *also* see unrelated errors from other integrations
(cameras, ESPHome, Modbus, …) in the same log.

```text
ble_client]  BLE connected to SCU AA:BB:CC:DD:EE:FF (MTU=247, chunk=242)
ble_client]  BLE TLS handshake complete: TLSv1.1 ('AES128-SHA', 'SSLv3', 128)
ble_client]  BLE TLS session established with SCU AA:BB:CC:DD:EE:FF
coordinator] BLE direct path established to SCU AA:BB:CC:DD:EE:FF (mode=ble)
coordinator] Sending 7 PIA subscription requests over BLE
coordinator] BLE PIA subscriptions + refresh sent
coordinator] BLE direct path active — running alongside SignalR
             (both paths: ~130 sensors, BLE ~50ms / SignalR ~500ms–2s)
```

Then, continuously, decoded sensor frames arriving over the local BLE link:

```text
ble_client]  BLE PIA RECV AA:BB:CC:DD:EE:FF: plaintext=29 B hex=…
pia_decoder] RAW PIA bus=8 | sid=2 | f10/wt2=hex:6c696e32 ("lin2") | f6/wt5=19.1
pia_decoder] DISCOVERY mapped (8,2) solar_voltage: 19.1 → 18.1
coordinator] SignalR push: 130 total sensors
```

What to check in your own log:

- **`MTU=247, chunk=242`** — the best case. **`MTU=23, chunk=20` is also fine:** it
  is a fully supported fallback (slower 20-byte Write-With-Response chunks). Since
  v2.65.16 it is logged at `INFO`, not as a warning, and never surfaces in HA's
  integration error panel.
- **`BLE TLS session established`** — the legacy-TLS handshake succeeded.
- **`Sending 7 PIA subscription requests over BLE`** → **`BLE PIA subscriptions +
  refresh sent`** — this is what unlocks the full ~130-sensor stream. Without it the
  SCU only pushes ~28 autonomous sensors.
- **`BLE direct path active — running alongside SignalR`** — both the fast local
  read path (BLE) and the cloud write path (SignalR) are up (`mode=dual`).
- Recurring **`BLE PIA RECV …`** / **`RAW PIA bus=…`** / **`DISCOVERY mapped …`**
  lines — live sensor values flowing in. This is the read mirror doing its job at
  ~50 ms latency.

> **Tip:** the `RAW PIA` and `DISCOVERY mapped` lines only appear with
> `custom_components.hymer_connect.pia_decoder: debug`. They are the ground truth
> for which bus/slot a value came from — very handy when reporting a mis-mapped
> sensor.

## Clear a stale BLE bond

If a previous pairing left a stale bond or a token that blocks re-pairing:

1. Go to **Settings → Devices & Services → HYMER Connect BLE → ⋮ → Configure**.
2. Enable **"Clear BLE bond (unpair from SCU)"** and submit.

This removes the BlueZ bond via `Adapter1.RemoveDevice()` **and** wipes the stored
EHG refresh token and BLE address, so the next Reconfigure triggers a clean
pairing. Also do this after you delete the Home Assistant pairing from the EHG
app's Bluetooth settings.

## See also

- [`../quick-start.md`](../quick-start.md) — the four setup paths (A–D)
- [README → Obtaining the EHG Refresh Token](../README.md#obtaining-the-ehg-refresh-token)
- [README → Enable debug logging](../README.md#option-3-enable-debug-logging) — full logger reference
- [`ble-communication.md`](ble-communication.md) — BLE protocol internals (bonding, TLS-over-NUS, PIA)
