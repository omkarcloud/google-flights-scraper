"""Cache TTL per /google-flights/* endpoint (cache.py, keyed on the
validated params — marshmallow fills the defaults, so an omitted param and
its default share a row).

Fares move within minutes, so live searches cache briefly; route insight
pages are recomputed by Google about daily; places and reference lists
barely move. Follow-up calls keyed on this API's own tokens cache as long
as the search that produced them."""
from datetime import timedelta

AIRPORT_SEARCH_CACHE = timedelta(days=7)
REFERENCE_CACHE = timedelta(days=7)

SEARCH_CACHE = timedelta(minutes=15)
RETURNING_CACHE = timedelta(minutes=15)
MULTI_CITY_CACHE = timedelta(minutes=15)
BOOKING_CACHE = timedelta(minutes=10)
PRICE_INSIGHTS_CACHE = timedelta(hours=1)

CALENDAR_CACHE = timedelta(hours=1)
DATE_GRID_CACHE = timedelta(hours=1)
EXPLORE_CACHE = timedelta(hours=1)
ROUTES_CACHE = timedelta(hours=12)
