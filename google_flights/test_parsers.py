"""Offline tests for the Google Flights endpoints + parsers, replaying
recorded raw upstream responses (google_flights/fixtures/<case>.json.gz
"cassettes").

Each case runs the real endpoint function with fetch.rpc / fetch.batchexecute
/ fetch.page / fetch.resolve_redirect patched to answer from the cassette
(keyed on the exact call arguments), so request building, token chaining
and parsing run without network or a browser. Pure-function tests (refs,
protocol) need no cassette.

    python -m pytest google_flights/test_parsers.py -q                            # replay (offline)
    GOOGLE_FLIGHTS_RECORD=1 python -m pytest google_flights/test_parsers.py -q    # re-record live
"""
import gzip
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from google_flights import explore, fetch, flights, places, prices, protocol, refs, route_pages  # noqa: E402

FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")
RECORD = os.environ.get("GOOGLE_FLIGHTS_RECORD") == "1"

# Endpoint kwargs exactly as the schemas hand them over (defaults filled).
FILTERS = dict(adults=1, children=0, infants_in_seat=0, infants_on_lap=0, cabin_class="economy", stops="any",
               airlines=None, max_price=None, departure_hours=None, arrival_hours=None, max_duration_minutes=None,
               max_layover_minutes=None, connecting_airports=None, less_emissions=False, carry_on_bags=0,
               checked_bags=0, sort_by="best", currency="USD", language="en-US", country="US")
BASIC = {k: FILTERS[k] for k in ("adults", "children", "infants_in_seat", "infants_on_lap", "cabin_class", "stops",
                                 "airlines", "max_price", "currency", "language", "country")}


def P(value):
    return refs.place_ref(value)


def _key(fn, args):
    return fn + ":" + json.dumps(args, sort_keys=True, default=str)


@pytest.fixture
def cassette(monkeypatch, request):
    """Patch the transport to record (GOOGLE_FLIGHTS_RECORD=1) or replay one case."""
    path = os.path.join(FIXTURES, request.node.name.replace("test_", "") + ".json.gz")
    real = {"rpc": fetch.rpc, "batchexecute": fetch.batchexecute, "page": fetch.page,
            "resolve_redirect": fetch.resolve_redirect}
    tape = {}
    if not RECORD:
        if not os.path.exists(path):
            pytest.skip(f"no cassette {os.path.basename(path)} (record with GOOGLE_FLIGHTS_RECORD=1)")
        with gzip.open(path, "rt") as f:
            tape = json.load(f)
    places._resolve_text.cache_clear()
    places._reference.update(at=0.0, data=None)

    def wrap(name):
        def call(*args, **kwargs):
            key = _key(name, [list(args), kwargs])
            if RECORD:
                out = real[name](*args, **kwargs)
                tape[key] = out
                return out
            if key not in tape:
                raise AssertionError(f"cassette miss: {key[:300]}")
            out = tape[key]
            return tuple(out) if name == "page" else out
        return call

    for name in real:
        monkeypatch.setattr(fetch, name, wrap(name))
    yield
    if RECORD:
        os.makedirs(FIXTURES, exist_ok=True)
        with gzip.open(path, "wt") as f:
            json.dump(tape, f)


def _all(result):
    return result["best_flights"] + result["other_flights"]


# ---- places / reference ---------------------------------------------------------------------------

def test_airport_search(cassette):
    r = places.search("paris")
    city = r["places"][0]
    assert city["type"] == "city" and city["id"] == "/m/05qtj" and city["name"] == "Paris"
    cdg = next(a for a in city["nearby_airports"] if a["code"] == "CDG")
    assert cdg["type"] == "airport" and cdg["distance"]["unit"] in ("mi", "km") and cdg["distance"]["value"] > 0
    assert any(p["type"] == "airport" and p["airport_code"] and p["city"] for p in r["places"][1:])


def test_reference_lists(cassette):
    c = places.currencies()
    assert c["count"] > 50 and any(x["code"] == "EUR" and x["symbol"] == "€" for x in c["currencies"])
    lang = places.languages()
    assert any(x["code"] == "en-US" for x in lang["languages"])
    assert any(x["code"] == "US" and x["name"] == "United States" for x in places.countries()["countries"])


# ---- flights ----------------------------------------------------------------------------------------

def test_one_way(cassette):
    r = flights.search_one_way(P("JFK"), P("LAX"), "2026-11-12", **{k: v for k, v in FILTERS.items()
                                                                   if k not in ("currency", "language", "country")})
    its = _all(r)
    assert r["search"]["trip_type"] == "one_way" and len(its) > 20 and r["best_flights"]
    it = its[0]
    assert it["price"] > 0 and it["currency"] == "USD" and it["booking_token"].startswith("gf1.")
    assert it["departure"]["airport"]["code"] == "JFK" and it["arrival"]["airport"]["code"] == "LAX"
    assert it["departure"]["time"] and it["duration_minutes"] > 300
    seg = it["segments"][0]
    assert seg["flight_number"] and seg["aircraft"] and seg["seat"]["legroom"]["unit"] == "in"
    assert seg["amenities"]["wifi"] in ("free", "for_a_fee", "available", None)
    assert seg["emissions"]["co2_kg"] > 0
    stop = next(i for i in its if i["stops"])
    assert stop["layovers"][0]["duration_minutes"] > 0 and stop["layovers"][0]["airport"]["code"]
    ins = r["price_insights"]
    assert ins["price_level"] in ("low", "typical", "high") and ins["typical_price_range"]["low"]
    assert ins["price_history"][0]["date"] and ins["price_history"][0]["price"] > 0
    assert r["filters"]["airlines"] and r["filters"]["duration_range_minutes"]["min"]
    assert any(p["baggage_link"] for p in r["airline_policies"]) and r["airports"][0]["coordinates"]
    assert "_raw_token" not in it and "_selection" not in it


def test_one_way_filters(cassette):
    kw = dict(FILTERS, stops="nonstop", airlines=["DL"], departure_hours=(6, 12), sort_by="price",
              currency="EUR", language="de", country="DE")
    loc = {k: kw.pop(k) for k in ("currency", "language", "country")}
    r = flights.search_one_way(P("JFK"), P("LAX"), "2026-11-12", **loc, **kw)
    its = _all(r)
    assert its and all(i["stops"] == 0 for i in its) and all(a["code"] == "DL" for i in its for a in i["airlines"])
    assert all(6 <= int(i["departure"]["time"][:2]) <= 12 for i in its) and its[0]["currency"] == "EUR"
    prices_ = [i["price"] for i in its if i["price"] is not None]
    assert prices_ == sorted(prices_)


def test_round_trip_chain(cassette):
    kw = {k: v for k, v in FILTERS.items() if k not in ("currency", "language", "country")}
    r = flights.search_round_trip(P("JFK"), P("LHR"), "2026-11-20", "2026-11-27", **kw)
    out = _all(r)[0]
    assert out["returning_token"].startswith("gf1.") and out["arrival"]["airport"]["code"] == "LHR"
    ret = flights.returning_flights(refs.token_ref("returning")(out["returning_token"]))
    back = _all(ret)[0]
    assert back["departure"]["airport"]["code"] == "LHR" and back["booking_token"]
    assert ret["selected_flights"][0]["flights"][0]["flight_number"] == out["segments"][0]["flight_number"]
    book = flights.booking_options(refs.token_ref("booking")(back["booking_token"]), resolve_links=False)
    assert len(book["selected_flights"]) == 2 and book["booking_options"]
    opt = book["booking_options"][0]
    assert opt["price"] == book["lowest_price"] and opt["seller"]["name"]
    assert opt["google_booking_link"] or all(t["google_booking_link"] for t in opt["tickets"])


def test_booking_options(cassette):
    kw = {k: v for k, v in FILTERS.items() if k not in ("currency", "language", "country")}
    r = flights.search_one_way(P("JFK"), P("LAX"), "2026-11-12", **kw)
    token = next(i["booking_token"] for i in _all(r) if i["booking_token"])
    b = flights.booking_options(refs.token_ref("booking")(token))
    opt = b["booking_options"][0]
    assert opt["fare_name"] and opt["features"] and opt["features"][0]["availability"] in (
        "included", "for_a_fee", "not_available")
    assert opt["booking_link"] and opt["booking_link"].startswith("http")
    assert b["selected_flights"][0]["segments"][0]["flight_number"]


def test_multi_city_chain(cassette):
    legs = refs.legs_ref("JFK,LHR,2026-11-20|LHR,CDG,2026-11-24|CDG,JFK,2026-11-28")
    kw = {k: v for k, v in FILTERS.items() if k not in ("currency", "language", "country")}
    r = flights.search_multi_city(legs, **kw)
    assert r["search"]["total_legs"] == 3 and r["search"]["current_leg"] == 1
    token = _all(r)[0]["next_leg_token"]
    second = flights.multi_city_next_leg(refs.token_ref("next_leg")(token))
    assert second["current_leg"] == 2 and _all(second)[0]["next_leg_token"]
    third = flights.multi_city_next_leg(refs.token_ref("next_leg")(_all(second)[0]["next_leg_token"]))
    assert third["current_leg"] == 3 and _all(third)[0]["booking_token"]
    assert [s["leg"] for s in third["selected_flights"]] == [1, 2]


# ---- prices -----------------------------------------------------------------------------------------

def test_calendar(cassette):
    c = prices.calendar(P("JFK"), P("MIA"), "2026-10-15", "2026-12-31", None, **BASIC)
    assert c["count"] > 60 and c["cheapest"]["price"] == min(p["price"] for p in c["prices"] if p["price"])
    rt = prices.calendar(P("JFK"), P("MIA"), "2026-10-15", "2026-11-30", 5, **BASIC)
    assert rt["prices"][0]["return_date"] and rt["search"]["trip_type"] == "round_trip"


def test_date_grid(cassette):
    g = prices.date_grid(P("JFK"), P("MIA"), "2026-11-10", "2026-11-17", **BASIC)
    assert g["count"] == 49 and all(p["departure_date"] and p["return_date"] for p in g["prices"])


# ---- explore ------------------------------------------------------------------------------------------

def test_explore(cassette):
    kw = dict(BASIC)
    e = explore.destinations(P("JFK"), departure_date="2026-11-12", **kw)
    d = e["destinations"][0]
    assert e["count"] > 50 and d["name"] and d["coordinates"] and d["price"] and d["airport_code"]
    flex = explore.destinations(P("JFK"), month=12, trip_length="one_week", region=refs.region_ref("europe"), **kw)
    assert flex["search"]["dates"] == "flexible" and any(x["country"] == "France" for x in flex["destinations"])
    priced = [x for x in flex["destinations"] if x["return_date"]]
    assert priced and priced[0]["departure_date"] < priced[0]["return_date"]


# ---- route insight pages --------------------------------------------------------------------------------

def test_route_pages(cassette):
    loc = dict(currency="USD", language="en-US", country="US")
    a = route_pages.from_city("new-york", **loc)
    assert a["origin"]["name"] == "New York" and a["origin_airports"][0]["code"] == "JFK"
    assert a["nonstop_airlines"][0]["top_routes"][0]["weekly_flights"] > 0 and a["popular_trips"][0]["destination"]
    b = route_pages.to_city("london", **loc)
    assert b["destination"]["name"] == "London" and b["destination_info"]["peak_season"]
    assert b["popular_trips"][0]["origin"]["name"]
    c = route_pages.city_pair("new-york", "london", **loc)
    assert c["deals"]["cheapest_round_trip"]["price"] and c["fastest_flight_minutes"] > 300
    assert len(c["price_by_month"]) == 12 and 1 <= c["cheapest_month"] <= 12
    assert c["airlines"][0]["weekly_nonstop_flights"] is not None and c["related_routes"][0]["link"]


# ---- pure functions (no cassette) ----------------------------------------------------------------------

def test_refs():
    assert refs.place_ref("jfk, EWR") == [{"id": "JFK", "kind": 0}, {"id": "EWR", "kind": 0}]
    assert refs.place_ref("/m/02_286") == [{"id": "/m/02_286", "kind": 4}]
    assert refs.place_ref("NYC") == [{"id": "/m/02_286", "kind": 4}]
    assert refs.place_ref("New York") == [{"query": "New York"}]
    legs = refs.legs_ref("JFK+EWR,LHR,2099-11-20|LHR,CDG,2099-11-24")
    assert len(legs[0]["origin"]) == 2 and legs[1]["date"] == "2099-11-24"
    assert refs.legs_ref('[{"origin":"JFK","destination":"LHR","date":"2099-11-20"},'
                         '{"origin":"LHR","destination":"JFK","date":"2099-11-27"}]')[1]["origin"][0]["id"] == "LHR"
    with pytest.raises(ValueError):
        refs.legs_ref("JFK,LHR,2099-11-20")
    assert refs.hours_ref("6-18") == (6, 18)
    with pytest.raises(ValueError):
        refs.hours_ref("18-6")
    assert refs.airlines_ref("dl, star alliance") == ["DL", "STAR_ALLIANCE"]
    assert refs.region_ref("Europe") == refs.REGIONS["europe"]
    assert refs.region_ref("71,40,35,-25") == [[71.0, 40.0], [35.0, -25.0]]
    assert refs.city_slug_ref("São Paulo") == "sao-paulo"
    link = "https://www.google.com/travel/flights/flights-from-new-york-to-london.html?gl=us"
    assert refs.city_slug_ref(link, "from") == "new-york" and refs.city_slug_ref(link, "to") == "london"
    assert refs.city_slug_ref("https://www.google.com/travel/flights/flights-to-paris.html", "to") == "paris"


def test_tokens_and_tfs():
    tok = protocol.pack_token("booking", g="abc", q=[1, [2]], s=[[["JFK", "2099-01-01", "LAX", None, "DL", "1"]]])
    data = protocol.unpack_token(tok, "booking")
    assert data["g"] == "abc" and data["s"][0][0][4] == "DL"
    with pytest.raises(ValueError):
        protocol.unpack_token(tok, "returning")
    with pytest.raises(ValueError):
        protocol.unpack_token("gf1.garbage", "booking")
    tfs = protocol.tfs(2, [{"origins": [{"id": "JFK", "kind": 0}], "destinations": [{"id": "LAX", "kind": 0}],
                            "date": "2026-10-20", "max_stops": None, "selected": None}])
    assert tfs == "CBwQAhoeEgoyMDI2LTEwLTIwagcIARIDSkZLcgcIARIDTEFYQAFIAXABmAEC"  # the search page accepted this exact query (2026-09-26)
