"""Google Flights: a parked headed Chrome on /travel/flights mints one
BotGuard token per f.req body (google_flights/minter.py). The token is bound
to the body only, not to IP/UA/cookies, so the box mints on its own IP.
The page cannot park elsewhere: the service object lives in the Flights
bundle."""
from browser_worker.types import TokenMint


class GoogleFlights(TokenMint):
    name = "google-flights"
    headless = False
    geoip_timezone = False
    variants = {"": "https://www.google.com/travel/flights?hl=en&gl=us&curr=USD"}
    park = False
    idle_seconds = 300
    max_age = 1800                    # minter recycle in config today
    max_uses = 2000
    ttl_seconds = 20 * 60             # "valid 20+ min" (minter.py:8-10)
    max_browsers = 2                  # the share (= config.INPAGE_BROWSER_SLOTS); GKE offers
                                      # share x BROWSER_BURST_FACTOR sessions "flights#i" as
                                      # candidates (google_flights/minter.py, token_any)

    def warm(self, driver, variant):
        from google_flights.minter import _prepare
        driver.call(_prepare, timeout=120)

    def mint_token(self, driver, payload):
        from google_flights.minter import _MINT_JS
        freq = payload if isinstance(payload, str) else (payload or {}).get("freq")
        if not freq:
            from browser_worker.types import RecipeError
            raise RecipeError("google-flights: input must be the f.req string (or {freq})")
        return driver.call(lambda page: page.evaluate(_MINT_JS, freq, isolated_context=False), timeout=30)


RECIPE = GoogleFlights
