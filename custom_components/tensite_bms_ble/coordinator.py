"""Cluster coordinator.

One coordinator per *cluster*, not per battery. This is forced by the hardware:
the ESP32 gateway accepts a single BLE central at a time, and connecting to the
cluster master relays frames for every battery in the bank. A coordinator per
battery would have them competing for the one connection slot.

**The connection is held open, not made per poll.** The gateway streams
unprompted once notifications are enabled, and it streams for the whole bank at
once: in a 182-second capture of the vendor app all four batteries emitted cell
frames every ~5.1 s concurrently, and kept doing so for 81 s after the app sent
its last byte. Connect-read-disconnect polling could not exploit any of that.
It cost ~12 s of connection setup for ~6 s of listening, and -- because a
connection can only be established while Home Assistant is holding a live
connectable scanner-device entry, which exists around advertisements -- it could
only run when this gateway advertised, every 245-300 seconds. So readings
arrived every five minutes from hardware that was publishing them every five
seconds.

Advertisements still matter, but only for *starting* the stream: they are when
a connection path exists. Once open it sustains itself, and the coordinator's
job becomes pushing what arrives out to the entities.

The cost of holding the slot is that nothing else can use it -- the vendor app
included. The Connection switch releases it deliberately; see switch.py.
"""

from __future__ import annotations

import logging

from bluetooth_data_tools import monotonic_time_coarse
from homeassistant.components.bluetooth import (
    BluetoothScanningMode,
    BluetoothServiceInfoBleak,
    async_ble_device_from_address,
    async_last_service_info,
)
from homeassistant.components.bluetooth.active_update_coordinator import (
    ActiveBluetoothDataUpdateCoordinator,
)
from homeassistant.core import HomeAssistant, callback
from tensite_bms_ble import (
    ClusterReading,
    TensiteClusterStream,
    merge_readings,
)

from .const import (
    CONNECT_TIMEOUT,
    MIN_STALE_WINDOW,
    UPDATE_THROTTLE,
)

_LOGGER = logging.getLogger(__name__)


class TensiteClusterCoordinator(
    ActiveBluetoothDataUpdateCoordinator[ClusterReading | None]
):
    """Holds one connection to a cluster master and fans frames out.

    Still an ``ActiveBluetoothDataUpdateCoordinator``, which looks odd for
    something that no longer polls. It earns its place: it registers the
    Bluetooth callbacks, keeps a resolved ``BLEDevice`` current, and calls back
    on every advertisement -- which is exactly when a connection can be made.
    The "poll" it schedules is just "make sure the stream is running".
    """

    def __init__(
        self,
        hass: HomeAssistant,
        address: str,
        serial: str | None,
        hide_sentinel_temperatures: bool = False,
    ) -> None:
        super().__init__(
            hass=hass,
            logger=_LOGGER,
            address=address,
            mode=BluetoothScanningMode.ACTIVE,
            needs_poll_method=self._needs_connection,
            poll_method=self._async_ensure_streaming,
            connectable=True,
        )
        self.serial = serial
        #: See CONF_HIDE_SENTINEL_TEMPERATURES.
        self.hide_sentinel_temperatures = hide_sentinel_temperatures
        #: Options as they were at setup. A reload is only worth doing when
        #: these change -- see _async_update_listener.
        self.options_snapshot: dict = {}
        #: False once the Connection switch has been turned off: the slot is
        #: released and must stay released until the user says otherwise.
        self._enabled = True
        self._stream: TensiteClusterStream | None = None

        #: What the master's own topology frame says the bank holds. The
        #: authoritative answer -- a count the master states, rather than one
        #: inferred from who happened to answer.
        self._roster_batteries = 0
        #: How many batteries the current connection has heard from.
        self._reported_batteries = 0
        #: Monotonic time of the last frames that produced a reading.
        self._last_data_at: float | None = None
        #: Monotonic time of the previous one, to report the achieved cadence.
        self._last_update_interval: float | None = None

        # --- diagnostics ---
        self._updates = 0
        self._connect_failures = 0
        self._connected_at: float | None = None
        #: Why the last connection attempt failed, for the diagnostics download.
        self._last_error: str | None = None
        #: Monotonic time of the last advertisement Home Assistant passed us.
        self._last_advertisement_at: float | None = None

    # -- connection lifecycle -------------------------------------------------

    def _needs_connection(
        self, service_info: BluetoothServiceInfoBleak, last_poll: float | None
    ) -> bool:
        """Whether this advertisement should be used to open the connection.

        Keeping the stream's device current is done here rather than on a
        schedule because this is the only moment a freshly resolved one exists.
        A stale ``BLEDevice`` points at an adapter that may no longer see the
        battery, and reconnects made with one fail in ways that read as the
        gateway being gone.
        """
        self._last_advertisement_at = monotonic_time_coarse()
        if self._stream is not None and service_info.connectable:
            self._stream.update_device(service_info.device)
        needed = self._enabled and not (self._stream and self._stream.is_running)
        if needed:
            _LOGGER.debug(
                "%s: advertisement (rssi=%s) -> opening the stream",
                self.address,
                service_info.rssi,
            )
        return needed

    async def _async_ensure_streaming(
        self, service_info: BluetoothServiceInfoBleak
    ) -> ClusterReading | None:
        """Open the stream if it is not already running.

        Never raises: a failed attempt keeps the last reading and waits for the
        next advertisement, which on this hardware is the next chance anyway.
        """
        if not self._enabled or (self._stream and self._stream.is_running):
            return self.data

        ble_device = service_info.device
        if not service_info.connectable:
            # Reached us via a non-connectable proxy; ask for a connectable
            # device at the same address instead.
            ble_device = async_ble_device_from_address(
                self.hass, self.address, connectable=True
            )
        if ble_device is None:
            self._note_failure("not in Home Assistant's Bluetooth cache")
            return self.data

        self._stream = TensiteClusterStream(
            ble_device,
            serial=self.serial,
            on_update=self._async_handle_reading,
            on_connection_change=self._async_handle_connection_change,
            connect_timeout=CONNECT_TIMEOUT,
            update_throttle=UPDATE_THROTTLE,
            logger=_LOGGER,
        )
        try:
            await self._stream.async_start()
        except Exception as err:  # noqa: BLE001 -- recorded, never propagated
            self._note_failure(f"{type(err).__name__}: {err}")
            _LOGGER.debug("%s: could not open the stream: %s", self.address, err)
            self._stream = None
            return self.data

        self._connect_failures = 0
        self._last_error = None
        return self.data

    async def async_set_enabled(self, enabled: bool) -> None:
        """Hold the connection, or release it for another app.

        Releasing is immediate and sticky: the gateway's one slot is free from
        the moment this returns, and no advertisement will reopen it until this
        is called again.
        """
        if enabled == self._enabled:
            return
        self._enabled = enabled
        if enabled:
            _LOGGER.debug("%s: reconnecting on request", self.address)
            if (info := async_last_service_info(self.hass, self.address, True)) is not None:
                await self._async_ensure_streaming(info)
            # Nothing heard from the gateway yet: the next advertisement opens
            # it, exactly as at startup.
        else:
            await self.async_release()
            _LOGGER.info(
                "%s: connection released -- the gateway is free for other apps",
                self.address,
            )
        self.async_update_listeners()

    async def async_release(self) -> None:
        """Disconnect and stop reconnecting, whatever the switch says."""
        stream, self._stream = self._stream, None
        if stream is not None:
            await stream.async_stop()
        self._connected_at = None

    @property
    def enabled(self) -> bool:
        """Whether the integration is allowed to hold the connection."""
        return self._enabled

    @property
    def is_connected(self) -> bool:
        return self._stream is not None and self._stream.is_connected

    @callback
    def _async_handle_connection_change(self, connected: bool) -> None:
        """Surface connect/disconnect immediately rather than at the next frame."""
        self._connected_at = monotonic_time_coarse() if connected else None
        if not connected:
            # The bank we were listening to is gone with the connection; what it
            # last said is kept, and goes stale on its own if nothing returns.
            self._reported_batteries = 0
        self.async_update_listeners()

    @callback
    def _async_handle_reading(self, reading: ClusterReading) -> None:
        """Take a reading pushed by the stream and update every entity."""
        now = monotonic_time_coarse()
        if self._last_data_at is not None:
            self._last_update_interval = now - self._last_data_at
        self._last_data_at = now
        self._updates += 1

        self._reported_batteries = reading.battery_count
        if reading.roster_count:
            self._roster_batteries = reading.roster_count

        # Carry forward anything this connection has not seen. Within one
        # connection the stream already accumulates, so this only matters across
        # a reconnect -- where replacing wholesale would blank every entity of a
        # battery that has not spoken yet in the new session.
        self.data = merge_readings(self.data, reading)
        if self.serial is None and self.data.master_serial:
            self.serial = self.data.master_serial
        self.async_update_listeners()

    def _note_failure(self, reason: str) -> None:
        self._connect_failures += 1
        self._last_error = reason

    # -- freshness ------------------------------------------------------------

    @property
    def has_fresh_data(self) -> bool:
        """Whether frames have arrived recently enough to trust the reading.

        Entities key their availability off this rather than off the connection
        state: a reconnect takes seconds and should not blank every reading,
        while a connection that is up but silent is not healthy either.
        """
        if self.data is None or self._last_data_at is None:
            return False
        return monotonic_time_coarse() - self._last_data_at <= self.stale_after

    @property
    def stale_after(self) -> float:
        """Seconds without frames before entities report unavailable.

        Generous relative to the ~5 s cadence, because the recovery path is
        slow: if the connection drops, reconnecting may have to wait for the
        gateway's next advertisement, which is 245-300 s away. A window shorter
        than that would blank every entity on a single routine drop.
        """
        return MIN_STALE_WINDOW

    @property
    def available(self) -> bool:
        return self.has_fresh_data

    @property
    def data_age(self) -> float | None:
        """Seconds since frames last produced a reading."""
        if self._last_data_at is None:
            return None
        return monotonic_time_coarse() - self._last_data_at

    @property
    def seconds_since_advertisement(self) -> float | None:
        """How long since Home Assistant last heard from the gateway."""
        if self._last_advertisement_at is None:
            return None
        return monotonic_time_coarse() - self._last_advertisement_at

    @property
    def connected_for(self) -> float | None:
        """Seconds the current connection has been up, None if it is not."""
        if self._connected_at is None:
            return None
        return monotonic_time_coarse() - self._connected_at

    # -- diagnostics ----------------------------------------------------------

    @property
    def update_interval_seconds(self) -> float | None:
        """Gap between the last two readings, as achieved.

        The number to look at when asking "how fresh is this really?": with the
        connection held it should sit near the update throttle, and a value in
        the minutes means the stream is down and reconnecting.

        Rounded, because the raw float differs on every update and would write
        a recorder row each time while saying nothing new.
        """
        if self._last_update_interval is None:
            return None
        return round(self._last_update_interval, 1)

    @property
    def connection_failures(self) -> int:
        """Failed attempts to open the stream since the last success."""
        return self._connect_failures

    @property
    def reconnects(self) -> int:
        """Times the connection has been re-established since it first opened."""
        return self._stream.reconnects if self._stream else 0

    @property
    def connection_health(self) -> dict[str, object]:
        """Everything needed to answer "is data flowing, and if not why?"."""
        return {
            "enabled": self._enabled,
            "connected": self.is_connected,
            "connected_for_s": (
                None if self.connected_for is None else round(self.connected_for, 1)
            ),
            "reconnects": self.reconnects,
            "updates": self._updates,
            "update_interval_s": (
                None
                if self._last_update_interval is None
                else round(self._last_update_interval, 1)
            ),
            "throttle_s": UPDATE_THROTTLE,
            "stale_after_s": round(self.stale_after, 1),
            "seconds_since_data": (
                None if self.data_age is None else round(self.data_age, 1)
            ),
            "connection_failures": self._connect_failures,
            "last_error": self._last_error or (
                self._stream.last_error if self._stream else None
            ),
            "batteries_expected": self.expected_batteries,
            "batteries_reported": self._reported_batteries,
            "battery_count_source": self.battery_count_source,
            "roster_batteries": self._roster_batteries or None,
            "rssi": self.rssi,
            "seconds_since_advertisement": (
                None
                if self.seconds_since_advertisement is None
                else round(self.seconds_since_advertisement, 1)
            ),
        }

    @property
    def cluster_name(self) -> str:
        """Human label for the cluster, preferring its serial over its address."""
        if self.serial:
            return f"Cluster {self.serial[-5:]}"
        return f"Cluster {self.address}"

    @property
    def rssi(self) -> int | None:
        """Signal strength from the last advertisement Home Assistant saw.

        Reported because it is the one cheap indicator of *why* a connection
        failed.
        """
        info = async_last_service_info(self.hass, self.address, connectable=True)
        return info.rssi if info else None

    # -- bank size ------------------------------------------------------------

    @property
    def expected_batteries(self) -> int:
        """How many batteries the bank holds. 0 means "not known yet"."""
        return self.detected_batteries

    @property
    def detected_batteries(self) -> int:
        """What the bank says it holds, falling back to who has been heard.

        The roster wins where they differ: it is a count the master *states*, in
        a header byte of its topology frame, rather than a tally of who
        answered. That frame is rare -- 18 against 252 summaries in one capture
        -- which used to mean an hour-long full scan cycle to catch one. On a
        held connection it simply arrives.

        Counting who has been heard is a sound fallback here, which it was not
        under polling: every battery reports every ~5 s on a live connection, so
        after the first few seconds the tally is the bank.
        """
        return self._roster_batteries or self._reported_batteries

    @property
    def batteries_expected(self) -> int:
        """Alias of expected_batteries, for the sensor of the same name."""
        return self.expected_batteries

    @property
    def batteries_reported(self) -> int:
        """How many batteries the current connection has heard from."""
        return self._reported_batteries

    @property
    def battery_count_source(self) -> str:
        """Where the count came from, for diagnostics."""
        if self._roster_batteries:
            return "roster"
        if self._reported_batteries:
            return "stream"
        return "unknown"
