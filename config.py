"""Configuration for the Google Flights Scraper. Everything can be set with an
environment variable; the defaults work out of the box.

    PORT                     port the API listens on (default 8000)
    GOOGLE_FLIGHTS_PROXY     proxy URL for every request, e.g. http://user:pass@host:port
                             (default: none — direct). Google Flights served
                             hundreds of back-to-back searches from one IP in
                             our tests, so you very likely don't need this.
                             Set it only if you start seeing 403 / 429 errors.
    GOOGLE_FLIGHTS_HEADLESS  run the token browser headless (default true).
                             Set to false to watch it, or if Google ever starts
                             rejecting headless tokens.

The scraper fetches every result over plain HTTP. One Chrome window (real
Google Chrome must be installed) stays open on google.com/travel/flights to
sign each request — that is how Google Flights protects its data.

Everything else below is a plain constant with a working default — edit it
here if you need to.
"""
import os

PORT = int(os.environ.get("PORT", "8000"))

# Retry policy for transport errors and blocks (every request).
MAX_RETRIES = 3
RETRY_BACKOFF = 2          # seconds, multiplied by the attempt number

GOOGLE_FLIGHTS_PROXY = os.environ.get("GOOGLE_FLIGHTS_PROXY") or None
GOOGLE_FLIGHTS_PROXY_COUNTRY = None               # kept for the module's docstrings; the proxy is GOOGLE_FLIGHTS_PROXY

# Token browser: headless by default so it runs anywhere; recycled every 30 min.
GOOGLE_FLIGHTS_MINTER_HEADLESS = os.environ.get("GOOGLE_FLIGHTS_HEADLESS", "true").strip().lower() not in ("0", "false", "no")
GOOGLE_FLIGHTS_MINTER_MAX_AGE = 1800
GOOGLE_FLIGHTS_MINTER_WARM = True                 # start the browser with the server, so the first search is fast

GOOGLE_FLIGHTS_DIRECT_COOLDOWN = 600              # seconds a blocked thread stays on the fallback proxy
GOOGLE_FLIGHTS_LINK_WORKERS = 6                   # parallel booking-link resolutions


def google_flights_proxy():
    """Proxy for every HTTP request (None = direct)."""
    return os.environ.get("GOOGLE_FLIGHTS_PROXY") or None


def google_flights_fallback_proxy():
    """Proxy a blocked request retries through (None = retry direct)."""
    return os.environ.get("GOOGLE_FLIGHTS_PROXY") or None


def google_flights_minter_proxy():
    """Proxy for the token browser (None = direct; tokens are not IP-bound)."""
    return os.environ.get("GOOGLE_FLIGHTS_PROXY") or None
