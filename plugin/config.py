"""Config read/write with ruamel.yaml round-trip + validators."""
import os
from pathlib import Path
from typing import Any

from ruamel.yaml import YAML

QUERIES_YAML = Path.home() / ".agents" / "skills" / "job-watcher" / "config" / "queries.yaml"

yaml = YAML()
yaml.preserve_quotes = True
yaml.indent(mapping=2, sequence=4, offset=2)
yaml.best_width = 4096  # prevent re-wrapping hand-aligned flow mappings


class ValidationError(ValueError):
    pass


EDITABLE_KEYS = {
    "min_score": "int",
    "hours_old": "int",
    "workmode_blacklist": "list",
    "title_must_not_match": "list",
    "company_must_not_match": "list",
    "queries": "list",
}

DEFAULTS = {
    "min_score": 40,
    "hours_old": 168,
    "workmode_blacklist": ["abroad"],
    "title_must_not_match": [],
    "company_must_not_match": [],
    "queries": [],
}

VALID_WORKMODES = {"onsite", "remote", "abroad"}


def validate_value(key: str, value: Any) -> Any:
    """Validate + coerce a single value or list entry. Raises ValidationError."""
    if key not in EDITABLE_KEYS:
        raise ValidationError(f"{key} is not editable from Discord. Read-only keys must be edited in queries.yaml directly.")

    if key == "min_score":
        try:
            v = int(value)
        except (ValueError, TypeError):
            raise ValidationError(f"min_score must be an integer 0-100. Got: \"{value}\"")
        if not (0 <= v <= 100):
            raise ValidationError(f"min_score must be 0-100. Got: {v}")
        return v

    if key == "hours_old":
        try:
            v = int(value)
        except (ValueError, TypeError):
            raise ValidationError(f"hours_old must be an integer 1-720. Got: \"{value}\"")
        if not (1 <= v <= 720):
            raise ValidationError(f"hours_old must be 1-720 (hours). Got: {v}")
        return v

    if key == "workmode_blacklist":
        s = str(value).strip().lower()
        if s not in VALID_WORKMODES:
            raise ValidationError(f"Invalid workmode \"{value}\". Valid: onsite, remote, abroad.")
        return s

    if key in ("title_must_not_match", "company_must_not_match"):
        s = str(value).strip()
        if not s:
            raise ValidationError(f"{key} entries must be non-empty.")
        if len(s) > 50:
            raise ValidationError(f"{key} entries max 50 chars. Got {len(s)}.")
        return s

    if key == "queries":
        s = str(value).strip()
        if not s:
            raise ValidationError("queries entries must be non-empty.")
        if len(s) > 100:
            raise ValidationError(f"queries entries max 100 chars. Got {len(s)}.")
        return s

    raise ValidationError(f"No validator for {key}")


def load() -> dict:
    """Load queries.yaml preserving comments + order."""
    with open(QUERIES_YAML, "r") as f:
        return yaml.load(f)


def save_atomic(data: dict) -> None:
    """Write queries.yaml atomically via temp + rename."""
    tmp = QUERIES_YAML.with_suffix(".yaml.tmp")
    with open(tmp, "w") as f:
        yaml.dump(data, f)
    os.rename(tmp, QUERIES_YAML)


def get_value(key: str) -> Any:
    """Return current value of a key, or None if missing."""
    return load().get(key)


def set_value(key: str, new_value: Any) -> tuple[Any, Any]:
    """Set a scalar value. For lists, this REPLACES the whole list. Returns (old, new)."""
    if key not in EDITABLE_KEYS:
        raise ValidationError(f"{key} is not editable from Discord.")
    data = load()
    old = data.get(key)
    if EDITABLE_KEYS[key] == "int":
        validated = validate_value(key, new_value)
    else:
        if not isinstance(new_value, (list, tuple)):
            raise ValidationError(f"{key} expects a list. Use !config add/rm for individual entries.")
        validated = [validate_value(key, x) for x in new_value]
        if key == "queries" and len(validated) == 0:
            raise ValidationError("queries must have at least 1 entry.")
    data[key] = validated
    save_atomic(data)
    return old, validated


def list_add(key: str, entry: Any) -> tuple[list, list]:
    """Append entry to a list config. Returns (old_list, new_list)."""
    if EDITABLE_KEYS.get(key) != "list":
        raise ValidationError(f"{key} is not a list config.")
    validated = validate_value(key, entry)
    data = load()
    old = list(data.get(key, []))
    if validated in old:
        raise ValidationError(f"{validated} already in {key}.")
    new = old + [validated]
    data[key] = new
    save_atomic(data)
    return old, new


def list_rm(key: str, entry: Any) -> tuple[list, list]:
    """Remove entry from a list config. Returns (old_list, new_list)."""
    if EDITABLE_KEYS.get(key) != "list":
        raise ValidationError(f"{key} is not a list config.")
    validated = validate_value(key, entry)
    data = load()
    old = list(data.get(key, []))
    if validated not in old:
        raise ValidationError(f"{validated} not in {key}.")
    new = [x for x in old if x != validated]
    if key == "queries" and len(new) == 0:
        raise ValidationError("Cannot remove last query (queries must have at least 1 entry).")
    data[key] = new
    save_atomic(data)
    return old, new


def reset(key: str) -> tuple[Any, Any]:
    """Reset a key to its default value."""
    if key not in EDITABLE_KEYS:
        raise ValidationError(f"{key} is not editable.")
    data = load()
    old = data.get(key)
    data[key] = DEFAULTS[key]
    save_atomic(data)
    return old, DEFAULTS[key]
