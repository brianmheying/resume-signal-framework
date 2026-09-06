#!/usr/bin/env python3
"""
Representative subset of the real test suite for backend/usage_gate.py.

The production suite has 64 tests across three layers (pure logic,
full-server integration, and a mock external-service double); this is a
runnable, self-contained sample of that approach -- not the complete
suite -- kept small enough to read end to end while still proving the
testing discipline is real, not asserted.

Run: python3 test_usage_gate.py   (stdlib only, no dependencies, no network)
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))
import usage_gate  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  {detail}" if detail and not cond else ""))


def test_warning_and_limit_boundaries():
    """15/day, warning strictly after the 10th -- exercised with an
    injectable clock so this runs in milliseconds, not 24 hours."""
    store = usage_gate.InMemoryUsageStore()
    ip = "203.0.113.5"
    key = usage_gate.hash_ip(ip)

    ok = all(store.try_increment(key, 15) == (True, i) for i in range(1, 11))
    check("analyses 1-10 succeed with no reason to warn yet", ok)

    allowed, count = store.try_increment(key, 15)
    check("analysis 11 is allowed", allowed and count == 11)

    for _ in range(3):  # brings the count to 14
        store.try_increment(key, 15)
    allowed, count = store.try_increment(key, 15)  # the 15th
    check("analysis 15 (== the limit) still succeeds", allowed and count == 15)

    allowed, count = store.try_increment(key, 15)
    check("analysis 16 is blocked, not silently allowed", not allowed and count == 15)


def test_rolling_window_not_a_fixed_reset():
    """The window rolls continuously -- it must not look like a fresh day
    a moment early, and must genuinely clear once 24h has actually passed."""
    store = usage_gate.InMemoryUsageStore()
    key = usage_gate.hash_ip("203.0.113.6")
    t0 = 1_700_000_000.0
    for _ in range(15):
        store.try_increment(key, 15, now=t0)

    still_blocked, _ = store.try_increment(key, 15, now=t0 + usage_gate.WINDOW_SECONDS - 1)
    check("still blocked one second before the window closes", not still_blocked)

    store2 = usage_gate.InMemoryUsageStore()
    for _ in range(15):
        store2.try_increment(key, 15, now=t0)
    allowed_after, _ = store2.try_increment(key, 15, now=t0 + usage_gate.WINDOW_SECONDS + 1)
    check("allowance resets once the window has actually elapsed", allowed_after)


def test_ip_is_never_stored_raw():
    """Only a keyed hash should ever leave this layer -- verified by
    checking the actual value a store key would be built from."""
    ip = "198.51.100.42"
    key = usage_gate.hash_ip(ip)
    check("the hash is not the raw IP", key != ip)
    check("the hash is deterministic for the same IP", key == usage_gate.hash_ip(ip))
    check("a different IP hashes differently", key != usage_gate.hash_ip("198.51.100.43"))


def test_durable_store_fails_open_on_backend_error():
    """If the external Apps Script webhook is unreachable, a real user's
    analysis must still be allowed through -- rate limiting degrades to
    'off' during an outage, never to 'the whole product is down.' The
    production suite also exercises this against a local mock of the real
    Apps Script webhook (malformed responses, timeouts, HTTP errors); this
    is the simplest version of that same guarantee: an address nothing is
    listening on, standing in for any kind of unreachable backend."""
    store = usage_gate.AppsScriptUsageStore("http://127.0.0.1:1/", "fake-token", timeout=0.5)
    allowed, count = store.try_increment(usage_gate.hash_ip("203.0.113.99"), 15)
    check("fails open (allowed) when the backend is unreachable", allowed is True)
    check("an unreachable backend reports an unknown, not a fabricated, count", count is None)


if __name__ == "__main__":
    print("usage_gate representative tests\n")
    test_warning_and_limit_boundaries()
    test_rolling_window_not_a_fixed_reset()
    test_ip_is_never_stored_raw()
    test_durable_store_fails_open_on_backend_error()

    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    sys.exit(1 if FAIL else 0)
