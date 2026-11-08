from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from pydantic import JsonValue

from src.log import logger

DEFAULT_TIMEZONE = ZoneInfo("America/Sao_Paulo")


def today() -> date:
    return datetime.now(tz=DEFAULT_TIMEZONE).date()


def split_date_range(
    start: date,
    end: date,
    /,
    *,
    max_days: int,
) -> list[tuple[date, date]]:
    """Quebra [start, end] (inclusivo) em janelas de no máximo `max_days` dias."""
    windows: list[tuple[date, date]] = []
    window_start = start
    while window_start <= end:
        window_end = min(window_start + timedelta(days=max_days - 1), end)
        windows.append((window_start, window_end))
        window_start = window_end + timedelta(days=1)
    return windows


def compact_json(value: JsonValue, /) -> JsonValue:
    """Remove recursivamente `null`, strings vazias, listas e objetos vazios."""
    if isinstance(value, dict):
        compacted = {key: compact_json(item) for key, item in value.items()}
        return {key: item for key, item in compacted.items() if not _is_empty(item)}
    if isinstance(value, list):
        compacted_items = [compact_json(item) for item in value]
        return [item for item in compacted_items if not _is_empty(item)]
    return value


def _is_empty(value: JsonValue) -> bool:
    return value is None or value in ("", [], {})


def downsample[T](items: list[T], /, *, max_items: int) -> list[T]:
    """Pega `max_items` elementos igualmente espaçados (sempre inclui o último)."""
    if max_items <= 0:
        return []
    if len(items) <= max_items:
        return items
    if max_items == 1:
        return [items[-1]]
    step = (len(items) - 1) / (max_items - 1)
    return [items[round(index * step)] for index in range(max_items)]
