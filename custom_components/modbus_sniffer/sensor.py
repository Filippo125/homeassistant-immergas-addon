"""Piattaforma sensore per l'integrazione Modbus Sniffer."""
from __future__ import annotations

import logging
from typing import Any, Dict, Iterable, List, Optional, Tuple

import voluptuous as vol

from homeassistant.components.sensor import PLATFORM_SCHEMA, SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_NAME
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import PlatformNotReady
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.typing import ConfigType

from .const import (
    ATTR_RAW_VALUE,
    ATTR_REGISTER,
    ATTR_UNIT_ID,
    CONF_CONNECTION_MODE,
    CONF_DEVICE,
    CONF_DEVICE_CLASS,
    CONF_DEVICE_TYPE,
    CONF_FORCE_UPDATE,
    CONF_ICON,
    CONF_OFFSET,
    CONF_PRECISION,
    CONF_REGISTER,
    CONF_SCALE,
    CONF_SENSORS,
    CONF_SOURCE_HOST,
    CONF_STATE_CLASS,
    CONF_STATE_MAP,
    CONF_TCP_HOST,
    CONF_TCP_PORT,
    CONF_UNIT,
    CONF_UNIT_ID,
    CONF_UDP_PORT,
    DATA_HUB,
    DATA_LISTENERS,
    DEFAULT_CONNECTION_MODE,
    DEFAULT_DEVICE_TYPE,
    DEFAULT_OFFSET,
    DEFAULT_PRECISION,
    DEFAULT_SCALE,
    DEFAULT_SOURCE_HOST,
    DEFAULT_TCP_PORT,
    DEFAULT_UDP_PORT,
    DEVICE_TYPE_LABELS,
    DOMAIN,
    MODE_TCP,
    MODE_UDP,
    SIGNAL_REGISTER_UPDATE,
)
from .hub import ModbusSnifferHub

_LOGGER = logging.getLogger(__name__)


def _coerce_register(value: Any) -> int:
    if isinstance(value, str):
        raw = value.strip()
        base = 16 if raw.lower().startswith("0x") else 10
        try:
            value = int(raw, base)
        except ValueError as err:
            raise vol.Invalid(f"Valore registro non valido: {value}") from err
    if not isinstance(value, int):
        raise vol.Invalid(f"Tipo registro non supportato: {type(value)}")
    if not 0 <= value <= 0xFFFF:
        raise vol.Invalid("Il registro deve essere compreso fra 0 e 0xFFFF")
    return value


def _coerce_unit_id(value: Any) -> int:
    if isinstance(value, str):
        raw = value.strip()
        base = 16 if raw.lower().startswith("0x") else 10
        try:
            value = int(raw, base)
        except ValueError as err:
            raise vol.Invalid(f"Unit ID non valido: {value}") from err
    if not isinstance(value, int):
        raise vol.Invalid("Unit ID deve essere un intero")
    if not 0 <= value <= 0xFF:
        raise vol.Invalid("Unit ID deve essere nell'intervallo 0-255")
    return value


def _coerce_state_map(value: Any) -> Dict[int, str]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise vol.Invalid("state_map deve essere un mapping")
    result: Dict[int, str] = {}
    for key, label in value.items():
        try:
            key_int = _coerce_register(key)
        except vol.Invalid:
            key_int = _coerce_unit_id(key)
        result[key_int] = cv.string(label)
    return result


def _coerce_identifiers(value: Any) -> Tuple[Tuple[str, str], ...]:
    identifiers = cv.ensure_list(value)
    result = []
    for item in identifiers:
        if not isinstance(item, str):
            raise vol.Invalid("Gli identificatori devono essere stringhe")
        result.append((DOMAIN, item))
    return tuple(result)


DEVICE_SCHEMA = vol.Schema(
    {
        vol.Optional("identifiers"): _coerce_identifiers,
        vol.Optional("manufacturer"): cv.string,
        vol.Optional("model"): cv.string,
        vol.Optional("name"): cv.string,
        vol.Optional("sw_version"): cv.string,
        vol.Optional("via_device"): cv.string,
    }
)


SENSOR_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_NAME): cv.string,
        vol.Required(CONF_REGISTER): _coerce_register,
        vol.Optional(CONF_UNIT_ID): _coerce_unit_id,
        vol.Optional(CONF_SCALE, default=DEFAULT_SCALE): vol.Coerce(float),
        vol.Optional(CONF_OFFSET, default=DEFAULT_OFFSET): vol.Coerce(float),
        vol.Optional(CONF_PRECISION, default=DEFAULT_PRECISION): vol.Any(None, vol.Coerce(int)),
        vol.Optional(CONF_STATE_MAP): _coerce_state_map,
        vol.Optional(CONF_UNIT): cv.string,
        vol.Optional(CONF_DEVICE_CLASS): cv.string,
        vol.Optional(CONF_STATE_CLASS): cv.string,
        vol.Optional(CONF_ICON): cv.string,
        vol.Optional(CONF_FORCE_UPDATE, default=False): cv.boolean,
        vol.Optional(CONF_DEVICE): DEVICE_SCHEMA,
    }
)


PLATFORM_SCHEMA = PLATFORM_SCHEMA.extend(
    {
        vol.Optional(CONF_CONNECTION_MODE, default=DEFAULT_CONNECTION_MODE): vol.In(
            [MODE_UDP, MODE_TCP]
        ),
        vol.Optional(CONF_SOURCE_HOST, default=DEFAULT_SOURCE_HOST): cv.string,
        vol.Optional(CONF_UDP_PORT, default=DEFAULT_UDP_PORT): cv.port,
        vol.Optional(CONF_TCP_HOST, default=""): cv.string,
        vol.Optional(CONF_TCP_PORT, default=DEFAULT_TCP_PORT): cv.port,
        vol.Optional(CONF_DEVICE_TYPE, default=DEFAULT_DEVICE_TYPE): vol.In(list(DEVICE_TYPE_LABELS)),
        vol.Optional(CONF_NAME, default=""): cv.string,
        vol.Required(CONF_SENSORS): vol.All(cv.ensure_list, [SENSOR_SCHEMA]),
    }
)


def _ensure_component_data(hass: HomeAssistant) -> Dict[str, Any]:
    data = hass.data.setdefault(DOMAIN, {})
    data.setdefault(DATA_HUB, {})
    data.setdefault(DATA_LISTENERS, {})
    return data


async def async_setup_platform(
    hass: HomeAssistant,
    config: ConfigType,
    async_add_entities,
    discovery_info=None,
):
    """Configura la piattaforma dei sensori Modbus Sniffer via YAML."""
    component_data = _ensure_component_data(hass)
    mode = config.get(CONF_CONNECTION_MODE, DEFAULT_CONNECTION_MODE)
    if mode == MODE_TCP:
        host = config.get(CONF_TCP_HOST, "").strip()
        port = config.get(CONF_TCP_PORT, DEFAULT_TCP_PORT)
        if not host:
            _LOGGER.error(
                "Configurazione Modbus Sniffer YAML: host TCP mancante per la modalità TCP"
            )
            return
    else:
        host = config.get(CONF_SOURCE_HOST, DEFAULT_SOURCE_HOST).strip()
        port = config.get(CONF_UDP_PORT, DEFAULT_UDP_PORT)
    instance_name = (config.get(CONF_NAME) or "").strip() or None
    device_type = config.get(CONF_DEVICE_TYPE, DEFAULT_DEVICE_TYPE)
    sensors_conf = config[CONF_SENSORS]
    await _async_setup_sensors(
        hass,
        mode,
        host,
        port,
        sensors_conf,
        component_data,
        async_add_entities,
        instance_name=instance_name,
        device_type=device_type,
    )


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities,
):
    """Configura i sensori partendo da una config entry."""
    component_data = _ensure_component_data(hass)
    mode = entry.data.get(CONF_CONNECTION_MODE, DEFAULT_CONNECTION_MODE)
    if mode == MODE_TCP:
        host = entry.data.get(CONF_TCP_HOST, "").strip()
        port = entry.data.get(CONF_TCP_PORT, DEFAULT_TCP_PORT)
        if not host:
            _LOGGER.error(
                "Config entry Modbus Sniffer %s: host TCP mancante in modalità TCP",
                entry.title,
            )
            return
    else:
        host = entry.data.get(CONF_SOURCE_HOST, DEFAULT_SOURCE_HOST).strip()
        port = entry.data.get(CONF_UDP_PORT, DEFAULT_UDP_PORT)
    instance_name = (entry.data.get(CONF_NAME) or "").strip() or None
    device_type = entry.data.get(CONF_DEVICE_TYPE, DEFAULT_DEVICE_TYPE)
    sensors_conf: Iterable[dict] = entry.options.get(CONF_SENSORS, entry.data.get(CONF_SENSORS, []))
    if not sensors_conf:
        _LOGGER.warning(
            "Nessun sensore definito per l'entry Modbus Sniffer %s (%s:%s)",
            entry.title,
            host,
            port,
        )
        return
    await _async_setup_sensors(
        hass,
        mode,
        host,
        port,
        sensors_conf,
        component_data,
        async_add_entities,
        instance_name=instance_name,
        device_type=device_type,
        entry_id=entry.entry_id,
    )


async def _async_setup_sensors(
    hass: HomeAssistant,
    mode: str,
    host: str,
    port: int,
    sensors_conf: Iterable[dict],
    component_data: Dict[str, Any],
    async_add_entities,
    *,
    instance_name: Optional[str] = None,
    device_type: Optional[str] = None,
    entry_id: Optional[str] = None,
) -> None:
    hubs: Dict[Tuple[str, str, int], ModbusSnifferHub] = component_data[DATA_HUB]
    listeners: Dict[Tuple[str, str, int], int] = component_data[DATA_LISTENERS]
    hub_key = (mode, host, int(port))

    hub = hubs.get(hub_key)
    if hub is None:
        hub = ModbusSnifferHub(hass, mode, host, int(port))
        try:
            await hub.async_start()
        except OSError as exc:
            raise PlatformNotReady(
                f"Impossibile avviare Modbus Sniffer {mode.upper()} su {host}:{port}"
            ) from exc
        hubs[hub_key] = hub
        listeners[hub_key] = 0
        if mode == MODE_UDP:
            _LOGGER.info("Listener UDP Modbus Sniffer attivo su %s:%s", host, port)
        else:
            _LOGGER.info("Listener TCP Modbus Sniffer verso %s:%s avviato", host, port)

    entities: List[ModbusSnifferSensor] = []
    for sensor_conf in sensors_conf:
        try:
            validated = SENSOR_SCHEMA(sensor_conf)
        except vol.Invalid as err:
            _LOGGER.error(
                "Configurazione sensore non valida per %s:%s -> %s", host, port, err
            )
            continue
        entities.append(
            ModbusSnifferSensor(
                hass,
                hub,
                hub_key,
                component_data,
                dict(validated),
                instance_name=instance_name,
                device_type=device_type,
                entry_id=entry_id,
            )
        )

    if not entities:
        _LOGGER.warning("Nessun sensore valido configurato per %s:%s", host, port)
        return

    listeners[hub_key] = listeners.get(hub_key, 0) + len(entities)
    async_add_entities(entities)


class ModbusSnifferSensor(SensorEntity):
    """Sensore Home Assistant alimentato dal Modbus Sniffer."""

    _attr_should_poll = False

    def __init__(
        self,
        hass: HomeAssistant,
        hub: ModbusSnifferHub,
        hub_key: Tuple[str, str, int],
        component_data: Dict[str, Any],
        config: dict,
        *,
        instance_name: Optional[str] = None,
        device_type: Optional[str] = None,
        entry_id: Optional[str] = None,
    ) -> None:
        self._hass = hass
        self._hub = hub
        self._hub_key = hub_key
        self._component_data = component_data
        self._instance_name = instance_name
        self._device_type = device_type
        self._entry_id = entry_id
        self._mode, self._host, self._port = hub_key
        base_name = config[CONF_NAME]
        if instance_name:
            self._name = f"{instance_name} {base_name}"
        else:
            self._name = base_name
        self._register = config[CONF_REGISTER]
        self._unit_id = config.get(CONF_UNIT_ID)
        self._scale = config[CONF_SCALE]
        self._offset = config[CONF_OFFSET]
        self._precision = config[CONF_PRECISION]
        self._state_map = config.get(CONF_STATE_MAP) or {}
        self._unit = config.get(CONF_UNIT)
        self._device_class = config.get(CONF_DEVICE_CLASS)
        self._state_class = config.get(CONF_STATE_CLASS)
        self._icon = config.get(CONF_ICON)
        self._force_update = config.get(CONF_FORCE_UPDATE, False)
        self._raw_value: Optional[int] = None
        self._native_value: Any = None
        self._unsubscribe = None
        device_conf = config.get(CONF_DEVICE) or {}
        identifiers = device_conf.get("identifiers")
        if identifiers:
            ident_set = set(identifiers)
        else:
            default_identifier = entry_id or f"{self._mode}:{self._host}:{self._port}"
            ident_set = {(DOMAIN, default_identifier)}
        device_info = {"identifiers": ident_set}
        for key in ("manufacturer", "model", "name", "sw_version", "via_device"):
            if key in device_conf:
                device_info[key] = device_conf[key]
        if "name" not in device_info and instance_name:
            device_info["name"] = instance_name
        if "model" not in device_info and device_type:
            device_info["model"] = DEVICE_TYPE_LABELS.get(device_type, device_type)
        self._device_info = device_info

    @property
    def name(self) -> str:
        return self._name

    @property
    def unique_id(self) -> str:
        uid_part = f"{self._unit_id:02X}" if self._unit_id is not None else "any"
        host = self._host
        port = self._port
        if self._mode == MODE_UDP:
            prefix = f"{DOMAIN}_{host}_{port}"
        else:
            prefix = f"{DOMAIN}_{self._mode}_{host}_{port}"
        return f"{prefix}_{uid_part}_{self._register:04X}"

    @property
    def device_info(self) -> dict:
        return self._device_info

    @property
    def native_unit_of_measurement(self) -> Optional[str]:
        return self._unit

    @property
    def device_class(self) -> Optional[str]:
        return self._device_class

    @property
    def state_class(self) -> Optional[str]:
        return self._state_class

    @property
    def icon(self) -> Optional[str]:
        return self._icon

    @property
    def force_update(self) -> bool:
        return self._force_update

    @property
    def native_value(self):
        return self._native_value

    @property
    def extra_state_attributes(self) -> dict:
        attrs = {
            ATTR_REGISTER: f"0x{self._register:04X}",
        }
        if self._unit_id is not None:
            attrs[ATTR_UNIT_ID] = self._unit_id
        if self._raw_value is not None:
            attrs[ATTR_RAW_VALUE] = self._raw_value
        return attrs

    async def async_added_to_hass(self) -> None:
        self._unsubscribe = async_dispatcher_connect(
            self._hass,
            SIGNAL_REGISTER_UPDATE,
            self._handle_register_update,
        )
        existing = self._hub.get_register(self._unit_id, self._register)
        if existing is not None:
            self._apply_new_value(existing)

    async def async_will_remove_from_hass(self) -> None:
        if self._unsubscribe:
            self._unsubscribe()
            self._unsubscribe = None
        listeners: Dict[Tuple[str, str, int], int] = self._component_data.get(DATA_LISTENERS, {})
        hubs: Dict[Tuple[str, str, int], ModbusSnifferHub] = self._component_data.get(DATA_HUB, {})
        count = listeners.get(self._hub_key)
        if count is None:
            return
        count -= 1
        if count <= 0:
            listeners.pop(self._hub_key, None)
            hub = hubs.pop(self._hub_key, None)
            if hub:
                await hub.async_stop()
        else:
            listeners[self._hub_key] = count

    @callback
    def _handle_register_update(self, unit_id: int, register: int, value: int) -> None:
        if register != self._register:
            return
        if self._unit_id is not None and unit_id != self._unit_id:
            return
        self._apply_new_value(value)

    @callback
    def _apply_new_value(self, raw_value: int) -> None:
        self._raw_value = raw_value
        if self._state_map:
            self._native_value = self._state_map.get(raw_value, str(raw_value))
        else:
            scaled = raw_value * self._scale + self._offset
            if self._precision is not None:
                scaled = round(scaled, self._precision)
            self._native_value = scaled
        self.async_write_ha_state()
