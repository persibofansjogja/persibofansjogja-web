"""Security helpers shared by admin routes, including distributed login throttling.

Production rate limiting uses Upstash Redis so limits are shared across Vercel
serverless instances. Local/test runs use a process-local fallback only.
"""
import hashlib
import hmac
import os
import secrets
import threading
import time

import requests
from flask import request, session

# Fixed-window limits. The account bucket slows targeted password guessing;
# the IP bucket slows attempts spread across many usernames.
LOGIN_WINDOW_SECONDS = 15 * 60
LOGIN_ACCOUNT_LIMIT = 5
LOGIN_IP_LIMIT = 10
_MEMORY_BUCKETS = {}
_MEMORY_LOCK = threading.Lock()

_RATE_LIMIT_LUA = """
local current = redis.call('INCR', KEYS[1])
if current == 1 then redis.call('EXPIRE', KEYS[1], tonumber(ARGV[1])) end
local ttl = redis.call('TTL', KEYS[1])
return {current, ttl}
"""


class RateLimitStoreError(RuntimeError):
    """Raised when the production rate-limit store is unavailable."""


def csrf_token():
    token = session.get("csrf_token")
    if not token:
        token = secrets.token_urlsafe(32)
        session["csrf_token"] = token
    return token


def valid_csrf_token():
    expected = session.get("csrf_token", "")
    supplied = request.form.get("csrf_token", "") or request.headers.get("X-CSRF-Token", "")
    return bool(expected and supplied and hmac.compare_digest(expected, supplied))


def _rate_limit_keys(username):
    # Do not store raw usernames or IP addresses in Redis keys.
    forwarded = request.headers.get("X-Forwarded-For", "")
    ip = (forwarded.split(",", 1)[0].strip() if forwarded else request.remote_addr) or "unknown"
    normalized_user = (username or "").strip().casefold()
    ip_hash = hashlib.sha256(ip.encode("utf-8")).hexdigest()
    user_hash = hashlib.sha256(normalized_user.encode("utf-8")).hexdigest()
    return f"pfj:login:ip:{ip_hash}", f"pfj:login:user:{user_hash}"


def _redis_command(command):
    base_url = os.getenv("UPSTASH_REDIS_REST_URL", "").strip().rstrip("/")
    token = os.getenv("UPSTASH_REDIS_REST_TOKEN", "").strip()
    if not base_url or not token:
        raise RateLimitStoreError("Upstash Redis belum dikonfigurasi.")
    try:
        response = requests.post(
            base_url,
            json=command,
            headers={"Authorization": f"Bearer {token}"},
            timeout=2.5,
        )
        response.raise_for_status()
        payload = response.json()
        if payload.get("error"):
            raise RateLimitStoreError("Upstash Redis mengembalikan error.")
        return payload.get("result")
    except (requests.RequestException, ValueError, AttributeError) as exc:
        raise RateLimitStoreError("Rate-limit store tidak dapat dihubungi.") from exc


def _is_production():
    return os.getenv("VERCEL") == "1" or os.getenv("FLASK_ENV", "").lower() == "production"


def login_rate_limit_status(username):
    """Return (blocked, retry_after_seconds); never fail open in production."""
    keys = _rate_limit_keys(username)
    if os.getenv("UPSTASH_REDIS_REST_URL") and os.getenv("UPSTASH_REDIS_REST_TOKEN"):
        results = [_redis_command(["GET", key]) for key in keys]
        counts = [int(value or 0) for value in results]
        if counts[0] >= LOGIN_IP_LIMIT or counts[1] >= LOGIN_ACCOUNT_LIMIT:
            ttls = [int(_redis_command(["TTL", key]) or 0) for key, count in zip(keys, counts) if count >= (LOGIN_IP_LIMIT if key == keys[0] else LOGIN_ACCOUNT_LIMIT)]
            return True, max(ttls or [1], default=1)
        return False, 0

    if _is_production():
        raise RateLimitStoreError("Upstash Redis wajib dikonfigurasi di production.")

    now = time.time()
    with _MEMORY_LOCK:
        for key, (_, expires) in list(_MEMORY_BUCKETS.items()):
            if expires <= now:
                _MEMORY_BUCKETS.pop(key, None)
        for key, limit in zip(keys, (LOGIN_IP_LIMIT, LOGIN_ACCOUNT_LIMIT)):
            count, expires = _MEMORY_BUCKETS.get(key, (0, now + LOGIN_WINDOW_SECONDS))
            if count >= limit:
                return True, max(1, int(expires - now))
    return False, 0


def record_failed_login(username):
    """Increment both distributed buckets after a failed credential check."""
    keys = _rate_limit_keys(username)
    if os.getenv("UPSTASH_REDIS_REST_URL") and os.getenv("UPSTASH_REDIS_REST_TOKEN"):
        results = [
            _redis_command(["EVAL", _RATE_LIMIT_LUA, "1", key, str(LOGIN_WINDOW_SECONDS)])
            for key in keys
        ]
        return max((int(item[1]) for item in results if item), default=1)

    if _is_production():
        raise RateLimitStoreError("Upstash Redis wajib dikonfigurasi di production.")
    now = time.time()
    with _MEMORY_LOCK:
        ttls = []
        for key in keys:
            count, expires = _MEMORY_BUCKETS.get(key, (0, now + LOGIN_WINDOW_SECONDS))
            _MEMORY_BUCKETS[key] = (count + 1, expires)
            ttls.append(max(1, int(expires - now)))
        return max(ttls, default=1)
