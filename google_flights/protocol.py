"""Google Flights wire format: the positional request arrays the
FlightsFrontendService RPCs take, the `tfs` protobuf the server-rendered
search page takes, and the opaque continuation tokens this API hands out.

Everything here is pure (no network). Positions were mapped 2026-09-26 by
replaying the web client's own requests with BotGuard tokens minted for
modified bodies (see google_flights/minter.py):

  place     [code, 0] airport IATA code | [geo_id, 4] city / region / country
            (Knowledge Graph id "/m/02_286"; metro codes like NYC are rejected)
  leg       [[origins]], [[destinations]], times, stops, airlines, None, date,
            [max_duration], selected, connecting, None, None, max_layover,
            emissions, 3
              times       [dep_from_hour, dep_to_hour, arr_from_hour, arr_to_hour]
              stops       0 any | 1 nonstop | 2 one stop or fewer | 3 two or fewer
              airlines    IATA codes and/or ONEWORLD / SKYTEAM / STAR_ALLIANCE
              selected    [[from, date, to, None, airline, number], ...] — the
                          chosen itinerary of that leg (round-trip / multi-city
                          next step, booking options)
              connecting  ["SEA", ...] only connect through these airports
              max_layover minutes (scalar)
              emissions   [1] = less-emissions itineraries only
  request   [None, None, trip, None, flex, cabin, [adults, children,
            infants_in_seat, infants_on_lap], [None, max_price], None, None,
            [carry_on_bags, checked_bags], None, None, legs, None, None, None, 1]
              trip   1 round trip | 2 one way | 3 multi-city
              cabin  1 economy | 2 premium economy | 3 business | 4 first
              flex   Explore only: [month (0 = next 6 months), trip length
                     (1 weekend | 2 one week | 3 two weeks)]
  shopping  [[None]*4, request, sort, 0, 0, 1]
              sort 0 best | 2 price | 3 departure | 4 arrival | 5 duration | 6 emissions
"""
import base64
import json
import re
import zlib

TRIP_ROUND = 1
TRIP_ONE_WAY = 2
TRIP_MULTI_CITY = 3

STOPS = {"any": 0, "nonstop": 1, "one_stop_or_fewer": 2, "two_stops_or_fewer": 3}
SORTS = {"best": 0, "price": 2, "departure_time": 3, "arrival_time": 4, "duration": 5, "emissions": 6}
ALLIANCES = {"ONEWORLD", "SKYTEAM", "STAR_ALLIANCE"}
EXPLORE_TRIP_LENGTHS = {"weekend": 1, "one_week": 2, "two_weeks": 3}

GEO_ID_RE = re.compile(r"^/(?:m|g)/[0-9a-zA-Z_]{2,20}$")
AIRPORT_RE = re.compile(r"^[A-Za-z]{3}$")

# Metro codes Google Flights does not accept as places -> their city ids.
METRO_CODES = {
    "NYC": "/m/02_286", "LON": "/m/04jpl", "PAR": "/m/05qtj", "TYO": "/m/07dfk", "CHI": "/m/01_d4",
    "WAS": "/m/0rh6k", "MIL": "/m/0947l", "ROM": "/m/06c62", "BJS": "/m/01914", "DTT": "/m/02dtg",
    "HOU": "/m/03l2n", "YMQ": "/m/052p7",
}


def place(value):
    """'JFK' -> {'id': 'JFK', 'kind': 0}; '/m/02_286' -> {'id': ..., 'kind': 4}."""
    if isinstance(value, dict):
        return value
    value = value.strip()
    if GEO_ID_RE.match(value):
        return {"id": value, "kind": 4}
    code = value.upper()
    if code in METRO_CODES:
        return {"id": METRO_CODES[code], "kind": 4}
    return {"id": code, "kind": 0}


def _places(places):
    return [[[p["id"], p["kind"]] for p in places]]


def leg(origins, destinations, date, *, departure_hours=None, arrival_hours=None, stops=0,
        airlines=None, max_duration=None, selected=None, connecting=None, max_layover=None,
        less_emissions=False):
    times = None
    if departure_hours or arrival_hours:
        dep = departure_hours or (None, None)
        arr = arrival_hours or (None, None)
        times = [dep[0], dep[1], arr[0], arr[1]]
    return [
        _places(origins) if origins else [],
        _places(destinations) if destinations else [],
        times,
        stops or 0,
        list(airlines) if airlines else None,
        None,
        date,
        [int(max_duration)] if max_duration else None,
        selected or None,
        list(connecting) if connecting else None,
        None,
        None,
        int(max_layover) if max_layover else None,
        [1] if less_emissions else None,
        3,
    ]


def request(trip, legs, *, cabin=1, adults=1, children=0, infants_in_seat=0, infants_on_lap=0,
            max_price=None, carry_on_bags=0, checked_bags=0, flex=None, interest=None):
    """flex / interest are Explore-only: flex [month, trip length] switches the
    request to flexible dates (slot 17 = 0); interest is a topic id in slot 27."""
    bags = [carry_on_bags or 0, checked_bags or 0] if (carry_on_bags or checked_bags) else None
    req = [
        None, None, trip, None, flex or [], cabin,
        [adults, children or 0, infants_in_seat or 0, infants_on_lap or 0],
        [None, int(max_price)] if max_price else None,
        None, None, bags, None, None, legs, None, None, None, 0 if flex else 1,
    ]
    if interest:
        req += [None] * 9 + [interest]
    return req


def shopping_body(req, sort=0):
    return [[None, None, None, None], req, sort or 0, 0, 0, 1]


def calendar_graph_body(req, start, end, trip_length=None):
    body = [None, req, [start, end]]
    if trip_length:
        body += [None, [trip_length, trip_length]]
    return body


def date_grid_body(req, departure_range, return_range=None):
    body = [None, req, list(departure_range)]
    if return_range:
        body.append(list(return_range))
    return body


def booking_body(google_token, req):
    return [[None, google_token], req, None, 0]


def explore_body(req, bounds=None, round_trip=False, tail=None):
    """bounds [[north, east], [south, west]] limits destinations to a map
    region; [1064, 920] is the web client's map viewport in pixels; the web
    client appends 2 for flexible dates and 4 with an interest."""
    body = [[], bounds, None, req, None, 1, None, 0, None, 1 if round_trip else 0, [1064, 920]]
    if tail is not None:
        body.append(tail)
    return body


def with_selection(req, selections):
    """A copy of `req` whose legs carry the chosen itineraries
    (selections[i] = that leg's [[from, date, to, None, airline, number], ...])."""
    req = json.loads(json.dumps(req))
    for i, sel in enumerate(selections):
        if sel and i < len(req[13]):
            req[13][i][8] = sel
    return req


# ---- continuation tokens -------------------------------------------------------------------
# returning_token / next_leg_token / booking_token: this API's own opaque
# tokens. They carry everything the follow-up call needs (the request,
# the selections so far, the locale and sort, plus Google's booking token),
# so follow-up endpoints take ONE param. zlib + urlsafe base64, "gf1." prefix.

TOKEN_PREFIX = "gf1."


def pack_token(kind, **data):
    raw = json.dumps({"k": kind, **data}, separators=(",", ":"), ensure_ascii=False).encode()
    return TOKEN_PREFIX + base64.urlsafe_b64encode(zlib.compress(raw, 9)).decode().rstrip("=")


def unpack_token(value, kind):
    """Decode a token of `kind`; ValueError with a user-facing message otherwise."""
    value = (value or "").strip()
    if not value.startswith(TOKEN_PREFIX):
        raise ValueError(f"not a {kind}_token from this API")
    body = value[len(TOKEN_PREFIX):]
    try:
        raw = zlib.decompress(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
        data = json.loads(raw)
    except Exception:
        raise ValueError(f"malformed {kind}_token (copy it whole from the previous response)")
    if not isinstance(data, dict) or data.get("k") != kind:
        raise ValueError(f"this is not a {kind}_token")
    return data


# ---- tfs: the server-rendered search page's query ------------------------------------------
# Used only by the token-free fallback when the minter is down.
#   1: 28   2: 2   3: leg {2 date, 4 selected segment {1 from 2 date 3 to 5 airline 6 number},
#   5 max stops, 13 from {1 kind (1 airport, 3 city) 2 id}, 14 to}
#   8: passenger (repeated; 1 adult 2 child 3 infant in seat 4 infant on lap)
#   9: cabin   14: 1   19: trip (1 round trip, 2 one way, 3 multi-city)

def _varint(n):
    out = b""
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out += bytes([b | 0x80])
        else:
            return out + bytes([b])


def _field(num, wire, payload):
    return _varint((num << 3) | wire) + payload


def _pb_str(num, value):
    data = value.encode()
    return _field(num, 2, _varint(len(data)) + data)


def _pb_int(num, value):
    return _field(num, 0, _varint(value))


def _pb_msg(num, data):
    return _field(num, 2, _varint(len(data)) + data)


def _pb_place(num, p):
    return _pb_msg(num, _pb_int(1, 1 if p["kind"] == 0 else 3) + _pb_str(2, p["id"]))


def tfs(trip, legs, *, cabin=1, adults=1, children=0, infants_in_seat=0, infants_on_lap=0):
    """legs: [{'origins': [place], 'destinations': [place], 'date': str,
    'max_stops': int|None, 'selected': [[from, date, to, None, airline, number]]}]"""
    body = _pb_int(1, 28) + _pb_int(2, 2)
    for spec in legs:
        data = _pb_str(2, spec["date"])
        for seg in spec.get("selected") or []:
            data += _pb_msg(4, _pb_str(1, seg[0]) + _pb_str(2, seg[1]) + _pb_str(3, seg[2])
                            + _pb_str(5, seg[4]) + _pb_str(6, seg[5]))
        if spec.get("max_stops") is not None:
            data += _pb_int(5, spec["max_stops"])
        for p in spec["origins"]:
            data += _pb_place(13, p)
        for p in spec["destinations"]:
            data += _pb_place(14, p)
        body += _pb_msg(3, data)
    for code, count in ((1, adults), (2, children), (3, infants_in_seat), (4, infants_on_lap)):
        for _ in range(count or 0):
            body += _pb_int(8, code)
    body += _pb_int(9, cabin) + _pb_int(14, 1) + _pb_int(19, trip)
    return base64.urlsafe_b64encode(body).decode().rstrip("=")
