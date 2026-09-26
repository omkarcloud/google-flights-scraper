"""Google Flights transport: curl_cffi with chrome TLS, direct egress.

Three surfaces (validated 2026-09-26):

  RPC     POST /_/FlightsFrontendUi/data/travel.frontend.flights.
          FlightsFrontendService/<Method>?hl&gl&curr&rt=c   form f.req=[null,"<json>"]
          + X-Goog-BatchExecute-Bgr (BotGuard token for that exact f.req,
          minted by minter.py). No f.sid / bl / cookies needed.
          Methods: GetShoppingResults, GetCalendarGraph, GetCalendarGrid,
          GetBookingResults, GetExploreDestinations.
  BATCH   POST /_/FlightsFrontendUi/data/batchexecute?rpcids=H028ib  (airport
          autocomplete) — open, no token.
  PAGES   GET /travel/flights/... server-rendered pages whose data sits in
          AF_initDataCallback({key: 'ds:N', data: ...}) blocks — open.

Responses to RPCs are "rt=c" chunked: )]}' then length-prefixed JSON lines,
each a list of envelopes ["wrb.fr", rpcid, "<payload json>", ...] or an
error ["wrb.fr", null, null, null, null, [code, null, [details]]] / ["er",
..., http_status, ..., code].

Error taxonomy (scraper_errors, mapped to HTTP by route_glue):
  GoogleFlightsBadRequest   Google rejected the parameters (INVALID_ARGUMENT)
  GoogleFlightsNotFound     unknown place / page
  GoogleFlightsBlocked      403 / 429 / captcha — retried on a residential exit
  GoogleFlightsUpstreamError transport failure / unparseable / empty — retryable
"""
import json
import os
import re
import sys
import threading
import time
from urllib.parse import urlencode

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
from scraper_errors import BadRequest, Blocked, NotFound, UpstreamError

SITE = "https://www.google.com"
RPC_PATH = "/_/FlightsFrontendUi/data/travel.frontend.flights.FlightsFrontendService/"
BATCH_PATH = "/_/FlightsFrontendUi/data/batchexecute"
IMPERSONATE = "chrome"
TIMEOUT = 40
PAGE_TIMEOUT = 45
ATTEMPTS = 3
RPC_ATTEMPTS = 4          # Google rejects valid bodies in short bursts (see rpc())
REJECT_BACKOFF = 1.5      # seconds x attempt after a parameter rejection

RPC_HEADERS = {
    "accept": "*/*",
    "accept-language": "en-US,en;q=0.9",
    "content-type": "application/x-www-form-urlencoded;charset=UTF-8",
    "origin": SITE,
    "referer": SITE + "/travel/flights",
    "x-same-domain": "1",
}
PAGE_HEADERS = {
    "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "accept-language": "en-US,en;q=0.9",
    "upgrade-insecure-requests": "1",
}

_AF_RE = re.compile(r"AF_initDataCallback\(\{key: '(ds:\d+)', hash: '[^']*', data:(.*?), sideChannel: \{\}\}\);", re.S)


class GoogleFlightsUpstreamError(UpstreamError):
    """Transport failure, 5xx, empty or unparseable answer — retryable."""


class GoogleFlightsBlocked(GoogleFlightsUpstreamError, Blocked):
    """403 / 429 / captcha — retried on a residential exit."""


class GoogleFlightsBadRequest(BadRequest):
    """Google rejected the request parameters on every attempt."""


class GoogleFlightsNotFound(NotFound):
    """No such place / page."""


class TokenRejected(GoogleFlightsUpstreamError):
    """Google answered [13] with no details: the BotGuard token was refused."""


# ---- sessions --------------------------------------------------------------------------------

_local = threading.local()


def _new_session(proxy):
    from curl_cffi import requests as curl_requests
    sess = curl_requests.Session(impersonate=IMPERSONATE)
    if proxy:
        sess.proxies = {"http": proxy, "https": proxy}
    return sess


def _session(fresh_exit=False):
    """The thread's curl session: direct (or config.GOOGLE_FLIGHTS_PROXY_COUNTRY),
    or a residential exit for GOOGLE_FLIGHTS_DIRECT_COOLDOWN after a block."""
    state = getattr(_local, "state", None)
    now = time.monotonic()
    if state is not None and state["fallback_until"] and now > state["fallback_until"]:
        _drop_session()
        state = None
    if fresh_exit:
        proxy = config.google_flights_fallback_proxy()
        if proxy is None:
            return state["session"] if state else _session()
        _drop_session()
        state = _local.state = {"session": _new_session(proxy), "proxy": proxy,
                                "fallback_until": now + config.GOOGLE_FLIGHTS_DIRECT_COOLDOWN}
        return state["session"]
    if state is None:
        proxy = config.google_flights_proxy()
        state = _local.state = {"session": _new_session(proxy), "proxy": proxy, "fallback_until": None}
    return state["session"]


def _drop_session():
    state = getattr(_local, "state", None)
    _local.state = None
    if state is not None:
        try:
            state["session"].close()
        except Exception:
            pass


def locale_query(locale):
    """{'language': 'en-US', 'country': 'US', 'currency': 'USD'} -> hl/gl/curr."""
    locale = locale or {}
    return {"hl": locale.get("language") or "en-US", "gl": locale.get("country") or "US",
            "curr": locale.get("currency") or "USD"}


def _ext_header(locale):
    q = locale_query(locale)
    return json.dumps([q["hl"], q["gl"], q["curr"], 2, None, [0], None, None, 7, []], separators=(",", ":"))


def dump_debug(name, text):
    dbg = os.environ.get("GOOGLE_FLIGHTS_DEBUG_DIR", "")
    if dbg and text:
        try:
            os.makedirs(dbg, exist_ok=True)
            with open(os.path.join(dbg, name), "w") as f:
                f.write(text)
        except OSError:
            pass


# ---- chunked response decoding ---------------------------------------------------------------

def decode_chunks(text):
    """rt=c body -> (payloads, errors). payloads: decoded wrb.fr payloads in
    order; errors: [(code, has_details)] from error envelopes, plus
    ('http', status, code) from "er" envelopes."""
    payloads, errors = [], []
    for line in (text or "").splitlines():
        line = line.strip()
        if not line.startswith("[["):
            continue
        try:
            envelopes = json.loads(line)
        except ValueError:
            continue
        for env in envelopes:
            if not isinstance(env, list) or not env:
                continue
            if env[0] == "wrb.fr":
                raw = env[2] if len(env) > 2 else None
                if isinstance(raw, str):
                    try:
                        payloads.append(json.loads(raw))
                    except ValueError:
                        pass
                elif len(env) > 5 and isinstance(env[5], list) and env[5]:
                    errors.append((env[5][0], len(env[5]) > 2 and bool(env[5][2])))
            elif env[0] == "er":
                errors.append(("http", env[5] if len(env) > 5 else None, env[9] if len(env) > 9 else None))
    return payloads, errors


def _raise_for_errors(errors, what):
    for err in errors:
        if err[0] == "http":
            status, code = err[1], err[2]
            if status == 400 or code == 3:
                raise GoogleFlightsBadRequest(f"{what}: Google rejected the parameters (HTTP {status})")
            raise GoogleFlightsUpstreamError(f"{what}: Google error HTTP {status} code {code}")
        code, detailed = err
        if code == 13 and not detailed:
            raise TokenRejected(f"{what}: BotGuard token rejected")
        if code in (3, 13):
            raise GoogleFlightsBadRequest(f"{what}: Google rejected the parameters "
                                          "(dates too far apart or in the past, unknown place, or an unsupported combination)")
        if code == 5:
            raise GoogleFlightsNotFound(f"{what}: not found")
        raise GoogleFlightsUpstreamError(f"{what}: Google error code {code}")


def _classify_http(resp, what):
    if resp.status_code in (403, 429) or "/sorry/" in (resp.url or ""):
        raise GoogleFlightsBlocked(f"{what}: HTTP {resp.status_code}")
    if resp.status_code == 400:
        payloads, errors = decode_chunks(resp.text)
        _raise_for_errors(errors, what)
        raise GoogleFlightsBadRequest(f"{what}: Google rejected the parameters (HTTP 400)")
    if resp.status_code >= 500:
        raise GoogleFlightsUpstreamError(f"{what}: HTTP {resp.status_code}")
    if resp.status_code != 200:
        raise GoogleFlightsUpstreamError(f"{what}: HTTP {resp.status_code}")


# ---- RPC ------------------------------------------------------------------------------------

def rpc(method, inner, locale=None):
    """Call one FlightsFrontendService method -> list of payload chunks
    (most methods answer one chunk; streaming ones answer several).
    Retries transport errors / blocks (the retry after a block goes out on a
    residential exit) and re-mints on a rejected token."""
    from google_flights.minter import MINTER
    freq = json.dumps([None, json.dumps(inner, separators=(",", ":"), ensure_ascii=False)],
                      separators=(",", ":"), ensure_ascii=False)
    query = dict(locale_query(locale), rt="c")
    url = f"{SITE}{RPC_PATH}{method}?{urlencode(query)}"
    headers = dict(RPC_HEADERS, **{"x-goog-ext-259736195-jspb": _ext_header(locale)})
    last = None
    blocked = False
    for attempt in range(1, RPC_ATTEMPTS + 1):
        token = MINTER.token(freq)
        try:
            resp = _session(fresh_exit=blocked).post(url, data={"f.req": freq},
                                                     headers=dict(headers, **{"x-goog-batchexecute-bgr": token}),
                                                     timeout=TIMEOUT)
        except Exception as e:
            last = GoogleFlightsUpstreamError(f"{method}: request failed: {type(e).__name__}: {e}")
            _drop_session()
            continue
        try:
            _classify_http(resp, method)
            payloads, errors = decode_chunks(resp.text)
            if not payloads:
                _raise_for_errors(errors, method)
                dump_debug(f"{method}-empty.txt", resp.text)
                raise GoogleFlightsUpstreamError(f"{method}: empty answer")
            return payloads
        except GoogleFlightsBlocked as e:
            last, blocked = e, True
        except TokenRejected as e:
            last = e
            MINTER.recycle()
        except GoogleFlightsUpstreamError as e:
            last = e
        except GoogleFlightsBadRequest as e:
            # Google answers a valid body with INVALID_ARGUMENT / a detailed
            # INTERNAL error now and then (2026-09-26: the same date grid or
            # Explore call failed once and passed on the next try), so a
            # rejection is only final when every attempt gets it. A second
            # rejection in a row also recycles the token browser: a stale
            # BotGuard page produced rejections a fresh one did not.
            # Rejections come in bursts of a second or more (2026-09-26: 3
            # quick tries all refused, the same body passed 10 s later), so
            # they back off longer than transport errors.
            if isinstance(last, GoogleFlightsBadRequest) and attempt < RPC_ATTEMPTS:
                MINTER.recycle()
            last = e
        print(f"google-flights: {method} attempt {attempt}/{RPC_ATTEMPTS} failed: {last}")
        if attempt < RPC_ATTEMPTS:
            time.sleep((REJECT_BACKOFF if isinstance(last, GoogleFlightsBadRequest) else 0.3) * attempt)
    raise last


def batchexecute(rpcid, inner, locale=None, source_path="/travel/flights"):
    """One open batchexecute RPC (no token) -> its decoded payload."""
    freq = json.dumps([[[rpcid, json.dumps(inner, separators=(",", ":"), ensure_ascii=False), None, "generic"]]],
                      separators=(",", ":"), ensure_ascii=False)
    q = locale_query(locale)
    query = {"rpcids": rpcid, "source-path": source_path, "hl": q["hl"], "gl": q["gl"], "curr": q["curr"], "rt": "c"}
    url = f"{SITE}{BATCH_PATH}?{urlencode(query)}"
    last = None
    blocked = False
    for attempt in range(1, ATTEMPTS + 1):
        try:
            resp = _session(fresh_exit=blocked).post(url, data={"f.req": freq}, headers=RPC_HEADERS, timeout=TIMEOUT)
            _classify_http(resp, rpcid)
            payloads, errors = decode_chunks(resp.text)
            if payloads:
                return payloads[0]
            _raise_for_errors(errors, rpcid)
            return None
        except GoogleFlightsBlocked as e:
            last, blocked = e, True
        except GoogleFlightsUpstreamError as e:
            last = e
        except (GoogleFlightsBadRequest, GoogleFlightsNotFound):
            raise
        except Exception as e:
            last = GoogleFlightsUpstreamError(f"{rpcid}: request failed: {type(e).__name__}: {e}")
            _drop_session()
        print(f"google-flights: {rpcid} attempt {attempt}/{ATTEMPTS} failed: {last}")
        time.sleep(0.3 * attempt)
    raise last


# ---- server-rendered pages --------------------------------------------------------------------

def af_blobs(html):
    """{'ds:1': data, ...} from a page's AF_initDataCallback blocks."""
    out = {}
    for key, raw in _AF_RE.findall(html or ""):
        try:
            out[key] = json.loads(raw)
        except ValueError:
            continue
    return out


def page(path, locale=None, params=None):
    """GET a /travel/flights page -> (final_url, html). 404 -> NotFound."""
    query = dict(locale_query(locale))
    query.update(params or {})
    url = f"{SITE}{path}?{urlencode(query)}"
    last = None
    blocked = False
    for attempt in range(1, ATTEMPTS + 1):
        try:
            resp = _session(fresh_exit=blocked).get(url, headers=PAGE_HEADERS, timeout=PAGE_TIMEOUT,
                                                    allow_redirects=True)
        except Exception as e:
            last = GoogleFlightsUpstreamError(f"{path}: request failed: {type(e).__name__}: {e}")
            _drop_session()
            time.sleep(0.3 * attempt)
            continue
        if resp.status_code == 404:
            raise GoogleFlightsNotFound(f"Google Flights has no page at {path}")
        try:
            _classify_http(resp, path)
            if "AF_initDataCallback" not in resp.text:
                dump_debug("page-unexpected.html", resp.text)
                raise GoogleFlightsUpstreamError(f"{path}: unexpected page (no data blocks)")
            return resp.url, resp.text
        except GoogleFlightsBlocked as e:
            last, blocked = e, True
        except GoogleFlightsUpstreamError as e:
            last = e
        print(f"google-flights: {path} attempt {attempt}/{ATTEMPTS} failed: {last}")
        time.sleep(0.3 * attempt)
    raise last


def resolve_redirect(link):
    """Follow Google's /travel/clk/f partner link (a meta-refresh page) to
    the partner's own booking URL. None when it cannot be resolved."""
    try:
        resp = _session().get(link, headers=PAGE_HEADERS, timeout=20, allow_redirects=False)
    except Exception:
        return None
    location = resp.headers.get("location")
    if location:
        return location
    m = re.search(r"""<meta[^>]+content=["']0;\s*url=['"]?([^'">]+)""", resp.text or "", re.I)
    if m:
        import html as htmllib
        return htmllib.unescape(m.group(1)).strip("'\" ")
    return None
