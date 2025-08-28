# custom_components/bacnet_hub/config_flow.py
from __future__ import annotations

from typing import Any, Dict, List, Optional
import logging
import re
import socket
import voluptuous as vol

from homeassistant.core import callback, HomeAssistant, State
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.helpers import selector as sel

_LOGGER = logging.getLogger(__name__)

try:
    from .const import DOMAIN  # type: ignore
except Exception:
    DOMAIN = "bacnet_hub"

# Limits / Defaults
DEFAULT_INSTANCE = 8123
MIN_INSTANCE = 0
MAX_INSTANCE = 4_194_302  # 4194302
DEFAULT_PREFIX = 24
DEFAULT_PORT = 47808

# -------------------- Helpers: Parsing & Validation --------------------

def _as_int(v, fb: int) -> int:
    try:
        return int(v)
    except Exception:
        return fb

_addr_re = re.compile(
    r"^\s*(?P<ip>(\d{1,3}\.){3}\d{1,3})"
    r"(?:/(?P<prefix>\d{1,2}))?"
    r"(?::(?P<port>\d{1,5}))?\s*$"
)

def _detect_local_ip() -> Optional[str]:
    """Ermittle bevorzugte IPv4 des Hosts (z. B. 192.168.x.x)."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        # Ziel egal, wird nicht gesendet – dient nur zur Routenwahl.
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        # sanity check
        socket.inet_aton(ip)
        return ip
    except Exception as e:
        _LOGGER.debug("Could not detect local IP: %s", e)
        return None

def _default_address() -> str:
    ip = _detect_local_ip() or "0.0.0.0"
    return f"{ip}/{DEFAULT_PREFIX}:{DEFAULT_PORT}"

def _validate_bacnet_address(addr: str) -> Optional[str]:
    """Valide IPv4[ /prefix ][ :port ]. Gibt None bei OK, sonst Fehlercode."""
    if not addr or not isinstance(addr, str):
        return "invalid_address"
    m = _addr_re.match(addr)
    if not m:
        return "invalid_address"

    ip = m.group("ip")
    try:
        parts = [int(x) for x in ip.split(".")]
        if any(p < 0 or p > 255 for p in parts):
            return "invalid_address"
    except Exception:
        return "invalid_address"

    prefix = m.group("prefix")
    if prefix is not None:
        try:
            pr = int(prefix)
            if pr < 0 or pr > 32:
                return "invalid_address"
        except Exception:
            return "invalid_address"

    port = m.group("port")
    if port is not None:
        try:
            pt = int(port)
            if pt < 1 or pt > 65535:
                return "invalid_address"
        except Exception:
            return "invalid_address"

    return None

def _entity_friendly_name(hass: HomeAssistant, entity_id: str) -> str:
    """Liefert den im Frontend sichtbaren Anzeigenamen."""
    st = hass.states.get(entity_id)
    if not st:
        return entity_id
    # HA >= 2024: State.name ist bereits der Human Name (fällt auf friendly_name zurück)
    if getattr(st, "name", None):
        return str(st.name)
    return str(st.attributes.get("friendly_name") or entity_id)

def _label_for_mapping(hass: HomeAssistant, m: Dict[str, Any]) -> str:
    """Schöne Zeile für die Übersicht im Publish-Menü."""
    ent = m.get("entity_id") or "?"
    obj_type = m.get("object_type", "?")
    inst = m.get("instance", "?")
    writable = " writable" if m.get("writable") else ""
    # Friendly Name: bevorzugt aus Config, sonst live bestimmen
    fr = m.get("friendly_name") or _entity_friendly_name(hass, ent)
    return f"{obj_type}:{inst} ⇐ {ent} ({fr}){writable}"

# -------------------- Heuristik: object_type / units --------------------

_BINARY_DOMAINS = {
    "binary_sensor", "switch", "light", "lock", "cover", "input_boolean",
    "alarm_control_panel", "device_tracker", "button",
}

def _is_numeric_state(state: Optional[State]) -> bool:
    if not state:
        return False
    try:
        float(state.state)  # raises for non-numeric
        return True
    except Exception:
        return False

def _determine_object_type_and_units(hass: HomeAssistant, entity_id: str) -> tuple[str, Optional[str]]:
    """
    Bestimme object_type (analogValue|binaryValue) + units (falls vorhanden)
    aus der Entität.
    Logik:
      - Wenn Domain 'binary' typisch → binaryValue
      - Sonst: hat unit_of_measurement ODER state numerisch → analogValue
      - Fallback → binaryValue
    """
    domain = (entity_id.split(".", 1)[0] if "." in entity_id else "").lower()
    st = hass.states.get(entity_id)

    if domain in _BINARY_DOMAINS:
        return "binaryValue", None

    uom = (st.attributes.get("unit_of_measurement") if st else None) if st else None
    if uom or _is_numeric_state(st):
        return "analogValue", (str(uom) if uom is not None else None)

    # String-Zustände wie "on/off", "open/closed" → binary
    if st:
        s = str(st.state).strip().lower()
        if s in ("on", "off", "open", "closed", "true", "false", "active", "inactive"):
            return "binaryValue", None

    return "binaryValue", None

# -------------------- Config Flow (create entry) --------------------

class BacnetHubConfigFlow(ConfigFlow, domain=DOMAIN):
    """Setup-Flow: fragt 'instance' und 'address' ab und speichert sie in entry.data."""
    VERSION = 1

    async def async_step_user(
        self, user_input: Optional[Dict] = None
    ) -> ConfigFlowResult:
        errors: Dict[str, str] = {}
        placeholders = {
            "min": f"{MIN_INSTANCE}",
            "max": "4194302",
            "default": f"{DEFAULT_INSTANCE}",
            "addr_hint": "z.B. 192.168.31.36/24:47808",
        }

        if user_input is not None:
            # Instance prüfen
            try:
                inst = int(user_input.get("instance", DEFAULT_INSTANCE))
                if inst < MIN_INSTANCE or inst > MAX_INSTANCE:
                    raise ValueError
            except Exception:
                errors["instance"] = "invalid_int"

            # Address prüfen
            addr = (user_input.get("address") or "").strip()
            if not addr:
                addr = _default_address()
            err = _validate_bacnet_address(addr)
            if err:
                errors["address"] = err

            if not errors:
                return self.async_create_entry(
                    title="BACnet - Hub",
                    data={"instance": inst, "address": addr},
                )

        schema = vol.Schema({
            vol.Required("instance", default=DEFAULT_INSTANCE): sel.NumberSelector(
                sel.NumberSelectorConfig(min=MIN_INSTANCE, max=MAX_INSTANCE, step=1, mode=sel.NumberSelectorMode.BOX)
            ),
            vol.Required("address", default=_default_address()): sel.TextSelector(
                sel.TextSelectorConfig(multiline=False, type=sel.TextSelectorType.TEXT)
            ),
        })
        return self.async_show_form(
            step_id="user",
            data_schema=schema,
            errors=errors,
            description_placeholders=placeholders,
        )

    async def async_step_import(self, user_input: Dict) -> ConfigFlowResult:
        return await self.async_step_user(user_input)

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        return BacnetHubOptionsFlow(config_entry)

# -------------------- Options Flow --------------------

class BacnetHubOptionsFlow(OptionsFlow):
    """Options: Menü mit 2 Bereichen: 'device' & 'publish'."""
    def __init__(self, config_entry) -> None:
        self._entry = config_entry
        self._opts: Dict[str, Any] = dict(config_entry.options or {})

    async def async_step_init(self, user_input: Optional[Dict] = None) -> ConfigFlowResult:
        # Menü anzeigen
        return self.async_show_menu(
            step_id="init",
            menu_options=["device", "publish"],
        )

    # ---- Geräteeinstellungen (instance + address) ----
    async def async_step_device(self, user_input: Optional[Dict] = None) -> ConfigFlowResult:
        current_instance = _as_int(
            self._opts.get("instance", self._entry.data.get("instance", DEFAULT_INSTANCE)),
            DEFAULT_INSTANCE,
        )
        current_address = self._opts.get("address") or self._entry.data.get("address") or _default_address()

        placeholders = {
            "min": f"{MIN_INSTANCE}",
            "max": "4194302",
            "current": f"{current_instance}",
            "addr_hint": "z.B. 192.168.31.36/24:47808",
        }

        errors: Dict[str, str] = {}
        if user_input is not None:
            inst = _as_int(user_input.get("instance"), current_instance)
            if inst < MIN_INSTANCE or inst > MAX_INSTANCE:
                errors["instance"] = "invalid_int"

            addr = (user_input.get("address") or "").strip()
            if not addr:
                addr = current_address or _default_address()
            err = _validate_bacnet_address(addr)
            if err:
                errors["address"] = err

            if not errors:
                self._opts["instance"] = inst
                self._opts["address"] = addr
                return self.async_create_entry(title="", data=self._opts)

        schema = vol.Schema({
            vol.Required("instance", default=current_instance): sel.NumberSelector(
                sel.NumberSelectorConfig(min=MIN_INSTANCE, max=MAX_INSTANCE, step=1, mode=sel.NumberSelectorMode.BOX)
            ),
            vol.Required("address", default=current_address): sel.TextSelector(
                sel.TextSelectorConfig(multiline=False, type=sel.TextSelectorType.TEXT)
            ),
        })
        return self.async_show_form(
            step_id="device",
            data_schema=schema,
            errors=errors,
            description_placeholders=placeholders,
        )

    # ---- Publish: Übersicht + Untermenü ----
    async def async_step_publish(self, user_input: Optional[Dict] = None) -> ConfigFlowResult:
        published: List[Dict[str, Any]] = list(self._opts.get("published", []))
        lines: List[str] = []
        for m in published:
            try:
                lines.append(f"- {_label_for_mapping(self.hass, m)}")
            except Exception:
                pass

        desc = "\n".join(lines) if lines else "Keine Mappings vorhanden."
        return self.async_show_menu(
            step_id="publish",
            menu_options=["publish_add", "publish_edit", "publish_delete"],
            description_placeholders={"current_mappings": desc},
        )

    # ---- Publish → Add ----
    async def async_step_publish_add(self, user_input: Optional[Dict] = None) -> ConfigFlowResult:
        errors: Dict[str, str] = {}
        if user_input is not None:
            entity_id: str = user_input.get("entity_id", "")
            writable: bool = bool(user_input.get("writable", False))
            entity_id = (entity_id or "").strip()

            if not entity_id or "." not in entity_id:
                errors["entity_id"] = "invalid_entity"
            else:
                # Typ & Units bestimmen
                obj_type, units = _determine_object_type_and_units(self.hass, entity_id)
                # Friendly Name ermitteln und mitspeichern
                friendly = _entity_friendly_name(self.hass, entity_id)

                # Zähler laden/initialisieren
                counters: Dict[str, int] = dict(self._opts.get("counters", {}))
                next_idx = int(counters.get(obj_type, 0))

                # Mapping erstellen (inkl. friendly_name)
                new_map = {
                    "entity_id": entity_id,
                    "object_type": obj_type,
                    "instance": next_idx,
                    "units": units,
                    "writable": writable,
                    "friendly_name": friendly,   # <-- NEU
                }

                published: List[Dict[str, Any]] = list(self._opts.get("published", []))
                published.append(new_map)
                self._opts["published"] = published

                # Zähler hochzählen (kein Zurücksetzen auf Lücken)
                counters[obj_type] = next_idx + 1
                self._opts["counters"] = counters

                return self.async_create_entry(title="", data=self._opts)

        schema = vol.Schema({
            vol.Required("entity_id"): sel.EntitySelector(sel.EntitySelectorConfig()),  # frei wählbar
            vol.Required("writable", default=False): sel.BooleanSelector(),
        })
        return self.async_show_form(
            step_id="publish_add",
            data_schema=schema,
            errors=errors,
        )

    # ---- Publish → Edit (Auswahl) ----
    async def async_step_publish_edit(self, user_input: Optional[Dict] = None) -> ConfigFlowResult:
        published: List[Dict[str, Any]] = list(self._opts.get("published", []))
        if not published:
            # nichts zu bearbeiten → zurück zur Übersicht
            return await self.async_step_publish()

        # Options für Selector
        options = {str(i): _label_for_mapping(self.hass, m) for i, m in enumerate(published)}

        if user_input is not None and "sel" in user_input:
            idx = str(user_input.get("sel"))
            if idx in options:
                self._opts["_edit_index"] = int(idx)
                return await self.async_step_publish_edit_form()

        schema = vol.Schema({
            vol.Required("sel"): sel.SelectSelector(
                sel.SelectSelectorConfig(
                    options=[sel.SelectOptionDict(value=k, label=v) for k, v in options.items()],
                    mode=sel.SelectSelectorMode.DROPDOWN,
                )
            )
        })
        return self.async_show_form(step_id="publish_edit", data_schema=schema)

    # ---- Publish → Edit (Form) ----
    async def async_step_publish_edit_form(self, user_input: Optional[Dict] = None) -> ConfigFlowResult:
        published: List[Dict[str, Any]] = list(self._opts.get("published", []))
        idx = int(self._opts.get("_edit_index", -1))
        if idx < 0 or idx >= len(published):
            return await self.async_step_publish()

        current = dict(published[idx])
        cur_entity = current.get("entity_id", "")
        cur_writable = bool(current.get("writable", False))
        # Friendly Name ggf. aus Mapping, sonst live bestimmen (Migration alter Einträge)
        cur_friendly = current.get("friendly_name") or _entity_friendly_name(self.hass, cur_entity)

        errors: Dict[str, str] = {}
        if user_input is not None:
            new_entity: str = (user_input.get("entity_id") or "").strip()
            new_writable: bool = bool(user_input.get("writable", False))
            new_friendly: str = (user_input.get("friendly_name") or "").strip()

            if not new_entity or "." not in new_entity:
                errors["entity_id"] = "invalid_entity"
            else:
                # Typ/Instance/Units BEIBEHALTEN (keine Verschiebungen!)
                current["entity_id"] = new_entity
                current["writable"] = new_writable
                # Friendly aktualisieren (falls leer, neu ableiten)
                current["friendly_name"] = new_friendly or _entity_friendly_name(self.hass, new_entity)

                published[idx] = current
                self._opts["published"] = published
                self._opts.pop("_edit_index", None)

                return self.async_create_entry(title="", data=self._opts)

        schema = vol.Schema({
            vol.Required("entity_id", default=cur_entity): sel.EntitySelector(sel.EntitySelectorConfig()),
            vol.Required("writable", default=cur_writable): sel.BooleanSelector(),
            vol.Required("friendly_name", default=cur_friendly): sel.TextSelector(
                sel.TextSelectorConfig(multiline=False, type=sel.TextSelectorType.TEXT)
            ),
        })
        return self.async_show_form(step_id="publish_edit_form", data_schema=schema, errors=errors)

    # ---- Publish → Delete ----
    async def async_step_publish_delete(self, user_input: Optional[Dict] = None) -> ConfigFlowResult:
        published: List[Dict[str, Any]] = list(self._opts.get("published", []))
        if not published:
            return await self.async_step_publish()

        options = {str(i): _label_for_mapping(self.hass, m) for i, m in enumerate(published)}

        if user_input is not None and "sel" in user_input:
            idx = str(user_input.get("sel"))
            if idx in options:
                i = int(idx)
                # Entfernen, Zähler NICHT zurücksetzen (keine Verschiebungen)
                published.pop(i)
                self._opts["published"] = published
                return self.async_create_entry(title="", data=self._opts)

        schema = vol.Schema({
            vol.Required("sel"): sel.SelectSelector(
                sel.SelectSelectorConfig(
                    options=[sel.SelectOptionDict(value=k, label=v) for k, v in options.items()],
                    mode=sel.SelectSelectorMode.DROPDOWN,
                )
            )
        })
        return self.async_show_form(step_id="publish_delete", data_schema=schema)
