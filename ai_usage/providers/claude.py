"""
Anthropic Claude usage provider.

Reads Claude Code OAuth credentials from (in order):
  1. macOS Keychain ("Claude Code-credentials")
  2. ~/.claude/.credentials.json
  3. ~/.pi/agent/auth.json  (pi coding agent)

Automatically refreshes expired tokens using the stored refresh_token.
Queries https://api.anthropic.com/api/oauth/usage.
Single-account for now (no multi-account switching).
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from ..core.models import AccountUsage, ModelQuota, UsageWindow
from .base import UsageProvider

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
BETA_HEADER = "oauth-2025-04-20"
TOKEN_URL = "https://console.anthropic.com/v1/oauth/token"
CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
REQUEST_TIMEOUT = 15

CREDENTIALS_FILE = Path.home() / ".claude" / ".credentials.json"
PI_AUTH_FILE = Path.home() / ".pi" / "agent" / "auth.json"

PLAN_NAMES = {
    "default_claude_pro": "pro",
    "default_claude_max_5x": "max",
    "default_claude_max_20x": "max",
}


# ---------------------------------------------------------------------------
# Credential readers
# ---------------------------------------------------------------------------

def _read_keychain() -> dict | None:
    """Read Claude Code credentials from macOS Keychain."""
    if sys.platform != "darwin":
        return None
    try:
        result = subprocess.run(
            ["/usr/bin/security", "find-generic-password", "-s", "Claude Code-credentials", "-w"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode != 0 or not result.stdout.strip():
            return None
        return json.loads(result.stdout.strip())
    except Exception:
        return None


def _read_file_credentials() -> dict | None:
    """Read Claude Code credentials from ~/.claude/.credentials.json."""
    if not CREDENTIALS_FILE.exists():
        return None
    try:
        return json.loads(CREDENTIALS_FILE.read_text())
    except Exception:
        return None


def _read_pi_credentials() -> dict | None:
    """Read Anthropic OAuth credentials from ~/.pi/agent/auth.json."""
    if not PI_AUTH_FILE.exists():
        return None
    try:
        data = json.loads(PI_AUTH_FILE.read_text())
        anthropic = data.get("anthropic")
        if not isinstance(anthropic, dict):
            return None
        token = anthropic.get("access")
        if not token:
            return None
        # Normalize to the same shape as Claude Code credentials
        return {
            "claudeAiOauth": {
                "accessToken": token,
                "refreshToken": anthropic.get("refresh"),
                "expiresAt": anthropic.get("expires"),
            }
        }
    except Exception:
        return None


def _refresh_token(refresh_token: str) -> str | None:
    """Exchange a refresh_token for a new access_token via Anthropic's token endpoint."""
    try:
        payload = json.dumps({
            "grant_type": "refresh_token",
            "client_id": CLIENT_ID,
            "refresh_token": refresh_token,
        }).encode()
        req = urllib.request.Request(TOKEN_URL, data=payload, headers={
            "Content-Type": "application/json",
            "User-Agent": "ai-usage/1.0",
            "Accept": "application/json",
        })
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
            data = json.loads(resp.read().decode())
        return data.get("access_token")
    except Exception:
        return None


def _get_oauth_credentials() -> tuple[dict | None, str]:
    """Return (oauth_dict, source_name) from the best available credential source.

    The oauth_dict has keys: accessToken, refreshToken, expiresAt, subscriptionType, etc.

    Prefers non-expired tokens.  Falls back to expired ones (which can be
    refreshed or will produce a clear API error).
    """
    sources = [
        (_read_keychain, "keychain"),
        (_read_file_credentials, "file"),
        (_read_pi_credentials, "pi"),
    ]
    now_ms = int(time.time() * 1000)
    best_expired: tuple[dict, str] | None = None

    for reader, source in sources:
        data = reader()
        if not data:
            continue
        oauth = data.get("claudeAiOauth")
        if not isinstance(oauth, dict):
            continue
        if not oauth.get("accessToken"):
            continue
        expires_at = oauth.get("expiresAt")
        if not expires_at or expires_at >= now_ms:
            # Non-expired — use immediately
            return oauth, source
        # Expired — remember as fallback
        if best_expired is None:
            best_expired = (oauth, source)

    if best_expired:
        return best_expired
    return None, ""


def _get_oauth_token() -> tuple[str | None, str | None, str]:
    """Return (access_token, subscription_type, source) from the best available source.

    Handles token refresh transparently when the access token is expired but
    a refresh_token is available.
    """
    oauth, source = _get_oauth_credentials()
    if not oauth:
        return None, None, ""

    token = oauth.get("accessToken")
    sub_type = oauth.get("subscriptionType")
    expires_at = oauth.get("expiresAt")

    if token and (not expires_at or expires_at >= time.time() * 1000):
        return token, sub_type, source

    # Token expired — try refresh
    refresh_tok = oauth.get("refreshToken")
    if refresh_tok:
        new_token = _refresh_token(refresh_tok)
        if new_token:
            return new_token, sub_type, source

    # Even if expired, return it — the API will give a clear 401 error
    # which is more helpful than "no accounts found"
    if token:
        return token, sub_type, source

    return None, None, ""


def _detect_plan(sub_type: str | None, source: str = "") -> str:
    if sub_type:
        plan = PLAN_NAMES.get(sub_type)
        if plan:
            return plan
        lower = sub_type.lower()
        if "max" in lower:
            return "max"
        if "pro" in lower:
            return "pro"
        if "free" in lower:
            return "free"
    # No subscription info — label by source
    if source:
        return source
    return "unknown"


# ---------------------------------------------------------------------------
# API parsing
# ---------------------------------------------------------------------------

def _parse_iso_time(s: str) -> float:
    """Parse an ISO timestamp and return seconds-until-reset."""
    try:
        cleaned = s.replace("Z", "+00:00")
        dt = datetime.fromisoformat(cleaned)
        delta = (dt - datetime.now(timezone.utc)).total_seconds()
        return max(0.0, delta)
    except Exception:
        return 0.0


def _parse_usage_response(data: dict, plan_hint: str) -> AccountUsage:
    """Parse the Anthropic usage API response into an AccountUsage."""
    # Five-hour window
    fh = data.get("five_hour") or data.get("fiveHour") or {}
    fh_used = fh.get("utilization") or fh.get("usage_pct") or 0
    fh_reset_str = fh.get("resets_at") or fh.get("reset_at") or fh.get("resetAt") or ""
    fh_reset_secs = _parse_iso_time(fh_reset_str) if fh_reset_str else 0.0

    # Seven-day window
    sd = data.get("seven_day") or data.get("sevenDay") or {}
    sd_used = sd.get("utilization") or sd.get("usage_pct") or 0
    sd_reset_str = sd.get("resets_at") or sd.get("reset_at") or sd.get("resetAt") or ""
    sd_reset_secs = _parse_iso_time(sd_reset_str) if sd_reset_str else 0.0

    # Per-model breakdown
    models_raw = data.get("models") or data.get("model_quotas") or []
    model_quotas = [
        ModelQuota(
            model_name=m.get("model") or m.get("name") or "unknown",
            used_percent=m.get("utilization") or m.get("usage_pct") or 0,
        )
        for m in models_raw
    ]

    plan_type = data.get("plan_type") or data.get("planType") or plan_hint

    limit_reached = fh_used >= 100 or sd_used >= 100

    return AccountUsage(
        provider="claude",
        name="",         # filled in by caller
        email="",
        plan_type=plan_type,
        is_active=True,  # single-account, always active
        limit_reached=limit_reached,
        five_hour=UsageWindow(used_percent=fh_used, reset_seconds=fh_reset_secs),
        seven_day=UsageWindow(used_percent=sd_used, reset_seconds=sd_reset_secs),
        model_quotas=model_quotas,
    )


# ---------------------------------------------------------------------------
# Provider
# ---------------------------------------------------------------------------

class ClaudeProvider(UsageProvider):
    def name(self) -> str:
        return "claude"

    def discover_accounts(self) -> list[dict]:
        """Return a list with at most one account (single-account for now)."""
        token, sub_type, source = _get_oauth_token()
        if not token:
            return []
        return [{
            "access_token": token,
            "subscription_type": sub_type,
            "plan_type": _detect_plan(sub_type, source),
            "source": source,
        }]

    def fetch_one(self, account: dict) -> AccountUsage:
        if "error" in account:
            return AccountUsage(provider="claude", error=account["error"])

        token = account["access_token"]
        plan_hint = account.get("plan_type", "pro")
        source = account.get("source", "claude")

        try:
            req = urllib.request.Request(USAGE_URL, headers={
                "Authorization": f"Bearer {token}",
                "anthropic-beta": BETA_HEADER,
                "User-Agent": "ai-usage/1.0",
                "Accept": "*/*",
            })
            with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
                data = json.loads(resp.read().decode())

            usage = _parse_usage_response(data, plan_hint)
            usage.name = source
            return usage

        except urllib.error.HTTPError as e:
            if e.code == 401:
                msg = "OAuth token expired (401) — run: claude login"
            elif e.code == 403:
                msg = "Access denied (403) — token may lack required permissions"
            elif e.code == 429:
                retry = ""
                try:
                    ra = e.headers.get("retry-after")
                    if ra:
                        retry = f" (retry after {ra}s)"
                except Exception:
                    pass
                msg = f"Usage API throttled (429){retry} — quota data unavailable, not a subscription limit"
            else:
                body = ""
                try:
                    body = e.read().decode()[:200]
                except Exception:
                    pass
                msg = f"HTTP {e.code}: {body}"
            return AccountUsage(provider="claude", error=msg)

        except Exception as e:
            return AccountUsage(provider="claude", error=str(e))

    def supports_switching(self) -> bool:
        return False
