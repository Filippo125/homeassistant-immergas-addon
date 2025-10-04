"""Inizializzazione dell'integrazione Modbus Sniffer."""
from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_NAME
from homeassistant.core import HomeAssistant
from homeassistant.helpers.typing import ConfigType

from .const import (
    CONF_CONNECTION_MODE,
    CONF_DEVICE_TYPE,
    CONF_SOURCE_HOST,
    CONF_UDP_PORT,
    DATA_HUB,
    DATA_LISTENERS,
    DEFAULT_CONNECTION_MODE,
    DEFAULT_DEVICE_TYPE,
    DEFAULT_SOURCE_HOST,
    DEFAULT_UDP_PORT,
    DOMAIN,
)

PLATFORMS: list[str] = ["sensor"]


def _ensure_domain_data(hass: HomeAssistant) -> dict:
    data = hass.data.setdefault(DOMAIN, {})
    data.setdefault(DATA_HUB, {})
    data.setdefault(DATA_LISTENERS, {})
    return data


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Configurazione dell'integrazione via YAML."""
    _ensure_domain_data(hass)
    return True


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Gestisce l'aggiornamento delle config entry alle nuove versioni."""

    if entry.version >= 2:
        return True

    data = dict(entry.data)
    data.setdefault(CONF_CONNECTION_MODE, DEFAULT_CONNECTION_MODE)
    data.setdefault(CONF_DEVICE_TYPE, DEFAULT_DEVICE_TYPE)
    if not data.get(CONF_NAME):
        fallback_name = entry.title or f"{data.get(CONF_SOURCE_HOST, DEFAULT_SOURCE_HOST)}:{data.get(CONF_UDP_PORT, DEFAULT_UDP_PORT)}"
        data[CONF_NAME] = fallback_name

    hass.config_entries.async_update_entry(entry, data=data, version=2)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Configura un'istanza tramite interfaccia UI."""
    _ensure_domain_data(hass)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Rimuove una config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    return unload_ok
