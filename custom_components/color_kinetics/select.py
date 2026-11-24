"""Saved-look, palette, and direction selectors."""

from __future__ import annotations

from homeassistant.components.select import SelectEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from .effects import (
    DEFAULT_EFFECT_DIRECTION,
    DEFAULT_EFFECT_PALETTE,
    DIRECTIONS,
    PALETTE_NAMES,
    normalize_direction,
    normalize_palette,
)
from .hub import ColorKineticsConfigEntry, ColorKineticsHub
from .properties import SUFFIX_EFFECT_DIRECTION, SUFFIX_EFFECT_PALETTE, SUFFIX_LOOK


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ColorKineticsConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add look, palette, and direction selectors."""
    hub = entry.runtime_data
    async_add_entities(LookSelect(hub, host) for host in hub.hosts)
    styles: dict[str, EffectPaletteSelect | EffectDirectionSelect] = {}

    def _sync() -> None:
        added: list[EffectPaletteSelect | EffectDirectionSelect] = []
        for owner, kind in _owners(hub):
            for factory, suffix, registry in (
                (EffectPaletteSelect, SUFFIX_EFFECT_PALETTE, hub.palette_entities),
                (EffectDirectionSelect, SUFFIX_EFFECT_DIRECTION, hub.direction_entities),
            ):
                unique_id = f"{owner}:{suffix}"
                entity = styles.get(unique_id)
                if entity is None:
                    entity = factory(hub, owner, unique_id, kind)
                    styles[unique_id] = entity
                    added.append(entity)
                elif entity.hass is not None:
                    entity.refresh()
                registry[owner] = entity
        if added:
            async_add_entities(added)

    entry.async_on_unload(hub.add_listener(_sync))
    _sync()


def _owners(hub: ColorKineticsHub) -> list[tuple[str, str]]:
    owners = [(hub.all_fixtures_owner(host), "supply") for host in hub.hosts]
    owners.extend((hub.group_owner(group["id"]), "group") for group in hub.light_groups())
    return owners


class LookSelect(SelectEntity):
    """Recall a saved look. The options are the look names."""

    _attr_has_entity_name = True
    _attr_translation_key = SUFFIX_LOOK
    _attr_icon = "mdi:palette"
    _attr_should_poll = False
    _attr_options: list[str]
    _attr_current_option: str | None = None

    def __init__(self, hub: ColorKineticsHub, host: str) -> None:
        """Bind the selector to one supply. Looks themselves are per entry."""
        self._hub = hub
        self._host = host
        self._unsub = None
        self._attr_unique_id = f"{hub.supply_identifier(host)}:{SUFFIX_LOOK}"
        self._attr_device_info = hub.supply_device_info(host)
        self._attr_options = hub.look_names()
        self._attr_current_option = hub.selected_look

    async def async_added_to_hass(self) -> None:
        """Follow saves, recalls, and deletes."""
        await super().async_added_to_hass()
        self._unsub = self._hub.add_look_listener(self.refresh)
        self.refresh()

    async def async_will_remove_from_hass(self) -> None:
        """Stop listening."""
        if self._unsub is not None:
            self._unsub()
            self._unsub = None

    def refresh(self) -> None:
        """Publish the current look names and the last recalled name."""
        self._attr_device_info = self._hub.supply_device_info(self._host)
        self._attr_options = self._hub.look_names()
        selected = self._hub.selected_look
        self._attr_current_option = selected if selected in self._attr_options else None
        if self.hass is not None:
            self.async_write_ha_state()

    async def async_select_option(self, option: str) -> None:
        """Recall the named look."""
        await self._hub.async_recall_look(option)
        self.refresh()


class _EffectChoice(RestoreEntity, SelectEntity):
    """A restored select on the Data Enabler or on a named group."""

    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_entity_category = EntityCategory.CONFIG
    _attr_options: list[str]
    _attr_current_option: str | None = None
    _suffix = ""
    _icon = "mdi:tune"
    _group_label = "effect"

    def __init__(
        self, hub: ColorKineticsHub, owner: str, unique_id: str, kind: str
    ) -> None:
        """Bind the selector to one effect owner."""
        self._hub = hub
        self._owner = owner
        self._kind = kind
        self._attr_unique_id = unique_id
        self._attr_icon = self._icon
        self._attr_options = list(self._options())
        self._attr_current_option = self._read()
        self._apply_identity()

    def _write(self, option: str) -> None:
        """Store the choice on the hub."""

    def _apply_identity(self) -> None:
        if self._kind == "supply":
            host = next(
                (
                    host
                    for host in self._hub.hosts
                    if self._hub.all_fixtures_owner(host) == self._owner
                ),
                self._hub.hosts[0] if self._hub.hosts else "",
            )
            self._attr_translation_key = self._suffix
            if host:
                self._attr_device_info = self._hub.supply_device_info(host)
            return
        group_id = self._owner.split(":", 1)[1]
        group = next((item for item in self._hub.light_groups() if item["id"] == group_id), None)
        name = group["name"] if group else "Group"
        self._attr_translation_key = None
        self._attr_name = f"{name} {self._group_label}"
        if group is not None:
            self._attr_device_info = self._hub.group_device_info(group)

    async def async_added_to_hass(self) -> None:
        """Restore the last selection."""
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        if last is not None and last.state in self._attr_options:
            self._attr_current_option = last.state
            self._write(last.state)

    async def async_select_option(self, option: str) -> None:
        """Store the choice. A running effect uses it on the next frame."""
        self._attr_current_option = option
        self._write(option)
        self.async_write_ha_state()

    def apply_external(self, option: str) -> None:
        """Show a choice restored with a look."""
        self._attr_current_option = option if option in self._attr_options else self._read()
        self._write(self._attr_current_option or self._read())
        if self.hass is not None:
            self.async_write_ha_state()

    def refresh(self) -> None:
        """Follow a renamed group or a replaced supply device."""
        self._apply_identity()
        if self.hass is not None:
            self.async_write_ha_state()


class EffectPaletteSelect(_EffectChoice):
    """Palette used by effects that read one."""

    _suffix = SUFFIX_EFFECT_PALETTE
    _icon = "mdi:palette-swatch"
    _group_label = "effect palette"

    def _options(self) -> tuple[str, ...]:
        return PALETTE_NAMES

    def _read(self) -> str:
        return normalize_palette(self._hub.effect_palette(self._owner) or DEFAULT_EFFECT_PALETTE)

    def _write(self, option: str) -> None:
        self._hub.set_effect_palette(self._owner, option)


class EffectDirectionSelect(_EffectChoice):
    """Forward or reverse along the fixture order."""

    _suffix = SUFFIX_EFFECT_DIRECTION
    _icon = "mdi:arrow-left-right"
    _group_label = "effect direction"

    def _options(self) -> tuple[str, ...]:
        return DIRECTIONS

    def _read(self) -> str:
        return normalize_direction(
            self._hub.effect_direction(self._owner) or DEFAULT_EFFECT_DIRECTION
        )

    def _write(self, option: str) -> None:
        self._hub.set_effect_direction(self._owner, option)
