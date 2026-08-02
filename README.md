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
| Cluster | System status | Ok / Problem across the bank, `device_class: problem` |
| Cluster | Active alarms | how many are firing, with their names as attributes |
| Cluster | Batteries expected / reported | detected bank size, and how many answered the last poll |
| Cluster | Stack voltage, Min / max cell voltage, Cell imbalance | across every cell |
| Cluster | Charging state | charging / discharging / idle, derived from current |
| Cluster | Time between polls, Poll duration, Consecutive poll failures | diagnostic |
| Cluster | Signal strength | RSSI, diagnostic, disabled by default |
| Battery | System status | Ok / Problem for that battery |
| Battery | Active alarms | count plus the firing alarm names |
| Battery | *29 named alarms* | one per app alarm, `device_class: problem`, diagnostic |
| Battery | Temperature sensor 1–6 | pack sensors; see [Temperatures](#temperatures) |
| Battery | Cell 01–16 voltage | on the battery device, not a device each |
| Battery | Stack voltage, Min / max cell voltage, Cell imbalance | |
| Battery | Weakest / strongest cell | a cell *position*, so no statistics |
| Battery | Relay 1–4 | diagnostic, disabled by default |

**Cell imbalance** is the one worth an automation — a cell drifting away from
its pack is the earliest visible sign of a failing cell.

## Alarms

The BMS reports 29 named alarms, matching the vendor app's alarm page row for
row. Each is a severity of 0-3, where the app renders 1/2/3 as "Level1/2/3
Fault".

The per-alarm entities are filed as **diagnostic**, so they appear in their
own card rather than burying the device page -- 29 per battery is otherwise
most of it. That is placement only: they keep their device class, still trigger
automations, and are still enabled. See [Grouping](#grouping) for why this is
the only lever available.

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

## Temperatures

Each battery reports four or six **pack** temperature sensors — these measure
the pack, not individual cells. They are whole degrees Celsius, sent as an
unsigned byte with a 50 °C offset, so the representable range is −50 to 205 °C.

Two values are not measurements: **−50 °C** and **−30 °C**. A sensor reporting
either is not returning a reading.

**What they mean is not established, and the vendor app cannot tell us.** Its
temperature page has no sentinel logic at all: it applies the −50 offset
unconditionally and prints whatever comes out, showing a dash only when a value
is absent entirely. There is no comparison against these values anywhere in the
app, and no `-50`/`-30` constant in its binary. So it displays −50 °C exactly as
it displays 25 °C, and so do we by default.

The capture evidence points in a direction opposite to intuition, which is why
nothing is claimed:

| Value | Where it appears | Reads as |
|---|---|---|
| −30 | position 5 on *both* six-sensor packs, in every sample | a position that model never fits |
| −50 | positions 3–4 on one pack, while an identical four-sensor pack reports real temperatures there | a genuine sensor fault |

So the *systematic* one is −30 and the *unit-specific* one is −50 — the reverse
of reading "more negative" as "more absent". Settling it needs someone to
unplug a known-good sensor and see which value appears.

**Option: "Hide non-reporting temperature sensors."** Off by default, matching
the app. Turn it on and sensors reporting either value become `unavailable`
instead, which keeps them out of temperature history and statistics. Only those
two exact values are affected — a genuinely cold pack below freezing is still
reported normally.

## Not supported

**SD-card status.** The protocol carries it — type `0x00`, offset `[29]`, the
app calls it `SDStatus` — but it reads `0x00` in every capture and **these
batteries have no SD-card slot**, so no other value can be produced to compare
against. The app parses it and does not display it on its summary page either.
It is decoded and exposed raw rather than interpreted, and no sensor is created
for it.

**Pack status byte.** Offset `[6]`, the app's `Status`. Only `0x00`–`0x02` seen,
and the app renders it nowhere, so there is no wording to attach. The
*Charging state* sensor is derived from the sign of the current, not from this
field — that is a deliberate distinction, not an oversight.

**Relay route values 0 vs 3.** Only `1` is established as active, by pairing a
capture with the app screenshot taken from it. The app draws `0` and `3`
identically, so they are passed through raw.

**Writing anything.** Every frame this integration sends is a read request. The
app's relay page builds no command message, so even the relays are status
readouts — hence `binary_sensor`, not `switch`.

## Grouping

Home Assistant device pages cannot be grouped arbitrarily. An entity's only
placement control is `entity_category`, which supports exactly two values --
Configuration and Diagnostic -- so a device page renders at most three buckets,
and which entities land where is fixed by the integration rather than by the
person looking at it.

With 75 entities on a battery, that is not enough. For real grouping use a
dashboard: [`dashboard.yaml`](dashboard.yaml) has a view per device sectioned
into Overview, Cell voltages, Temperatures, Alarms, Relays and Diagnostics.

```
Settings -> Dashboards -> Add dashboard -> open it -> Edit
-> three-dot menu -> Raw configuration editor -> paste
```

Entity ids are installation specific, so regenerate after adding a battery:

```bash
uv run --with pyyaml python tools/make_dashboard.py root@homeassistant > dashboard.yaml
```

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

- **Delay between polls** (default 60 s, min 60). A floor, not a schedule:
  polls are triggered by the gateway's Bluetooth advertisements, and this
  hardware advertises only every 4–5 minutes, so that cadence is the real
  ceiling. Raising this polls less often; lowering it cannot poll more often
  than the gateway advertises. Watch *Time between polls* for what is actually
  happening.
- **Detect battery count automatically** (default on). The bank master states
  how many batteries it has, so there is normally nothing to configure. While
  this is on, the count below is ignored and kept in step with whatever the
  bank reports.
- **Battery count** (1–8, the hardware maximum). Only used when detection is
  off — for instance if a battery is offline for a while and you would rather
  not have every poll waiting for it. Home Assistant option forms are static,
  so the checkbox above cannot grey this field out; it stays editable but has
  no effect while detection is on.
- **Hide non-reporting temperature sensors** (default off). See
  [Temperatures](#temperatures).

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
