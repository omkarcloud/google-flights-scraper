"""Airport / city search (Google Flights' "Where from?" autocomplete, the
open batchexecute RPC H028ib), free-text place resolution for the other
endpoints, and the reference lists (currencies, languages, countries)."""
import threading
import time
from functools import lru_cache

from google_flights import fetch, parsers
from google_flights.fetch import GoogleFlightsNotFound

AUTOCOMPLETE_RPC = "H028ib"
REFERENCE_TTL = 24 * 3600
_reference = {"at": 0.0, "data": None}
_reference_lock = threading.Lock()


def locale_of(language=None, country=None, currency=None):
    return {"language": language or "en-US", "country": country or "US", "currency": currency or "USD"}


def _autocomplete(query, locale):
    payload = fetch.batchexecute(AUTOCOMPLETE_RPC, [query, [1, 2, 3, 5, 4], None, [1, 1, 1], 1], locale)
    return parsers.parse_autocomplete(payload)


def search(query, language=None, country=None):
    """/google-flights/airports/search — cities (with their airports and
    distances), airports, regions and train stations matching `query`. Each
    result's `id` is accepted by every origin / destination param."""
    locale = locale_of(language, country)
    places = _autocomplete(query, locale)
    return {"query": query, "count": len(places), "places": places}


@lru_cache(maxsize=2048)
def _resolve_text(text, language):
    places = _autocomplete(text, locale_of(language))
    for p in places:
        if p["type"] in ("city", "region") and p["id"]:
            return p["id"], 4, p["name"]
        if p["type"] == "airport" and p["airport_code"]:
            return p["airport_code"], 0, p["name"]
    raise GoogleFlightsNotFound(f"no airport or city matches {text!r} (try /google-flights/airports/search)")


def resolve(specs, locale=None):
    """[{'id','kind'} | {'query': text}] -> [{'id','kind'}] (free text
    resolved through autocomplete, cached)."""
    language = (locale or {}).get("language") or "en-US"
    out = []
    for spec in specs:
        if "query" in spec:
            pid, kind, _ = _resolve_text(spec["query"].strip().lower(), language)
            spec = {"id": pid, "kind": kind}
        if spec not in out:
            out.append(spec)
    return out


def _reference_lists():
    with _reference_lock:
        if _reference["data"] and time.monotonic() - _reference["at"] < REFERENCE_TTL:
            return _reference["data"]
    _, html = fetch.page("/travel/flights")
    data = parsers.reference_lists(fetch.af_blobs(html))
    if not all(data.values()):
        raise fetch.GoogleFlightsUpstreamError("reference lists missing from the Google Flights page")
    with _reference_lock:
        _reference.update(at=time.monotonic(), data=data)
    return data


def currencies():
    rows = _reference_lists()["currencies"]
    return {"count": len(rows), "currencies": rows}


def languages():
    rows = _reference_lists()["languages"]
    return {"count": len(rows), "languages": rows}


def countries():
    rows = _reference_lists()["countries"]
    return {"count": len(rows), "countries": rows}
