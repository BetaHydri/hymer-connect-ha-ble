# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [2.91.0] - 2026-08-26

### Added

- **New diagnostic `binary_sensor` "SCU frozen" that turns on when the SCU firmware is hung.** A hung SCU keeps its connection (`SCU connected` stays on) and may keep pushing frames, but its internal clock (`scu_internal_time`) stops advancing and it silently ignores every command over BOTH BLE and cloud — including the restart button. Until now this looked like an unresponsive dashboard with no explanation: the 12 V main tile stuck at "on", switches reverting to the stale readback after the command hold-off, and a full re-auth + reconnect firing per command while nothing actually changes. The new sensor (device class *problem*, under Diagnostics) detects the condition — SCU still connected and frames still arriving, but the clock unchanged for 15 minutes — and exposes the frozen clock value, how long it has been stuck, and the recovery step in its attributes. The only recovery is a physical Aufbaubatterie power-cycle; no software restart (BLE or cloud) reaches a hung SCU. Works on cloud-only and BLE setups alike.
- **A frozen SCU no longer triggers a pointless re-authentication storm.** When a switch command is not confirmed and the SCU is detected as frozen, the integration now stops instead of forcing a full OAuth2 re-auth + SignalR reconnect per command — that reconnect cannot help a hung SCU and only churned. The command is dropped, the "SCU frozen" sensor shows why, and normal operation resumes automatically once the SCU is power-cycled.

## [2.90.0] - 2026-08-26

### Fixed

- **BLE now detects and recovers from two host-BlueZ link failures that previously required a Home Assistant or host restart (#24).**
  - **Silently-dead link:** BlueZ can drop the SCU channel **without firing a disconnect callback**, so `bleak` keeps reporting `is_connected = True`, the listen loop spins on an empty queue, and the reconnect watchdog short-circuits as "already connected" — BLE stays dead until a restart (invisible because the cloud path keeps entities whole). A new **receive-liveness check** no longer trusts `is_connected`: if BLE claims connected but no BLE frame has arrived for ~60–90 s **while data is still flowing over the cloud** (the SCU is provably awake), the link is torn down and reconnected on the next watchdog tick. A genuine 12 V-off standby (both transports silent) does not churn reconnects. Confirmed on-vehicle: detection ~80 s after a `bluetoothctl disconnect`, reconnect ~1.5 s later at MTU 247, no restart.
  - **Wedged write channel:** if a reconnect lands back on a leaked `[org.bluez.Error.NotPermitted] Write acquired` acquisition that a fresh GATT session cannot clear (MTU pinned at 23, TLS/writes fail), it is now treated as a **hard failure** with an **escalating back-off (2–15 min)** instead of an identical reconnect every 30 s, and a diagnostic **`binary_sensor` "BLE degraded"** (device class *problem*) turns on with the reason in its attributes. The reassuring "MTU 23 is normal" log is suppressed when the low MTU is caused by a stale acquisition; a genuinely benign MTU-23 link (e.g. a BLE proxy) is unchanged. Recovering a wedged channel is host-side: a **full host reboot** on Home Assistant OS (which has no host `systemd`), or `systemctl restart bluetooth` on Supervised/Proxmox/container installs with a host shell.
- The cloud/SignalR path is unaffected throughout. This is most relevant to hosts running **Home Assistant OS as a Proxmox VM with USB-passthrough Bluetooth**, where the host's own `bluetoothd` can re-claim the adapter.

This stable release consolidates the `2.90.0b1`/`2.90.0b2` betas, and includes the `2.89.1` BLE-address (upper-case) normalization fix and everything from `2.89.0` (#23). As always after updating, **restart** Home Assistant.

## [2.90.0b2] - 2026-08-26 (pre-release)

### Fixed

- **BETA — a silently-dead BLE link is now detected and reconnected instead of staying dead until a restart (#24).** Beta testing of 2.90.0b1 surfaced a distinct, more common half of the same family: after an external BLE disconnect the host's BlueZ stack can drop the channel **without firing a disconnect callback**, so `bleak` keeps reporting `is_connected = True`, the listen loop just spins on an empty queue, and the integration never notices — no `BLE disconnected` line, no reconnect attempt, the 30 s watchdog short-circuits as "already connected," and BLE stays silently dead until Home Assistant is restarted. It stayed invisible because the cloud path carried on and the monotonic union held the full sensor set, so entity-wise nothing looked wrong. This build adds a **BLE receive-liveness check** that does not trust `is_connected`: if BLE claims connected but no BLE frame has arrived for ~60 s **while data is still flowing over another transport** (the SCU is provably awake, witnessed by the cloud), the link is treated as dead — BLE is torn down and reconnected on the next watchdog tick. The "data still arriving" gate means a genuine 12 V-off standby (both transports silent) does not churn reconnects. This directly fixes the case Stefan reported where a deliberate `bluetoothctl disconnect` left BLE dead for 15 minutes with no reaction.
- **BETA — the stale-channel recovery hint is now correct for Home Assistant OS.** The 2.90.0b1 log/message recommended `systemctl restart bluetooth`, which **does not exist on Home Assistant OS** (the SSH add-on has host D-Bus but no host systemd or Supervisor access, so the `bluetoothd` daemon can only be recycled by a full host reboot). The messages now say: reboot the host (required on HAOS) or, on Supervised/Proxmox/container installs with a real host shell, restart the bluetooth service. Thanks to Stefan for measuring this on HAOS.

Everything from 2.90.0b1 (stale-write-channel hard failure + escalating backoff, conditional MTU-23 log, "BLE degraded" diagnostic sensor) and from the stable 2.89.0 (#23) is included. As always after updating, **restart** Home Assistant.

## [2.90.0b1] - 2026-08-26 (pre-release)

### Fixed

- **BETA — a stale BlueZ write/notify channel is now detected as a hard failure and surfaced, instead of silently degrading to MTU 23 and retrying identically forever (#24).** After an external BLE disconnect (a phone app claiming the SCU, an adapter reset, a BlueZ hiccup, or a deliberate `bluetoothctl disconnect`), the host's BlueZ daemon can keep a leaked `[org.bluez.Error.NotPermitted] Write acquired` acquisition. The link then reconnects onto that same stale channel: the MTU stays at the 23-byte default, the TLS handshake never completes, and every write fails — yet the connect “succeeded,” so the integration kept re-attempting an identical reconnect on the ~30 s watchdog indefinitely. The leaked file descriptors live in the `bluetoothd` daemon, so neither a Home Assistant restart nor a `bluetoothctl power off/on` releases them — only `systemctl restart bluetooth` or a host reboot does. This build: (1) treats “fresh GATT session still comes back with MTU 23 + `Write acquired`” as a **hard failure** and backs off on an escalating schedule (2—15 min) instead of hammering every 30 s; (2) makes the reassuring “MTU 23 is normal and does not affect functionality” log line **conditional** — it is suppressed when the low MTU was caused by a stale acquisition (the link is actually dead), and an honest warning with the `systemctl restart bluetooth` recovery hint is logged instead; and (3) adds a diagnostic **`binary_sensor` “BLE degraded”** (device class *problem*) that turns on while the write channel is stale, with the reason in its attributes, so the condition is visible without reading the debug log. The cloud/SignalR path is unaffected throughout, and a genuinely benign MTU-23 link (e.g. a BLE proxy that never grants a larger MTU) is unchanged — the new handling only triggers on an actual leaked acquisition. This is a **pre-release for testing on the affected setup**; it also contains everything from 2.89.0 (the #23 habitation-entity fix). Reproduce with `bluetoothctl disconnect <SCU>` and watch for the new “BLE degraded” sensor and warning; feedback welcome on #24. As always after updating, **restart** Home Assistant.
## [2.89.1] - 2026-08-26

### Fixed

- **BLE connections no longer fail with "never seen by any scanner" when the stored SCU address contains lower-case letters.** Home Assistant records Bluetooth advertisements under the upper-case MAC address emitted by the scanner. A manually entered or imported mixed-case address therefore did not match, even while the SCU was visible and connectable in Home Assistant's Advertisements view. SCU addresses are now normalized to upper case when entered, read, connected, or used to clear a bond. Existing configurations are corrected automatically after updating and restarting; reconfiguration is not required.

## [2.89.0] - 2026-08-26

### Fixed

- **Habitation entities no longer stay `unavailable` after a restart when the BLE direct path is enabled at startup (#23).** On retrofit SCUs (confirmed on a Schaudt EBL 400 / PIA 0.32.0-rc.2, HYMER B-ML I 780), a plain Home Assistant restart with BLE enabled left the water pump, 12 V main switch, shoreline, fresh-water level and the Dometic S10 selects stuck `unavailable` — and they never recovered until BLE was disabled, the cloud settled, and BLE re-enabled. On-vehicle debugging traced it to a firmware behaviour: those habitation **control** slots arrive only over the **cloud/SignalR** path, and while a BLE session is connected the SCU withholds them from the cloud channel — so at a BLE-first restart the cloud never delivers them before the entity platforms run their discovery pass, and the gated entities are never created. The fix has three parts, all add-only and self-guarding: (1) the merged sensor set is now a **monotonic union persisted at the config-entry level**, so a slot seen from either transport is never dropped and survives a coordinator rebuild; (2) a **cold-start cloud-first gate** defers the first BLE connection at a restart until the cloud snapshot has **plateaued** (stopped growing) — automating the known cloud-first-then-BLE workaround so the cloud-only slots reach the union before BLE claims the link; and (3) a startup **warm-up re-observe** re-runs discovery as belt-and-braces. **Non-retrofit vehicles are unaffected in substance** — they receive the full set over BLE anyway, so BLE simply comes up a few seconds later with the identical set and nothing is lost. The gate only ever delays BLE connect attempts: **cloud-only deployments and the cloud/SignalR path are untouched**, and an off-grid restart with no cloud releases BLE after a short window instead of waiting. Confirmed fixed on-vehicle (full 194-slot set, complete entity list) across host reboots and plain restarts. As always after updating, **restart** Home Assistant.
- **Slow-moving sensors keep their last known value across a restart** (fuel level/consumption/range, battery state-of-charge, all water-tank levels, odometer, distance-to-service, tyre pressures) instead of briefly showing *unknown* until fresh data arrives. Live data always wins. (Carried over from 2.88.0.)

## [2.89.0b4] - 2026-08-26 (pre-release)

### Changed

- **BETA (precautionary) — the cold-start cloud-first gate no longer delays the BLE path at an off-grid restart.** The 2.89.0b3 gate used a single 45 s cap for “cloud is slow” and “cloud is absent,” which meant a normal SCU parked **without LTE/internet** would wait up to ~45 s for its BLE link after a Home Assistant restart. The gate is now split into two cases: when the cloud **is** connecting it waits ~20 s for the snapshot to land (so the retrofit cloud-only habitation slots seed the union first — the #23 fix); when there is **no cloud at all** it releases BLE after ~20 s instead of waiting for a cloud that isn’t coming. Net effect for the fleet: the off-grid BLE delay drops from ~45 s to ~20 s, and retrofit-on-a-slow-link is actually more robust because the settle window is measured from cloud-connect rather than from startup. Everything else from the 2.89.0b1–b3 line is unchanged (monotonic union, warm-up re-observe, cold-start gate). Note: this is a **precautionary** refinement for the off-grid edge case; full offline availability of learned habitation entities with **no cloud at all** at a cold start is a larger resilience change still to come. As always after updating, **restart** Home Assistant.

## [2.89.0b3] - 2026-08-26 (pre-release)

### Fixed

- **BETA — Habitation entities stay `unavailable` after a BLE-first restart because an active BLE link makes the SCU withhold the cloud-only control slots (#23).** Beta testing of 2.89.0b2 inverted the diagnosis. A decisive on-vehicle test (disconnecting BLE at the OS level, changing nothing else) made the merged sensor set jump from 134 to 194 and every habitation entity materialise — on the cloud path alone, with BLE staying down. So the bus-2 controls (water pump, 12 V main, fresh-water, shoreline) and the Dometic S10 selects on bus 9 are **not** delivered over BLE at all on the affected firmware (Schaudt EBL 400 / PIA 0.32.0-rc.2); they come from the cloud/SignalR path, and while the BLE direct path is connected the SCU withholds them from the cloud channel. That is why the previous warm-up could not help and why the known workaround (start cloud-only, then enable BLE) always worked. This build adds a **cold-start cloud-first gate**: at a restart the first BLE connection is deferred until the cloud snapshot has landed (or a short grace cap), so the cloud-only slots reach the monotonic union before BLE claims the link — exactly automating the working workaround. The gate **latches open after startup**, so in-session BLE reconnects and the manual BLE toggle are never delayed. Non-retrofit vehicles that already receive the full set over BLE are unaffected in substance — the cloud seeds within seconds, so BLE simply comes up a few seconds later with the identical set and nothing is lost; a hard grace cap guarantees BLE still comes up even with no cloud connection. The 2.89.0b2 monotonic union stays in (it is what keeps those slots once seen), as does the b1 warm-up. This is a **pre-release for re-testing on the affected firmware**; feedback welcome on #23. As always after updating, **restart** Home Assistant.

## [2.89.0b2] - 2026-08-26 (pre-release)

### Fixed

- **BETA — Habitation entities still stay `unavailable` after a BLE-first restart because the merged sensor set is wiped when the cloud connects (#23).** Beta testing of 2.89.0b1 on the affected firmware (Schaudt EBL 400 / PIA 0.32.0-rc.2) showed the startup warm-up fires correctly but cannot fix the problem on its own: the debug log revealed the coordinator's merged sensor set goes **194 → 0 → 133** the moment the SignalR cloud connects — the full BLE snapshot is not just shrunk, it is emptied. Because the merge only ever *adds* keys and never clears them, a drop to zero can only mean the coordinator instance is rebuilt mid-startup (a config-entry re-setup), and the fresh instance starts with an empty set, discards the rich 194-slot BLE snapshot, and fills only the ~133 keys the cloud reports. The one-shot habitation control slots (water pump, 12 V main, fresh-water, shoreline, Dometic S10) live only in that discarded BLE snapshot, so they are never seen when discovery runs — warm-up or not. This build makes the merged sensor set a **monotonic union persisted at the config-entry level**: it survives a coordinator rebuild (a fresh coordinator re-seeds the accumulated set instead of starting empty) and a slot seen from either transport is never dropped when the other connects, so the 194-slot BLE set is intact when discovery runs and the habitation entities materialise. The 2.89.0b1 warm-up re-observe is kept as belt-and-braces. The persisted set is cleared when the integration is removed, and is in-memory only (a real Home Assistant restart still starts fresh). This is a **pre-release for re-testing on the affected firmware**; feedback welcome on #23. As always after updating, **restart** Home Assistant.

## [2.89.0b1] - 2026-08-26 (pre-release)

### Fixed

- **BETA — Gated habitation entities stay `unavailable` after a restart when BLE is enabled at startup (#23).** On some SCUs (confirmed on a Schaudt EBL 400 / PIA 0.32.0-rc.2 retrofit), a plain restart with the BLE direct path already enabled left the water pump, 12 V main switch, shoreline, fresh-water level and the Dometic S10 selects stuck `unavailable`, and they never recovered — only disabling BLE, letting the cloud settle, then re-enabling BLE brought them back. Root cause (confirmed from a debug log): those habitation **control** slots are delivered exactly once, in the initial BLE subscription+refresh snapshot, and the SCU never re-pushes them; if the coordinator's sensor set is reset around the moment the entity platforms attach their discovery listeners, the discovery pass runs against a set that no longer contains those one-shot slots, so the gated entities are never created and there is no second delivery to recover from. This build adds a **startup warm-up re-observe**: shortly after the platforms are set up (at ~15/45/90 s) the integration re-issues the BLE subscription+refresh burst and forces a coordinator refresh, so discovery re-runs against the full accumulated set and materialises any habitation entity that missed the initial window. Add-only and self-guarding — no effect on vehicles that already come up cleanly. This is a **pre-release for testing on the affected firmware**; feedback welcome on #23. As always after updating, **restart** Home Assistant.

## [2.88.0] - 2026-08-26

### Added

- **Slow-moving sensors now keep their last known value across a Home Assistant restart instead of showing "unknown" until fresh data arrives (add-only).** After a restart or integration reload the SCU snapshot is empty for a moment, so parked-relevant sensors used to fall back to *unknown*/*Unbekannt* — and computed fuel consumption needed a fresh ~5 km drive to repopulate. These sensors now restore their last value from Home Assistant's state store on startup and display it until the SCU pushes a new reading. **Live data always wins** — the restored value is only a placeholder for the brief "no data yet" window and is overwritten the moment the SCU reports again, so a draining battery or emptying tank still tracks in real time. Restore is enabled for: **fuel level (L), fuel consumption (L/100 km), estimated range**, **battery state-of-charge** (lithium/BOS, body/habitation, Cerbo), **all fresh/grey/black/waste water tank levels** (Philippi, CBE, AD100, TEB310D, SeeLevel, Thetford, EBL400), **odometer** and **distance-to-service**, and the **HYMER Smart System tyre-pressure** accessory sensors. Fast-moving live values (instantaneous power/current) and clocks were deliberately left non-restoring because a stale reading there would mislead. No entity was renamed and nothing new is created — this only changes how existing sensors behave right after startup. As always after a HACS update, **restart** Home Assistant.

## [2.87.0] - 2026-08-25

### Added

- **Two Dometic compressor-fridge diagnostic sensors (bus 60), verified against the decompiled EHG app bundle.** The `Fan1Available` / `Fan2Available` capability flags (slots 14/15, `bool r`) were already decoded internally but not surfaced; they are now diagnostic binary sensors (`dometic_fan_1_available` / `dometic_fan_2_available`), observation-gated on bus 60. All 21 bus-60 slots were re-verified slot-by-slot against the app's own single-line records (Dan Simms' extraction methodology). The remaining unsurfaced slots stay **decode-only** on purpose: the write-only command flags (`Lock`, `Sync`, `CMode`, `NewProtocol`) have no readback so they can't be observation-gated, and `Page` / `Heartbeat` / `Lstat` / `RChange` / `LChange` are internal or opaque with no user value. Existing entities are unaffected; the two new sensors only appear on vehicles that report bus 60. `docs/sensor-map.md` updated to document this accurately.

## [2.86.0] - 2026-08-25

### Added

- **Full climate + boiler + energy controls for the Truma Combi E (bus 6) and gas Combi (bus 31), gated and add-only.** These two Truma Combi variants now get proper control entities — a **thermostat** (target air temperature), a **boiler-mode select** (Off/ECO/HOT), and, for the Combi E (which has the 230 V electric element), a **heater-energy select** — reusing the exact same battle-tested driver classes as the already-shipped Combi D (bus 57) and Combi DE (bus 58) profiles. No new code paths: the integration already discovers any `truma_heater_*` profile generically, so bus 6 and bus 31 slot in cleanly. The slot layout is confirmed identical to bus 57/58 — the bus-6/31 diagnostic slots mapped in v2.82.0 (panel-busy 7, error 10, response-error 12, shoreline 13, window 14) line up 1:1 with bus 57. Also adds the backing readback sensors (energy source, water mode, setpoint, electric power). Each profile is `require_observed` and mutually exclusive in practice (a vehicle reports one Truma bus), so **existing vehicles and entity IDs are unaffected** — no entity was renamed. The **write paths are UNVERIFIED** on-vehicle (no bus-6/31 owner has confirmed yet) and are flagged as test controls in the map. As always after a HACS update, **restart** Home Assistant.

## [2.85.0] - 2026-08-25

### Added

- **Writable cooling controls for the Vitrifrigo (bus 103) and Thetford T2095 (bus 106) compressor fridges (gated, add-only).** Building on the v2.82.0/v2.83.0 read-sensor work, these two compressor fridges now get full control entities mirroring the confirmed Dometic/Thetford driver pattern: an **Off/1-5 cooling-step select** (routes "Off" to `FridgeOn=false`, and any level turns power on then sets the step via the slot-1-then-slot-3 dance), plus power/level readback sensors and — for the Vitrifrigo — a **fridge mode select** (`NORMAL`/`TURBO`/`NIGHT`/`SILENT`, the EHG wire values). Slot ground truth (FridgeOn = slot 1 bool, level = slot 3 int, mode = slot 2 string) comes from the decompiled EHG app catalog; enum values come from Dan Simms' metadata overlay. The **write paths are UNVERIFIED** on-vehicle (no owner has confirmed yet) and are flagged as test controls in the map. Every entity is `require_observed` (created only when your vehicle reports that bus), so **existing vehicles and entity IDs are unaffected** — no entity was renamed. As always after a HACS update, **restart** Home Assistant.

## [2.84.0] - 2026-08-25

### Fixed

- **Reconfigure can now reliably re-pair over BLE after moving Home Assistant to a new host.** When a Home Assistant backup is restored onto a freshly installed Raspberry Pi (or any new host), the config entry keeps working over the cloud, but the OS-level Bluetooth bond does not survive — the new host is unbonded, so the local BLE path stays down. Re-pairing through **Reconfigure** silently did nothing: the EHG token field is pre-filled with the stored token, and because an empty field means "keep the current value", the pairing step was skipped and the dialog returned "reconfigure successful" within a fraction of a second, with the old token still in place. There was no data-loss bug (the token was never actively restored, just never cleared), but there was also no dependable way to force a fresh bond from the Reconfigure dialog. Added an explicit **"Re-pair over BLE (mint a new EHG token)"** checkbox to Reconfigure: tick it, leave the pre-filled token as-is, press **CONNECTION** on the SCU, and submit within ~25 seconds — the pre-filled token is ignored, the BLE pairing progress dialog runs (up to ~2 minutes), and a fresh EHG token is minted and stored. The old token is preserved if pairing fails, so the cloud connection is never broken by an unsuccessful attempt. A QR activation token (entered now or already stored) is required and validated up front. As always after a HACS update, **restart** Home Assistant.

## [2.83.0] - 2026-08-25

### Added

- **89 more gated read sensors across 20 components, straight from the decompiled EHG catalog (add-only).** Newly surfaced observation-gated read entities for previously unmapped or partially-mapped components: Truma Aventa Comfort / Comfort Direct / Aventa 2G A/C (buses 7/65/123), Teleco Flatsat + TenHaaft satellite extras (33/10), CBE PL50 water + sensor + battery-info modules (53/87/54/55), Philippi black-water sensor (35), PD1600 inverter (92), Modulus power hub (98), BOS habitation battery (111), AD100 / AD100-no-pump / Teleco TEB310D habitation controllers (110/122/126), the inflatable-roof controller (101), shoreline (112), and single-slot gaps on Alde (5) and SeeLevel (91). Names follow existing family conventions (`aventa_*`, `sat_*`, `cbe_water_*`, `battery_bos_*`, `ad100_*`, …). Every entity is `require_observed` (created only when your vehicle reports that bus), so **existing vehicles and entity IDs are unaffected** — no entity was renamed. Martin's SIU smart-sensor range (buses 70–77) and opaque unknown components were deliberately left untouched. Writable controls follow later. As always after a HACS update, **restart** Home Assistant.

## [2.82.0] - 2026-08-25

### Added

- **More appliance read sensors from the decompiled EHG catalog (gated, add-only).** New observation-gated read entities for previously unmapped components: **Truma Combi (gas, bus 31)** and **Truma Combi E (bus 6)** diagnostics (panel-busy, combi/response error, shoreline-connected, window-switch), the **Vitrifrigo fridge (bus 103)** warning, the **Thetford T2095 fridge (bus 106)** door/warning/supply-voltage, and the two missing **Thetford N4000 absorber fridge (bus 32)** slots (warning + automatic-mode). Every entity is `require_observed` (created only when your vehicle reports that bus), so **existing vehicles and entity IDs are unaffected**. Ground truth = the decompiled EHG app slot catalog; writable controls for these components follow in a later release. As always after a HACS update, **restart** Home Assistant.

## [2.81.0] - 2026-08-25

### Changed

- **12V-dependent entities (lights, water pump) grey out much faster after 12V main is switched off.** Cutting habitation 12V does not power the SCU down — it freezes the `main_switch` readback at `"On"` and simply stops streaming — so data-silence is the reliable "12V off" signal. That silence threshold was previously coupled to the 3-minute SignalR reconnect cap (`STALE_DATA_TIMEOUT`), so the dashboard took up to ~3 min to strike the entities through. It is now a dedicated, **transport-aware** threshold: **~15 s in BLE-only mode** (sub-second stream cadence makes silence conclusive quickly) and **~60 s when the cloud is involved** (cloud pushes can gap ~30–40 s, so headroom avoids false flicker). When the SCU does report `main_switch = "Off"` directly (e.g. switching 12V off from HA), the entities still go unavailable within the readback latency (BLE ~200–300 ms). The 3-minute constant is unchanged for SignalR reconnect health.

No entity IDs change and no configuration migration is required. As always after a HACS update, **restart** Home Assistant.

## [2.80.2] - 2026-08-25

### Fixed

- **Lights and the water pump no longer go `unavailable` in BLE mode on vehicles without a bus-3 habitation controller** (#20, [@stbcgn](https://github.com/stbcgn)). The 12V-off availability guard hid any entity whose `main_switch` reading was not the string `"On"`. On vehicles like the B-ML I 780 (EBL 400 on bus 2, no bus-3 controller) `main_switch` is never populated over the cloud, but the BLE slot discovery surfaces a **phantom** raw bus-3 value (int `1`), which `str(main) != "On"` misread as "12V off". The guard now trusts only a properly mapped `main_switch` **string** (`"Off"` = hide); phantom raw values are ignored. Genuine 12V-off is still caught by the transport-agnostic data-silence check added in v2.76.6, so nothing is lost on vehicles that do report a real main switch.
- **Disabling the BLE direct path now tears the link down immediately**, instead of leaving it connected until a manual reload (#20, [@stbcgn](https://github.com/stbcgn)). The option toggle was wired for the enable direction only; it now mirrors both ways (enable → immediate connect, disable → immediate `stop_ble()`).

No entity IDs change and no configuration migration is required. As always after a HACS update, **restart** Home Assistant.

## [2.80.1] - 2026-08-25

### Changed

- **BLE `wakeScuUp` is now sent only as a handshake-recovery step, not on every connect.** v2.79.0 added the EHG `wakeScuUp` nudge (`0x0A` → SCU POWER_CONTROL) before every TLS handshake. It is now issued **only after a first handshake attempt stalls** with `Timed out waiting for SCU BLE data`, then the handshake is retried once. A healthy, already-awake SCU completes on the first attempt and never receives the extra write, so the common path is unchanged; the wake is reserved for the genuine standby case it was meant to address.

No entity IDs change and no configuration migration is required. As always after a HACS update, **restart** Home Assistant.

## [2.80.0] - 2026-08-25

### Added

- **Dual-zone Airxcel A/C thermostat (`climate`) entities.** The Airxcel AC Gateway (bus 95) now exposes a **front** and a **rear** Home Assistant climate card with **separate heat/cool target temperatures**: HVAC mode (Off/Cool/Heat/Heat-Cool/Fan), a target-temperature range in the Heat-Cool mode (low = heat, high = cool), current/ambient temperature, and a combined fan mode (Auto + Low/Med/High). Enum wire values match the decompiled EHG app (`AIRXCEL_AC_MODES` / `AIRXCEL_FAN_SPEEDS`).
- **Timberline air-heater thermostat (`climate`) entity.** The Timberline zone heater (bus 125) now exposes a climate card driven by its int-enum mode slot (Off/Heat/Fan) plus target + ambient temperature.

Both build on the slot-level controls shipped in v2.77.0 and complete the aggregated climate cards begun in v2.78.0 (Teleco/Saphir). Every entity is **observation-gated** (created only when the vehicle reports the bus), so existing vehicles are unaffected and no entity IDs change. **Write paths are UNVERIFIED** test controls until confirmed on-vehicle; the granular per-slot select/number controls remain available. As always after a HACS update, **restart** Home Assistant.

## [2.79.0] - 2026-08-25

### Fixed

- **BLE: clean connect but the SCU never answers (`Timed out waiting for SCU BLE data`).** On some hosts the BLE link connects and negotiates full MTU, then the TLS handshake stalls for the full 20s timeout because the SCU accepted the GATT connection while its NUS/TLS stack was still asleep — so the ClientHello was silently dropped. After ~11 such failures the retry backs off to 15 minutes, so BLE effectively stays down until a reconnect happens to land while the SCU is awake. The integration now sends the EHG app's **`wakeScuUp`** nudge (a single `0x0A` byte to the SCU POWER_CONTROL characteristic) immediately before the ClientHello, so the SCU's communication stack is listening when the handshake starts. The write is best-effort — it never blocks or fails the handshake — and is harmless when the SCU is already awake, so vehicles with healthy BLE are unaffected. Look for `BLE wakeScuUp sent (0x0A → POWER_CONTROL)` followed by `BLE TLS session established` in the debug log to confirm it took effect.

No entity IDs change and no configuration migration is required. As always after a HACS update, **restart** Home Assistant.

## [2.78.0] - 2026-08-25

### Added

- **Air-conditioner thermostat (`climate`) entities for single-zone A/C units.** The Teleco Telair DualClima (bus 36), Truma Saphir Compact (bus 79) and Saphir Comfort RC (bus 89) now expose a proper Home Assistant **climate card** — HVAC mode (Off/Cool/Heat/Fan/Dry/Auto), target + current temperature, and fan mode — aggregating the slot-level controls added in v2.77.0 into one thermostat. Each entity is **observation-gated** (created only when the vehicle reports the bus) and JSON-driven from the new `climate.air_conditioners` section. Enum wire values match the decompiled EHG app (`AIRCON_MODE_OPTIONS` / `FAN_MODE_OPTIONS`); **write paths are UNVERIFIED** test controls until confirmed on-vehicle. The granular per-slot select/number controls remain available. The dual-zone Airxcel AC Gateway (bus 95, separate heat/cool targets) and the Timberline heaters (buses 124/125) keep their slot-level controls for now; aggregated climate cards for those will follow.

No entity IDs change for existing vehicles and no configuration migration is required. As always after a HACS update, **restart** Home Assistant.

## [2.77.0] - 2026-08-25

### Added

- **MaxxFan roof ventilation fan support (EHG bus 102, dual front + rear).** 12 read entities (on, dome position, roof-fan-speed state, rain sensor, device-failure, air-direction, firmware — front and rear) plus **two writable roof-fan-speed selects** (`OFF`/`LOW`/`MEDIUM`/`HIGH`). The component/slot model was recovered from the decompiled EHG app (componentId 102); the enum wire values match [@dan-simms1's](https://github.com/dan-simms1/hymer-connect-ha) `MAXXFAN_SPEEDS`. Everything is **observation-gated** (`require_observed`) so the entities are created **only** on vehicles that actually report bus 102 — **existing vehicles are unaffected**. The fan-speed **write path is UNVERIFIED** (test control): it is modelled from metadata but not yet confirmed on a MaxxFan-equipped vehicle. If you have one, please report whether setting a speed works (`Command sent over BLE (…, status=1)`), so it can be marked confirmed.
- **Garnet SeeLevel 709 tank monitor (EHG bus 91).** Read entities for primary + secondary fresh / black / grey / LPG tank levels (`%`), plus device-failure and firmware diagnostics. Read-only (no writable slots). Observation-gated.
- **Thetford iNDUS toilet (EHG bus 56) and iNDUS toilet ECO (EHG bus 127).** Read entities for grey / black / fresh tank levels, grey / flush / black cartridge levels, availability + D+/pulsing-flush/grey-reuse statuses, and notifications. Observation-gated. (The bus-56 clock/bluetooth/diagnostic write slots are intentionally not exposed.)
- **EHG SwitchPad control panel (EHG bus 109).** Mode-status / device-failure / firmware read entities, a writable **mode select** (`ON_BOARD_MODE`/`AWAY_MODE`/`SLEEP_MODE`) and three writable button-brightness **numbers**. Observation-gated. **Write paths UNVERIFIED.**
- **DellCool (EHG bus 116) and Indel B (EHG bus 118) compressor fridges.** Compressor / door / warning read entities plus writable **cooling-step** selects (Off/1–5, power+level "dance" mirroring the Dometic/Thetford drivers) and **power-mode** selects (DellCool `NORMAL_MODE`/`SILENT_MODE`/`AUTO_MODE`; Indel B `NORMAL_MODE`/`NIGHT_MODE`/`TURBO_MODE`/`NIGHT_AND_TURBO_MODE`). Observation-gated. **Write paths UNVERIFIED.**
- **Air-conditioner and modern-heater components (data-level controls).** Read sensors + writable slot-level controls (mode/fan selects, target-temperature numbers, on/off switches) for: **Teleco Telair DualClima** (EHG bus 36), **Truma Saphir Compact** (bus 79) and **Saphir Comfort RC** (bus 89), the dual-zone **Airxcel AC Gateway** (bus 95, front + rear: A/C mode, fan mode/speed, roof-fan on/off/mode/speed, airflow, dome, heat/cool target temperatures), and the **Timberline** water + zone heaters (buses 124/125: water/furnace/air-mode selects, floor/air target-temperature + hysteresis + fan-speed numbers, engine-preheat/floor-heater/storage-mode switches). All enum wire values are the SCU values from the decompiled EHG app cross-checked with [@dan-simms1's](https://github.com/dan-simms1/hymer-connect-ha) constants. Observation-gated; **write paths UNVERIFIED**. A polished **aggregated HA climate (thermostat) entity** for these A/C and heater families will follow in a separate code change; this release ships the underlying read + slot-level controls.

All component/slot models and enum wire values were recovered from the decompiled EHG app and cross-checked against [@dan-simms1's](https://github.com/dan-simms1/hymer-connect-ha) resolved constants. Every new entity is **observation-gated** — created only when your vehicle reports that bus — so **existing vehicles are unaffected** and no entity IDs change. Writable controls are marked **UNVERIFIED** (test controls) until confirmed on-vehicle. As always after a HACS update, **restart** Home Assistant.

## [2.76.8] - 2026-08-25

### Changed

- **Honest reconnect log line.** After a BLE drop the coordinator logged `BLE will be retried on next poll cycle.` — but since v2.76.7 the reliable driver is the independent watchdog, not the (push-starved) poll. The line now reads `BLE will be retried automatically.`, which is accurate regardless of which driver fires.
- **Docs:** `docs/ble-troubleshooting.md` gains a "Did my command go over BLE or the cloud?" section (the `Command sent over BLE (…, status=1)` / `Cloud command sent (… ble_connected=False)` INFO lines), a watchdog/back-off log mini-reference, and verbatim wording for two pairing-stage log lines so they are text-searchable.

No entity IDs change and no configuration migration is required. As always after a HACS update, **restart** Home Assistant (not just "Reload").

## [2.76.7] - 2026-08-25

### Changed

- **BLE now (re)connects on its own — enabling it no longer requires a reload.** BLE connect/recover lived only in the coordinator poll, which is starved whenever SignalR keeps pushing (every push reschedules the poll), so ticking *Enable BLE direct path* in Options did nothing until an explicit integration reload, and a dropped BLE link would not recover while the cloud stayed healthy. An **independent watchdog timer** now drives BLE (re)connect regardless of cloud activity, and toggling the option on kicks an **immediate** attempt. The connect logic was extracted into one re-entrant method shared by the poll, the watchdog and the option toggle. Reported alongside [#20](https://github.com/BetaHydri/hymer-connect-ha-ble/issues/20) (@stbcgn).
- **Clearer debug log.** The per-frame merge line `SignalR push: N total sensors` is renamed to `Sensor data merged (SignalR/BLE): N total sensors` — it always fired for BLE frames too (they share the merge), which had been a source of confusion when diagnosing BLE-mode issues.
- **Docs:** `docs/ble-troubleshooting.md` gains sections on the BLE-mode `unavailable` symptom (fixed in v2.76.6) and the enable-without-reload behaviour; `README.md` notes the 12V availability guard is now transport-agnostic.

No entity IDs change and no configuration migration is required. As always after a HACS update, **restart** Home Assistant (not just "Reload").

## [2.76.6] - 2026-08-25

### Fixed

- **Entities no longer go `unavailable` when the local BLE direct path is active.** The v2.76.1 12V-off availability guard (lights + `requires_12v` switches) treated "no fresh data" as 12V-off using a **SignalR-only** clock (`signalr_client.data_silence_seconds`). BLE frames flow through a shared merge (`_on_signalr_update`) but never refreshed that clock, so in `mode=ble` the guard saw growing silence and marked almost every gated entity unavailable even though ~130 sensors kept arriving over BLE. On vehicles whose habitation controller is not on bus 3 (e.g. Schaudt EBL 400 on bus 2, HYMER B-ML I 780) there is no `main_switch` readback, so the data-silence check was the *only* determinant and the whole gated surface dropped. The guard now uses a **transport-agnostic** coordinator clock that any SignalR **or** BLE frame refreshes, so BLE mode stays available while a genuine 12V-off (both transports silent) is still detected. Reported by @stbcgn ([#20](https://github.com/BetaHydri/hymer-connect-ha-ble/issues/20)); this also unblocks on-vehicle testing of the local BLE write path.

No entity IDs change and no configuration migration is required. As always after a HACS update, **restart** Home Assistant (not just "Reload").

## [2.76.5] - 2026-08-25

### Fixed

- **Integration updates/reloads no longer orphan the BLE stack (fixes "every update drops BLE until a full host reboot").** On a config-entry unload — which is exactly what a HACS update or a manual "Reload" does — the integration only stopped the SignalR/cloud connection and left the BLE side running: the `BleakClient` (with its active notify subscription) was never disconnected and its fire-and-forget listen loop kept running against the old client. That orphaned connection held the BlueZ GATT link open, so the freshly-loaded integration could not reconnect, and on USB-passthrough hosts (e.g. a Bluetooth dongle passed into a Proxmox VM) the adapter stayed wedged until a full host reboot. Unload now performs a full teardown (`async_shutdown`): it stops SignalR **and** cancels the BLE listen task and disconnects the client, guarded by a timeout so a stuck BlueZ cleanup can never block the reload. Reported by a Proxmox user (Jos).

No entity IDs change and no configuration migration is required. As always after a HACS update, **restart** Home Assistant (not just "Reload").

## [2.76.4] - 2026-08-25

### Fixed

- **BLE pairing could reject the SCU's own bond (regression in v2.76.3).** The new device-locked pairing agent compared the BlueZ device path against a suffix that upper-cased only the address hex but left `dev_` lower-case, while the path was compared fully upper-cased — so `endswith()` failed for the target SCU and the agent answered a legitimate `RequestConfirmation`/`RequestAuthorization` callback with `org.bluez.Error.Rejected`, aborting `Device1.Pair()`. The comparison is now fully case-normalised on both sides, so the SCU's own pairing callbacks are accepted and foreign devices are still rejected. Anyone who updated to v2.76.3 should update to v2.76.4 before pairing. Caught by a new local pairing unit harness.

No entity IDs change and no configuration migration is required.

## [2.76.3] - 2026-08-25

### Changed

- **More reliable BLE pairing on more hosts.** The built-in D-Bus pairing agent now also answers the **legacy PIN/passkey** callbacks (`RequestPinCode` → `"0000"`, `RequestPasskey` → `0`), which some BlueZ/adapter/SCU combinations select instead of the JustWorks `RequestConfirmation`. Previously those combinations made `Device1.Pair()` fail with an unknown-method error; the bond now completes without user interaction. The success path (JustWorks confirmation) is unchanged — the PIN/passkey handlers are pure additive fallbacks. (Independently confirmed on Dan Simms' Linux/BlueZ pairing tool.)
- **Pairing agent is now device-locked to your SCU.** The agent only answers pairing callbacks for the target SCU address; a stray device that happens to pair in the same window is rejected (`org.bluez.Error.Rejected`) instead of being auto-accepted.
- **Clearer in-app pairing instructions.** The Vehicle-activation, Reconfigure and BLE-pairing dialogs now tell you to **press CONNECTION on the SCU first, then submit within ~25 seconds** (some SCUs hold the pairing window open only ~30 s), and the Reconfigure step spells out that the pre-filled EHG token field must be **cleared** to trigger a fresh BLE re-pair. `quick-start.md` and `docs/ble-troubleshooting.md` document the device-locked agent and the legacy PIN/passkey handling.

No entity IDs change and no configuration migration is required.

## [2.76.2] - 2026-08-24

### Changed

- **Documentation + in-app help refreshed for the current (v2.70–v2.76) state — no code-logic changes.** The in-app description of the *Enable BLE direct path* option now spells out its three concrete benefits (faster ~50 ms updates, works with no internet/LTE, and — the key one — live sensors even while 12 V is OFF, since the SCU's BLE radio stays active in standby while the cloud stops pushing). `README.md` and the `docs/` set were brought up to date: the sensor-map bus index now lists all newly-mapped observation-gated buses (2/7/9/52/93/96/97/100/105/107/117/119/120) and marks interior lights as living in `lights.json`; the Alde ACC output is documented as a writable A/C switch; the stale "BLE is read-only / all writes via cloud (v2.62.24)" notes were corrected to BLE-first-with-cloud-fallback (v2.67.0+); a "cloud-only → add BLE for the full dual-path" guide and a "how the two BLE checkboxes interact (and what pairing sets)" section were added; and @stbcgn's B-ML I 780 contributions were credited.

No entity IDs change and no configuration migration is required.

## [2.76.1] - 2026-08-24

### Fixed

- **12V main switch OFF is reliable again on vehicles whose SCU does not power down with the habitation 12V (e.g. CBE EBL402 on bus 3, Grand Canyon S 600).** On these units 12V-off keeps `scu_connected` reporting `true` and freezes the `main_switch` readback at `"On"` — the SCU simply stops streaming. The switch verify logic mistook the frozen `"On"` for a dead command channel and forced a pointless full re-auth + reconnect, reverting the UI back to On. OFF is now confirmed by data-silence (no fresh frames since the command) regardless of `scu_connected`, so the switch stays Off without a reconnect storm. The 12V-ON path (60 s wake holdoff) is unchanged.
- **Lights and the water pump are shown unavailable (struck-through) again while 12V is off.** Their availability keyed only on the `main_switch` readback, which on the above vehicles freezes at `"On"` — so they never greyed out. Availability now additionally treats prolonged SCU data-silence as 12V-off. No entity IDs change.

No entity IDs change. As always after a HACS update, **restart** Home Assistant (not just “Reload”).

## [2.76.0] - 2026-08-24

### Added

- **Truma Aventa Compact air conditioner (bus 59, `aventa_compact_*`), observation-gated.** The previously-undecoded Eriba bus-59 placeholders (`ac_aventa_slot_1..8`, which created no entities) are now a proper read-only decode in `base.json`: target/room temperature, mode, fan speed, and the on-unit light + error / manual / automatic status flags. Ground truth: decompiled EHG app `componentId 59`. Distinct from the Truma Aventa Comfort on bus 7 (`aventa_*`); now available on any brand that reports bus 59.

### Changed

- **Eriba brand overlay emptied — its components migrated into the shared, brandless maps.** The shower-ambient (bus 18) and bedroom-furniture (bus 93) lights moved into the gated `lights.json` (the `light.*` entity IDs are unchanged and stay gated, so no phantoms appear on other brands; each also gains a gated status `binary_sensor` like every other mapped light). `eriba.json` is now an empty overlay. With both the HYMER and Eriba overlays empty, the sensor mapping is effectively fully brandless — every fixed EHG component lives in `base.json` / `lights.json`, observation-gated.

No entity IDs change. As always after a HACS update, **restart** Home Assistant (not just “Reload”).

## [2.75.0] - 2026-08-24

### Changed

- **HYMER brand overlay fully emptied — the remaining fixed EHG components moved into the shared, observation-gated `base.json`.** The Votronic solar (bus 8), Thetford absorber / N4000 fridges (buses 32/34/37), Truma LIM + Combi DE heater (buses 49/58), BOS BMS (bus 99), the ML-T compressor fridge (bus 114) and the habitation battery (bus 29) — plus their climate / select / switch controls — now live in `base.json`. `hymer.json` is now an empty overlay; the HYMER brand adds nothing beyond `base.json`. **Entity names/IDs are unchanged**, so existing dashboards keep working — the entities simply materialise from `base.json` now. This completes the brandless-mapping effort begun in v2.69.

### Added

- **The selected EHG brand is now shown in the integration's entry title — e.g. `HYMER Connect BLE (for HYMER)` / `HYMER Connect BLE (for Eriba)`.** New installs get the format automatically; existing installs are upgraded from the old `HYMER Connect (<Brand>)` title on restart — unless you renamed the entry yourself, in which case your name is never overwritten. The device manufacturer stays `Erwin Hymer Group`.

No entity IDs change. As always after a HACS update, **restart** Home Assistant (not just “Reload”).

## [2.74.0] - 2026-08-24

### Changed

- **Brandless `base.json` migration completed — the Alde 3030 (bus 5), TenHaaft satellite (bus 10) and Victron MultiPlus (bus 121) components moved out of the HYMER overlay into the shared, observation-gated `base.json`.** All three are fixed EHG components, so any brand that reports their bus now gets them automatically — a further step toward removing brand selection entirely. Entity names are unchanged (`alde_*`, `sat_*`, `victron_*`), so vehicles that already had them (the BMC I 680's Alde 3030 + TenHaaft dish, the S600's Victron) are byte-for-byte unaffected; the entities simply materialise from `base.json` now instead of `hymer.json`. What remains brand-tied in `hymer.json` is only the Votronic solar (bus 8), the Thetford / Truma Combi DE fridge+heater stack (buses 34/37/49/58/114/32) and the BOS BMS (bus 99).
- **Gated the habitation/body-battery state-of-charge (`body_battery_soc`, bus 29).** It was the last un-gated entity in the overlay and left a phantom “unavailable” entity on vehicles without a bus-29 battery (S600/S700/ML-T). It now honours `require_observed`; the BMC I 680 (which reports bus 29) is unchanged — on the other models Home Assistant offers the Delete action to clean up the stale entry.

No entity IDs change and no user action is required.

## [2.73.0] - 2026-08-24

### Added

- **Writable controls for the v2.71/v2.72 brandless `base.json` components — all observation-gated (created only once the vehicle reports the bus).** The read-only components mapped over the last two releases gained their writable controls:
  - **Truma Combi NEO / NEO E (bus 119/120)** — water/air mode, energy-source and air target-temperature selects + number (`truma_neo_*` / `truma_neo_e_*`).
  - **Schaudt EBL 400 (bus 2)** — 12 V main switch, water-pump switch, battery-type select and leisure-battery-capacity number (`ebl400_*`).
  - **CBE PL50 (bus 52)** — seven tri-state output switches: interior/exterior/switched lights, water pump, EIS/EX, multimedia and remote on/off (`cbe_pl50_*`).
  - **Dometic Series 10 absorber fridge (bus 9)** — power+cooling-step and power-source selects (`dometic_s10_*`).
- **ZipDee power awning (bus 107) as a `cover` entity (`awning`), observation-gated.** A new JSON-driven `cover` platform: momentary open/close, stop, 0–100 position and a user-lock select. The tilt slots are intentionally not exposed (they map to no standard Home Assistant entity).
- **Truma Aventa Comfort air-conditioner read sensors (bus 7 = `TrumaAventaComfort`), observation-gated (`aventa_*`).** Setpoint, operating mode and fan speed, read-only — the SCU mirrors the Aventa and silently drops writes to bus 7. Distinct from the Truma Aventa Compact on bus 59 (Eriba). Contributed via [#18](https://github.com/BetaHydri/hymer-connect-ha-ble/pull/18) (@stbcgn, HYMER B-ML I 780).
- **Alde 3030 ACC output as a writable A/C switch (`alde_acc_ctrl`, bus 5 slot 11).** On a HYMER B-ML I 780 this output switches the air conditioning (the bus-7 Aventa), which then regulates to the setpoint entered at the Alde panel; the slot was previously mapped read-only with an unknown function. Contributed via [#18](https://github.com/BetaHydri/hymer-connect-ha-ble/pull/18) (@stbcgn).

### Changed

- **Several write paths confirmed on a second vehicle (HYMER B-ML I 780, @stbcgn / [#18](https://github.com/BetaHydri/hymer-connect-ha-ble/pull/18)) — the `WRITE PATH UNVERIFIED` caveat was dropped.** The Alde 3030 (bus 5) heating/gas switches, the target-temperature number, and the energy-priority and hot-water selects; the TenHaaft satellite-position select (bus 10); and the `base.json` EBL 400 12 V/pump switches (bus 2) and Dometic Series 10 fridge selects (bus 9) are now confirmed on-vehicle.

> The remaining new writable controls without an on-vehicle confirmation (Truma NEO, CBE PL50, the EBL 400 battery-type/capacity, and the awning) stay marked `WRITE PATH UNVERIFIED` — they are risk-free because each is observation-gated and only materialises on a vehicle that reports the bus.

## [2.72.0] - 2026-08-24

### Added

- **Intelligent Battery Sensor / IBS (bus 105), observation-gated (`ibs_*`).** The EHG `SmartBatterySensor` battery monitor: voltage, current, temperature, state-of-charge, state-of-health, available capacity and time-remaining ship as enabled measurements; the deep battery-table state and tolerance/capacity-loss fields are `entity_category: diagnostic`. Named `ibs_*` to stay distinct from the BOS BMS on bus 99 (`bms_*`). It materialises only on vehicles that report bus 105.
- **Truma Combi NEO (bus 119, `truma_neo_*`) and Combi NEO E (bus 120, `truma_neo_e_*`), observation-gated.** The CP Plus NEO combi water+air heater in its two variants, exposed read-only for now: shower/heat-up times and the current room temperature as enabled measurements, the 230 V / window-switch / manual-mode / device-error status flags as diagnostic sensors. The two NEO variants use separate prefixes (they share the same slot layout and a vehicle reports only one); both stay distinct from the legacy Combi on bus 58 (`heater_*`) and the Combi D on bus 57 (`heater_d_*`).
- **Schaudt EBL 400 (bus 2, `ebl400_*`) and CBE PL50 (bus 52, `cbe_pl50_*`) habitation controllers, observation-gated.** Two more habitation controllers besides the EBL 402 on bus 3 (which uses the bare canonical names in `base.json`). EBL 400: living/starter battery voltage + current, fresh/waste water levels, water-sensor-failure and shoreline. CBE PL50: 230 V / ignition / solar / D+ signals and the leisure/vehicle battery status flags. A vehicle has exactly one habitation controller, so the `ebl400_*` / `cbe_pl50_*` qualifiers avoid any collision with the bus-3 names, and gating means only the present controller materialises.

All new entities are read-only and metadata-derived (decompiled EHG app + `tools/ehg_metadata.json`); the corresponding writable controls (NEO climate/selects, habitation switches) are intentionally deferred. This completes mapping of the documented EHG component set into the shared, brandless `base.json`.

## [2.71.0] - 2026-08-24

### Added

- **Entity naming convention codified in `base.json` (the `_naming` block).** Documents the scheme `<group>[_<qualifier>]_<function>` (group = a function domain such as `fridge`/`heater`/`solar`, or a brand when the brand is the identity such as `dometic`/`victron`; never the EHG componentId or the bus number) plus a FROZEN rule: a shipped entity name is never renamed (that would break user dashboards) — corrections are made by adding a new alias. This guides all the new brandless components below.
- **Victron Cerbo GX (bus 97) added to `base.json`, observation-gated (`cerbo_*`).** LPG bottle levels, battery voltage/current/SoC and solar voltage/current materialise only on vehicles that report bus 97 (distinct from the Victron inverter on bus 121). Battery time-remaining (the EHG `%` unit is bogus) and solar power (implausible max) ship `enabled: false` with the scale marked UNVERIFIED until a Cerbo-equipped vehicle confirms. Ground truth: `tools/ehg_metadata.json`.
- **Dometic Series 10 absorber fridge (bus 9), observation-gated (`dometic_s10_*`).** Power/mode/level readbacks plus busy / door / temperature-state / energy-source / warning sensors. A different product from the Dometic *compressor* fridge on bus 60 (`dometic_*`, confirmed on-vehicle by owner 'Jos'), hence the `s10` qualifier. Metadata-derived and UNVERIFIED until a Series-10 owner confirms — but risk-free: it materialises only when the vehicle reports bus 9.
- **ZipDee power awning (bus 107), observation-gated (`awning_*`).** Status plus front/rear tilt binary sensors; the writable open/close/tilt/position controls are left for a future cover entity.
- **CBE solar charger (bus 117), observation-gated (`solar_cbe_*`).** Active flag, voltage and current — kept distinct from the Votronic MPPT `solar_*` on bus 8.
- **BatteryGuard 1000 DC power guard (bus 96), observation-gated (`battery_guard_*`).** Low-battery warning, device-failure and firmware sensors; the writable DC-disconnects are left for a future switch.
- **Truma Combi D (bus 57) support — the diesel-only Combi heater/boiler, observation-gated in `base.json`.** Some HYMER (and other EHG-brand) vehicles carry the **Truma Combi D** (`TrumaCombi_D`, diesel-only), which the SCU reports on **bus 57** — not the **Truma Combi DE** (`TrumaCombi_DE`, diesel+electric) on **bus 58** that the integration mapped until now. On those vehicles every heater entity stayed `unavailable` because bus 58 never produced data. This release adds a second, independent observation-gated Truma profile (`climate.truma_heater_d`): the climate thermostat and the Off/ECO/Turbo boiler select (reusing the existing `HymerHeaterClimate` / `HymerBoilerSelect` classes) plus the bus-57 read sensors and binary sensors. The two profiles are mutually exclusive — a vehicle materialises whichever bus it actually reports — so bus-58 vehicles (e.g. the Grand Canyon S 600 / S 700) are completely unchanged. Because the Combi D is diesel-only (it has no electric-power slot), the Diesel/Mix/Electric energy select is intentionally not offered for it. Ground truth: decompiled EHG app `componentId 57` + `tools/ehg_metadata.json`.

### Changed

- **Interior lights moved from `hymer.json` into a shared, observation-gated `lights.json` — a step toward removing brand selection.** All `light_*` status sensors and the controllable `light.` entities now load for every brand but materialise only once their bus is reported (new gating in `light.py`, mirroring the proven `switch` pattern), so no phantom lights appear on brands/floorplans without a given circuit. Entity names are byte-for-byte unchanged, so existing dashboards keep working. The bus→room names are a per-floorplan best-guess, adjustable via the Home Assistant rename UI or `lights.json`; a later release will read the labels dynamically from the SCU and make the hardcoded names obsolete.
- **Bus-57 heater entities use a `heater_d_*` naming qualifier** (e.g. `sensor.*_heater_d_setpoint`, `sensor.*_heater_d_operating_mode`, the `Diesel heater …` binary sensors) so they never collide with the bus-58 `heater_*` entities. Owners of a Truma Combi D whose heater entities were previously stuck `unavailable` (bus 58) will now get these **new** `heater_d_*` entities once the vehicle reports bus 57 — they must **repoint any dashboard cards and automations** to the new entity IDs. The climate thermostat (`climate.*_truma_heater`) and the boiler select keep their existing entity IDs (only one Truma profile is ever created per vehicle).

## [2.70.0] - 2026-08-24

### Added

- **Collision lint (`tools/_test_collision_lint.py`) - canonicity guardrail toward brandless auto-mapping.** Asserts (A) the same `bus,slot` is never defined with a different name across map files and (B) no entity name maps to multiple slots within a resolved brand. This protects the growing `base.json` as more components are moved there.
- **TPMS component (bus 100) added to `base.json`, observation-gated.** The EHG factory TPMS (fixed wheel slots front/back left/right + spare) materialises only on vehicles that report bus 100. Temperatures/status/firmware are enabled (unambiguous decode); tyre pressures ship `enabled: false` with the scale marked UNVERIFIED (the raw `0-9500 psi` range implies a scaled unit) until a TPMS-equipped vehicle confirms the transform. This is distinct from the aftermarket HYMER Smart tyre sensors on bus 70.

### Changed

- **HYMER Smart Sensors (bus 70 tyre / 71 gas / 73 contact / 74 temperature) and the fine wired water levels (bus 76: `fresh_water_level` / `gray_water_level`) moved from `hymer.json` into `base.json`.** These are fixed EHG mechanisms (auto-slot via PIA field 10, and the pin-6/pin-7 discriminators) that are canonical across brands, so any brand with these paired sensors now gets them automatically - a step toward removing brand selection. The HSS auto-slot templates are inherently observation-gated (the `{n}` template is never a concrete entity).
- **Fixed a latent phantom: the fine water levels on bus 76 are now `require_observed`.** Previously they were created unconditionally, so on vehicles that only have the coarse EBL levels (bus 3, e.g. the Grand Canyon S600) `fresh_water_level` / `gray_water_level` lingered as phantom "unavailable" entities next to the real `*_ebl` values. They are now created only when the vehicle actually reports the bus-76 sensors; on vehicles without them Home Assistant offers the Delete action to clean up the stale entries.

## [2.69.4] - 2026-08-24

### Added

- **Gating completeness lint (`tools/_test_gating_completeness.py`).** A new regression check derives the optional-appliance universe from the decompiled EHG metadata table (`docs/ehg-app-metadata.md`) and asserts that every mapped appliance entity carries `require_observed`. Almost every appliance category has several mutually-exclusive hardware variants (9 fridges, ~8 heaters, ~8 ACs, 3 BMS, 4 tank monitors, ...) but a vehicle carries only one per category, so an un-gated appliance entity is a guaranteed phantom on every other variant. The lint is the enforcement that makes the eventual move of components into the universal `base.json` (brandless auto-mapping) safe. Universal always-present kinds (chassis / SCU / vehicle info) and naming-variable kinds (lights / habitation) are intentionally not enforced.

### Changed

- **Observation gating extended to the remaining mapped HYMER appliances flagged by the new lint: Votronic MPP250 solar charger (bus 8), BOS Connect battery monitor (bus 99) and Victron MultiPlus inverter/charger (bus 121).** All 25 read sensors/binaries on these three buses now honour `require_observed`, so on a HYMER vehicle with a different BMS/inverter (or no solar) they are no longer provided instead of lingering as phantom entities. Vehicles that actually report these buses (e.g. the S600 with Votronic + BOS + Victron) are unchanged.

## [2.69.3] - 2026-08-24

### Added

- **Observation gating now covered by the `number` platform too.** The `number` platform learned the same `require_observed` deferral the sensor/binary_sensor/select/switch/climate platforms already had: a gated number slot is created only once its backing read sensor is reported, then materialised via a coordinator listener. This lets the Alde setpoint sliders be gated (see below).

### Changed

- **Observation gating extended to the remaining BMC I 680-only fixed components: the absorber fridge (bus 32), the Alde 3030 heater (bus 5) and the TenHaaft satellite dish (bus 10).** These components exist only on the BMC I 680 and left phantom "unavailable" entities on every other vehicle (S600/S700/ML-T). They now honour `require_observed` and are created only once the vehicle reports one of their slots:
  - **Absorber fridge (bus 32):** read sensors `fridge_absorber_power` / `_power_mode` / `_cooling_step` / `_door` and the selects `fridge_absorber_cooling_step` / `fridge_absorber_power_mode`.
  - **Alde 3030 heater (bus 5):** all read sensors/binaries (`alde_inside_temp`, `alde_setpoint`, `alde_energy_priority`, `alde_warning`, `alde_heating_on`, `alde_heating_active`, `alde_outside_temp`, `alde_zone2_temp`, `alde_zone2_setpoint`, `alde_hot_water_mode`, `alde_electric_setting`, `alde_gas_active`, `alde_acc_setting`, `alde_error`), the `alde_heating_ctrl` / `alde_gas_ctrl` switches, the `alde_setpoint` / `alde_zone2_setpoint` number sliders and the `alde_energy_priority` / `alde_electric_boost` / `alde_hot_water` selects.
  - **TenHaaft satellite dish (bus 10):** read sensors/binaries (`sat_satellite`, `sat_status`, `sat_signal_strength`, `sat_dish_moving`, `sat_safe_position`, `sat_standby`), the `sat_control` switch and the `sat_position` select.

  On vehicles without these components the entities are no longer provided, so Home Assistant offers Delete to clean up the stale registry entries. BMC I 680 vehicles are unchanged.

## [2.69.2] - 2026-08-24

### Changed

- **Observation gating extended to the ML-T compressor fridge (bus 114).** The Thetford Compressor T2120C fridge on bus 114 (read sensors, the power/silent switches and the two stepped selects `fridge_compressor_freezer` / `fridge_compressor_cooling_step`) now honours `require_observed`: it is created only once the vehicle reports a bus-114 slot. On vehicles without it (e.g. an S600 with a Thetford absorber on bus 34/37) these entities are no longer provided, so Home Assistant offers the Delete action to clean up the stale registry entries left over from earlier versions. Vehicles with the ML-T compressor fridge are unchanged.

## [2.69.1] - 2026-08-24

### Changed

- **Observation gating extended to the dedicated Thetford fridge + Truma Combi classes and the `switch` / `climate` platforms (in-place, still in `hymer.json`).** The S600/S700 Thetford fridge (`HymerFridgeSelect`, bus 34/37) and the Truma Combi heater (`HymerBoilerSelect` / `HymerHeaterEnergySelect` / the `HymerHeaterClimate` thermostat, bus 58) plus the `fridge_eco` switch now honour `require_observed`: they are created only once the vehicle actually reports a component-specific read sensor (e.g. `fridge_power`, `heater_setpoint`), instead of unconditionally at setup. The gate watches only component-unique sensors (the generic `outside_temperature` is excluded). The Thetford (bus 34/37) and Truma (bus 49/58) read sensors + the two `CLIMATE_DEFS` blocks are marked `require_observed` in `hymer.json`. Vehicles with the component (e.g. an S600 with Thetford + Truma Combi 6E) are unchanged; vehicles without it no longer get phantom fridge/heater entities. Definitions stay in `hymer.json` (no `base.json` move yet).

## [2.69.0] - 2026-08-24

### Added

- **Observation-gated entities (`require_observed`) — entities materialise only once the vehicle actually reports their slot.** A new opt-in JSON flag `require_observed: true` (honoured by the sensor, binary_sensor and select platforms) defers entity creation until the backing `(bus, slot)` is seen in a PiaResponse frame, instead of creating it unconditionally at setup. This removes phantom "unknown" entities on vehicles that lack a component, and lets a fixed EHG component be mapped **once in `base.json`** and auto-appear on any brand that reports it — no per-brand overlay duplication needed. Entries **without** the flag are unchanged, so existing entities on all vehicles behave exactly as before.

### Changed

- **Dometic compressor fridge (bus 60) moved from `hymer.json` + `eriba.json` into `base.json`, observation-gated.** The `DometicCompressorFridge` is a fixed EHG component always bound to bus 60, so its read sensors, the door binary sensor and the two writable selects (`fridge_dometic_cooling_step`, `fridge_dometic_mode`) now live once in `base.json` with `require_observed`. Any brand that reports bus 60 (HYMER, Eriba, …) gets the full control automatically; vehicles without a Dometic fridge no longer get phantom bus-60 entities. Functionally unchanged for Jos (HYMER) and Eriba owners. Thetford (bus 32/34/114) and Truma controls are intentionally left in the brand overlays for now.

## [2.68.3] - 2026-08-24

### Added

- **Dometic compressor fridge control for the Eriba Car 602 (bus 60).** Eriba-brand vehicles load only `base.json` + `eriba.json` (never `hymer.json`), so the v2.68.0–v2.68.2 Dometic controls did not reach them. This release mirrors the two writable selects into `eriba.json`: `select.*_dometic_fridge_cooling_step` (Off / 1–5 via PowerOn slot 8 + Temperature slot 2) and `select.*_dometic_fridge_mode` (Performance Cooling / Silent Mode / Turbo Mode, slot 1). The read sensors already existed in `eriba.json` (contributed by @mvondemhagen, #54). Eriba is a HYMER Group brand running the **identical EHG SCU** and the **same `DometicCompressorFridge` component**, and the write paths were on-vehicle confirmed on a HYMER-brand vehicle, so these controls are treated as working (not marked unverified).

## [2.68.2] - 2026-08-24

### Verified

- **Bus 60 slot 1 (`UserMode`) mode-select write confirmed on-vehicle** by HYMER Dometic owner **Jos** — `select.*_dometic_fridge_mode` (Performance Cooling / Silent Mode / Turbo Mode) is no longer unverified. The SCU wire-value readback/write round-trip works; the EHG app localizes these for display (DE: Normal / Leise / Turbo). With this, the **entire Dometic bus-60 fridge control is on-vehicle verified**: power on/off (slot 8), cooling level 1–5 (slot 2), and user mode (slot 1). Options MUST stay as the SCU wire values — do not relabel to the German app strings.

## [2.68.1] - 2026-08-23

### Added

- **Dometic fridge door-open sensor (`binary_sensor.*_dometic_fridge_door`) for HYMER-brand vehicles (bus 60).** The Dometic compressor fridge has no dedicated door bool slot — the door-open state is encoded as **value 10 in the slot-16 warning enum** (`dometic_fridge_warning`, `WarningErrorInformation`). This release surfaces it as a proper `door` device-class binary sensor while keeping the raw slot-16 code as a diagnostic sensor. Confirmed on-vehicle by HYMER Dometic owner **Jos**.

### Verified

- **Bus 60 slot 8 (`PowerOn`) on/off write confirmed on-vehicle** by Jos — the **Off** branch of `select.*_dometic_fridge_cooling_step` (slot 8 `PowerOn` = false) is no longer unverified. Together with the previously confirmed cooling-level write (slot 2), the full Off/1–5 cooling-step control is now on-vehicle verified. The user-mode select (slot 1) remains unverified.

## [2.68.0] - 2026-08-23

### Added

- **Dometic compressor fridge control for HYMER-brand vehicles (bus 60).** HYMER motorhomes fitted with a Dometic compressor fridge report it on bus 60 (`DometicCompressorFridge`) — the same bus the Eriba Car 602 uses, but the `eriba.json` overlay does not load for HYMER-brand vehicles, so those owners previously saw only unnamed `bus60_*` discovery entries and had no way to control the fridge. This release adds the bus-60 Dometic read sensors **and** two writable controls to `hymer.json`:
  - **`select.*_dometic_fridge_cooling_step`** — Off / 1–5. Mirrors the Thetford drivers: writing a level first powers the fridge on (slot 8, bool) then sets the cooling level (slot 2, int 1–5); **Off** switches the fridge off via slot 8. The cooling-level write (bus 60 slot 2) was **confirmed landing on-vehicle** by a HYMER Dometic owner who had patched his own integration to the same slot.
  - **`select.*_dometic_fridge_mode`** — user mode Performance Cooling / Silent Mode / Turbo Mode (slot 1).
- The bus/slot model (slot 1 UserMode string, slot 2 Temperature int 1–5, slot 8 PowerOn bool) is the authoritative definition from the decompiled EHG app (`componentId 60`). Zero risk for S600/S700 (Thetford bus 34/37), ML-T 570 (bus 114) and BMC I 680 (bus 32), which do not use bus 60.

### Unverified

- The **Off** branch (bus 60 slot 8 `PowerOn` = false) and the **user-mode** select (slot 1) write paths are not yet confirmed on-vehicle — only the cooling-level write (slot 2) is. If the SCU drops either write, adjust or revert.

## [2.67.2] - 2026-08-23

### Added

- **One concise `INFO` line per command sent over the local BLE path**, e.g. `Command sent over BLE (send_light_command bus=21 sid=1, status=1)`. This mirrors the existing cloud `Cloud command sent (…)` line, so the **normal** log now shows *what* was actioned over BLE — not just the raw `BLE setValues ACK: request_id=… status=1` line, which lacks the entity context. Deliberately low-noise: it fires **only** on a successful BLE command (i.e. per button press), never for incoming sensor traffic. Raw `SEND`/`TX`/`PIA RECV` frames stay at `DEBUG` (they fire many times per second and would flood the log). No behavioural change.

## [2.67.1] - 2026-08-23

### Fixed

- **Transient cloud REST timeouts no longer log a scary `ERROR` traceback.** On instances that run over a mobile/LTE link (e.g. the SCU's own connection in the vehicle), the first cloud REST call at startup can occasionally time out before connectivity settles. `api._request` only wrapped `aiohttp.ClientError`, so a `TimeoutError` escaped unwrapped and surfaced via the coordinator as *"Unexpected error fetching hymer_connect data"* with a full traceback. It now also catches `TimeoutError` and raises the same `HymerConnectApiError`, so a transient timeout is handled on the normal retry path (quiet "will retry") instead of a red error. Purely cosmetic — the integration always recovered on the next update cycle; behaviour is otherwise unchanged.

## [2.67.0] - 2026-08-23

> ✅ **BLE write path verified on-vehicle and now ON by default.** The v2.66.0 write path and v2.66.2 subscription path were confirmed working on a **Grand Canyon S 600 (SCU firmware 1.13.0.0)** — every `setValues` write returned a real `BLE setValues ACK … status=1`, and the automatic cloud fallback was also observed working when the BLE TLS session dropped. The option is now **on by default** (opt-out). A fully cloud-isolated (LTE-off) confirmation is still pending, but because BLE writes only fire when BLE is connected and any un-ACKed write falls back to the cloud, the worst case remains identical to cloud-only.

### Changed

- **BLE command path is now enabled by default** (was an off-by-default opt-in in v2.66.0). When BLE is connected, commands (lights, switches, heater, fridge, etc.) go over the local BLE link first — faster and works without internet — and fall back to the cloud automatically if the SCU does not acknowledge within ~3 seconds. If BLE is not connected, everything goes via the cloud as before. The option (Settings → the integration → Configure, now labelled **"Send commands over BLE when connected (recommended)"**) can be **unticked to force cloud-only**. Existing users who explicitly disabled the option keep their setting.
- Dropped the "experimental / UNVERIFIED" wording from the option and docs now that the path is confirmed on the Grand Canyon S.

### Verified

- On-vehicle confirmation: Grand Canyon S 600, SCU fw 1.13.0.0 — 11/11 BLE writes acknowledged with `status=1`, physical actuation confirmed, and BLE→cloud fallback observed after a mid-session TLS drop.

### Credit

- Root-cause diagnosis and the original on-vehicle proof (Grand Canyon S 700, SCU fw 1.49.7) by **Dan Simms** ([dan-simms1/hymer-connect-ha](https://github.com/dan-simms1/hymer-connect-ha), PR #17). Thank you.

## [2.66.2] - 2026-08-22

> ⚠️ **BLE subscription-path correction — UNVERIFIED on our own vehicle.** Companion to the v2.66.0 write-path fix. Applies the same field-1 rewrap to the BLE **subscription/refresh** path so the SCU actually parses our subscriptions as requests. Only affects the BLE read path; if BLE misbehaves, roll back to v2.66.1. Cloud/SignalR read coverage is unchanged either way.

### Fixed

- **BLE subscription/refresh requests are now framed correctly** (companion to the v2.66.0 write-path fix). `ble_client.send_pia_command` — used by the coordinator to subscribe to sensor pushes over BLE — wrapped the PIA `Request` in protobuf field 2 (the cloud DataHub envelope). Over BLE, field 2 is `BleProtocol.response`, so the SCU parsed our subscription requests as *responses* and ignored them, leaving only the ~28 sensors the SCU pushes autonomously. The subscription/refresh path now rewraps as field 1 (`BleProtocol.request`) — via the same `_rewrap_cloud_payload_as_ble_request` helper as the write path — and is sent **write-with-response** so a multi-chunk subscription burst is not truncated at low MTU. This should let the SCU honour our BLE subscriptions and stream the full sensor set over BLE. Offline byte-level regression test extended (`tools/_test_ble_write_frame.py`).

### Credit

- The `send_pia_command` observation (that correcting it also fixes the subscription path) is from **Dan Simms** ([dan-simms1/hymer-connect-ha](https://github.com/dan-simms1/hymer-connect-ha), PR #17). Thank you.

## [2.66.1] - 2026-08-22

### Fixed

- Removed a duplicate `fresh_water_level` translation key in `strings.json` (a second, identical entry). Cosmetic only — no user-facing change; clears the JSON duplicate-key warning.

## [2.66.0] - 2026-08-22

> ⚠️ **Experimental opt-in BLE write path — UNVERIFIED on our own vehicle.** This re-enables BLE `setValues` writes (removed in v2.62.24) behind a new, **off-by-default** option. It is based on an external finding (credit below) and a byte-level offline test, but has **not yet been confirmed on our S600 SCU**. With the option off, behaviour is identical to v2.65.18 (cloud-only writes). If a BLE write is not acknowledged by the SCU, the integration falls back to the cloud automatically, so worst case is unchanged.

### Added

- **Opt-in "Send commands over BLE (experimental)" option** (Settings → the integration → Configure). When enabled, write commands (lights, switches, heater, fridge, etc.) are attempted over the local BLE link first and fall back to the cloud/SignalR path automatically if the SCU does not acknowledge within ~3 seconds. Off by default — leave it off to keep sending all commands via the cloud.

### Fixed

- **BLE `setValues` writes are now framed correctly** (root cause of the v2.62.24 "SCU silently drops BLE writes" conclusion). Our command encoders wrap the PIA `Request` in top-level protobuf field 2 — correct for the cloud/DataHub envelope, but over BLE field 2 is `BleProtocol.response`, so the SCU parsed every command as a *response*, found no matching outstanding request, and discarded it without any error or ACK. The BLE write path now rewraps the command as field 1 (`BleProtocol.request`) — the same wrapper the pairing path already used — and sends it **write-with-response** (required so multi-chunk writes at MTU 23 are not silently truncated). Command success is now judged only on a real `BleProtocol.response` whose `request_id` matches the request (status 1 = SUCCESS). Read/subscription behaviour is unchanged. Offline byte-level regression test: `tools/_test_ble_write_frame.py`.

### Credit

- Root-cause diagnosis and on-vehicle proof (Grand Canyon S 700, SCU firmware 1.49.7, cloud offline, `status=1 SUCCESS`) by **Dan Simms** ([dan-simms1/hymer-connect-ha](https://github.com/dan-simms1/hymer-connect-ha)). The field-1-vs-field-2 envelope asymmetry and the write-with-response requirement are his findings; this release implements them as an opt-in path in the upstream integration. Thank you.

## [2.65.18] - 2026-08-19

> ⚠️ **BLE re-pairing/reconnect robustness — UNVERIFIED on-vehicle.** These fixes target the BLE bond/reconnect recovery path reported in [#16](https://github.com/BetaHydri/hymer-connect-ha-ble/issues/16). They are defensive and guarded (worst case identical to v2.65.17), but the leaked-write-channel and bond-preservation behaviour can only be confirmed on the reporter's hardware. If BLE regresses for you, roll back to v2.65.17.

### Fixed

- **Stale BlueZ write/notify channel after an aborted BLE session is now self-healed** ([#16](https://github.com/BetaHydri/hymer-connect-ha-ble/issues/16), Punkt 1 & 4). When a running BLE session is torn down abruptly (e.g. starting a second pairing while one is active), BlueZ can keep the `AcquireWrite`/`AcquireNotify` file descriptor, so every following attempt failed with `[org.bluez.Error.NotPermitted] Write acquired`, stayed at MTU 23, then died with `UNLIKELY_ERROR: 14` — previously only recoverable via `systemctl restart bluetooth`. The MTU-acquire step now detects this leaked-channel condition, and the setup-failure handler forces a D-Bus `Device1.Disconnect()` (the only client-side call that makes BlueZ release the descriptor) followed by a fresh-session reconnect. The self-heal trigger was broadened to also cover `UNLIKELY_ERROR` and `Write acquired`, and `start_notify` is now bounded by an 8-second timeout so a stale notify channel fails fast into recovery instead of hanging ~11 seconds.
- **A bonded SCU is no longer un-paired after two transient connection failures** ([#16](https://github.com/BetaHydri/hymer-connect-ha-ble/issues/16), Punkt 2). Weak signal or an SCU that is still booting shows up as `failed to discover services, device disconnected` — a transient condition, not a corrupt bond. The integration previously called `RemoveDevice` after only two failures in ~4 seconds, destroying a perfectly good bond and forcing a physical re-pair at the vehicle. Transient GATT failures now retry up to three times with growing back-off while **keeping the bond intact**, then fall back to cloud and let the coordinator retry BLE later. The bond is now only cleared on a genuine authentication/encryption error.
- **Longer settle after a fresh bond** ([#16](https://github.com/BetaHydri/hymer-connect-ha-ble/issues/16), Punkt 4). The post-bonding reconnect now waits 1.5 s (was 0.5 s) before re-opening the GATT session, giving the SCU's GATT server time to re-expose its services after the encryption change.

### Documentation

- **Corrected the SCU pairing-window guidance** ([#16](https://github.com/BetaHydri/hymer-connect-ha-ble/issues/16), Punkt 3). Some SCUs (e.g. B-MC I 680 / SCU 1.13.0.0) hold the pairing window for only **~30 seconds**, not ~2 minutes. `docs/ble-troubleshooting.md` now recommends pressing **CONNECTION first, then submitting the Reconfigure form within ~25 seconds** so `Device1.Pair()` fires inside even a short window.

## [2.65.17] - 2026-08-11

### Fixed

- **BLE startup can no longer block Home Assistant setup indefinitely** ([#15](https://github.com/BetaHydri/hymer-connect-ha-ble/issues/15)). BLE startup is now capped at 90 seconds and cleanup after a failed startup is separately capped at 5 seconds, so BlueZ/GATT hangs during a new host bond fall back to the cloud/SignalR path instead of leaving Home Assistant stuck at `Waiting for integrations to complete setup`. BLE client cancellation cleanup is also bounded so a stuck disconnect cannot defeat the startup timeout.

## [2.65.16] - 2026-07-20

### Changed

- **BLE MTU-default message downgraded from `WARNING` to `INFO` so it no longer appears in Home Assistant's custom-integration error panel.** When the adapter/proxy keeps the 23-byte default MTU, the integration falls back to 20-byte Write-With-Response chunks — a fully supported, reliable path (just slightly slower). This is normal on many Bluetooth adapters and BLE proxies and does not affect functionality, so it should never have been surfaced as an error. The log text was also reworded to make clear it is informational, not a fault.
- **MTU acquisition now also falls back to the wrapped bleak backend.** Previously `_acquire_mtu()` was only attempted on Home Assistant's `HaBleakClientWrapper`; if the wrapper did not expose it, no negotiation was tried. It now additionally looks up `_acquire_mtu()` on the wrapped `_backend` (depending on habluetooth/bleak version) before falling back to D-Bus negotiation. Fully guarded with `getattr` and wrapped in the existing `try/except`, so the worst case is identical to prior behaviour (MTU 23, 20-byte chunks) — no path leaves BLE in a worse state.

## [2.65.15] - 2026-07-20

### Fixed

- **BLE frame accumulator no longer drops a magic byte split across notification boundaries.** `_FrameAccumulator.feed()` previously cleared its entire buffer whenever the 2-byte PIA magic (`0xA0CB`) was not yet found. If a BLE notification boundary split the magic between two chunks (first byte `0xA0` at the end of one chunk, `0xCB` at the start of the next), the trailing `0xA0` was discarded and the following frame could be lost. It now retains the last `len(magic) - 1` bytes as a potential partial-magic prefix for the next `feed()` call. Low-probability edge case (a frame almost always arrives whole inside one TLS record), but eliminates a latent reassembly bug.
- **Removed unreachable dead code** after the `return None` in `ScuBleClient.check_bonding_state()` (a duplicated `stop_notify`/`disconnect` block that could never execute). Cosmetic; no behavioural change.

## [2.65.14] - 2026-07-20

> **✅ Confirmed working:** verified on-device on a Samsung Galaxy S20 FE 5G — the extractor now completes the full handshake and successfully mints the EHG refresh token. This confirms the entire legacy-TLS-over-BLE fix chain (v2.65.9 → v2.65.14) end-to-end on modern Android.

### Fixed

- **EHG Token Extractor APK — SCU response is now recognised (incoming PIA frames are reassembled).** With the v2.65.13 buffer fix the handshake completes end-to-end: a tester on a Samsung Galaxy S20 FE 5G reached `✅ TLS session established`, the `PairMobileRequest` was sent encrypted, and the SCU actually replied (80 B, 1357 B, then a run of small 38–39 B frames were `Received … bytes decrypted`) — yet the app still ended in `Timed out waiting for PairMobileResponse`. Root cause: the token app fed the raw accumulated plaintext straight into the protobuf parser, but every SCU message is PIA-framed (2-byte magic `0xA0CB` + 4-byte length + 4-byte CRC32 + payload). The protobuf walk therefore started on the `0xA0` magic byte and mis-parsed, so the response was never recognised even though the bytes arrived, and multiple frames/status pushes were concatenated into one blob. Fixed by adding a `PiaFrameAccumulator` (mirroring the proven `_FrameAccumulator` in the HA integration's `ble_client.py`): it resyncs to each frame's magic marker, waits for a complete frame, strips the 10-byte header, and yields payloads ready for protobuf parsing — buffering any partial trailing frame until the rest arrives. The protobuf walker was also hardened to skip fixed32/fixed64 fields instead of aborting. The app now also prints a clear "press ALLOW on the SCU touchscreen" hint when the SCU signals `confirmationRequired`. Token-app only; download the updated APK from the release assets.

## [2.65.13] - 2026-07-20

### Fixed

- **EHG Token Extractor APK — TLS handshake now proceeds past ClientHello (`BufferOverflowException` fixed).** With the v2.65.12 protocol fix the handshake finally reached the SCU: the 169-byte ClientHello was sent (`TX 169 bytes -> 1 chunks`), but the very first response chunk crashed with `BufferOverflowException` in `TlsOverBle.feedEncrypted`. Root cause: the persistent `peerNetBuffer` was created with `ByteBuffer.allocate(16384)`, which leaves it in *write* mode; the first `compact()` then treated the entire 16 KB as unread data and left zero writable space, so `put(incoming)` overflowed. This bug was previously unreachable because the handshake always died earlier in `beginHandshake`. The buffer now starts empty in *read* mode (`.apply { flip() }`), so the first `compact()` yields a fully writable buffer and inbound TLS records accumulate correctly. Token-app only; download the updated APK from the release assets.

## [2.65.12] - 2026-07-20

### Fixed

- **EHG Token Extractor APK — TLS handshake now works on Samsung Galaxy S20 FE 5G (and other phones that disable legacy TLS).** After the v2.65.9 BouncyCastle switch, a tester on a Samsung Galaxy S20 FE 5G still failed at Step 6 with `IllegalStateException: No usable protocols enabled` (thrown from `ProvSSLContextSpi.getActiveProtocolVersions`). That phone's Android build lists `TLSv1, TLSv1.1` in the `jdk.tls.disabledAlgorithms` security property, and BouncyCastle intersects the enabled protocols with that constraint — leaving an empty set even though the app requested TLS 1.0/1.1. The token app now clears `jdk.tls.disabledAlgorithms` before the handshake so the legacy TLS 1.0/1.1 + AES-CBC-SHA path the SCU requires stays usable on every device. Safe: it is a throwaway, trust-all handshake to the self-signed SCU. No change to the Home Assistant integration itself; download the updated APK from the release assets.

## [2.65.11] - 2026-07-20

### Changed

- **TenHaaft satellite dish On/Off switch — write path CONFIRMED** ([#13](https://github.com/BetaHydri/hymer-connect-ha-ble/issues/13)). @FrankHae confirmed on-vehicle that `switch.hymer_satellite_dish` (bus 10 slots 1/2, added in v2.65.10) actually deploys and retracts the dish. The v2.65.10 "write unverified" caveat is removed — the satellite On/Off switch is now a fully verified writable control. Docs and mapping `_doc` updated accordingly. JSON/docs-only change; no behavioural change for existing installs.

## [2.65.10] - 2026-07-20

### Added

- **TenHaaft satellite dish On/Off switch (HYMER BMC I 680)** ([#13](https://github.com/BetaHydri/hymer-connect-ha-ble/issues/13)). New `switch.hymer_satellite_dish` ("Satellite dish") behaves like a normal dashboard toggle:
  - **On** = Start — deploy the dish and auto-search (**bus 10 slot 1**).
  - **Off** = Park — retract to the safe position (**bus 10 slot 2**).
  - State is derived from `SafePositionState` (**bus 10 slot 10**) **inverted** — a parked dish shows Off, a deployed dish shows On.
  - > ⚠️ The write path on bus 10 slots 1/2 is **unverified** — only the satellite `select` (slot 5) has been confirmed writable so far. @FrankHae to confirm on-vehicle. Revert if the SCU drops the writes.
- **Satellite state binary sensors (HYMER BMC I 680)** ([#13](https://github.com/BetaHydri/hymer-connect-ha-ble/issues/13)). Frank's three previously-unclear bus-10 status slots are now exposed for full insight:
  - `binary_sensor.hymer_sat_dish_moving` — **bus 10 slot 9** (`DishMovingState`, device class *moving*): on while the dish is deploying/retracting/searching.
  - `binary_sensor.hymer_sat_safe_position` — **bus 10 slot 10** (`SafePositionState`): on = dish parked (also feeds the switch, inverted).
  - `binary_sensor.hymer_sat_standby` — **bus 10 slot 13** (`StandbyModeState`): on = antenna in standby.
- **Reusable command-pair switch pattern.** The JSON switch platform now supports split write slots (`write_on_bus`/`write_on_sid`, `write_off_bus`/`write_off_sid`), momentary trigger values (`bool_on`/`bool_off`), and inverted read (`on_value: false`) — so any future device that starts/stops via two separate momentary commands can be modelled as a single On/Off switch entirely from `sensor_maps/*.json`. Fully backward-compatible: existing switches are unchanged.

## [2.65.9] - 2026-07-20

### Fixed

- **EHG Token Extractor APK — TLS handshake now works on all Android versions.** The bundled token-extractor app failed at Step 6 ("ERROR: null") on modern phones because it used Android's platform TLS (Conscrypt), which dropped the legacy TLS 1.0/1.1 + `TLS_RSA_WITH_AES_128/256_CBC_SHA` cipher suites the SCU requires. The handshake now runs through BouncyCastle's pure-Java JSSE provider (`bctls-jdk18on`), which speaks legacy TLS on **any** Android version — the same approach the official EHG app uses (it bundles node-forge, a JS TLS stack, which is why it pairs fine even on Android 16). No change to the Home Assistant integration itself; download the updated APK from the release assets.

## [2.65.8] - 2026-07-19

### Removed

- **Bathroom sink ambient light — reverted (HYMER BMC I 680)** ([#9](https://github.com/BetaHydri/hymer-connect-ha-ble/issues/9)). The provisional `light.hymer_light_bathroom_sink_ambient` mapping on **bus 20** (added in v2.65.7) is removed. @FrankHae confirmed on-vehicle that nothing appears on bus 20, so the entity only ever showed as `unavailable` for BMC owners. Bus 20 was an unconfirmed guess from the EHG catalog; per the confirm-then-map policy it should not have shipped. All confirmed BMC mappings (bus 5 Alde, 10 satellite, 29 battery, 32 fridge, floor/shower/ceiling lights) are retained.

## [2.65.7] - 2026-07-17

### Added

- **Alde 3030 second heating zone (Zone 2)** ([#9](https://github.com/BetaHydri/hymer-connect-ha-ble/issues/9)). At @FrankHae's request the Zone-2 slots are now mapped so future dual-zone floorplans (e.g. HYMER 780/880) work out of the box:
  - `sensor.hymer_alde_zone2_temp` — **bus 5 slot 2** (`zone_2_actual_temperature`, read-only).
  - `sensor.hymer_alde_zone2_setpoint` — **bus 5 slot 4** (`zone_2_target_temperature`) read-back, plus `number.hymer_alde_zone2_setpoint` as a **slider** (5–30 °C, step 0.5 °C) mirroring the Zone-1 setpoint.
  - > ℹ️ On single-zone vehicles (e.g. BMC I 680) these entities simply idle. The Zone-2 write path is **unverified** — no dual-zone vehicle was available to test.
- **Bathroom sink ambient light — provisional (HYMER BMC I 680)** ([#9](https://github.com/BetaHydri/hymer-connect-ha-ble/issues/9)). New `light.hymer_light_bathroom_sink_ambient` maps **bus 20** (EHG `LightCircuit10` = "Ambient light sink"), the likely missing bathroom ambient light @FrankHae was looking for.
  - > ⚠️ **Provisional:** discovered bus IDs are assigned per-vehicle, so bus 20 is an educated guess from the EHG catalog and is not yet confirmed on-vehicle. If the new entity doesn't switch the bathroom light, a RAW PIA toggle log will pin the real bus number.

## [2.65.6] - 2026-07-16

### Added

- **Habitation battery state of charge (HYMER BMC I 680)** ([#9](https://github.com/BetaHydri/hymer-connect-ha-ble/issues/9)). New `sensor.hymer_body_battery_soc` maps **bus 29 slot 1** to the leisure/body battery (Aufbaubatterie) charge level in %. Confirmed on-vehicle by @FrankHae: the value matches both the EHG app's battery percentage and his Home Assistant history timing. `device_class: battery`, `state_class: measurement`. Bus 29 is unused on S600/S700/ML-T.

### Changed

- **Alde setpoint is now a slider (HYMER BMC I 680)** ([#9](https://github.com/BetaHydri/hymer-connect-ha-ble/issues/9)). `number.hymer_alde_setpoint` (bus 5 slot 3) now renders as a **slider** instead of a keyboard-entry box, at @FrankHae's request. The generic `number` platform gained a JSON-configurable `mode` (`slider` / `box` / `auto`), so any future writable float slot can pick its own control style.
- **Alde warning vs. error split (HYMER BMC I 680)** ([#9](https://github.com/BetaHydri/hymer-connect-ha-ble/issues/9)). At @FrankHae's request the two Alde status flags now read correctly:
  - **Bus 5 slot 8** was renamed `alde_error` → **`alde_warning`** (`binary_sensor.hymer_alde_warning`). This slot is a panel-attention/warning flag (e.g. the antibacterial/Legionella boiler-service reminder), not a hard fault.
  - **Bus 5 slot 12** was renamed `alde_fault` → **`alde_error`** and is now **enabled by default** (`binary_sensor.hymer_alde_error`). This is the true hard-fault flag — it stayed `False` throughout Frank's reminder lockout, confirming it only trips on a real fault.
  - > ℹ️ **BMC I 680 owners:** the entity that was `binary_sensor.hymer_alde_error` now reads the hard-fault slot (12), and a new `binary_sensor.hymer_alde_warning` (slot 8) carries the panel reminder. Update any dashboards/automations accordingly.

### Verified on-vehicle (issue #9)

- **Alde setpoint float write CONFIRMED on bus 5 slot 3.** @FrankHae's RAW PIA log shows `bus=5 sid=3 f6/wt5=8.0` landing on the SCU (`alde_setpoint 7.5 → 8.0`). The v2.65.5 "write unverified" caveat is removed — the Alde Zone-1 setpoint is now a fully verified writable control.

## [2.65.5] - 2026-07-15

### Added

- **Writable Alde 3030 setpoint (HYMER BMC I 680)** ([#9](https://github.com/BetaHydri/hymer-connect-ha-ble/issues/9)). New `number.hymer_alde_setpoint` entity lets you set the Alde Zone-1 target temperature (bus 5 slot 3 = `Zone1TargetTemperature`) from Home Assistant — range 5–30 °C, step 0.5 °C. The value is written to the SCU as a 32-bit float via the multi-sensor command path (the same path the Truma climate setpoint uses).
  - **Confirmed writable via decompilation.** The EHG app's own component model marks `Zone1TargetTemperature` (componentId 5, id 3) as `mode: 'rw', datatype: 'float'`, and its protobuf encoder serialises a `floatValue` with the fixed32 `float` writer — matching the encoding this integration already uses.
  - A new generic, JSON-driven **`number`** platform backs this control (`climate.numbers.<key>` in the brand overlay), so future writable float slots (e.g. floor/air-heater setpoints) are a JSON-only addition.
  - > ⚠️ **The write to bus 5 slot 3 is not yet verified on a vehicle.** Bus-5 bool/int/string writes are already confirmed landing on Frank's SCU, and the float path is proven on the Truma setpoint, so this should work — but please confirm via a RAW PIA log and revert/adjust if the SCU drops the float write.

## [2.65.4] - 2026-07-15

### Changed

- **Alde electric booster now shows kW units (HYMER BMC I 680)** ([#9](https://github.com/BetaHydri/hymer-connect-ha-ble/issues/9)). At @FrankHae's request, `select.hymer_alde_electric_booster` (bus 5 slot 7) now displays the options **`Off` / `1 kW` / `2 kW` / `3 kW`** instead of the bare `Off / 1 / 2 / 3`. The integer step (0–3) is still what gets written to and read from the SCU — a new optional `option_values` mapping in the stepped-select driver lets the friendly label differ from the underlying integer. Fridge cooling-step selects are unaffected (they keep their bare-int labels).

### Verified on-vehicle (issue #9)

- **All Alde 3030 bus-5 write controls confirmed landing on @FrankHae's SCU.** The electric booster (5,7 `ElectricitySetting`), hot water (5,6 `HotWaterSetting`), gas (5,10 `GasSetting`), energy priority (5,5) and heating master switch (5,9) all write successfully and arrive correctly in the EHG app. The docs and mappings no longer mark bus-5 writes as unverified. The Alde **setpoint** (5,3, float) remains read-only for now (float writes are not yet supported by the command path).
- **Bathroom Ambient light is not exposed by the SCU on the BMC I 680.** @FrankHae has no `Discovered bus 18/20/23` entities (only 5/10/29/32) and toggling the bathroom ambient light at the vehicle produces **no** bus/slot in the PIA log — only the bathroom ceiling (bus 19) and the Privat group (bus 27) react. Like other panel-only functions, it cannot be mapped. The bathroom **ceiling** light (bus 19) is confirmed working.

## [2.65.3] - 2026-07-14

### Changed

- **More robust BLE SCU auto-detection during setup ([#8](https://github.com/BetaHydri/hymer-connect-ha-ble/issues/8)).** Two users reported that Path A (BLE) setup could not find the SCU even with the CONNECTION button pressed at the vehicle. The auto-scan used during the config flow (when no BLE MAC address is entered) has been hardened:
  - **Active-scan fallback.** The scan first reads Home Assistant's passive Bluetooth advertisement cache; if that yields no SCU candidate it now runs an **active `BleakScanner.discover()`** pass as a second stage. Previously an empty passive cache returned immediately with no SCU found — even though the SCU was pairable. The SCU advertises only intermittently (especially in standby), so the passive cache is often empty at the moment of setup.
  - **Scan retries.** The active scan now runs up to three short passes while no candidate is seen, instead of a single attempt, to catch the SCU's intermittent advertisement.
  - **Brand-aware name filter.** The SCU advertises as `<BRAND> <serial>` (confirmed e.g. `HYMER 00013970`). The detector previously matched only the `hymer`/`scu` name substrings, so a Bürstner / Carado / Dethleffs / Eriba / LMC / Niesmann+Bischoff / Sunlight / Laika / FreeOnTour SCU could be missed unless it also advertised the Nordic UART Service UUID. The name filter now recognises **every supported EHG brand** (plus the generic `scu` / `siu` / `ehg` markers and the NUS service UUID).
  - **Clearer diagnostics.** BLE scan debug logging now reports how many devices were seen versus how many matched as SCU candidates, per scan pass — making it obvious whether a failed setup is a discovery problem (SCU never advertised) or a pairing problem (SCU seen but bonding rejected).
  - > ℹ️ If the SCU never appears in any scan (as on issue [#8](https://github.com/BetaHydri/hymer-connect-ha-ble/issues/8)), it is an adapter / range / standby issue at the vehicle, not a filter problem — verify with `bluetoothctl scan le` near the vehicle. Cloud-only mode (Path B) remains fully available in the meantime.

## [2.65.2] - 2026-07-14

### Added

- **Alde 3030 writable controls — electric booster, gas, hot-water (HYMER BMC I 680)** ([#9](https://github.com/BetaHydri/hymer-connect-ha-ble/issues/9)). Mapped the remaining Alde `Klima` controls from @FrankHae's EHG-app screenshots + decompiled `Alde3020` slot model:
  - **`select.hymer_alde_electric_booster`** (bus 5 slot 7 = `ElectricitySetting`, int 0–3 kW) — app „Elektrische Zusatzheizung“/„Max Elektrizität“. Options `Off / 1 / 2 / 3` (kW steps; `Off` = „Aus“). Backed by read sensor `sensor.hymer_alde_electric_booster_level` (5,7).
  - **`switch.hymer_alde_gas`** (bus 5 slot 10 = `GasSetting`, bool) — app „Gas aktivieren“. Backed by `binary_sensor.hymer_alde_gas_active` (5,10).
  - **`select.hymer_alde_hot_water`** (bus 5 slot 6 = `HotWaterSetting`, string) — app „Warmwasser Boiler“ + „Turbo Modus“. Options `Off / Normal / Boost`. Backed by `sensor.hymer_alde_hot_water_mode` (5,6).
  - > ⚠️ **Write paths on bus 5 slots 6/7/10 are not yet verified on a vehicle.** Bus-5 writes (Alde on/off, energy priority) are already confirmed landing on Frank's SCU, so these should work — but the hot-water option strings are unconfirmed (Frank has no water in the lines yet). Revert/adjust if the SCU drops a write.

### Verified via decompilation (issue #9 cross-check)

- Re-checked every bus-5 slot against the decompiled EHG app (`Alde3020` component, `source/androidapp/_hermes_decompiled/index.js`). Ground truth: slot 6 `HotWaterSetting` = `rw string ['Off','Normal','Boost']`, slot 7 `ElectricitySetting` = `rw int` (kW, range 0–3), slot 9 `PanelOn` = `rw bool`, slot 10 `GasSetting` = `rw bool`, slot 11 `AccSetting` = `rw bool`, slot 8 `PanelBusy` = `r bool`, slot 12 `Error` = `r bool`. This confirms the new controls above and **corrects `docs/ehg-app-metadata.md`**, which had slots 10/11 as `string r` (they are `bool rw`).
- Added two more read-only sensors from that model: `binary_sensor.hymer_alde_accessory_setting` (5,11 `AccSetting` — function unknown, exposed read-only so @FrankHae can observe before we add a writable switch) and a disabled-by-default `binary_sensor.hymer_alde_fault` (5,12 `Error` — the dedicated hard-fault flag, kept separate from the 5,8 panel-busy `alde_error`).

## [2.65.1] - 2026-07-14

### Fixed

- **Alde 3030 error sensor remapped `5,12` → `5,8` (HYMER BMC I 680)** — The `binary_sensor.hymer_alde_error` was reading the wrong slot ([#9](https://github.com/BetaHydri/hymer-connect-ha-ble/issues/9)). @FrankHae hit a real Alde panel error again (13–14 Jul 2026) and reported that slot 12 stayed `False` the whole time while **slot 8** read `True` for the exact error window and `False` otherwise. The sensor now reads **bus 5 slot 8**. The decompiled EHG `Alde3020` component labels slot 12 `error`, but that did not match Frank's vehicle/firmware — on-vehicle evidence wins. The old `5,12` reverts to a disabled `Discovered bus 5 slot 12` diagnostic.
  - > ℹ️ Entity id, name and `device_class: problem` are unchanged — only the underlying bus/slot moved, so no dashboard or automation changes are needed.

## [2.65.0] - 2026-07-12

### Added

- **Alde 3030 error/warning sensor (HYMER BMC I 680)** — New read-only `binary_sensor.hymer_alde_error` (bus 5 slot 12 = `error`, `device_class: problem`) for @FrankHae's BMC ([#9](https://github.com/BetaHydri/hymer-connect-ha-ble/issues/9)). It flags when the Alde panel has a pending error/warning — such as the antibacterial/Legionella boiler-service reminder that Frank hit, which blocked remote on/off from both Home Assistant and the EHG app until it was acknowledged with **OK on the Alde panel**.
  - > ℹ️ **Only a boolean flag is transmitted over the SCU** — the actual message text is panel-only and never sent over PIA, so we can surface *that* an Alde error is pending, but not *which* one. The panel-side acknowledge/lockout is Alde firmware behaviour and cannot be overridden remotely (the EHG app is blocked the same way).
  - > ⚠️ **Mapping still to be confirmed:** @FrankHae observed slot 12 permanently `False` during normal operation; a RAW-PIA line with `5,12 = True` captured during an actual Alde error is still needed to make the mapping watertight.

### Confirmed on-vehicle by @FrankHae (issue #9)

- All the v2.64.8 / v2.64.9 test-build **writes now confirmed working** on the BMC I 680 (SCU accepts HA → cloud writes on buses 5 / 10 / 32): `switch.hymer_alde_heating` (5,9), `select.hymer_alde_energy_priority` (5,5), `select.hymer_absorber_fridge_power_source` (32,2), `select.hymer_absorber_fridge_cooling_step` (32,3, Off/1–5), and `select.hymer_satellite` (10,5). Alde inside temperature (5,1) matches the app; the Alde target temperature written from HA (5,3) reaches the heater. Satellite signal strength reads 255 when idle, then 0–100 % when the dish is active.
- Still untested (hardware preconditions): fridge `SetPowerMode` 12V / AC modes (need 220 V / engine running) and the Alde boiler (no water in the lines yet).

## [2.64.9] - 2026-07-12

### Added

- **Thetford N4142E+ fridge power-source select + TenHaaft satellite select (HYMER BMC I 680, test build)** — Two more writable controls for @FrankHae's BMC ([#9](https://github.com/BetaHydri/hymer-connect-ha-ble/issues/9)), with option lists recovered from the decompiled EHG app (APK 2.10.14) and cross-checked against Frank's on-vehicle readback:
  - **`select.hymer_absorber_fridge_power_source`** (bus 32 slot 2 `SetPowerMode`) — `Automatic mode` / `Gas mode` / `12V mode` / `AC mode`. This is the fridge energy-source control Frank asked about. A read-only `sensor.hymer_absorber_fridge_power_source` (32,2) was added to back it.
  - **`select.hymer_satellite`** (bus 10 slot 5 `SatellitePosition`) — pick the target satellite from the app's full 19-entry list (`Astra 1`, `Eutelsat 9`, `Hotbird`, …); reuses the existing `sat_satellite` (10,5) sensor for readback.
  - > ⚠️ **Write paths on bus 32 slot 2 and bus 10 are not yet verified on a vehicle.** Shipped for @FrankHae to test; readback is confirmed. Revert if the SCU drops the writes.

### Verified via decompilation (issue #9 cross-check)

- Cross-checked **every** BMC I 680 slot Frank reported against `docs/ehg-app-metadata.md` **and** the decompiled EHG app. All of Frank's interpretations hold. Two clarifications: bus 10 slot 5 is a **writable string** `SatellitePosition` (the metadata doc wrongly listed it as `int`), and bus 10 slot 9 is `dish_moving_state` (not “satellite found”). The metadata doc's bus 5 slot 7 (`string`) is corrected to `int` (0–3 kW) per the app.
- The decompiled `TenhaaftSatAntenna` also exposes write-only commands (slots 1–4: `start` / `park` / `stop_movement` / `open_sleep_mode`) — candidates for future `button` entities.

## [2.64.8] - 2026-07-12

### Added

- **Alde 3030 writable controls (HYMER BMC I 680, test build)** — First writable controls for the Alde heater ([#9](https://github.com/BetaHydri/hymer-connect-ha-ble/issues/9)). The complete Alde slot model (labels, datatypes, read/write flags, option lists) was recovered from the decompiled EHG app (`Alde3020` component, APK 2.10.14) and cross-checked with @FrankHae's on-vehicle readback:
  - **`select.hymer_alde_energy_priority`** (bus 5 slot 5) — `Prio Gas` / `Prio EL`, from the app's `PrioElectricityGas` `stringRange`.
  - **`switch.hymer_alde_heating`** (bus 5 slot 9 `PanelOn`) — heater master on/off.
  - > ⚠️ **The write path on bus 5 is NOT yet verified on a vehicle.** These controls are shipped so @FrankHae can test whether the SCU accepts Alde writes from Home Assistant. If writes are silently dropped (as BLE writes are), they will be reverted. Readback for both controls is already confirmed.
- **Generic string-valued select support** — The JSON stepped-switch select driver now also supports string-state selects via `read.value_sensor` (reflects a string sensor) and a `writes.option` recipe with `$option` substitution. Reusable for future string enums (AC modes, hot-water settings, etc.). No change to existing integer stepped selects.

### Notes

- Still read-only / not yet exposed pending confirmation that bus-5 writes land: Alde target temperature (5,3, float — needs float-write support), hot-water setting (5,6, `Off`/`Normal`/`Boost`), electricity power (5,7, int 0–3 kW), and the second heating zone (5,2 / 5,4).

## [2.64.7] - 2026-07-12

### Added

- **BMC I 680 (MY2024) Alde 3030, satellite dish and absorber-fridge sensors** — Mapped the read-only slots confirmed via RAW PIA logs by @FrankHae ([#9](https://github.com/BetaHydri/hymer-connect-ha-ble/issues/9)). This is the first **Alde** heater in the repository. JSON-only change; buses 5, 10 and 32 are unused by the S600/S700/ML-T layouts, so there is zero risk for those vehicles.
  - **Alde 3030 (bus 5)** — `alde_inside_temp` (5,1), `alde_outside_temp` (5,15), `alde_setpoint` (5,3), `alde_energy_priority` (5,5, `Prio Gas`/`Prio EL`), `alde_heating_on` (5,9) and `alde_heating_active` (5,14). Read-only for now; writable climate/select controls will follow once the remaining slots are confirmed.
  - **TenHaaft satellite dish (bus 10)** — `sat_satellite` (10,5, selected satellite), `sat_status` (10,6, dish status) and `sat_signal_strength` (10,8, 0–100 %).
  - **Thetford N4142E+ absorber fridge (bus 32)** — `fridge_absorber_power` (32,1), `fridge_absorber_cooling_step` (32,3) and `fridge_absorber_door` (32,5). Named with an `_absorber_` prefix to stay distinct from the S600 N4000 fridge on bus 34.
  - **Writable absorber-fridge cooling step** — Added a JSON-driven stepped-switch `select.fridge_absorber_cooling_step` (Off / 1–5) for bus 32, mirroring the confirmed S600 N4000 (bus 34) and ML-T Thetford Compressor (bus 114) drivers (writes power sid 1 as bool, then cooling-step sid 3 as uint). ⚠️ Readback is confirmed, but the **write path is not yet verified on @FrankHae's vehicle** — if the SCU does not accept the cloud write, this select will be reverted.
  - `strings.json` and `translations/en.json` updated in the `sensor` and `binary_sensor` sections and remain fully key-synchronised. (The stepped-switch select takes its display name directly from the JSON, so it needs no translation entry.)
  - `docs/sensor-map.md` — added reference sections for bus 5 (Alde 3030), bus 10 (TenHaaft satellite) and bus 32 (Thetford N4142E+ absorber fridge).

### Still open (issue #9)

- **Bathroom ambient light** — toggling it produces no distinct discoverable slot in the logs (only bus 19 ceiling and bus 27 Privat group react); needs another at-vehicle capture.
- **Alde writable controls** — setpoint, energy priority, fan steps and day/night switching still to be built as climate/select entities (first Alde in the repo).
- **Unmapped Alde slots** (5,2/5,4/5,6–5,13), **fridge gas mode** (32,2, always `Gas Mode`) and **satellite “found” flag** (10,9) remain as discovered diagnostic sensors pending confirmation.

## [2.64.6] - 2026-07-07

### Added

- **BMC I 680 (MY2024) individual lights** — Mapped two more single lights confirmed via RAW PIA toggle logs by @FrankHae ([#9](https://github.com/BetaHydri/hymer-connect-ha-ble/issues/9)):
  - Bus 13 → **Floor ambient** (`light_floor_ambient`) — living-area floor ambient lighting, member of the Wohnen group.
  - Bus 17 → **Shower ceiling** (`light_shower_ceiling`) — shower ceiling light, member of the Privat group.
  Both are dimmable (slot 2 = brightness 0–100 %), no colour temperature. JSON-only change, zero risk for S600/S700/ML-T — buses 13 and 17 are not used by those layouts.

### Fixed

- **`distance_to_service` scaling** — Corrected the bus 1 slot 5 transform in `base.json` from `div100` to `div10` (km). *(Still awaiting on-vehicle verification.)*
- **Translation cleanup (`strings.json` / `en.json`)** — Removed 21 orphaned translation keys left behind by earlier entity renames/removals so they no longer clutter the files: `fridge_dometic_power`/`_power_ctrl`/`_silent`/`_silent_ctrl` (renamed to `fridge_compressor_*` in v2.63.1), `smart_temperature_1`/`smart_humidity_1` (migrated to the `hss_temp{n}_*` auto-slot family), `tire_pressure` (migrated to `hss_tyre{n}_pressure`), `door_sliding`/`door_rear`/`coolant_temp`/`adblue_temp` (renamed in `base.json` to `wiping_water_empty`/`motor_oil_warning`/`coolant_warning`/`adblue_level`), plus `light_bus22_unknown`, `light_led_bar_color_temp`, `light_1_level`, `light_2_level`, `alarm_armed`, `alarm_battery`, `step_retracted`, `water_pump`, `fuel_range` and `total_fuel_used`. None of these keys referenced an existing entity.
- **Eriba light translations completed** — Added the missing `light_shower_ambient` and `light_bedroom_furniture` name entries (on/off, brightness) that `eriba.json` defines, so they no longer fall back to translation-key-style names. `strings.json` and `en.json` are now fully key-synchronised in every section.

## [2.64.5] - 2026-07-01

### Changed

- **HYMER Smart Sensor overlay back to the `{n}` template syntax** — Now that the `{n}` form is confirmed working end-to-end on real hardware (@mcfly1969, ML-T 570 CrossOver, SCU 1.13.0.0 — all four tyre sensors, contact and gas-level sensors reporting live via cloud after the SCU resumed pushing), the built-in `hymer.json` returns to the cleaner `auto:<group>:{n}` template form (`hss_tyre{n}_*`, …). The v2.64.4 `:1` anchor form was only a precautionary measure while the earlier "no data" report was investigated — that turned out to be a transient SCU standby, not a code issue. Both forms remain fully supported and behave identically; this is a cosmetic/consistency change with no runtime difference.

## [2.64.4] - 2026-07-01

### Changed

- **HYMER Smart Sensor overlay reverted to the hardware-tested `:1` anchor form** — The `hymer.json` tyre (bus 70), temperature (bus 74), contact (bus 73) and gas-level (bus 71) auto-slot templates now use the `auto:<group>:1` anchor form (`hss_tyre1_*`, …) instead of the `{n}` placeholder introduced in v2.64.2. This is byte-identical to the implementation originally tested end-to-end on the ML-T 570 CrossOver (SCU 1.13.0.0) by @mcfly1969. Both forms remain fully supported by the loader (see v2.64.3) and behave identically at runtime — device #1 is now pre-created at startup, devices #2..N are still materialised on the fly. No entity-name or numbering change versus a working v2.64.0 install.

## [2.64.3] - 2026-07-01

### Fixed

- **HYMER Smart Sensors disappeared / `{n}` ghost entities (v2.64.2 regression)** — After the v2.64.2 sensor-map migration to the `{n}` placeholder syntax, all HYMER Smart Sensors (tyre pressure bus 70, temperature/humidity bus 74, contact bus 73, gas level bus 71) stopped producing data and instead created dead entities with a literal `{n}` in their name (e.g. `HSS Tyre{N} Status` → *unavailable*). Root cause: the sensor-map loader only recognised the legacy `auto:<group>:1` template form and silently rejected the new `auto:<group>:{n}` discriminator (`"{n}".isdigit()` is `False`), so those buses were never registered as auto-slot groups and inbound device frames were never matched. The loader now natively understands the `auto:<group>:{n}` template form (and still accepts the legacy `:1` form), and never registers an unresolved `{n}` placeholder name as an entity. Reported by @mcfly1969 on the ML-T 570 CrossOver (SCU firmware 1.13.0.0).

### Documentation

- **Auto-slot template guide** — Rewrote the *Pinned sensor mappings and auto-slot templates* section of [`docs/sensor-map.md`](docs/sensor-map.md) and the `README.md` brand-overlay walkthrough so users can write their own multi-device sensor JSON: clarified the `auto:<group>:{n}` syntax, corrected the (previously wrong) claim that numbering is shared across users — it is **per-install, starting at 1** — added a "write your own auto-slot family" recipe, and noted that auto-slot sensors need **no** `strings.json` / translations entries.

## [2.64.2] - 2026-06-29

### Changed

- **Auto-Slot Template Syntax** — All HYMER Smart Sensor templates migrated from `#X1` notation to new `{n}` placeholder syntax for automatic device numbering:
  - Tyres (bus 70): `hss_tyre{n}_*` with `auto:tyre:{n}`
  - Temperature sensors (bus 74): `hss_temp{n}_*` with `auto:temp:{n}`
  - Contact sensors (bus 73): `hss_contact{n}_*` with `auto:contact:{n}`
  - Gas-bottle sensors (bus 71): `hss_gaslevel{n}_*` with `auto:gas:{n}`
  - The integration automatically expands `{n}` to instantiate devices #1–#N without manual JSON duplication. Future-proof for unlimited device discovery.

## [2.64.1] - 2026-06-29

### Fixed

- **Commands failed after 50-minute reconnect timeout** — After the automatic SignalR reconnect every 50 minutes (when connection age exceeds 3000 seconds), light and boiler control commands were silently dropped until the user manually reloaded the integration. Root cause: after handshake, subscriptions were sent asynchronously, but commands could arrive before the SCU finished processing them, causing silent drops. Fixed by: (1) sending explicit re-subscriptions after reconnect, and (2) waiting for first sensor data before allowing commands to confirm subscriptions are active. Reported during v2.63.11 production use (2026-06-24).

## [2.64.0] - 2026-06-24

### Features

- **Auto-Slot Templates** — Dynamic SIU numbering for multi-device bus slots (e.g., `auto:tyre:1`, `auto:temp:1`, etc.), allowing flexible sensor discovery and naming across multiple external sensors on the same bus.
- **Pinned Sensor Mappings** — Fixed discriminators for dual-source buses (pin-6, pin-7) to ensure consistent slot-to-entity mapping when multiple sensors report on the same bus.
- **BMC I 680 Onboarding** — Complete sensor-map documentation and JSON examples for the Hymer BMC I 680 model with external sensors, Alde heater support, and brand-specific overrides.
- **RestoreEntity Mixin** (Optional) — Experimental support for optional last-value restore on integration startup, for entities that need state persistence across HA restarts.

### Fixed

- **60-Second Toggle Bug** — Removed device command retransmission on SignalR reconnect (v2.63.11 rapid-reconnect cooldown feature). Previously, commands sent just before a reconnect trigger were re-sent on reconnect, causing double-toggles (lights turned on, then immediately off). Now commands are only sent once.
- **BLE Write Path Removed** — Removed deprecated BLE write path (disabled since v2.62.23). Cloud/SignalR is now the only write transport. BLE remains read-only for low-latency sensor pushes.

### Testing

- **Backward Compatible** — All S600/S700 buses verified safe: Truma Combi D6E, Thetford Fridge, Voltronic Solar, Lights.
- **No Regressions** — Refactored pia_decoder.py & sensor.py (additive features, no breaking changes). All existing sensor mappings unchanged.
- **Translation Complete** — No missing keys; v2.62.28 regression avoided.

## [2.63.11] - 2026-06-09

### Fixed

- **TrackerEntity deprecation** — import `TrackerEntity` from `homeassistant.components.device_tracker` instead of the deprecated `config_entry` submodule. Removes the HA Core 2027.6 deprecation warning that appeared in logs on every startup.
- **SignalR rapid-reconnect cooldown** — when a SignalR session drops within 30 seconds (e.g., the 8-message rapid drops seen in logs), the coordinator now waits 5 seconds before reconnecting. This gives the Azure SignalR service time to clean up the old session server-side. Long-lived sessions (>30 s) still reconnect immediately.

## [2.63.10] - 2026-06-09

### Fixed

- **Fix SignalR cycling / "Initialisieren" loop** — the `_async_options_updated` callback was calling `async_reload()` which tore down SignalR and all entities every time HA evaluated the config entry options (~5 min intervals). Options (tank capacity, BLE address, BLE enabled) are already read dynamically on every poll cycle, so the full reload was unnecessary. Also fixes the HA 2026.12 deprecation warning for `update_listener`. Reported by @mcfly1969 on the ML-T 570 CrossOver ([#8](https://github.com/BetaHydri/hymer-connect-ha-ble/issues/8)).

## [2.63.9] - 2026-06-09

### Reverted

- **Revert non-functional NightMode switches** — the NightMode switches for light groups (buses 24/25/27, slot 3) added in v2.63.8 were reverted. The SCU reads back brightness (0–100 %) on these slots, not a boolean — sending `True` is silently ignored. NightMode is likely an app-side convenience that sets brightness via slot 2. Removed switch entries from `hymer.json`, `strings.json`, and `translations/en.json`. Dashboard night mode tiles also removed.

## [2.63.8] - 2026-06-09

### Added

- **Solar MPPT diagnostic sensors promoted to binary_sensor** — bus 8 slots 4–6 renamed from legacy `vent_1`/`vent_2`/`vent_3` to `solar_error`, `solar_reduced_power`, `solar_aes_active` and promoted from plain sensors to `binary_sensor` entities with proper device classes. Slot 7 renamed from `tire_pressure` to `solar_power_raw` (decode-only, superseded by computed V×A). Added to dashboard Energy page.
- **NightMode switches for light groups** (buses 24/25/27, slot 3) — *(reverted in v2.63.9)*.

### Changed

- **Light group slot 3 relabelled** from `color_temp` to `night_mode` on buses 24, 25, 27. The EHG app metadata confirms these are NightMode controls, not color temperature.

## [2.63.7] - 2026-06-09

### Fixed

- **Remove incorrect `color_temp` from kitchen light** (bus 21) — EHG app metadata shows `LightCircuit11` has only 2 slots (on/off + brightness). Slot 3 was incorrectly mapped as color temperature. Removed slot 3 from `hymer.json` and set `color_temp: false` in the lights section.

### Added

- **Complete EHG component slot tables** — expanded `docs/ehg-app-metadata.md` with 35+ new slot tables covering all 127 components (929+ slot definitions). Includes heaters, AC units, fridges, habitation, batteries, toilets, satellite, tanks, chassis, ventilation, switches, dimmers, and lights.
- **DellCool compressor fridge error codes** — documented error codes 0–11 in `docs/sensor-map.md` with detailed descriptions (voltage failure, starting fault, tilt angle, etc.).
- **Dashboard YAML updated** from live HA instance — reflects kitchen light fix and latest entity layout.

## [2.63.6] - 2026-06-08

### Changed

- **Fridge DC voltage now displayed in Volts** — `fridge_dc_voltage` (bus 34, slot 7) converted from raw mV to V via `div1000` transform. Now shows `13.0 V` instead of `13000 mV`. Added `device_class: voltage` for proper HA formatting. Consistent with the compressor fridge supply voltage (bus 114, slot 7) added in v2.63.2.

### Added

- **Fridge warning code labels** — `fridge_warning` (bus 34, slot 6) now shows human-readable labels instead of raw integers. Thetford N4000 absorber codes 0–13 are mapped to "Error 0" through "Error 13" (EHG app only provides generic "check the manual" descriptions for this fridge type). DellCool compressor error codes 0–11 (with detailed descriptions like "Voltage failure", "Starting fault", "Abnormal tilt angle") are documented in `pia_decoder.py` for future use when the correct bus 114 PIA slot is confirmed.

## [2.63.5] - 2026-06-08

### Added

- **Bus 76 — ML-T 570 water tank levels.** The ML-T 570 uses **bus 76** for water levels instead of bus 3 slots 8/9 (which the S 600/S 700 uses via the CBE EBL402). Confirmed by @mcfly1969 by running water and watching discovered sensor changes ([#8](https://github.com/BetaHydri/hymer-connect-ha-ble/issues/8)):
  - `76,1` → `sensor.fresh_water_level`: fresh water tank level in % (value decreases when water flows)
  - `76,2` → `sensor.gray_water_level`: grey water tank level in % (value increases when drain fills)
- The existing `fresh_water_level_ebl` / `grey_water_level_ebl` sensors (bus 3, slots 8/9) remain for S 600/S 700 users.

## [2.63.4] - 2026-06-08

### Removed

- **Duplicate `water_pump` binary sensor** — the hardcoded static `binary_sensor.water_pump` in `binary_sensor.py` (reading from `signalr_sensors.light_nightlight` — a legacy cross-reference from the v1.x era when bus 16 was misidentified as the water pump) conflicted with the new JSON-driven `binary_sensor.water_pump_active` from v2.63.3. Both had `translation_key: "Water pump"`, causing HA to create a duplicate `_2` entity. Removed the hardcoded static entry; the correct pump status is now solely provided by `water_pump_active` (bus 3, slot 3) via `base.json`.

## [2.63.3] - 2026-06-08

### Changed

- **Rename `charger_active` → `water_pump_active`** — bus 3 slot 3 was misidentified as the EBL charger in early development (v2.8.0). Confirmed on both the S 600 and ML-T 570 that this slot is actually the **water pump on/off state** — the `water_pump_ctrl` switch already writes to the same `(bus=3, sid=3)`. Sensor now uses `device_class: running` and `mdi:water-pump` icon instead of the misleading `battery_charging` / `mdi:battery-charging`. The old `charger_active` translation key is retained so HA’s entity registry migration finds it. Triggered by @mcfly1969’s observation in [#8](https://github.com/BetaHydri/hymer-connect-ha-ble/issues/8).

### Breaking

- `binary_sensor.charger_active` → `binary_sensor.water_pump_active`. Dashboard cards or automations referencing the old entity ID must be updated. **Existing users**: after HACS update + HA restart, the old entity becomes orphaned — delete it from Settings → Entities and use the new one.

## [2.63.2] - 2026-06-08

### Changed

- **Bus 114 slot 7 identified as fridge supply voltage** — the value oscillates between 12800 and 12900, corresponding to **12.8–12.9 V** in millivolts. Voltage drops under compressor load and recovers when the compressor pauses — classic battery behavior. Renamed from `fridge_compressor_warning` to `fridge_compressor_supply_voltage` with `unit: V`, `transform: div1000`, `device_class: voltage`. Confirmed dynamic by @mcfly1969 ([#8](https://github.com/BetaHydri/hymer-connect-ha-ble/issues/8)).

### Added

- **Bus 74 — first SIU Smart Temperature Sensor mapped.** The EHG ecosystem uses a Smart Interface Unit (SIU) to connect external BLE sensors to the SCU. Bus 74 is the first SIU sensor bus ever confirmed in this integration. Confirmed by @mcfly1969 on the ML-T 570 CrossOver ([#8](https://github.com/BetaHydri/hymer-connect-ha-ble/issues/8)):
  - `74,1` → `sensor.smart_temperature_1`: temperature in °C (`device_class: temperature`). User reports 37.0 °C matching EHG app.
  - `74,2` → `sensor.smart_humidity_1`: humidity in % (`device_class: humidity`). User reports 32–33 % matching EHG app.
- The ML-T 570 has 3 SIU temperature sensors in the EHG app (Kühlschrank / Schlafbereich / Wohnbereich) but only bus 74 is visible so far. The other two likely use different discovered buses (71, 73, 76, etc.) — under investigation.

### Breaking

- **ML-T 570 users**: `sensor.fridge_compressor_warning` renamed to `sensor.fridge_compressor_supply_voltage`. Dashboard cards or automations referencing the old entity ID must be updated.

## [2.63.1] - 2026-06-07

### Changed

- **Bus 114 fridge corrected from Dometic to Thetford Compressor T2120C** (Item-No: 693465, 101.6 L + 17 L freezer). User @mcfly1969 confirmed the ML-T 570 CrossOver uses a Thetford compressor fridge, not Dometic ([#8](https://github.com/BetaHydri/hymer-connect-ha-ble/issues/8)). All entity IDs renamed from `fridge_dometic_*` to `fridge_compressor_*`.

### Added

- **Bus 114 slot 3** — `fridge_compressor_cooling_step`: main compartment cooling step 1–5, writable via `select.fridge_compressor_cooling_step_ctrl` (stepped-switch driver). Confirmed by @mcfly1969.
- **Bus 114 slot 5** — `fridge_compressor_door`: door open/closed (`binary_sensor`, `device_class: door`). Confirmed by @mcfly1969.
- **Bus 114 slot 6** — `fridge_compressor_slot6`: purpose unknown, user reports value 15 (possibly internal fridge temperature °C). Under observation.
- **Bus 114 slot 7** — `fridge_compressor_warning`: warning/error code sensor. Shows "unavailable" when no active fault (normal).

### Breaking

- **ML-T 570 users**: all bus 114 entity IDs changed from `fridge_dometic_*` to `fridge_compressor_*`. Dashboard cards and automations referencing the old IDs must be updated:
  - `binary_sensor.fridge_dometic_power` → `binary_sensor.fridge_compressor_power`
  - `binary_sensor.fridge_dometic_silent` → `binary_sensor.fridge_compressor_silent`
  - `switch.fridge_dometic_power_ctrl` → `switch.fridge_compressor_power_ctrl`
  - `switch.fridge_dometic_silent_ctrl` → `switch.fridge_compressor_silent_ctrl`
  - `select.fridge_dometic_freezer_ctrl` → `select.fridge_compressor_freezer_ctrl`

## [2.63.0] - 2026-06-02

### Added

- **Generic JSON-driven `stepped_switch` select driver** for stepped appliances (fridge cooling steps, freezer levels, fan speeds, etc.). New devices can be added by editing the brand overlay JSON only — no Python changes required. The driver lives in `select.HymerSteppedSelect`, reads its definitions from `climate.selects.<key>` in any `sensor_maps/*.json` overlay, and supports multi-step write recipes with optional inter-step delays. Existing entities are unaffected:
  - The Thetford T2000 fridge driver (`select.fridge_mode_ctrl`, S 600 / S 700) is unchanged.
  - The Truma Combi heater / boiler driver (`select.boiler_mode_ctrl`, `select.heater_energy_ctrl`) is unchanged.
- **Dometic compressor fridge freezer compartment is now writable** on the HYMER ML-T 570 CrossOver: new `select.fridge_dometic_freezer_ctrl` with options `Off / 1 / 2 / 3`, driven by the new stepped-switch driver. Confirmed against bus 114 slot 4 ([#7](https://github.com/BetaHydri/hymer-connect-ha-ble/issues/7), [#8](https://github.com/BetaHydri/hymer-connect-ha-ble/issues/8)).

### Changed

- Stepped-switch selects get their display name **directly from the JSON `name` field** — no edits to `strings.json` / `translations/en.json` are needed when you add a new stepped device. (Sensors / lights / switches / the existing fridge / heater selects still use translation keys and the dual-file rule still applies to those — see the docs.)

### Breaking

- `sensor.fridge_dometic_freezer` (read-only sensor introduced in v2.62.29) is **removed**. The same value is now exposed as the writable `select.fridge_dometic_freezer_ctrl`. ML-T 570 users with dashboard cards or automations referencing `sensor.fridge_dometic_freezer` must replace them with `select.fridge_dometic_freezer_ctrl`. The underlying SCU value remains available in coordinator data as `signalr_sensors.fridge_dometic_freezer`.

### Notes

- Full Dometic *cooling step* (slot 114,3) and *door / warning* sensors (slots 5 / 7) are still pending real-vehicle confirmation from [@mcfly1969](https://github.com/mcfly1969); once confirmed they will reuse the same `stepped_switch` driver (no further code changes needed). See [#8](https://github.com/BetaHydri/hymer-connect-ha-ble/issues/8).
- Documentation: see the new **"Stepped switch / select driver"** section in `docs/sensor-map.md` for the JSON schema and a worked Dometic example.

## [2.62.29] - 2026-06-01

### Added

- **HYMER ML-T 570 CrossOver Dometic compressor fridge mappings (read + switch).** Added bus 114 to `sensor_maps/hymer.json` after [@mcfly1969](https://github.com/mcfly1969) confirmed it at the vehicle via the dynamic-discovery diagnostic sensors ([#7](https://github.com/BetaHydri/hymer-connect-ha-ble/issues/7)):
  - `114,1` → `binary_sensor.fridge_dometic_power` + `switch.fridge_dometic_power_ctrl` (on/off, write bool)
  - `114,2` → `binary_sensor.fridge_dometic_silent` + `switch.fridge_dometic_silent_ctrl` (night / silent mode, write bool)
  - `114,4` → `sensor.fridge_dometic_freezer` (freezer compartment level, 0 = Off, 1–3 = step; read-only for now)
- This is the **first Dometic compressor fridge** ever mapped in this repo. All previously supported fridges (S 600 / S 700 etc.) are Thetford on bus 34/37 — those mappings are untouched, so both fridge types coexist without any user action.

### Notes

- Sensor-map-only change — no Python code modified.
- Full Dometic *climate / select* entity (writable cooling step 1–5, door sensor, warning bits, freezer-step write) is tracked separately for **v2.63.0** and requires real-vehicle confirmation of bus 114 slots 3, 5 and 7 first. See the tracking issue for status.
- Existing `discovered_bus_114_*` diagnostic sensors become redundant for slots 1, 2 and 4 once the named entities appear and can be disabled.

## [2.62.28] - 2026-06-01

### Added

- **HYMER ML-T 570 CrossOver light mappings.** Added bus 14 (`light_bedroom_ceiling` / `..._brightness`) and bus 66 (`light_dinette_pendant` / `..._brightness`) to `sensor_maps/hymer.json`. Both lights are dimmable (0–100 %), no color-temperature channel. Bus 14 is a member of the bus 27 *Privat* group; bus 66 of the bus 24 *Wohnen* group, so the existing group toggles also drive them. No conflict with Grand Canyon S 600 / S 700 mappings — these buses are not used on those models. Confirmed by [@mcfly1969](https://github.com/mcfly1969) via the dynamic-discovery diagnostic sensors and verified at the vehicle ([#7](https://github.com/BetaHydri/hymer-connect-ha-ble/issues/7)).

### Notes

- Sensor-map-only change — no Python code modified. After updating via HACS and reloading the integration, two new light entities (`light.<device>_bedroom_ceiling` and `light.<device>_dinette_pendant`) plus their underlying on/off + brightness sensors are created automatically.
- Existing `discovered_bus_14_*` / `discovered_bus_66_*` diagnostic sensors from dynamic discovery become redundant once these named entities appear and can be disabled.

## [2.62.27] - 2026-05-21

### Fixed

- **Broken EHG Token Extractor download link in README.** The README pointed at `releases/latest/download/ehg-token-extractor.apk`, but the `build-token-app` workflow only ever uploaded the APK as a GitHub *Actions artifact* (login required, 90-day retention) — it was never attached to a GitHub Release, so the link 404'd. The workflow now triggers on `release: published`, builds the APK, and attaches it to the release as `ehg-token-extractor.apk`. From this release onward, the README link resolves correctly for every release. `workflow_dispatch` also accepts an optional `release_tag` input for backfilling older releases.

### Notes

- No integration / runtime behaviour change. CI-only release.
- Version v2.62.26 was prepared but its release tag could not be created due to the repository's immutable-releases setting (now disabled). v2.62.27 supersedes it; there is no v2.62.26 release.

## [2.62.25] - 2026-05-21

### Removed

- Deprecated BLE-write constants `CONF_CLOUD_FALLBACK`, `CONF_BLE_ACK_TIMEOUT`, `DEFAULT_BLE_ACK_TIMEOUT`, `MIN_BLE_ACK_TIMEOUT`, `MAX_BLE_ACK_TIMEOUT` deleted from `const.py`. They were already unused after v2.62.24; Home Assistant silently ignores unknown keys in saved options dicts, so dropping the Python identifiers is safe.

### Changed

- `pia_decoder.build_light_command()` and `build_multi_sensor_command()` now log a neutral *"no cached instance (cloud accepts both)"* at DEBUG instead of the stale *"NO cached instance — write may be dropped"* warning. The original message was a leftover from the BLE-write era and was misleading on the cloud-only write path shipped in v2.62.24.

## [2.62.24] - 2026-05-21

### Removed

- **BLE write path removed; all commands now go via cloud / SignalR.** After the v2.62.17 → v2.62.23 investigation (per-bus instance cache, depth-walking seeder, tunable ACK timeout) we conclusively proved on a Grand Canyon S 600 with SCU firmware **1.12.0.0** that every BLE `setValues` write is silently dropped by the SCU. A decisive test with `cloud_fallback=OFF` and `ble_ack_timeout=4000ms` produced **0/5 successful writes** across the fridge (bus 34), Truma heater (bus 58) and lights (buses 12/19), and the EHG app on LTE confirmed no SCU state change. The supposed "BLE ACKs" observed with cloud fallback enabled in v2.62.17–v2.62.22 were in fact cloud-driven echoes relayed back over BLE ~500 ms after the SignalR send — they were not driven by the BLE write itself.
  - `coordinator._send_with_retry()` now routes **every** command straight to the cloud / SignalR path with one reconnect-retry. The BLE preflight, ACK wait, and cloud-fallback dance have been deleted.
  - `coordinator._send_via_ble()` is retained as a no-op stub so any external plug-in that imported it keeps working; it returns `False` and logs at DEBUG.

### Changed

- **BLE is now a read-only mirror.** `ble_enabled` still subscribes to the SCU's sensor-push stream over BLE for low-latency local updates and continues to seed the per-bus instance cache (free, future-proof). It no longer participates in any write path. The option label in the *Configure* dialog was updated to *"Enable BLE direct path (sensor reads only)"* with a description explaining that writes go via cloud.
- **`_seed_instance_cache_walk()` is retained** — it adds no traffic, primes a useful debugging signal, and would be needed unchanged if a future SCU firmware unlocks BLE writes.

### Deprecated

- `CONF_CLOUD_FALLBACK` (`cloud_fallback` Options key) — no longer read by the coordinator. Existing values in saved options dicts are ignored. The constant is retained in `const.py` so older code paths importing it don't crash. The option is no longer shown in the *Configure* dialog.
- `CONF_BLE_ACK_TIMEOUT` / `DEFAULT_BLE_ACK_TIMEOUT` / `MIN_BLE_ACK_TIMEOUT` / `MAX_BLE_ACK_TIMEOUT` — same treatment. There is no BLE ACK to wait for any more, so a timeout is meaningless.
- Both keys will be removed in a future release once we are confident no users still depend on the legacy schema.

### Notes

- Users on v2.62.17 → v2.62.23 with `cloud_fallback=ON` (the default) will not notice any behaviour change other than slightly snappier commands (no 2.5 s BLE wait before the cloud send fires).
- Users who set `cloud_fallback=OFF` to test BLE writes will find that commands now work again — they were silently failing in those releases.
- If a future SCU firmware fixes the BLE write path, restoring the BLE-first leg is a localised change in `coordinator._send_with_retry`. The PIA encoder (`build_light_command` / `build_multi_sensor_command`) and instance-cache seeder are still in place.

## [2.62.23] - 2026-05-21

### Fixed

- **BLE write instance-cache now seeds from cloud SignalR responses** — v2.62.21 added a per-bus `_BUS_INSTANCE_CACHE` so BLE `setValues` writes could echo back `connectedComponentInstance` (CCValue field 10), which the SCU requires on buses like 34 (fridge `lin1`), 58 (Truma heater) and 99 (BMS `can2`). However the populator only fired from inside `_parse_sensor_entry`, which is gated by the sensor-mapping depth filter — and crucially **BLE push frames are depth-2 `PushSensorValue` blocks whose CCValue body is only 8 bytes and omits field 10 entirely**. Result: the cache was never primed via the BLE path, and every BLE write for instanced buses still went out without field 10 → silently dropped by the SCU (vehicle log 2026-05-21 07:24: `bus=19 sid=1: NO cached instance — write may be dropped`).
  - New `_seed_instance_cache_walk()` in `pia_decoder.py` walks every inbound PIA payload at all depths (up to 8) looking for any nested message that carries simultaneously field 1 (sid varint), field 2 (bus varint), and field 10 (length-delim instance bytes). Bus + instance are cached unconditionally — independent of the sensor-mapping depth gate.
  - Called from `decode_pia_payload()`, so it primes the cache from **both** the SignalR PiaResponse pipeline (which already delivers field 10 in its subscription response for every instanced bus) and the BLE pipeline (no-op for current SCU firmware, but future-proof).
  - Zero new BLE traffic: the cloud subscription response is already received within seconds of HA startup and contains the full component catalog. By the time the user issues their first BLE write, the cache is primed.
  - Cache writes are logged at DEBUG: `Instance cache seeded (cloud/BLE walk): bus=99 sid=5 instance=b'can2'`.
  - The existing `_parse_sensor_entry` populator is retained as a belt-and-suspenders fallback for the BLE-only / no-cloud configuration.

## [2.62.22] - 2026-05-21

### Added

- **User-tunable BLE ACK timeout** — new `ble_ack_timeout` option in the integration's *Configure* dialog. Range 1.0–5.0 s, default **2.5 s** (lowered from the previous hard-coded 3.0 s). Vehicle measurements show the SCU echoes accepted writes in 1969–2331 ms, so 2.5 s keeps a small safety margin while making the cloud-fallback path snappier when the SCU silently drops writes. Lower values further reduce perceived UI lag at the cost of more frequent false fallbacks when the BLE path is healthy.
  - Log lines now report the effective timeout (e.g. `BLE ACK timeout (2500ms): ...`).
  - `coordinator.py` reads the option on every command, so changes take effect without an integration reload.
  - New constants in `const.py`: `CONF_BLE_ACK_TIMEOUT`, `DEFAULT_BLE_ACK_TIMEOUT` (2.5), `MIN_BLE_ACK_TIMEOUT` (1.0), `MAX_BLE_ACK_TIMEOUT` (5.0).

## [2.62.21] - 2026-05-21

### Fixed

- **BLE writes now include `connectedComponentInstance` (CCValue field 10)** — the SCU was silently dropping every `setValues` command on buses that carry a per-instance identifier (e.g. CAN/LIN bus 99 BMS = `b"can2"`). Discovered by analysing v2.62.20 hex-dump logs from a real vehicle test: the SCU's own outbound responses for bus 99 BMS carried `52 04 63616e32` (field 10, 4 bytes, `b"can2"`) while every write attempt from the integration (lights bus 19/12, fridge bus 34, Truma heater bus 58 pair-write) omitted the field and timed out without any SCU response — cross-confirmed with the EHG app showing no physical state change.
  - Added per-bus `_BUS_INSTANCE_CACHE` in `pia_decoder.py`, populated automatically by `_parse_sensor_entry` from every inbound CCValue that carries a field-10 instance.
  - `build_light_command` and `build_multi_sensor_command` now consult the cache and echo the cached instance back as `field 10 = bytes` on every outbound write. Buses with no cached instance (e.g. bus 30 SCU singleton) continue to work as before — the field is simply omitted.
  - Added `get_bus_instance(bus_id)` public helper and DEBUG logging for cache hits/misses on every write build.

## [2.62.20] - 2026-05-20

### Reverted

- **v2.62.19 `connectedComponentIndex` fix was incorrect** — Deeper decompile of the EHG app's `toPiaValues` builder (`index.js:1333489`) and `ConnectedComponentValue.encode` (`index.js:500080`) confirmed that field 9 (`connectedComponentIndex`) is **never** populated during normal write flows; only field 1 (`connectedComponentValueId`), field 2 (`connectedComponentId`), the type-specific value field (3/4/5/6), and conditionally field 10 (`connectedComponentInstance`) are set. The v2.62.19 addition of `field 9 = 0` did not match the real app output and did not fix the silent SCU drop on BLE writes. Reverted in `build_light_command` and `build_multi_sensor_command`.

### Added

- **BLE wire-level hex-dump logging** — At DEBUG level the BLE client now logs the plaintext PIA payload of every outbound `send_pia_command` and every inbound response after decryption. Enables forensic capture of failed light/heater/fridge writes for byte-level comparison against real EHG-app BLE traffic. Enable with:
  ```yaml
  logger:
    logs:
      custom_components.hymer_connect.ble_client: debug
      custom_components.hymer_connect.pia_decoder: debug
  ```

### Investigation status

Structural analysis confirms our setValues envelope matches the EHG app byte-for-byte except for the (now reverted) spurious field 9. Working BLE subscriptions use the same outer wrap and same `Request.connectedComponent` field 4 path. Remaining hypotheses (require capture from v2.62.20 logs to disambiguate): (a) `Request.user` (field 8) required for writes; (b) `connectedComponentInstance` (field 10) required per bus type; (c) SCU firmware quirk requiring session refresh before writes. See `/memories/repo/ble-investigation.md` for full schema documentation.

## [2.62.19] - 2026-05-20

### Fixed

- **BLE write commands now work — missing `connectedComponentIndex` field** — The SCU silently dropped BLE write commands (lights, switches, heater, fridge) because the protobuf payload was missing the `connectedComponentIndex` field (protobuf field 9, always 0). This field is required by the SCU for BLE-path command processing but not for cloud/SignalR. Discovered by decompiling the EHG Android app's Hermes bytecode and comparing the `ConnectedComponentValue` protobuf schema against our `build_light_command()` output. BLE sensor reads were unaffected because they use a different message type (`getCapabilities`/`getValues`).

## [2.62.18] - 2026-05-20

### Added

- **Cloud fallback toggle in integration options** — New "Cloud fallback on BLE timeout" checkbox (enabled by default). When disabled, BLE commands that don't receive an ACK within 3 seconds are NOT re-sent via cloud/SignalR. Useful for BLE-only testing, debugging SCU response times, or avoiding dashboard toggle flicker from double-sends. Found under Settings → Integrations → HYMER Connect BLE → Configure.

## [2.62.17] - 2026-05-20

### Fixed

- **BLE ACK timeout too short (1.5s → 3.0s)** — Vehicle testing showed SCU responds to BLE commands in 1969–2331ms consistently, well past the 1500ms timeout. Every BLE command was falsely re-sent via cloud, causing unnecessary dashboard toggle flicker and double-sends. Increased timeout to 3000ms based on measured response times.
- **False BLE ACK from unrelated sensor pushes** — The ACK mechanism treated *any* PIA response as confirmation of a pending command. If an unrelated periodic sensor update (e.g. `battery_current`) arrived during the ACK wait window, it was wrongly counted as a command ACK. Now tracks the expected sensor name from the commanded `(bus, slot)` via the sensor map and only counts matching responses.

## [2.62.16] - 2026-05-20

### Changed

- **Custom app icon for EHG Token Extractor** — The Android token extractor app now uses the HC CONNECT logo with a key badge overlay instead of the default Android icon. Rebuilt APK attached to this release.

## [2.62.15] - 2026-05-20

### Changed

- **Attach token extractor APK to GitHub releases** — The EHG Token Extractor Android app (`ehg-token-extractor.apk`) is now attached as a release asset, downloadable without a GitHub account. Previously it was only available as a GitHub Actions artifact (login required). Updated README download link to point directly to the release asset.

## [2.62.14] - 2026-05-19

### Fixed

- **Correct misleading GPS sensor descriptions** — The README GPS sensor row and dashboard table listed altitude, heading, satellites, signal quality, and fix status as GPS entities. These slots (bus 30, slots 3–7) were relabelled in v2.62.12 to their actual functions (LTE signal, SCU voltage, BT device counts). Only slot (30,1) `gps_coordinates` is actual GPS data. Updated all documentation to reflect reality.
- **Remove orphaned `gps_fix` translation key** — Cleaned up stale `gps_fix` entries from `strings.json` and `translations/en.json`. This entity was relabelled to `lte_connection_state` in the sensor map but the old translation keys were never removed.

### Added

- **Document Find-My-RV prerequisite for GPS** — GPS coordinates require the "Find-My-RV" service to be enabled in the EHG app (Mehr → Services und Abonnements). Added prerequisite callouts to the README sensor table, Device Tracker section, and `docs/sensor-map.md` bus 30 section.

## [2.62.13] - 2026-05-19

### Changed

- **Move dev tools out of custom_components/** — `decode_cmds.py` (offline protobuf decoder) and `mitm_hymer_ws.py` (mitmproxy addon) are standalone developer tools not imported by the integration. Moved to `tools/` so they no longer ship with HACS installs.

## [2.62.12] - 2026-05-19

### Fixed

- **Correct misleading Speed/RPM/Torque documentation** — The README incorrectly stated that speed, RPM, and engine torque were available on the S700 but not the S600. In reality, the SCU never exposes these driving sensors via PIA on any Mercedes-based EHG model — the original bus 1 slot labels were wrong, not model-specific. Corrected based on @dan-simms1’s S700 verification (#37). Added mbapi2020 reference for driving data.
- **Update stale bus 1 labels in mitm_hymer_ws.py** — The standalone WebSocket capture tool still used legacy-incorrect names (speed, rpm, coolant_temp, door_sliding, etc.). Updated all 23 bus 1 entries to match the corrected `base.json` mapping.
- **Remove orphaned translation keys** — Cleaned up unused `speed`, `rpm`, and `engine_torque` entries from `strings.json` and `translations/en.json` that no sensor map references.

## [2.62.11] - 2026-05-18

### Fixed

- **Correct stale BLE sensor count in log message** — After BLE PIA subscriptions are sent successfully, the SCU pushes all ~130 sensors over BLE (not just ~28 autonomous sensors). The startup log message now correctly reflects this: "both paths: ~130 sensors" instead of the outdated "BLE: ~28 sensors".

## [2.62.10] - 2026-05-16

### Fixed

- **Proactive re-auth before commands during extended standby** — Backport of hymer-connect-ha v2.56.1. After >10 min of SCU standby (12V off), the server-side SignalR hub→SCU routing becomes stale, causing commands to be silently dropped. Previously relied on `_verify_send` (60s timeout) for reactive recovery — too slow, required manual reload. Now `async_ensure_signalr_healthy()` proactively detects extended standby and forces a full OAuth2 re-auth + SignalR reconnect BEFORE sending the command.

### Migration Notes

- **Non-breaking.** HACS update + restart is sufficient.

## [2.62.9] - 2026-05-14

### Fixed

- **Force re-auth on command failure during extended standby** — backport of hymer-connect-ha#58 / v2.56.0. When the SCU has been offline for more than 10 minutes (extended standby), the cloud session's OAuth2 routing becomes stale and commands are silently dropped by the server instead of being queued. `_verify_send` now detects extended standby via `_scu_disconnected_at` and triggers `force_reauth_and_reconnect()` with a single retry, instead of the previous "command queued until SCU wakes" give-up path. Short standby (<10 min) retains the existing queue-and-wait behavior.

## [2.62.8] - 2026-05-13

### Fixed

- **Fix `_scu_address` AttributeError preventing BLE listen loop from starting** — the background task name referenced `self._scu_address` which does not exist on the coordinator; corrected to `self.ble_address`. This was the actual root cause of both the `coroutine _ble_listen_loop was never awaited` RuntimeWarning and the cloud fallback.

## [2.62.7] - 2026-05-13

### Fixed

- **Fix `coroutine _ble_listen_loop was never awaited` warning** — v2.62.6 used `config_entry.async_create_background_task()` which failed silently (likely `self.config_entry` not yet set on the coordinator at that point), causing the coroutine to be garbage-collected without scheduling. Now uses `hass.async_create_background_task()` directly, which is always available. The BLE listen loop is still fire-and-forget (not blocking bootstrap/shutdown); it just isn't tied to config entry lifecycle cancellation (the loop has its own `finally` cleanup).

## [2.62.6] - 2026-05-13

### Fixed

- **Fix bootstrap timeout caused by BLE listen loop** — The BLE listen loop (`_ble_listen_loop`) was started with `hass.async_create_task()`, which HA tracks during bootstrap and shutdown. Since the listen loop runs indefinitely (30s `uart_queue.get()` cycles), HA's bootstrap would time out waiting for it with `"Setup timed out for bootstrap waiting on _ble_listen_loop"`. Now uses `config_entry.async_create_background_task()` which is truly fire-and-forget and not awaited during bootstrap/shutdown.

## [2.62.5] - 2026-05-13

### Changed

- **Use `bleak-retry-connector` for BLE connections** — BLE GATT connections now use Home Assistant's `establish_connection()` from `bleak-retry-connector` instead of raw `BleakClient.connect()`. This provides automatic retries with exponential backoff for transient BLE errors (D-Bus glitches, adapter slot exhaustion, broken pipes) and eliminates the `BleakClient.connect() called without bleak-retry-connector` deprecation warning. Falls back gracefully to raw `BleakClient` when running standalone (tools). All existing bonding, retry, and TLS logic is preserved.

### Fixed

- **Potential `NameError` on BLE connection failure** — When GATT connection failed before a `BleakClient` was created, the stale-bond retry path could reference an unassigned `client` variable. Now safely guarded with `if client:` check.

## [2.62.4] - 2026-05-13

### Fixed

- **BLE ACK timeout increased from 500ms to 1500ms** — Vehicle testing showed the SCU responds to BLE commands in 600-1100ms depending on the target bus. The 500ms timeout was too tight, causing unnecessary cloud double-sends that produced dashboard toggle flicker (light appearing to switch twice). With 1500ms, the BLE ACK is captured for all observed response times while the cloud safety net still catches genuine BLE failures.

## [2.62.3] - 2026-05-13

### Fixed

- **SCU Restart button availability** — The Restart SCU button was only available when SignalR (cloud) was connected, ignoring BLE connectivity. On the Vehicle HA instance, if SignalR was disconnected but BLE was active, the button was greyed out and unusable. Now the button is available when **either** BLE or SignalR is connected, matching the dual-path command routing in `_send_with_retry()`.

## [2.62.2] - 2026-05-13

### Fixed

- **Bonding-aware BLE backoff** — When the SCU rejects `Device1.Pair()` with `AuthenticationFailed` (CONNECTION not pressed), the integration now applies a 2→3→4→5 min escalating backoff instead of retrying every 60s. This cuts wasted GATT+Pair churn from 5 attempts to ~2 before backing off, while still catching the button press within a reasonable window. Non-bonding failures (timeout, device not found) keep the existing fast-retry-then-escalate behavior. Log messages now show `(bonding rejected)` suffix for clarity.

### Added

- **BLE pairing documentation** — New README section "BLE Pairing — How It Works at the Vehicle" with sequence diagram, step-by-step guide, log message reference table, post-pairing lifecycle, and retry behavior. Covers the full flow from pressing CONNECTION to token storage.

## [2.62.1] - 2026-05-13

### Fixed

- **Force GATT reconnect after fresh bonding** — After a successful `Device1.Pair()`, the pre-bonding BleakClient still reports `is_connected=True` but its GATT service handle table is invalidated by the encryption layer change. The TLS ClientKeyExchange write (342 bytes → 18 chunks) failed at chunk 6 with `Service Discovery has not been performed yet`. Now the code always disconnects and reconnects with a fresh BleakClient after a new bonding, regardless of `is_connected` state. Already-bonded devices (where `Pair()` was skipped) are unaffected.

## [2.61.3] - 2026-05-13

### Fixed

- **Increase WriteReq pacing from 50ms to 100ms for TLS handshake** — Vehicle testing showed ATT error 0x0e at chunk 16/18 of the 342-byte TLS CertificateVerify message with 50ms pacing at MTU=23. Write-With-Response adds ~30ms ACK overhead (effective ~80ms), which wasn't enough for the SCU's NUS RX buffer to drain. Increased to 100ms (effective ~130ms per chunk, ~2.3s total for 18 chunks).
- **Dashboard entity reference** — Fixed `update.hymer_connect_update` → `update.hymer_connect_ble_update` in shipped dashboard YAML.

## [2.61.2] - 2026-05-13

### Fixed

- **Revert skip-bonding — SCU requires BLE bonding for TLS** — Vehicle testing confirmed the SCU silently ignores TLS data on an unbonded GATT link (20s timeout, no ServerHello). The `skip_bonding` parameter introduced in v2.61.1 is removed. Bonding decision is now based solely on BlueZ bond state: if bonded → skip `Pair()`, if not bonded → attempt `Pair()` (requires CONNECTION button). The retry-before-clearing logic from v2.61.1 is preserved to protect valid bonds from transient failures.

## [2.61.1] - 2026-05-13

### Fixed

- **Skip BLE bonding when EHG token already exists** — After a successful pairing ceremony, subsequent restarts no longer attempt `Device1.Pair()`. A failed `Pair()` call corrupts the GATT session, making TLS impossible. Now, when an EHG refresh token is already stored, `connect(skip_bonding=True)` proceeds directly to notify + TLS without touching the bonding layer.
- **Retry before clearing BLE bond** — When a bonded device rejected a GATT connection (e.g. SCU still recovering from a prior ATT error), the code immediately destroyed the bond via `RemoveDevice`, requiring the user to physically press CONNECTION again. Now it retries once after a 2s delay before clearing the bond. Only clears on the second consecutive failure.
- **Skip redundant Pair() for already-bonded devices** — When BlueZ already has valid bonding keys, the bonding step is now skipped entirely instead of calling `Pair()` again (which could trigger unnecessary `AuthenticationFailed` errors).
- **Preserve BLE address on stale bond errors** — The coordinator no longer clears the stored SCU BLE address when a stale bond is cleared. The address is still valid; only the bond keys were stale.

## [2.61.0] - 2026-05-13

### Added

- **BLE pairing and data path** — Full BLE communication with the SCU via Nordic UART Service (NUS) over TLS-encrypted PIA protobuf. Includes BLE device scanning, GATT bonding, TLS 1.1 handshake, PairMobileRequest ceremony, PIA subscription, and bidirectional command/sensor data.
- **Token exchange diagnostic logging** — Debug/warning logging for `get_remote_access_token()` and `_refresh_access_token()` in `api.py`, completing the token lifecycle logging chain from BLE pairing through EHG exchange to SignalR authentication.
- **README: token exchange troubleshooting profile** — Dedicated `logger` configuration block for troubleshooting EHG token exchange and authentication issues.

### Fixed

- **BLE write pacing for MTU=23** — Both WriteReq (PairMobileRequest, 63 chunks) and WriteCmd (PIA subscriptions, 70 chunks) overflowed the SCU's NUS RX buffer at fast pacing rates, causing `ATT error: 0x0e (Unlikely Error)` and immediate BLE disconnect. WriteReq pacing set to 50ms, WriteCmd pacing set to 20ms for large writes (>10 chunks).
- **Stale BleakClient after BLE session death** — When the BLE TLS session died, the coordinator left the dead `BleakClient` in place, causing subsequent commands to fail with `Service Discovery has not been performed yet`. The listen loop now uses a `finally` block to disconnect and nullify the client, ensuring a fresh session on reconnect.
- **Per-chunk error logging** — When a GATT write fails mid-stream, the log now shows exactly which chunk number failed (e.g. `BLE TX chunk 12/63 failed after 11 successful writes`).

## [2.61.0-alpha.10] - 2026-05-09

### Fixed (ported from cloud repo v2.54.0–v2.55.2)

- **Retry command after dead SignalR send channel** — When `_verify_send` detects SCU readback mismatch after holdoff, it now waits up to 90 s for SignalR reconnect and re-sends the failed command once. Previously the command was silently lost. (cloud v2.54.0)
- **Refresh UpdateTokens on SCU standby transition (true→false)** — When the SCU enters standby, the Azure SignalR hub routing becomes stale for the send direction. Now both `false→true` (wake) and `true→false` (standby) transitions trigger an UpdateTokens refresh. On standby entry, resubscribe is skipped to avoid stale state echoes that would overwrite real values (e.g. main_switch "Off" reverting to "On"). (cloud v2.55.0 + v2.55.1)
- **12V OFF confirmed by SCU going offline** — Main switch OFF + SCU offline is now treated as confirmed (the SCU going to standby IS the proof that 12V went off). No more UI revert to stale "On" state. (cloud v2.55.1)
- **Immediate reconnect on dead send channel** — `_verify_send` now triggers `async_request_refresh()` instead of passively waiting for the next 60 s poll, cutting recovery from ~5 min to ~6 s. (cloud v2.55.2)
- **Reset `_shutting_down` flag after successful reconnect** — `start_signalr()` now resets `_shutting_down` after successful reconnect so `_on_signalr_connection_lost` is not permanently suppressed after the first age-based reconnect cycle. (cloud v2.55.2)
- **Rename `heater_window_switch_closed` → `heater_diesel_safety`** — Bus 58 sid 14 is the Truma Combi D6E diesel safety interlock flag, not a window contact. Removed misleading `device_class: window`, updated icon to `mdi:shield-check`. (cloud v2.55.0)

### Changed

- **Dashboard** — Added `secondary_info: last-changed` to Outside Temp entities (3 locations) so stale readings with ignition OFF are immediately visible. Updated diesel safety entity reference, renamed "SCU Connected" → "SCU Online", updated SCU Telemetry section icon. Dashboard README title updated to "S600 / S700".
- **Documentation** — Added Bus 1 ignition dependency callout and stale CAN cache theory to `sensor-map.md`. Updated `signalr-connection.md` for diesel safety rename.

## [2.61.0-alpha.9] - 2026-05-09

### Fixed

- **BLE pairing timeout at MTU=23** — The PairMobileRequest (1253 bytes encrypted) was sent as 63 fire-and-forget chunks (WriteCmd, no ACK) at the default MTU of 23. The SCU's NUS RX buffer overflows at that volume, silently dropping chunks and corrupting the protobuf frame. The SCU then ignores it, causing a 120s timeout. Fix: `pair_mobile()` now uses `force_response=True` which forces Write With Response (ACK per chunk), guaranteeing every chunk arrives even at MTU=23.
- **MTU negotiation fallback** — HA's habluetooth-wrapped BleakClient doesn't expose `_acquire_mtu()`. Added a D-Bus fallback that reads the negotiated ATT MTU from BlueZ's `org.bluez.Device1.MTU` property. Logs a warning when MTU stays at 23 so the root cause is visible in diagnostics.

## [2.61.0-alpha.8] - 2026-05-05

### Fixed

- **Translation error: UNCLOSED_TAG** — The OAuth client header description in `en.json` contained `<base64>` which HA’s translation parser treated as an unclosed HTML tag. Replaced with `…` ellipsis to avoid the parse error.

## [2.61.0-alpha.7] - 2026-05-05

### Fixed

- **Bus 30 sensor map corrected** — Slots 2–14 were incorrectly labelled as GPS data (altitude, satellites, heading, etc.). Verified against EHG app Hermes bundle (APK 2.10.14): they are actually SCU internal time, LTE quality/state, SCU voltage, paired/connected BT devices, battery cutoff switch, user active flag, D+ alternator signal, chassis wake-up, battery switch active, shore power (SCU), and vehicle movement. GPS position comes only from slot 1; GPS fix/altitude/satellites are REST API only, not PIA.
- **Fridge door sensor** — Was incorrectly reading from bus 37 slot 2 (`fridge_status`/`VehicleBrand`). Now correctly mapped to bus 34 slot 5 (`DoorOpen`) via JSON. Removed the hardcoded static `fridge_door` entry from `binary_sensor.py`.
- **Removed phantom `ambient_temp` sensor** — Static sensor description in `sensor.py` referenced a non-existent slot. The real temperature source is `outside_temperature` (bus 1, slot 9, Mercedes bumper sensor). Climate `temp_sensor` in `hymer.json` corrected accordingly.

### Added

- **Fridge extended sensors** — `fridge_freezer_level` (34,4), `fridge_warning` (34,6), `fridge_dc_voltage` (34,7) from EHG app metadata.
- **Bus 30 SCU telemetry entities** — `scu_internal_time`, `lte_connection_quality`, `lte_connection_state`, `scu_voltage`, `paired_bt_devices`, `connected_bt_devices`, `battery_cutoff_switch`, `user_active`, `d_plus_signal`, `wake_up_chassis`, `battery_switch_active`, `shoreline_connected_scu`, `vehicle_movement` (most disabled by default).
- **Climate logging** — `_LOGGER.info()` for HEAT on, OFF, and setpoint changes in `climate.py`.
- **Dashboard** — Connectivity section (LTE signal, SCU time, SCU voltage), SCU Telemetry section (bus 30 flags), BMS time remaining gauge. Fixed entity IDs for shore power, heater energy source, fridge ECO, and distance to service.

### Changed

- **Translations** (`strings.json`, `en.json`) — Updated entity section to match corrected sensor names. Removed GPS-assumed entries, added LTE/SCU/fridge extended entries and bus 30 binary sensor names.
- **`sensor-map.md`** — Synced with cloud repo (authoritative reference).

## [2.61.0-alpha.6] - 2026-05-04

### Added

- **Per-entry OAuth client header (`oauth_basic_auth`)** — Ported from the cloud repo (v2.49.0). New optional field in the **initial config flow**, **Reconfigure** dialog, and **Options** screen titled *"OAuth client header"*. Paste the full `Authorization: Basic <base64>` header captured from your own EHG mobile-app traffic; the integration uses your value for OAuth `/token` requests instead of the bundled default. Field is validated client-side (must start with `Basic ` and decode to a non-empty `client_id:secret` pair); a paste mistake surfaces as `invalid_basic_auth`. The reauth dialog continues to use the entry's stored value (no re-paste needed during routine re-auth).

### Changed

- **OAuth Basic-auth handling** — The bundled `OAUTH2_BASIC_AUTH` constant has been renamed to `OAUTH2_BASIC_AUTH_LEGACY_DEFAULT` and is now a *fallback* used only when an entry has no per-entry `oauth_basic_auth` value. When the fallback is hit the integration logs a one-time deprecation warning per setup. The 4 internal `HymerConnectApi(...)` instantiations across `config_flow.py` (`_async_try_authenticate`, the QR-token re-auth, and the two BLE-pairing re-auths) now thread the per-entry header value through.
- **Translations** (`en.json`) updated with labels and descriptions for the new field across user / reconfigure / options init steps and a new `invalid_basic_auth` error string.

### Deprecated

- **Bundled OAuth client header** — The hard-coded fallback used by installs without a configured per-entry value will be **removed** in a future release. Existing installs continue to work unchanged after upgrading; users should paste their own `Basic …` header into the **Reconfigure** or **Options** dialog at their convenience to silence the deprecation warning. The cloud-repo capture tool (`tools/Start-EhgTokenCapture.ps1` in `hymer-connect-ha`) harvests the header from your own EHG-app traffic in the same session that captures the EHG refresh token.

### Migration Notes

- **Non-breaking for existing users.** No entity changes, no forced re-auth. After upgrading you will see one warning per startup (`OAuth client header not configured for this entry; falling back to bundled legacy default…`). To silence it: open *Settings → Devices & Services → HYMER Connect → Configure*, paste your own header into the *OAuth client header* field, and save.
- **For new installs**, the field is presented in the initial config flow (still optional during the deprecation window).
- **BLE-only path is unaffected** by the OAuth header — it only matters for the cloud `/oauth/token` calls used to obtain access tokens for the SignalR fallback and the REST APIs that resolve vehicle metadata.

## [2.61.0-alpha.5] - 2026-05-03

### Documentation

- **Clarified QR activation token source** — README setup steps and config-flow field descriptions (`strings.json` / `translations/en.json`) now state explicitly that the QR code is on the **dealer-provided vehicle handover document**, not a sticker on the SCU or anywhere on the vehicle. Eliminates confusion for users searching the SCU for a non-existent sticker.
- **Repo-relative asset links** — README image, GIF, MP4, and dashboard YAML links now point to the `hymer-connect-ha-ble` repo (previously pointed at the cloud-only `hymer-connect-ha` repo, breaking the dashboard demo GIF and screenshots when viewed from this repo or via HACS).

### Migration Notes

- **Non-breaking** — Documentation and UI strings only. No code, entity, or config changes. HACS update + restart picks up the new strings.

## [2.61.0-alpha.4] - 2026-05-03

### Fixed

- **Commands silently dropped after long SCU standby + 12V wake (cloud path)** — Ported from the cloud repo (v2.48.0). After the SCU spent extended time in standby (12V OFF), waking it up via the dashboard 12V switch sometimes left commands sent over a healthy SignalR WebSocket that never reached the SCU. Reload was the only workaround. Two bugs fixed:
  1. **Race in `_refresh_tokens_and_resubscribe`** — `UpdateTokens` was fire-and-forget and PIA subscriptions were sent before the SignalR backplane confirmed the new routing, so subscriptions / set_value commands were processed under stale standby routing. Fix: 3 s settle before `UpdateTokens`, then 750 ms wait for routing to propagate before subscribing.
  2. **Optimistic `_sensor_data["main_switch"]` write in `switch.py`** — Poisoned `_verify_send` (always self-confirmed) and `is_standby` (bypassed 3-min stale-data reconnect). Fix: removed the cache write; `_optimistic_on` already drives the UI.

  The BLE path is unaffected (it uses a separate transport and its own ACK), but the cloud fallback now self-heals correctly.

### Migration Notes

- **Non-breaking** — No entity or option changes. HACS update + restart.

## [2.61.0-alpha.3] - 2026-05-03

### Changed

- **HACS release packaging** — GitHub Releases now published as regular releases (not pre-release) so HACS discovers them without requiring beta opt-in. The repo itself is the alpha/BLE branch.

## [2.61.0-alpha.2] - 2026-05-03

### Added

- **JWT validation for BLE token extraction** — The BLE pairing flow now validates the EHG refresh token (`ett=access-refresh`) extracted from the SCU's PairMobileResponse protobuf. Checks JWT format (`eyJ` prefix, 3 dot-separated parts) and decodes the payload to log `ett`, `urn`, and token length. Validation is non-blocking — tokens are always stored, but invalid or unexpected formats produce clear warnings in the HA log for troubleshooting. Improves diagnostics when pairing with different EHG vehicle models (Eriba, Bürstner, etc.) that may have different token formats.

## [2.61.0-alpha.1] - 2026-05-03

### Added

- **BLE dual-path command routing** — Write commands (lights, switches, heater, fridge, boiler, climate) now route through the BLE direct path when connected (~50ms latency), with automatic fallback to cloud/SignalR if BLE send fails or is disconnected. Previously, all commands always went through the cloud path even when BLE was connected for sensor streaming.
- **BLE command ACK with cloud safety net** — After sending a command via BLE, the coordinator waits up to 500ms for the SCU to echo back a PIA response (confirming it processed the command). BLE round-trip is ~50–200ms, so 500ms provides 2.5–10x margin. If no response arrives within the timeout, the same command is automatically re-sent via the cloud/SignalR path as a safety net. Commands are idempotent (set-value, not toggle), so a duplicate is harmless.
- **`_send_via_ble()`** — New coordinator method that sends base64-encoded PIA commands over the BLE TLS tunnel via `ble_client.send_pia_command()`.
- **Concurrent BLE + SignalR sensor streaming** — BLE and SignalR now run simultaneously instead of mutually exclusive. Both feed into the same data dict, giving full sensor coverage with BLE's low-latency updates. Commands still route BLE-first with cloud fallback (no duplicate commands).
- **BLE PIA subscriptions** — After TLS is established, the coordinator sends the same 7 PIA subscription requests + refresh command over BLE that SignalR uses. This should unlock all ~130 sensors via BLE (previously only ~28 were pushed autonomously by the SCU). If subscriptions fail, SignalR still provides full coverage. After the initial setup (OAuth2 login + EHG token exchange require internet), BLE can operate fully offline — sensor streaming and control commands work without any cloud connectivity.

### Changed

- **`_send_with_retry()` now BLE-aware** — The coordinator's central command dispatcher tries BLE first (builds PIA payload locally, sends over BLE TLS), then falls back to SignalR cloud with reconnect + retry. All platform entities (lights, switches, climate, select, button) benefit automatically — no changes needed in platform files.
- **Connection mode `"dual"`** — When both BLE and SignalR are active, `connection_mode` reports `"dual"` instead of `"ble"`. Falls back to `"cloud"` when BLE disconnects.
- **README updated** — BLE path comparison table now shows full command support instead of "read-only".

## [2.60.0-alpha.1] - 2026-05-02

### Added

- **JSON-driven sensor architecture** — All platform entity definitions (sensors, binary sensors, lights, switches, climate, select) are now configured via JSON overlay files in `sensor_maps/`. Cherry-picked from cloud-only repo [hymer-connect-ha](https://github.com/BetaHydri/hymer-connect-ha) v2.41.0–v2.45.0.
- **Per-brand JSON sensor map overlays** — `sensor_maps/base.json` (shared across all EHG brands) + brand-specific overlays (e.g. `hymer.json`, `eriba.json`) loaded at startup via `load_sensor_map(brand)` in `pia_decoder.py`.
- **JSON-driven lights and switches** — Light and switch entity definitions loaded from `"lights"` and `"switches"` sections in JSON overlays. No more hardcoded light/switch lists in Python.
- **Climate/select bus IDs parameterized via JSON** — Heater, boiler, and fridge bus/slot IDs now parameterized per brand via `"climate"` section in JSON overlays, enabling multi-brand support for climate and select entities.
- **Dometic compressor fridge support** — Bus 60 mapping for Eriba and other brands with Dometic fridge (via `eriba.json` overlay).
- **Dynamic entity builders** — `sensor.py`, `binary_sensor.py`, `light.py`, `switch.py` now use dynamic builders that read from `ENTITY_DEFS`, `LIGHT_DEFS`, `SWITCH_DEFS`, `CLIMATE_DEFS` at entity setup time.

### Changed

- **Static `SENSOR_MAP` replaced by runtime JSON loading** — The hardcoded 200+ entry `SENSOR_MAP` dict in `pia_decoder.py` is now empty at import time and populated from `base.json` + brand overlay at startup. All sensor name→bus/slot mappings moved to JSON.
- **Platform files significantly reduced** — `sensor.py` (884→392 lines), `binary_sensor.py` (360→182 lines) — hardcoded entity descriptions replaced by JSON-driven dynamic builders. Only computed sensors (solar power, fuel) and SCU restart button remain hardcoded.

### Unchanged (BLE dual-path preserved)

- `ble_client.py` — Full BLE pairing ceremony, TLS, D-Bus agent (1540 lines)
- `config_flow.py` — 3-step flow: login → QR activation → BLE pairing
- `coordinator.py` — Dual-path: BLE first → SignalR fallback → BLE recovery
- `api.py` — `byToken`, `confirmationToken` API calls
- `button.py` — SCU restart button
- All tools (`tools/ehg-token-app/`, `capture_ehg_token.py`, etc.)
- GitHub Actions workflows

## [2.40.0-alpha.2] - 2026-05-01

### Fixed

- **BLE pairing failed before TLS handshake** — The `client.connected` property requires both BLE GATT connection **and** TLS to be established. The config flow checked `client.connected` before calling `establish_tls()`, so it always returned `False` after successful bonding — causing `ble_pairing_failed` before ever reaching TLS or the pairing ceremony. Added `ble_connected` property that checks only the BLE GATT state, and updated the config flow to use it.

### Verified (hardware testing 2026-05-01)

> **MAJOR BREAKTHROUGH** — The BLE pairing path is now fully operational end-to-end. For the first time, the EHG remote-access refresh token can be obtained **automatically** via BLE — no mitmproxy, no phone interception, no manual token pasting. Just press CONNECTION on the SCU, and Home Assistant does the rest.

- **BLE pairing fully operational** — Tested on a **HYMER Grand Canyon S 600 CrossOver** (2025) with Home Assistant running on a **Raspberry Pi 4** (built-in Bluetooth 5.0). Full BLE ceremony completes successfully: D-Bus JustWorks bonding → TLS 1.1 (AES128-SHA) → PairMobileRequest (1201 bytes, 63 chunks) → PairMobileResponse (status=1) → PairMobileConfirmation → **EHG remote-access refresh token obtained**.
- **EHG token enables full SignalR authentication** — The BLE-obtained refresh token is used to mint a fresh EHG access token (`UpdateTokens SUCCESS`), unlocking the authenticated SignalR datahub. This is the same token that previously required mitmproxy capture from a phone.
- **130 sensors via authenticated SignalR** — With the EHG token, the SignalR subscription returns the full vehicle state: 130 sensors across all buses (Mercedes CAN bus 1, CBE EBL bus 3, Voltronic MPPT bus 8, lights bus 11–27, GPS bus 30, Thetford fridge bus 34/37, Truma Combi D6E bus 58, BOS LUX BMS bus 99). Without the token, only empty PIA responses (`0 fields updated`) were returned.
- **Real-time sensor data via BLE direct path** — 28 sensors streaming live from the SCU at ~1–2 second intervals over the BLE direct path (~50ms latency). Verified sensors: `bms_current` (bus 99), `solar_voltage` (bus 8), `gps_utc_time` (bus 30), `battery_current` (bus 3).
- **Light control verified** — All vehicle lights toggled successfully via SignalR commands authenticated with the BLE-obtained token:
  - Living ceiling light (bus 11): `light_living_ceiling` + `light_wohnen_group` — battery_current jumped from -1.33A to -1.73A on activation
  - LED bar (bus 25): `light_led_bar` + `light_led_bar_2` — battery_current increased to -1.85A, bms_current dropped to 0.64A (solar compensation visible)
  - Nightlight (bus 16): `light_nightlight` + `light_privat_group`
  - All lights confirmed off with state changes and current draw recovery
- **Automatic BLE/cloud failover** — Coordinator establishes BLE direct path on startup, falls back to SignalR cloud when BLE is unavailable, and recovers BLE when back in range.

## [2.40.0-alpha.1] - 2026-04-27

### Added

- **BLE dual-path pairing (experimental)** — Full BLE pairing pipeline: D-Bus JustWorks bonding via raw messages + introspection XML → TLS 1.1 handshake (AES128-SHA) → PairMobileRequest with Write Without Response + 5ms pacing (matching EHG app's Nordic BLE `.split()` behavior). PairMobileResponse pending vehicle test (ATT 0x0e fix: switched from Write With Response to Write Without Response).
- **Config flow Step 3 — BLE Pairing UI** — Progress spinner with 2-minute retry loop (12 attempts, 8s apart). User presses CONNECTION (Verbindung) on SCU while spinner shows. On failure, creates entry in cloud-only mode.
- **BLE enabled checkbox in Step 2** — Users can choose whether to use BLE for ongoing data or only for initial token pairing. Checkbox also visible in Options (Configure).
- **Reconfigure triggers BLE pairing** — Empty submit re-triggers Step 3 pairing. No need to delete and re-add integration.
- **SCU bonding state check** — Polls `fff40004` characteristic (challenge-response) to detect CONNECTION press. Only available after bonding.
- **Sensor Discovery Tool: multi-brand support & JSON export** — The standalone `tools/discover_sensors.py` now accepts a `--brand` parameter (supports `hymer`, `eriba`, `buerstner`, `dethleffs`, `lmc`, `niesmann-bischoff`, `sunlight`, `carado`, `laika`) so non-HYMER vehicle owners can run sensor discovery against their SCU. Results are auto-exported as a JSON file (`sensor_discovery_<brand>.json`) for easy sharing on GitHub issues. Use `--output <path>` to customize the export path. This is a standalone tool only — no changes to the HA integration code.
- **Robust JWT scanning in token extractor** — `tools/capture_ehg_token.py` now uses generic JWT regex scanning (`eyJ...` pattern) across all request/response bodies, HTTP headers, and WebSocket messages instead of relying on specific JSON keys. Fixes token detection for vehicles where the token is not located under the expected `data.token` key or `ehgAccessToken` WebSocket field. Synced from [hymer-connect-ha#53](https://github.com/BetaHydri/hymer-connect-ha/issues/53).

### Fixed

- **D-Bus pairing agent** — `bleak.pair()` has no agent; `bluetoothctl` blocked in HAOS; `dbus-fast` ServiceInterface annotations fail. Solution: pure raw D-Bus messages with `add_message_handler()` + introspection XML.
- **GATT write pacing** — 1253-byte PairMobileRequest (63 chunks at 20 bytes) overwhelmed SCU NUS RX buffer (ATT error 0x0e). Switched to Write Without Response with 5ms pacing for large payloads (>10 chunks), matching EHG app's Nordic BLE `.split()` behavior. Small payloads use Write With Response.
- **Stale bond recovery** — `BleakClient.unpair()` doesn't clear BlueZ bonds. Now uses D-Bus `Adapter1.RemoveDevice()`. Detects corrupt bonds (bonded + disconnect) and clears automatically.
- **Coordinator/config flow race condition** — `_ble_pairing_in_progress` flag prevents concurrent BLE attempts.
- **Options flow BLE defaults** — Checkbox and address now fall back to `config_entry.data` when `options` is empty.
- **BLE address preserved on bonding rejection** — Only cleared on connection-level failures, not bonding rejection.
- **Bonding retry loop** — Config flow retries bonding 12 times over 2 minutes (was single attempt that failed in ~40ms).

### Security

- **Removed log files from repository** — Log files containing VIN, vehicle URN, and BLE MAC address removed. Added `logs/` to `.gitignore`.
- **Anonymized SCU example** — README example output uses placeholder MAC and SCU ID.

## [2.37.0] - 2026-04-25

### Added

- **BLE pairing protocol — `pair_mobile()` in `ble_client.py`** — Implemented the full SCU mobile-device pairing ceremony over BLE/TLS, matching the EHG app's flow: send `PairMobileRequest` (activation token + confirmation token + device name) → wait for user to press ALLOW on SCU touchscreen → receive `PairMobileResponse` with `remote_access_token` and `remote_access_refresh_token` → send `PairMobileConfirmation(success=true)`. This eliminates the need for the mitmproxy token capture workflow when the HA instance (e.g. RPi4) has BLE hardware and is physically near the vehicle. The protobuf field layout was reverse-engineered by Dan Simms (`dan-simms1/hymer-connect-ha`) in the standalone `hymer_token_tool`.

- **Two-step config flow with QR code activation** — The config flow now mirrors the EHG app's setup process: **Step 1** (Login) collects brand, email, password, and optional EHG refresh token. **Step 2** (Vehicle Activation) collects the QR code activation token text from the vehicle sticker and optionally the SCU Bluetooth MAC address. The QR token is resolved via `GET /api/ehg/v1/vehicles/byToken` to obtain the vehicle URN and SCU URN, which are stored in the config entry for use by the coordinator and BLE client.

- **Protobuf encoding/decoding for PairMobileRequest/Response** — Added minimal protobuf wire-format helpers (varint, length-delimited, string, bool fields) with no external dependency. Field numbers match the decompiled EHG app exactly: `BleProtocol(1) → Request(1/2/3/8) → User.PairMobileDevice(4) → activation_token(1), confirmation_token(2), device_name(3), wait_for_confirmation(4)`. Response decoder extracts `remote_access_token(1)`, `remote_access_refresh_token(2)`, and `confirmation_required(3)` from the `Response.mobilePair(9)` field.

- **`PairMobileResponse` dataclass** — New return type for `ScuBleClient.pair_mobile()` containing `remote_access_token`, `remote_access_refresh_token`, `confirmation_required`, `request_id`, `status`, `timestamp`.

- **`CONF_QR_TOKEN` config key** — New constant for the QR code activation token text input in the config flow.

### Changed

- **Config flow is now two steps** — Previously a single-step login form that created the config entry immediately. Now advances to a vehicle activation step after successful login. The config entry now includes `vehicle_urn`, `scu_urn`, `ble_scu_address`, and `ble_enabled` in the entry data (in addition to the existing auth tokens).

- **Coordinator reads BLE settings from both `data` and `options`** — The `ble_enabled` and `ble_address` properties now check `options` first (user-configurable post-setup), falling back to `data` (set during config flow). This means BLE settings from the config flow are immediately effective without requiring a separate options flow visit.

- **BLE has priority over cloud (SignalR)** — The coordinator's `_async_update_data()` now tries BLE first on every 60-second poll. If BLE connects, SignalR is stopped to avoid duplicate data. If BLE disconnects, the listen loop immediately falls back to cloud SignalR. On the next poll, BLE is retried — if it recovers, SignalR is stopped again. Full failover cycle: BLE → Cloud → BLE.

- **Vehicle activation step is optional** — Cloud-only users can skip Step 2 by leaving both QR code and BLE address empty. The integration falls back to auto-discovering the vehicle at runtime. QR code is required when a BLE address is provided (validation error otherwise).

- **Reconfigure flow** — Added `async_step_reconfigure()` for adding QR code, BLE address, or EHG refresh token to an existing config entry post-setup. Accessible via Settings → Integrations → HYMER Connect → ⋮ → Reconfigure.

- **Auto-pairing in coordinator** — `start_ble()` automatically triggers `pair_mobile()` when no EHG refresh token exists and a QR activation token is in the config data. Gets `confirmationToken` from cloud API, sends `PairMobileRequest` over BLE/TLS, waits for user to press ALLOW, stores the returned refresh token in the config entry.

- **Pairing button instruction** — Config flow, coordinator logs (WARNING level), and BLE timeout errors all instruct the user to press the PAIRING button on the SCU control panel before the first connection, matching the EHG app's UX flow.

- **DEBUG-level BLE logging** — Added debug logging for BLE scan (device count, candidates), GATT connect, UART notifications (byte count), PairMobileRequest frame size, and encrypted payload size. Enable with `logger: logs: custom_components.hymer_connect: debug` for troubleshooting.

- **Manifest updated** — Added `bleak>=0.21.0` to requirements, `bluetooth` to dependencies, bumped version to 2.37.0, documentation and issue tracker URLs point to `hymer-connect-ha-ble`.

### Fixed (hardware testing 2026-04-26)

- **TLS 1.0/1.1 on Python 3.14 / OpenSSL 3.x** — The SCU firmware only speaks TLS 1.0/1.1 with `AES128-SHA`/`AES256-SHA`, but modern HAOS (Python 3.14 + OpenSSL 3.x) disables these legacy protocols by default. Fixed by setting `@SECLEVEL=0` in the cipher string and clearing `OP_NO_TLSv1` / `OP_NO_TLSv1_1` flags. Without this, the TLS handshake fails with `[SSL: NO_PROTOCOLS_AVAILABLE]`.

- **HA BleakClient wrapper compatibility** — Home Assistant wraps `bleak.BleakClient` with `HaBleakClientWrapper`, which does not expose the `get_services()` method. Fixed to use the `.services` property with fallback.

- **BLE bonding before TLS** — The SCU requires OS-level BLE bonding (`client.pair()`) before it will respond to TLS handshakes. Without bonding, the TLS ClientHello is sent but the SCU never replies (20-second timeout). Bonding requires the user to press the CONNECTION button on the SCU control panel first.

- **BLE disconnect on failure** — Previously failed BLE attempts left the GATT connection open, causing `Notify acquired` and `already connected` errors on retry. Now properly calls `disconnect()` before clearing the client reference.

- **Auto-scan BLE enabled** — Providing a QR token in config flow Step 2 now always sets `ble_enabled = True`, even without a MAC address. Previously `bool("")` was `False`, preventing auto-scan from working.

- **BlueZ stale notify acquisition leak** — When `start_notify()` failed (e.g. `[org.bluez.Error.NotPermitted] Notify acquired`), `self._client` was never assigned because it was set *after* `start_notify()`. The coordinator's `disconnect()` found `None` and skipped cleanup, leaking the raw `BleakClient`. BlueZ retained the stale D-Bus notify acquisition, causing every subsequent BLE connect attempt to fail identically — a permanent poison loop. Fixed by wrapping all post-GATT-connect setup in `try/except` that guarantees `client.disconnect()` on any failure. On `Notify acquired`: fully disconnect, wait 1s for BlueZ to settle, then reconnect with a fresh GATT session (one retry, no infinite recursion).

- **BLE bonding requires D-Bus pairing agent** — `bleak.pair()` calls `Device1.Pair()` on D-Bus but does NOT register a pairing agent. BlueZ waits ~8s for an agent response that never comes, then cancels with `AuthenticationCanceled` — even when CONNECTION is pressed on the SCU. Tried `bluetoothctl` subprocess but HAOS already has HA's agent registered (`Failed to register agent object`). Fixed by using `dbus-fast` (shipped with HA Core) to register a temporary `NoInputNoOutput` agent directly on D-Bus, call `Device1.Pair()`, auto-confirm JustWorks requests, then unregister.

- **BLE `unpair()` on connected client kills GATT** — `BleakClient.unpair()` removes the device from BlueZ entirely (`RemoveDevice`), terminating the active GATT session. Then `pair()` fails instantly because there's no connection. Fixed by calling `unpair()` via a temporary `BleakClient` *before* the main `connect()`.

- **Coordinator/config flow BLE race condition** — The coordinator's 60s poll was starting a concurrent BLE `connect()`/`pair()` while the config flow's Step 3 pairing task was mid-bonding. The concurrent `unpair()` killed the active bonding negotiation, causing `InProgress` → `AuthenticationCanceled`. Fixed by adding `_ble_pairing_in_progress` flag — coordinator skips BLE while config flow pairing is active.

- **BLE address cleared on bonding rejection** — The stored BLE address was cleared on every failure, forcing a re-scan even when the address was valid (bonding just needed CONNECTION). Now only clears the address on connection-level failures (timeout, device not found), not on bonding rejection.

### Added

- **Step 3 BLE Pairing UI** — After Step 2 (Vehicle QR + BLE MAC), the config flow shows a progress spinner: *"Waiting for SCU to accept BLE pairing..."*. Background task runs the full BLE ceremony (scan → GATT connect → bonding → TLS → PairMobileRequest). On success, EHG token is stored. On failure, entry is created in cloud-only mode.

- **Reconfigure triggers BLE pairing** — The Reconfigure flow (⋮ → Reconfigure) now routes to the Step 3 BLE pairing spinner when QR token + BLE is enabled and no EHG token was manually provided. Allows retrying BLE pairing without deleting and re-adding the integration.

- **Exponential backoff for BLE failures** — First 5 failures retry at normal 60s poll interval (catches the SCU pairing window). After 5 failures, escalates to 5min/10min/max 15min.

- **D-Bus pairing agent** — `bleak.pair()` has no D-Bus agent; `bluetoothctl` blocked in HAOS; `dbus-fast` ServiceInterface annotations fail at import. Solution: pure raw D-Bus messages with `add_message_handler()` + introspection XML that tells BlueZ the agent supports `RequestConfirmation`, `AuthorizeService`, etc. Auto-accepts all JustWorks callbacks.

- **GATT write pacing** — Large BLE payloads (PairMobileRequest = 1253 bytes = 63 chunks at 20 bytes) overwhelm the SCU's NUS RX buffer, causing `ATT error 0x0e`. Added 10ms inter-chunk delay for writes >10 chunks.

- **Stale/corrupt bond recovery** — After ATT error 0x0e, bond keys become corrupt. SCU rejects GATT with `device disconnected`. Now detects bonded + disconnect → clears stale bond → retries with fresh bonding.

- **BlueZ bond status check** — `_connect_inner()` now checks the `Paired` D-Bus property before calling `unpair()`. Preserves valid bonds, only clears stale/failed ones.

### Credits

- **Dan Simms** (`dan-simms1/hymer-connect-ha`) — The PairMobileRequest/Response protobuf field layout, the BLE pairing ceremony (activation token + confirmation token + SCU touchscreen ALLOW + refresh token minting), and the `hymer_token_tool` RUNBOOK documenting the full 4-step pairing sequence were invaluable for implementing the BLE pairing path in this integration.

## [2.36.6] - 2026-04-25

### Fixed

- **Fridge door and heater window contact never updated after initial state** — The PIA protobuf decoder's depth filter (`depth <= 3`) silently dropped real-time SCU push updates for sensors like `fridge_status` (37,2) and `heater_window_switch_closed` (58,14). The initial subscription response nests sensors at depth 2–3 (so the initial "Closed" state was received), but real-time state-change pushes from the SCU arrive at depth 4 and were discarded. Relaxed the filter to accept known `SENSOR_MAP` entries at depth 4 while keeping the phantom-value protection for unknown entries at depth ≥ 4. `binary_sensor.hymer_fridge_door` and `binary_sensor.hymer_heater_window_contact` now update in real time when the physical door/window is opened or closed.

### Added

- **INFO-level logging for fridge door and window contact state changes** — State transitions for `fridge_status` and `heater_window_switch_closed` are now logged at INFO level (e.g. `State change (37,2) fridge_status: 'Closed' → 'Open' (depth=4)`) so changes are visible in the HA log without enabling DEBUG.

## [2.36.5] - 2026-04-25

### Changed

- **Truma Combi diagnostics now enabled by default** — The three bus-58 diagnostic binary sensors added in v2.35.0 (`binary_sensor.hymer_heater_combi_error`, `binary_sensor.hymer_heater_response_error`, `binary_sensor.hymer_heater_shoreline_connected`) are now enabled by default instead of disabled. Field use confirmed they catch genuine transient SCU/Truma faults (e.g. a 21-second `Combi Error` window observed on 2026-04-24 23:24:42 → 23:26:21) that would otherwise be invisible without per-entity opt-in. The window safety contact (`binary_sensor.hymer_heater_window_contact`) was already enabled by default. Existing installations that explicitly disabled these entities keep their preference; only fresh installs and never-seen entities are affected.
- **Heater Status dashboard card extended** — The default Truma dashboard now surfaces `Combi Error`, `Response Error`, and `Shoreline (230 V)` rows alongside the existing window-contact row, giving a complete at-a-glance Truma health view.
- **Bus 58 documentation rewritten** — `docs/sensor-map.md` now lists every bus-58 slot with both its local sensor key and the EHG canonical name in parentheses. Slots 10/12/13/14 (renamed in v2.35.0) are no longer shown under the obsolete `heater_sensor_*` placeholders. Slot 58:5 is explicitly flagged as a legacy misnomer (the local key `heater_fan_speed` actually reads `water_heater_mode`, the boiler — kept for backwards compat with existing dashboards/history).
- **`sensor.hymer_heater_fan_speed` disabled by default** — This legacy sensor reads slot 58:5 (`water_heater_mode`), which is the boiler mode and is already exposed (and writable) via `select.hymer_boiler_mode_ctrl`. The duplicate sensor is now disabled by default for new installs to avoid confusion. Existing installs keep their current state (enable/disable) — manually disable it in the entity registry if you want it gone.

## [2.36.4] - 2026-04-24

### Fixed

- **`binary_sensor.hymer_dinette_window_diesel_safety` showed `Geöffnet` while the window was actually closed** — Slot 58:14 was added in v2.35.0 with `on_value=False` based on the misleading EHG metadata name `window_switch_closed`. Captured traces (six `ws_capture_*.jsonl` files) prove the opposite: the slot's resting state is `false` while the window is closed, and it flips to `true` when the dinette window is opened (one capture at 2026-04-19 15:23:58 shows the live `false → true → false` transition). So the raw value already matches HA's WINDOW device-class semantics (`True = open`). Removed the `on_value=False` inversion.

## [2.36.3] - 2026-04-24

### Removed

- **`select.hymer_heater_mode`** — The Heater Mode select (Off/Normal/Automatic) backed by slot 58:11 (`heater_air_mode`) was removed. Investigation against captured SignalR traffic (six `ws_capture_*.jsonl` sessions across three days) and the decoded mitm `.flow` files showed:
  1. The official EHG app exposes **no** heater-mode control on the Klima tab — only heating on/off + setpoint, electric aux wattage, energy source, boiler on/off, and Turbo mode.
  2. Slot 58:11 was **never** written to by the EHG app in any captured trace — the only bus-58 writes observed were 58:5 (`water_heater_mode`) paired with 58:4 (`heater_fuel_type`).
  3. Slot 58:11 was always read as `"Normal"` in every capture; v2.36.1's pairing-with-fuel-slot fix did not change SCU behavior.
  This matches the situation already documented for the heater fan slot in v2.36.0: the EHG metadata `rw` flag is a per-firmware capability hint, not a guarantee, and the Truma firmware silently reverts unsupported writes. The reading is still available via `sensor.hymer_heater_operating_mode`.
- Heater Mode tile removed from the dashboard.
- Translation entries for `heater_air_mode_ctrl` removed.

## [2.36.2] - 2026-04-24

### Fixed

- **Dashboard "Fan Speed" mirrored Boiler Mode** — The Heater Status card showed a `Fan Speed` row backed by `sensor.hymer_heater_fan_speed`, but that sensor reads slot 58:5 which is `water_heater_mode` (the boiler). So toggling the boiler to ECO made the row read `Eco`, looking like the heater fan was responding when it wasn't. Removed the misleading row from the dashboard. The underlying sensor entity is left in place for backwards compatibility but is no longer surfaced on the default dashboard. The real Truma fan power (Eco/High) is not exposed on the SCU bus and remains panel-only.

## [2.36.1] - 2026-04-24

### Fixed

- **Heater mode reverted to `Normal` after selecting `Automatic`** — Standalone `set_value` writes to slot 58:11 were silently rolled back by the SCU. Switched the Heater Mode select to a multi-sensor command paired with the fuel slot (58:4), matching the pattern every other writable 58:* slot uses (setpoint, boiler mode, energy source). Captured EHG traffic always pairs writes on bus 58 this way.

## [2.36.0] - 2026-04-24

### Fixed

- **Climate fan_mode was actually controlling the boiler** — The Truma climate entity exposed an `Eco`/`High` fan mode that wrote to slot 58:5, but per EHG metadata that slot is `water_heater_mode` (the boiler), not a heater fan-speed slot. Selecting `High` on the climate card was silently turning the **boiler** to `HOT` while doing nothing to the heater fan. Removed `FAN_MODE` from the climate entity's supported features and the `Vent` HVAC mode (the SCU bus has no writable fan-speed slot — the 1–10 numeric vent steps and the panel `VENT` mode are physical-panel only).

### Added

- **Heater mode select** (`select.hymer_heater_mode`) writing to slot 58:11 (`heater_air_mode` per EHG metadata). Options: `Off` / `Normal` / `Automatic`. This is the actual heater on/off/auto mode toggle on the SCU bus.
- Heater Mode tile on the Heating dashboard, next to Boiler Mode.
- Translation entries for `heater_air_mode_ctrl` and the previously-missing `heater_energy_ctrl`.

## [2.35.2] - 2026-04-24

### Fixed

- **Truma fan mode `High` was silently ignored** — The climate handler was writing the literal string `"High"` to bus 58 slot 5, but the SCU only accepts the EHG-canonical values `OFF` / `ECO` / `HOT` (per the EHG app metadata). The write was accepted on the wire but the panel kept showing `Eco`. Now writes `"HOT"` for the `High` HA fan_mode option, matching what the OEM app sends.

## [2.35.1] - 2026-04-24

### Fixed

- **Truma fan mode `NameError`** — `climate.async_set_fan_mode` referenced an undefined `fuel` variable, causing every Eco/High/Vent change to fail with `NameError: name 'fuel' is not defined`. Now resolves the current air energy source via `_get_fuel_type()` before sending the multi-sensor command, matching the pattern used by `async_set_hvac_mode` and `async_set_temperature`.

## [2.35.0] - 2026-04-24

### Added

- **Truma Combi window safety contact** — New `binary_sensor.hymer_heater_window_contact` exposes the window switch on the dinette window where the Truma diesel exhaust is routed. When the window is open the SCU automatically blocks the diesel heater (safety interlock); the entity uses HA's `WINDOW` device class so dashboards/automations can react instantly. Source: bus 58 sid 14 (`window_switch_closed`) per EHG app metadata.
- **Truma Combi diagnostics** — Three additional diagnostic binary sensors (disabled by default): `heater_combi_error`, `heater_response_error`, `heater_shoreline_connected`. Sources: bus 58 sids 10/12/13 per EHG app metadata.
- **Bus 58 slot annotations** — All 11 mapped slots on bus 58 now carry inline comments naming their canonical EHG meaning (`TrumaCombi_DE` component) so future contributors do not have to re-derive them.

### Changed

- **Renamed 4 placeholder slots on bus 58** (no entity bindings, safe rename): `heater_sensor_10` -> `heater_combi_error`, `heater_sensor_12` -> `heater_response_error`, `heater_sensor_13` -> `heater_shoreline_connected`, `heater_sensor_14` -> `heater_window_switch_closed`. The previously confusing `heater_sensor_14` users may have seen via v2.34.0 dynamic discovery is now a properly named binary sensor. Existing entities (`heater_setpoint`, `heater_state`, `heater_fuel_type`, `heater_fan_speed`, `heater_electric_power`, `heater_operating_mode`) are unchanged to preserve dashboards and history, but their canonical EHG names are documented in code comments for clarity.

## [2.34.0] - 2026-04-24

### Added

- **Dynamic slot discovery** — Any PIA `(bus_id, sensor_id)` pair reported by the SCU that is not present in `SENSOR_MAP` now automatically appears as a generic diagnostic sensor named `Discovered bus N slot M` (entity id `sensor.hymer_discovered_bus{N}_slot_{M}`). Entities are **disabled by default** so they do not pollute the UI — enable them via the entity registry to inspect the raw value reported by an unknown slot. This brings the discovery capability of `tools/discover_sensors.py` directly into Home Assistant, making it easier to identify what unmapped slots actually report when you trigger physical actions on the vehicle. Existing 129 named entities are unaffected (the decoder only emits fallback `bus{N}_s{M}` keys for slots NOT in `SENSOR_MAP`).

## [2.33.1] - 2026-04-24

### Fixed

- **12V main switch ON flicker** — The verify timer now waits 30s (was 15s) for the main switch, matching the existing OFF holdoff. The SCU reboots on any 12V state change and pushes a stale "Off" readback during reconnect. The old 15s verify fired too early, falsely declared the SignalR send channel dead, forced a reconnect, and left the dashboard stuck on "Aus" even though the vehicle's 12V was actually ON. The fix suppresses this flicker by holding the optimistic state through the SCU reboot window.
- **Case-insensitive readback comparison** — The switch verify check now uses case-insensitive string matching, consistent with the binary sensor fix in v2.32.0.

## [2.33.0] - 2026-04-24

### Added

- **SCU Restart button** — New `button.hymer_restart_scu` entity sends a cold reboot command to the Smart Control Unit. Useful when the SCU is stuck or not responding to commands. Located in the System tab with a confirmation prompt ("Are you sure?"). The integration auto-reconnects after reboot (~30-60s). Credit: Dan Simms decoded the `Request.command.restart` PIA protocol path.

### Fixed

- **Shutdown-safe SignalR** — The coordinator now marks itself as shutting down before tearing down the SignalR connection. Reconnect attempts during HA shutdown/unload are suppressed, eliminating the `Session is closed` log noise that appeared on every HA restart.

## [2.32.0] - 2026-04-24

### Added

- **Fridge door binary sensor** — New `binary_sensor.hymer_fridge_door` with `BinarySensorDeviceClass.DOOR`. Reads `fridge_status` (bus 37, sid 2) which the SCU reports as int 0/1, mapped to "Open"/"Closed" by the PIA decoder. Previously only exposed as a text sensor that stayed stuck on "Closed". Dashboard updated to use the new binary sensor.

### Fixed

- **Case-insensitive `is_on` for string-based binary sensors** — The `is_on` comparison now uses case-insensitive string matching for all string-valued binary sensors (doors, lock, main switch, chassis flags). Previously, if the SCU sent `"ON"` instead of `"On"`, the sensor would silently show the wrong state. Credit: Dan Simms' metadata-driven implementation (`dan-simms1/hymer-connect-ha`) uses device-class-aware string matching sets (`_DOOR_TRUE_VALUES`, `_CONNECTIVITY_TRUE_VALUES`, etc.) which highlighted this fragility in our per-entity `on_value` approach.
- **Vehicle Bus Architecture documentation** — Added comprehensive README section covering CAN/LIN bus topology, dual-path BLE/LTE control architecture, Mermaid diagram, and bus summary table.

## [2.31.0] - 2026-04-23

### Fixed

- **Dashboard engine entity** — Corrected entity ID from `binary_sensor.hymer_engine_running` to `binary_sensor.hymer_engine` (HA-generated ID). Dashboard and template helper now work correctly.
- **Fridge door sensor** — Confirmed (37,2) IS a door sensor (EHG app shows open/closed). HA entity doesn’t update — suspected decoder depth filter issue, needs mitmproxy capture.
- **Documentation** — Updated README (LED bar, native groups, ~130 entities, door sensors corrected), sensor-map (bus 22 = LED bar, bus 121 Victron, door verification notes).

## [2.30.2] - 2026-04-23

### Fixed

- **Removed orphan rear door entity** — `binary_sensor.hymer_rear_door` referenced a dead path (`signalr_sensors.door_rear`) after slot (1,14) was renamed to `motor_oil_warning`. Confirmed at vehicle: (1,14) = SNA (not connected on S600). Rear doors only available via Mercedes ME API.

## [2.30.1] - 2026-04-23

### Fixed

- **Bus 22 is LED bar, not fresh water** — Confirmed at vehicle: both water tanks were empty but bus 22 showed 88%, matching LED bar brightness on bus 25. Bus 22 is a duplicate LED bar SCU component. Sensor renamed and disabled by default. Dashboard water sensors remain on EBL (bus 3).

### Confirmed at vehicle (2026-04-23)

- **Door sensors**: Only driver (1,12) and passenger (1,13) doors have PIA sensors. Sliding side door and rear barn doors are CAN-bus only (Mercedes ME API).
- **Motor oil warning (1,14)**: Shows "SNA" (Sensor Not Available) — not connected on S600.
- **No Victron bus 121**: Not detected with current Victron switch state. Entities remain disabled.
- **No Truma ventilation**: EHG app only supports heating mode, not fan-only ventilation (issue #38 — won’t fix).

## [2.30.0] - 2026-04-23

### Added

- **Victron MultiPlus 12/1600/70 support** — Bus 121 sensor mapping (19 slots) from EHG app metadata extraction. Includes inverter state/voltage/current/frequency, charger state/voltage/current, shore power input, device failure status, and firmware version. All entities disabled by default — enable in Settings > Entities when Victron physical switch is ON.
- **Victron binary sensors** — `victron_inverter_on` and `victron_charger_on` for inverter/charger power state monitoring.

## [2.29.0] - 2026-04-23

### Added

- **Fuel level in liters** — Computed sensor `fuel_level_liters` converts fuel level percentage to absolute liters using the configured tank capacity.
- **Fuel consumption (L/100km)** — Computed sensor `fuel_consumption` tracks diesel usage from odometer + fuel level deltas. Resets on refueling (>5% fuel increase). Requires minimum 5 km driven.
- **Estimated range** — Computed sensor `fuel_range_estimated` calculates remaining driving range in km from current fuel and consumption rate.
- **Configurable diesel tank capacity** — Options flow allows users to set their diesel tank size (30–200 L, default 93 L for Sprinter 419/519 CDI). Go to Settings > Integrations > HYMER Connect > Configure.
- **Dashboard fuel section** — Diesel gauges and fuel entities card added to the Vehicle tab.

## [2.28.0] - 2026-04-22

### Added

- **EBL402 water tank sensors** — Bus 3 slots (3,8) and (3,9) renamed from `light_1_level`/`light_2_level` to `fresh_water_level_ebl`/`grey_water_level_ebl`. These are the EBL402's built-in tank level inputs (per Dan's S700 PR #44). Direct percentages, no invert.

### Removed

- **Grey water sensor from bus 25** — Bus 25 is the LED bar (confirmed v2.27.0), not grey water. Old `gray_water_level` sensor entity removed.

### Changed

- **Dashboard water gauges** — Updated to use new EBL402 water sensors instead of the old bus 22/25 mappings.

## [2.27.0] - 2026-04-22

### Added

- **Outside LED bar light entity (Bus 25)** — Confirmed via mitmproxy capture: the EHG app sends on/off + brightness commands to bus 25 when toggling the LED bar. Issue #46 resolved!

### Fixed

- **Bus 25 was grey water, not LED bar** — Mitmproxy capture proved bus 25 is the outside LED bar. Grey water sensor removed from bus 25.
- **Fresh water invert100 transform removed** — Bus 22 raw values are direct percentages (empty tanks show ~15 raw ≈ <10% in EHG app). The invert100 transform was producing incorrect 85% readings for empty tanks.

## [2.26.0] - 2026-04-22

### Changed

- **Native SCU light groups replace HA groups** — Bus 24 (All Wohnen) and Bus 27 (All Privat) are hardware group controls built into the SCU. One command toggles all lights in each group at the hardware level — faster and more reliable than HA light groups.

### Fixed

- **Bus 24 was not an outside light** — Corrected from individual outside light to All Wohnen group control (verified: toggling activates all living area lights)
- **Bus 27 was not the LED bar** — Corrected from LED bar to All Privat group control (verified: toggling activates all bedroom/bath lights)
- **Outside LED bar bus ID still unknown** — Issue #46 remains open. The LED bar is not in the 129 discovered sensor buses.

## [2.25.0] - 2026-04-22

### Added

- **Outside LED bar light entity** — Discovered Bus 27 as the outside LED bar via the new `discover_sensors.py` tool. Adds a controllable light entity with on/off and brightness (bus 27, sensor IDs 1-3). EHG app supports on/off + brightness for this light.
- **Sensor discovery tool** (`tools/discover_sensors.py`) — Standalone script that connects to the SCU via the cloud, subscribes to all PIA data, and outputs a complete (bus_id, sensor_id) mapping table with mapped/unmapped status. Discovered 129 sensors, 126 mapped, 3 unmapped.
- **EHG token capture scripts** (`tools/capture_ehg_token.py`, `tools/Start-EhgTokenCapture.ps1`) — Simplified one-click proxy for capturing the EHG refresh token. Windows launcher auto-installs prerequisites.

### Documentation

- Updated sensor map (`docs/sensor-map.md`) with Bus 27 LED bar mapping and full discovery scan results
- Updated README with simplified token capture guide, cross-platform instructions, and prerequisites table

## [2.24.0] - 2026-04-22

### Fixed

- **Duplicate WebSocket connections cause ghost connection state** — When Azure SignalR closes the connection (token expiry, server recycling), the connection-lost callback and the coordinator poll could race to reconnect simultaneously, creating two parallel WebSocket connections with double the traffic. Azure would then throttle or drop one, leaving a "ghost" connection that appears alive (pings succeed, data flows) but silently drops all commands. Added an `asyncio.Lock` to serialize reconnection attempts so only one connection is ever created.

## [2.23.2] - 2026-04-22

### Changed

- **Reduce HA error log noise from expected SignalR reconnections** — Downgraded routine WebSocket close and listen-loop-ended messages from WARNING to INFO level. Only actual WebSocket errors remain at WARNING. Azure SignalR periodically closes connections (token expiry, server-side recycling); the client reconnects automatically and these events are not user-actionable.

## [2.23.1] - 2026-04-21

### Fixed

- **SCU data goes stale after 3 minutes** — The SCU stops pushing sensor data without periodic prodding. Restored a lightweight refresh command (1 message) every 60s poll to keep data flowing, while keeping the full 7-subscription resubscribe at every 10 min. Traffic: ~108 messages/hour (vs 480 pre-v2.23.0, vs 48 in v2.23.0 which was too low).

## [2.23.0] - 2026-04-21

### Fixed

- **SignalR connection drops due to excessive traffic** — Reduced PIA resubscribe frequency from every 60 seconds to every 10 minutes; the SCU pushes state changes automatically, resubscribe only refreshes slow-changing values (battery SOC, solar current). This cuts outbound traffic from ~480 to ~48 messages/hour, preventing server-side disconnects.
- **SignalR reconnection stuck in exponential backoff** — After 5 consecutive connection failures, forces an OAuth2 token refresh and resets backoff instead of silently retrying every 15 minutes.
- **Race condition in connection-lost handler** — Replaced `call_soon_threadsafe` with direct `async_create_task` since the listen loop already runs on the HA event loop; prevents silently swallowed reconnect errors.
- **Potential infinite 401 retry loop** — Added recursion guard to API `_request()` to prevent endless token refresh cycles when the refresh token itself is expired.

### Changed

- **Reconnect backoff logging** — Upgraded from debug to warning level so reconnect-skipped events are visible in HA logs with attempt count.

## [2.22.0] - 2026-04-21

### Added

- **12V main switch availability guard** — All light entities and the water pump switch become unavailable in HA when the 12V main switch is off, preventing interaction with components that won't respond without habitation power. The main switch itself, fridge, boiler, and heater remain controllable regardless of 12V state.

### Documentation

- **Energy Dashboard setup guide** — Step-by-step instructions for creating a Solar Energy (kWh) sensor from `solar_power` (W) using HA's Riemann Sum helper, plus guidance on which sensors are compatible with the Energy dashboard and their required attributes

## [2.21.1] - 2026-04-21

### Fixed

- **`bt_connected` → `scu_flag_5`** — slot 30/12 is not BT connected (phones were remote while value was `True`); reverted to unknown flag pending identification

## [2.21.0] - 2026-04-21

### Changed

- **SCU diagnostic sensors renamed** — bus 30 slots 8-14 renamed from generic `gps_sensor_N` to descriptive names based on observed S600 values and S700 mapping (unconfirmed best-guess, pending vehicle validation):
  - `(30, 8)` → `scu_flag_1` — unknown flag (observed: `False`)
  - `(30, 9)` → `lte_connected` — likely LTE connection state (observed: `True`)
  - `(30, 10)` → `scu_flag_2` — unknown flag (observed: `False`)
  - `(30, 11)` → `paired_bt_devices` — likely paired BT device count (observed: `3`)
  - `(30, 12)` → `bt_connected` — likely BT device connected (observed: `True`)
  - `(30, 13)` → `scu_flag_3` — unknown flag (observed: `False`)
  - `(30, 14)` → `scu_flag_4` — unknown flag (observed: `False`)

## [2.20.0] - 2026-04-21

### Added

- **GPS UTC time sensor** — `sensor.hymer_gps_utc_time` (bus 30, slot 2) exposes SCU internal time
- **SCU diagnostic sensors (bus 30, slots 8-14)** — 7 new sensors (`SCU slot 30/8` through `30/14`) disabled by default; enable to discover potential LTE/SCU telemetry data

## [2.19.3] - 2026-04-21

### Fixed

- **Remote-access commands stop working after ~30 minutes** — Periodic `UpdateTokens` refresh every 15 min
- **Components not controllable after 12V ON** — Auto re-auth on SCU reconnect (`scu_connected` false→true)

## [2.18.0] - 2026-04-20

### Added

- **Shore power binary sensor** — `binary_sensor.hymer_shoreline_connected` (bus 3, sid 22, EBL 402)

### Changed

- **`switch_22` → `shoreline_connected`** — renamed in PIA decoder

## [2.17.0] - 2026-04-20

### Fixed

- **SignalR auto-recovery after 12V toggle** — optimistic `main_switch` update prevents standby bypass from blocking reconnect; 30-min safety cap added (fixes #46)

## [2.16.0] - 2026-04-20

### Changed

- **(1,11) and (1,14) remapped** from doors to vehicle warnings per S700 PR #44
- **Dashboard: Vehicle Warnings section** added, Doors cleaned up

## [2.15.2] - 2026-04-20

### Added

- **Discovery logging for unmapped PIA sensors** — enable via logger config at `info` level

### Fixed

- **Dashboard cleanup** — removed stale entity references, BMS Time Remaining

## [2.15.1] - 2026-04-20

### Fixed

- **S600 door mapping corrected (take 2)** — v2.15.0 had the swap reversed. Correct mapping:
  - (1,12) = `door_driver`, (1,13) = `door_passenger`
  - (1,11) `door_sliding` and (1,14) `door_rear` don't update on S600

## [2.15.0] - 2026-04-20

### Fixed

- **SignalR auto-reconnect on connection loss** — when the WebSocket listen loop ends unexpectedly, the coordinator now triggers an immediate reconnect (resets backoff, schedules refresh) instead of waiting up to 15 minutes for the next poll + exponential backoff cycle. Fixes the issue where the integration became unresponsive after a connection drop and required manual reload.
- **Light/switch/climate/select commands auto-reconnect** — all controllable entities now attempt to reconnect SignalR before sending a command. If reconnection fails, a `HomeAssistantError` is raised with a user-visible toast message instead of silently failing.
- **S600 door sensor mapping corrected** — confirmed at vehicle (2026-04-20):
  - (1,11) `door_driver` → `door_passenger` — physically tested
  - (1,12) `door_passenger` → `door_sliding` — physically tested
  - (1,13)/(1,14) do not update on S600 (no rear door sensors via SCU)

### Added

- **Truma heater energy source: 5 modes** matching physical Truma Combi panel:
  - Diesel (FUEL), Mix 900W (MIX 1), Mix 1800W (MIX 2), Electric 900W (EL 1), Electric 1800W (EL 2)
- **VENT fan mode read-only display** — when VENT is set on the physical Truma panel, HA shows `fan_mode: Vent` and `hvac_action: Fan`
- **`on_connection_lost` callback** in `HymerSignalRClient`

### Changed

- **Heater energy select labels** — renamed to match Truma panel display
- **`heater_fan_speed` value labels** — added VENT mapping

## [2.14.0] - 2026-04-20

### Fixed

- **Stale SignalR send channel auto-detection** — after sending a switch command, verify SCU readback after 15s. If the readback doesn't match the commanded state, the connection is marked as dead and the coordinator reconnects automatically on the next poll. Fixes recurring issue where commands appeared to send but SCU ignored them.
- **SignalR send error handling** — `send_pia_request` now catches send exceptions and marks the connection as dead instead of silently failing.
- **Dashboard: removed stale `current_gear` entity** from Vehicle tab (remapped to `bms_state_of_health` in v2.12.0).

## [2.13.0] - 2026-04-20

### Changed

- **Dashboard: BMS section moved to Power tab** — removed duplicate from Vehicle tab
- **Dashboard: current sensor labels clarified** — "Load Draw" (EBL), "Net Battery Current" (BMS)
- **Diagnostic sensors for EBL slots (3,8) and (3,9)** — temporary sensors to verify if these are water levels (compare with bus 22/25)

### Documentation

- **Power Flow diagram** added to sensor-map.md explaining Solar → BMS → EBL current relationship
- **Bus 3 annotations corrected** — (3,8)/(3,9) flagged as unverified "light levels", likely water levels
- **Bus 8 labels corrected** — all 7 slots are Voltronic MPPT solar data, not water/vents/tire

## [2.12.0] - 2026-04-20

### Fixed

- **Bus 99 remapped to BOS LUX BMS** — bms_voltage, bms_current, bms_temperature, bms_time_remaining, bms_state_of_health, bms_capacity_remaining (fixes #14)

## [2.11.0] - 2026-04-20

### Fixed

- **Bus 1 slots 2, 5, 9 remapped** — speed→fuel_level, rpm→distance_to_service, coolant_temp→outside_temperature (fixes #16)

## [2.10.1] - 2026-04-20

### Fixed

- **SignalR stays alive during 12V standby** — Dead-connection detector skips recycling when main_switch="Off" (fixes #45)
- **12V switch confirmation dialog** — Bidirectional toggle confirmation
- **Dashboard Standheizung entity IDs** — Fixed to match HA translation-based IDs

## [2.10.0] - 2026-04-20

### Fixed

- **Bus 1 slots 17-22 remapped** — Were vehicle lights, actually chassis state flags (parking brake, aux heater, cruise control, etc.). Confirmed by (1,18)="ON" while parked = parking brake

### Removed

- **Vehicle light binary sensors** — headlamp, high_beam, parking_light, fog_front, fog_rear, turn_signal removed (mislabelled)

### Added

- **Chassis state sensors** — parking_brake, standheizung_available/state, cruise_control_can, downhill_assist, coolant_warning

## [2.9.9] - 2026-04-20

### Fixed

- **12V switch OFF holds state through SCU reconnection** — 30s holdoff prevents stale "On" readback from overwriting commanded OFF (fixes #40 for 12V switch)

## [2.9.8] - 2026-04-19

### Changed

- **Dashboard redesigned with clear visual hierarchy** — Section headers, controls, and status tiles are now visually distinct

## [2.9.7] - 2026-04-19

### Fixed

- **Fridge status shows door state** — Labels changed to Open/Closed matching EHG app
- **Energy Source dashboard tile** — Corrected entity ID to `select.hymer`
- **Fresh water level inverted** — 100% when empty fixed (fixes #43)
- **Grey water level inverted** — Same inversion fix (fixes #41)

## [2.9.0] - 2026-04-19

### Fixed

- **12V main switch now works** — Switch sends `str_value="On"/"Off"` instead of `bool_value` (fixes #39)

### Added

- **Heater energy source select** — Diesel / Both 900W / Both 1800W / Electric (fixes #42)
- **String value support for switch commands**
- **Modern tile-based dashboard**

## [2.8.8] - 2026-04-19

### Added

- **Refresh command after subscription** — Sends a PIA poll/refresh command (field 9) after subscribing to sensor data, matching the EHG app's "aktualisiere" behavior. This forces the SCU to re-report all current states including correct light on/off values, fixing stale cached states after HA restart

## [2.8.7] - 2026-04-19

### Changed

- **Fridge ECO is now a separate switch** — `switch.hymer_fridge_eco_ctrl` (Leise) is an independent toggle that can be enabled on top of any cooling step, matching the EHG app behavior. Previously ECO was a mutually exclusive option in the select dropdown
- **Fridge select simplified** — Options are now Off/1/2/3/4/5 only. ECO removed from the dropdown since it's an overlay, not a mode
- **Dashboard** — Fridge section now shows: Cooling Step (Kühlstufe) select, Quiet Mode (Leise) toggle, Door (Tür) status — matching the EHG app layout

## [2.8.6] - 2026-04-19

### Fixed

- **Fridge command timing** — Added 500ms delay between power-on and cooling step commands to give the SCU time to process. Removed unnecessary ECO-off command when setting cooling steps (matching EHG app behavior)

## [2.8.5] - 2026-04-19

### Added

- **Heater fan speed control (experimental)** — Fan mode Eco/High available in the Truma heater climate entity. This sends `bus=58, sid=5` with `ECO` or `High` string values. Note: the EHG app does NOT expose this control — use at your own risk. Test at the vehicle before relying on it
- **Thermostat card Heat/Off buttons** — Added explicit HVAC mode feature to the dashboard thermostat card

## [2.8.4] - 2026-04-19

### Fixed

- **Dashboard entity ID alignment** — Fixed 14 entity IDs in the dashboard that didn't match HA's auto-generated names from translation keys (e.g. `sensor.hymer_battery_soc` → `sensor.hymer_battery_level`, `sensor.hymer_coolant_temp` → `sensor.hymer_coolant_temperature`, `binary_sensor.hymer_lock_status` → `binary_sensor.hymer_lock`)

## [2.8.3] - 2026-04-19

### Fixed

- **Clean entity IDs** — Device name simplified from `HYMER HYMER Connect (HYMER)` to `HYMER`, producing clean entity IDs like `sensor.hymer_battery_voltage` instead of `sensor.hymer_hymer_connect_hymer_battery_voltage`. **Requires removing and re-adding the integration for existing installations**
- **Dashboard** — All entity references updated to use clean `hymer_` prefix

## [2.8.2] - 2026-04-19

### Fixed

- **Fridge select auto-powers on** — Selecting a cooling step (1-5) or ECO now automatically powers on the fridge first (bus 34, sid 1). Selecting Off disables ECO and powers off. Previously the fridge stayed off because only the cooling step was sent without the power-on command

## [2.8.1] - 2026-04-19

### Fixed

- **Outside light** — Moved from switch (bus 25) to proper light entity (bus 24) with brightness and color temperature control, matching all other interior lights
- **Removed duplicate** — Outside light no longer appears in both Lights and Controls sections of the dashboard
- **Bus 25 sensor** — Reverted bus 25 sid 1 back to grey water sensor (was incorrectly mapped as outside light)

## [2.8.0] - 2026-04-19

### Added

- **Climate entity for Truma heater** — `climate.truma_heater` with ON/OFF and target temperature (5-30°C). Sends multi-sensor PIA commands (setpoint + fuel type) matching the official EHG app protocol
- **Select entity for fridge mode** — `select.fridge_mode_ctrl` with options: Off, 1-5 (cooling steps), ECO. Controls bus 34 sensors (sid 1=power, sid 2=ECO, sid 3=cooling step)
- **Select entity for boiler mode** — `select.boiler_mode_ctrl` with options: Off, ECO, Turbo. Sends bus 58 sid 5 with values OFF/ECO/HOT + fuel type
- **Outside light switch** — `switch.outside_light_ctrl` on bus 25, sid 1
- **Multi-sensor PIA command builder** — `build_multi_sensor_command()` in pia_decoder.py supports string and float protobuf fields for heater/boiler commands
- **New Controls view** in dashboard with all switches

### Fixed

- **Water pump switch** — Corrected from bus 22/sid 1 to bus 3/sid 3 (confirmed via mitmproxy capture)
- **Sensor map** — Bus 34 correctly mapped as fridge control (sid 1=power, 2=ECO, 3=cooling step), bus 25 as outside light
- **Heater fan speed labels** — Added "HOT" → "Hot" mapping for boiler turbo mode

### Removed

- **Heater switch** — Replaced by the new climate entity which provides proper thermostat controls

### Changed

- **Dashboard Climate view** — Replaced sensor-only heater display with thermostat card, boiler select, and fridge select controls

## [2.7.0] - 2026-04-19

### Added

- **Switch platform** — New controllable switch entities for 12V Main switch (bus 3), Water pump (bus 22), and Heater (bus 34). Uses the same PIA protobuf command structure as lights. Includes optimistic state with SCU confirmation

## [2.6.4] - 2026-04-18

### Fixed

- **Charge phase always showing "Bulk"** — The EBL controller reports its last known charge phase even when no charging is active. The sensor now shows "Idle" when neither solar nor mains charger is actively charging, and only displays the real phase (Bulk/Absorption/Float) during actual charging

## [2.6.3] - 2026-04-12

### Fixed

- **DPF status is a status flag, not a percentage** — Reverted `%` unit added in v2.6.2. The SCU reports DPF as a binary status (0/1), not a soot load percentage. Added human-readable labels: `0` = "Normal", `1` = "Regeneration"

## [2.6.2] - 2026-04-12

### Fixed

- **DPF status missing unit** — Added `%` unit to DPF status sensor in PIA decoder and sensor definition. The Mercedes CAN bus reports DPF soot load as a percentage of maximum capacity

### Added

- **Stale CAN sensor workaround documentation** — Added dashboard README section with HA template sensor workarounds for stale cached CAN values (engine running, speed, RPM, engine torque) and known limitations (DPF status, coolant temperature)

## [2.6.1] - 2026-04-07

### Fixed

- **Solar/sensor data going stale** — Reverted resubscription throttle from 5min back to every poll (60s). The SCU only pushes fresh sensor data in response to subscription requests — throttling resubscriptions caused sensors like solar voltage/current to show outdated values

## [2.6.0] - 2026-04-07

### Fixed

- **Stale data / silent disconnection** — SignalR WebSocket connections silently died when the Azure token expired (~1h) and reconnection could fail indefinitely without backoff, leaving the dashboard stuck on stale data until HA reboot
- **Excessive API calls** — REST metadata (VIN, model, URNs) was re-fetched on every 60s poll despite being static; now cached and refreshed every 10 minutes

### Added

- **Proactive connection recycling** — SignalR connection is proactively recycled after 50 minutes (before Azure token expiry at ~1h)
- **Dead connection detection** — If no sensor data arrives for 10 minutes on a "connected" WebSocket, the connection is flagged as dead and recycled
- **Exponential reconnection backoff** — Failed reconnection attempts use exponential backoff (60s → 120s → … → 15min cap) to avoid hammering the API when the server is unavailable
- **Improved `connected` property** — Now checks actual WebSocket state (`ws.closed`) in addition to the internal flag

## [2.5.4] - 2026-04-06

### Removed

- **Group switch light entities** — Removed "All Wohnen" (bus 24) and "All Privat" (bus 15) group switch entities. These used hardware group toggles (sid=1) that behaved unpredictably. Use HA light groups instead for reliable group control of individual lights

### Changed

- **Simplified light code** — Removed `use_brightness_for_on_off` flag and all associated branching logic. All 8 lights now use the same simple on/off + brightness + color_temp control path

## [2.5.3] - 2026-04-06

### Fixed

- **Lights switch back on after turning off** — The timer-based optimistic clear was reading stale SCU sensor data (still showing ON) and overwriting the OFF command. Replaced with confirmation-based approach: optimistic on/off state now persists until the SCU pushes a matching value via SignalR. No more timer, no more stale readback

## [2.5.2] - 2026-04-06

### Fixed

- **Lights revert to off after 5 seconds** — `_schedule_clear_optimistic` was calling `async_request_refresh()` which triggered a resubscribe. The SCU returned stale cached `False` values for light sensors, making HA think the light turned off. Removed the refresh call — optimistic state now clears after 10s and the next regular 60s poll or SignalR push updates the real state
- **Bedroom ambient brightness restored** — Re-added `brightness_path` for bedroom ambient since (15,2) is now correctly mapped back to `light_bedroom_ambient_brightness` in v2.5.0

## [2.5.1] - 2026-04-06

### Fixed

- **Light controls broken — commands replayed on every resubscribe** — `_PIA_REQUESTS` contained 6 device command payloads (light ON/OFF on bus 15, fridge ECO/OFF on bus 58, water valve ON/OFF on bus 34) that were accidentally captured from an app session alongside the 7 legitimate subscription payloads. Every 60-second resubscribe cycle re-sent these commands, causing lights to toggle ON then immediately OFF. Removed the 6 command payloads, keeping only the 7 subscription/init requests
- **Dead variable in `build_light_command`** — Removed unused `command` variable

## [2.5.0] - 2026-04-06

### Fixed

- **Protobuf decoder bug** — Message wrappers (F1>1000) were misidentified as sensor entries, blocking recursion into nested data. Only 20 of 129 sensors were decoded per PiaResponse. Added guard to skip wrapper entries and recurse into actual sensor data
- **Bus 8 sid 2/3 remapped** — Previously wrongly mapped as indoor/outdoor temperature. Live correlation with the Hymer app confirmed these are **solar voltage (V)** and **solar current (A)** from the **Voltronic MPP260CI** MPPT charger. Delta tracking shows voltage fluctuating 16–20V matching cloud cover on the 95W panel
- **Bus 15 sid 2 restored as bedroom ambient brightness** — Was incorrectly mapped as solar_current in v2.4.3

### Added

- **Solar voltage sensor** — Real-time panel voltage from Voltronic MPP260CI (bus 8, sid 2)
- **Solar current sensor** — Real-time charge current from Voltronic MPP260CI (bus 8, sid 3)
- **Solar power sensor** — Computed voltage × current (W) for HA Energy dashboard
- **solar_active binary sensor** — True when solar current > 0

### Removed

- **indoor_temp / outdoor_temp** — These sensors never existed on this vehicle; the values were actually solar voltage/current from the Voltronic charger

## [2.4.3] - 2026-04-06

> **Note:** Versions 2.2.1–2.4.3 were iterative development releases for light control stabilisation. The final stable result is captured in v2.5.x above. These entries are preserved for reference.

<details>
<summary><strong>v2.2.1–v2.4.3 development history</strong> (click to expand)</summary>

### [2.4.3] — Solar current sensor restored

- `(15, 2)` confirmed as solar current (READ with div10), not bedroom brightness. Light OFF + app showing 3.6A proves it. Restored `solar_current` sensor with `div10` transform
- `brightness_path` removed since (15,2) reads solar current not brightness. Write commands (sid=2) still control brightness. Bedroom ambient now has on/off + color temp only
- Bus 15 is dual-purpose: READ sid=2 = solar current, WRITE sid=2 = bedroom ambient brightness

### [2.4.2] — Brightness/color temp persist until SCU confirms

- Optimistic brightness and color temp are no longer cleared on the 5s timer. They persist until the SCU pushes the updated value via SignalR (within ~5% tolerance)

### [2.4.1] — Bedroom ambient on/off restored

- Removed `use_brightness_for_on_off` which was preventing sid=1 from being sent. Bedroom ambient now sends sid=1 for on/off like all other lights

### [2.4.0] — Optimistic hold reduced to 5s

- Normal lights now refresh state after 5 seconds instead of 30. Bedroom ambient uses permanent optimistic (no timer)

### [2.3.9] — Bedroom ambient on/off via brightness

- Re-enabled `use_brightness_for_on_off` for bedroom ambient (bus 15). On/off toggle now sends brightness=100/0 instead of sid=1 (avoiding group switch)

### [2.3.8] — Lights don't turn on (reverted)

- v2.3.7 broke all lights because HA always includes ATTR_BRIGHTNESS in kwargs for COLOR_TEMP/BRIGHTNESS modes. Reverted: normal lights always send sid=1 on turn_on

### [2.3.7] — Brightness slider clears optimistic on state

- Attribute-only changes don't schedule optimistic clear, preserving the on state from the previous toggle

### [2.3.6] — Optimistic hold increased to 30s

- Bedroom ambient was falling back to off after 10s because bus 15 sid=1 read state doesn't reflect individual on/off

### [2.3.5] — Sliders don't trigger on/off

- `optimistic_on` only set when sid=1 is actually sent (pure on/off toggle). Adjusting brightness/color temp on an off light only stores the value without toggling

### [2.3.4] — Brightness/color temp slider doesn't send sid=1

- For ALL lights, sid=1 (on) is only sent for pure on/off toggle (no attributes). Prevents bus 15 group switch from triggering when adjusting sliders

### [2.3.3] — Bedroom ambient uses normal sid=1 on/off

- Removed `use_brightness_for_on_off`. Bus 15 sid=1 is the private area group switch — hardware limitation

### [2.3.2] — Bedroom ambient is_on uses brightness

- For `use_brightness_for_on_off` lights only, `is_on` checks brightness > 0

### [2.3.1] — Private area group switch

- Bus 15 sid=1 controls all private area lights. Added as 10th light entity "Privat all lights"

### [2.3.0] — Bedroom ambient always shows on (fixed)

- Brightness-based is_on was reading non-zero brightness even when light is off. Reverted is_on to always use on_off_path

### [2.2.9] — All lights bouncing off (reverted)

- v2.2.7 changed `is_on` for ALL lights with brightness_path — broke lights where brightness reads 0 when off. Reverted to on_off_path

### [2.2.8] — Bedroom ambient sid 1 is group switch

- Added `use_brightness_for_on_off` flag for bus 15 to avoid triggering the private area group switch

### [2.2.7] — Bedroom ambient on/off state

- For lights with brightness_path, `is_on` derives from brightness > 0 instead of on_off_path

### [2.2.6] — Bedroom ambient brightness restored

- App screenshot confirms bus 15 has brightness (26%) + color temp (100). The `div10` transform was the bug

### [2.2.5] — Bedroom ambient bounce-off root cause

- Bus 15 sid 2 is NOT brightness, it's solar current. Removed brightness_path from bedroom ambient

### [2.2.4] — Bedroom ambient still bouncing off

- Reordered command sequence: send on (sid=1) first, then brightness, then color temp. Increased optimistic hold to 10s

### [2.2.3] — Light turns off immediately after on

- Immediate `async_request_refresh()` was reading stale SCU state. Added optimistic state with 5s hold

### [2.2.2] — Bedroom ambient brightness + color temp

- `(15, 2)` mapped to brightness, `(15, 3)` to color temp

### [2.2.1] — Night light brightness confirmed

- `(16, 2)` remapped from `water_pump_status` to `light_nightlight_brightness`

</details>

## [2.2.0] - 2026-04-06

### Added

- **Color temperature slider** — lights with color temp support (Living ambient, Kitchen) now show a warm↔cool slider in the HA UI
  - Maps SCU 0-100% range to 2700K (warm white) – 6500K (daylight)
  - Color temp commands sent via `(bus, sid=3, uint=0-100)` write protocol
- **Uniform light card dashboard** — all 9 lights use `type: light` cards showing on/off toggle, brightness slider, and color temp slider based on each light's capabilities

## [2.1.0] - 2026-04-06

### Added

- **Brightness slider controls** — lights with brightness support now show a slider in the HA UI
  - Living ceiling, Living ambient, Kitchen, Seating overhead, Bathroom ceiling, Bedroom overhead: brightness 0-100%
  - Bedroom ambient, Night light, Wohnen group: on/off only
- Brightness commands sent via `(bus, sid=2, uint=0-100)` write protocol

## [2.0.4] - 2026-04-06

### Fixed

- **Bus 24 is Wohnen group switch, not outside light** — toggling bus 24 turns on/off ALL living area lights (confirmed by user). Renamed to "Wohnen all lights" and moved to Wohnen section in dashboard. Outside light bus still needs to be identified via mitmproxy capture

## [2.0.3] - 2026-04-06

### Added

- **Outside light (LED strip)** — bus 24 added as 9th controllable light entity
- Dashboard updated with Außen (Outside) section

## [2.0.2] - 2026-04-06

### Fixed

- **6 of 8 lights not created** — lights with BRIGHTNESS color mode weren't registered by HA. Changed all lights to ONOFF mode so all 8 entities are created

## [2.0.1] - 2026-04-06

### Fixed

- **Import error on startup** — `ATTR_COLOR_TEMP` removed from `homeassistant.components.light` in newer HA versions. Removed unused import

## [2.0.0] - 2026-04-06

### Added

- **Light write controls** — 8 controllable HA light entities with on/off and brightness (#23, #6)
  - Turn lights on/off from Home Assistant via SignalR PiaRequest commands
  - Brightness control (0-100%) with HA slider
  - New `light.py` platform with `LightEntity` subclass
  - `build_light_command()` protobuf encoder in pia_decoder.py
  - `send_light_command()` method in signalr_client.py
  - `signalr_client` property exposed on coordinator
- **Dedicated Lights dashboard page** with Wohnen (Living) and Privat (Private) groups

### Known Issues

- Light state reading may not update when lights are toggled physically or via the Hymer app (#25)
- Outside light brightness shows 10000 instead of percentage (#21)

## [1.11.0] - 2026-04-05

### Added

- **Individual light sensors** — all 9 lights now mapped with on/off binary sensors and brightness percentage sensors (#18):
  - **Wohnen**: Living ceiling (bus 11), Living ambient (bus 12), Kitchen (bus 21), Seating overhead (bus 43)
  - **Privat**: Bedroom ambient (bus 15), Night light (bus 16), Bathroom ceiling (bus 19), Bedroom overhead (bus 44)
  - **Außen**: Outside light (bus 24)
- **8 new binary sensors** for light on/off state
- **6 new brightness sensors** showing last-used brightness percentage

### Changed

- **Sensor renames** — bus IDs previously misidentified as alarm/step/dimmer are actually individual lights:
  - Bus 11: `alarm_armed` → `light_living_ceiling`, `alarm_battery` → `light_living_ceiling_brightness`
  - Bus 12: `step_retracted` → `light_living_ambient`, `step_sensor_2` → `light_living_ambient_brightness`
  - Bus 16: `water_pump` → `light_nightlight` (shared bus — same signal)
  - Bus 15: `solar_charger_boost` → `light_bedroom_ambient` (shared bus with solar current)

## [1.10.1] - 2026-04-05

### Fixed

- **engine_hours wrong divisor** — v1.10.0 used `div100` (= 36174h, impossible for a 9-month-old vehicle). Raw CAN value is in **seconds**, not hundredths of hours. Corrected to `div3600` (seconds → hours): 3,617,400s ÷ 3600 = **1,004.8 hours** — plausible for 11k km with idle time

## [1.10.0] - 2026-04-05

### Fixed

- **engine_hours now shows correct value** — raw CAN value 3617400 was displayed as-is; now applies `div100` transform to show 36174.0 hours. Confirmed via mitmproxy traces across 3 sessions (#15)
- **Removed stale translation keys** — cleaned up orphaned `fuel_consumption`, `trip_distance`, `solar_voltage`, and `solar_power` entries from strings.json and translations/en.json that were left over from v1.9.1 sensor removal

### Added

- **heat_setpoint_raw div1000 transform** — heating control raw setpoint (bus 34, sensor 7) now converts from millidegrees to °C (raw 13000 → 13.0°C)
- **New sensor map entries from mitmproxy capture** — added 14 previously unmapped sensors discovered during Apr 5 WebSocket trace:
  - GPS extended: `(30, 8-14)` — additional GPS metadata sensors
  - Heat control: `(34, 4-6)` — additional heating controller sensors
  - SCU: `(45, 9-10)` — additional SCU status sensors
  - Heater: `(58, 10, 12-14)` — additional Truma heater sensors

## [1.9.1] - 2026-04-05

### Fixed

- **Battery SOC now shows correct live value** — was reading from bus 3 s10 (habitation electronics, stale at 95%). Now reads from bus 99 s4 (`lithium_soc`) which reports the actual Lithium BMS SOC (93%) and updates via re-subscription
- **Removed false `fuel_consumption` sensor** — bus 99 s4 was misidentified as fuel consumption; it’s actually Lithium battery SOC
- **Removed false `trip_distance` sensor** — bus 99 s8 was misidentified as trip distance (showed 93 “km” when actual trip was 0.1 km); it’s a duplicate Lithium SOC value
- **Outdoor temperature** — confirmed as cached Mercedes CAN value from last drive (shows 3°C when actual outdoor temp is 19°C). Only updates when engine is running

## [1.9.0] - 2026-04-05

### Added

- **Periodic PIA re-subscription** — the coordinator now re-sends all PIA subscription requests on each 60-second poll cycle. This is required because the SCU only pushes updated sensor values in response to subscription requests. Without re-subscribing, sensors like battery SOC, solar current, fuel range, and trip distance stay at their initial cached values. Confirmed via 5-minute delta capture: re-subscribing triggered fuel_range, trip_distance, engine_torque, and total_fuel_used updates

### Changed

- **Outdoor temperature** — documented as Mercedes CAN cached value (bus 8 s3 / bus 99 s3). Only updates when the engine is running. The Hymer has no dedicated outdoor temperature sensor; requires a mitmproxy capture of Mercedes me API for real-time outdoor temp

## [1.8.6] - 2026-04-05

### Fixed

- **Stale sensor data / no live updates** — SignalR listen loop could silently die (unhandled exception in message handler, WebSocket disconnect), leaving sensor data frozen at initial values. Fixed with:
  - Proper error handling in the listen loop — individual message errors no longer crash the entire loop
  - Automatic reconnection — coordinator now detects dead connections on each poll and reconnects with fresh subscriptions
  - Stale client cleanup — old SignalR client is properly stopped before creating a new one
  - Warning-level log when listen loop ends — logs message count for diagnostics

## [1.8.5] - 2026-04-05

### Fixed

- **Solar Active always showing "Aus"** — bus 15 s1 is a PWM pulse indicator, not a steady charging flag. Changed `solar_active` to be computed from `solar_current > 0` — shows "Ein" whenever solar is producing current, regardless of the charger’s internal pulse state

## [1.8.4] - 2026-04-05

### Fixed

- **False errors in HA log** — coordinator and SignalR client used `warning` level for normal operational messages ("SignalR not connected", "Data update", "UpdateTokens SUCCESS"), causing them to appear as errors in the HA UI. Downgraded to `info`/`debug` level. Only actual failures remain as warnings/errors

## [1.8.3] - 2026-04-05

### Changed

- **Solar voltage removed** — bus 3 s19 always reports sentinel 3276.8 even during active solar charging; the SCU does not expose solar panel voltage via SignalR. The Hymer app likely reads voltage from the solar charger directly via a different channel
- **Solar power removed** — bus 15 s3 is not watts (value 58 doesn’t match V×I); likely panel temperature or charger internal value (renamed to `solar_panel_temp`)
- **Solar active** — reverted to bus 15 s1 which toggles True/False during active charging (confirmed in live capture)
- **Dashboard** — removed solar voltage, solar power, and solar charger status entities (unavailable via SignalR)

## [1.8.2] - 2026-04-05

### Fixed

- **Solar Active showing "Aus" while charging** — bus 15 s1 is NOT the solar active flag (it’s False even during active charging); changed `solar_active` binary sensor to read from `solar_connected` (bus 3, s20) which correctly reports 1 when solar is active
- Bus 15 s1 renamed to `solar_charger_boost` (purpose still TBD)

## [1.8.1] - 2026-04-05

### Fixed

- **Fresh water level wrong bus** — was mapped to bus 21 s2 (=91%, a config value); corrected to bus 22 s2 which shows ~6% matching empty tanks
- **Grey water level wrong bus** — was mapped to bus 12 s2 (=35%, likely step/drainage sensor); corrected to bus 25 s2 which shows ~6% matching empty tanks
- Both water levels now match the Hymer Connect app's "<10%" display when tanks are empty

## [1.8.0] - 2026-04-05

### Added

- **Solar current sensor** (bus 15, s2) — solar panel charge current in amps (div10 transform)
- **Solar power sensor** (bus 15, s3) — solar panel output power in watts
- **Solar active binary sensor** (bus 15, s1) — indicates whether the solar charger is actively charging
- **Fresh water level sensor** (bus 21, s2) — fresh water tank fill percentage
- **Water pump binary sensor** (bus 16, s1) — water pump on/off state
- **Sentinel value filtering** — CAN "no data" values (3276.8, 32768, 65535, 6553.5) now filtered out in both the decoder and sensor entities, preventing display of stale/invalid readings
- **30+ missing translations** — added translation keys for all existing sensors that were previously untranslated

### Fixed

- **Solar voltage showing 3276.8V** — removed incorrect `div1000` transform; the protobuf float value IS the voltage directly (like battery voltage). The 3276.8 value was a CAN sentinel (32768/10) indicating "sensor unavailable" when main power is off
- **Fridge mode labels** — expanded from `{8: Off}` to `{0: On, 1: Eco, 2: Boost, 8: Off}`
- **Fridge status labels** — expanded from `{1: Off}` to `{0: Running, 1: Off, 2: Standby}`
- **Dashboard YAML** — added solar current, solar power, solar active, fresh water level, and water pump entities

## [1.7.4] - 2026-04-04

### Fixed

- **Solar voltage reading 3276.8V** — raw protobuf value is in millivolts; added `div1000` transform so it displays correctly as ~3.3V
- **Dashboard entity ID** — fixed `solar_voltage` entity reference to actual HA-generated ID `sensor.hymer_hymer_connect_hymer_spannung`

## [1.7.3] - 2026-04-04

### Fixed

- **Sensor misidentification — solar, not mains** (closes [#13](https://github.com/BetaHydri/hymer-connect-ha/issues/13)) — sensors (3,19), (3,20), (3,21) are the **solar panel** charger, not 230V mains power:
  - `ext_charger_voltage` → `solar_voltage` (reads ~2-3V with no sun, higher in daylight)
  - `mains_connected` → `solar_connected` (always 1 because solar panel is hardwired)
  - `charger_status` → `solar_charger_status` (1 = standby)
- Icons updated to `mdi:solar-power` / `mdi:solar-power-variant`

## [1.7.2] - 2026-04-04

### Fixed

- **Mains power sensor false positive** — `mains_connected` incorrectly reported "plugged in" when the vehicle was parked without shore power; caused by protobuf bool field (field 5) overwriting the uint field (field 3) — since Python `True == 1`, the `on_value=1` check always matched; fixed by preferring uint/int over bool when multiple value fields are present

## [1.7.1] - 2026-04-04

### Fixed

- **Door sensors inverted** — `OFF` now correctly maps to "Closed" (was incorrectly "Open"); confirmed via Mercedes-Benz app showing vehicle locked with all doors closed

## [1.7.0] - 2026-04-04

### Added

- **26 new sensor entities** (closes [#8](https://github.com/BetaHydri/hymer-connect-ha/issues/8)):
  - Engine: RPM, engine hours
  - Heater: state, electric power (W), operating mode
  - Fridge: mode, status
  - Fuel: range (km), consumption, total used, trip distance
  - Engine: torque (%), AdBlue temperature
  - DPF status
  - Charger: external voltage, charger status
  - Lights: dimmer level 1 & 2 (%)
  - Tire pressure (bar)
  - Alarm battery (%)
  - SCU firmware, Truma firmware, Truma status
  - GPS: satellites, heading
- **10 new binary sensor entities**:
  - Rear door, headlamp, high beam, parking light, fog front/rear, turn signal
  - Truma connected, step retracted

## [1.6.3] - 2026-04-04

### Fixed

- **SignalR log noise** — changed PiaResponse and SignalR message logs from WARNING to DEBUG level; connection events changed to INFO

## [1.6.2] - 2026-04-04

### Fixed

- **device_tracker setup error** — import `TrackerEntity` from `config_entry` module (fixes integration load failure in v1.6.1)

## [1.6.1] - 2026-04-04

### Fixed

- **Brand images** — move to `brand/` subfolder for HA 2026.3+ local brand API

## [1.6.0] - 2026-04-04

### Fixed

- **current_gear sensor** — map raw CAN value 100 to "P" (Park), gears 1-7 for drive positions (closes [#5](https://github.com/BetaHydri/hymer-connect-ha/issues/5))

### Added

- **device_tracker entity** — shows vehicle location on the HA map from GPS coordinates with altitude, heading, satellites, and signal quality as attributes (closes [#11](https://github.com/BetaHydri/hymer-connect-ha/issues/11))
- HC brand logo (icon.png, logo.png) for HA integration UI and GitHub README
- "Open in HACS" button in README

### Changed

- Hardened `.gitignore` — excludes `.venv*/`, `.env`, `private_*` files

## [1.5.2] - 2026-04-04

### Added

- Created GitHub issues for all known TODOs and missing functionality
- Updated README screenshots (new ha-screenshot.png, added ha-screenshot_2.png)
- Synced root README.md to v1.5.0 component version

### Known Issues

- ~~**current_gear shows raw value 100**~~ — fixed in v1.6.0 ([#5](https://github.com/BetaHydri/hymer-connect-ha/issues/5))
- **Integration is read-only** — no write controls for lights, heater, fridge, awning, switches ([#6](https://github.com/BetaHydri/hymer-connect-ha/issues/6))
- **9 bus IDs unmapped** — awning, ext_light, dimmer, roof_vent, screen, inverter, generator, wifi, bluetooth ([#7](https://github.com/BetaHydri/hymer-connect-ha/issues/7))
- **30+ mapped sensors not exposed as HA entities** — rpm, engine_hours, fridge, tire_pressure, fuel_range, and more ([#8](https://github.com/BetaHydri/hymer-connect-ha/issues/8))
- **Several sensors show Nicht verfügbar** — fresh water, fuel level, heater mode, lock status, duplicate sliding door ([#9](https://github.com/BetaHydri/hymer-connect-ha/issues/9))
- **Delta-only updates after reconnect** — SCU only sends full dump on first connection ([#10](https://github.com/BetaHydri/hymer-connect-ha/issues/10))
- ~~**GPS not exposed as device_tracker**~~ — fixed in v1.6.0 ([#11](https://github.com/BetaHydri/hymer-connect-ha/issues/11))
- **Truma boiler sensors unmapped** — bus 58 sensors 10-14 ([#12](https://github.com/BetaHydri/hymer-connect-ha/issues/12))

## [1.5.1] - 2026-04-04

### Changed

- **heater_mode → heater_fan_speed** — sensor (58,5) reports the Truma Combi 6E fan speed setting: Off/Eco/High (confirmed via PiaRequest protobuf decode)
- Added ECO and HIGH value labels for fan speed display
- Updated sensor icon to `mdi:fan`

## [1.5.0] - 2026-04-04

### Changed

- **heater_fan_speed → heater_electric_power** — Truma Combi 6E sensor (58,9) reports electric heating element power in Watts (0/900/1800), not fan speed

### Discovered (not yet mapped)

- **Fridge OFF state**: `fridge_mode=8`, `fridge_status=1` — can identify fridge on/off
- **Truma heater OFF state**: `heater_mode=Off`, `heater_state=False`, `heater_setpoint=-273.0` — correctly mapped
- **Truma boiler OFF state**: bus58 sensors 10-14 all False when boiler is off
- **Light control is write-only** — the app sends PiaRequest commands to toggle lights, but the SCU does not report light state changes back through sensor data. `light_1_level`/`light_2_level` (3,8)/(3,9) show 0% regardless of light state. Needs mitmproxy capture of write PiaRequests at vehicle.
- **Lights have dimmer + color temperature (CCT)** — each light group supports brightness % and warm↔cool white
- **SCU only sends full sensor dump on first connection** — subsequent connections receive delta updates only (~17 sensors)

## [1.4.0] - 2026-04-04

### Fixed

- Battery SOC: renamed from fresh_water_level to battery_soc (3,10) — matches app Lithium-Batterie 95%
- Chassis battery voltage: renamed from solar_voltage to chassis_battery_voltage (3,7) — matches app 12.3V
- AdBlue level: renamed from fuel_level to adblue_level (1,6) — matches app 88%
- Odometer divisor: div1000 (raw 11113500 / 1000 = 11,113.5 km)

## [1.3.0] - 2026-04-04

### Changed

- Doors converted to binary sensors with DOOR device class — HA auto-translates: Offen/Geschlossen (DE), Open/Closed (EN)
- Lock converted to binary sensor with LOCK device class — HA: Gesperrt/Entsperrt
- Main switch converted to binary sensor with POWER device class — HA: Ein/Aus
- No more mixed English/German labels — all states translated by HA based on user's language

### Removed

- Duplicate text sensors for doors, lock, and main switch (replaced by binary sensors)

## [1.2.1] - 2026-04-04

### Changed

- Door sensors now show "Open"/"Closed" instead of raw CAN values "OFF"/"CLS"/"SNA"
- Ignition sensor shows "Off"/"On"/"Starting" instead of "IGN_LOCK"/"IGN_ON"/"IGN_START"
- Lock status shows "Locked"/"Unlocked" instead of raw strings
- Headlamp, fog lights, heater mode show "On"/"Off" instead of raw "ON"/"OFF"

## [1.2.0] - 2026-04-04

### Added

- Proper friendly names for all 39 sensor and binary sensor entities (Odometer, Speed, Fuel level, Lock status, Ignition, Driver door, etc.)

### Fixed

- Entities showing generic "HYMER HYMER Connect (HYMER)" name instead of descriptive sensor names
- Heater setpoint showing -273.0°C when heater is off (now shows as unavailable)
- Translation keys in strings.json/en.json now match all sensor entity descriptions

## [1.1.0] - 2026-04-04

### Added

- **PiaRequest subscription** — integration now sends all 13 PiaRequest messages after UpdateTokens to subscribe to sensor data streams
- 142 live sensors now populate in Home Assistant (battery, GPS, water, temps, doors, heater, fridge, alarm, odometer, and more)

### Fixed

- Sensor entities were showing `unknown` because PiaRequest subscription messages were missing after SignalR connection
- Fixed subscription payload to use exact captured protobuf from the Hymer Connect app

## [1.0.0] - 2026-04-04

### Added

- **Real-time sensor data via SignalR** — 130+ sensors including odometer, GPS, battery, water levels, temperatures, door status, heater, fridge, alarm, and more
- **EHG Remote Access Token refresh flow** — discovered `POST /api/ehg/v1/vehicles/{urn}/remoteAccessToken` endpoint that exchanges a long-lived refresh token for short-lived access tokens
- **EHG Refresh Token field** in the integration config flow (optional, required for real-time sensors)
- **`get_remote_access_token()` method** in API client for automatic token exchange
- **Comprehensive README** with step-by-step token extraction guide, mermaid architecture diagrams, and sequence diagrams
- **`.env` support** for local development credentials (`.env` added to `.gitignore`)

### Changed

- **SignalR client rewritten** — single refresh-based authentication flow instead of multi-variant fallback attempts
- **Coordinator** passes EHG refresh token through to SignalR client
- **Config flow** updated with optional EHG refresh token input field
- **Version bumped** from 0.3.x to 1.0.0

### Removed

- Hardcoded owner activation token from `signalr_client.py`
- Multi-variant UpdateTokens fallback logic (no longer needed)
- Obsolete "Help Wanted" section from README
- Outdated development status checklist from README

### Security

- Removed all hardcoded tokens and credentials from source code
- Added `.env` to `.gitignore` to prevent credential leaks
- Credentials stored locally only, never in version control

## [0.3.16] - 2026-04-03

### Fixed

- Parse paginated EHG vehicles response (`{content: [...]}` wrapper) to correctly extract vehicle URN

## [0.3.15] - 2026-04-03

### Fixed

- Allow SignalR to start with only SCU URN when vehicle URN is not yet discovered

## [0.3.14] - 2026-04-03

### Fixed

- Upgrade coordinator URN discovery and SignalR start logs to WARNING level for visibility

## [0.3.13] - 2026-04-03

### Fixed

- Remove auth headers from SignalR negotiate request to match real app behavior

## [0.3.12] - 2026-04-03

### Fixed

- Try owner activation token (`ett=owner`) as `ehgAccessToken` in UpdateTokens

## [0.3.11] - 2026-04-03

### Fixed

- Use correct `vehicleUrn` (`urn:ehg:vehicle:hy-...`) from EHG API instead of SCU URN

## [0.3.10] - 2026-04-03

### Fixed

- Test multiple `ehgAccessToken` variants with SignalR negotiate token as `accessToken`

## [0.3.9] - 2026-04-03

### Fixed

- Try SignalR negotiate token as `accessToken` in UpdateTokens

## [0.3.8] - 2026-04-03

### Fixed

- Continue after UpdateTokens failure (connection authenticated via JWT in URL)
- Log all SignalR messages at WARNING level for debugging

## [0.3.7] - 2026-04-03

### Fixed

- Try multiple UpdateTokens argument format variants sequentially

## [0.3.6] - 2026-04-03

### Fixed

- Revert UpdateTokens to dict format with 3 keys

## [0.3.5] - 2026-04-03

### Fixed

- Use positional args for UpdateTokens instead of object

## [0.3.4] - 2026-04-03

### Fixed

- Upgrade SignalR flow logs to WARNING/INFO for system_log visibility

## [0.3.3] - 2026-04-03

### Changed

- Add `*.docx` to `.gitignore`

## [0.3.2] - 2026-04-03

### Fixed

- Re-authenticate on startup, fix token refresh URL encoding, propagate auth errors

## [0.3.0] - 2026-04-03

### Added

- **SignalR datahub integration** with real API protocol
- **PIA Protobuf decoder** — 131 sensors mapped from vehicle bus data
- Pre-computed Basic auth header to avoid encoding issues with special characters

## [0.1.0-alpha] - 2026-04-03

### Added

- Initial HYMER Connect integration for Home Assistant
- OAuth2 ROPC authentication with EHG cloud API
- REST API sensors (vehicle model, VIN, model year)
- Binary sensors (SIU online, mains power, doors, windows, alarm, heater, fridge)
- Config flow with brand selection and credential input
- Reauth flow support
- Ready-to-use Lovelace dashboard
- HACS compatibility

[1.3.0]: https://github.com/BetaHydri/hymer-connect-ha/compare/v1.2.1...v1.3.0
[1.2.1]: https://github.com/BetaHydri/hymer-connect-ha/compare/v1.2.0...v1.2.1
[1.2.0]: https://github.com/BetaHydri/hymer-connect-ha/compare/v1.1.0...v1.2.0
[1.1.0]: https://github.com/BetaHydri/hymer-connect-ha/compare/v1.0.0...v1.1.0
[1.0.0]: https://github.com/BetaHydri/hymer-connect-ha/compare/v0.3.16...v1.0.0
[0.3.16]: https://github.com/BetaHydri/hymer-connect-ha/compare/v0.3.15...v0.3.16
[0.3.15]: https://github.com/BetaHydri/hymer-connect-ha/compare/v0.3.14...v0.3.15
[0.3.14]: https://github.com/BetaHydri/hymer-connect-ha/compare/v0.3.13...v0.3.14
[0.3.13]: https://github.com/BetaHydri/hymer-connect-ha/compare/v0.3.12...v0.3.13
[0.3.12]: https://github.com/BetaHydri/hymer-connect-ha/compare/v0.3.11...v0.3.12
[0.3.11]: https://github.com/BetaHydri/hymer-connect-ha/compare/v0.3.10...v0.3.11
[0.3.10]: https://github.com/BetaHydri/hymer-connect-ha/compare/v0.3.9...v0.3.10
[0.3.9]: https://github.com/BetaHydri/hymer-connect-ha/compare/v0.3.8...v0.3.9
[0.3.8]: https://github.com/BetaHydri/hymer-connect-ha/compare/v0.3.7...v0.3.8
[0.3.7]: https://github.com/BetaHydri/hymer-connect-ha/compare/v0.3.6...v0.3.7
[0.3.6]: https://github.com/BetaHydri/hymer-connect-ha/compare/v0.3.5...v0.3.6
[0.3.5]: https://github.com/BetaHydri/hymer-connect-ha/compare/v0.3.4...v0.3.5
[0.3.4]: https://github.com/BetaHydri/hymer-connect-ha/compare/v0.3.3...v0.3.4
[0.3.3]: https://github.com/BetaHydri/hymer-connect-ha/compare/v0.3.2...v0.3.3
[0.3.2]: https://github.com/BetaHydri/hymer-connect-ha/compare/v0.3.0...v0.3.2
[0.3.0]: https://github.com/BetaHydri/hymer-connect-ha/compare/v0.1.0-alpha...v0.3.0
[0.1.0-alpha]: https://github.com/BetaHydri/hymer-connect-ha/releases/tag/v0.1.0-alpha
