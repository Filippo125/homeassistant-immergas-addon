"""Costanti per l'integrazione Modbus Sniffer."""

from homeassistant.const import CONF_NAME

DOMAIN = "modbus_sniffer"
DATA_HUB = "hub"
DATA_LISTENERS = "listeners"

CONF_SENSORS = "sensors"
CONF_REGISTER = "register"
CONF_SCALE = "scale"
CONF_OFFSET = "offset"
CONF_PRECISION = "precision"
CONF_STATE_MAP = "state_map"
CONF_SOURCE_HOST = "source_host"
CONF_UDP_PORT = "udp_port"
CONF_UNIT = "unit_of_measurement"
CONF_DEVICE_CLASS = "device_class"
CONF_STATE_CLASS = "state_class"
CONF_ICON = "icon"
CONF_FORCE_UPDATE = "force_update"
CONF_DEVICE = "device"
CONF_UNIT_ID = "unit_id"
CONF_INCLUDE_DEFAULTS = "include_defaults"
CONF_CONNECTION_MODE = "connection_mode"
CONF_TCP_HOST = "tcp_host"
CONF_TCP_PORT = "tcp_port"
CONF_DEVICE_TYPE = "device_type"

MODE_UDP = "udp"
MODE_TCP = "tcp"

DEVICE_TYPE_IMMERGAS_AUDAX_12 = "immergas_audax_12"

DEVICE_TYPE_LABELS = {
    DEVICE_TYPE_IMMERGAS_AUDAX_12: "IMMERGAS Audax 12",
}

DEFAULT_SOURCE_HOST = "0.0.0.0"
DEFAULT_UDP_PORT = 7777
DEFAULT_TCP_PORT = 502
DEFAULT_SCALE = 1.0
DEFAULT_OFFSET = 0.0
DEFAULT_PRECISION = None
DEFAULT_CONNECTION_MODE = MODE_UDP
DEFAULT_DEVICE_TYPE = DEVICE_TYPE_IMMERGAS_AUDAX_12

SIGNAL_REGISTER_UPDATE = f"{DOMAIN}_register_update"

ATTR_REGISTER = "register"
ATTR_RAW_VALUE = "raw_value"
ATTR_UNIT_ID = "unit_id"
ATTR_LAST_UPDATE = "last_update"

DEFAULT_SENSOR_TEMPLATES_BY_DEVICE = {
    DEVICE_TYPE_IMMERGAS_AUDAX_12: [
        {
            CONF_NAME: "Temperatura esterna",
            CONF_REGISTER: 0x0001,
            CONF_SCALE: 0.1,
            CONF_PRECISION: 1,
            CONF_UNIT: "°C",
            CONF_DEVICE_CLASS: "temperature",
        },
        {
            CONF_NAME: "Temperatura ritorno",
            CONF_REGISTER: 0x0003,
            CONF_SCALE: 0.1,
            CONF_PRECISION: 1,
            CONF_UNIT: "°C",
            CONF_DEVICE_CLASS: "temperature",
        },
        {
            CONF_NAME: "Temperatura mandata",
            CONF_REGISTER: 0x0004,
            CONF_SCALE: 0.1,
            CONF_PRECISION: 1,
            CONF_UNIT: "°C",
            CONF_DEVICE_CLASS: "temperature",
        },
        {
            CONF_NAME: "Temperatura impianto calcolata",
            CONF_REGISTER: 0x0030,
            CONF_SCALE: 0.1,
            CONF_PRECISION: 1,
            CONF_UNIT: "°C",
            CONF_DEVICE_CLASS: "temperature",
        },
        {
            CONF_NAME: "Stato",
            CONF_REGISTER: 0x003F,
            CONF_STATE_MAP: {
                1: "Raffreddamento",
                2: "Riscaldamento",
                21: "OFF",
                22: "Solo circolatore",
            },
        },
        {
            CONF_NAME: "Setpoint mandata",
            CONF_REGISTER: 0x0005,
            CONF_SCALE: 0.1,
            CONF_PRECISION: 1,
            CONF_UNIT: "°C",
            CONF_DEVICE_CLASS: "temperature",
        },
    ],
}
