"""Value helpers shared by the google_flights parsers.

Google Flights payloads are positional JSON arrays (protobuf-as-JSON), so
every accessor here is defensive: a missing index, a None where a list was
expected or a changed type yields None instead of raising."""
from datetime import datetime, timezone

CABIN_CODES = {"economy": 1, "premium_economy": 2, "business": 3, "first": 4}
CABIN_NAMES = {1: "economy", 2: "premium_economy", 3: "business", 4: "first",
               5: "economy", 6: "economy"}


def at(value, *path):
    """value[p0][p1]... or None when any step is missing / not indexable."""
    for key in path:
        if value is None:
            return None
        try:
            value = value[key]
        except (IndexError, KeyError, TypeError):
            return None
    return value


def as_list(value):
    return value if isinstance(value, list) else []


def as_str(value):
    if value is None or isinstance(value, (bool, list, dict)):
        return None
    text = str(value).strip()
    return text or None


def as_int(value):
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value)
    try:
        return int(str(value).strip())
    except ValueError:
        return None


def as_bool(value):
    return bool(value) if value is not None else None


def price_of(value):
    """[null, 249] (Google's Money: [currency?, units]) -> 249; tolerates the
    bare number and the [[null, 249], token] wrapper."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return value
    if isinstance(value, list):
        if len(value) >= 2 and isinstance(value[1], (int, float)) and not isinstance(value[1], bool):
            return value[1]
        if value and isinstance(value[0], list):
            return price_of(value[0])
    return None


def date_of(value):
    """[2026, 11, 12] -> '2026-11-12'; an ISO string passes through."""
    if isinstance(value, str):
        return value[:10] or None
    if isinstance(value, list) and len(value) >= 3 and all(isinstance(v, int) for v in value[:3]):
        try:
            return datetime(value[0], value[1], value[2]).date().isoformat()
        except ValueError:
            return None
    return None


def time_of(value):
    """[15, 8] -> '15:08'; [7] -> '07:00' (Google drops trailing zero
    minutes); [] (midnight) -> '00:00'; None -> None."""
    if not isinstance(value, list):
        return None
    hour = value[0] if len(value) > 0 and isinstance(value[0], int) else 0
    minute = value[1] if len(value) > 1 and isinstance(value[1], int) else 0
    return f"{hour:02d}:{minute:02d}"


def epoch_ms_date(value):
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    try:
        return datetime.fromtimestamp(value / 1000, tz=timezone.utc).date().isoformat()
    except (OverflowError, OSError, ValueError):
        return None


def minutes_of(hours, minutes):
    """(7, 9) -> 429 minutes; either part may be None."""
    if hours is None and minutes is None:
        return None
    return (as_int(hours) or 0) * 60 + (as_int(minutes) or 0)


def google_link(path):
    """'/travel/flights/...' -> absolute google.com link."""
    path = as_str(path)
    if not path:
        return None
    if path.startswith("http"):
        return path
    return "https://www.google.com" + (path if path.startswith("/") else "/" + path)


def compact(items):
    return [i for i in items if i is not None]
