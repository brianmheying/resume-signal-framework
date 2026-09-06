"""
Public-usage gate for RSF.

NOTE (public repo): this is the real module from the production backend,
included as-is because it has no proprietary or sensitive content. The
request-handling server that calls it (server.py) is not included in this
repository -- see docs/architecture.md for how the pieces fit together.

A lightweight per-IP daily allowance that stands between the public internet
and the Anthropic API, plus an owner-issued override code that raises the
limit for people Brian has vouched for. This module holds the pure counting/
validation logic; the production request handler wires it into the HTTP
request lifecycle and decides, at startup, which store backs it (see
set_store()).

Two store implementations:
  - InMemoryUsageStore: process-local only. Resets on every restart, which on
    Render's free tier happens on every idle spin-down (~15 min) — not just
    deploys. Fine for local dev/tests; NOT durable in production.
  - AppsScriptUsageStore: durable. Delegates to the same Google Apps Script
    webhook the usage ledger already posts to (RSF_LEDGER_URL/TOKEN) — no new
    service. Only ever sent a keyed hash of the IP, never the IP itself, and
    fails OPEN (allows the request, uncounted) on any error so a Google-side
    problem degrades to "no rate limiting right now," never to "RSF is down."

The production request handler wires exactly one of these in at startup via
set_store() — never both at once, so there is a single, unambiguous source
of truth for counts.
"""

import hashlib
import hmac
import json
import os
import threading
import time
import urllib.error
import urllib.request

WINDOW_SECONDS = 24 * 60 * 60

DAILY_LIMIT = int(os.environ.get("RSF_DAILY_LIMIT", "15"))
WARN_AT = int(os.environ.get("RSF_WARN_AT", "10"))

_ACCESS_CODES_RAW = os.environ.get("RSF_ACCESS_CODES", "")
ACCESS_CODES = [c.strip() for c in _ACCESS_CODES_RAW.split(",") if c.strip()]

# Used only to keyed-hash IPs before they ever touch a counter/store — never
# stored or logged raw. If unset, falls back to a fixed pepper; set a real
# secret via RSF_IP_HASH_SECRET once a durable (external) store is wired in,
# since only then does the hash need to resist offline guessing.
_IP_HASH_SECRET = (os.environ.get("RSF_IP_HASH_SECRET", "").strip()
                   or "rsf-default-pepper-set-RSF_IP_HASH_SECRET")


def hash_ip(ip):
    """One-way, keyed hash. Raw IPs are never stored, logged, or sent to the
    frontend."""
    return hmac.new(_IP_HASH_SECRET.encode("utf-8"), ip.encode("utf-8"),
                     hashlib.sha256).hexdigest()


def hash_code(code):
    return hashlib.sha256(code.encode("utf-8")).hexdigest()


def _valid_code_hashes():
    # Recomputed each call (cheap; list is tiny) so a changed RSF_ACCESS_CODES
    # env var takes effect on the next process start without any other code
    # path to keep in sync.
    return {hash_code(c) for c in ACCESS_CODES}


def is_valid_override_hash(code_hash):
    """Constant-time membership check against the *currently configured*
    codes. Revoking a code (remove it from RSF_ACCESS_CODES, let Render
    restart) takes effect immediately for every future request — including
    for someone still holding an old cookie from before the revoke."""
    if not code_hash:
        return False
    valid = _valid_code_hashes()
    if not valid:
        return False
    return any(hmac.compare_digest(code_hash, v) for v in valid)


def check_override_code(code):
    """Validate a raw code a user just typed in.

    Returns the code's hash (to store in a cookie) on success, or None on
    failure. Never reveals which part was wrong or how many codes exist —
    callers should return one generic error message either way.
    """
    code = (code or "").strip()
    if not code or not ACCESS_CODES:
        return None
    candidate = hash_code(code)
    return candidate if is_valid_override_hash(candidate) else None


class UsageStore:
    """Pluggable persistence for per-key hit counts within a rolling window."""

    def try_increment(self, key, limit, now=None):
        """Atomically: if the key has fewer than `limit` hits in the window,
        record one more and return (True, new_count); otherwise return
        (False, current_count) WITHOUT recording. Must be atomic per key —
        this is the single checkpoint that decides + consumes, so there's no
        separate check-then-act race between concurrent requests."""
        raise NotImplementedError

    def count(self, key, now=None):
        raise NotImplementedError


class InMemoryUsageStore(UsageStore):
    """Process-local, thread-safe. See the module docstring: not durable
    across a Render free-tier restart. Suitable for local dev and tests."""

    def __init__(self):
        self._lock = threading.Lock()
        self._hits = {}  # key -> [timestamps]

    def _prune_locked(self, key, now):
        cutoff = now - WINDOW_SECONDS
        pruned = [t for t in self._hits.get(key, []) if t > cutoff]
        self._hits[key] = pruned
        return pruned

    def count(self, key, now=None):
        now = time.time() if now is None else now
        with self._lock:
            return len(self._prune_locked(key, now))

    def try_increment(self, key, limit, now=None):
        now = time.time() if now is None else now
        with self._lock:
            hits = self._prune_locked(key, now)
            if len(hits) >= limit:
                return False, len(hits)
            hits.append(now)
            self._hits[key] = hits
            return True, len(hits)


RATE_LIMIT_TIMEOUT = 5  # seconds — this call blocks a real user's request


class AppsScriptUsageStore(UsageStore):
    """Durable store: a blocking POST to the same Apps Script webhook used
    for the usage ledger. Sends only a keyed hash of the visitor's IP plus
    the policy numbers (limit/window) — never a raw IP, never any resume/JD/
    profile content, and the Apps Script side owns no policy of its own.

    Fails OPEN: any network error, timeout, non-2xx, or malformed/unexpected
    response is logged (exception type/message + a short hash prefix for
    correlation — never a raw IP or request content) and treated as
    "allowed, but the true count is unknown" — see try_increment's return
    contract below. This deliberately does NOT fall back to recording the hit
    in a second (in-memory) store: that would create two counters that could
    silently disagree. During an outage, requests are simply allowed and
    uncounted until the backend is reachable again.
    """

    def __init__(self, url, token, timeout=RATE_LIMIT_TIMEOUT):
        self._url = url
        self._token = token
        self._timeout = timeout

    def try_increment(self, key, limit, now=None):
        now = time.time() if now is None else now
        try:
            body = json.dumps({
                "token": self._token,
                "action": "rate_limit_check",
                "hashed_ip": key,
                "limit": limit,
                "window_seconds": WINDOW_SECONDS,
                "now_ms": int(now * 1000),
            }).encode("utf-8")
            req = urllib.request.Request(
                self._url, data=body,
                headers={"Content-Type": "application/json"}, method="POST")
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            if not data.get("ok"):
                raise ValueError(f"backend returned ok=false: {data.get('error')!r}")
            return bool(data["allowed"]), int(data["count"])
        except Exception as e:  # noqa: BLE001 — any failure here must fail open
            print(f"[usage_gate] rate-limit check failed for {key[:12]}...: "
                  f"{type(e).__name__}: {e} — failing OPEN (allowed, uncounted)")
            return True, None  # None = "unknown count" (see try_consume)

    def count(self, key, now=None):
        # Not used by the gate itself (try_consume only ever calls
        # try_increment); provided only to satisfy the UsageStore interface.
        raise NotImplementedError(
            "AppsScriptUsageStore does not support a read-only count() -- "
            "use try_increment()"
        )


_store = InMemoryUsageStore()


def get_store():
    return _store


def set_store(store):
    """Called once at server startup to select the active backend. See the
    module docstring — exactly one store is ever active."""
    global _store
    _store = store


def reset_store_for_tests():
    """Test-only helper: swap in a fresh in-memory store so tests don't leak
    state. Integration tests that need the Apps Script path use set_store()
    directly with a store pointed at a local mock."""
    global _store
    _store = InMemoryUsageStore()
    return _store


def client_ip_from_headers(headers_get, fallback_ip):
    """Best-effort real client IP behind Render (+ Cloudflare — confirmed in
    front of resumesignalframework.com via response headers at audit time).

    `headers_get` is a case-insensitive `.get(name)` lookup (e.g.
    `BaseHTTPRequestHandler.headers.get`), so callers can pass the real
    request headers or a plain dict in tests.

    Preference order:
      1. CF-Connecting-IP — set by Cloudflare at its edge; a client cannot
         forge this because Cloudflare overwrites/strips any client-supplied
         copy before the request reaches the origin.
      2. X-Forwarded-For — first (leftmost) entry. Each hop appends its own
         view of the previous one, so the first entry is the original client
         as seen by the first proxy in the chain.
      3. The raw socket peer address — on Render this is Render's own proxy,
         not the visitor, so this degrades to "treat every visitor as one
         shared bucket" rather than silently mis-attributing usage to the
         wrong person.
    """
    cf_ip = headers_get("CF-Connecting-IP")
    if cf_ip and cf_ip.strip():
        return cf_ip.strip()
    xff = headers_get("X-Forwarded-For")
    if xff:
        first = xff.split(",")[0].strip()
        if first:
            return first
    return fallback_ip


def try_consume(ip, override_code_hash, store=None):
    """The single gate checkpoint. Call exactly once per request, only after
    all local validation has passed and immediately before the request would
    reach Anthropic — never on a request that fails validation first.

    Returns (allowed: bool, info: dict) where info carries 'bypassed',
    'remaining', 'limit', 'warn' (all safe to send to the client — no IPs,
    no codes).
    """
    if override_code_hash and is_valid_override_hash(override_code_hash):
        return True, {"bypassed": True, "remaining": None, "limit": None,
                       "warn": False}
    store = store or _store
    key = hash_ip(ip)
    allowed, count = store.try_increment(key, DAILY_LIMIT)
    if count is None:
        # The durable backend failed and we failed open (see
        # AppsScriptUsageStore). We don't know the real count, so we don't
        # guess at "remaining" or show a warning — just let the request
        # through quietly.
        return True, {"bypassed": False, "remaining": None,
                       "limit": DAILY_LIMIT, "warn": False, "degraded": True}
    remaining = max(0, DAILY_LIMIT - count)
    return allowed, {
        "bypassed": False,
        "remaining": remaining,
        "limit": DAILY_LIMIT,
        # Strictly greater: WARN_AT=10 means the warning begins on the 11th
        # analysis (after 10 are already consumed), matching the spec's
        # "begin warning after they have consumed 10" — not on the 10th.
        "warn": allowed and count > WARN_AT,
    }
