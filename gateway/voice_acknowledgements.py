"""Profile-scoped, in-memory voice acknowledgement catalog."""

from __future__ import annotations

from dataclasses import dataclass
import logging
from pathlib import Path
import random
from typing import Any, Dict, Iterable, Mapping, Optional, Tuple

import yaml

from hermes_constants import get_hermes_home

logger = logging.getLogger(__name__)

CATALOG_RELATIVE_PATH = Path("voice") / "acknowledgements.yaml"
_SUPPORTED_SCHEMA_VERSION = 2

_BUILTIN_DEFAULTS: Dict[str, Any] = {
    "enabled": True,
    "weight": 1,
    "models": {"include": ["*"], "exclude": []},
    "voice": {"style": None, "stability": None, "speed": None},
}


@dataclass(frozen=True)
class VoiceAcknowledgement:
    """A resolved acknowledgement ready for TTS."""

    text: str
    weight: float
    voice_settings: Dict[str, float]
    include_models: Tuple[str, ...]
    exclude_models: Tuple[str, ...]


def _merge_settings(base: Mapping[str, Any], override: Mapping[str, Any]) -> Dict[str, Any]:
    """Merge acknowledgement metadata with leaf-level nested precedence."""
    merged: Dict[str, Any] = {
        "enabled": base.get("enabled", True),
        "weight": base.get("weight", 1),
        "models": dict(base.get("models") or {}),
        "voice": dict(base.get("voice") or {}),
    }
    for key in ("enabled", "weight"):
        if key in override:
            merged[key] = override[key]
    for key in ("models", "voice"):
        value = override.get(key)
        if isinstance(value, Mapping):
            merged[key].update(value)
    return merged


def _model_names(values: Any, default: Iterable[str]) -> Tuple[str, ...]:
    if isinstance(values, str):
        values = [values]
    if not isinstance(values, (list, tuple, set)):
        values = default
    return tuple(
        name
        for value in values
        if (name := str(value or "").strip())
    )


def _provider_free_model_name(model_name: str) -> str:
    return str(model_name or "").strip().rsplit("/", 1)[-1]


class VoiceAcknowledgementCatalog:
    """Parsed acknowledgement data cached for the lifetime of a gateway adapter."""

    def __init__(self, entries: Optional[Mapping[str, Iterable[VoiceAcknowledgement]]] = None):
        self._entries: Dict[str, Tuple[VoiceAcknowledgement, ...]] = {
            str(event): tuple(items)
            for event, items in (entries or {}).items()
            if items
        }

    def __bool__(self) -> bool:
        return bool(self._entries)

    @classmethod
    def load(cls, path: Optional[Path] = None) -> "VoiceAcknowledgementCatalog":
        """Parse one profile catalog, failing soft to an empty catalog."""
        catalog_path = Path(path) if path is not None else get_hermes_home() / CATALOG_RELATIVE_PATH
        if not catalog_path.is_file():
            logger.debug("Voice acknowledgement catalog not found: %s", catalog_path)
            return cls()
        try:
            raw = yaml.safe_load(catalog_path.read_text(encoding="utf-8")) or {}
            if not isinstance(raw, Mapping):
                raise ValueError("catalog root must be a mapping")
            if raw.get("schema_version") != _SUPPORTED_SCHEMA_VERSION:
                raise ValueError(
                    f"unsupported schema_version {raw.get('schema_version')!r}; "
                    f"expected {_SUPPORTED_SCHEMA_VERSION}"
                )
            acknowledgements = raw.get("acknowledgements")
            if not isinstance(acknowledgements, Mapping):
                raise ValueError("acknowledgements must be a mapping")
            raw_defaults = raw.get("defaults")
            if not isinstance(raw_defaults, Mapping):
                raw_defaults = {}
            file_defaults = _merge_settings(_BUILTIN_DEFAULTS, raw_defaults)
            entries: Dict[str, Tuple[VoiceAcknowledgement, ...]] = {}
            for event_name, event_data in acknowledgements.items():
                parsed = cls._parse_event(file_defaults, event_data)
                if parsed:
                    entries[str(event_name)] = parsed
            return cls(entries)
        except Exception as exc:
            logger.warning(
                "Could not load voice acknowledgement catalog %s: %s",
                catalog_path,
                exc,
            )
            return cls()

    @staticmethod
    def _parse_event(
        file_defaults: Mapping[str, Any],
        event_data: Any,
    ) -> Tuple[VoiceAcknowledgement, ...]:
        if not isinstance(event_data, Mapping):
            return ()
        groups = event_data.get("groups")
        if not isinstance(groups, Mapping):
            return ()
        parsed = []
        for group_data in groups.values():
            if not isinstance(group_data, Mapping):
                continue
            group_settings = _merge_settings(file_defaults, group_data)
            phrases = group_data.get("phrases")
            if not isinstance(phrases, (list, tuple)):
                continue
            for phrase in phrases:
                if isinstance(phrase, str):
                    text = phrase.strip()
                    phrase_settings = group_settings
                elif isinstance(phrase, Mapping):
                    text = str(phrase.get("text") or "").strip()
                    phrase_settings = _merge_settings(group_settings, phrase)
                else:
                    continue
                if not text or not bool(phrase_settings.get("enabled", True)):
                    continue
                try:
                    weight = float(phrase_settings.get("weight", 1))
                except (TypeError, ValueError):
                    continue
                if weight <= 0:
                    continue
                models = phrase_settings.get("models") or {}
                include_models = _model_names(models.get("include"), ("*",))
                exclude_models = _model_names(models.get("exclude"), ())
                voice = phrase_settings.get("voice") or {}
                voice_settings = {}
                for key in ("style", "stability", "speed"):
                    value = voice.get(key)
                    if value is None:
                        continue
                    try:
                        voice_settings[key] = float(value)
                    except (TypeError, ValueError):
                        continue
                parsed.append(
                    VoiceAcknowledgement(
                        text=text,
                        weight=weight,
                        voice_settings=voice_settings,
                        include_models=include_models,
                        exclude_models=exclude_models,
                    )
                )
        return tuple(parsed)

    def eligible(
        self,
        event_name: str,
        *,
        model_name: str = "",
    ) -> Tuple[VoiceAcknowledgement, ...]:
        """Return phrases eligible for the active provider-free LLM name."""
        active_model = _provider_free_model_name(model_name)
        eligible = []
        for entry in self._entries.get(event_name, ()):
            if "*" in entry.exclude_models or active_model in entry.exclude_models:
                continue
            if "*" not in entry.include_models and active_model not in entry.include_models:
                continue
            eligible.append(entry)
        return tuple(eligible)

    def choose(
        self,
        event_name: str,
        *,
        model_name: str = "",
    ) -> Optional[VoiceAcknowledgement]:
        """Choose one weighted eligible phrase without reading the catalog again."""
        eligible = self.eligible(event_name, model_name=model_name)
        if not eligible:
            return None
        return random.choices(
            eligible,
            weights=[entry.weight for entry in eligible],
            k=1,
        )[0]
