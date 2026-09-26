"""Flight search endpoints: one-way, round-trip (+ returning flights for a
chosen outbound), multi-city (+ next leg), booking options, price insights.

Flow (same as the Google Flights UI):
  one-way      -> each result carries a booking_token
  round-trip   -> each OUTBOUND result carries a returning_token
                  /flights/returning-flights -> each result a booking_token
  multi-city   -> each first-leg result carries a next_leg_token
                  /flights/multi-city/next-leg -> ... the last leg's results
                  carry booking_tokens
  booking_token -> /flights/booking-options (who sells it, fare features,
                  bag fees, links)

Data comes from GetShoppingResults / GetBookingResults over curl_cffi with a
BotGuard token (fetch.rpc). When the token browser is down, one-way and
round-trip searches fall back to the token-free server-rendered search page
(same payload shape) and apply the filters it cannot express locally.
"""
from concurrent.futures import ThreadPoolExecutor

import config
from google_flights import fetch, parsers, places, protocol
from google_flights.fetch import GoogleFlightsBadRequest, GoogleFlightsNotFound, GoogleFlightsUpstreamError
from google_flights.minter import MinterUnavailable
from google_flights.shared import CABIN_CODES

SORT_KEYS = {
    "price": lambda it: (it["price"] is None, it["price"] or 0),
    "departure_time": lambda it: (it["departure"]["date"] or "", it["departure"]["time"] or ""),
    "arrival_time": lambda it: (it["arrival"]["date"] or "", it["arrival"]["time"] or ""),
    "duration": lambda it: it["duration_minutes"] or 0,
    "emissions": lambda it: ((it["emissions"] or {}).get("co2_kg") or 0),
}


# ---- request assembly ------------------------------------------------------------------------

def _locale(currency, language, country):
    return places.locale_of(language, country, currency)


def _leg_options(kw, prefix=""):
    return {
        "departure_hours": kw.get(prefix + "departure_hours"),
        "arrival_hours": kw.get(prefix + "arrival_hours"),
        "stops": protocol.STOPS[kw.get("stops") or "any"],
        "airlines": kw.get("airlines"),
        "max_duration": kw.get("max_duration_minutes"),
        "connecting": kw.get("connecting_airports"),
        "max_layover": kw.get("max_layover_minutes"),
        "less_emissions": bool(kw.get("less_emissions")),
    }


def _request(trip, legs, kw, **extra):
    return protocol.request(
        trip, legs,
        cabin=CABIN_CODES[kw.get("cabin_class") or "economy"],
        adults=kw.get("adults") or 1, children=kw.get("children") or 0,
        infants_in_seat=kw.get("infants_in_seat") or 0, infants_on_lap=kw.get("infants_on_lap") or 0,
        max_price=kw.get("max_price"),
        carry_on_bags=kw.get("carry_on_bags") or 0, checked_bags=kw.get("checked_bags") or 0,
        **extra,
    )


def _pack_locale(locale):
    return [locale["language"], locale["country"], locale["currency"]]


def _unpack_locale(value):
    language, country, currency = (list(value or []) + [None, None, None])[:3]
    return places.locale_of(language, country, currency)


# ---- shopping ----------------------------------------------------------------------------------

def _shop(req, sort, locale):
    chunks = fetch.rpc("GetShoppingResults", protocol.shopping_body(req, protocol.SORTS[sort or "best"]), locale)
    return chunks[0]


def _results(payload, locale, tokenize, filters=None, sort=None):
    currency = locale["currency"]
    best, other, extras = parsers.parse_shopping(payload, currency)
    if filters:
        best = [it for it in best if _passes(it, filters)]
        other = [it for it in other if _passes(it, filters)]
    if sort and sort != "best" and filters is not None:
        other = sorted(best + other, key=SORT_KEYS[sort])
        best = []
    for it in best + other:
        raw_token = it.pop("_raw_token", None)
        selection = it.pop("_selection", None)
        tokenize(it, raw_token, selection)
    return {
        "price_insights": extras["price_insights"],
        "best_flights": best,
        "other_flights": other,
        "filters": extras["filters"],
        "airline_policies": extras["airline_policies"],
        "airports": list(extras["airports"].values()),
    }


def _search_meta(trip_type, origins, destinations, kw, **extra):
    meta = {
        "trip_type": trip_type,
        "origin": [o["id"] for o in origins],
        "destination": [d["id"] for d in destinations],
        "departure_date": kw.get("departure_date"),
    }
    meta.update(extra)
    meta.update({
        "cabin_class": kw.get("cabin_class") or "economy",
        "passengers": {"adults": kw.get("adults") or 1, "children": kw.get("children") or 0,
                       "infants_in_seat": kw.get("infants_in_seat") or 0,
                       "infants_on_lap": kw.get("infants_on_lap") or 0},
        "currency": kw.get("currency") or "USD",
    })
    return meta


# ---- token-free fallback (minter down) ---------------------------------------------------------

def _fallback_filters(kw, prefix=""):
    """Filters the search page cannot express, applied locally; None when a
    requested option cannot be honoured without the RPC (alliances, bags)."""
    airlines = kw.get("airlines") or []
    if any(a in protocol.ALLIANCES for a in airlines) or kw.get("carry_on_bags") or kw.get("checked_bags"):
        return None
    return {
        "airlines": set(airlines), "max_price": kw.get("max_price"),
        "departure_hours": kw.get(prefix + "departure_hours"), "arrival_hours": kw.get(prefix + "arrival_hours"),
        "max_duration": kw.get("max_duration_minutes"), "max_layover": kw.get("max_layover_minutes"),
        "connecting": set(kw.get("connecting_airports") or []), "less_emissions": bool(kw.get("less_emissions")),
    }


def _hour(time_text):
    try:
        return int((time_text or "")[:2])
    except ValueError:
        return None


def _passes(it, f):
    if f["airlines"] and not all(a["code"] in f["airlines"] for a in it["airlines"]):
        return False
    if f["max_price"] and (it["price"] is None or it["price"] > f["max_price"]):
        return False
    for key, side in (("departure_hours", "departure"), ("arrival_hours", "arrival")):
        window = f[key]
        hour = _hour(it[side]["time"])
        if window and (hour is None or not (window[0] <= hour <= window[1])):
            return False
    if f["max_duration"] and (it["duration_minutes"] or 0) > f["max_duration"]:
        return False
    if f["max_layover"] and any((l["duration_minutes"] or 0) > f["max_layover"] for l in it["layovers"]):
        return False
    if f["connecting"] and any(l["airport"]["code"] not in f["connecting"] for l in it["layovers"]):
        return False
    if f["less_emissions"] and (it["emissions"] or {}).get("level") != "lower":
        return False
    return True


_TFS_STOPS = {0: None, 1: 0, 2: 1, 3: 2}


def _page_payload(trip, leg_specs, kw, locale):
    tfs = protocol.tfs(
        trip, leg_specs, cabin=CABIN_CODES[kw.get("cabin_class") or "economy"],
        adults=kw.get("adults") or 1, children=kw.get("children") or 0,
        infants_in_seat=kw.get("infants_in_seat") or 0, infants_on_lap=kw.get("infants_on_lap") or 0)
    _, html = fetch.page("/travel/flights/search", locale, {"tfs": tfs})
    payload = fetch.af_blobs(html).get("ds:1")
    if not isinstance(payload, list):
        raise GoogleFlightsUpstreamError("the search page carried no results")
    return payload


def _tfs_leg(origins, destinations, day, kw, selected=None):
    return {"origins": origins, "destinations": destinations, "date": day,
            "max_stops": _TFS_STOPS[protocol.STOPS[kw.get("stops") or "any"]], "selected": selected}


# ---- endpoints -----------------------------------------------------------------------------------

def search_one_way(origin, destination, departure_date, currency=None, language=None, country=None, **kw):
    """/google-flights/flights/search-one-way"""
    locale = _locale(currency, language, country)
    kw.update(departure_date=departure_date, currency=locale["currency"])
    origins, destinations = places.resolve(origin, locale), places.resolve(destination, locale)
    req = _request(protocol.TRIP_ONE_WAY, [protocol.leg(origins, destinations, departure_date, **_leg_options(kw))], kw)
    sort = kw.get("sort_by") or "best"

    def tokenize(it, raw, selection):
        it["booking_token"] = (protocol.pack_token("booking", g=raw, q=req, s=[selection], l=_pack_locale(locale))
                               if raw else None)

    try:
        out = _results(_shop(req, sort, locale), locale, tokenize)
    except MinterUnavailable:
        local = _fallback_filters(kw)
        if local is None:
            raise
        payload = _page_payload(protocol.TRIP_ONE_WAY, [_tfs_leg(origins, destinations, departure_date, kw)], kw, locale)
        out = _results(payload, locale, tokenize, filters=local, sort=sort)
    return {"search": _search_meta("one_way", origins, destinations, kw), **out}


def search_round_trip(origin, destination, departure_date, return_date, currency=None, language=None,
                      country=None, **kw):
    """/google-flights/flights/search-round-trip — outbound options priced
    as round trips; pick one with its returning_token."""
    locale = _locale(currency, language, country)
    kw.update(departure_date=departure_date, currency=locale["currency"])
    origins, destinations = places.resolve(origin, locale), places.resolve(destination, locale)
    legs = [protocol.leg(origins, destinations, departure_date, **_leg_options(kw)),
            protocol.leg(destinations, origins, return_date, **_leg_options(kw, "return_"))]
    req = _request(protocol.TRIP_ROUND, legs, kw)
    sort = kw.get("sort_by") or "best"

    def tokenize(it, raw, selection):
        it["returning_token"] = protocol.pack_token("returning", q=req, s=[selection], l=_pack_locale(locale), o=sort)

    try:
        out = _results(_shop(req, sort, locale), locale, tokenize)
    except MinterUnavailable:
        local = _fallback_filters(kw)
        if local is None:
            raise
        payload = _page_payload(protocol.TRIP_ROUND, [_tfs_leg(origins, destinations, departure_date, kw),
                                                      _tfs_leg(destinations, origins, return_date, kw)], kw, locale)
        out = _results(payload, locale, tokenize, filters=local, sort=sort)
    return {"search": _search_meta("round_trip", origins, destinations, kw, return_date=return_date), **out}


def returning_flights(returning_token):
    """/google-flights/flights/returning-flights — return options for the
    chosen outbound; prices are round-trip totals."""
    data = returning_token
    locale = _unpack_locale(data.get("l"))
    req = protocol.with_selection(data["q"], data["s"])

    def tokenize(it, raw, selection):
        it["booking_token"] = (protocol.pack_token("booking", g=raw, q=data["q"], s=data["s"] + [selection],
                                                   l=data.get("l")) if raw else None)

    out = _results(_shop(req, data.get("o"), locale), locale, tokenize)
    return {"selected_flights": _selected_summary(data["s"]), **out}


def search_multi_city(legs, currency=None, language=None, country=None, **kw):
    """/google-flights/flights/search-multi-city — options for the FIRST leg,
    priced for the whole trip; continue with next_leg_token."""
    locale = _locale(currency, language, country)
    kw.update(currency=locale["currency"])
    built, resolved = [], []
    for spec in legs:
        origins, destinations = places.resolve(spec["origin"], locale), places.resolve(spec["destination"], locale)
        resolved.append({"origin": [o["id"] for o in origins], "destination": [d["id"] for d in destinations],
                         "date": spec["date"]})
        built.append(protocol.leg(origins, destinations, spec["date"], **_leg_options(kw)))
    req = _request(protocol.TRIP_MULTI_CITY, built, kw)
    sort = kw.get("sort_by") or "best"
    total = len(built)

    def tokenize(it, raw, selection):
        it["next_leg_token"] = protocol.pack_token("next_leg", q=req, s=[selection], l=_pack_locale(locale), o=sort)

    out = _results(_shop(req, sort, locale), locale, tokenize)
    return {"search": {"trip_type": "multi_city", "legs": resolved, "current_leg": 1, "total_legs": total,
                       "cabin_class": kw.get("cabin_class") or "economy", "currency": locale["currency"]}, **out}


def multi_city_next_leg(next_leg_token):
    """/google-flights/flights/multi-city/next-leg — options for the next leg
    given the choices so far; the last leg's results carry booking_tokens."""
    data = next_leg_token
    locale = _unpack_locale(data.get("l"))
    req = protocol.with_selection(data["q"], data["s"])
    total = len(data["q"][13])
    current = len(data["s"]) + 1
    if current > total:
        raise GoogleFlightsBadRequest("every leg is already chosen — use the booking_token")
    last = current == total

    def tokenize(it, raw, selection):
        if last:
            it["booking_token"] = (protocol.pack_token("booking", g=raw, q=data["q"], s=data["s"] + [selection],
                                                       l=data.get("l")) if raw else None)
        else:
            it["next_leg_token"] = protocol.pack_token("next_leg", q=data["q"], s=data["s"] + [selection],
                                                       l=data.get("l"), o=data.get("o"))

    out = _results(_shop(req, data.get("o"), locale), locale, tokenize)
    return {"current_leg": current, "total_legs": total, "selected_flights": _selected_summary(data["s"]), **out}


def _selected_summary(selections):
    out = []
    for leg_index, sel in enumerate(selections, 1):
        out.append({"leg": leg_index, "flights": [
            {"flight_number": f"{s[4]} {s[5]}", "origin": s[0], "destination": s[2], "date": s[1]} for s in sel]})
    return out


def _resolve_links(options):
    """Follow every google.com/travel/clk link (options and their separate
    tickets) to the seller's own booking URL, in parallel."""
    todo = [o for o in options if o.get("google_booking_link")]
    todo += [t for o in options for t in o.get("tickets") or [] if t.get("google_booking_link")]
    if not todo:
        return
    with ThreadPoolExecutor(max_workers=max(1, config.GOOGLE_FLIGHTS_LINK_WORKERS)) as pool:
        for item, link in zip(todo, pool.map(lambda o: fetch.resolve_redirect(o["google_booking_link"]), todo)):
            item["booking_link"] = link


def booking_options(booking_token, resolve_links=True):
    """/google-flights/flights/booking-options — every seller of the chosen
    itinerary with price, fare name, fare features (changes, refunds, seat
    selection, bags ...), checked-bag fees and the booking link."""
    data = booking_token
    locale = _unpack_locale(data.get("l"))
    if not data.get("g"):
        raise GoogleFlightsBadRequest("this itinerary has no bookable price")
    req = protocol.with_selection(data["q"], data["s"])
    chunks = fetch.rpc("GetBookingResults", protocol.booking_body(data["g"], req), locale)
    options, insights, legs, baggage = parsers.parse_booking(chunks, locale["currency"])
    if not options:
        raise GoogleFlightsNotFound("Google Flights shows no booking options for this itinerary any more "
                                    "(prices change — search again)")
    if resolve_links is not False:
        _resolve_links(options)
    flights = []
    for raw in legs:
        it = parsers.parse_itinerary(raw, None, locale["currency"])
        it.pop("_raw_token", None)
        it.pop("_selection", None)
        it.pop("price", None)
        it.pop("currency", None)
        flights.append(it)
    options.sort(key=lambda o: (o["price"] is None, o["price"] or 0))
    return {
        "selected_flights": flights,
        "lowest_price": options[0]["price"],
        "currency": locale["currency"],
        "booking_options": options,
        "price_insights": insights,
        "baggage_policies": baggage,
    }


def price_insights(origin, destination, departure_date, return_date=None, currency=None, language=None,
                   country=None, **kw):
    """/google-flights/flights/price-insights — Google's verdict on the
    current price (low / typical / high), the typical range and the price
    history for the route and dates."""
    if return_date:
        result = search_round_trip(origin, destination, departure_date, return_date, currency, language, country, **kw)
    else:
        result = search_one_way(origin, destination, departure_date, currency, language, country, **kw)
    insights = result.get("price_insights")
    if not insights:
        raise GoogleFlightsNotFound("Google Flights has no price insights for this route and dates")
    return {"search": result["search"], "price_insights": insights}
