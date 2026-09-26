"""Live endpoint smoke tests: one call per /google-flights/* route against a
running service, with example values proven to return data (2026-09-26). The
listing tooling reads each route's FIRST call from this file's AST as its
working example, so the values stay literals (move the dates forward when
they pass).

Skipped unless GOOGLE_FLIGHTS_BASE points at a running service:

    ONLY_SCRAPER=google-flights SCRAPERS_PORT=6021 python run.py
    GOOGLE_FLIGHTS_BASE=http://127.0.0.1:6021 python -m pytest google_flights/test_endpoints.py -q
"""
import os

import pytest

BASE = os.environ.get("GOOGLE_FLIGHTS_BASE", "").rstrip("/")

pytestmark = pytest.mark.skipif(not BASE, reason="set GOOGLE_FLIGHTS_BASE to run live endpoint tests")


def call(path, **params):
    from curl_cffi import requests
    resp = requests.get(BASE + path, params=params, timeout=240)
    assert resp.status_code == 200, f"{path} {params} -> {resp.status_code} {resp.text[:300]}"
    body = resp.json()
    assert body, f"{path} returned an empty body"
    return body


def status(path, **params):
    from curl_cffi import requests
    return requests.get(BASE + path, params=params, timeout=240).status_code


def flights_of(body):
    return body["best_flights"] + body["other_flights"]


def test_airport_search():
    body = call("/google-flights/airports/search", query="new york")
    assert body["places"][0]["id"] == "/m/02_286" and body["places"][0]["nearby_airports"][0]["code"]


def test_reference_lists():
    assert call("/google-flights/meta/currencies")["count"] > 50
    assert call("/google-flights/meta/languages")["count"] > 50
    assert call("/google-flights/meta/countries")["count"] > 150


def test_search_one_way_and_booking():
    body = call("/google-flights/flights/search-one-way", origin="JFK", destination="LAX", departure_date="2026-11-12")
    its = flights_of(body)
    assert len(its) > 10 and its[0]["segments"][0]["flight_number"] and body["price_insights"]["price_level"]
    body = call("/google-flights/flights/search-one-way", origin="New York", destination="LAX,BUR",
                departure_date="2026-11-12", stops="nonstop", airlines="DL,AA", departure_hours="6-14",
                sort_by="price", adults=2, cabin_class="economy", currency="EUR")
    its = flights_of(body)
    assert its and all(i["stops"] == 0 for i in its) and its[0]["currency"] == "EUR"
    book = call("/google-flights/flights/booking-options", booking_token=its[0]["booking_token"])
    opt = book["booking_options"][0]
    assert opt["price"] and opt["seller"]["name"] and (opt["booking_link"] or opt["tickets"])


def test_search_round_trip_chain():
    body = call("/google-flights/flights/search-round-trip", origin="JFK", destination="LHR",
                departure_date="2026-11-20", return_date="2026-11-27")
    out = flights_of(body)[0]
    back = call("/google-flights/flights/returning-flights", returning_token=out["returning_token"])
    ret = flights_of(back)[0]
    assert ret["departure"]["airport"]["code"] == "LHR" and ret["booking_token"]


def test_search_multi_city_chain():
    body = call("/google-flights/flights/search-multi-city", legs="JFK,LHR,2026-11-20|LHR,CDG,2026-11-24|CDG,JFK,2026-11-28")
    assert body["search"]["total_legs"] == 3
    nxt = call("/google-flights/flights/multi-city/next-leg", next_leg_token=flights_of(body)[0]["next_leg_token"])
    assert nxt["current_leg"] == 2 and flights_of(nxt)[0]["next_leg_token"]


def test_price_insights():
    body = call("/google-flights/flights/price-insights", origin="SFO", destination="SEA", departure_date="2026-11-15")
    assert body["price_insights"]["typical_price_range"]["low"] and body["price_insights"]["price_history"]


def test_price_calendar():
    body = call("/google-flights/prices/calendar", origin="JFK", destination="MIA", start_date="2026-10-15",
                end_date="2026-12-31")
    assert body["count"] > 60 and body["cheapest"]["price"]
    body = call("/google-flights/prices/calendar", origin="JFK", destination="MIA", start_date="2026-10-15",
                end_date="2026-11-30", trip_length_days=5)
    assert body["prices"][0]["return_date"]


def test_date_grid():
    body = call("/google-flights/prices/date-grid", origin="JFK", destination="MIA", departure_date="2026-11-10",
                return_date="2026-11-17")
    assert body["count"] == 49


def test_explore():
    body = call("/google-flights/explore/destinations", origin="JFK", departure_date="2026-11-12")
    assert body["count"] > 50 and body["destinations"][0]["price"]
    body = call("/google-flights/explore/destinations", origin="New York", month=12, trip_length="one_week",
                region="europe")
    assert any(d["country"] == "France" for d in body["destinations"])
    body = call("/google-flights/explore/destinations", origin="Denver", month=1, interest="skiing")
    assert body["count"] > 10


def test_routes():
    body = call("/google-flights/routes/from-city", city="New York")
    assert body["origin"]["name"] == "New York" and body["nonstop_airlines"]
    body = call("/google-flights/routes/to-city", city="https://www.google.com/travel/flights/flights-to-paris.html")
    assert body["destination"]["name"] == "Paris" and body["destination_airports"]
    body = call("/google-flights/routes/city-pair", origin="London", destination="Tokyo")
    assert len(body["price_by_month"]) == 12 and body["deals"]


def test_validation():
    assert status("/google-flights/flights/search-one-way", origin="JFK", destination="LAX") == 400
    assert status("/google-flights/flights/search-one-way", origin="JFK", destination="LAX",
                  departure_date="2020-01-01") == 400
    assert status("/google-flights/flights/booking-options", booking_token="nope") == 400
    assert status("/google-flights/prices/calendar", origin="JFK", destination="MIA", start_date="2026-10-15",
                  end_date="2027-06-01") == 400
    assert status("/google-flights/routes/from-city", city="zzqx-not-a-city") == 404
