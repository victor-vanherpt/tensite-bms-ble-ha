# tensite-bms-ble-ha

Home Assistant integration for Tensite / UhomeEnergy battery clusters over
Bluetooth LE. Home Assistant wiring only — all protocol and BLE work lives in
[`tensite-bms-ble`](https://github.com/victor-vanherpt/tensite-bms-ble).

Point it at one battery cluster and it discovers every battery in that bank and
exposes their cell voltages.

## Install

**HACS** → Custom repositories → add this repo as an *Integration* → install →
restart Home Assistant.

**Manual** — copy `custom_components/tensite_bms_ble/` into your `config/custom_components/`
and restart.

Batteries advertise manufacturer ID `0xE502` carrying `UHOME`, so Home Assistant
should offer the cluster on its own under **Settings → Devices & Services**. If
not, add it manually and pick the master from the list.

## Device hierarchy

The registry mirrors the hardware, so each entity attaches where it belongs:

```
Cluster C01                      ← the BLE gateway
├── Battery PA0 08146            ← master
│   ├── Cell 01 … Cell 16        ← voltage per cell
├── Battery P01 08099
│   └── Cell 01 … Cell 16
├── Battery P02 08313
└── Battery P03 08051
```

Batteries and cells appear after the first successful poll, not at setup, since
the bank is only enumerated once frames start arriving.

### Entities

| Level | Entity | Notes |
|---|---|---|
| Cluster | Batteries | count in the bank (diagnostic) |
| Cluster | Stack voltage | mean across the bank |
| Cluster | Min / max cell voltage, Cell imbalance | across every cell |
| Cluster | Signal strength | RSSI, diagnostic, disabled by default |
| Battery | Stack voltage | sum of that battery's cells |
| Battery | Min / max cell voltage, Cell imbalance | |
| Battery | Cells | count, diagnostic, disabled by default |
| Cell | Voltage | the measurement everything else derives from |
| Cluster | Fault | on when any battery is faulted; lists which |
| Battery | Fault | on when this battery is faulted; lists which alarms |
| Battery | *29 named alarms* | one per app alarm, diagnostic, disabled by default |
| Battery | Relay 1–4 | matches the app's Relay tab, diagnostic, disabled by default |

**Cell imbalance** is the one worth an automation — a cell drifting away from
its pack is the earliest visible sign of a failing cell.

## Alarms

The BMS reports 29 named alarms, matching the vendor app's alarm page row for
row. Each is a severity of 0-3, where the app renders 1/2/3 as "Level1/2/3
Fault".

Most people want the single **Fault** sensor per battery — it turns on for any
alarm and its `active_alarms` attribute names what fired, which is enough for
an automation and for a notification that says something useful. The 29
individual sensors exist for when you want to trigger on one specific alarm;
they are disabled by default, because 29 per battery would bury everything else
in a bank that is normally entirely healthy.

The bit layout was recovered from the vendor app's own parser rather than
guessed from captures, and checked against a real fault — see
[`docs/ble-protocol.md`](../../docs/ble-protocol.md) for the derivation.

## Relays

The BMS reports four relay routes, the same ones the app's Relay tab draws.
They are **read-only**: the app's page builds no command message, so these are
`binary_sensor`s rather than `switch`es — there is nothing to write back.
Disabled by default, since they have not moved in any capture taken so far.

Each carries the underlying 0–3 value as a `value` attribute. Only `1` is
established as the active state (by matching a capture against the screenshot
taken from it); `0` and `3` both draw unhighlighted in the app, so they are not
distinguished.

## Why one connection per cluster

The ESP32 gateway accepts **one BLE central at a time**, and connecting to the
master relays frames for the whole bank. So this integration keeps exactly one
coordinator per cluster. A coordinator per battery — the obvious-looking design
— has them fighting over the single connection slot.

The practical consequence: **nothing else may talk to the gateway** while this
integration is loaded. Stop any batmon-ha add-on, script, or the vendor app
before expecting data.

## Options

**Settings → Devices & Services → Tensite BMS → Configure**

- **Polling interval** (default 300 s, min 60). Each poll holds the single
  connection for several seconds. Cell voltages drift slowly, so frequent
  polling costs availability and gains almost nothing.
- **Batteries in this cluster** (default 0 = auto). Ends a poll as soon as that
  many batteries have reported instead of waiting out the timeout. Left at 0,
  the count is learned from the first poll and reused.

## What it reports

Only what has actually been decoded and verified against the vendor app:
**per-cell voltages**, which match the app's Cell Voltage tab exactly on live
hardware.

**Not** pack current, SOC, or the 4–6 pack temperature sensors — those live in
frame types not yet decoded. Battery/cluster "stack voltage" is the sum of the
cells, which tracks the app's pack voltage closely but is derived, not reported.
No entity here is a guess.

## Bluetooth notes

Built to the [Home Assistant Bluetooth guidelines](https://developers.home-assistant.io/docs/bluetooth/):

- Uses Home Assistant's shared scanner and Bluetooth cache; it never starts a
  scanner of its own.
- Polls from `service_info.device`, the cheapest route to a usable `BLEDevice`,
  falling back to `async_ble_device_from_address` when the advertisement
  arrived via a non-connectable proxy.
- Connects through `bleak-retry-connector`, with a ≥10 s timeout so BlueZ can
  resolve services on a first connection.
- Declares `bluetooth_adapters` as a dependency so remote adapters are up first.
- Polls are driven by advertisements, so a cluster that goes out of range stops
  being polled instead of timing out repeatedly.

Works over ESPHome Bluetooth proxies, provided the proxy allows **active
connections** (ESPHome 2022.9.3+). Advertisement-only proxies are rejected in
the config flow with a clear reason, since cell data needs a real connection.

## Troubleshooting

**No devices found.** Check the batteries are powered and in range, and that
nothing else holds the gateway's single connection. Advertising is intermittent
— give discovery a minute.

**Entities unavailable after setup.** The cluster is advertising but no poll has
succeeded. Usually something else is holding the connection slot.

**Only some batteries appear.** The gateway round-robins the bank at roughly
5–6 s per battery. Set *Batteries in this cluster* to your actual count, or wait
another poll.

Debug logging:

```yaml
logger:
  logs:
    custom_components.tensite_bms_ble: debug
    tensite_bms_ble: debug
```

## License

MIT
