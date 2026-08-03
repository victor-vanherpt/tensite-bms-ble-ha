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

Batteries and cells appear once the connection opens, not at setup, since the
bank is only enumerated when frames start arriving.

### Entities

| Level | Entity | Notes |
|---|---|---|
| Cluster | System status | Ok / Problem across the bank, `device_class: problem` |
| Cluster | Active alarms | how many are firing, with their names as attributes |
| Cluster | Batteries expected / reported | the bank's own count, and how many are currently reporting |
| Cluster | Stack voltage, Min / max cell voltage, Cell imbalance | across every cell |
| Cluster | Charging state | charging / discharging / idle, derived from current |
| Cluster | Connection | switch: release the gateway for another app |
| Cluster | Time between updates, Reconnects, Connection failures | diagnostic |
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

## How data arrives

The connection is **held open**, not made per reading. That follows from what
the gateway does: it streams unprompted the moment notifications are enabled,
for the whole bank at once. In a 182-second capture of the vendor app, all four
batteries emitted cell frames every ~5.1 s *concurrently*, and kept doing so for
81 s after the app sent its last byte.

Polling could not use any of that. Connecting cost ~12 s of a ~18 s cycle, and a
connection can only be established while Home Assistant holds a connectable path
for the address — which exists around advertisements, and this gateway
advertises every 245–300 s. So readings arrived every five minutes from hardware
publishing them every five seconds.

Advertisements still matter, but only for *opening* the connection. Once open it
sustains itself, and updates are pushed as frames arrive, coalesced to at most
one every 5 s — the measured cell cadence, so nothing is dropped and nothing is
re-reported. *Time between updates* shows what is actually achieved; on live
hardware it reads 5.1 s, the gateway's own rate.

### The Activity feed

Home Assistant's logbook shows a sensor unless it counts as *continuous* — one
with a unit, a state class, or a numeric device class. On a held connection
every reading is rewritten every few seconds, so a sensor missing all three
writes a logbook line at that rate.

*Cell data age* is a measurement in seconds for exactly this reason; it was a
timestamp, and at ~585 changes an hour per battery it buried everything else in
Activity. Binary sensors are never filtered, which is why alarms and system
status still appear there — that is the intent.

Two sensors report a cell *position* rather than a measurement, so they cannot
honestly be made continuous and still show up. If their changes bother you:

```yaml
logbook:
  exclude:
    entity_globs:
      - sensor.*_weakest_cell
      - sensor.*_strongest_cell
```

### Recorder volume

Readings every 5 s instead of every 5 minutes is a lot more history. Measured on
a live bank of four, recorder rows per ten minutes for this integration's
entities: **205** under five-minute polling, **3838** streaming. Most of that is
genuine change — a 2 s throttle only raised it to 4742 — so it is the price of
the data, not overhead.

If your database is on an SD card, or you only want the long-term shape,
exclude the noisiest entities:

```yaml
recorder:
  exclude:
    entity_globs:
      - sensor.*_cell_??_voltage      # 64 entities changing every 5 s
      - sensor.*_time_between_updates
```

## Battery count

There is nothing to configure. The bank master states how many batteries it
has, in a header byte of its topology frame, and *Batteries expected* reports
that. That frame is rare — 18 against 252 summaries in a captured session —
which under polling meant an hour-long full-scan cycle to catch one. On a held
connection it simply arrives.

*Batteries reported* is how many are currently reporting. On a live connection
every battery reports every ~5 s, so this should equal the expected count within
seconds of connecting; it drops to zero when the connection does.

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
integration holds the connection — the vendor app included.

That is what the **Connection** switch on the cluster device is for. Turn it off
and the integration disconnects immediately and stays disconnected: no
advertisement reopens it, and the app can connect. Turn it back on and it
reconnects at the next opportunity. Every other entity goes unavailable while it
is off, which is the honest state; the switch itself stays available, since it is
the control that undoes this.

## Options

**Settings → Devices & Services → Tensite BMS → Configure**

- **Hide non-reporting temperature sensors** (default off). See
  [Temperatures](#temperatures).

There is no update interval to set: the connection is held open and the bank
pushes on its own cadence. Releasing the connection is the **Connection** switch
rather than an option, because it is something you toggle and then toggle back.

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
- Connects from `service_info.device`, the cheapest route to a usable
  `BLEDevice`, falling back to `async_ble_device_from_address` when the
  advertisement arrived via a non-connectable proxy, and re-adopts a freshly
  resolved one on every advertisement so reconnects do not use a stale handle.
- Connects through `bleak-retry-connector`, with a ≥10 s timeout so BlueZ can
  resolve services on a first connection.
- Declares `bluetooth_adapters` as a dependency so remote adapters are up first.
- Connections are opened on advertisements, so a cluster that goes out of range
  stops being retried instead of timing out repeatedly.

Works over ESPHome Bluetooth proxies, provided the proxy allows **active
connections** (ESPHome 2022.9.3+). Advertisement-only proxies are rejected in
the config flow with a clear reason, since cell data needs a real connection.

## Troubleshooting

**No devices found.** Check the batteries are powered and in range, and that
nothing else holds the gateway's single connection. Advertising is intermittent
— give discovery a minute.

**Entities unavailable after setup.** The cluster is advertising but the
connection has not opened. Usually something else is holding the single slot —
check the *Connection* switch is on and that the vendor app is closed. The
connection opens on the gateway's next advertisement, which can be five minutes
away.

**Only some batteries appear.** On a held connection every battery reports
within a few seconds, so this should resolve itself; if it does not, check
*Batteries expected* against *Batteries reported* and the frame reject rate.

Debug logging:

```yaml
logger:
  logs:
    custom_components.tensite_bms_ble: debug
    tensite_bms_ble: debug
```

## License

MIT
