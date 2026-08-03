"""The Lovelace card actually loads.

A card that throws while evaluating is silent from every angle the rest of
this suite can see: the integration registers it, Home Assistant serves it,
nothing is logged, and the dashboard says only "Custom element doesn't exist".
The one way to catch it is to run the file.

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

#: Just enough browser for the module to reach its last line.
HARNESS = """
import { readFileSync } from "node:fs";
import vm from "node:vm";
const defined = {};
const sandbox = {
  HTMLElement: class {},
  customElements: { get: (n) => defined[n], define: (n, c) => (defined[n] = c) },
  console,
};
sandbox.window = sandbox;
vm.runInNewContext(readFileSync(process.argv[2], "utf8"), sandbox, {
  filename: "card.js",
});
console.log(JSON.stringify({
  defined: Object.keys(defined),
  cards: (sandbox.window.customCards || []).map((c) => c.type),
}));
"""


@pytest.fixture(scope="module")
def loaded(tmp_path_factory):
    if not shutil.which("node"):
        pytest.skip("node not installed")
    harness = tmp_path_factory.mktemp("card") / "harness.mjs"
    harness.write_text(HARNESS)
    result = subprocess.run(
        ["node", str(harness), str(CARD)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, f"card threw while loading:\n{result.stderr}"
    return json.loads(result.stdout)


def test_it_defines_its_element(loaded):
    """The name a dashboard refers to as `custom:tensite-cell-grid`."""
    assert loaded["defined"] == ["tensite-cell-grid"]


def test_it_registers_in_the_card_picker(loaded):
    assert "tensite-cell-grid" in loaded["cards"]


def test_defining_twice_does_not_throw():
    """An installation that still has the old /local resource loads it twice,
    and an unguarded second define takes the whole dashboard down."""
    assert 'customElements.get("tensite-cell-grid")' in CARD.read_text()
