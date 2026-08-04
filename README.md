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

## Cell grid cards

Two cards ship with the integration: `tensite-cell-grid` shows one battery's
sixteen cells as a 2x8 grid, and `tensite-cluster-grid` puts every battery in a
bank side by side on one shared colour scale.

```yaml
type: custom:tensite-cluster-grid
device: TS-L5000-8146       # the cluster; omit it if you only have one
grid_options:
  columns: full             # see below -- without this they will wrap
```

The cluster card builds a cell grid per battery rather than reimplementing one,
so everything below applies to both. Batteries are laid out side by side,
master first -- position `PA0`, the one relaying for the rest.

### Width

**In a *sections* view, give the card `grid_options: {columns: full}`.** A card
there is confined to one section column, about 430 px, and four batteries in
430 px wrap two by two with the rest of the screen empty however the card is
configured. The generated `dashboard.yaml` sets this already.

Given room, batteries fill the width and wrap only when there is not enough for
another, down to a single column on a phone. `columns:` caps how many sit side
by side; the default is all of them:

| Option | Meaning | Default |
|---|---|---|
| `columns` on the cluster card | batteries across, as a maximum | all of them |
| `cell_columns` on the cluster card | passed to each grid as its `columns` | 2 |
| `columns` on a cell grid | cells across | 2, the two strings of eight the pack is wired in |

The two `columns` mean different things, so the cluster card does not pass its
own down -- `cell_columns` is how you reach the grids from up there.

Cells shrink gracefully when squeezed: padding goes first, then the cell
number, then the type size. The voltage stays legible longest, because it is
the thing worth reading.

Cells are shaded by where each sits relative to the rest of the bank, with each
battery's own highest and lowest ringed in dashed yellow and blue.

```
        ▲   1.24 kW   ▲          ← up/blue discharging, down/yellow charging
  ┌───────────┬───────────┐
  │ 01  3.282 │ 02  3.290 │      ← green shaded by position within the pack
  │ 03  3.288 │ 04  3.288 │
  │ …         │ …         │
  │ 13  3.279 │ 14  3.289 │      ← 13 is the lowest: blue dashed border
  │ 15  3.285 │ 16  3.280 │
  └───────────┴───────────┘
  Pack voltage        52.59 V
  Imbalance              11 mV
```

It runs entirely in the browser. Colouring sixteen cells by value needs either
Jinja evaluated on the server -- which re-renders on every state change while
the view is open, several thousand times an hour now that readings arrive every
5 s -- or something client-side. The card writes each cell's *raw voltage* into
a CSS custom property and lets CSS do the arithmetic and the colour mixing;
JavaScript computes only the pack bounds and a couple of reciprocals, because
`calc()` division with a variable divisor is the one part browsers disagree
about.

### Install

Nothing to install. The card ships inside the integration, which serves it at
`/tensite_bms_ble/tensite-cell-grid.js` and loads it into the frontend itself,
so there is no file to copy into `/config/www` and no Lovelace resource to add.
The two are versioned together, which is the point: a card that expects a
sensor added last week cannot end up installed beside an integration that does
not have it.

Just add it to a dashboard:

```yaml
type: custom:tensite-cell-grid
device: Battery PA0 08146
```

`device` is the only required option -- everything else is found on that
device, and finding by device rather than by name is what makes the card
immune to entity ids changing under it.

Installations from before 2026-08 have per-cell entities named
`sensor.cell_01_voltage`, with `_2`/`_3`/`_4` appended for the second, third
and fourth battery: cells used to be devices of their own, and Home Assistant
derives an entity id from the device name only at creation. Those ids say
nothing about which battery they belong to, and the numeric suffix is assigned
in whatever order the batteries first reported, so it does not survive the
integration being removed and re-added. Renaming them to
`sensor.battery_pa0_08146_cell_01_voltage` is worth doing; move the recorder's
`states_meta.entity_id` and `statistics_meta.statistic_id` rows at the same
time and ten days of history follows the entity instead of being stranded
under the old name.

### Colours

| Where the cell sits | Colour |
|---|---|
| Within the normal band | green, palest at the pack's lowest cell, deepest at its highest |
| Below `normal_min` (3.0 V) | blue, deepening toward `critical_min` (2.5 V) |
| Above `normal_max` (3.45 V) | red, deepening toward `critical_max` (3.65 V) |

The green ramp is scaled to the **bank's** spread, not to the absolute band and
not to each battery separately.

Not absolute, because these cells live inside a 60 mV window: an absolute scale
would render a healthy pack as sixteen identical squares and show nothing. Not
per battery, because then every card would use its full green range and a pack
balanced to 2 mV would look exactly like one spread over 150 mV -- glancing
between them would say nothing. Sharing one scale is what makes the comparison
mean something, and it needs no wiring between the cards: each one works out
the same bounds from its own cluster, so a standalone grid on a battery page
shades identically to the same battery inside the cluster card. Set
`scale: battery` to opt a card out.

A floor of 20 mV (`min_spread`) stops a bank balanced to within noise from
being drawn at full contrast.

Cells outside the normal band are excluded from that ramp and coloured by
severity instead. Both parts of that are deliberate: including a failed cell in
the bounds lets it stretch the scale until the healthy fifteen collapse into
one shade, and scoring a fault by its position in the pack made it *paler* than
its neighbours -- the pack minimum has, by definition, nothing below it.

The thresholds are defaults for LiFePO4, not values read from the BMS: the
pack's own alarm setpoints live in a settings frame this integration cannot
read yet. Override them per card if yours differ:

```yaml
type: custom:tensite-cell-grid
device: Battery PA0 08146
normal_min: 3.0
normal_max: 3.45
critical_min: 2.5
critical_max: 3.65
min_spread: 0.02
```

The dashed borders stay per battery even though the colours are shared: each
card points at its own weakest and strongest cell, which is what you want when
looking at one pack, while the shading is what you compare across packs.

Blue means down and yellow means up, throughout: blue rings the lowest cell and
tints the arrows while discharging, yellow rings the highest and tints them
while charging. **Red is reserved for a cell outside its safe band.** It used to
ring the highest cell and colour the discharging arrows too, which meant a
perfectly healthy pack showed red somewhere at all times -- and a colour that is
always present is a colour the eye stops reading.

The header shows the pack's power in kW, flanked by a direction marker: a blue
▲ while discharging, a yellow ▼ while charging, and a grey ● at rest.

The dot is drawn full size and full opacity while the arrows are shrunk and
dimmed, which is the opposite of what "resting" would suggest -- deliberately.
A pack in a solar bank should be charging or discharging; one sitting at zero
while its siblings work is the anomaly worth catching from across the room, and
grey is then the only unusual colour on the card.

The state follows the BMS rather than the sign of the current, which matters
because it applies a 0.3 A deadband -- a pack trickling at 0.1 A reads idle
instead of flickering between charging and discharging. The footer
rows carry pack voltage and cell imbalance.

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
