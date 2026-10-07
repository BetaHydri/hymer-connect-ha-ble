# SignalR Connection Architecture

> **Audience:** Maintainers and advanced troubleshooters. Normal users only need
> the setup and troubleshooting guidance in the main README.

> **Last updated:** 2026-08-25 (v2.87.0)

This document explains how the HYMER Connect integration maintains its real-time
connection to the vehicle SCU (Smart Connectivity Unit) through Azure SignalR Service.
It covers the connection lifecycle, token management, reconnection logic, and lessons
learned from production issues.

> **Important — 12V Safety:** The integration **never** automatically switches the
> 12V main power on or off. All reconnects, refreshes, resubscribes, and backoff
> retries are purely **connection-level** operations — they only manage the WebSocket
> link to the cloud, no switch commands are sent. The 12V state only changes when
> the user explicitly toggles it via HA or the EHG app. This is critical because
> the 12V rail powers downstream devices (private router, local HA instance, etc.)
> that would drain the battery if left on unintentionally.

## Overview

```
Home Assistant
    └── coordinator.py (DataUpdateCoordinator, polls every 60s)
            ├── ble_client.py (BLE direct path — sensor reads + BLE-first writes since v2.67.0)
            │       └── SCU in vehicle (via BLE GATT / TLS / PIA)
            └── signalr_client.py (always active* — full sensor coverage + write fallback)
                    └── Azure SignalR Service (ehg-prod-signalr.service.signalr.net)
                            └── SCU in vehicle (via LTE)
```

> \* *Always active by default.* The opt-in **`cloud_on_demand`** option
> (v2.99.0b4+, default **off**) tears the SignalR session down while BLE is
> healthy and reconnects it only when BLE drops/degrades — see
> [Cloud on Demand (opt-in)](#cloud-on-demand-opt-in). A cloud-only enrollment
> is never affected.

Both paths run concurrently. With BLE subscriptions, both paths can provide
all ~130 sensors — BLE at ~50 ms latency, SignalR at ~500 ms–2 s. Both merge
into the same data store. **Writes go over BLE first with automatic cloud
fallback (v2.67.0+, on by default):** when BLE is connected, a command is sent
over the local BLE link and falls back to SignalR if the SCU does not acknowledge;
when BLE is not connected, all writes go via SignalR. The earlier conclusion that
the SCU silently drops BLE `setValues` writes (v2.62.24) turned out to be a
**client-side protobuf-encoding bug** — fixed in **v2.66.0** (subscription path in
**v2.66.2**, on-by-default in **v2.67.0**), root cause found by **Dan Simms**; see
[`ble-communication.md`](ble-communication.md) for the full investigation. On the
SignalR leg the coordinator does one reconnect-retry on failure.

After initial setup (OAuth2 login + EHG token exchange), the BLE path can
operate fully offline. SignalR requires ongoing internet connectivity.
There is no REST API for real-time data — the SCC REST API only provides
static metadata (VIN, model, URNs).

## Connection Establishment

The connection requires a **5-step handshake**:

1. **Negotiate step 1** — `POST scc-appcomm/datahub/negotiate` (no auth headers)
   Returns: Azure SignalR URL + short-lived JWT token (~1 hour)

2. **Negotiate step 2** — `POST {azure_url}/negotiate` with JWT bearer
   Returns: `connectionToken` for WebSocket URL

3. **WebSocket connect** — `wss://ehg-prod-signalr.service.signalr.net/client/?hub=datahub&id={connectionToken}&access_token={jwt}`

4. **Protocol handshake** — Send `{"protocol": "json", "version": 1}`

5. **UpdateTokens** — Send OAuth2 access token + EHG remote access token + vehicle/SCU URNs
   This authenticates the connection and enables data flow

6. **PIA subscription** — Send 7 protobuf-encoded PiaRequest messages to subscribe
   to all sensor groups, plus a refresh command to force initial state push

After step 6, the SCU starts pushing `PiaResponse` messages with sensor data.

## Token Types

> User-facing token/pairing overview (which token is which, how each setup path
> obtains the refresh token, per-device minting) lives in
> [`ehg-token-and-pairing.md`](ehg-token-and-pairing.md). The table below is the
> maintainer-focused view with the SignalR-specific timing.

The integration manages **4 different tokens** — confusing them causes silent failures:

| Token | Source | Lifetime | Used For |
|-------|--------|----------|----------|
| OAuth2 access token | `POST /api/v2/oauth/token` (ROPC) | ~1 hour | REST API calls, UpdateTokens `accessToken` field |
| OAuth2 refresh token | Same endpoint | Long-lived | Refreshing OAuth2 access token on 401 |
| SignalR negotiate JWT | `POST scc-appcomm/datahub/negotiate` | ~1 hour | WebSocket URL `access_token` parameter |
| EHG remote access token | `POST /api/ehg/v1/vehicles/{urn}/remoteAccessToken` | ~30 min | UpdateTokens `ehgAccessToken` field — required for remote commands |

### Token Refresh Strategy

- **OAuth2 access token**: Auto-refreshed on 401 responses via `_request()` retry
- **SignalR negotiate JWT**: Not refreshable — connection must be recycled before expiry
- **EHG remote access token**: Refreshed every 15 min via `send_refresh()` → `_send_update_tokens()`

## SCU Data Freshness

The SCU does **not** continuously push sensor data on its own. After the initial
subscription response, it goes silent within **2–3 minutes** unless periodically
prodded. Without prodding, the stale-data detector (`STALE_DATA_TIMEOUT = 3 min`)
triggers a reconnect — which is wasteful and causes unnecessary churn.

### Two-Tier Polling Strategy (v2.23.1+)

| Tier | Method | Frequency | Messages | Purpose |
|------|--------|-----------|----------|---------|
| **Lightweight refresh** | `send_refresh()` | Every 60s (each poll) | 1 | Prod SCU to re-report current values |
| **Full resubscribe** | `resubscribe()` | Every 10 min | 7 + 1 | Reinitialise all sensor groups |

The **refresh command** (protobuf field 9 = empty) is the same "aktualisiere" command
the EHG app sends when the user swipes between views (Dashboard, Licht, Wasser, etc.)
or pulls to refresh. Each view change triggers a single refresh to get fresh values.
It triggers a full state report from the SCU without the overhead of re-sending all
7 subscription requests.

The **full resubscribe** re-sends all 7 PIA subscription requests to ensure no sensor
group is missed (e.g., after a reconnect where the initial subscription partially failed).

### Why the SCU Goes Silent

> **Note:** This is an educated guess based on observed behaviour, not confirmed
> by Hymer/EHG documentation.

The SCU firmware implements a request/response model rather than continuous
streaming — likely to conserve the **cellular data plan** included with the
vehicle. The LTE data cost is covered by Hymer (not the owner), so the SCU
minimises upstream traffic by only reporting values when explicitly asked.

- **Event-driven values** (light toggled, door opened, 12V switched) are pushed immediately
- **Slow-changing values** (battery SOC, solar current, temperatures) are only reported in response to refresh/subscription requests
- Without periodic prodding, the SCU assumes no client is actively watching and stops sending data after ~2–3 minutes

## Connection Lifecycle

### Normal Operation

```
connect → listen loop (receives PiaResponse messages)
                ↕ send commands (PiaRequest for lights, heater, etc.)
                ↔ refresh every 60s (1 msg — prod SCU to push fresh data)
                ↔ full resubscribe every 10 min (8 msgs — reinit sensor groups)
                ↕ UpdateTokens every 15 min (keep EHG access token valid)
    ~4 h → proactive disconnect (recycle floor)
                → reconnect (new negotiate → new WebSocket → new subscriptions)
```

The connection is **proactively recycled every 4 hours** (`MAX_CONNECTION_AGE = 4 * 60 * 60`).
Azure SignalR validates the access-token JWT only at negotiate/connect time and does
**not** drop an established WebSocket when that JWT expires (~1 h), so the recycle is a
rare safety floor rather than a token-expiry workaround: the datahub `ehgAccessToken` is
refreshed in-place every 15 min (`UPDATE_TOKENS_INTERVAL`) and a dead socket is caught
within 90 s by the keepalive (`KEEPALIVE_TIMEOUT`). This is purely a **connection-level**
operation — no device commands (12V, lights, etc.) are sent during reconnection. It
produces expected log messages:

```
SignalR connection lost — scheduling immediate reconnect
SignalR connected for urn:ehg:vehicle:...
```

### Standby Mode (12V Off)

When the 12V main switch is off, the SCU enters standby:
- The WebSocket stays open but no sensor data is pushed
- The stale-data timeout (`STALE_DATA_TIMEOUT = 3 min`) is **skipped** to avoid
  unnecessary reconnections during standby
- A safety cap (`STANDBY_MAX_SILENCE = 30 min`) forces a **WebSocket reconnect**
  even in standby. This only re-establishes the SignalR connection — it does
  **NOT** send a 12V switch-on command. The 12V remains off until manually
  switched back on by the user (via HA or EHG app). The reconnect handles the
  edge case where 12V was physically toggled back ON at the vehicle but the
  `main_switch` sensor is still cached as "Off" in HA

### SCU Reconnect (12V Off → On)

When 12V is toggled back ON **by the user**, the SCU reinitialises (whether this is a
full reboot or just a reconnection is unknown) and registers a new session at Azure SignalR.
The integration detects this via `scu_connected` transitioning `false → true` and automatically:
1. Re-sends UpdateTokens (refreshes routing at the hub)
2. Re-subscribes to all sensor data
3. Waits 2 seconds for SCU initialisation before acting

This is a **read-only** recovery — it restores command delivery and data flow but
does not send any switch commands. Without it, commands would be silently rejected
because the hub's routing table points to the old SCU session.

## Reconnection Logic

### Trigger Sources

| Trigger | Handler | Backoff | Sends commands? |
|---------|---------|---------|----------------|
| WebSocket closed/error | `_on_connection_lost()` | Reset to 60s (or 5s cooldown after rapid drop) | No — connection only |
| No WebSocket activity for 90s | Keepalive timeout in `listen()` | Reset to 60s | No — connection only |
| Connection age > 4 h | `needs_reconnect` property | Immediate | No — connection only |
| Send failure | `_send_with_retry()` | Immediate (1 retry) | Only retries the user''s command |

### Backoff Strategy

```
Failure 1: wait  60s
Failure 2: wait 120s
Failure 3: wait 240s
Failure 4: wait 480s
Failure 5: force OAuth2 token refresh, reset to 60s  ← hard reset
Failure 6: wait  60s (fresh cycle)
...
Cap: 900s (15 min) maximum between attempts
```

After **5 consecutive failures**, the integration assumes the OAuth2 token has expired
and forces a full token refresh before retrying. This prevents getting permanently stuck
in backoff when the auth state is stale.

### Rapid-Drop Cooldown (v2.63.11)

When a SignalR session drops within 30 seconds of being established (`_RAPID_DROP_THRESHOLD`),
the coordinator applies a 5-second cooldown (`_RAPID_DROP_COOLDOWN`) before reconnecting.
This prevents hammering the Azure SignalR Service when the server hasn't cleaned up the
old session yet — a pattern observed as "8-message rapid drops" in production logs:

```
SignalR listen loop ended after 8 messages — requesting immediate reconnect
SignalR connection dropped after 0.9s — applying 5s cooldown before reconnect
```

Sessions that lasted longer than 30 seconds reconnect immediately (the normal path).
The coordinator tracks the connection timestamp via `_signalr_connected_at` (set on
successful connect in `start_signalr()`).

### Options Update (v2.63.10)

The config entry `update_listener` callback (`_async_options_updated`) no longer calls
`async_reload()`. The previous behavior caused a full integration teardown and re-setup
(killing SignalR, destroying all entities, then recreating everything) every time HA
evaluated the config entry options (~every 5 minutes). Since options like tank capacity
and BLE address are read dynamically from `config_entry.options` on every poll cycle,
no reload is needed. This also fixes the HA 2026.12 deprecation warning for
`add_update_listener`.

### Command Retry (`_send_with_retry`)

All control commands (lights, heater, fridge, switches) use a 2-attempt strategy:

```
attempt 1: ensure_healthy → send → success? done
attempt 1: ensure_healthy → send → fail?
    → force reconnect
attempt 2: ensure_healthy → send → success? done
attempt 2: ensure_healthy → send → fail? → raise HomeAssistantError
```

This means **a single transient connection drop never causes a visible command failure** —
the user just sees a slightly delayed response.

### Entity-level verify-and-retry (v2.92.0)

Separate from the transport-level `_send_with_retry` above, each **commandable
entity** verifies its own command against the SCU readback. Every entity sets an
OPTIMISTIC value immediately, then reconciles it:

- **TTL self-heal** — an optimistic value the SCU never confirms is dropped after
  ~20 s (`OPTIMISTIC_STATE_TTL`) so the real readback wins. Without this, a
  command that was dropped downstream (e.g. a BLE write that fell back to cloud
  but was not applied) would leave the entity stuck on the wrong value forever,
  because the SCU keeps reporting the OLD value and a clear-only-on-match never
  fires.
- **One-shot re-send** — after a command, the entity waits ~8 s
  (`COMMAND_VERIFY_DELAY`) for a confirming readback; if none arrives it re-sends
  the exact same command once (the coordinator escalates BLE→cloud). The retry is
  **skipped** when it could not be confirmed anyway (12V-off / data silence or a
  frozen SCU), and a newer user command cancels a pending verification.

This is centralised in `optimistic.py` (`OptimisticCommandMixin`) and applied to
the **climate, cover, number and select** platforms. The **water-pump switch**
keeps its own equivalent verification task (`_verify_send`) and is unchanged. The
safety motivation is concrete: turning the water pump OFF, or the diesel heater
OFF, must not be silently lost. Lights gained the same protection in v2.91.11.

## Traffic Budget

### Why Traffic Matters

The Azure SignalR Service (and/or the EHG backend) enforces connection limits.
Excessive message volume causes **server-side disconnects** without explicit error messages —
the WebSocket simply closes.

### Message Breakdown (v2.23.1)

| Source | Frequency | Messages | Per Hour |
|--------|-----------|----------|----------|
| PIA refresh (lightweight) | Every 60s | 1 | ~60 |
| PIA full resubscribe | Every 10 min | 7 + 1 refresh | ~48 |
| UpdateTokens refresh | Every 15 min | 1 | ~4 |
| Client keepalive ping | Every 30s | 1 | ~120 |
| Server pings (responded to) | Variable | ~1/min | ~60 |
| **Total outbound** | | | **~292** |

### v2.23.0 — Too Little Traffic (Caused Stale Data)

| Source | Frequency | Messages | Per Hour |
|--------|-----------|----------|----------|
| PIA resubscribe | Every 10 min | 7 + 1 refresh | ~48 |
| UpdateTokens refresh | Every 15 min | 1 | ~4 |
| Client keepalive ping | Every 30s | 1 | ~120 |
| **Total outbound** | | | **~172** |

The SCU went silent after ~3 min without prodding, triggering `STALE_DATA_TIMEOUT`
and unnecessary reconnects every ~10 min (matching the resubscribe interval).

### Pre-v2.23.0 — Too Much Traffic (Caused Server Disconnects)

| Source | Frequency | Messages | Per Hour |
|--------|-----------|----------|----------|
| PIA resubscribe | Every 60s | 7 + 1 refresh | **~480** |
| UpdateTokens refresh | Every 15 min | 1 | ~4 |
| Client keepalive ping | Every 30s | 1 | ~120 |
| **Total outbound** | | | **~604** |

The EHG mobile app sends subscriptions **once on connect** and never resubscribes.
Our previous 60s resubscribe was ~480× the app''s rate, which triggered disconnects
after 4-5 hours of continuous operation.

### Lesson Learned

> **The SCU needs regular prodding but not heavy resubscription.**
> A single lightweight refresh command (field 9) every 60 seconds is enough to
> keep data flowing. The full 7-subscription resubscribe should only run every
> 10 minutes. Sending all 8 messages every 60 seconds (~480/hr) triggers
> server-side disconnects; sending nothing for 10 minutes causes the SCU to
> go silent and triggers stale-data reconnects.

### Field-Observed Steady-State Cadence (anonymized)

A ~96-minute production `signalr_client` INFO log from a stationary vehicle
(vehicle URN anonymized as `urn:ehg:vehicle:hy-xxxxxxxxxx`) confirms the budget
above. It is **not** a per-sensor polling loop — the integration holds **one
long-lived WebSocket** and the SCU pushes over it. The only recurring **outbound**
activity is driven by two hard caps:

| Outbound activity | Observed cadence | What it sends |
|-------------------|------------------|---------------|
| `UpdateTokens` refresh | ~every 15–16 min (900 s token TTL) | 1 `UpdateTokens` + 1 token `GET` |
| Full reconnect | ~every 4 h (`MAX_CONNECTION_AGE`; was ~50 min / 3000 s before v2.99.0) | negotiate + handshake + `UpdateTokens` + **7 PiaRequest subscriptions** + 1 refresh |

Everything else in the log is **inbound**: repeated
`SCU disconnected (scu_connected=false)` lines are the SCU's own standby/keepalive
push frames (~1 every 2–4 min), and the `listen loop ended after N messages`
counters (e.g. 429 messages in a 36 min window, 1044 in a 52 min window) count
**received** frames, not requests we make.

Representative outbound events over the window (timestamps relative, `t0` = log start):

```
t0            UpdateTokens sent → SUCCESS
t0 +16 min    UpdateTokens age 959s exceeds 900s — refreshing
t0 +32 min    UpdateTokens age 959s exceeds 900s — refreshing
t0 +36 min    SignalR connection age 3121s exceeds max 3000s — reconnect needed
              → negotiate → handshake → UpdateTokens → 7 PiaRequest subscriptions → refresh
...           (pattern repeats: ~4 token refreshes/hour, ~1.2 reconnects/hour)
```

> The sample above was captured on the pre-v2.99.0 build (50 min / 3000 s cap).
> **v2.99.0 raised `MAX_CONNECTION_AGE` to 4 h**, so the full-reconnect row now fires
> ~every 4 h (~0.25/hour) instead of ~1.2/hour — token refreshes are unchanged at
> ~every 15 min. See *Why Proactive Connection Recycling?* below.

So in steady state the integration issues roughly **~4 token refreshes/hour** plus
**~1.2 full reconnects/hour** (each reconnect being the only burst of the 7
subscriptions + refresh). The loud signal to the EHG/Azure backend is the
**persistent 24/7 SignalR connection itself**, not request count — which is exactly
what the opt-in `cloud_on_demand` option (v2.99.0b4) reduces on BLE-capable
installs by dropping the cloud socket once BLE holds healthy.

## Cloud on Demand (opt-in)

`cloud_on_demand` (const `CONF_CLOUD_ON_DEMAND`, **default off**, added in
**v2.99.0b4**) minimises how much the integration talks to the EHG/Azure cloud.
Its motivation is the traffic observation above: the dominant backend footprint
is the **persistent 24/7 SignalR connection itself** (plus the 60 s poll and
15 min token refresh), not request volume. BLE is invisible to EHG, so a
BLE-capable install can run almost entirely local.

### Behaviour

When enabled, once the BLE link has been **healthy and non-degraded for a
stability grace window** (`CLOUD_ON_DEMAND_BLE_STABLE_SECONDS = 120 s`), the
coordinator calls `stop_signalr()`, sets `connection_mode = "ble"`, and runs
**BLE-only** — no SignalR socket, no token refresh, no resubscribe, no 60 s
refresh to the cloud. The cloud session is **reconnected automatically** the
moment BLE drops or its write channel degrades (`#24`), so there is no loss of
coverage — only a short reconnect window on failover.

### Safe-by-construction gate

The suppression gate (`_cloud_on_demand_active()`) requires **all** of:

| Condition | Why |
|-----------|-----|
| `cloud_on_demand` option is on | Opt-in only |
| `ble_enabled` | BLE must be a usable transport |
| `_ble_connected` | A live BLE link must currently exist |
| not `_ble_write_degraded` | The BLE write/notify channel must be healthy (not the stale `Write acquired` wedge) |
| `_ble_healthy_since` ≥ 120 s ago | The link must have held stable through the grace window |

Because `ble_enabled` + `_ble_connected` are hard requirements, a **cloud-only
enrollment can never enter this branch** — cloud stays always-on for it. The
`_ble_healthy_since` clock is reset to `0` on every BLE teardown/degrade, so a
flapping link can never suppress the cloud.

### When to use it

Ideal for a stationary vehicle with a reliable BLE link that holds for long
stretches (e.g. a confirmed 30+ min hold). Leave it **off** if BLE is marginal
(frequent ~90 s drops): the repeated cloud teardown/reconnect on each failover
would add churn rather than remove it. One option change per log while testing,
so the logs stay interpretable.

> **Not a ToS-evasion measure.** It only drops the cloud socket while BLE is
> genuinely carrying the data; it does not spoof, jitter, or throttle traffic.
> Raising the poll interval was deliberately rejected (persistent SignalR is the
> fingerprint, and a higher interval breaks live push for negligible gain).

## Troubleshooting

### Symptom: "SignalR connection lost" every ~4 hours

**This is normal.** The connection is proactively recycled as a safety floor (every
4 h since v2.99.0; it was ~50 min on older builds). Check that it''s followed by
"SignalR connected for..." within a few seconds.

### Symptom: Connection drops and never reconnects

Check HA logs for:
- `"SignalR reconnect backoff: Xs remaining (attempt N/5)"` — backoff is active
- `"OAuth2 token refreshed after consecutive failures"` — hard reset triggered
- `"SignalR connection failed (N/5): ..."` — the actual error causing failures

**Common causes:**
1. **OAuth2 refresh token expired** — Re-authenticate by removing and re-adding the integration
2. **EHG servers down** — Check if the EHG app itself works
3. **Network issue** — Check HA''s internet connectivity

### Symptom: "Session is closed" warnings on HA restart

**Fixed in v2.33.0.** The coordinator now sets `_shutting_down = True` before
tearing down SignalR during config entry unload or HA stop. The connection-lost
callback checks this flag and suppresses reconnect attempts. If you still see
this on older versions, upgrade to v2.33.0+.

### Symptom: SCU is stuck and not responding to commands

Use the **Restart SCU** button (v2.33.0+) in the dashboard System tab or via
`button.hymer_restart_scu`. This sends a PIA `Request.command.restart` (cold reboot)
to the SCU. The SCU will disconnect, reboot, and reconnect within ~30–60 seconds.
The integration auto-reconnects after the reboot.

### Symptom: Commands sent from HA but nothing happens on vehicle

1. Check if SignalR is connected: look for recent "SignalR connected" in logs
2. Check UpdateTokens status: look for "UpdateTokens SUCCESS" or "UpdateTokens failed"
3. If UpdateTokens shows a non-OK status, the EHG remote access token may be expired
4. Try reloading the integration (Settings → Integrations → HYMER Connect → Reload)

### Symptom: Sensor data is stale / not updating

1. Check if 12V main switch is on (SCU stops pushing data in standby)
2. Check last "PIA re-subscription sent" log entry — should be within last 10 min
3. If no resubscription logs, the connection is likely dead — check reconnection logs

### Symptom: Fridge door / window contact stuck on initial state

**Fixed in v2.36.6.** The PIA protobuf decoder's depth filter (`depth <= 3`)
silently dropped real-time push updates for some sensors (e.g. `fridge_status`,
`heater_diesel_safety`) because the SCU nests state-change pushes at
protobuf depth 4 — one level deeper than the initial subscription response.
The initial value was received correctly but subsequent open/close events were
discarded. If you still see this on older versions, upgrade to v2.36.6+.

### Symptom: Fridge door shows changes in EHG app but not in HA

The EHG app connects via **BLE** (Bluetooth Low Energy) directly to the SCU
when you are near the vehicle. This works even with **12V off** because BLE
communication bypasses the cloud entirely.

If Home Assistant is running **cloud-only** (no BLE path, or BLE not connected), it
has only the **SignalR cloud path**. When 12V is off, the SCU enters standby and
stops pushing passive sensor data (door state, temperatures, water levels) to the
cloud. Commands (fridge on/off, lights) still work because the SCU echoes command
responses, but passive sensors like the fridge door (bus 37) do not update.

> **With the BLE path connected this is not an issue** — the SCU's BLE link stays
> active in 12V standby, so a BLE-connected HA host keeps receiving passive sensor
> updates (door, temps, water) even with 12V off.

**Solution:** Turn 12V ON, wait for `SCU reconnected (scu_connected false→true)`
in the HA log, then test the fridge door. You should see:
```
State change (37,2) fridge_status: 'Closed' → 'Open' (depth=4)
```

### Symptom: "Command failed after reconnect+retry"

The connection is fully broken and automatic recovery failed. Actions:
1. Reload the integration
2. If reload fails, check HA logs for auth errors
3. As last resort, remove and re-add the integration

## Architecture Decisions

### Why Proactive Connection Recycling?

Azure SignalR JWTs expire after ~1 hour, but Azure validates that JWT **only at
negotiate/connect time** and does not drop an already-established WebSocket when it
expires. The datahub `ehgAccessToken` is refreshed in-place every ~15 min and a dead
socket is caught within 90 s by the keepalive, so the proactive recycle is only a rare
safety floor: **v2.99.0 raised it from 50 min to 4 h** (`MAX_CONNECTION_AGE = 4 * 60 * 60`),
cutting the full-reconnect churn against the EHG datahub by ~90 %. If Azure ever does drop
the socket near its ~1 h JWT lifetime it simply reconnects reactively, so the worst case is
unchanged.

### Why Not Use the HA Poll Interval for Full Resubscribe?

The HA coordinator polls every 60s for REST metadata updates. Initially, we piggybacked
full PIA resubscriptions (8 messages) on this poll. This caused server-side
disconnects after 4-5 hours (~480 msgs/hr). Reducing to 10-min-only caused
the SCU to go silent after ~3 min. The solution is a **two-tier approach**:
lightweight refresh (1 msg) every poll, full resubscribe every 10 min.

### Why Fire-and-Forget for Periodic UpdateTokens?

During initial connect, we wait for the UpdateTokens completion response. During periodic
refresh (every 15 min), we use fire-and-forget because the listen loop is already running
and will receive the completion message. Waiting would block the coordinator poll.

### Why Detect SCU Reconnect via `scu_connected`?

When 12V is toggled OFF→ON **by the user**, the SCU reinitialises and gets a new session at Azure SignalR.
Our existing WebSocket stays open (it''s connected to the Azure hub, not directly to the SCU),
but the hub''s routing table now points to the SCU''s new session. Without re-sending
UpdateTokens, our commands go to the old (dead) session and are silently dropped.
Note: this recovery only restores the connection — it never sends switch commands.

### Why Does the Integration Never Auto-Switch 12V?

The 12V main switch controls the habitation power rail. When 12V is on, downstream
devices (private router, local HA instance, Truma heater standby, etc.) draw power
from the lithium battery. Automatically switching 12V on would cause unintended
battery drain when the owner is away. Therefore, all automatic operations in the
integration (reconnects, refreshes, resubscribes, backoff retries, SCU reconnect
detection) are strictly **connection-level** — they never send 12V or any other
switch/light/device commands.

## File Reference

| File | Role |
|------|------|
| `coordinator.py` | Connection lifecycle, reconnection backoff, command routing |
| `signalr_client.py` | WebSocket management, PIA protocol, keepalive, listen loop |
| `api.py` | OAuth2 auth, token refresh, SignalR negotiate, REST API |
| `pia_decoder.py` | Protobuf encode/decode for PIA sensor data and commands. Depth filter (depth ≤ 3, or depth 4 for known sensors) prevents phantom values |
| `button.py` | SCU restart button entity (Request.command.restart) |
| `const.py` | Timing constants, API URLs, header names |
