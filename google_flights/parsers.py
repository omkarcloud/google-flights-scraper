"""Google Flights payload parsers (positional JSON -> clean dicts).

Field positions were mapped 2026-09-26 against the web client's own
renderers (the bundle's label tables), e.g. segment[12] is the amenities
message, segment[13] the legroom class, segment[32] the contrail warming
potential, booking option[21][1] the fare features.

Intentionally skipped raw fields (and why):
  * internal ids / request echoes: response[0] (event ids, session token),
    itinerary[17] (render key), itinerary[18] (event id), option[17],
    option[19] (serialized itinerary protobuf), booking token blobs other
    than the one we wrap in booking_token, response[12]/[18]/[30] (tracking
    protobufs), [14] (event ids).
  * duplicated data: segment[30] (legroom long form "31 inches" — segment[14]
    "31 in" kept), response[25] (lowest price, also in price insights),
    per-date price-point tokens in calendars.
  * image thumbnails of airports' cities (response[1]/[17] [2][2]) — generic
    stock thumbnails, not airport photos.
  * unverified meaning (not exposed rather than mislabeled): itinerary[4],
    [5], [9], [12], [19], [23], option[0], [4], [10], [22], [24], Explore
    price entry [15] ([[null, n]] — possibly a hotel price), response[7][4]/[5].
"""
import re

from google_flights.shared import (as_int, as_list, as_str, at, compact, date_of, epoch_ms_date, google_link,
                                   minutes_of, price_of, time_of)

# ---- lookup tables (from the Google Flights bundle's renderers) ------------------------------

WIFI = {1: "available", 2: "free", 3: "for_a_fee"}
SEAT_TYPES = {1: "average_legroom", 2: "below_average_legroom", 3: "above_average_legroom",
              4: "extra_reclining_seat", 5: "lie_flat_seat", 6: "individual_suite",
              8: "standard_recliner_seat", 9: "angled_flat_seat"}
CONTRAIL = {1: "low", 2: "medium", 3: "high"}
EMISSION_LEVELS = {1: "lower", 2: "typical", 3: "higher"}
PRICE_LEVELS = {1: "low", 2: "typical", 3: "typical", 4: "typical", 5: "high"}
TRANSPORT = {2: "train", 3: "bus"}
PLACE_TYPES = {1: "airport", 3: "city", 4: "region", 5: "train_station"}

# fare feature code -> (key, {status: description})
FARE_FEATURES = {
    1: ("ticket_changes", {1: "Free change, possible fare difference", 2: "Ticket changes for a fee", 4: "No ticket changes"}),
    2: ("refunds", {1: "Full refunds", 2: "Partial refund", 4: "No refunds"}),
    3: ("priority_boarding", {1: "Priority boarding", 2: "Priority boarding for a fee", 4: "No priority boarding"}),
    4: ("seat_selection", {1: "Free seat selection", 2: "Seat selection for a fee", 4: "No seat selection"}),
    5: ("upgrades", {2: "Upgrades available", 4: "No upgrades"}),
    6: ("premium_seat", {1: "Premium seat", 2: "Premium seat for a fee"}),
    7: ("mileage", {1: "Mileage counted", 4: "No mileage counted"}),
    8: ("carry_on_bag", {1: "1 free carry-on", 2: "Carry-on bag for a fee", 3: "Carry-on bag for a fee", 4: "No carry-on bags"}),
    9: ("checked_bag", {1: "1st checked bag free", 2: "Checked bag for a fee", 3: "Checked bag for a fee", 4: "No checked bags"}),
    10: ("standard_boarding", {1: "Standard boarding", 2: "Standard boarding for a fee", 4: "Last to board"}),
    11: ("extra_legroom", {1: "Extra legroom", 2: "Extra legroom available for a fee"}),
    12: ("standard_seat", {1: "Standard seat"}),
    13: ("lie_flat_seat", {1: "Lie-flat seat"}),
    14: ("alcoholic_drinks", {1: "Complimentary alcoholic drinks", 2: "Alcoholic drinks for a fee"}),
    15: ("meal", {1: "Free meal service", 2: "Food available for a fee", 4: "No meal service"}),
    17: ("lounge_access", {1: "Lounge access", 2: "Lounge access for a fee", 4: "No lounge access"}),
    18: ("premium_lounge_access", {1: "Premium lounge access", 2: "Premium lounge access for a fee",
                                   4: "No premium lounge access"}),
}
FEATURE_STATUS = {1: "included", 2: "for_a_fee", 3: "for_a_fee", 4: "not_available"}

_DISTANCE_RE = re.compile(r"([\d.,]+)\s*([a-zA-Z]+)")


def _measure(text):
    """'14 mi' -> {'value': 14, 'unit': 'mi'}; '31 in' -> {'value': 31, 'unit': 'in'}."""
    text = as_str(text)
    if not text:
        return None
    m = _DISTANCE_RE.search(text)
    if not m:
        return None
    number = m.group(1).replace(",", "")
    try:
        value = float(number)
    except ValueError:
        return None
    return {"value": int(value) if value.is_integer() else value, "unit": m.group(2)}


# ---- airports / places ---------------------------------------------------------------------

def airport_directory(response):
    """{code: airport} from a shopping response's airport lists (response[17]
    every airport in the results, response[1] the searched ones)."""
    out = {}

    def add(entry):
        code = at(entry, 0, 0)
        if not isinstance(code, str) or code in out:
            return
        lat, lng = at(entry, 3, 0), at(entry, 3, 1)
        out[code] = {
            "code": code,
            "name": as_str(at(entry, 1)),
            "city": as_str(at(entry, 2, 1)),
            "city_id": as_str(at(entry, 2, 0)),
            "country": as_str(at(entry, 6)),
            "country_code": as_str(at(entry, 4)),
            "coordinates": {"latitude": lat, "longitude": lng} if lat is not None and lng is not None else None,
        }

    for entry in as_list(at(response, 17)):
        add(entry)
    for side in as_list(at(response, 1)):
        for group in as_list(side):
            for entry in as_list(group):
                add(entry)
    return out


def _airport(code, name, directory):
    info = (directory or {}).get(code) or {}
    return {"code": code, "name": name or info.get("name"), "city": info.get("city")}


# ---- itineraries ---------------------------------------------------------------------------

def _amenities(raw):
    if not isinstance(raw, list):
        return None
    flag = lambda i: bool(at(raw, i))
    power = "in_seat" if (flag(1) or flag(3)) else "some_seats" if (flag(2) or flag(4)) else None
    usb = "in_seat" if (flag(1) or flag(5)) else "some_seats" if (flag(2) or flag(6)) else None
    entertainment = compact([
        "personal_video_screen" if flag(7) else None,
        "live_tv" if flag(8) else None,
        "on_demand_video" if flag(9) else None,
        "stream_media_to_device" if flag(10) else None,
    ])
    return {"wifi": WIFI.get(at(raw, 11)), "power_outlet": power, "usb_outlet": usb, "entertainment": entertainment}


def parse_segment(seg, directory=None):
    airline_code = as_str(at(seg, 22, 0))
    number = as_str(at(seg, 22, 1))
    transport = TRANSPORT.get(at(seg, 25), "flight")
    legroom = as_str(at(seg, 14))
    flags = at(seg, 18)
    return {
        "flight_number": f"{airline_code} {number}" if airline_code and number else None,
        "airline": {"code": airline_code, "name": as_str(at(seg, 22, 3))},
        "operated_by": as_str(at(seg, 2)),
        "transport": transport,
        "departure": {
            "airport": _airport(as_str(at(seg, 3)), as_str(at(seg, 4)), directory),
            "date": date_of(at(seg, 20)),
            "time": time_of(at(seg, 8)),
        },
        "arrival": {
            "airport": _airport(as_str(at(seg, 6)), as_str(at(seg, 5)), directory),
            "date": date_of(at(seg, 21)),
            "time": time_of(at(seg, 10)),
        },
        "duration_minutes": as_int(at(seg, 11)),
        "aircraft": as_str(at(seg, 17)),
        "seat": {
            "type": SEAT_TYPES.get(at(seg, 13)),
            "legroom": _measure(legroom),
        },
        "amenities": _amenities(at(seg, 12)),
        "emissions": {
            "co2_kg": round(at(seg, 31) / 1000) if isinstance(at(seg, 31), (int, float)) else None,
            "contrail_warming_potential": CONTRAIL.get(at(seg, 32)),
        },
        "is_overnight": bool(at(flags, 0)),
        "is_often_delayed": bool(at(flags, 1)),
        "is_canceled": at(seg, 24, 0) == 1,
    }


def _layover(raw, directory=None):
    arrival_code, departure_code = as_str(at(raw, 1)), as_str(at(raw, 2))
    flags = as_list(at(raw, 3))
    change = bool(arrival_code and departure_code and arrival_code != departure_code) or 2 in flags
    out = {
        "duration_minutes": as_int(at(raw, 0)),
        "airport": _airport(arrival_code, as_str(at(raw, 4)), directory),
        "city": as_str(at(raw, 5)),
        "is_airport_change": change,
        "is_long": 3 in flags,
    }
    out["departure_airport"] = _airport(departure_code, as_str(at(raw, 6)), directory) if change else None
    return out


def _emissions(raw):
    if not isinstance(raw, list):
        return None
    grams, typical = at(raw, 7), at(raw, 8)
    kg = lambda g: round(g / 1000) if isinstance(g, (int, float)) else None
    return {
        "co2_kg": kg(grams),
        "typical_for_route_co2_kg": kg(typical),
        "difference_percent": as_int(at(raw, 3)),
        "level": EMISSION_LEVELS.get(at(raw, 2)),
    }


def parse_itinerary(it, directory=None, currency=None):
    """One itinerary (a result row). Returns the clean dict plus a private
    `_raw_token` (Google's booking token) and `_selection` (the leg selection
    array) that the endpoint wraps into this API's own tokens and pops."""
    head = at(it, 0) or []
    segments = [parse_segment(s, directory) for s in as_list(at(head, 2))]
    layovers = [_layover(l, directory) for l in as_list(at(head, 13))]
    airlines, seen = [], set()
    for seg in segments:
        code = seg["airline"]["code"]
        if code and code not in seen:
            seen.add(code)
            airlines.append(dict(seg["airline"]))
    first = segments[0] if segments else None
    last = segments[-1] if segments else None
    out = {
        "price": price_of(at(it, 1, 0)),
        "currency": currency,
        "airlines": airlines,
        "departure": {
            "airport": _airport(as_str(at(head, 3)), first and first["departure"]["airport"]["name"], directory),
            "date": date_of(at(head, 4)),
            "time": time_of(at(head, 5)),
        },
        "arrival": {
            "airport": _airport(as_str(at(head, 6)), last and last["arrival"]["airport"]["name"], directory),
            "date": date_of(at(head, 7)),
            "time": time_of(at(head, 8)),
        },
        "duration_minutes": as_int(at(head, 9)),
        "stops": max(len(segments) - 1, 0),
        "segments": segments,
        "layovers": layovers,
        "emissions": _emissions(at(head, 22)),
        "_raw_token": as_str(at(it, 1, 1)),
        "_selection": [[s["departure"]["airport"]["code"], s["departure"]["date"], s["arrival"]["airport"]["code"],
                        None, s["airline"]["code"], (s["flight_number"] or " ").split(" ", 1)[-1]]
                       for s in segments],
    }
    return out


def price_insights(raw, currency=None):
    if not isinstance(raw, list):
        return None
    history = []
    for point in as_list(at(raw, 10, 0)):
        day, price = epoch_ms_date(at(point, 0)), at(point, 1)
        if day and isinstance(price, (int, float)):
            history.append({"date": day, "price": price})
    lowest, typical = price_of(at(raw, 1)), price_of(at(raw, 2))
    low, high = price_of(at(raw, 4)), price_of(at(raw, 5))
    return {
        "lowest_price": lowest,
        "price_level": PRICE_LEVELS.get(at(raw, 0)),
        "typical_price": typical,
        "typical_price_range": {"low": low, "high": high} if low is not None or high is not None else None,
        "difference_from_typical": (lowest - typical) if isinstance(lowest, (int, float)) and isinstance(typical, (int, float)) else None,
        "currency": currency,
        "price_history": history,
    }


def result_filters(raw, currency=None):
    if not isinstance(raw, list):
        return None
    pairs = lambda rows, keys: [dict(zip(keys, (as_str(at(r, 0)), as_str(at(r, 1))))) for r in as_list(rows)]
    return {
        "price_range": {"min": price_of(at(raw, 0, 0)), "max": price_of(at(raw, 0, 1)), "currency": currency},
        "duration_range_minutes": {"min": as_int(at(raw, 3, 0)), "max": as_int(at(raw, 3, 1))},
        "layover_range_minutes": {"min": as_int(at(raw, 2, 1)), "max": as_int(at(raw, 2, 2))},
        "airlines": pairs(at(raw, 1, 1), ("code", "name")),
        "alliances": pairs(at(raw, 1, 0), ("code", "name")),
        "connecting_airports": pairs(at(raw, 2, 0), ("code", "city")),
    }


def airline_policies(response):
    """Baggage (response[11]) and accessibility (response[26]) policy links per airline."""
    out = {}
    for row in as_list(at(response, 11)):
        code = as_str(at(row, 0))
        if code:
            out.setdefault(code, {"code": code, "name": as_str(at(row, 1)), "baggage_link": None,
                                  "accessibility_link": None})["baggage_link"] = as_str(at(row, 2))
    for row in as_list(at(response, 26)):
        code = as_str(at(row, 0))
        if code:
            out.setdefault(code, {"code": code, "name": as_str(at(row, 1)), "baggage_link": None,
                                  "accessibility_link": None})["accessibility_link"] = as_str(at(row, 2))
    return list(out.values())


def parse_shopping(response, currency=None):
    """GetShoppingResults payload (or the search page's ds:1) ->
    (best, other, extras). Best/other are parsed itineraries."""
    directory = airport_directory(response)
    best = [parse_itinerary(it, directory, currency) for it in as_list(at(response, 2, 0))]
    other = [parse_itinerary(it, directory, currency) for it in as_list(at(response, 3, 0))]
    extras = {
        "price_insights": price_insights(at(response, 5), currency),
        "filters": result_filters(at(response, 7), currency),
        "airline_policies": airline_policies(response),
        "airports": directory,
    }
    return best, other, extras


# ---- booking options -------------------------------------------------------------------------

def _fare_features(raw):
    out = []
    for row in as_list(raw):
        code, status = at(row, 0), at(row, 1)
        spec = FARE_FEATURES.get(code)
        if not spec:
            continue
        key, labels = spec
        out.append({"feature": key, "availability": FEATURE_STATUS.get(status), "description": labels.get(status)})
    return out


def _bag_fees(raw, currency):
    fees = []
    for i, row in enumerate(as_list(raw), 1):
        price = price_of(at(row, 1, 0))
        if price is not None:
            fees.append({"bag_number": i, "price": price, "currency": currency})
    return fees


def _click_link(opt):
    link = at(opt, 5, 2)
    if not (isinstance(link, list) and as_str(at(link, 0))):
        return None
    params = "&".join(f"{at(p, 0)}={at(p, 1)}" for p in as_list(at(link, 1)) if at(p, 0))
    return as_str(at(link, 0)) + ("?" + params if params else "")


def _seller(opt):
    sellers = as_list(at(opt, 1))
    names = [as_str(at(s, 1)) for s in sellers if as_str(at(s, 1))]
    return {
        "name": " + ".join(names) or None,
        "airline_code": as_str(at(sellers, 0, 0)) if at(sellers, 0, 3) and len(sellers) == 1 else None,
        "is_airline": bool(sellers) and all(bool(at(s, 3)) for s in sellers),
        "website": as_str(at(opt, 5, 0)),
    }


def _flights_of(opt):
    return [f"{at(f, 0)} {at(f, 1)}" for f in as_list(at(opt, 3)) if at(f, 0) and at(f, 1)]


def parse_booking_option(opt, currency=None):
    """One seller offer. option[0] == 5 marks separate tickets booked
    together: option[2] then holds one sub-offer (seller, flights, link) per
    ticket and the top-level option has no link of its own."""
    fare = at(opt, 21)
    tickets = []
    for sub in as_list(at(opt, 2)):
        if isinstance(sub, list) and at(sub, 1):
            tickets.append({"seller": _seller(sub), "flights": _flights_of(sub),
                            "google_booking_link": _click_link(sub), "booking_link": None})
    return {
        "seller": _seller(opt),
        "price": price_of(at(opt, 7, 0)),
        "currency": currency,
        "fare_name": as_str(at(fare, 3)),
        "fare_code": as_str(at(fare, 0, 1)),
        "is_basic_economy": bool(at(fare, 2)),
        "is_separate_tickets": bool(tickets),
        "flights": _flights_of(opt),
        "features": _fare_features(at(fare, 1)),
        "checked_bag_fees": _bag_fees(at(opt, 18), currency),
        "google_booking_link": _click_link(opt),
        "booking_link": None,
        "tickets": tickets,
    }


def parse_booking(chunks, currency=None):
    """GetBookingResults chunks -> (options, price_insights, itinerary_raws,
    baggage links). The option list arrives in the last chunk that has one."""
    options, insights, legs, baggage = [], None, [], []
    for chunk in chunks or []:
        body = at(chunk, 1)
        rows = at(body, 0)
        if isinstance(rows, list) and rows and isinstance(at(rows, 0), list) and at(rows, 0, 7) is not None:
            options = [parse_booking_option(o, currency) for o in rows]
        if at(body, 12) is not None:
            insights = price_insights(at(body, 12), currency)
        # body[5] = [[itinerary head, ...], 1, [1]] — bare heads (no price
        # wrapper), one per chosen leg; wrap them like result rows.
        heads = at(body, 5, 0)
        if isinstance(heads, list) and heads and isinstance(at(heads, 0, 0), str):
            legs = [[head] for head in heads]
        for row in as_list(at(body, 9)):
            code = as_str(at(row, 0))
            if code and all(b["code"] != code for b in baggage):
                baggage.append({"code": code, "name": as_str(at(row, 1)), "baggage_link": as_str(at(row, 2))})
    return options, insights, legs, baggage


# ---- calendars ---------------------------------------------------------------------------------

def calendar_points(payload, currency=None):
    out = []
    for point in as_list(at(payload, 1)):
        price = price_of(at(point, 2, 0))
        out.append({
            "departure_date": date_of(at(point, 0)),
            "return_date": date_of(at(point, 1)),
            "price": price,
            "currency": currency if price is not None else None,
        })
    return out


# ---- explore ------------------------------------------------------------------------------------

def parse_explore(chunks, currency=None):
    places, prices = {}, {}
    for chunk in chunks or []:
        for p in as_list(at(chunk, 3, 0)):
            if as_str(at(p, 0)):
                places[at(p, 0)] = p
        for q in as_list(at(chunk, 4, 0)):
            if as_str(at(q, 0)):
                prices[at(q, 0)] = q
    out = []
    for pid, p in places.items():
        q = prices.get(pid)
        info = at(q, 6)
        price = price_of(at(q, 1, 0))
        code = as_str(at(info, 0))
        names = as_str(at(info, 1))
        lat, lng = at(p, 1, 0), at(p, 1, 1)
        via = None
        if at(q, 10) == 2 and as_str(at(info, 7)):
            via = {"id": as_str(at(info, 10)), "name": as_str(at(info, 7))}
        out.append({
            "id": pid,
            "name": as_str(at(p, 2)),
            "country": as_str(at(p, 4)),
            "airport_code": as_str(at(p, 15)) or as_str(at(info, 5)),
            "coordinates": {"latitude": lat, "longitude": lng} if lat is not None and lng is not None else None,
            "image": as_str(at(p, 7)) or as_str(at(p, 3)),
            "departure_date": date_of(at(p, 11)) or date_of(at(p, 29)),
            "return_date": date_of(at(p, 12)),
            "price": price,
            "currency": currency if price is not None else None,
            "airline": ({"code": None if code == "multi" else code, "name": names}
                        if (code or names) else None),
            "stops": as_int(at(info, 2)),
            "flight_duration_minutes": as_int(at(info, 3)),
            "via_city": via,
        })
    out.sort(key=lambda d: (d["price"] is None, d["price"] or 0))
    return out


def explore_bounds(chunks):
    raw = at(chunks, 0, 2, 1)
    if not isinstance(raw, list):
        return None
    return {"north": at(raw, 0, 0), "east": at(raw, 0, 1), "south": at(raw, 1, 0), "west": at(raw, 1, 1)}


# ---- autocomplete ---------------------------------------------------------------------------------

def _suggest_place(raw):
    """[type, full name, short name, description, geo id, airport code, ...]
    — for airports / stations the short name is their CITY."""
    kind = PLACE_TYPES.get(at(raw, 0)) or PLACE_TYPES.get(at(raw, 11))
    code = as_str(at(raw, 5))
    geo = as_str(at(raw, 4))
    if kind in ("airport", "train_station"):
        return {"type": kind, "id": code or geo, "name": as_str(at(raw, 1)), "full_name": as_str(at(raw, 1)),
                "description": as_str(at(raw, 3)), "airport_code": code, "city": as_str(at(raw, 2)), "city_id": geo}
    return {"type": kind, "id": geo, "name": as_str(at(raw, 2)), "full_name": as_str(at(raw, 1)),
            "description": as_str(at(raw, 3)), "airport_code": None, "city": None, "city_id": None}


def parse_autocomplete(payload):
    out = []
    for group in as_list(at(payload, 0)):
        place = _suggest_place(at(group, 0))
        nearby = []
        for row in as_list(at(group, 1)):
            airport = _suggest_place(at(row, 0))
            nearby.append({"type": airport["type"], "code": airport["airport_code"], "name": airport["name"],
                           "city": airport["city"], "distance": _measure(at(row, 1))})
        place["nearby_airports"] = nearby
        out.append(place)
    return out


# ---- reference lists ----------------------------------------------------------------------------

def reference_lists(blobs):
    """Currencies / languages / countries from any Flights page's ds blobs
    (shape-detected: the block keys move between pages)."""
    out = {"currencies": [], "languages": [], "countries": []}
    for data in blobs.values():
        rows = at(data, 0)
        if not isinstance(rows, list) or not rows or not isinstance(rows[0], list):
            continue
        first = rows[0]
        if len(first) == 4 and isinstance(at(first, 0), str) and len(first[0]) == 3 and first[0].isupper():
            out["currencies"] = [{"code": r[0], "name": as_str(at(r, 1)), "symbol": as_str(at(r, 2)),
                                  "is_popular": bool(at(r, 3))} for r in rows if isinstance(r, list)]
        elif len(first) == 3 and all(isinstance(x, str) for x in first):
            out["languages"] = [{"code": r[0], "name": as_str(at(r, 2)), "native_name": as_str(at(r, 1))}
                                for r in rows if isinstance(r, list)]
        elif len(first) == 2 and all(isinstance(x, str) for x in first) and len(first[0]) == 2:
            out["countries"] = [{"code": r[0], "name": as_str(at(r, 1))} for r in rows if isinstance(r, list)]
    return out


# ---- route insight pages (/travel/flights/flights-from-X[-to-Y].html) ---------------------------

def _deal(raw, currency):
    """[[price, token], None, depart, return, from, to, hours, minutes, stops, [codes], [names]]"""
    if not isinstance(raw, list):
        return None
    price = price_of(at(raw, 0, 0))
    codes = [c for c in as_list(at(raw, 9)) if isinstance(c, str)]
    names = [n for n in as_list(at(raw, 10)) if isinstance(n, str)]
    return {
        "price": price,
        "currency": currency if price is not None else None,
        "departure_date": date_of(at(raw, 2)),
        "return_date": date_of(as_str(at(raw, 3))),
        "origin_airport_code": as_str(at(raw, 4)),
        "destination_airport_code": as_str(at(raw, 5)),
        "duration_minutes": minutes_of(at(raw, 6), at(raw, 7)),
        "stops": as_int(at(raw, 8)),
        "airlines": [{"code": c, "name": n} for c, n in zip(codes, names)] if codes else [{"code": None, "name": n} for n in names],
    }


def _route_airports(rows):
    out = []
    for r in as_list(rows):
        out.append({
            "code": as_str(at(r, 0)),
            "name": as_str(at(r, 3)),
            "city": as_str(at(r, 4)),
            "country": as_str(at(r, 6)),
            "drive_time_minutes": as_int(at(r, 1)),
            "distance": _measure(at(r, 2)),
            "is_main_airport": bool(at(r, 5)),
        })
    return out


def _weekdays(raw):
    names = {1: "sunday", 2: "monday", 3: "tuesday", 4: "wednesday", 5: "thursday", 6: "friday", 7: "saturday"}
    return [names[d] for d in as_list(raw) if d in names]


def _geo(raw):
    return {"id": as_str(at(raw, 0, 0)), "name": as_str(at(raw, 1))}


def _route_link(raw):
    return google_link(at(raw, 2, 2))


def parse_route_page(blobs, currency=None):
    """The ds:1 block list of a route insight page -> sections."""
    blocks = {}
    for blk in as_list(at(blobs.get("ds:1"), 1)):
        if isinstance(blk, list) and blk and isinstance(blk[0], int):
            blocks.setdefault(blk[0], blk)
    out = {}

    # 13: airports near origin / destination
    near = at(blocks.get(13), 13)
    if isinstance(near, list):
        out["origin_airports"] = _route_airports(at(near, 0))
        out["destination_airports"] = _route_airports(at(near, 1))

    # cheapest deals by category
    categories = {6: "cheapest_one_way", 7: "cheapest_round_trip", 20: "cheapest_nonstop",
                  19: "cheapest_business_class", 21: "last_minute_weekend"}
    deals = {}
    for code, key in categories.items():
        blk = blocks.get(code)
        if blk:
            deals[key] = _deal(at(blk, code, 0), currency)
    if deals:
        out["deals"] = deals

    # 4: fastest flight
    fastest = at(blocks.get(4), 4)
    if isinstance(fastest, list):
        out["fastest_flight_minutes"] = as_int(at(fastest, 0))
        out["has_nonstop_flights"] = at(fastest, 1) == 1

    # 5: typical price per month for 1-week trips
    months = at(blocks.get(5), 5)
    if isinstance(months, list):
        def month_row(r):
            return {"month": as_int(at(r, 0)), "typical_price_low": price_of(at(r, 1, 0)),
                    "typical_price_high": price_of(at(r, 2, 0)), "currency": currency}
        out["price_by_month"] = [month_row(r) for r in as_list(at(months, 0))]
        out["cheapest_month"] = as_int(at(months, 1, 0, 0))
        out["most_expensive_month"] = as_int(at(months, 2, 0, 0))

    # 15: airlines on the route (city pair pages)
    rows = at(blocks.get(15), 15, 0)
    if isinstance(rows, list):
        airlines = []
        for r in rows:
            airlines.append({
                "code": as_str(at(r, 0)),
                "name": as_str(at(r, 1)),
                "typical_price_range": {"low": price_of(at(r, 4, 0, 0)), "high": price_of(at(r, 4, 1, 0)),
                                        "currency": currency},
                "weekly_nonstop_flights": as_int(at(r, 7)),
                "weekly_connecting_flights": as_int(at(r, 6)),
                "nonstop_days": _weekdays(at(r, 9)),
                "connecting_days": _weekdays(at(r, 10)),
                "nonstop_origin_airports": [a for a in as_list(at(r, 11)) if isinstance(a, str)],
                "nonstop_destination_airports": [a for a in as_list(at(r, 12)) if isinstance(a, str)],
                "connecting_origin_airports": [a for a in as_list(at(r, 2)) if isinstance(a, str)],
                "connecting_destination_airports": [a for a in as_list(at(r, 3)) if isinstance(a, str)],
                "cheapest_round_trip": _deal(at(r, 8), currency),
            })
        out["airlines"] = airlines

    # 17: airlines flying nonstop from / to the city, with their top routes
    rows = at(blocks.get(17), 17, 0)
    if isinstance(rows, list):
        carriers = []
        for r in rows:
            routes = []
            for route in as_list(at(r, 4)):
                routes.append({
                    "city": _geo(at(route, 0)),
                    "weekly_flights": as_int(at(route, 1)),
                    "days": _weekdays(at(route, 2)),
                    "flight_duration_minutes": as_int(at(route, 3)),
                })
            carriers.append({
                "code": as_str(at(r, 0)),
                "name": as_str(at(r, 1)),
                "airports": [a for a in as_list(at(r, 2)) if isinstance(a, str)],
                "nonstop_destinations_count": as_int(at(r, 3)),
                "top_routes": routes,
            })
        out["nonstop_airlines"] = carriers

    # 11 (from-city pages: trips TO other cities) / 16 (to-city pages: trips
    # FROM other cities): [0] depart, [1] return, [16] airline code ("multi"),
    # [17] airline names, [18] stops, [19] minutes, [24] the other city,
    # [27] [price, token], [29] destination airport, [49] origin airport.
    trips = []
    for code, side in ((11, "destination"), (16, "origin")):
        for r in as_list(at(blocks.get(code), code, 0)):
            price = price_of(at(r, 27, 0))
            city = at(r, 24)
            lat, lng = at(city, 3, 0), at(city, 3, 1)
            trips.append({
                side: {"id": as_str(at(city, 0, 0)), "name": as_str(at(city, 1)),
                       "image": as_str(at(city, 2, 2, 0, 0)),
                       "coordinates": {"latitude": lat, "longitude": lng} if lat is not None and lng is not None else None},
                "price": price,
                "currency": currency if price is not None else None,
                "departure_date": date_of(at(r, 0)),
                "return_date": date_of(as_str(at(r, 1))),
                "airline": {"code": None if at(r, 16) == "multi" else as_str(at(r, 16)), "name": as_str(at(r, 17))},
                "stops": as_int(at(r, 18)),
                "duration_minutes": as_int(at(r, 19)),
                "origin_airport_code": as_str(at(r, 49)),
                "destination_airport_code": as_str(at(r, 29)),
            })
    if trips:
        out["popular_trips"] = trips

    # 12: destination description + seasons
    about = at(blocks.get(12), 12)
    if isinstance(about, list):
        out["destination_info"] = {
            "description": as_str(at(about, 0)),
            "image": as_str(at(about, 1, 1, 0)),
            "peak_season": as_str(at(about, 2)),
            "off_season": as_str(at(about, 3)),
        }

    # 8: overview text (seasonality)
    overview = at(blocks.get(8), 8)
    if isinstance(overview, list):
        text = as_str(at(overview, 6, 0))
        if text:
            out.setdefault("destination_info", {})["when_to_visit"] = text

    # 9: related routes
    related = []
    for group in as_list(at(blocks.get(9), 9)):
        for r in as_list(group):
            origin, destination = at(r, 0), at(r, 1)
            related.append({
                "origin": _geo(origin) if isinstance(origin, list) else None,
                "destination": _geo(destination) if isinstance(destination, list) else None,
                "link": _route_link(r),
            })
    if related:
        out["related_routes"] = related
    return out
