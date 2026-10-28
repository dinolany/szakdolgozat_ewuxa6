import json
from pathlib import Path
from typing import Any, Dict, Iterable


# A paraméterek két szekciója diagnosztikánként
THRESHOLDS = "thresholds"
WINDOW = "window"
SECTIONS = (THRESHOLDS, WINDOW)
ENABLED = "enabled"
REVIEW = "review"   # feladat (issue) csak jóváhagyással: a jelzések a tanuló körbe mennek


class ThresholdManager:
    """A regisztrált diagnosztikák küszöbeinek, mérésiablak-paramétereinek és engedélyezésének tárolója.

    Minden szekció diagnosztikánként (config_key) tárolódik:
        config        = {config_key: {threshold_key: érték}}
        window_config = {config_key: {window_key: érték}}
        enabled       = {config_key: bool}
    """

    def __init__(
        self,
        diagnostics: Iterable[Any] = (),
        custom_config: Dict[str, Dict[str, Any]] | None = None,
        storage_path: str | None = None,
    ):
        self.diagnostics = list(diagnostics)
        self.storage_path = Path(storage_path) if storage_path else None
        self.definitions = {d.config_key: list(d.threshold_definitions()) for d in self.diagnostics}
        self.window_definitions = {d.config_key: list(d.window_definitions()) for d in self.diagnostics}
        self.config: Dict[str, Dict[str, Any]] = self._defaults(self.definitions)
        self.window_config: Dict[str, Dict[str, Any]] = self._defaults(self.window_definitions)
        self.enabled: Dict[str, bool] = {d.config_key: True for d in self.diagnostics}
        self.review: Dict[str, bool] = {d.config_key: bool(getattr(d, "review_by_default", False)) for d in self.diagnostics}
        self._merge(self.config, custom_config or {})
        if self.storage_path:
            self.load()

    @staticmethod
    def _defaults(definitions: Dict[str, list]) -> Dict[str, Dict[str, Any]]:
        return {
            config_key: {definition.key: definition.default for definition in items}
            for config_key, items in definitions.items()
        }

    @staticmethod
    def _merge(target: Dict[str, Dict[str, Any]], values: Dict[str, Dict[str, Any]]) -> None:
        for config_key, section_values in values.items():
            if config_key not in target or not isinstance(section_values, dict):
                continue
            for key, value in section_values.items():
                if key in target[config_key]:
                    target[config_key][key] = value

    def _section(self, section: str):
        if section == THRESHOLDS:
            return self.definitions, self.config
        if section == WINDOW:
            return self.window_definitions, self.window_config
        raise KeyError(f"Ismeretlen paraméterszekció: {section}")

    def get_thresholds(self, config_key: str) -> Dict[str, Any]:
        return self.config.get(config_key, {}).copy()

    def update_param(self, section: str, config_key: str, key: str, value: Any) -> bool:
        """Egy paraméter módosítása; True, ha az érték ténylegesen megváltozott."""
        definitions, values = self._section(section)
        definition = next((d for d in definitions.get(config_key, []) if d.key == key), None)
        if definition is None:
            raise KeyError(f"Ismeretlen paraméter: {section}.{config_key}.{key}")
        new_value = definition.coerce(value)
        if values[config_key].get(key) == new_value:
            return False
        values[config_key][key] = new_value
        self.save()
        return True

    def update_threshold(self, config_key: str, key: str, value: Any) -> bool:
        return self.update_param(THRESHOLDS, config_key, key, value)

    def is_enabled(self, config_key: str) -> bool:
        return self.enabled.get(config_key, True)

    @property
    def disabled(self) -> frozenset[str]:
        return frozenset(key for key, value in self.enabled.items() if not value)

    def set_enabled(self, config_key: str, enabled: bool) -> bool:
        """Diagnosztika be- vagy kikapcsolása; True, ha az állapot ténylegesen megváltozott."""
        if config_key not in self.enabled:
            raise KeyError(f"Ismeretlen diagnosztika: {config_key}")
        if self.enabled[config_key] == bool(enabled):
            return False
        self.enabled[config_key] = bool(enabled)
        self.save()
        return True

    @property
    def review_keys(self) -> frozenset[str]:
        """A jóváhagyásos módú diagnosztikák (config_key)."""
        return frozenset(key for key, value in self.review.items() if value)

    def set_review(self, config_key: str, review: bool) -> bool:
        """Jóváhagyásos mód be/ki; True, ha ténylegesen változott."""
        if config_key not in self.review:
            raise KeyError(f"Ismeretlen diagnosztika: {config_key}")
        if self.review[config_key] == bool(review):
            return False
        self.review[config_key] = bool(review)
        self.save()
        return True

    def get_ui_schema(self) -> Dict[str, Dict[str, Any]]:
        """Diagnosztikánként: azonosítók, ablaktípus, és a két paraméterszekció értékekkel."""
        schema = {}
        for diagnostic in self.diagnostics:
            config_key = diagnostic.config_key
            schema[config_key] = {
                "diagnostic_key": diagnostic.key,
                "family": diagnostic.family,
                "window_type": str(diagnostic.window_type),
                ENABLED: self.is_enabled(config_key),
                REVIEW: self.review.get(config_key, False),
                THRESHOLDS: self._section_schema(self.definitions[config_key], self.config[config_key]),
                WINDOW: self._section_schema(self.window_definitions[config_key], self.window_config[config_key]),
            }
        return schema

    @staticmethod
    def _section_schema(definitions: list, values: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
        return {
            definition.key: {**definition.to_dict(), "value": values[definition.key]}
            for definition in definitions
        }

    def load(self) -> None:
        if not self.storage_path or not self.storage_path.exists():
            return
        try:
            stored = json.loads(self.storage_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        if not isinstance(stored, dict):
            return
        if THRESHOLDS in stored or WINDOW in stored:
            self._merge(self.config, stored.get(THRESHOLDS) or {})
            self._merge(self.window_config, stored.get(WINDOW) or {})
            for config_key, value in (stored.get(ENABLED) or {}).items():
                if config_key in self.enabled:
                    self.enabled[config_key] = bool(value)
            for config_key, value in (stored.get(REVIEW) or {}).items():
                if config_key in self.review:
                    self.review[config_key] = bool(value)
        else:
            self._merge(self.config, stored)  # régi, csak küszöböket tartalmazó formátum

    def save(self) -> None:
        if not self.storage_path:
            return
        self.storage_path.parent.mkdir(parents=True, exist_ok=True)
        self.storage_path.write_text(
            json.dumps(
                {THRESHOLDS: self.config, WINDOW: self.window_config, ENABLED: self.enabled, REVIEW: self.review},
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
