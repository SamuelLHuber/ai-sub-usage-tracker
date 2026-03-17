"""
OpenAI Codex / ChatGPT usage provider.

Reads accounts from:
  1. ~/.codex/*.auth.json  (codex CLI profiles)
  2. ~/.pi/agent/auth.json (pi coding agent, "openai-codex" entry)

Queries chatgpt.com/backend-api/wham/usage.
Supports multi-account discovery, load-balancing (--fix), and backup safety.
"""

from __future__ import annotations

import base64
import json
import os
import shutil
import urllib.error
import urllib.request
from pathlib import Path

from ..core.models import AccountUsage, UsageWindow
from ..core.display import CYAN, RESET
from .base import UsageProvider

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

CODEX_DIR = Path.home() / ".codex"
PI_AUTH_FILE = Path.home() / ".pi" / "agent" / "auth.json"
USAGE_URL = "https://chatgpt.com/backend-api/wham/usage"
REQUEST_TIMEOUT = 15


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _decode_jwt_payload(token: str) -> dict:
    """Decode JWT payload without verification."""
    try:
        parts = token.split(".")
        if len(parts) < 2:
            return {}
        payload = parts[1] + "=="
        decoded = base64.urlsafe_b64decode(payload)
        return json.loads(decoded)
    except Exception:
        return {}


# ---------------------------------------------------------------------------
# Provider
# ---------------------------------------------------------------------------

class CodexProvider(UsageProvider):
    def name(self) -> str:
        return "codex"

    # -- discovery --------------------------------------------------------

    def discover_accounts(self) -> list[dict]:
        """Find and load all auth.json files, deduplicated by account_id."""
        main_auth = CODEX_DIR / "auth.json"

        # Determine which account_id is currently active
        active_account_id = None
        if main_auth.exists():
            try:
                with open(main_auth) as f:
                    active_data = json.load(f)
                active_account_id = active_data.get("tokens", {}).get("account_id")
            except Exception:
                pass

        entries: list[dict] = []
        seen_accounts: set[str] = set()

        for p in sorted(CODEX_DIR.glob("*.auth.json")):
            profile = p.stem.replace(".auth", "")
            try:
                with open(p) as f:
                    data = json.load(f)

                tokens = data.get("tokens", {})
                access_token = tokens.get("access_token")
                account_id = tokens.get("account_id")

                if not access_token or not account_id:
                    continue
                if account_id in seen_accounts:
                    continue
                seen_accounts.add(account_id)

                id_token = tokens.get("id_token", "")
                jwt_payload = _decode_jwt_payload(id_token)
                email = jwt_payload.get("email", "unknown")
                auth_info = jwt_payload.get("https://api.openai.com/auth", {})
                plan_type = auth_info.get("chatgpt_plan_type", "unknown")

                entries.append({
                    "name": profile,
                    "path": str(p),
                    "email": email,
                    "plan_type": plan_type,
                    "account_id": account_id,
                    "access_token": access_token,
                    "is_active": account_id == active_account_id,
                    "has_backup": True,
                })
            except Exception as e:
                entries.append({"name": profile, "path": str(p), "error": str(e)})

        # If active account has no named backup, add from auth.json
        if active_account_id and active_account_id not in seen_accounts:
            try:
                with open(main_auth) as f:
                    data = json.load(f)
                tokens = data.get("tokens", {})
                jwt_payload = _decode_jwt_payload(tokens.get("id_token", ""))
                email = jwt_payload.get("email", "unknown")
                auth_info = jwt_payload.get("https://api.openai.com/auth", {})

                entries.insert(0, {
                    "name": "active",
                    "path": str(main_auth),
                    "email": email,
                    "plan_type": auth_info.get("chatgpt_plan_type", "unknown"),
                    "account_id": active_account_id,
                    "access_token": tokens.get("access_token"),
                    "is_active": True,
                    "has_backup": False,
                })
            except Exception:
                pass

        # Also check ~/.pi/agent/auth.json for an openai-codex entry
        self._add_pi_account(entries, seen_accounts, active_account_id)

        # Sort: active first, then alphabetically by email
        entries.sort(key=lambda x: (not x.get("is_active", False), x.get("email", "")))
        return entries

    @staticmethod
    def _add_pi_account(
        entries: list[dict],
        seen_accounts: set[str],
        active_account_id: str | None,
    ) -> None:
        """Add openai-codex account from ~/.pi/agent/auth.json if not already seen."""
        if not PI_AUTH_FILE.exists():
            return
        try:
            data = json.loads(PI_AUTH_FILE.read_text())
            codex = data.get("openai-codex")
            if not isinstance(codex, dict):
                return
            access_token = codex.get("access")
            account_id = codex.get("accountId")
            if not access_token or not account_id:
                return
            if account_id in seen_accounts:
                return
            seen_accounts.add(account_id)

            # Decode JWT to get email / plan info
            jwt_payload = _decode_jwt_payload(access_token)
            email = jwt_payload.get("email", "unknown")
            auth_info = jwt_payload.get("https://api.openai.com/auth", {})
            plan_type = auth_info.get("chatgpt_plan_type", "unknown")

            entries.append({
                "name": "pi",
                "path": str(PI_AUTH_FILE),
                "email": email,
                "plan_type": plan_type,
                "account_id": account_id,
                "access_token": access_token,
                "is_active": account_id == active_account_id,
                "has_backup": True,
            })
        except Exception:
            pass

    # -- fetching ---------------------------------------------------------

    def fetch_one(self, account: dict) -> AccountUsage:
        if "error" in account:
            return AccountUsage(
                provider="codex",
                name=account.get("name", "?"),
                email=account.get("email", "?"),
                error=account["error"],
                meta={"path": account.get("path", "")},
            )

        try:
            req = urllib.request.Request(USAGE_URL, headers={
                "Authorization": f"Bearer {account['access_token']}",
                "chatgpt-account-id": account["account_id"],
                "accept": "*/*",
            })
            with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
                usage = json.loads(resp.read().decode())

            rate_limit = usage.get("rate_limit") or {}
            primary = rate_limit.get("primary_window") or {}
            secondary = rate_limit.get("secondary_window") or {}

            fh_used = primary.get("used_percent", 0) or 0
            fh_reset = primary.get("reset_after_seconds", 0) or 0
            sd_used = secondary.get("used_percent", 0) or 0
            sd_reset = secondary.get("reset_after_seconds", 0) or 0

            limit_reached = rate_limit.get("limit_reached", False) or not rate_limit.get("allowed", True)

            return AccountUsage(
                provider="codex",
                name=account["name"],
                email=account["email"],
                plan_type=usage.get("plan_type", account["plan_type"]),
                is_active=account["is_active"],
                limit_reached=limit_reached,
                five_hour=UsageWindow(used_percent=fh_used, reset_seconds=fh_reset),
                seven_day=UsageWindow(used_percent=sd_used, reset_seconds=sd_reset),
                meta={
                    "path": account["path"],
                    "has_backup": account.get("has_backup", True),
                    "account_id": account["account_id"],
                },
            )

        except urllib.error.HTTPError as e:
            if e.code == 401:
                msg = "Token expired (401) — run: codex login"
            elif e.code == 403:
                msg = "Access denied (403)"
            elif e.code == 429:
                msg = "Rate limited by API (429) — try fewer workers"
            else:
                body = ""
                try:
                    body = e.read().decode()[:200]
                except Exception:
                    pass
                msg = f"HTTP {e.code}: {body}"
            return AccountUsage(
                provider="codex",
                name=account["name"],
                email=account.get("email", "?"),
                error=msg,
            )

        except Exception as e:
            return AccountUsage(
                provider="codex",
                name=account["name"],
                email=account.get("email", "?"),
                error=str(e),
            )

    # -- switching --------------------------------------------------------

    def supports_switching(self) -> bool:
        return True

    def switch_account(self, usage: AccountUsage) -> bool:
        source = Path(usage.meta["path"])
        target = CODEX_DIR / "auth.json"
        if not source.exists():
            return False
        shutil.copy2(source, target)
        return True

    def ensure_backup(self, quiet: bool = False) -> bool:
        """Ensure the current auth.json has a named *.auth.json backup."""
        main_auth = CODEX_DIR / "auth.json"
        if not main_auth.exists():
            return True

        try:
            with open(main_auth) as f:
                data = json.load(f)
            tokens = data.get("tokens", {})
            account_id = tokens.get("account_id")
            if not account_id:
                return True
        except Exception:
            return True

        # Check if any named *.auth.json has this account_id
        for p in CODEX_DIR.glob("*.auth.json"):
            try:
                with open(p) as f:
                    named = json.load(f)
                if named.get("tokens", {}).get("account_id") == account_id:
                    return True
            except Exception:
                continue

        # Not backed up — create one
        id_token = tokens.get("id_token", "")
        jwt_payload = _decode_jwt_payload(id_token)
        email = jwt_payload.get("email", "")

        if email:
            safe_name = email.split("@")[0].replace(".", "-").replace("+", "-")
            backup_path = CODEX_DIR / f"{safe_name}.auth.json"
            suffix = 2
            while backup_path.exists():
                backup_path = CODEX_DIR / f"{safe_name}-{suffix}.auth.json"
                suffix += 1
        else:
            backup_path = CODEX_DIR / f"account-{account_id[:8]}.auth.json"

        shutil.copy2(main_auth, backup_path)
        os.chmod(str(backup_path), 0o600)

        if not quiet:
            display_name = email or account_id[:12]
            print(f"  {CYAN}💾 Backed up current auth → {backup_path.name}{RESET}")
            print(f"    Account: {display_name}")
            print()

        return True
