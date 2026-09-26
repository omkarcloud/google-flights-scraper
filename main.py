"""Use the scraper straight from Python — no server needed.

    python main.py

Every function returns the same JSON the API does; results are written to
output/*.json. The first call starts one Chrome window that signs Google
Flights requests (a few seconds), the rest are fast.
"""
import json
import os

from google_flights import explore, flights, refs, route_pages

os.makedirs("output", exist_ok=True)

SEARCH = dict(adults=1, children=0, infants_in_seat=0, infants_on_lap=0, cabin_class="economy", stops="any",
              airlines=None, max_price=None, departure_hours=None, arrival_hours=None, max_duration_minutes=None,
              max_layover_minutes=None, connecting_airports=None, less_emissions=False, carry_on_bags=0,
              checked_bags=0, sort_by="best")


def save(name, data):
    path = os.path.join("output", name)
    with open(path, "w") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print(f"saved {path}")


if __name__ == "__main__":
    # every flight New York -> Los Angeles on one day: prices, legroom, Wi-Fi, CO2, layovers
    save("flights_jfk_lax.json", flights.search_one_way(refs.place_ref("JFK"), refs.place_ref("LAX"), "2026-11-12", **SEARCH))

    # where can you fly from New York for a week in December?
    save("explore_new_york.json", explore.destinations(refs.place_ref("JFK"), month=12, trip_length="one_week"))

    # route intel: cheapest fares, prices by month, airlines
    save("route_new_york_london.json", route_pages.city_pair("new-york", "london"))
