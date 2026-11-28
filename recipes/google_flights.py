"""Google Flights: parked headed Chromes on /travel/flights mint one
BotGuard token per f.req body (google_flights/minter.py). The page cannot
park elsewhere: the service object lives in the Flights bundle.

Since 2026-11-28 a token passes only on a request from the minting
Chrome's IP with its User-Agent, and the box's own IP does not pass even
then (minter.py), so GKE sends a sticky residential exit per Chrome (the
token action's candidates) and POSTs each RPC through the exit its token
was minted on. In the kit the recipe runs in-process on the machine's own
IP, with no proxy."""
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
                                      # share x BROWSER_BURST_FACTOR exits as candidates
                                      # (google_flights/minter.py, token_any)

    def chrome_args_for(self, proxy):
        """Through a proxy the Chrome needs HTTP/1.1: over HTTP/2 in the
        CONNECT tunnel it never gets Flights' first byte (2026-11-28, the
        Ubuntu box on a US residential exit: Page.goto timeout 60000 ms
        twice; with --disable-http2 a Chrome on a residential exit loaded
        Flights in 8 s and passed 120/120 searches; google-finance measured
        the same on 2026-10-22). On the machine's own IP Chrome keeps HTTP/2."""
        return tuple(self.chrome_args) + (("--disable-http2",) if proxy else ())

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
