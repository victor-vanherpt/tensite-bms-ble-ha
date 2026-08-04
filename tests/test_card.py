"""The Lovelace cards, driven in a stubbed DOM.

A card that throws while evaluating is silent from every angle the rest of this
suite can see: the integration registers it, Home Assistant serves it, nothing
is logged, and the dashboard says only "Custom element doesn't exist". Running
the file is the only way to catch it -- and the same harness can drive the
parts that would otherwise need a browser to check: which batteries the cluster
card fans out to, and what colour scale a grid ends up using.

Node is not a dependency of this project, so this skips without it rather than
forcing a JavaScript toolchain on a Python test suite.
"""

from __future__ import annotations

import json
import pathlib
import shutil
import subprocess

import pytest

from custom_components.tensite_bms_ble.const import CARD_FILENAME

CARD = pathlib.Path("custom_components/tensite_bms_ble/www") / CARD_FILENAME

#: Just enough browser for the cards to run: elements that record the CSS
#: custom properties set on them, and a custom element registry that hands back
#: the classes so they can be instantiated.
HARNESS = """
import { readFileSync } from "node:fs";
import vm from "node:vm";

const defined = {};
const madeCells = [];
const elementStub = () => {
  const el = {
    props: {},
    style: { setProperty: (k, v) => (el.props[k] = v) },
    classList: { _s: new Set(), toggle(c, on) { on ? this._s.add(c) : this._s.delete(c); } },
    querySelector: () => elementStub(),
    querySelectorAll: () => [],
    appendChild() {},
    innerHTML: "", textContent: "",
  };
  return el;
};
const sandbox = {
  HTMLElement: class {},
  customElements: { get: (n) => defined[n], define: (n, c) => (defined[n] = c) },
  document: {
    createElement(tag) {
      const el = elementStub();
      if (tag === "tensite-cell-grid") {
        el.setConfig = (c) => (el.config = c);
        madeCells.push(el);
      }
      return el;
    },
  },
  console,
};
sandbox.window = sandbox;
vm.runInNewContext(readFileSync(process.argv[2], "utf8"), sandbox, { filename: "cards.js" });

// A bank of two batteries -- one balanced to 2 mV, one spread over 150 mV --
// plus a second cluster that must not be swallowed by the first.
const devices = { c1: { id: "c1", name: "Cluster One" }, c2: { id: "c2", name: "Cluster Two" } };
const entities = {}, states = {};
const packs = { tight: ["c1", 3.300, 3.302], wide: ["c1", 3.250, 3.400], other: ["c2", 3.3, 3.3] };
for (const [id, [cluster, lo, hi]] of Object.entries(packs)) {
  devices[id] = { id, name: id === "tight" ? "Battery PA0 tight" : `Battery P01 ${id}`,
                  via_device_id: cluster };
  for (let i = 1; i <= 16; i++) {
    const eid = `sensor.${id}_cell_${String(i).padStart(2, "0")}_voltage`;
    entities[eid] = { entity_id: eid, device_id: id };
    states[eid] = { state: String(lo + ((hi - lo) * (i - 1)) / 15) };
  }
}
const hass = { devices, entities, states };

function mount(tag, config) {
  const el = new defined[tag]();
  const grid = elementStub();
  el.attachShadow = () => ({ innerHTML: "", querySelector: () => grid });
  el.setConfig(config);
  el.hass = hass;
  return { el, grid };
}

const tight = mount("tensite-cell-grid", { device: "Battery PA0 tight" });
const wide = mount("tensite-cell-grid", { device: "Battery P01 wide" });
const own = mount("tensite-cell-grid", { device: "Battery PA0 tight", scale: "battery" });
madeCells.length = 0;
mount("tensite-cluster-grid", { device: "Cluster One" });

const scale = (m) => ({ lo: Number(m.grid.props["--lo"]),
                        spread: Number((1 / Number(m.grid.props["--inv-spread"])).toFixed(4)),
                        cells: m.el._resolved.scale_cells.length });

console.log(JSON.stringify({
  defined: Object.keys(defined),
  cards: (sandbox.window.customCards || []).map((c) => c.type),
  tight: scale(tight),
  wide: scale(wide),
  own_scale: scale(own),
  cluster_children: madeCells.map((c) => ({ device: c.config.device,
                                            title: c.config.title,
                                            embedded: c.config.embedded })),
  cluster_got_hass: madeCells.every((c) => c.hass === hass),
}));
"""


@pytest.fixture(scope="module")
def run(tmp_path_factory):
    if not shutil.which("node"):
        pytest.skip("node not installed")
    harness = tmp_path_factory.mktemp("cards") / "harness.mjs"
    harness.write_text(HARNESS)
    result = subprocess.run(
        ["node", str(harness), str(CARD)], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, f"cards threw while loading:\n{result.stderr}"
    return json.loads(result.stdout)


class TestLoading:
    def test_the_cards_are_packaged(self):
        """A file outside custom_components/ would not be shipped by HACS."""
        assert CARD.is_file()

    def test_both_elements_are_defined(self, run):
        assert set(run["defined"]) == {"tensite-cell-grid", "tensite-cluster-grid"}

    def test_both_appear_in_the_card_picker(self, run):
        assert set(run["cards"]) == {"tensite-cell-grid", "tensite-cluster-grid"}

    def test_defining_twice_does_not_throw(self):
        """An installation that still has the old /local resource loads the
        file twice, and an unguarded second define takes the dashboard down."""
        assert 'customElements.get("tensite-cell-grid")' in CARD.read_text()


class TestSharedScale:
    """Colours have to mean the same thing on every battery.

    With a per-battery scale each card uses its full green range, so a pack
    balanced to 2 mV looks exactly like one spread over 150 mV and glancing
    between them says nothing.
    """

    def test_every_battery_in_a_bank_shades_alike(self, run):
        assert run["tight"]["lo"] == run["wide"]["lo"]
        assert run["tight"]["spread"] == run["wide"]["spread"]

    def test_the_scale_covers_the_whole_bank(self, run):
        """32 cells across the two batteries of cluster one -- and not the 16
        belonging to the other cluster."""
        assert run["tight"]["cells"] == 32
        assert run["tight"]["spread"] == pytest.approx(0.15, abs=0.001)

    def test_a_battery_can_opt_out(self, run):
        """`scale: battery`, for a card that is on its own anyway."""
        assert run["own_scale"]["cells"] == 16
        assert run["own_scale"]["lo"] == 3.3


class TestClusterCard:
    def test_it_builds_one_grid_per_battery(self, run):
        assert len(run["cluster_children"]) == 2

    def test_it_leaves_other_clusters_alone(self, run):
        assert all("other" not in c["device"] for c in run["cluster_children"])

    def test_the_master_comes_first(self, run):
        """PA0 is cluster A position 0 -- the one relaying for the rest."""
        assert "PA0" in run["cluster_children"][0]["title"]

    def test_children_are_embedded(self, run):
        """Nested ha-cards would give every battery its own border and shadow
        inside another border and shadow."""
        assert all(c["embedded"] for c in run["cluster_children"])

    def test_children_receive_hass(self, run):
        assert run["cluster_got_hass"] is True
