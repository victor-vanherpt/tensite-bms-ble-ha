/**
 * Lovelace cards for Tensite battery banks.
 *
 *   tensite-cell-grid     one battery's cells as a 2x8 grid
 *   tensite-cluster-grid  every battery in a bank, side by side
 *
 * Both in one file so they cannot be installed at different versions, and
 * because the cluster card is little more than a layout around the other.
 *
 * tensite-cell-grid -- a 2x8 cell voltage grid for one battery.
 *
 * Why a card rather than a stack of templates: colouring sixteen cells by
 * their value needs either Jinja evaluated on the server, which re-renders on
 * every state change while the view is open, or something that runs in the
 * browser. The bank pushes a new reading every ~5 s, so the server-side route
 * would have Home Assistant rendering markdown several thousand times an hour
 * per open dashboard for a display that changes by a millivolt.
 *
 * So this writes *raw values* into CSS custom properties and lets CSS do the
 * arithmetic and the colour mixing. The only numbers JavaScript computes are
 * reciprocals of the (constant) threshold spans, because `calc()` division
 * with a variable divisor is the one part of this that browsers disagree
 * about -- multiplying by a precomputed reciprocal is equivalent and safe.
 *
 * Colours:
 *   - within the normal band, green shaded by where the cell sits between the
 *     pack's own lowest and highest cell, so imbalance is visible at all --
 *     an absolute scale would render a healthy pack as sixteen identical
 *     squares, since these cells live inside a 60 mV window
 *   - below normal_min, mixed toward blue; above normal_max, toward red
 *   - the extremes get a dashed border, yellow for the highest and blue for
 *     the lowest, leaving red to mean a cell outside its safe band and
 *     nothing else
 *
 * The tint is applied as a translucent wash over the card background rather
 * than an opaque fill, which keeps the theme's own text colour readable in
 * both light and dark themes without the card having to know which is active.
 *
 * Config:
 *
 *   type: custom:tensite-cell-grid
 *   device: Battery PA0 08146     # device name or id; everything is found on it
 *
 * Optional: title, columns (cells across, default 2 -- the pack is wired in
 * two strings of eight), normal_min, normal_max, critical_min, critical_max,
 * min_spread, scale, and explicit power/voltage/imbalance/status entity
 * overrides when auto-detection picks the wrong one.
 */

const DEFAULTS = {
  // LiFePO4. Deliberately not read from the BMS: the pack's own alarm
  // thresholds live in a settings frame this integration cannot read yet.
  normal_min: 3.0,
  normal_max: 3.45,
  critical_min: 2.5,
  critical_max: 3.65,
  // Floor on the spread used for green shading. Without it, a pack balanced
  // to within a millivolt of noise would show full contrast across the grid
  // and look alarming.
  min_spread: 0.02,
  // Cells across. Two by default because that is how the pack is physically
  // wired: sixteen cells in two strings of eight.
  columns: 2,
};

const STYLE = `
  :host { display: block; }
  ha-card { padding: 12px; }
  .bare { padding: 0; }

  .title {
    font-size: 0.95em;
    font-weight: 500;
    opacity: 0.8;
    padding: 0 2px 8px;
  }

  .grid {
    display: grid;
    grid-template-columns: repeat(var(--columns, 2), 1fr);
    gap: 6px;
    /* So the rules below can measure this grid rather than the viewport: four
       batteries side by side each get a quarter of the card, and a cell then
       has to fit in an eighth of it. */
    container-type: inline-size;

    /* Palette. Hues only -- how much of each is applied is decided per cell
       below, so these stay constant while the pack moves. */
    --c-ok: #43a047;
    --c-low: #1e88e5;
    --c-high: #e53935;
    /* Current direction and the pack's own extremes, kept apart from --c-high
       so red means one thing only: a cell outside its safe band. A pack's
       highest cell is not a fault, and colouring it like one taught the eye to
       ignore the colour that matters. */
    --c-charge: #f9a825;
    /* Idle. The theme's own muted colour, so it recedes in either scheme. */
    --c-idle: var(--disabled-text-color, #9e9e9e);
    --wash-min: 0.08;
    --wash-range: 0.5;
    --wash-max: 0.58; /* --wash-min + --wash-range */
  }

  .span { grid-column: 1 / -1; }

  .power {
    display: flex;
    justify-content: center;
    align-items: baseline;
    gap: 10px;
    padding: 6px 0 10px;
    font-size: 1.9em;
    font-weight: 500;
    letter-spacing: 0.02em;
  }
  .power .arrow { font-size: 0.8em; opacity: 0.75; }
  /* Blue out, yellow in: discharging lowers the pack voltage, charging
     raises it, matching the blue-low / yellow-high marking on the cells. */
  .power.charging .arrow { color: var(--c-charge); }
  .power.discharging .arrow { color: var(--c-low); }
  /* Idle is a state worth *noticing*, not an absence. A pack in a solar bank
     should be charging or discharging; sitting at zero while its siblings work
     is the anomaly you want to catch from across the room. So the dot is not
     shrunk or dimmed the way the arrows are -- it is full size and full
     opacity, and the only grey thing on the card. */
  .power.idle .arrow { color: var(--c-idle); font-size: 1em; opacity: 1; }

  .cell {
    min-width: 0;
    /* Everything below is derived from --v, the cell's raw voltage, plus the
       pack bounds and thresholds set on .grid. No JavaScript involved. */
    --bal: clamp(0, calc((var(--v) - var(--lo)) * var(--inv-spread)), 1);
    --under: clamp(0, calc((var(--n-lo) - var(--v)) * var(--inv-low)), 1);
    --over: clamp(0, calc((var(--v) - var(--n-hi)) * var(--inv-high)), 1);

    /* Green, pulled toward blue when low and red when high. */
    --tint: color-mix(in oklab,
              color-mix(in oklab, var(--c-ok), var(--c-low) calc(var(--under) * 100%)),
              var(--c-high) calc(var(--over) * 100%));

    /* How strongly the tint is washed in.
       In band: where the cell sits between the pack's lowest and highest.
       Out of band: severity, starting at the deepest in-band wash so that
       crossing a threshold only ever makes a cell *more* prominent.
       Taking max(balance, severity) instead looks right until you try it --
       a cell at 2.85 V is the pack minimum, so its balance term is 0, and it
       came out paler than its healthy neighbours. A fault must never be the
       quietest thing on the card.
       --is-fault is a step function: CSS has none, so multiply the severity
       up and clamp, which gives 0 for exactly zero and 1 for anything more. */
    --fault: max(var(--under), var(--over));
    --is-fault: min(1, calc(var(--fault) * 1000));
    --wash-band: calc(var(--wash-min) + var(--wash-range) * var(--bal));
    --wash-fault: calc(var(--wash-max) + (0.85 - var(--wash-max)) * var(--fault));
    --wash: calc(var(--is-fault) * var(--wash-fault) +
                 (1 - var(--is-fault)) * var(--wash-band));

    background: color-mix(in srgb, var(--tint) calc(var(--wash) * 100%), transparent);
    border: 2px solid transparent;
    border-radius: 8px;
    padding: 8px 10px;
    display: flex;
    align-items: baseline;
    justify-content: space-between;
    gap: 8px;
    min-height: 34px;
  }
  .cell .index {
    font-size: 0.78em;
    opacity: 0.6;
    font-variant-numeric: tabular-nums;
  }
  .cell .value {
    font-size: 1.05em;
    font-variant-numeric: tabular-nums;
    white-space: nowrap;
  }

  /* Squeezed: drop the padding first, then the cell number, then the size.
     The voltage is the thing worth keeping legible. */
  @container (max-width: 240px) {
    .cell { padding: 6px 7px; min-height: 30px; }
    .power { font-size: 1.5em; }
  }
  @container (max-width: 190px) {
    .cell .index { display: none; }
    .cell { justify-content: center; }
    .cell .value { font-size: 0.95em; }
    .power { font-size: 1.25em; gap: 6px; }
    .foot .label { font-size: 0.85em; }
  }
  .cell.highest { border-color: var(--c-charge); border-style: dashed; }
  .cell.lowest { border-color: var(--c-low); border-style: dashed; }
  .cell.stale { opacity: 0.35; }

  .foot {
    display: flex;
    justify-content: space-between;
    align-items: baseline;
    padding: 6px 4px 0;
    border-top: 1px solid var(--divider-color, rgba(127, 127, 127, 0.3));
    margin-top: 6px;
  }
  .foot + .foot { border-top: none; margin-top: 0; padding-top: 2px; }
  .foot .label { opacity: 0.7; font-size: 0.92em; }
  .foot .value { font-size: 1.15em; font-variant-numeric: tabular-nums; }

  .warning { padding: 16px; color: var(--warning-color, #ffa726); }
`;

class TensiteCellGrid extends HTMLElement {
  static getStubConfig(hass) {
    const device = Object.values(hass.devices || {}).find((d) =>
      (d.name_by_user || d.name || "").startsWith("Battery ")
    );
    return { device: device ? device.name_by_user || device.name : "" };
  }

  setConfig(config) {
    if (!config.device && !config.cells) {
      throw new Error("tensite-cell-grid: `device` is required");
    }
    this._config = { ...DEFAULTS, ...config };
    this._resolved = null;
    this._signature = null;
    this._root = null;
  }

  set hass(hass) {
    this._hass = hass;
    this._render();
  }

  getCardSize() {
    return Math.ceil(16 / this._config.columns / 2) + 3;
  }

  /**
   * Find every entity this card needs, once.
   *
   * Scoped to the device rather than matched on entity ids: the per-cell
   * entities are named `sensor.cell_01_voltage`, with `_2`/`_3`/`_4` appended
   * for the second, third and fourth battery, because cells used to be
   * devices of their own. Nothing in those ids says which battery they belong
   * to -- only the device does.
   */
  _resolve() {
    const hass = this._hass;
    const wanted = this._config.device;
    const device = Object.values(hass.devices || {}).find(
      (d) => d.id === wanted || d.name_by_user === wanted || d.name === wanted
    );
    if (!device) return { error: `No device named "${wanted}"` };

    const mine = Object.values(hass.entities || {}).filter(
      (e) => e.device_id === device.id
    );
    const cells = [];
    for (const entry of mine) {
      const match = entry.entity_id.match(/cell_(\d+)_voltage(_\d+)?$/);
      if (match) cells.push({ index: Number(match[1]), entity_id: entry.entity_id });
    }
    cells.sort((a, b) => a.index - b.index);
    if (!cells.length) return { error: `No cell voltage entities on "${wanted}"` };

    // Cells to shade *against*. By default every cell in the cluster, not just
    // this battery's: a per-battery scale makes each card use its full green
    // range, so a pack balanced to 3 mV looks exactly like one spread over
    // 60 mV and two cards side by side cannot be compared at all. Sharing one
    // scale is what makes a glance across the bank mean something.
    const siblings =
      this._config.scale === "battery" || !device.via_device_id
        ? [device]
        : Object.values(hass.devices).filter(
            (d) => d.via_device_id === device.via_device_id
          );
    const siblingIds = new Set(siblings.map((d) => d.id));
    const scale_cells = Object.values(hass.entities || {})
      .filter(
        (e) =>
          siblingIds.has(e.device_id) &&
          /cell_(\d+)_voltage(_\d+)?$/.test(e.entity_id)
      )
      .map((e) => e.entity_id);

    // Suffix matching is safe now that the search is confined to one device.
    const pick = (test) => {
      const found = mine.find((e) => e.entity_id.startsWith("sensor.") && test(e.entity_id));
      return found ? found.entity_id : null;
    };
    return {
      cells,
      scale_cells,
      power: this._config.power || pick((id) => id.endsWith("_power")),
      voltage:
        this._config.voltage ||
        pick((id) => id.endsWith("_voltage") && !id.includes("cell")),
      imbalance: this._config.imbalance || pick((id) => id.endsWith("_cell_imbalance")),
      status: this._config.status || pick((id) => id.endsWith("_status")),
      name: device.name_by_user || device.name,
    };
  }

  _build(resolved) {
    const root = this.attachShadow ? this.shadowRoot || this.attachShadow({ mode: "open" }) : this;
    const title = this._config.title
      ? `<div class="title">${this._config.title}</div>`
      : "";
    // Embedded means "no card chrome": the cluster card draws one ha-card and
    // puts several of these inside it, and nesting ha-cards gives every
    // battery its own border and shadow inside another border and shadow.
    root.innerHTML = this._config.embedded
      ? `<style>${STYLE}</style><div class="bare">${title}<div class="grid"></div></div>`
      : `<style>${STYLE}</style><ha-card>${title}<div class="grid"></div></ha-card>`;
    const grid = root.querySelector(".grid");

    const c = this._config;
    grid.style.setProperty(
      "--columns",
      Math.min(Math.max(1, Math.round(c.columns) || 1), resolved.cells.length)
    );
    grid.style.setProperty("--n-lo", c.normal_min);
    grid.style.setProperty("--n-hi", c.normal_max);
    // Reciprocals rather than the spans themselves: see the file header.
    grid.style.setProperty("--inv-low", 1 / (c.normal_min - c.critical_min));
    grid.style.setProperty("--inv-high", 1 / (c.critical_max - c.normal_max));

    const header = document.createElement("div");
    header.className = "span power";
    header.innerHTML =
      '<span class="arrow">▲</span><span class="kw">–</span><span class="arrow">▲</span>';
    grid.appendChild(header);

    const cells = resolved.cells.map(({ index }) => {
      const el = document.createElement("div");
      el.className = "cell";
      el.innerHTML = `<span class="index">${String(index).padStart(2, "0")}</span><span class="value">–</span>`;
      grid.appendChild(el);
      return el;
    });

    const foot = ["Pack voltage", "Imbalance"].map((label) => {
      const el = document.createElement("div");
      el.className = "span foot";
      el.innerHTML = `<span class="label">${label}</span><span class="value">–</span>`;
      grid.appendChild(el);
      return el;
    });

    this._root = { grid, header, cells, foot };
  }

  _render() {
    if (!this._hass || !this._config) return;
    if (!this._resolved) {
      this._resolved = this._resolve();
      if (this._resolved.error) {
        const root = this.attachShadow ? this.shadowRoot || this.attachShadow({ mode: "open" }) : this;
        root.innerHTML = `<ha-card><div style="padding:16px">${this._resolved.error}</div></ha-card>`;
        // Retry on the next state change: the device may not be loaded yet.
        this._resolved = null;
        return;
      }
      this._build(this._resolved);
    }

    const state = (id) => (id && this._hass.states[id] ? this._hass.states[id].state : null);
    const r = this._resolved;

    // The hass setter fires on *every* state change in the system, not just
    // ours. Touching the DOM each time would mean thousands of pointless
    // writes an hour, so compare first and bail when nothing we show moved.
    const values = r.cells.map((c) => state(c.entity_id));
    const scaleValues = r.scale_cells.map(state);
    const signature = [
      ...values,
      // Included because the shading scale is the cluster's: a cell moving on
      // another battery changes what the colours here mean.
      ...scaleValues,
      state(r.power),
      state(r.voltage),
      state(r.imbalance),
      state(r.status),
    ].join("|");
    if (signature === this._signature) return;
    this._signature = signature;

    this._paintCells(values, scaleValues);
    this._paintHeader(state(r.power), state(r.status));
    this._paintFoot(state(r.voltage), state(r.imbalance));
  }

  _paintCells(values, scaleValues) {
    const numbers = values.map(Number).filter((v) => Number.isFinite(v));
    const scaleNumbers = scaleValues.map(Number).filter((v) => Number.isFinite(v));
    // The green ramp describes the *healthy* population, so cells outside the
    // normal band are left out of its bounds. Including them lets a single
    // failing cell stretch the scale so far that the remaining fifteen
    // collapse into one indistinguishable shade -- exactly when reading the
    // balance of the rest matters most. Out-of-band cells are coloured by
    // severity instead and need no place on this scale.
    const inBand = scaleNumbers.filter(
      (v) => v >= this._config.normal_min && v <= this._config.normal_max
    );
    const scale = inBand.length ? inBand : scaleNumbers.length ? scaleNumbers : numbers;
    const scaleLo = scale.length ? Math.min(...scale) : 0;
    const scaleHi = scale.length ? Math.max(...scale) : 0;
    const spread = Math.max(scaleHi - scaleLo, this._config.min_spread);

    this._root.grid.style.setProperty("--lo", scaleLo);
    this._root.grid.style.setProperty("--inv-spread", 1 / spread);

    // The dashed borders mark the extremes of the *whole* pack, not of the
    // shading scale: a cell far enough out of band to be excluded from the
    // ramp is precisely the one worth pointing at.
    const packLo = numbers.length ? Math.min(...numbers) : 0;
    const packHi = numbers.length ? Math.max(...numbers) : 0;
    // Only mark them once the pack is measurably unbalanced -- with every cell
    // equal, every cell is both the highest and the lowest.
    const marked = packHi - packLo >= 0.001;

    this._root.cells.forEach((el, i) => {
      const raw = values[i];
      const value = Number(raw);
      const ok = Number.isFinite(value);
      el.classList.toggle("stale", !ok);
      el.classList.toggle("highest", ok && marked && value === packHi);
      el.classList.toggle("lowest", ok && marked && value === packLo);
      // The only thing handed to CSS: the reading itself.
      el.style.setProperty("--v", ok ? value : scaleLo);
      el.querySelector(".value").textContent = ok ? `${value.toFixed(3)} V` : "–";
    });
  }

  _paintHeader(power, status) {
    const watts = Number(power);
    const kw = Number.isFinite(watts) ? Math.abs(watts) / 1000 : null;
    // Follows the BMS's own charging state rather than the sign of the power:
    // it applies a 0.3 A deadband, so a pack trickling at 0.1 A reads idle
    // instead of flickering between charging and discharging. The power sign
    // is only a fallback for when that sensor is missing -- current is
    // positive out of the pack, so positive power is discharging.
    const mode = ["charging", "discharging", "idle"].includes(status)
      ? status
      : !Number.isFinite(watts) || Math.abs(watts) < 1
        ? "idle"
        : watts > 0
          ? "discharging"
          : "charging";
    const arrow = mode === "discharging" ? "▲" : mode === "charging" ? "▼" : "●";
    this._root.header.className = `span power ${mode}`;
    for (const el of this._root.header.querySelectorAll(".arrow")) el.textContent = arrow;
    this._root.header.querySelector(".kw").textContent =
      kw === null ? "–" : `${kw.toFixed(2)} kW`;
  }

  _paintFoot(voltage, imbalance) {
    const volts = Number(voltage);
    const mv = Number(imbalance);
    this._root.foot[0].querySelector(".value").textContent = Number.isFinite(volts)
      ? `${volts.toFixed(2)} V`
      : "–";
    this._root.foot[1].querySelector(".value").textContent = Number.isFinite(mv)
      ? `${Math.round(mv)} mV`
      : "–";
  }
}

/**
 * tensite-cluster-grid -- every battery in one bank, side by side.
 *
 * Builds a cell grid per battery rather than reimplementing one. The children
 * each derive their own colour scale from the whole cluster, so nothing has to
 * be passed between them and a standalone grid on another view shades
 * identically.
 *
 *   type: custom:tensite-cluster-grid
 *   device: TS-L5000-8146      # the cluster; optional if there is only one
 *   columns: 4                 # batteries across; default is all of them
 *   cell_columns: 2            # passed to each grid as its `columns`
 *
 * `columns` is a maximum, not a fixed count: the batteries fill the width they
 * are given and wrap when there is not enough for another, down to a single
 * column on a phone. Note that in a dashboard *sections* view a card is
 * confined to one section column unless it is given `grid_options: {columns:
 * full}`, and four batteries in 430 px will wrap however this card is
 * configured.
 */
const CLUSTER_STYLE = `
  :host { display: block; }
  ha-card { padding: 12px; }
  .title {
    font-size: 1.05em;
    font-weight: 500;
    padding: 2px 2px 10px;
  }
  .banks {
    display: grid;
    /* Set from JavaScript, which knows how many batteries there are. See
       _columnRule: as many side by side as fit, never more than the bank
       holds, down to one on a phone. */
    gap: 16px;
  }
  .warning { padding: 16px; }
`;

//: Narrower than this and a battery is not worth drawing, so it wraps instead.
//: Two 2-column cell grids fit either side of it on a phone in landscape.
const BANK_MIN_WIDTH = 150;
const BANK_GAP = 16;

class TensiteClusterGrid extends HTMLElement {
  setConfig(config) {
    this._config = { ...config };
    this._children = null;
    this._resolved = null;
  }

  /**
   * Lay out *count* batteries across, responsively.
   *
   * `repeat(auto-fit, minmax(150px, 1fr))` alone wraps as soon as the card is
   * narrower than 150px per battery, which in a dashboard *section* -- about
   * 430px wide -- means four batteries come out two by two with the rest of
   * the screen empty. Raising the floor to a share of the container instead
   * caps the count: at 100%/4 no fifth column can ever fit, and when the card
   * is too narrow for four at 150px each it drops to three, then two, then
   * one. So it fills the width it is given and still stacks on a phone.
   */
  _columnRule(count) {
    const share = `calc((100% - ${(count - 1) * BANK_GAP}px) / ${count})`;
    return `repeat(auto-fit, minmax(max(${BANK_MIN_WIDTH}px, ${share}), 1fr))`;
  }

  set hass(hass) {
    this._hass = hass;
    this._render();
  }

  getCardSize() {
    return 8;
  }

  /** The cluster device and its batteries, in bank order. */
  _resolve() {
    const devices = Object.values(this._hass.devices || {});
    const entities = Object.values(this._hass.entities || {});

    const hasCells = new Set(
      entities
        .filter((e) => /cell_\d+_voltage(_\d+)?$/.test(e.entity_id))
        .map((e) => e.device_id)
    );
    const batteries = devices.filter((d) => hasCells.has(d.id));

    let cluster;
    if (this._config.device) {
      const wanted = this._config.device;
      cluster = devices.find(
        (d) => d.id === wanted || d.name_by_user === wanted || d.name === wanted
      );
      if (!cluster) return { error: `No device named "${wanted}"` };
    } else {
      // Identified by being the gateway a battery hangs off, rather than by
      // its model string: one fact about the device tree beats a name that
      // could be translated or renamed.
      const clusterIds = [...new Set(batteries.map((d) => d.via_device_id))].filter(
        Boolean
      );
      if (clusterIds.length !== 1) {
        return {
          error: clusterIds.length
            ? "More than one cluster here -- name one with `device:`"
            : "No battery cluster found",
        };
      }
      cluster = devices.find((d) => d.id === clusterIds[0]);
    }

    const mine = batteries.filter((d) => d.via_device_id === cluster.id);
    if (!mine.length) return { error: `No batteries on "${this._config.device}"` };

    // Master first, then by name. Its position label is PA0 -- cluster A,
    // position 0 -- and it is the one relaying for the rest, so it is the one
    // to look at first.
    const name = (d) => d.name_by_user || d.name || "";
    mine.sort((a, b) => {
      const master = (d) => (/\bPA\d/.test(name(d)) ? 0 : 1);
      return master(a) - master(b) || name(a).localeCompare(name(b));
    });
    return { cluster, batteries: mine };
  }

  _render() {
    if (!this._hass || !this._config) return;

    if (!this._children) {
      const resolved = this._resolve();
      const root = this.attachShadow
        ? this.shadowRoot || this.attachShadow({ mode: "open" })
        : this;
      if (resolved.error) {
        root.innerHTML = `<ha-card><div style="padding:16px">${resolved.error}</div></ha-card>`;
        return; // Retried on the next state change; devices may still be loading.
      }
      this._resolved = resolved;

      const title =
        this._config.title === false
          ? ""
          : `<div class="title">${
              this._config.title ||
              resolved.cluster.name_by_user ||
              resolved.cluster.name
            }</div>`;
      root.innerHTML = `<style>${CLUSTER_STYLE}</style><ha-card>${title}<div class="banks"></div></ha-card>`;
      const banks = root.querySelector(".banks");

      // `columns` here means batteries across, never more than there are.
      const across = Math.min(
        Math.max(1, Math.round(this._config.columns) || resolved.batteries.length),
        resolved.batteries.length
      );
      banks.style.setProperty("grid-template-columns", this._columnRule(across));

      this._children = resolved.batteries.map((device) => {
        const card = document.createElement("tensite-cell-grid");
        // Same options the user set here, so thresholds configured once on the
        // cluster apply to every battery in it -- except the two that mean
        // different things at each level. `columns` is batteries across here
        // and cells across there, so it is dropped and `cell_columns` carries
        // the intent instead.
        const child = { ...this._config, device: device.id, embedded: true };
        delete child.type;
        delete child.columns;
        delete child.cell_columns;
        if (this._config.cell_columns) child.columns = this._config.cell_columns;
        card.setConfig({
          ...child,
          title: device.name_by_user || device.name,
        });
        banks.appendChild(card);
        return card;
      });
    }

    // Children hold their own signature check, so this stays cheap.
    for (const card of this._children) card.hass = this._hass;
  }
}

// Guarded: the integration serves this itself now, but an installation that
// still has the old /local/tensite-cell-grid.js resource would load it twice,
// and a second customElements.define throws and takes the dashboard with it.
if (!customElements.get("tensite-cell-grid")) {
  customElements.define("tensite-cell-grid", TensiteCellGrid);
  customElements.define("tensite-cluster-grid", TensiteClusterGrid);
  window.customCards = window.customCards || [];
  window.customCards.push(
    {
      type: "tensite-cell-grid",
      name: "Tensite cell grid",
      description: "Per-cell voltages for one battery, shaded by balance.",
    },
    {
      type: "tensite-cluster-grid",
      name: "Tensite cluster grid",
      description: "Every battery in a bank, on one shared colour scale.",
    }
  );
}
