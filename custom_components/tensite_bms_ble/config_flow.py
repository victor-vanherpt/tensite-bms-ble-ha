"""Config flow for Tensite BMS (BLE).

Batteries advertise manufacturer ID 0xE502 carrying ASCII "UHOME", which the
manifest matches on, so clusters are normally offered automatically rather than
typed in by hand.
"""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from homeassistant.components.bluetooth import (
    BluetoothServiceInfoBleak,
    async_ble_device_from_address,
    async_discovered_service_info,
)
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import callback
from homeassistant.helpers import config_validation as cv
from tensite_bms_ble import (
    SERIAL_MARKER,
    TensiteClusterClient,
    TensiteError,
    TensiteNoDataError,
    is_tensite_advertisement,
)

from .const import (
    CONF_ADDRESS,
    CONF_EXPECTED_BATTERIES,
    CONF_HIDE_SENTINEL_TEMPERATURES,
    CONF_MEMBER_SERIALS,
    CONF_SCAN_INTERVAL,
    CONF_SERIAL,
    CONNECT_TIMEOUT,
    DEFAULT_EXPECTED_BATTERIES,
    DEFAULT_HIDE_SENTINEL_TEMPERATURES,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    MAX_SCAN_INTERVAL,
    MIN_SCAN_INTERVAL,
    PROBE_TIMEOUT,
)

_LOGGER = logging.getLogger(__name__)


def _serial_of(info: BluetoothServiceInfoBleak) -> str | None:
    """Serial from the advertised local name, if it carries one.

    Only the advertisement is trusted here. ``BLEDevice.name`` can be a cached
    GATT Device Name, which for these units is the unhelpful string "ESP32".
    """
    name = info.advertisement.local_name or info.name or ""
    return name if SERIAL_MARKER in name else None


def _title(serial: str | None, address: str) -> str:
    return f"Tensite cluster {serial[-5:]}" if serial else f"Tensite cluster {address}"


class TensiteConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for a Tensite battery cluster."""

    VERSION = 1

    def __init__(self) -> None:
        self._discovered: dict[str, str | None] = {}
        self._address: str | None = None
        self._serial: str | None = None
        self._members: list[str] = []
        self._master_serial: str | None = None
        self._expected: int = 0

    async def async_step_bluetooth(
        self, discovery_info: BluetoothServiceInfoBleak
    ) -> ConfigFlowResult:
        """Handle a cluster discovered over Bluetooth."""
        await self.async_set_unique_id(discovery_info.address)
        self._abort_if_unique_id_configured()

        if not discovery_info.connectable:
            # Only advertisements reached us; we need an actual connection.
            return self.async_abort(reason="not_connectable")

        self._address = discovery_info.address
        self._serial = _serial_of(discovery_info)
        self.context["title_placeholders"] = {
            "name": _title(self._serial, self._address)
        }
        return await self.async_step_confirm()

    async def async_step_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Confirm adding a discovered cluster, after checking it relays data."""
        assert self._address is not None
        errors: dict[str, str] = {}
        if user_input is not None:
            error = await self._async_probe()
            if error is None:
                return await self.async_step_battery_count()
            errors["base"] = error

        self._set_confirm_only()
        return self.async_show_form(
            step_id="confirm",
            errors=errors,
            description_placeholders={
                "name": _title(self._serial, self._address),
                "address": self._address,
            },
        )

    async def async_step_battery_count(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Confirm how many batteries this cluster contains.

        Nothing in the protocol states the bank size -- it can only be inferred
        from how many batteries happen to answer, and the gateway round-robins
        them, so a single poll can easily undercount. Pinning it here lets each
        poll stop as soon as they have all reported instead of always waiting
        out the listening window, and stops an undercount becoming permanent.
        """
        found = len(self._members)
        if user_input is not None:
            self._expected = user_input[CONF_EXPECTED_BATTERIES]
            return self._create_entry()

        return self.async_show_form(
            step_id="battery_count",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_EXPECTED_BATTERIES, default=found or 1
                    ): vol.All(cv.positive_int, vol.Range(min=1, max=32))
                }
            ),
            description_placeholders={
                "found": str(found),
                "serials": ", ".join(s[-5:] for s in self._members) or "none",
            },
        )

    async def _async_probe(self) -> str | None:
        """Check the candidate actually reports batteries. None means good.

        Whether a battery relays the bank cannot be told from its advertisement
        -- every unit advertises identically -- so the only way to know is to
        connect and look. Doing that for every discovery would be slow and
        would fight over the gateway's single connection slot, so it happens
        here, once, for the one the user chose.

        A unit that reports only itself is still perfectly valid; one battery
        is enough to pass.
        """
        assert self._address is not None
        device = async_ble_device_from_address(
            self.hass, self._address, connectable=True
        )
        if device is None:
            return "cannot_connect"

        client = TensiteClusterClient(
            device,
            serial=self._serial,
            connect_timeout=CONNECT_TIMEOUT,
            listen_timeout=PROBE_TIMEOUT,
            logger=_LOGGER,
        )
        try:
            reading = await client.async_read(expect=1)
        except TensiteNoDataError:
            _LOGGER.warning(
                "%s: connected but reported no batteries", self._address
            )
            return "no_batteries"
        except TensiteError as err:
            _LOGGER.warning("%s: probe failed: %s", self._address, err)
            return "cannot_connect"
        except Exception:  # noqa: BLE001 -- a config flow must not raise
            _LOGGER.exception("%s: unexpected error probing", self._address)
            return "cannot_connect"

        self._members = sorted(reading.batteries)
        if reading.master_serial:
            self._master_serial = reading.master_serial
        _LOGGER.info(
            "%s: reports %d batteries (%s)",
            self._address,
            reading.battery_count,
            ", ".join(self._members),
        )
        return None

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Pick a battery to connect through, from those currently visible."""
        errors: dict[str, str] = {}
        if user_input is not None:
            self._address = user_input[CONF_ADDRESS]
            self._serial = self._discovered.get(self._address)
            await self.async_set_unique_id(self._address, raise_on_progress=False)
            self._abort_if_unique_id_configured()
            error = await self._async_probe()
            if error is None:
                return await self.async_step_battery_count()
            errors["base"] = error

        current = self._async_current_ids()
        for info in async_discovered_service_info(self.hass, connectable=True):
            if info.address in current or not is_tensite_advertisement(
                info.advertisement
            ):
                continue
            self._discovered[info.address] = _serial_of(info)

        if not self._discovered:
            return self.async_abort(reason="no_devices_found")

        return self.async_show_form(
            step_id="user",
            errors=errors,
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_ADDRESS): vol.In(
                        {
                            address: _title(serial, address)
                            for address, serial in self._discovered.items()
                        }
                    )
                }
            ),
        )

    def _create_entry(self) -> ConfigFlowResult:
        assert self._address is not None
        return self.async_create_entry(
            title=_title(self._serial, self._address),
            data={
                CONF_ADDRESS: self._address,
                # The serial we connect *through*, which is not necessarily the
                # bank master -- connecting via a specific battery is a valid
                # thing to want, e.g. when debugging one unit.
                CONF_SERIAL: self._serial,
                CONF_MEMBER_SERIALS: self._members,
            },
            options={CONF_EXPECTED_BATTERIES: self._expected},
        )

    @staticmethod
    @callback
    def async_get_options_flow(entry: ConfigEntry) -> TensiteOptionsFlow:
        return TensiteOptionsFlow()


class TensiteOptionsFlow(OptionsFlow):
    """Tune polling behaviour after setup."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(data=user_input)

        options = self.config_entry.options
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Optional(
                        CONF_SCAN_INTERVAL,
                        default=options.get(
                            CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL
                        ),
                    ): vol.All(
                        cv.positive_int,
                        vol.Range(min=MIN_SCAN_INTERVAL, max=MAX_SCAN_INTERVAL),
                    ),
                    vol.Optional(
                        CONF_HIDE_SENTINEL_TEMPERATURES,
                        default=options.get(
                            CONF_HIDE_SENTINEL_TEMPERATURES,
                            DEFAULT_HIDE_SENTINEL_TEMPERATURES,
                        ),
                    ): cv.boolean,
                    vol.Optional(
                        CONF_EXPECTED_BATTERIES,
                        default=options.get(
                            CONF_EXPECTED_BATTERIES, DEFAULT_EXPECTED_BATTERIES
                        ),
                    ): vol.All(cv.positive_int, vol.Range(min=0, max=32)),
                }
            ),
        )
