"""The 17 Google Flights endpoints. Every path is served with and without the
`/google-flights` prefix, so code generated against the hosted API (paths
like /flights/search-one-way) runs unchanged against this server.

Params are validated by the same marshmallow schemas the hosted API uses
(google_flights/schemas.py): unknown params and bad values answer 400 with
the reason."""
import json

from bottle import request, response, route

import config
from google_flights import explore, flights, minter, places, prices, route_pages, schemas
from schema_fields import load_query
from scraper_errors import BadRequest, NotFound


def json_response(data, status=200):
    response.status = status
    response.content_type = "application/json"
    return json.dumps(data, ensure_ascii=False)


def query_dict():
    """The query as unicode strings (bottle 0.12's .get() hands back latin-1
    decoded bytes, so a UTF-8 "São Paulo" would arrive as "SÃ£o Paulo")."""
    return {key: request.query.getunicode(key) for key in request.query.keys()}


def call(label, schema, fn):
    """Validate the query, run the scraper, map failures: bad params -> 400,
    missing entity -> 404, transport / blocks -> 500."""
    data, error = load_query(schema, query_dict())
    if error:
        return json_response(error, 400)
    try:
        return json_response(fn(**data))
    except ValueError as e:                # bad params
        return json_response({"error": str(e)}, 400)
    except BadRequest as e:                # Google rejected the request
        return json_response({"error": f"google-flights rejected the request: {e}"}, 400)
    except NotFound as e:
        return json_response({"error": str(e) or "not found"}, 404)
    except Exception as e:                 # retries exhausted / blocked
        return json_response({"error": f"google-flights {label} failed: {e}"}, 500)


ENDPOINTS = [
    ("/flights/search-one-way", schemas.OneWaySchema, flights.search_one_way),
    ("/airports/search", schemas.AirportSearchSchema, places.search),
    ("/flights/search-round-trip", schemas.RoundTripSchema, flights.search_round_trip),
    ("/flights/returning-flights", schemas.ReturningSchema, flights.returning_flights),
    ("/flights/booking-options", schemas.BookingOptionsSchema, flights.booking_options),
    ("/flights/search-multi-city", schemas.MultiCitySchema, flights.search_multi_city),
    ("/flights/multi-city/next-leg", schemas.NextLegSchema, flights.multi_city_next_leg),
    ("/flights/price-insights", schemas.PriceInsightsSchema, flights.price_insights),
    ("/prices/calendar", schemas.CalendarSchema, prices.calendar),
    ("/prices/date-grid", schemas.DateGridSchema, prices.date_grid),
    ("/explore/destinations", schemas.ExploreSchema, explore.destinations),
    ("/routes/city-pair", schemas.CityPairSchema, route_pages.city_pair),
    ("/routes/from-city", schemas.FromCitySchema, route_pages.from_city),
    ("/routes/to-city", schemas.ToCitySchema, route_pages.to_city),
    ("/meta/currencies", schemas.EmptySchema, places.currencies),
    ("/meta/languages", schemas.EmptySchema, places.languages),
    ("/meta/countries", schemas.EmptySchema, places.countries),
]


def mount(path, schema, fn):
    """Serve one endpoint at /path and /google-flights/path."""
    def handler():
        return call(path.strip("/"), schema, fn)
    handler.__name__ = "google_flights_" + path.strip("/").replace("/", "_").replace("-", "_")
    route(path, method="GET")(handler)
    route("/google-flights" + path, method="GET")(handler)


for _path, _schema, _fn in ENDPOINTS:
    mount(_path, _schema, _fn)


@route("/", method="GET")
@route("/health", method="GET")
def health():
    return json_response({"status": "ok", "token_browser": minter.MINTER.status(),
                          "endpoints": [p for p, _, _ in ENDPOINTS]})


if config.GOOGLE_FLIGHTS_MINTER_WARM:
    minter.warm_async()
