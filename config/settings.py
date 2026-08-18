"""
Centralized settings and multi-key/provider rotation utility.

Extends the existing project pattern (os.getenv + python-dotenv) rather than
introducing a separate config system. All new provider keys (Firecrawl,
Bright Data) and any future multi-key providers should read through here.

Key rotation pattern per provider:
  PROVIDER_API_KEY          (single-key shorthand, used if no numbered keys exist)
  PROVIDER_API_KEY_1
  PROVIDER_API_KEY_2
  ... etc

If only PROVIDER_API_KEY is set, it is used alone (no rotation required).
If numbered keys exist, they are tried in order 1, 2, 3... on transient
provider failures (auth error, rate limit, HTTP 429, timeout).
"""

import os
import re
import time
import logging
from typing import Callable, List, Optional, Any, TypeVar
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

T = TypeVar("T")


# ---------------------------------------------------------------------------
# Exceptions used to signal WHY a call failed — this distinction is what lets
# the rotation/fallback logic tell "provider failed" apart from "provider
# succeeded but found zero results" (a legitimate result, not a failure).
# ---------------------------------------------------------------------------
class ProviderAuthError(Exception):
    """Raised when a specific API key is invalid/unauthorized. Triggers key rotation."""
    pass


class ProviderRateLimitError(Exception):
    """Raised on HTTP 429 / explicit rate-limit response. Triggers key rotation."""
    pass


class ProviderUnavailableError(Exception):
    """Raised on timeout/connection failure/5xx. Triggers key rotation, then layer fallback."""
    pass


class ProviderUnsupportedSiteError(Exception):
    """
    Raised when a provider explicitly refuses to handle a target site as a
    matter of policy (e.g. Firecrawl returning "we do not support this site"
    for LinkedIn). This is NOT a transient failure -- retrying with a
    different key or waiting out a cooldown will never help. Callers should
    skip straight to the next fallback method without burning a rotation
    attempt or cooldown window on a request that was always going to fail.
    """
    pass


class ProviderCooldownError(ProviderUnavailableError):
    """
    Raised when EVERY configured key for this provider is currently in
    cooldown from a recent real failure — meaning NO actual request was
    attempted this call. Kept as a subclass of ProviderUnavailableError so
    existing `except ProviderUnavailableError` call sites keep working
    unchanged; callers that need the distinction (e.g. live platform status
    reporting) can check `isinstance(e, ProviderCooldownError)` to tell
    "temporarily cooling down after a failure" apart from "just failed now"
    or "not configured at all".
    """
    pass


# Exceptions that should trigger trying the NEXT KEY for the same provider
ROTATION_TRIGGERING_EXCEPTIONS = (ProviderAuthError, ProviderRateLimitError, ProviderUnavailableError)


class KeyRotator:
    """
    Loads and rotates through numbered API keys for a given provider prefix.

    Example:
        rotator = KeyRotator("FIRECRAWL_API_KEY")
        result = rotator.call_with_rotation(lambda key: my_api_call(key))

    Behavior:
      - If PROVIDER_API_KEY_1, _2, etc. exist, tries them in order.
      - If only PROVIDER_API_KEY (unsuffixed) exists, uses it alone.
      - A key is temporarily skipped (cooldown) after a rotation-triggering
        failure, so a single bad key doesn't get retried every call.
      - Raises ProviderUnavailableError if ALL configured keys fail.
      - Raises ValueError if NO keys are configured at all (caller should
        treat this as "provider not configured", not "provider failed").
    """

    # How long (seconds) a key stays in cooldown after a rotation-triggering failure
    DEFAULT_COOLDOWN_SECONDS = 300

    def __init__(self, provider_prefix: str, cooldown_seconds: Optional[int] = None,
                 single_key_only: bool = False):
        self.provider_prefix = provider_prefix
        self.cooldown_seconds = cooldown_seconds or self.DEFAULT_COOLDOWN_SECONDS
        self._cooldowns: dict = {}  # key -> unix timestamp when cooldown expires
        # SINGLE-KEY MODE: load exactly one key and ignore any numbered
        # siblings. Used for Firecrawl -- see the note on self.firecrawl below.
        self.single_key_only = single_key_only
        self._keys = self._load_keys()
        if self.single_key_only and len(self._keys) > 1:
            logger.info(
                f"[KeyRotator:{provider_prefix}] Single-key mode: using 1 key, "
                f"ignoring {len(self._keys) - 1} additional configured key(s)."
            )
            self._keys = self._keys[:1]

    def _load_keys(self) -> List[str]:
        """
        Loads all configured keys for this provider, in order:
        numbered keys (_1, _2, ...) first if present, else the bare key.

        In single_key_only mode the BARE key wins if present, so
        FIRECRAWL_API_KEY is authoritative and any leftover
        FIRECRAWL_API_KEY_1..N in an old .env are ignored rather than
        silently taking precedence.
        """
        if self.single_key_only:
            bare = os.getenv(self.provider_prefix)
            if bare and bare.strip():
                return [bare.strip()]
            first_numbered = os.getenv(f"{self.provider_prefix}_1")
            return [first_numbered.strip()] if first_numbered and first_numbered.strip() else []

        numbered_keys = []
        i = 1
        while True:
            val = os.getenv(f"{self.provider_prefix}_{i}")
            if not val:
                break
            numbered_keys.append(val.strip())
            i += 1

        if numbered_keys:
            return numbered_keys

        bare_key = os.getenv(self.provider_prefix)
        return [bare_key.strip()] if bare_key and bare_key.strip() else []

    def is_configured(self) -> bool:
        return len(self._keys) > 0

    def available_key_count(self) -> int:
        return len(self._keys)

    def _mark_cooldown(self, key: str):
        self._cooldowns[key] = time.time() + self.cooldown_seconds

    def _is_in_cooldown(self, key: str) -> bool:
        expiry = self._cooldowns.get(key)
        return bool(expiry and time.time() < expiry)

    def call_with_rotation(self, call_fn: Callable[[str], T], max_key_attempts: Optional[int] = None) -> T:
        """
        Calls call_fn(api_key) for each configured key in order until one
        succeeds. call_fn should raise ProviderAuthError / ProviderRateLimitError /
        ProviderUnavailableError on failures that warrant trying the next key.
        Any other exception propagates immediately (it's not a key-rotation
        situation — e.g. a programming error).

        Raises:
            ValueError — no keys configured at all for this provider.
            ProviderUnavailableError — all configured keys failed.
        """
        if not self._keys:
            raise ValueError(f"No API keys configured for provider prefix '{self.provider_prefix}'.")

        attempts = 0
        last_error: Optional[Exception] = None
        limit = max_key_attempts or len(self._keys)

        keys_in_cooldown = 0

        for idx, key in enumerate(self._keys, start=1):
            if attempts >= limit:
                break
            if self._is_in_cooldown(key):
                logger.info(f"[KeyRotator:{self.provider_prefix}] Key #{idx} is in cooldown, skipping.")
                keys_in_cooldown += 1
                continue

            attempts += 1
            try:
                logger.info(f"[KeyRotator:{self.provider_prefix}] Attempting with key #{idx}/{len(self._keys)}...")
                result = call_fn(key)
                return result
            except ROTATION_TRIGGERING_EXCEPTIONS as e:
                logger.warning(f"[KeyRotator:{self.provider_prefix}] Key #{idx} failed ({type(e).__name__}): {e}. Rotating to next key.")
                self._mark_cooldown(key)
                last_error = e
                continue
            # Any other exception type is NOT a key issue — propagate immediately
            # (do not silently swallow real bugs as if they were rotation failures)

        if attempts == 0 and keys_in_cooldown > 0:
            # Every configured key was skipped due to cooldown — nothing was
            # actually attempted. This is a distinct state from "tried and
            # failed": it means the provider is likely fine, we're just
            # deliberately backing off after a recent real failure.
            raise ProviderCooldownError(
                f"[KeyRotator:{self.provider_prefix}] All {keys_in_cooldown} configured key(s) "
                f"are in cooldown from a recent failure; none attempted this call."
            )

        raise ProviderUnavailableError(
            f"[KeyRotator:{self.provider_prefix}] All {attempts} attempted key(s) failed. "
            f"Last error: {last_error}"
        )


def redact_key(key: Optional[str]) -> str:
    """Never log/expose a real key — use this in any log line that might include one."""
    if not key:
        return "<none>"
    if len(key) <= 8:
        return "***"
    return f"{key[:4]}...{key[-4:]}"


# ---------------------------------------------------------------------------
# Provider settings — centralizes what already existed scattered as
# os.getenv() calls across connector files, PLUS the new providers.
# Existing connectors are not required to switch to this immediately;
# new Firecrawl/Bright Data code uses it from the start.
# ---------------------------------------------------------------------------
class Settings:
    def __init__(self):
        # Existing providers (unchanged behavior — single key each, as before)
        self.ADZUNA_APP_ID = os.getenv("ADZUNA_APP_ID", "")
        self.ADZUNA_APP_KEY = os.getenv("ADZUNA_APP_KEY", "")
        self.RAPIDAPI_KEY = os.getenv("RAPIDAPI_KEY", "")
        self.SERPAPI_API_KEY = os.getenv("SERPAPI_API_KEY", "")
        self.APIFY_API_TOKEN = os.getenv("APIFY_API_TOKEN", "")
        self.ALERT_WEBHOOK_URL = os.getenv("ALERT_WEBHOOK_URL", "")
        self.USAJOBS_API_KEY = os.getenv("USAJOBS_API_KEY", "")
        self.USAJOBS_EMAIL = os.getenv("USAJOBS_EMAIL", "")

        # New providers — support key rotation via KeyRotator
        # FIRECRAWL: SINGLE KEY BY DESIGN.
        #
        # Multi-key rotation was actively harmful here. Every portal fetch
        # walked all 5 keys; when the account ran out of credit all 5 failed in
        # sequence within one request and landed in cooldown together, which
        # then logged five "key #N in cooldown, skipping" lines per portal per
        # request -- pure latency and noise with no chance of success.
        #
        # Firecrawl bills per credit on ONE account, so five keys never bought
        # five times the quota; they bought five times the failure logging.
        # One key, one clear signal, plus the hourly/daily budget in
        # connectors/firecrawl_client.py, is the correct shape.
        self.firecrawl = KeyRotator("FIRECRAWL_API_KEY", single_key_only=True)
        self.brightdata = KeyRotator("BRIGHTDATA_API_KEY")
        self.BRIGHTDATA_ZONE = os.getenv("BRIGHTDATA_ZONE", "")

    def provider_status_summary(self) -> dict:
        """Non-sensitive summary of which providers are configured — safe to expose via API."""
        return {
            "adzuna": bool(self.ADZUNA_APP_ID and self.ADZUNA_APP_KEY),
            "serpapi": bool(self.SERPAPI_API_KEY),
            "apify": bool(self.APIFY_API_TOKEN),
            "usajobs": bool(self.USAJOBS_API_KEY and self.USAJOBS_EMAIL),
            "firecrawl": self.firecrawl.is_configured(),
            "firecrawl_key_count": self.firecrawl.available_key_count(),
            "brightdata": self.brightdata.is_configured(),
            "brightdata_key_count": self.brightdata.available_key_count(),
        }


_settings_instance: Optional[Settings] = None


def get_settings() -> Settings:
    global _settings_instance
    if _settings_instance is None:
        _settings_instance = Settings()
    return _settings_instance
