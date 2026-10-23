# ✈️ Google Flights Scraper

Google Flights Scraper is a **free and open-source** scraper that gets you **unlimited** detailed Google Flights data for free.

## ✨ What Can I Get?

- ✈️ **Every flight on any route** — prices, airlines, times, stops, layovers, legroom, Wi-Fi & CO2
- 💰 **Who sells it, for how much** — airlines vs. agencies, fare rules, bag fees & direct booking links
- 📅 **The cheapest days to fly** — 92-day price calendar, date grid & Google's low / typical / high verdict
- 🌍 **Where to go next** — cheapest destinations by region, month or interest, plus route insights in 71 currencies

## 🎥 Example: A Full Google Flights Search

```json
{
  "search": { "trip_type": "one_way", "origin": ["JFK"], "destination": ["LAX"], "departure_date": "2026-11-12", "currency": "USD" },
  "price_insights": {
    "lowest_price": 215,
    "price_level": "typical",
    "typical_price_range": { "low": 85, "high": 225 }
  },
  "best_flights": [
    {
      "price": 249,
      "currency": "USD",
      "airlines": [{ "code": "DL", "name": "Delta" }],
      "departure": { "airport": { "code": "JFK", "name": "John F. Kennedy International Airport", "city": "New York" }, "date": "2026-11-12", "time": "12:00" },
      "arrival": { "airport": { "code": "LAX", "name": "Los Angeles International Airport", "city": "Los Angeles" }, "date": "2026-11-12", "time": "15:08" },
      "duration_minutes": 368,
      "stops": 0,
      "segments": [
        {
          "flight_number": "DL 1788",
          "aircraft": "Airbus A330",
          "seat": { "type": "average_legroom", "legroom": { "value": 31, "unit": "in" } },
          "amenities": { "wifi": "free", "power_outlet": "in_seat", "usb_outlet": "in_seat", "entertainment": ["on_demand_video"] },
          "emissions": { "co2_kg": 261, "contrail_warming_potential": "low" }
        }
      ],
      "emissions": { "co2_kg": 261, "typical_for_route_co2_kg": 347, "difference_percent": -25, "level": "lower" },
      "booking_token": "gf1.eNqFTl1rwjAU_S_3OYUk…"
    }
  ]
}
```

*Trimmed for readability.*

## 🚀 Unlimited Free Google Flights Data — Get It in 60 Seconds

1️⃣ Clone and install (you need [Google Chrome](https://www.google.com/chrome/) installed — the scraper keeps one Chrome window open to sign its requests):
```bash
git clone https://github.com/omkarcloud/google-flights-scraper
cd google-flights-scraper
python -m pip install -r requirements.txt
```

2️⃣ Start the API:
```bash
python run.py
```

3️⃣ Get your first data:
```bash
curl "http://localhost:8000/flights/search-one-way?origin=JFK&destination=LAX&departure_date=2026-11-12"
```

```json
{
  "search": { "trip_type": "one_way", "origin": ["JFK"], "destination": ["LAX"], "departure_date": "2026-11-12", "cabin_class": "economy", "currency": "USD" },
  "price_insights": { "lowest_price": 215, "price_level": "typical", "typical_price": 127, "typical_price_range": { "low": 85, "high": 225 } },
  "best_flights": [
    {
      "price": 249,
      "airlines": [{ "code": "DL", "name": "Delta" }],
      "departure": { "airport": { "code": "JFK", "city": "New York" }, "date": "2026-11-12", "time": "12:00" },
      "arrival": { "airport": { "code": "LAX", "city": "Los Angeles" }, "date": "2026-11-12", "time": "15:08" },
      "duration_minutes": 368,
      "stops": 0,
      "segments": [{ "flight_number": "DL 1788", "aircraft": "Airbus A330", "amenities": { "wifi": "free" } }]
    }
  ],
  "other_flights": [
    { "price": 215, "airlines": [{ "code": "AS", "name": "Alaska" }], "duration_minutes": 621, "stops": 1,
      "layovers": [{ "duration_minutes": 67, "airport": { "code": "SEA", "city": "Seattle" } }] }
  ]
}
```

All 17 endpoints are now live at `http://localhost:8000`.

## 📚 Endpoints

17 endpoints cover everything you need.

| Endpoint | Path | Returns |
|---|---|---|
| One Way Search | `/flights/search-one-way` | Every flight with price, stops, legroom, Wi-Fi and CO2 |
| Airport Search | `/airports/search` | Cities, airports and regions with their nearby airports |
| Round Trip Search | `/flights/search-round-trip` | Outbound flights priced for the whole round trip |
| Returning Flights | `/flights/returning-flights` | Return options for your picked outbound, round-trip totals |
| Booking Options | `/flights/booking-options` | Airlines and agencies selling it, fare rules, bag fees, links |
| Multi-City Search / Next Leg | `/flights/search-multi-city`, `/flights/multi-city/next-leg` | Multi-city trips, one leg at a time, priced end to end |
| Price Insights | `/flights/price-insights` | Google's low / typical / high verdict and price history |
| Price Calendar | `/prices/calendar` | Cheapest fare for every departure date, up to 92 days |
| Date Grid | `/prices/date-grid` | Round-trip fares for every nearby departure × return date |
| Explore Destinations | `/explore/destinations` | Cheapest places to fly by region, month or interest |
| Route Insights | `/routes/city-pair` | Cheapest fares, prices by month, airlines, fastest flight |
| Flights From / To City | `/routes/from-city`, `/routes/to-city` | Airports, nonstop airlines and the cheapest trips |
| Currencies / Languages / Countries | `/meta/currencies`, `/meta/languages`, `/meta/countries` | Every supported currency, language and country |


## 🔍 Exploring Parameters

The same API is published on RapidAPI, and its playground is the easiest place to try parameters and see raw responses. Once a request looks right, run it locally for **unlimited free** data.

1. [Subscribe to the free plan](https://rapidapi.com/OmkarCloud/api/best-google-flights-scraper-free-1000-calls/pricing) — 1,000 calls/month, no credit card.
2. [Try the endpoints in the playground](https://rapidapi.com/OmkarCloud/api/best-google-flights-scraper-free-1000-calls/playground) — every param is pre-filled, so you see real data in one click.
3. Copy the generated code and replace `https://best-google-flights-scraper-free-1000-calls.p.rapidapi.com` with `http://localhost:8000`. It will now run against your local API.

```python
import requests

# generated by the playground, host swapped for the local API
response = requests.get(
    "http://localhost:8000/flights/search-one-way",
    params={"origin": "JFK", "destination": "LAX", "departure_date": "2026-11-12"},
)
print(response.json())
```

## 💬 Have Questions? We Have Answers.

You're a developer — we know how hard completing a project can be. So we offer full support: just message us and we'll reply ✅ with a solution within 1 working day.

[![Message Us on WhatsApp about Google Flights Scraper](https://raw.githubusercontent.com/omkarcloud/assets/master/images/whatsapp-us.png)](https://api.whatsapp.com/send?phone=918178804274&text=I%20need%20help%20using%20the%20Google%20Flights%20Scraper%20API.)

[![Ask Us by Email about Google Flights Scraper](https://raw.githubusercontent.com/omkarcloud/assets/master/images/ask-on-email.png)](mailto:happy.to.help@omkar.cloud?subject=Help%20with%20Google%20Flights%20Scraper%20API&body=I%20need%20help%20using%20the%20Google%20Flights%20Scraper%20API.)

## ⚡ Popular Scrapers by Omkar Cloud

- [**Google Maps Scraper (3,100+ GitHub Stars)**](https://github.com/omkarcloud/google-maps-scraper) — type "dentists in New York", get every business as a ready-to-call lead list: phones, emails, websites & reviews. Up to 100K free leads/month.
- [**G2 Scraper**](https://www.omkar.cloud/tools/g2-scraper) — G2 product details, ratings & AI-found contacts
- [**Website Email Contact Scraper**](https://www.omkar.cloud/tools/website-email-contact-scraper) — emails, phones & socials from any website
- [**AliExpress Scraper**](https://www.omkar.cloud/tools/aliexpress-scraper-api) — live product details, SKU variants, stock & shipping
- [**Booking Scraper**](https://www.omkar.cloud/tools/booking-scraper) — Booking.com hotels: prices, ratings, rooms & amenities
- [**Etsy Scraper**](https://www.omkar.cloud/tools/etsy-scraper) — Etsy products: prices, discounts, shops & variations

## ⭐ Love It? [Star It ⭐!](https://github.com/omkarcloud/google-flights-scraper)

Star the repo ⭐ and become my star hero!

It's just 1 click, but it means the world to me.

[![Star us on GitHub](https://raw.githubusercontent.com/omkarcloud/google-maps-scraper/master/screenshots/star-us.png)](https://github.com/omkarcloud/google-flights-scraper)
