"""Google Flights input parsing: ONE param per input that auto-detects its
forms (tripadvisor QueryOrLinkField convention — never a sibling url / id
pair). Offline; free-text names are resolved to places by places.resolve()
at call time (autocomplete).

  place (origin / destination / explore origin)
      JFK                      airport IATA code
      /m/02_286                city / region / country id (from /airports/search)
      NYC, LON, PAR ...        metro codes -> their city id
      New York                 free text -> resolved with autocomplete
      JFK,EWR,LGA              several of the above (comma list)
  legs (multi-city)
      JFK,LHR,2026-11-20|LHR,CDG,2026-11-24|CDG,JFK,2026-11-28
      (legs split by "|", each origin,destination,date; "+" joins several
      airports: JFK+EWR,LHR,2026-11-20) — or a JSON array
      [{"origin": "JFK", "destination": "LHR", "date": "2026-11-20"}, ...]
  city (route insight pages)
      New York | new-york | JFK | a google.com/travel/flights/flights-from-new-york.html link
  hours
      6-18                     from 06:00 up to 18:59 (hours 0-23)
  region (Explore)
      europe | asia | ... | north,east,south,west bounds ("71,40,35,-25")
"""
import json
import re
import unicodedata
from datetime import date, datetime
from urllib.parse import urlparse

from google_flights import protocol

MAX_PLACES = 7
MAX_LEGS = 6

INTERESTS = {"outdoors": "/g/11bc58l13w", "beaches": "/m/0b3yr", "museums": "/m/09cmq",
             "history": "/m/03g3w", "skiing": "/m/071k0"}

# [[north, east], [south, west]]
REGIONS = {
    "europe": [[71, 40], [35, -25]],
    "asia": [[55, 150], [-10, 60]],
    "southeast_asia": [[24, 142], [-11, 92]],
    "middle_east": [[42, 63], [12, 26]],
    "africa": [[38, 52], [-35, -18]],
    "north_america": [[72, -52], [15, -168]],
    "united_states": [[49, -66], [24, -125]],
    "central_america": [[23, -77], [7, -118]],
    "caribbean": [[27, -59], [10, -86]],
    "south_america": [[13, -34], [-56, -82]],
    "oceania": [[0, 180], [-48, 110]],
}

_FREE_TEXT_RE = re.compile(r"^[^\x00-\x1f<>{}\[\]\\|]{2,80}$")
_HOURS_RE = re.compile(r"^(\d{1,2})\s*-\s*(\d{1,2})$")
_AIRLINE_RE = re.compile(r"^[A-Z0-9]{2}$")


def place_one(value):
    """One place token -> {'id', 'kind'} or {'query': text} (free text)."""
    value = (value or "").strip()
    if not value:
        raise ValueError("empty place")
    if protocol.GEO_ID_RE.match(value):
        return {"id": value, "kind": 4}
    if protocol.AIRPORT_RE.match(value):
        return protocol.place(value)
    if _FREE_TEXT_RE.match(value):
        return {"query": value}
    raise ValueError(f"{value!r} is not an airport code (JFK), a place id (/m/02_286) or a city name")


def place_ref(value, joiner=","):
    """'JFK,EWR' / 'New York' / '/m/02_286' -> list of place specs."""
    value = (value or "").strip()
    parts = [p for p in (x.strip() for x in value.split(joiner)) if p]
    if not parts:
        raise ValueError("pass an airport code (JFK), a place id (/m/02_286) or a city name")
    if len(parts) > MAX_PLACES:
        raise ValueError(f"at most {MAX_PLACES} airports / places")
    out = []
    for p in parts:
        spec = place_one(p)
        if spec not in out:
            out.append(spec)
    return out


def _leg_date(value):
    value = (value or "").strip()
    try:
        parsed = datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        raise ValueError(f"leg date {value!r} must be YYYY-MM-DD")
    if parsed < date.today():
        raise ValueError(f"leg date {value} is in the past")
    return parsed.isoformat()


def legs_ref(value):
    """Multi-city legs -> [{'origin': [places], 'destination': [places], 'date'}]."""
    value = (value or "").strip()
    raw = []
    if value.startswith("["):
        try:
            items = json.loads(value)
        except ValueError:
            raise ValueError("legs JSON is malformed")
        for item in items if isinstance(items, list) else []:
            if not isinstance(item, dict):
                raise ValueError("each leg must be an object with origin, destination and date")
            raw.append((str(item.get("origin") or ""), str(item.get("destination") or ""), str(item.get("date") or "")))
    else:
        for chunk in value.split("|"):
            chunk = chunk.strip()
            if not chunk:
                continue
            fields = [f.strip() for f in chunk.split(",")]
            if len(fields) != 3:
                raise ValueError(f"leg {chunk!r} must be origin,destination,date (e.g. JFK,LHR,2026-11-20)")
            raw.append(tuple(fields))
    if len(raw) < 2:
        raise ValueError("a multi-city trip needs at least 2 legs (e.g. JFK,LHR,2026-11-20|LHR,CDG,2026-11-24)")
    if len(raw) > MAX_LEGS:
        raise ValueError(f"at most {MAX_LEGS} legs")
    legs, previous = [], None
    for origin, destination, day in raw:
        leg = {"origin": place_ref(origin, joiner="+"), "destination": place_ref(destination, joiner="+"),
               "date": _leg_date(day)}
        if previous and leg["date"] < previous:
            raise ValueError("leg dates must be in chronological order")
        previous = leg["date"]
        legs.append(leg)
    return legs


def hours_ref(value):
    """'6-18' -> (6, 18)."""
    m = _HOURS_RE.match((value or "").strip())
    if not m:
        raise ValueError("must be an hour range like 6-18 (0-23)")
    start, end = int(m.group(1)), int(m.group(2))
    if not (0 <= start <= 23 and 0 <= end <= 23) or start > end:
        raise ValueError("hours must be 0-23 with start <= end (e.g. 6-18)")
    return start, end


def airlines_ref(value):
    """'DL,UA' / 'star_alliance' -> ['DL', 'UA'] / ['STAR_ALLIANCE']."""
    out = []
    for item in (value or "").split(","):
        item = item.strip().upper().replace(" ", "_").replace("-", "_")
        if not item:
            continue
        if item == "STAR":
            item = "STAR_ALLIANCE"
        if item not in protocol.ALLIANCES and not _AIRLINE_RE.match(item):
            raise ValueError(f"{item!r} is not a 2-character airline code (DL, BA, 6E) or an alliance "
                             "(oneworld, skyteam, star_alliance)")
        if item not in out:
            out.append(item)
    if not out:
        raise ValueError("pass at least one airline code")
    return out


def airport_codes_ref(value):
    out = []
    for item in (value or "").split(","):
        item = item.strip().upper()
        if not item:
            continue
        if not protocol.AIRPORT_RE.match(item):
            raise ValueError(f"{item!r} is not a 3-letter airport code")
        if item not in out:
            out.append(item)
    if not out:
        raise ValueError("pass at least one airport code")
    return out


def region_ref(value):
    """Named region or 'north,east,south,west' -> [[north, east], [south, west]]."""
    raw = (value or "").strip().lower()
    name = raw.replace(" ", "_").replace("-", "_")
    if name in REGIONS:
        return REGIONS[name]
    parts = [p.strip() for p in raw.split(",") if p.strip()]
    if len(parts) == 4:
        try:
            north, east, south, west = (float(p) for p in parts)
        except ValueError:
            pass
        else:
            if not (-90 <= south < north <= 90 and -180 <= west <= 180 and -180 <= east <= 180):
                raise ValueError("bounds must be north,east,south,west with south < north")
            return [[north, east], [south, west]]
    raise ValueError(f"region must be one of: {', '.join(REGIONS)} — or bounds as north,east,south,west")


def interests_ref(value):
    out = []
    for item in (value or "").split(","):
        item = item.strip().lower()
        if not item:
            continue
        if item not in INTERESTS:
            raise ValueError(f"{item!r} is not one of: {', '.join(INTERESTS)}")
        out.append(INTERESTS[item])
    if not out:
        raise ValueError("pass at least one interest")
    if len(out) > 1:
        raise ValueError("Google Flights Explore takes one interest at a time")
    return out[0]


def slugify(text):
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


_ROUTE_LINK_RE = re.compile(r"/flights-(?:from-(?P<from>[a-z0-9-]+?)(?:-to-(?P<to1>[a-z0-9-]+))?|to-(?P<to2>[a-z0-9-]+))\.html")


def city_slug_ref(value, side="from"):
    """City name / slug / airport code / route-page link -> page slug.
    `side` picks which end of a two-city link to use ("from" or "to")."""
    value = (value or "").strip()
    if not value:
        raise ValueError("pass a city name (New York), a slug (new-york), an airport code or a Google Flights route link")
    if value.startswith(("http://", "https://", "www.", "google.")):
        url = value if value.startswith("http") else "https://" + value
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower()
        if "google." not in host:
            raise ValueError("not a Google Flights link")
        m = _ROUTE_LINK_RE.search(parsed.path)
        if not m:
            raise ValueError("not a Google Flights route link (…/travel/flights/flights-from-<city>.html)")
        slug = (m.group("to1") or m.group("to2")) if side == "to" else m.group("from")
        if not slug:
            raise ValueError(f"that link has no {'destination' if side == 'to' else 'origin'} city")
        return slug
    slug = slugify(value)
    if not slug:
        raise ValueError("city name has no usable characters")
    return slug


def token_ref(kind):
    def resolve(value):
        return protocol.unpack_token(value, kind)
    return resolve
