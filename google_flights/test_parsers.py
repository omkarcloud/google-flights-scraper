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
import threading
from datetime import date, timedelta

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from google_flights import explore, fetch, flights, places, prices, protocol, refs, route_pages, shared  # noqa: E402
from scraper_utils.testing import hosted_only

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
            # `done` (rpc's stop-reading predicate) is a function: it does
            # not change the recorded answer and has no stable key
            key = _key(name, [list(args), {k: v for k, v in kwargs.items() if k != "done"}])
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


class _RecordedDay(date):
    """date whose today() is the cassettes' recording day, so request
    building that clamps to today (leg dates, the date grid's windows)
    rebuilds the recorded calls."""

    @classmethod
    def today(cls):
        return cls(2026, 9, 26)


@pytest.fixture
def recorded_day(monkeypatch):
    for module in (refs, prices):
        monkeypatch.setattr(module, "date", _RecordedDay)


def test_multi_city_chain(cassette, recorded_day):
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


def test_date_grid(cassette, recorded_day):
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
    d1, d2 = (date.today() + timedelta(days=20)).isoformat(), (date.today() + timedelta(days=24)).isoformat()
    legs = refs.legs_ref(f"JFK+EWR,LHR,{d1}|LHR,CDG,{d2}")
    assert len(legs[0]["origin"]) == 2 and legs[1]["date"] == d2
    assert refs.legs_ref(f'[{{"origin":"JFK","destination":"LHR","date":"{d1}"}},'
                         f'{{"origin":"LHR","destination":"JFK","date":"{d2}"}}]')[1]["origin"][0]["id"] == "LHR"
    with pytest.raises(ValueError):
        refs.legs_ref(f"JFK,LHR,{d1}")
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


@hosted_only
def test_token_minter_waits_for_a_cold_mint(monkeypatch):
    import browser_client
    from google_flights import minter
    calls = []
    import config
    monkeypatch.setattr(config, "INPAGE_BROWSER_SLOTS", 2)
    monkeypatch.setattr(config, "BROWSER_BURST_FACTOR", 4)
    monkeypatch.setattr(minter, "_same_machine", lambda: False)
    monkeypatch.setattr(browser_client, "token_any",
                        lambda site, candidates, freq, variant="", timeout=None: calls.append((site, candidates, freq, timeout)) or (3, "t" * 200))
    m = minter.TokenMinter()
    tok, exit = m.token("[[freq]]")
    assert tok.startswith("t") and m.mints == 1
    site, candidates, freq, timeout = calls[0]
    assert (site, freq, timeout) == ("google-flights", "[[freq]]", minter.TOKEN_TIMEOUT) and minter.TOKEN_TIMEOUT == 180
    assert exit == candidates[3][0]                                       # the exit of the Chrome that minted it


# ---- one exit for the minting Chrome and the POST, 2026-11-28 -----------------------------------------

def test_minter_gives_each_box_chrome_a_residential_exit(monkeypatch):
    # The box's own IP is refused even with the RPC sent from the box
    # (round trip 1/50, 2026-11-28): each box Chrome gets a sticky
    # residential exit, one per candidate. In this process (the kit)
    # Chrome and curl share the machine's IP: no exit.
    import browser_client
    import config
    from google_flights import minter
    monkeypatch.setattr(config, "INPAGE_BROWSER_SLOTS", 2)
    n = config.burst_slots(2)                                              # 8 hosted (share 2 x factor 4)
    ports = iter(range(10001, 10100))
    monkeypatch.setattr(minter, "_new_exit", lambda: f"http://user__cr.us:pw@gw.example:{next(ports)}")
    monkeypatch.setattr(minter, "_same_machine", lambda: False)
    m = minter.TokenMinter()
    cands = m._candidates()
    assert len(cands) == n and len({p for p, _ in cands}) == n and all(p and s is None for p, s in cands)
    assert m._candidates() == cands                                        # sticky
    m.rotate(cands[-1][0])
    after = m._candidates()
    assert after[-1][0] not in {p for p, _ in cands} and after[:-1] == cands[:-1] and m.rotations == 1
    monkeypatch.setattr(minter, "_same_machine", lambda: True)
    assert minter.TokenMinter()._candidates() == [(None, f"flights#{i}") for i in range(n)]

    refused = browser_client.BoxRefused(f"google-flights/token: 502 Page.goto: Timeout 60000ms exceeded "
                                        f"(proxy {after[0][0]})", 0)
    refused.proxy = after[0][0]

    def token_any(site, candidates, freq, variant="", timeout=None):
        raise refused
    monkeypatch.setattr(minter, "_same_machine", lambda: False)
    monkeypatch.setattr(browser_client, "token_any", token_any)
    with pytest.raises(minter.MinterUnavailable) as e:
        m.token("[[freq]]")
    assert after[0][0] not in {p for p, _ in m._candidates()}              # refused on that exit
    assert ":pw@" not in str(e.value) and ":pw@" not in m.status()["last_error"]   # no proxy login in errors

    # A Chrome that cannot load Flights through its exit fails in warm()
    # with a Page.goto timeout, which is no RecipeError: the box answers
    # 500 and the client raises BoxUnavailable naming the candidate. That
    # exit is rotated too (it stayed, and a cold box launched it again on
    # every mint), by the token ask and by the user-agent ask alike.
    for ask in (lambda: m.token("[[freq]]"), m.user_agent):
        dead = m._candidates()[0][0]
        refused = browser_client.BoxUnavailable("google-flights/token: box answered 500 TimeoutError: "
                                                "Page.goto: Timeout 60000ms exceeded.", 0)
        refused.proxy = dead
        try:
            assert ask() is None
        except minter.MinterUnavailable:
            pass
        assert dead not in {p for p, _ in m._candidates()}
    busy = browser_client.BoxUnavailable("google-flights/token: none of 8 browser slots free in 20s")
    monkeypatch.setattr(browser_client, "token_any", lambda *a, **k: (_ for _ in ()).throw(busy))
    kept, rotations = m._candidates(), m.rotations
    with pytest.raises(minter.MinterUnavailable):
        m.token("[[freq]]")
    assert m._candidates() == kept and m.rotations == rotations            # a busy box names no exit: none rotated


def test_recipe_disables_http2_only_through_a_proxy(monkeypatch):
    # The Ubuntu box's Chrome on a residential exit never loaded Flights over
    # HTTP/2 (Page.goto timeout 60000 ms, 2 of 2, 2026-11-28)
    import types as pytypes
    from recipes.google_flights import GoogleFlights
    launched = []
    fake = pytypes.ModuleType("patchright_driver")
    fake.PatchrightDriver = lambda **kw: launched.append(kw) or object()
    monkeypatch.setitem(sys.modules, "patchright_driver", fake)
    recipe = GoogleFlights()
    recipe.launch("http://user:pw@gw.example:10001")
    recipe.launch(None)
    assert "--disable-http2" in launched[0]["extra_args"] and "--disable-http2" not in launched[1]["extra_args"]


class _Stream:
    """A streamed answer: its parts go to content_callback in turn; with
    `hold_after` the parts after that index wait for `rest.set()`, the way
    Google holds a cold search's refinements for seconds."""

    def __init__(self, parts, hold_after=None):
        self.status_code, self.url, self.parts, self.hold_after = 200, "", parts, hold_after
        self.rest, self.ended = threading.Event(), threading.Event()

    def pieces(self, part):
        return part if isinstance(part, list) else [part]

    @property
    def text(self):
        return b"".join(b for part in self.parts for b in self.pieces(part)).decode()


def _streamed(payloads, piece=16384):
    """rt=c bytes as curl hands them over: one part per chunk, each cut in
    `piece`-byte writes (a chunk's line is 25-80 KB, 2026-11-28)."""
    parts = [b")]}'\n\n"]
    for p in payloads:
        line = json.dumps([["wrb.fr", None, json.dumps(p)]]) + "\n"
        whole = f"{len(line)}\n{line}".encode()
        parts.append([whole[i:i + piece] for i in range(0, len(whole), piece)])
    return parts


with gzip.open(os.path.join(FIXTURES, "one_way_streamed.json.gz"), "rt") as _f:
    STREAMED = json.load(_f)["payloads"]


def test_a_search_that_streams_a_progress_chunk_first_answers_its_flights(monkeypatch):
    # ORD-DEN 2027-02-18 (2026-11-28): chunk 0 has no flights; flights._shop
    # read only chunks[0], so the one-way answered 200 with no flights.
    from google_flights import parsers
    monkeypatch.setattr(fetch, "rpc", lambda *a, **k: STREAMED)
    r = flights.search_one_way(P("ORD"), P("DEN"), "2027-02-18", **BASIC)
    assert len(_all(r)) == 18 and r["best_flights"] and r["price_insights"]["price_level"]
    assert parsers.shopping_chunk(STREAMED) is STREAMED[2]          # flights AND price insights
    assert parsers.shopping_chunk(STREAMED[:2]) is STREAMED[1]      # flights only
    assert parsers.shopping_chunk(STREAMED[:1]) is STREAMED[0]


def test_rpc_answers_at_the_first_complete_chunk(fake_rpc):
    # A cold search keeps streaming 7-14 s after its flights arrived
    # (LAX-SEA: flights + insights at 1.5 s, end 13.2 s, 2026-11-28)
    from google_flights import parsers
    answers, sent, minter = fake_rpc
    stream = _Stream(_streamed(STREAMED), hold_after=3)             # header, chunk 0, 1, 2 (the complete one)
    answers.append(stream)
    payloads = fetch.rpc("GetShoppingResults", [1], done=parsers.shopping_complete)
    assert len(payloads) == 3 and parsers.shopping_complete(payloads[-1])
    assert not stream.ended.is_set()                                # answered while Google still streams
    stream.rest.set()
    assert stream.ended.wait(5)
    for _ in range(500):                                            # back in the pool once the transfer ended
        if fetch._RPC_SESSIONS._idle.get(None):
            break
        threading.Event().wait(0.01)                                # not time.sleep: fake_rpc replaces it
    answers.append(_Stream(_streamed(STREAMED)))
    assert len(fetch.rpc("GetCalendarGraph", [1])) == 4             # no `done`: read to the end
    posts = [s for s in sent if s[0] == "post"]
    assert posts[0][4] is posts[1][4]                               # the same session (and connection) again


def test_a_pooled_rpc_session_keeps_one_curl_handle_across_threads():
    # curl_cffi's default is a curl handle per calling thread, and a
    # streamed POST runs on a thread of its own: 6 POSTs through one pooled
    # session opened 6 connections (a local server, 2026-11-28)
    session = fetch._new_rpc_session(None)
    handles = []
    thread = threading.Thread(target=lambda: handles.append(session.curl))
    thread.start()
    thread.join()
    assert handles == [session.curl]
    session.close()


def test_rpc_posts_through_the_exit_its_token_was_minted_on(fake_rpc):
    answers, sent, minter = fake_rpc
    minter.exits = ["http://u:pw@gw.example:10001", "http://u:pw@gw.example:10002"]

    def through():
        return [(s[3], s[1], s[2]) for s in sent if s[0] == "post"]
    answers.extend([OK_BODY])
    assert fetch.rpc("GetShoppingResults", [1]) == [[1]]
    assert through() == [("http://u:pw@gw.example:10001", "token-1", minter.ua)]   # never this machine's own IP

    # blocked on an exit: that exit is rotated and the retry is minted (and sent) elsewhere
    sent.clear()
    answers.extend([_Blocked(), OK_BODY])
    assert fetch.rpc("GetShoppingResults", [1]) == [[1]]
    assert [t[0] for t in through()] == ["http://u:pw@gw.example:10001", "http://u:pw@gw.example:10002"]
    assert minter.rotated == ["http://u:pw@gw.example:10001"]

    # refused twice on one exit in one call: rotated as well
    sent.clear()
    minter.exits = ["http://u:pw@gw.example:10003", "http://u:pw@gw.example:10004"]
    answers.extend([REJECTIONS["refused_valid_round_trip"]] * 2 + [OK_BODY])
    assert fetch.rpc("GetShoppingResults", [1]) == [[1]]
    assert minter.rotated[-1] == "http://u:pw@gw.example:10003"
    assert [t[0] for t in through()] == ["http://u:pw@gw.example:10003"] * 2 + ["http://u:pw@gw.example:10004"]


# ---- rejections: refused (502) vs invalid input (400), 2026-11-28 -------------------------------------

with open(os.path.join(FIXTURES, "rejections.json")) as _f:
    REJECTIONS = json.load(_f)
OK_BODY = ')]}\'\n\n20\n[["wrb.fr",null,"[1]"]]\n'


class _Resp:
    def __init__(self, text):
        self.status_code, self.text, self.url = 200, text, ""


class _Blocked:
    status_code, text, url = 403, "", ""


def _answer(a):
    return _Resp(a) if isinstance(a, str) else a


class _FakeMinter:
    """Tokens token-1, token-2, ...; each minted on exits[0] (None = this
    machine's own IP) until rotate() drops that exit, like the ExitPool."""

    def __init__(self, ua="Mozilla/5.0 (X11; Linux x86_64) Chrome/156.0.0.0"):
        self.tokens, self.ua, self.exits, self.rotated = 0, ua, [], []

    def token(self, freq):
        self.tokens += 1
        return f"token-{self.tokens}", (self.exits[0] if self.exits else None)

    def rotate(self, exit):
        self.rotated.append(exit)
        if exit in self.exits:
            self.exits.remove(exit)

    def user_agent(self):
        return self.ua

    def recycle(self):
        pass


class _FakeSession:
    """A curl session answering from `answers`; every POST is logged in
    `sent` as ("post", token, user agent, proxy, session)."""

    def __init__(self, proxy, answers, sent):
        self.proxy, self.answers, self.sent = proxy, answers, sent

    def post(self, url, data=None, headers=None, timeout=None, content_callback=None):
        self.sent.append(("post", headers.get("x-goog-batchexecute-bgr"), headers.get("user-agent"), self.proxy, self))
        a = _answer(self.answers.pop(0))
        if content_callback is None:
            return a
        if isinstance(a, _Stream):
            for i, part in enumerate(a.parts):
                if a.hold_after is not None and i > a.hold_after:
                    assert a.rest.wait(5)
                for piece in a.pieces(part):
                    content_callback(piece)
            a.ended.set()
        else:
            content_callback(a.text.encode())
        return a

    def close(self):
        pass


@pytest.fixture
def fake_rpc(monkeypatch):
    """fetch.rpc against scripted answers (a body text or a response object):
    returns (answers list to fill, sent list, minter)."""
    from google_flights import minter as minter_mod
    answers, sent = [], []
    fake = _FakeMinter()
    monkeypatch.setattr(minter_mod, "MINTER", fake)
    monkeypatch.setattr(fetch.time, "sleep", lambda s: sent.append(("sleep", s)))
    monkeypatch.setattr(fetch, "_RPC_SESSIONS", fetch._RpcSessions())
    monkeypatch.setattr(fetch, "_new_rpc_session", lambda proxy: _FakeSession(proxy, answers, sent))
    monkeypatch.setattr(fetch, "_session", lambda fresh_exit=False: _FakeSession("fallback", answers, sent))
    return answers, sent, fake


def test_decode_rejection_reasons():
    assert fetch.decode_chunks(REJECTIONS["refused_valid_round_trip"])[1] == [(13, True, 0)]
    assert fetch.decode_chunks(REJECTIONS["invalid_past_date"])[1] == [(3, True, 2)]
    assert fetch.decode_chunks(REJECTIONS["invalid_too_far"])[1] == [(3, True, 3)]


def test_refused_valid_request_is_a_502_with_fresh_tokens(fake_rpc):
    answers, sent, minter = fake_rpc
    answers.extend([REJECTIONS["refused_valid_round_trip"]] * fetch.RPC_ATTEMPTS)
    with pytest.raises(fetch.GoogleFlightsRefused) as e:
        fetch.rpc("GetShoppingResults", [1])
    assert isinstance(e.value, fetch.UpstreamError) and not isinstance(e.value, fetch.BadRequest)
    posts = [s for s in sent if s[0] == "post"]
    assert [p[1] for p in posts] == [f"token-{i}" for i in range(1, fetch.RPC_ATTEMPTS + 1)]   # a fresh token each try
    assert all(p[2] == minter.ua for p in posts)                                                # the minting Chrome's UA
    assert sum(s[1] for s in sent if s[0] == "sleep") < 3                                       # no 9 s of back-off


def test_refused_then_passing_answers(fake_rpc):
    answers, sent, _ = fake_rpc
    answers.extend([REJECTIONS["refused_valid_round_trip"], OK_BODY])
    assert fetch.rpc("GetShoppingResults", [1]) == [[1]]


def test_invalid_input_is_a_400_naming_the_problem(fake_rpc):
    answers, sent, _ = fake_rpc
    answers.extend([REJECTIONS["invalid_too_far"]] * fetch.RPC_ATTEMPTS)
    with pytest.raises(fetch.GoogleFlightsBadRequest, match="329 days ahead"):
        fetch.rpc("GetShoppingResults", [1])
    assert len([s for s in sent if s[0] == "post"]) == fetch.INVALID_ATTEMPTS
    answers.clear()
    answers.extend([REJECTIONS["invalid_past_date"]] * fetch.RPC_ATTEMPTS)
    with pytest.raises(fetch.GoogleFlightsBadRequest, match="in the past"):
        fetch.rpc("GetShoppingResults", [1])


def test_dates_past_the_search_window_are_refused_before_google():
    from google_flights import schemas
    last = date.today() + timedelta(days=shared.MAX_DAYS_AHEAD)
    ok = schemas.RoundTripSchema().load({"origin": "BFS", "destination": "MAN", "departure_date": last.isoformat(),
                                         "return_date": last.isoformat()})
    assert ok["departure_date"] == last.isoformat()
    from marshmallow import ValidationError
    with pytest.raises(ValidationError) as e:
        schemas.RoundTripSchema().load({"origin": "BFS", "destination": "MAN", "departure_date": "2027-01-07",
                                        "return_date": (last + timedelta(days=1)).isoformat()})
    assert list(e.value.messages) == ["return_date"]
    with pytest.raises(ValueError, match="329 days ahead"):
        refs.legs_ref(f"BFS,MAN,{last.isoformat()}|MAN,BFS,{(last + timedelta(days=1)).isoformat()}")


def test_date_grid_windows_end_inside_the_search_window():
    last = date.today() + timedelta(days=shared.MAX_DAYS_AHEAD)
    start, end = prices._window(last - timedelta(days=3), date.today(), last)
    assert end == last and (end - start).days == 2 * prices.GRID_HALF_WINDOW
    start, end = prices._window(date.today() + timedelta(days=30), date.today(), last)
    assert (end - start).days == 2 * prices.GRID_HALF_WINDOW and start == date.today() + timedelta(days=30)


def test_mint_page_tells_its_user_agent(monkeypatch):
    import browser_client
    from google_flights import minter
    asked = []

    def token_any(site, candidates, freq, variant="", timeout=None):
        asked.append(freq)
        return 0, "Mozilla/5.0 (X11; Linux x86_64) Chrome/156.0.0.0 Safari/537.36"
    monkeypatch.setattr(browser_client, "token_any", token_any)
    m = minter.TokenMinter()
    assert m.user_agent().startswith("Mozilla/5.0") and m.user_agent() and asked == [minter.UA_INPUT]   # cached
    assert minter.UA_INPUT in minter._MINT_JS
    old = minter.TokenMinter()                       # a box still on the older _MINT_JS answers a token
    monkeypatch.setattr(browser_client, "token_any", lambda *a, **k: (0, '[";' + "x" * 300 + '"]'))
    assert old.user_agent() is None


def test_a_slow_failed_user_agent_ask_is_not_asked_again_at_once(monkeypatch):
    # The box can take the whole TOKEN_TIMEOUT (180 s) to fail, longer than
    # UA_RETRY (60 s). The retry time counted from before the ask was then
    # already past, so every caller queued on the lock asked again, 180 s each.
    import browser_client
    from google_flights import minter
    clock = {"now": 1000.0}
    asks = []

    def token_any(site, candidates, freq, variant="", timeout=None):
        asks.append(freq)
        clock["now"] += minter.TOKEN_TIMEOUT
        raise browser_client.BoxUnavailable("timed out")

    monkeypatch.setattr(minter.time, "monotonic", lambda: clock["now"])
    monkeypatch.setattr(browser_client, "token_any", token_any)
    m = minter.TokenMinter()
    assert m.user_agent() is None and m.user_agent() is None
    assert len(asks) == 1                            # the second caller takes the answer just learned
    clock["now"] += minter.UA_RETRY + 1
    assert m.user_agent() is None and len(asks) == 2    # asked again once UA_RETRY has passed
