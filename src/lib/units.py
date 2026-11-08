from src.log import logger

_SECONDS_PER_HOUR = 3600
_SECONDS_PER_MINUTE = 60


def format_duration(seconds: float | None, /) -> str | None:
    """Segundos → `h:mm:ss` (ou `m:ss` abaixo de uma hora)."""
    if seconds is None or seconds < 0:
        return None
    total = round(seconds)
    hours, rest = divmod(total, _SECONDS_PER_HOUR)
    minutes, secs = divmod(rest, _SECONDS_PER_MINUTE)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def format_pace(speed_mps: float | None, /, *, per_meters: float = 1000) -> str | None:
    """Velocidade em m/s → pace `m:ss` para percorrer `per_meters` metros."""
    if speed_mps is None or speed_mps <= 0:
        return None
    return format_duration(per_meters / speed_mps)


def mps_to_kmh(speed_mps: float | None, /) -> float | None:
    if speed_mps is None:
        return None
    return round(speed_mps * 3.6, 1)


def meters_to_km(meters: float | None, /) -> float | None:
    if meters is None:
        return None
    return round(meters / 1000, 2)


def hours_to_minutes(hours: float | None, /) -> float | None:
    if hours is None:
        return None
    return round(hours * 60, 1)


def round_or_none(value: float | None, /, digits: int = 1) -> float | None:
    if value is None:
        return None
    return round(value, digits)
