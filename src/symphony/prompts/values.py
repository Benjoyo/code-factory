from __future__ import annotations

from dataclasses import asdict, is_dataclass
from datetime import date, datetime, time
from typing import Any, cast


def to_liquid_value(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat().replace("+00:00", "Z")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, time):
        return value.isoformat()
    if is_dataclass(value) and not isinstance(value, type):
        dataclass_value = cast(Any, value)
        return {
            key: to_liquid_value(nested)
            for key, nested in asdict(dataclass_value).items()
        }
    if isinstance(value, dict):
        return {str(key): to_liquid_value(nested) for key, nested in value.items()}
    if isinstance(value, list | tuple):
        return [to_liquid_value(item) for item in value]
    return value
