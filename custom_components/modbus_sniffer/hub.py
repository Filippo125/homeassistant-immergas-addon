"""Gestore dell'ascolto UDP e decodifica dei frame Modbus."""
from __future__ import annotations

import asyncio
import logging
from collections import defaultdict, deque
from dataclasses import dataclass
from typing import Deque, Dict, Optional, Tuple

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.util import dt as dt_util

from .const import (
    ATTR_LAST_UPDATE,
    ATTR_RAW_VALUE,
    ATTR_REGISTER,
    ATTR_UNIT_ID,
    MODE_TCP,
    MODE_UDP,
    SIGNAL_REGISTER_UPDATE,
)
from .parser import (
    parse_fc03_request,
    parse_fc03_response,
    parse_fc06,
    parse_fc16_request,
    split_modbus_frames,
)

_LOGGER = logging.getLogger(__name__)


@dataclass
class RegisterValue:
    """Rappresenta l'ultimo valore visto per un registro holding."""

    unit_id: int
    register: int
    value: int
    updated_at: float

    @property
    def as_dict(self) -> dict:
        return {
            ATTR_UNIT_ID: self.unit_id,
            ATTR_REGISTER: self.register,
            ATTR_RAW_VALUE: self.value,
            ATTR_LAST_UPDATE: dt_util.utcnow(),
        }


class _UDPProtocol(asyncio.DatagramProtocol):
    """Protocollo asincrono per ricevere datagrammi UDP."""

    def __init__(self, hub: "ModbusSnifferHub") -> None:
        self._hub = hub

    def datagram_received(self, data: bytes, addr: Tuple[str, int]) -> None:  # type: ignore[override]
        self._hub.handle_datagram(data, addr)

    def error_received(self, exc: Exception) -> None:  # type: ignore[override]
        _LOGGER.error("Errore dal socket UDP Modbus Sniffer: %s", exc)


class ModbusSnifferHub:
    """Gestisce il binding di rete e converte i frame in aggiornamenti registro."""

    def __init__(self, hass: HomeAssistant, mode: str, host: str, port: int) -> None:
        self._hass = hass
        self._mode = mode
        self._host = host
        self._port = port
        self._transport: Optional[asyncio.BaseTransport] = None
        self._tcp_task: Optional[asyncio.Task] = None
        self._tcp_writer: Optional[asyncio.StreamWriter] = None
        self._stop_requested = False
        self._buffer = b""
        self._pending_reads: Dict[int, Deque[Tuple[int, int, float]]] = defaultdict(deque)
        self._values: Dict[Tuple[int, int], RegisterValue] = {}
        self._lock = asyncio.Lock()

    @property
    def address(self) -> Tuple[str, int]:
        return self._host, self._port

    @property
    def mode(self) -> str:
        return self._mode

    async def async_start(self) -> None:
        """Avvia l'ascolto in base alla modalità configurata."""
        async with self._lock:
            if self._mode == MODE_UDP:
                if self._transport is not None:
                    return
                loop = asyncio.get_running_loop()
                _LOGGER.debug("Avvio listener UDP Modbus Sniffer su %s:%s", self._host, self._port)
                try:
                    transport, _ = await loop.create_datagram_endpoint(
                        lambda: _UDPProtocol(self),
                        local_addr=(self._host, self._port),
                    )
                except OSError as exc:
                    _LOGGER.error(
                        "Impossibile avviare il listener UDP Modbus Sniffer su %s:%s: %s",
                        self._host,
                        self._port,
                        exc,
                    )
                    raise
                self._transport = transport
            else:
                if self._tcp_task and not self._tcp_task.done():
                    return
                loop = asyncio.get_running_loop()
                self._stop_requested = False
                self._tcp_task = loop.create_task(self._tcp_run())

    async def async_stop(self) -> None:
        """Ferma l'ascolto."""
        async with self._lock:
            if self._transport:
                _LOGGER.debug("Arresto listener UDP Modbus Sniffer su %s:%s", self._host, self._port)
                self._transport.close()
                self._transport = None
            if self._tcp_task:
                _LOGGER.debug("Arresto listener TCP Modbus Sniffer verso %s:%s", self._host, self._port)
                self._stop_requested = True
                task = self._tcp_task
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
                self._tcp_task = None
            if self._tcp_writer:
                self._tcp_writer.close()
                try:
                    await self._tcp_writer.wait_closed()
                except Exception:
                    pass
                self._tcp_writer = None
            self._buffer = b""
            self._pending_reads.clear()

    def handle_datagram(self, data: bytes, addr: Tuple[str, int]) -> None:
        """Elabora un datagramma UDP proveniente dallo sniffer."""
        self._process_bytes(data, source=f"udp://{addr[0]}:{addr[1]}")

    def _process_bytes(self, data: bytes, *, source: Optional[str] = None) -> None:
        if not data:
            return
        payload = self._buffer + data
        frames, leftover = split_modbus_frames(payload)
        self._buffer = leftover
        if leftover:
            origin = source or f"{self._host}:{self._port}"
            _LOGGER.debug(
                "Frame incompleto da %s (%d byte mantenuti)",
                origin,
                len(leftover),
            )
        for frame in frames:
            self._handle_frame(frame)

    async def _tcp_run(self) -> None:
        backoff = 1
        while not self._stop_requested:
            try:
                _LOGGER.debug(
                    "Connessione TCP Modbus Sniffer verso %s:%s in corso", self._host, self._port
                )
                reader, writer = await asyncio.open_connection(self._host, self._port)
            except asyncio.CancelledError:
                raise
            except OSError as exc:
                if self._stop_requested:
                    break
                _LOGGER.error(
                    "Impossibile connettersi al server TCP Modbus Sniffer %s:%s: %s",
                    self._host,
                    self._port,
                    exc,
                )
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30)
                continue

            self._tcp_writer = writer
            backoff = 1
            _LOGGER.info(
                "Connessione TCP Modbus Sniffer attiva verso %s:%s",
                self._host,
                self._port,
            )
            try:
                while not self._stop_requested:
                    data = await reader.read(1024)
                    if not data:
                        _LOGGER.warning(
                            "Connessione TCP Modbus Sniffer chiusa dal server %s:%s",
                            self._host,
                            self._port,
                        )
                        break
                    self._process_bytes(data, source=f"tcp://{self._host}:{self._port}")
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # pylint: disable=broad-except
                if self._stop_requested:
                    break
                _LOGGER.exception(
                    "Errore durante l'ascolto TCP Modbus Sniffer %s:%s: %s",
                    self._host,
                    self._port,
                    exc,
                )
            finally:
                if self._tcp_writer is writer:
                    writer.close()
                    try:
                        await writer.wait_closed()
                    except Exception:  # pylint: disable=broad-except
                        pass
                    self._tcp_writer = None
                self._buffer = b""
            if self._stop_requested:
                break
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30)

    def _handle_frame(self, frame: bytes) -> None:
        unit = frame[0]
        func = frame[1]
        fc = func & 0x7F
        now_monotonic = asyncio.get_running_loop().time()

        if fc == 3:
            request = parse_fc03_request(frame)
            if request:
                _, start_addr, quantity = request
                self._pending_reads[unit].append((start_addr, quantity, now_monotonic))
                self._purge_old_requests(unit, now_monotonic)
                return
            response = parse_fc03_response(frame)
            if response:
                _, values = response
                start_addr = self._pop_matching_request(unit, len(values), now_monotonic)
                if start_addr is None:
                    start_addr = 0
                for offset, value in enumerate(values):
                    register = start_addr + offset
                    self._store_register(unit, register, value)
                return
        elif fc == 6:
            parsed = parse_fc06(frame)
            if parsed:
                _, register, value = parsed
                self._store_register(unit, register, value)
                return
        elif fc == 16:
            write_multi = parse_fc16_request(frame)
            if write_multi:
                _, start_addr, values = write_multi
                for offset, value in enumerate(values):
                    register = start_addr + offset
                    self._store_register(unit, register, value)
                return
        else:
            # Altre funzioni non gestite ma potrebbe essere utile loggare in debug
            _LOGGER.debug("Frame Modbus non gestito: func=0x%02X len=%d", func, len(frame))

    def _purge_old_requests(self, unit: int, now: float) -> None:
        queue = self._pending_reads[unit]
        while queue and now - queue[0][2] > 5.0:
            queue.popleft()

    def _pop_matching_request(self, unit: int, values_len: int, now: float) -> Optional[int]:
        queue = self._pending_reads[unit]
        self._purge_old_requests(unit, now)
        if not queue:
            return None
        start_addr, quantity, _ = queue.popleft()
        if quantity and quantity < values_len:
            return start_addr
        return start_addr

    def _store_register(self, unit_id: int, register: int, value: int) -> None:
        key = (unit_id, register)
        updated_at = asyncio.get_running_loop().time()
        self._values[key] = RegisterValue(unit_id, register, value, updated_at)
        _LOGGER.debug(
            "Aggiornamento registro unit=0x%02X reg=0x%04X val=%d",
            unit_id,
            register,
            value,
        )
        async_dispatcher_send(self._hass, SIGNAL_REGISTER_UPDATE, unit_id, register, value)

    def get_register(self, unit_id: Optional[int], register: int) -> Optional[int]:
        if unit_id is not None:
            sample = self._values.get((unit_id, register))
            return sample.value if sample else None
        for (uid, reg), sample in reversed(list(self._values.items())):
            if reg == register:
                return sample.value
        return None
