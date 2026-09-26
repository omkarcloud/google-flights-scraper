"""Route insights from Google Flights' server-rendered route pages (open,
plain curl_cffi):

  /travel/flights/flights-from-<city>.html           from-city
  /travel/flights/flights-to-<city>.html             to-city
  /travel/flights/flights-from-<a>-to-<b>.html       city pair

Each carries: airports near the city (drive time + distance), airlines with
nonstop routes (weekly flights, days, flight time), cheapest trips to / from
popular cities, and for city pairs the cheapest one-way / round-trip /
nonstop / business / last-minute fares, typical prices per month, cheapest
and most expensive month, the fastest flight and per-airline route stats,
plus the destination's description and peak / off seasons.

Slugs are Google's lower-case hyphenated English names (new-york,
los-angeles, sao-paulo); airport codes also work (jfk, lhr).
"""
import html as htmllib
import re

from google_flights import fetch, parsers, places
from google_flights.fetch import GoogleFlightsNotFound
from google_flights.shared import at

_TITLE_RE = re.compile(r"<title>(.*?)</title>", re.S)


def _load(path, locale, what):
    try:
        url, html = fetch.page(path, locale)
    except GoogleFlightsNotFound:
        raise GoogleFlightsNotFound(f"Google Flights has no route page for {what} — use the city's English name "
                                    "(new-york, sao-paulo) or an airport code (jfk)")
    blobs = fetch.af_blobs(html)
    sections = parsers.parse_route_page(blobs, locale["currency"])
    if not sections:
        raise GoogleFlightsNotFound(f"Google Flights has no route data for {what}")
    title = _TITLE_RE.search(html)
    crumbs = {}
    for blk in at(blobs.get("ds:1"), 1) or []:
        if isinstance(blk, list) and blk and blk[0] == 10:
            for row in at(blk, 10, 0) or []:
                level, place = at(row, 0), at(row, 1)
                if isinstance(place, list) and at(place, 1):
                    crumbs[level] = {"id": at(place, 0, 0), "name": at(place, 1)}
    page = {"title": htmllib.unescape(re.sub(r"\s+", " ", title.group(1)).strip()) if title else None,
            "link": url.split("?")[0]}
    return page, crumbs, sections


def _require(crumbs, levels, what):
    """Google answers an unknown slug with its generic landing page (200, no
    breadcrumb for the city) — that is a not-found."""
    if any(not crumbs.get(level) for level in levels):
        raise GoogleFlightsNotFound(f"Google Flights has no route page for {what} — use the city's English name "
                                    "(new-york, sao-paulo) or an airport code (jfk)")


def from_city(city, currency=None, language=None, country=None):
    """/google-flights/routes/from-city"""
    locale = places.locale_of(language, country, currency)
    page, crumbs, sections = _load(f"/travel/flights/flights-from-{city}.html", locale, f"flights from {city}")
    _require(crumbs, (2,), f"flights from {city}")
    return {"origin": crumbs.get(2), "page": page, "currency": locale["currency"], **sections}


def to_city(city, currency=None, language=None, country=None):
    """/google-flights/routes/to-city"""
    locale = places.locale_of(language, country, currency)
    page, crumbs, sections = _load(f"/travel/flights/flights-to-{city}.html", locale, f"flights to {city}")
    _require(crumbs, (3,), f"flights to {city}")
    return {"destination": crumbs.get(3), "page": page, "currency": locale["currency"], **sections}


def city_pair(origin, destination, currency=None, language=None, country=None):
    """/google-flights/routes/city-pair"""
    locale = places.locale_of(language, country, currency)
    page, crumbs, sections = _load(f"/travel/flights/flights-from-{origin}-to-{destination}.html", locale,
                                   f"flights from {origin} to {destination}")
    _require(crumbs, (2, 3), f"flights from {origin} to {destination}")
    return {"origin": crumbs.get(2), "destination": crumbs.get(3), "page": page, "currency": locale["currency"],
            **sections}
