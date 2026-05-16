"""Runtime-editable app configuration.

Values live in the `app_config` table as TEXT with a `value_type` discriminator
so new keys never need a migration — just an entry in CONFIG_REGISTRY.
"""
from datetime import datetime
from typing import Any, Callable

from sqlalchemy import select
from sqlalchemy.orm import Session

from magsearch.models import ConfigEntry


def _coerce_bool(s: str) -> bool:
    return s.strip().lower() in {"true", "1", "yes", "on"}


_DECODERS: dict[str, Callable[[str], Any]] = {
    "bool": _coerce_bool,
    "int": int,
    "str": str,
}

_ENCODERS: dict[str, Callable[[Any], str]] = {
    "bool": lambda v: "true" if bool(v) else "false",
    "int": lambda v: str(int(v)),
    "str": lambda v: str(v),
}


CONFIG_REGISTRY: dict[str, dict[str, Any]] = {
    "require_login": {
        "type": "bool",
        "default": False,
        "label": "Require login for all pages",
        "help": "When enabled, anonymous visitors are redirected to /login.",
    },
}


def _registry(key: str) -> dict[str, Any]:
    if key not in CONFIG_REGISTRY:
        raise KeyError(f"unknown config key: {key}")
    return CONFIG_REGISTRY[key]


def get_value(db: Session, key: str) -> Any:
    meta = _registry(key)
    row = db.get(ConfigEntry, key)
    if row is None:
        return meta["default"]
    decoder = _DECODERS.get(row.value_type)
    if decoder is None:
        return meta["default"]
    try:
        return decoder(row.value)
    except (ValueError, TypeError):
        return meta["default"]


def set_value(db: Session, key: str, raw_value: Any) -> None:
    meta = _registry(key)
    value_type = meta["type"]
    encoder = _ENCODERS[value_type]
    encoded = encoder(raw_value)
    row = db.get(ConfigEntry, key)
    if row is None:
        db.add(ConfigEntry(
            key=key,
            value=encoded,
            value_type=value_type,
            updated_at=datetime.utcnow(),
        ))
    else:
        row.value = encoded
        row.value_type = value_type
        row.updated_at = datetime.utcnow()


def get_all(db: Session) -> dict[str, Any]:
    return {key: get_value(db, key) for key in CONFIG_REGISTRY}
