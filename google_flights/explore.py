"""Explore: where can I fly from here, and for how much
(GetExploreDestinations).

Date modes (the Explore UI's "Specific dates" / "Flexible dates"):
  * departure_date [+ return_date]      fixed dates (one-way or round trip)
  * month and/or trip_length (no dates)  flexible round trips: Google picks
    the cheapest dates per destination in that month (or the next 6 months)
    for a weekend / one-week / two-week trip.
region limits destinations to a map area; interest (outdoors, beaches,
museums, history, skiing) needs a round trip (Google rejects it one-way).
"""
from datetime import date, timedelta

from google_flights import fetch, parsers, places, protocol
from google_flights.fetch import GoogleFlightsBadRequest, GoogleFlightsNotFound
from google_flights.flights import _leg_options, _locale, _request

TRIP_LENGTH_DAYS = {"weekend": 2, "one_week": 7, "two_weeks": 14}


def _flex_dates(month, trip_length):
    """Placeholder leg dates for a flexible search (Google ignores them in
    favour of the flex window, but the request needs valid future dates)."""
    today = date.today()
    start = today + timedelta(days=14)
    if month:
        year = today.year if month >= today.month else today.year + 1
        first = date(year, month, 1)
        start = max(first, today + timedelta(days=1))
    return start, start + timedelta(days=TRIP_LENGTH_DAYS[trip_length])


def destinations(origin, departure_date=None, return_date=None, month=None, trip_length=None, region=None,
                 interest=None, currency=None, language=None, country=None, **kw):
    """/google-flights/explore/destinations"""
    locale = _locale(currency, language, country)
    origins = places.resolve(origin, locale)
    flexible = not departure_date
    if not flexible and (month or trip_length):
        raise GoogleFlightsBadRequest("pass either departure_date (specific dates) or month / trip_length "
                                      "(flexible dates), not both")
    options = _leg_options(kw)
    if flexible:
        trip_length = trip_length or "one_week"
        dep, ret = _flex_dates(month, trip_length)
        legs = [protocol.leg(origins, None, dep.isoformat(), **options),
                protocol.leg(None, origins, ret.isoformat(), **options)]
        req = _request(protocol.TRIP_ROUND, legs, kw,
                       flex=[month or 0, protocol.EXPLORE_TRIP_LENGTHS[trip_length]], interest=interest)
        body = protocol.explore_body(req, region, round_trip=True, tail=4 if interest else 2)
        round_trip = True
    else:
        round_trip = bool(return_date)
        if interest and not round_trip:
            raise GoogleFlightsBadRequest("interest needs a round trip: add return_date, or use month / trip_length")
        legs = [protocol.leg(origins, None, departure_date, **options)]
        if round_trip:
            legs.append(protocol.leg(None, origins, return_date, **options))
        req = _request(protocol.TRIP_ROUND if round_trip else protocol.TRIP_ONE_WAY, legs, kw, interest=interest)
        body = protocol.explore_body(req, region, round_trip=round_trip, tail=4 if interest else None)
    chunks = fetch.rpc("GetExploreDestinations", body, locale)
    results = parsers.parse_explore(chunks, locale["currency"])
    if not results:
        raise GoogleFlightsNotFound("Google Flights Explore found no destinations for this search")
    return {
        "search": {
            "origin": [o["id"] for o in origins],
            "trip_type": "round_trip" if round_trip else "one_way",
            "dates": "flexible" if flexible else "specific",
            "departure_date": departure_date,
            "return_date": return_date,
            "month": month if flexible else None,
            "trip_length": trip_length if flexible else None,
            "map_bounds": parsers.explore_bounds(chunks),
            "currency": locale["currency"],
        },
        "count": len(results),
        "destinations": results,
    }
