"""Google Flights transport: curl_cffi with chrome TLS, direct egress; an
RPC leaves through the exit of the Chrome that minted its token (rpc()).

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
  GoogleFlightsBadRequest   Google rejected the parameters ([3] INVALID_ARGUMENT)
  GoogleFlightsRefused      Google refused a valid request ([13] with details) — 502
  GoogleFlightsNotFound     unknown place / page
  GoogleFlightsBlocked      403 / 429 / captcha — retried on a residential exit
  GoogleFlightsUpstreamError transport failure / unparseable / empty — retryable
"""
import codecs
import json
import fastjson
import os
import re
import sys
import threading
import time
from urllib.parse import urlencode

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
import egress
from google_flights.shared import MAX_DAYS_AHEAD
from scraper_errors import BadRequest, Blocked, NotFound, UpstreamError
from scraper_utils import sessions

SITE = "https://www.google.com"
RPC_PATH = "/_/FlightsFrontendUi/data/travel.frontend.flights.FlightsFrontendService/"
BATCH_PATH = "/_/FlightsFrontendUi/data/batchexecute"
IMPERSONATE = "chrome"
TIMEOUT = 40
PAGE_TIMEOUT = 45
ATTEMPTS = 3
RPC_ATTEMPTS = 4          # a refused valid request is retried with a fresh token (see rpc())
INVALID_ATTEMPTS = 2      # a [3] is about the input: one more try, then 400 (see rpc())
RETRY_BACKOFF = 0.3       # seconds x attempt between RPC attempts

# [3] INVALID_ARGUMENT carries a reason as the last item of its
# travel.frontend.flights.ErrorResponse (measured 2026-11-28 with good
# tokens, so the input alone decided): 2 = a date in the past, 3 = a date
# past today + MAX_DAYS_AHEAD (also a calendar / grid window ending there),
# 0 = anything else (an unknown airport code such as QQQ, a return before
# the departure).
INVALID_REASONS = {
    2: "a date is in the past",
    3: f"a date is more than {MAX_DAYS_AHEAD} days ahead (Google Flights searches no further)",
}
INVALID_OTHER = "an airport code Google Flights does not know, or a return before the departure"

RPC_HEADERS = {
    "accept": "*/*",
    "accept-language": egress.site_accept_language("google-flights"),
    "content-type": "application/x-www-form-urlencoded;charset=UTF-8",
    "origin": SITE,
    "referer": SITE + "/travel/flights",
    "x-same-domain": "1",
}
PAGE_HEADERS = {
    "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "accept-language": egress.site_accept_language("google-flights"),
    "upgrade-insecure-requests": "1",
}

_AF_RE = re.compile(r"AF_initDataCallback\(\{key: '(ds:\d+)', hash: '[^']*', data:(.*?), sideChannel: \{\}\}\);", re.S)


class GoogleFlightsUpstreamError(UpstreamError):
    """Transport failure, 5xx, empty or unparseable answer — retryable."""


class GoogleFlightsBlocked(GoogleFlightsUpstreamError, Blocked):
    """403 / 429 / captcha — retried on a residential exit."""


class GoogleFlightsBadRequest(BadRequest):
    """Google rejected the request parameters ([3] INVALID_ARGUMENT)."""


class GoogleFlightsRefused(GoogleFlightsUpstreamError):
    """Google answered a VALID request with [13] and an ErrorResponse: it
    refused the request, not its parameters, so this is a 502 (not charged),
    never a 400.

    Measured 2026-11-28 (BFS-MAN round trip 2027-01-07 / 2027-01-10, the
    search Jane's 400s came from): a token minted in a Chrome passes only
    when the request leaves from that Chrome's IP with that Chrome's
    User-Agent. A local Chrome's tokens: 8/8 from its own IP with its UA,
    0/10 with curl_cffi's UA, 1/8 from a residential exit, 0-1/8 from a
    datacenter exit. The production path (token minted on the browser box,
    POST from the GKE pod's IP with curl_cffi's Windows Chrome 152 UA, box
    Chrome is Linux 156): 0 of 60 POSTs passed from a GKE pod (12 runs x 5
    attempts: same token, fresh token, the box's UA), every one answered
    [13, null, [["type.googleapis.com/travel.frontend.flights.ErrorResponse",
    [[null, [[<ts>, ...], ..., [[0]]], 0, "<event id>", "<id>"], 0]]]].
    The same body passed 8/8 from a browser on one residential exit with
    the POST sent through that exit. Invalid input answers [3] instead."""


class GoogleFlightsNotFound(NotFound):
    """No such place / page."""


class TokenRejected(GoogleFlightsUpstreamError):
    """Google answered [13] with no details: the BotGuard token was refused."""


# ---- sessions --------------------------------------------------------------------------------

_local = threading.local()


def _new_session(proxy):
    return sessions.curl_session(IMPERSONATE, proxy)


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


def _new_rpc_session(proxy):
    """A session for the RPC pool (_RpcSessions): ONE curl handle for its
    life, not curl_cffi's default of a handle per calling thread. The pool
    lends a session to one POST at a time, so threads take turns on it, and
    a streamed POST runs on a thread of its own (_post): with a handle per
    thread every POST got a new handle, so a new connection (a new tunnel
    through the exit), and the pooled session reused nothing (2026-11-28,
    a local server: 6 POSTs through one pooled session opened 6
    connections; with one handle, 1). Its close() also only closed the
    closing thread's handle."""
    return egress.new_session(proxy, impersonate=IMPERSONATE, use_thread_local_curl=False)


def _close(session):
    try:
        session.close()
    except Exception:
        pass


class _RpcSessions:
    """curl sessions for the RPCs, per exit (the proxy URL of the Chrome
    that minted the token, minter.token; None = this machine's own IP).
    A session is lent to one POST at a time and comes back when its
    transfer has ended, also when rpc() already returned early (_post), so
    the next POST through that exit reuses its connection. KEEP idle
    sessions per exit, EXITS exits (rotated exits age out, oldest first)."""
    KEEP = 4
    EXITS = 32

    def __init__(self):
        self._idle = {}                 # exit -> [session], most recently used exit last
        self._lock = threading.Lock()

    def take(self, exit):
        with self._lock:
            idle = self._idle.pop(exit, [])
            session = idle.pop() if idle else None
            self._idle[exit] = idle
        return session or _new_rpc_session(exit or config.google_flights_proxy())

    def give(self, exit, session, healthy=True):
        """Back to the pool after its transfer ended (`healthy` False closes it)."""
        stale = []
        with self._lock:
            if healthy and exit in self._idle and len(self._idle[exit]) < self.KEEP:
                self._idle[exit].append(session)
            else:
                stale.append(session)
            while len(self._idle) > self.EXITS:
                stale += self._idle.pop(next(iter(self._idle)))
        for s in stale:
            _close(s)

    def forget(self, exit):
        """Close the idle sessions of a rotated exit."""
        with self._lock:
            stale = self._idle.pop(exit, [])
        for s in stale:
            _close(s)


_RPC_SESSIONS = _RpcSessions()


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

def _error_reason(details):
    """The reason int of an ErrorResponse detail
    [["type.googleapis.com/travel.frontend.flights.ErrorResponse", [[...], reason]]]
    (INVALID_REASONS), or None."""
    try:
        reason = details[0][1][-1]
    except (IndexError, KeyError, TypeError):
        return None
    return reason if isinstance(reason, int) and not isinstance(reason, bool) else None


def decode_chunks(text):
    """rt=c body -> (payloads, errors). payloads: decoded wrb.fr payloads in
    order; errors: [(code, has_details, reason)] from error envelopes
    (reason: _error_reason, or None), plus ('http', status, code) from "er"
    envelopes."""
    payloads, errors = [], []
    for line in (text or "").splitlines():
        line = line.strip()
        if not line.startswith("[["):
            continue
        try:
            envelopes = fastjson.loads(line)
        except ValueError:
            continue
        for env in envelopes:
            if not isinstance(env, list) or not env:
                continue
            if env[0] == "wrb.fr":
                raw = env[2] if len(env) > 2 else None
                if isinstance(raw, str):
                    try:
                        payloads.append(fastjson.loads(raw))
                    except ValueError:
                        pass
                elif len(env) > 5 and isinstance(env[5], list) and env[5]:
                    details = env[5][2] if len(env[5]) > 2 else None
                    errors.append((env[5][0], bool(details), _error_reason(details)))
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
        code, detailed, reason = err
        if code == 13 and not detailed:
            raise TokenRejected(f"{what}: BotGuard token rejected")
        if code == 13:
            raise GoogleFlightsRefused(f"{what}: Google Flights refused the request although its parameters "
                                       "are valid (not charged); retry in a minute")
        if code == 3:
            raise GoogleFlightsBadRequest(f"{what}: Google rejected the parameters: "
                                          f"{INVALID_REASONS.get(reason, INVALID_OTHER)}")
        if code == 5:
            raise GoogleFlightsNotFound(f"{what}: not found")
        raise GoogleFlightsUpstreamError(f"{what}: Google error code {code}")


def _classify_http(resp, what, text=None):
    """`text`: the body when it was already read (a streamed RPC answer)."""
    if resp.status_code in (403, 429) or "/sorry/" in (resp.url or ""):
        raise GoogleFlightsBlocked(f"{what}: HTTP {resp.status_code}")
    if resp.status_code == 400:
        payloads, errors = decode_chunks(resp.text if text is None else text)
        _raise_for_errors(errors, what)
        raise GoogleFlightsBadRequest(f"{what}: Google rejected the parameters (HTTP 400)")
    if resp.status_code >= 500:
        raise GoogleFlightsUpstreamError(f"{what}: HTTP {resp.status_code}")
    if resp.status_code != 200:
        raise GoogleFlightsUpstreamError(f"{what}: HTTP {resp.status_code}")


# ---- RPC ------------------------------------------------------------------------------------

class _Answer:
    """What _classify_http reads off one RPC answer."""

    def __init__(self, status_code, url, text):
        self.status_code, self.url, self.text = status_code, url, text


class _Reader:
    """content_callback of a streamed answer: collects the text and sets
    `ready` at the first payload `done` accepts."""

    def __init__(self, done):
        self.done, self.ready, self.accepted = done, threading.Event(), False
        self._decoder = codecs.getincrementaldecoder("utf-8")("replace")
        self._parts, self._tail, self._lock = [], "", threading.Lock()

    def feed(self, data):
        """curl's write callback: answers the number of bytes it took (all
        of them). Answering nothing counts as 0 bytes taken: curl_cffi 0.16
        warns "Wrote bytes != received bytes" on every write and names it a
        future exception, which would fail every streamed POST (the
        requirement is unpinned)."""
        piece = self._decoder.decode(data)
        with self._lock:
            self._parts.append(piece)
        if self.accepted:
            return len(data)
        self._tail += piece                 # a chunk's line spans many 16 KB pieces
        if "\n" not in piece:
            return len(data)
        lines, self._tail = self._tail.rsplit("\n", 1)     # complete lines, each decoded once
        if any(self.done(p) for p in decode_chunks(lines)[0]):
            self.accepted = True
            self.ready.set()
        return len(data)

    def text(self):
        with self._lock:
            return "".join(self._parts)


# Seconds rpc() waits for a streamed answer's accepted payload or its end
# (curl's own TIMEOUT ends the transfer; this only guards the hand-over).
STREAM_WAIT = TIMEOUT + 5


def _post(session, url, freq, headers, done=None, release=None):
    """One RPC POST -> _Answer. With `done` (a predicate on one decoded
    payload) it returns at the first payload `done` accepts, while the rest
    of the answer is read off in a background thread; `release(healthy)` is
    called when the transfer has ended either way, so the session (and its
    connection) can serve the next POST.

    GetShoppingResults (done=parsers.shopping_complete), measured
    2026-11-28: a search Google has not cached keeps streaming 7 to 14 s
    after its flights arrived (LAX-SEA flights + insights at 1.5 s, end at
    13.2 s), and 2 of 50 such one-ways from this Mac ran past the 40 s
    TIMEOUT. curl_cffi's stream=True closes the request's curl handle when
    the stream is closed, so stopping there would open a new tunnel through
    the exit for every call; the background read keeps the connection."""
    release = release or (lambda healthy: None)
    if done is None:
        try:
            resp = session.post(url, data={"f.req": freq}, headers=headers, timeout=TIMEOUT)
        except Exception:
            release(False)
            raise
        release(True)
        return _Answer(resp.status_code, resp.url, resp.text)
    reader, out = _Reader(done), {}

    def transfer():
        try:
            out["resp"] = session.post(url, data={"f.req": freq}, headers=headers, timeout=TIMEOUT,
                                       content_callback=reader.feed)
        except Exception as e:
            out["error"] = e
        finally:
            release("error" not in out)
            reader.ready.set()

    threading.Thread(target=transfer, name="google-flights-rpc", daemon=True).start()
    reader.ready.wait(STREAM_WAIT)
    if reader.accepted:
        return _Answer(200, url, reader.text())
    if "error" in out:
        raise out["error"]
    resp = out.get("resp")
    if resp is None:
        raise GoogleFlightsUpstreamError(f"no answer in {STREAM_WAIT} s")
    return _Answer(resp.status_code, resp.url, reader.text())


def rpc(method, inner, locale=None, done=None):
    """Call one FlightsFrontendService method -> list of payload chunks
    (most methods answer one chunk; streaming ones answer several; `done`
    answers at the first payload it accepts, see _post).
    The POST leaves through the exit of the Chrome that minted its token,
    with that Chrome's UA (minter.py, 2026-11-28). Retries transport errors
    and blocks (a block on an exit rotates that exit and re-mints; on the
    machine's own IP the retry goes out on a residential exit), re-mints on
    a rejected or refused token, and gives a [3] (invalid input) one more
    try before it is a 400."""
    from google_flights.minter import MINTER, scrub
    freq = json.dumps([None, json.dumps(inner, separators=(",", ":"), ensure_ascii=False)],
                      separators=(",", ":"), ensure_ascii=False)
    query = dict(locale_query(locale), rt="c")
    url = f"{SITE}{RPC_PATH}{method}?{urlencode(query)}"
    headers = dict(RPC_HEADERS, **{"x-goog-ext-259736195-jspb": _ext_header(locale)})
    # the minting Chrome's UA: a token on a request with curl_cffi's own UA
    # is refused (minter.py, 2026-11-28)
    user_agent = MINTER.user_agent()
    if user_agent:
        headers["user-agent"] = user_agent
    last = None
    blocked = False
    invalid = 0
    refused_exits = set()
    # The token binds to `f.req` and stays valid for 20+ minutes
    # (minter.py), so a transport error or a [3] retries with the SAME
    # token and exit; a rejected ([13] bare) or refused ([13] detailed) one
    # is minted again (until 2026-11-09 every attempt minted, up to 4 per call).
    token = exit = None
    for attempt in range(1, RPC_ATTEMPTS + 1):
        if token is None:
            token, exit = MINTER.token(freq)
        signed = dict(headers, **{"x-goog-batchexecute-bgr": token})
        try:
            if blocked and not exit:
                # this machine's own IP was blocked: the thread's residential fallback session
                answer = _post(_session(fresh_exit=True), url, freq, signed)
            else:
                session = _RPC_SESSIONS.take(exit)
                answer = _post(session, url, freq, signed, done,
                               release=lambda healthy, s=session, x=exit: _RPC_SESSIONS.give(x, s, healthy))
        except Exception as e:
            last = GoogleFlightsUpstreamError(scrub(f"{method}: request failed: {type(e).__name__}: {e}"))
            if blocked and not exit:
                _drop_session()
            continue
        try:
            _classify_http(answer, method, answer.text)
            payloads, errors = decode_chunks(answer.text)
            if not payloads:
                _raise_for_errors(errors, method)
                dump_debug(f"{method}-empty.txt", answer.text)
                raise GoogleFlightsUpstreamError(f"{method}: empty answer")
            return payloads
        except GoogleFlightsBlocked as e:
            last = e
            if exit:
                # the exit is burned for Google: its Chrome and this POST
                # move together to a fresh one
                MINTER.rotate(exit)
                _RPC_SESSIONS.forget(exit)
                token = None
            else:
                blocked = True
        except TokenRejected as e:
            last = e
            token = None
            MINTER.recycle()
        except GoogleFlightsRefused as e:
            # A refusal is about where the request comes from, not the body
            # (GoogleFlightsRefused), and waiting does not lift it: the same
            # token re-sent at once, 3 s later or from a new session passed
            # 0 of 24 times (2026-11-28). Until 2026-11-28 it counted as a
            # parameter rejection: a 400 "Google rejected the parameters"
            # after 1.5 + 3 + 4.5 s of back-off, so each one took 9 to 15 s
            # (Jane, user 23702: 15 x /explore/destinations and 6 x
            # /flights/search-round-trip in the 72 h to Nov 28). Now each
            # retry carries a fresh token, without the long back-off, and
            # the last refusal is a 502. An exit refused twice in one call
            # is rotated (its IP may have changed under the sticky port, so
            # the Chrome and the POST no longer leave from one IP).
            last = e
            if exit and exit in refused_exits:
                MINTER.rotate(exit)
                _RPC_SESSIONS.forget(exit)
            refused_exits.add(exit)
            token = None
        except GoogleFlightsUpstreamError as e:
            last = e
        except GoogleFlightsBadRequest as e:
            # [3] is about the input (a past or too-far date, an unknown
            # airport, a return before the departure: measured 2026-11-28,
            # INVALID_REASONS); valid bodies answer [13] instead. One more
            # try covers a passing glitch (2026-09-26: a date grid failed
            # once and passed on the next try), then it is the 400.
            last = e
            invalid += 1
            if invalid >= INVALID_ATTEMPTS:
                raise
        print(f"google-flights: {method} attempt {attempt}/{RPC_ATTEMPTS} failed: {last}")
        if attempt < RPC_ATTEMPTS:
            time.sleep(RETRY_BACKOFF * attempt)
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
            out[key] = fastjson.loads(raw)
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
