"""Config flow per l'integrazione Modbus Sniffer."""
from __future__ import annotations

from collections.abc import Mapping
import copy
from typing import Any, Dict, List

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.data_entry_flow import FlowResult
from homeassistant.const import CONF_NAME

from .const import (
    CONF_CONNECTION_MODE,
    CONF_DEVICE_CLASS,
    CONF_DEVICE_TYPE,
    CONF_FORCE_UPDATE,
    CONF_ICON,
    CONF_INCLUDE_DEFAULTS,
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
    DEFAULT_CONNECTION_MODE,
    DEFAULT_DEVICE_TYPE,
    DEFAULT_OFFSET,
    DEFAULT_PRECISION,
    DEFAULT_SCALE,
    DEFAULT_SENSOR_TEMPLATES_BY_DEVICE,
    DEFAULT_SOURCE_HOST,
    DEFAULT_TCP_PORT,
    DEFAULT_UDP_PORT,
    DEVICE_TYPE_LABELS,
    DOMAIN,
    MODE_TCP,
    MODE_UDP,
)
from .sensor import SENSOR_SCHEMA


class ModbusSnifferConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Gestisce il flusso di configurazione via UI."""

    VERSION = 2

    async def async_step_user(self, user_input: Dict[str, Any] | None = None) -> FlowResult:
        errors: Dict[str, str] = {}
        if user_input is None:
            user_input = {}

        mode = user_input.get(CONF_CONNECTION_MODE, DEFAULT_CONNECTION_MODE)

        submitted = CONF_NAME in user_input
        if submitted:
            name_raw = user_input.get(CONF_NAME, "")
            name = name_raw.strip() if isinstance(name_raw, str) else ""
            if not name:
                errors[CONF_NAME] = "required"

            device_type = user_input.get(CONF_DEVICE_TYPE, DEFAULT_DEVICE_TYPE)
            if device_type not in DEVICE_TYPE_LABELS:
                errors[CONF_DEVICE_TYPE] = "invalid_device_type"

            if mode not in (MODE_UDP, MODE_TCP):
                errors[CONF_CONNECTION_MODE] = "invalid_connection_mode"

            conn_host: str | None = None
            conn_port: int | None = None

            if not errors:
                if mode == MODE_TCP:
                    tcp_host_raw = user_input.get(CONF_TCP_HOST, "")
                    tcp_host = tcp_host_raw.strip() if isinstance(tcp_host_raw, str) else ""
                    if not tcp_host:
                        errors[CONF_TCP_HOST] = "required"
                    else:
                        conn_host = tcp_host
                    try:
                        tcp_port = int(user_input.get(CONF_TCP_PORT, DEFAULT_TCP_PORT))
                    except (TypeError, ValueError):
                        errors[CONF_TCP_PORT] = "invalid_port"
                    else:
                        if not 1 <= tcp_port <= 65535:
                            errors[CONF_TCP_PORT] = "invalid_port"
                        else:
                            conn_port = tcp_port
                else:
                    source_host_raw = user_input.get(CONF_SOURCE_HOST, DEFAULT_SOURCE_HOST)
                    source_host = (
                        source_host_raw.strip()
                        if isinstance(source_host_raw, str)
                        else DEFAULT_SOURCE_HOST
                    )
                    if not source_host:
                        errors[CONF_SOURCE_HOST] = "required"
                    conn_host = source_host or DEFAULT_SOURCE_HOST
                    try:
                        udp_port = int(user_input.get(CONF_UDP_PORT, DEFAULT_UDP_PORT))
                    except (TypeError, ValueError):
                        errors[CONF_UDP_PORT] = "invalid_port"
                    else:
                        if not 1 <= udp_port <= 65535:
                            errors[CONF_UDP_PORT] = "invalid_port"
                        else:
                            conn_port = udp_port

            if submitted and not errors:
                assert conn_host is not None
                assert conn_port is not None
                unique_id = f"{mode}:{conn_host}:{conn_port}"
                await self.async_set_unique_id(unique_id)
                self._abort_if_unique_id_configured()

                include_defaults = user_input.get(CONF_INCLUDE_DEFAULTS, True)
                device_type = user_input.get(CONF_DEVICE_TYPE, DEFAULT_DEVICE_TYPE)
                sensor_templates = DEFAULT_SENSOR_TEMPLATES_BY_DEVICE.get(device_type, [])
                sensors = copy.deepcopy(sensor_templates) if include_defaults else []

                data = {
                    CONF_NAME: name,
                    CONF_DEVICE_TYPE: device_type,
                    CONF_CONNECTION_MODE: mode,
                    CONF_SENSORS: sensors,
                }
                if mode == MODE_TCP:
                    data[CONF_TCP_HOST] = conn_host
                    data[CONF_TCP_PORT] = conn_port
                else:
                    data[CONF_SOURCE_HOST] = conn_host
                    data[CONF_UDP_PORT] = conn_port

                title = name or f"{conn_host}:{conn_port}"
                return self.async_create_entry(title=title, data=data)

        schema_dict: Dict[Any, Any] = {
            vol.Required(
                CONF_NAME,
                default=user_input.get(CONF_NAME, DEVICE_TYPE_LABELS[DEFAULT_DEVICE_TYPE]),
            ): str,
            vol.Required(
                CONF_DEVICE_TYPE,
                default=user_input.get(CONF_DEVICE_TYPE, DEFAULT_DEVICE_TYPE),
            ): vol.In(DEVICE_TYPE_LABELS),
            vol.Required(
                CONF_CONNECTION_MODE,
                default=mode,
            ): vol.In({MODE_UDP: "UDP", MODE_TCP: "TCP"}),
        }

        if mode == MODE_TCP:
            schema_dict.update(
                {
                    vol.Required(
                        CONF_TCP_HOST,
                        default=user_input.get(CONF_TCP_HOST, ""),
                    ): str,
                    vol.Required(
                        CONF_TCP_PORT,
                        default=user_input.get(CONF_TCP_PORT, DEFAULT_TCP_PORT),
                    ): vol.All(vol.Coerce(int), vol.Range(min=1, max=65535)),
                }
            )
        else:
            schema_dict.update(
                {
                    vol.Required(
                        CONF_SOURCE_HOST,
                        default=user_input.get(CONF_SOURCE_HOST, DEFAULT_SOURCE_HOST),
                    ): str,
                    vol.Required(
                        CONF_UDP_PORT,
                        default=user_input.get(CONF_UDP_PORT, DEFAULT_UDP_PORT),
                    ): vol.All(vol.Coerce(int), vol.Range(min=1, max=65535)),
                }
            )

        schema_dict[vol.Optional(CONF_INCLUDE_DEFAULTS, default=user_input.get(CONF_INCLUDE_DEFAULTS, True))] = bool

        schema = vol.Schema(schema_dict)
        return self.async_show_form(
            step_id="user",
            data_schema=schema,
            errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: config_entries.ConfigEntry) -> config_entries.OptionsFlow:
        return ModbusSnifferOptionsFlowHandler(config_entry)


class ModbusSnifferOptionsFlowHandler(config_entries.OptionsFlow):
    """Gestisce l'options flow per la definizione dei sensori."""

    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        self._config_entry = config_entry
        base = config_entry.options.get(CONF_SENSORS)
        if base is None:
            base = config_entry.data.get(CONF_SENSORS, [])
        self._sensors: List[dict] = [dict(sensor) for sensor in base]

    async def async_step_init(self, user_input: Dict[str, Any] | None = None) -> FlowResult:
        if user_input is not None:
            action = user_input["action"]
            if action == "add":
                return await self.async_step_add()
            if action == "remove":
                return await self.async_step_remove()
            if action == "clear":
                self._sensors.clear()
                return await self._async_save()
            if action == "finish":
                return await self._async_save()

        actions = ["finish", "add", "remove", "clear"]
        schema = vol.Schema({vol.Required("action", default="finish"): vol.In(actions)})
        return self.async_show_form(
            step_id="init",
            data_schema=schema,
            description_placeholders={"sensor_count": str(len(self._sensors))},
        )

    async def async_step_add(self, user_input: Dict[str, Any] | None = None) -> FlowResult:
        errors: Dict[str, str] = {}
        if user_input is not None:
            try:
                sensor_data = self._normalize_sensor_input(user_input)
                validated = SENSOR_SCHEMA(sensor_data)
            except vol.Invalid as err:
                message = getattr(err, "error_message", None)
                if message in {"invalid_state_map", "invalid_precision"}:
                    errors["base"] = message
                else:
                    errors["base"] = "invalid_sensor"
            else:
                self._sensors.append(dict(validated))
                return await self._async_save()

        schema = vol.Schema(
            {
                vol.Required(CONF_NAME): str,
                vol.Required(CONF_REGISTER): str,
                vol.Optional(CONF_UNIT_ID, default=""): str,
                vol.Optional(CONF_SCALE, default=DEFAULT_SCALE): vol.Coerce(float),
                vol.Optional(CONF_OFFSET, default=DEFAULT_OFFSET): vol.Coerce(float),
                vol.Optional(CONF_PRECISION, default=""): str,
                vol.Optional(CONF_UNIT, default=""): str,
                vol.Optional(CONF_DEVICE_CLASS, default=""): str,
                vol.Optional(CONF_STATE_CLASS, default=""): str,
                vol.Optional(CONF_ICON, default=""): str,
                vol.Optional(CONF_FORCE_UPDATE, default=False): bool,
                vol.Optional(CONF_STATE_MAP, default=""): str,
            }
        )
        return self.async_show_form(
            step_id="add",
            data_schema=schema,
            errors=errors,
        )

    async def async_step_remove(self, user_input: Dict[str, Any] | None = None) -> FlowResult:
        if not self._sensors:
            return self.async_show_form(
                step_id="remove",
                data_schema=vol.Schema({}),
                errors={"base": "no_sensors"},
            )

        errors: Dict[str, str] = {}
        if user_input is not None:
            index = int(user_input["sensor_index"])
            if 0 <= index < len(self._sensors):
                self._sensors.pop(index)
                return await self._async_save()
            errors["base"] = "invalid_selection"

        options = {str(idx): sensor.get("name", f"Registro {sensor.get('register')}") for idx, sensor in enumerate(self._sensors)}
        schema = vol.Schema(
            {
                vol.Required("sensor_index"): vol.In(list(options.keys()))
            }
        )
        return self.async_show_form(
            step_id="remove",
            data_schema=schema,
            errors=errors,
            description_placeholders={"sensor_list": ", ".join(options.values())},
        )

    async def _async_save(self) -> FlowResult:
        return self.async_create_entry(title="", data={CONF_SENSORS: self._sensors})

    def _normalize_sensor_input(self, user_input: Mapping[str, Any]) -> Dict[str, Any]:
        sensor: Dict[str, Any] = {
            CONF_NAME: user_input[CONF_NAME],
            CONF_REGISTER: user_input[CONF_REGISTER],
            CONF_SCALE: user_input.get(CONF_SCALE, DEFAULT_SCALE),
            CONF_OFFSET: user_input.get(CONF_OFFSET, DEFAULT_OFFSET),
            CONF_FORCE_UPDATE: user_input.get(CONF_FORCE_UPDATE, False),
        }
        unit_id_raw = user_input.get(CONF_UNIT_ID)
        if unit_id_raw:
            sensor[CONF_UNIT_ID] = unit_id_raw
        precision_raw = user_input.get(CONF_PRECISION, "").strip()
        if precision_raw:
            try:
                sensor[CONF_PRECISION] = int(precision_raw)
            except ValueError as err:
                raise vol.Invalid("invalid_precision") from err
        unit = user_input.get(CONF_UNIT, "").strip()
        if unit:
            sensor[CONF_UNIT] = unit
        for key in (CONF_DEVICE_CLASS, CONF_STATE_CLASS, CONF_ICON):
            value = user_input.get(key, "").strip()
            if value:
                sensor[key] = value
        state_map_raw = user_input.get(CONF_STATE_MAP, "").strip()
        if state_map_raw:
            sensor[CONF_STATE_MAP] = self._parse_state_map(state_map_raw)
        return sensor

    def _parse_state_map(self, raw: str) -> Dict[str, str]:
        mapping: Dict[str, str] = {}
        parts = raw.replace("\n", ",").split(",")
        for part in parts:
            chunk = part.strip()
            if not chunk:
                continue
            if "=" not in chunk:
                raise vol.Invalid("invalid_state_map")
            key, value = chunk.split("=", 1)
            mapping[key.strip()] = value.strip()
        return mapping
