"""Price calendar (cheapest fare per departure date, GetCalendarGraph) and
the round-trip date grid (departure x return, GetCalendarGrid).

Limits measured 2026-09-26: the calendar accepts up to 92 days per call
(six months is rejected); round trips need a fixed trip length. The date
grid accepts a 7 x 7 window (a 15 x 15 window is rejected). Measured
2026-11-28: a calendar or grid window that ends past today +
MAX_DAYS_AHEAD is refused with [3] (reason 3, too far ahead), so both end
their windows on that day at the latest."""
from datetime import date, timedelta

from google_flights import fetch, parsers, places, protocol
from google_flights.flights import _leg_options, _locale, _request
from google_flights.fetch import GoogleFlightsNotFound
from google_flights.shared import MAX_DAYS_AHEAD

GRID_HALF_WINDOW = 3


def _last_day():
    return date.today() + timedelta(days=MAX_DAYS_AHEAD)


def _window(start, floor, last):
    """A 2 * GRID_HALF_WINDOW + 1 day window from `start` (never before
    `floor`), moved back so it ends on `last` at the latest."""
    width = timedelta(days=2 * GRID_HALF_WINDOW)
    start = max(start, floor)
    if start + width > last:
        start = max(floor, last - width)
    return start, min(start + width, last)


def _cheapest(points):
    priced = [p for p in points if p["price"] is not None]
    return min(priced, key=lambda p: p["price"]) if priced else None


def calendar(origin, destination, start_date, end_date=None, trip_length_days=None, currency=None, language=None,
             country=None, **kw):
    """/google-flights/prices/calendar — the cheapest fare for every
    departure date in [start_date, end_date]; with trip_length_days the
    fares are round trips returning that many days later."""
    locale = _locale(currency, language, country)
    start = date.fromisoformat(start_date)
    end = date.fromisoformat(end_date) if end_date else min(start + timedelta(days=60), _last_day())
    origins, destinations = places.resolve(origin, locale), places.resolve(destination, locale)
    legs = [protocol.leg(origins, destinations, start.isoformat(), **_leg_options(kw))]
    trip = protocol.TRIP_ONE_WAY
    if trip_length_days:
        trip = protocol.TRIP_ROUND
        legs.append(protocol.leg(destinations, origins, (start + timedelta(days=trip_length_days)).isoformat(),
                                 **_leg_options(kw)))
    req = _request(trip, legs, kw)
    chunks = fetch.rpc("GetCalendarGraph",
                       protocol.calendar_graph_body(req, start.isoformat(), end.isoformat(), trip_length_days), locale)
    points = parsers.calendar_points(chunks[0], locale["currency"])
    if not points:
        raise GoogleFlightsNotFound("Google Flights has no fares for this route in that date range")
    return {
        "search": {"trip_type": "round_trip" if trip_length_days else "one_way",
                   "origin": [o["id"] for o in origins], "destination": [d["id"] for d in destinations],
                   "start_date": start.isoformat(), "end_date": end.isoformat(),
                   "trip_length_days": trip_length_days, "cabin_class": kw.get("cabin_class") or "economy",
                   "currency": locale["currency"]},
        "cheapest": _cheapest(points),
        "count": len(points),
        "prices": points,
    }


def date_grid(origin, destination, departure_date, return_date, currency=None, language=None, country=None, **kw):
    """/google-flights/prices/date-grid — round-trip fares for every
    departure x return combination within 3 days of the given dates."""
    locale = _locale(currency, language, country)
    dep, ret = date.fromisoformat(departure_date), date.fromisoformat(return_date)
    today, last = date.today(), _last_day()
    half = timedelta(days=GRID_HALF_WINDOW)
    dep_from, dep_to = _window(dep - half, today, last)
    ret_from, ret_to = _window(ret - half, dep_from, last)
    origins, destinations = places.resolve(origin, locale), places.resolve(destination, locale)
    legs = [protocol.leg(origins, destinations, departure_date, **_leg_options(kw)),
            protocol.leg(destinations, origins, return_date, **_leg_options(kw))]
    req = _request(protocol.TRIP_ROUND, legs, kw)
    chunks = fetch.rpc("GetCalendarGrid",
                       protocol.date_grid_body(req, [dep_from.isoformat(), dep_to.isoformat()],
                                               [ret_from.isoformat(), ret_to.isoformat()]), locale)
    points = [p for p in parsers.calendar_points(chunks[0], locale["currency"]) if p["return_date"]]
    if not points:
        raise GoogleFlightsNotFound("Google Flights has no round-trip fares around those dates")
    return {
        "search": {"trip_type": "round_trip", "origin": [o["id"] for o in origins],
                   "destination": [d["id"] for d in destinations],
                   "departure_dates": {"from": dep_from.isoformat(), "to": dep_to.isoformat()},
                   "return_dates": {"from": ret_from.isoformat(), "to": ret_to.isoformat()},
                   "cabin_class": kw.get("cabin_class") or "economy", "currency": locale["currency"]},
        "cheapest": _cheapest(points),
        "count": len(points),
        "prices": points,
    }
