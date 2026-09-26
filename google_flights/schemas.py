"""Marshmallow request schemas for every /google-flights/* route.

Generic fields come from the shared schema_fields.py; this module adds the
Google Flights resolvers (refs.py) and the per-route schemas. Every
schema's load() output is the kwargs dict its endpoint function takes.

ONE param per input (tripadvisor QueryOrLinkField convention, never a
sibling url / id pair):
  origin / destination   airport code (JFK) | place id (/m/02_286) | metro
                         code (NYC) | city name (New York) | comma list of those
  legs                   JFK,LHR,2026-11-20|LHR,CDG,2026-11-24 (or a JSON array)
  city                   city name | slug | airport code | route-page link
  returning_token / next_leg_token / booking_token
                         opaque tokens returned by the previous call
"""
from datetime import date

from marshmallow import ValidationError, fields, missing, validate, validates_schema

from google_flights import refs
from schema_fields import (BaseSchema, ChoiceField, CountryCodeField, CurrencyField, DateField, Flag,
                           LanguageCodeField, PositiveInt, QueryField, RefField)

# Choice tables as literals; the publishing tooling reads enums from this file's AST.
CABIN_CLASSES = ["economy", "premium_economy", "business", "first"]
STOPS = ["any", "nonstop", "one_stop_or_fewer", "two_stops_or_fewer"]
SORTS = ["best", "price", "departure_time", "arrival_time", "duration", "emissions"]
TRIP_LENGTHS = ["weekend", "one_week", "two_weeks"]
REGIONS = ["europe", "asia", "southeast_asia", "middle_east", "africa", "north_america", "united_states",
           "central_america", "caribbean", "south_america", "oceania"]
INTERESTS = ["outdoors", "beaches", "museums", "history", "skiing"]
MAX_CALENDAR_DAYS = 92
MAX_PASSENGERS = 9


# ---- id-or-link fields -------------------------------------------------------------------------

class PlaceField(RefField):
    resolver = staticmethod(refs.place_ref)


class LegsField(RefField):
    resolver = staticmethod(refs.legs_ref)


class CityField(RefField):
    resolver = staticmethod(refs.city_slug_ref)


class OriginCityField(RefField):
    resolver = staticmethod(lambda v: refs.city_slug_ref(v, "from"))


class DestinationCityField(RefField):
    resolver = staticmethod(lambda v: refs.city_slug_ref(v, "to"))


class TokenField(RefField):
    def __init__(self, kind, **kwargs):
        super().__init__(resolver=refs.token_ref(kind), **kwargs)


class _OptionalRef(RefField):
    def __init__(self, **kwargs):
        kwargs.setdefault("required", False)
        kwargs.setdefault("load_default", None)
        super().__init__(**kwargs)


class AirlinesField(_OptionalRef):
    resolver = staticmethod(refs.airlines_ref)


class HoursField(_OptionalRef):
    resolver = staticmethod(refs.hours_ref)


class AirportCodesField(_OptionalRef):
    resolver = staticmethod(refs.airport_codes_ref)


class RegionField(_OptionalRef):
    resolver = staticmethod(refs.region_ref)


class InterestField(_OptionalRef):
    resolver = staticmethod(refs.interests_ref)


def _travel_date(required=False):
    """YYYY-MM-DD, today or later (DateField defaults load_default=None, which
    marshmallow forbids on required fields)."""
    if required:
        return DateField(bound="future", required=True, load_default=missing)
    return DateField(bound="future")


def _count(default=0, maximum=8):
    return fields.Integer(load_default=default, strict=False, validate=validate.Range(min=0, max=maximum))


# ---- shared blocks -------------------------------------------------------------------------------

class LocaleSchema(BaseSchema):
    currency = CurrencyField(load_default="USD")
    language = LanguageCodeField(load_default="en-US")
    country = CountryCodeField(load_default="US")


class PassengersSchema(LocaleSchema):
    adults = PositiveInt(load_default=1, max_value=MAX_PASSENGERS)
    children = _count()
    infants_in_seat = _count()
    infants_on_lap = _count()
    cabin_class = ChoiceField(CABIN_CLASSES, load_default="economy")

    @validates_schema
    def _passengers(self, data, **kwargs):
        total = sum(data.get(k) or 0 for k in ("adults", "children", "infants_in_seat", "infants_on_lap"))
        if total > MAX_PASSENGERS:
            raise ValidationError(f"At most {MAX_PASSENGERS} passengers in total.", "adults")
        if (data.get("infants_on_lap") or 0) > (data.get("adults") or 1):
            raise ValidationError("Each infant on lap needs an adult.", "infants_on_lap")


class FiltersSchema(PassengersSchema):
    stops = ChoiceField(STOPS, load_default="any")
    airlines = AirlinesField()
    max_price = PositiveInt()


class FlightFiltersSchema(FiltersSchema):
    departure_hours = HoursField()
    arrival_hours = HoursField()
    max_duration_minutes = PositiveInt(max_value=6000)
    max_layover_minutes = PositiveInt(max_value=2880)
    connecting_airports = AirportCodesField()
    less_emissions = Flag(load_default=False)
    carry_on_bags = _count(maximum=1)
    checked_bags = _count(maximum=2)
    sort_by = ChoiceField(SORTS, load_default="best")


def _check_return(data):
    dep, ret = data.get("departure_date"), data.get("return_date")
    if dep and ret and ret < dep:
        raise ValidationError("Must be on or after departure_date.", "return_date")


# ---- places -----------------------------------------------------------------------------------------

class AirportSearchSchema(BaseSchema):
    query = QueryField(max_length=100)
    language = LanguageCodeField(load_default="en-US")
    country = CountryCodeField(load_default="US")


class EmptySchema(BaseSchema):
    pass


# ---- flights ----------------------------------------------------------------------------------------

class OneWaySchema(FlightFiltersSchema):
    origin = PlaceField()
    destination = PlaceField()
    departure_date = _travel_date(required=True)


class RoundTripSchema(FlightFiltersSchema):
    origin = PlaceField()
    destination = PlaceField()
    departure_date = _travel_date(required=True)
    return_date = _travel_date(required=True)
    return_departure_hours = HoursField()
    return_arrival_hours = HoursField()

    @validates_schema
    def _dates(self, data, **kwargs):
        _check_return(data)


class ReturningSchema(BaseSchema):
    returning_token = TokenField("returning")


class MultiCitySchema(FlightFiltersSchema):
    legs = LegsField()


class NextLegSchema(BaseSchema):
    next_leg_token = TokenField("next_leg")


class BookingOptionsSchema(BaseSchema):
    booking_token = TokenField("booking")
    resolve_links = Flag(load_default=True)


class PriceInsightsSchema(FiltersSchema):
    origin = PlaceField()
    destination = PlaceField()
    departure_date = _travel_date(required=True)
    return_date = _travel_date()

    @validates_schema
    def _dates(self, data, **kwargs):
        _check_return(data)


# ---- prices -----------------------------------------------------------------------------------------

class CalendarSchema(FiltersSchema):
    origin = PlaceField()
    destination = PlaceField()
    start_date = DateField(bound="future", default_days_ahead=1)
    end_date = _travel_date()
    trip_length_days = PositiveInt(max_value=30)

    @validates_schema
    def _span(self, data, **kwargs):
        start, end = data.get("start_date"), data.get("end_date")
        if start and end:
            days = (date.fromisoformat(end) - date.fromisoformat(start)).days
            if days < 0:
                raise ValidationError("Must be on or after start_date.", "end_date")
            if days > MAX_CALENDAR_DAYS - 1:
                raise ValidationError(f"At most {MAX_CALENDAR_DAYS} days per call.", "end_date")


class DateGridSchema(FiltersSchema):
    origin = PlaceField()
    destination = PlaceField()
    departure_date = _travel_date(required=True)
    return_date = _travel_date(required=True)

    @validates_schema
    def _dates(self, data, **kwargs):
        _check_return(data)


# ---- explore ----------------------------------------------------------------------------------------

class ExploreSchema(FiltersSchema):
    origin = PlaceField()
    departure_date = _travel_date()
    return_date = _travel_date()
    month = fields.Integer(load_default=None, strict=False, validate=validate.Range(min=1, max=12))
    trip_length = ChoiceField(TRIP_LENGTHS)
    region = RegionField()
    interest = InterestField()

    @validates_schema
    def _dates(self, data, **kwargs):
        _check_return(data)
        if data.get("return_date") and not data.get("departure_date"):
            raise ValidationError("return_date needs a departure_date.", "return_date")


# ---- route insights --------------------------------------------------------------------------------

class FromCitySchema(LocaleSchema):
    city = CityField()


class ToCitySchema(LocaleSchema):
    city = DestinationCityField()


class CityPairSchema(LocaleSchema):
    origin = OriginCityField()
    destination = DestinationCityField()
