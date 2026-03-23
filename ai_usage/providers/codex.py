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
import subprocess
import sys
import time
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
        payload = parts[1]
        padding = "=" * (-len(payload) % 4)
        payload = payload + padding
        decoded = base64.urlsafe_b64decode(payload)
        return json.loads(decoded)
    except Exception:
        return {}


def _jwt_expiry_ms(token: str) -> int | None:
    payload = _decode_jwt_payload(token)
    exp = payload.get("exp")
    if isinstance(exp, (int, float)):
        return int(exp * 1000)
    return None


def _read_json_file(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text())
    except Exception:
        return None


def _write_json_file(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n")
    os.chmod(str(path), 0o600)


def _codex_identity_from_payloads(*payloads: dict) -> dict:
    email = "unknown"
    plan_type = "unknown"
    user_id = None
    subject = None
    org_ids: list[str] = []

    for payload in payloads:
        if not isinstance(payload, dict):
            continue
        if email == "unknown":
            email = payload.get("email", "unknown")
        if subject is None:
            subject = payload.get("sub")

        auth_info = payload.get("https://api.openai.com/auth", {})
        if not isinstance(auth_info, dict):
            auth_info = {}

        if plan_type == "unknown":
            plan_type = auth_info.get("chatgpt_plan_type", "unknown")
        if user_id is None:
            user_id = auth_info.get("chatgpt_user_id") or auth_info.get("user_id")
        if not org_ids:
            organizations = auth_info.get("organizations", [])
            if isinstance(organizations, list):
                org_ids = [
                    org.get("id") for org in organizations
                    if isinstance(org, dict) and org.get("id")
                ]

    return {
        "email": email,
        "plan_type": plan_type,
        "user_id": user_id,
        "subject": subject,
        "org_ids": org_ids,
    }


def _account_identity_key(snapshot: dict) -> tuple[str, str]:
    user_id = snapshot.get("user_id")
    if user_id:
        return (snapshot.get("account_id", ""), user_id)
    return (snapshot.get("account_id", ""), snapshot.get("email", "unknown"))


def _classify_discovered_entries(entries: list[dict]) -> None:
    account_groups: dict[str, list[dict]] = {}
    identity_groups: dict[tuple[str, str], list[dict]] = {}
    for entry in entries:
        account_groups.setdefault(entry.get("account_id", ""), []).append(entry)
        identity_key = entry.get("identity_key")
        if identity_key is not None:
            identity_groups.setdefault(identity_key, []).append(entry)

    for entry in entries:
        shared_group = account_groups.get(entry.get("account_id", ""), [])
        duplicate_group = identity_groups.get(entry.get("identity_key"), [])
        entry["shared_account_files"] = sorted({item.get("path", "") for item in shared_group if item.get("path")})
        entry["duplicate_identity_files"] = sorted({item.get("path", "") for item in duplicate_group if item.get("path")})


def _profile_name_from_path(path: str) -> str:
    p = Path(path)
    if p.name == "auth.json":
        return "auth"
    if p.name.endswith(".auth.json"):
        return p.name[:-10]
    return p.stem


def _format_codex_profile_rows(entries: list[dict]) -> str:
    rows: list[tuple[str, str, str, str]] = []
    for entry in entries:
        rows.append((
            Path(entry.get("path", "")).name,
            entry.get("account_id", ""),
            entry.get("email", "unknown"),
            entry.get("plan_type", "unknown"),
        ))

    widths = [len(h) for h in ("file", "account_id", "email", "plan")]
    for row in rows:
        for i, value in enumerate(row):
            widths[i] = max(widths[i], len(str(value)))

    out = ["Current ~/.codex profiles:\n"]
    headers = ["file", "account_id", "email", "plan"]
    out.append("  " + "  ".join(h.ljust(widths[i]) for i, h in enumerate(headers)))
    out.append("  " + "  ".join("-" * widths[i] for i in range(len(headers))))
    for row in rows:
        out.append("  " + "  ".join(str(v).ljust(widths[i]) for i, v in enumerate(row)))

    duplicate_identity = [
        entry for entry in entries
        if len(entry.get("duplicate_identity_files", [])) > 1
    ]
    shared_accounts = [
        entry for entry in entries
        if len(entry.get("shared_account_files", [])) > 1
    ]

    out.append("")
    if duplicate_identity:
        out.append("Duplicate identities found:")
        seen: set[tuple[str, str]] = set()
        for entry in entries:
            identity_key = entry.get("identity_key")
            if not identity_key or identity_key in seen:
                continue
            dupes = entry.get("duplicate_identity_files", [])
            if len(dupes) > 1:
                seen.add(identity_key)
                out.append(f"  {identity_key[0]} / {identity_key[1]}: " + ", ".join(Path(p).name for p in dupes))
    else:
        out.append("No duplicate identities found.")

    if shared_accounts:
        out.append("")
        out.append("Shared workspace account_ids found:")
        seen_account_ids: set[str] = set()
        for entry in entries:
            account_id = entry.get("account_id", "")
            if not account_id or account_id in seen_account_ids:
                continue
            group = entry.get("shared_account_files", [])
            unique_identities = {
                item.get("identity_key") for item in entries if item.get("account_id") == account_id
            }
            if len(unique_identities) > 1:
                seen_account_ids.add(account_id)
                out.append(f"  {account_id}: " + ", ".join(Path(p).name for p in group))
    return "\n".join(out)


def _codex_snapshot_from_auth(data: dict) -> dict | None:
    tokens = data.get("tokens", {})
    if not isinstance(tokens, dict):
        return None

    access_token = tokens.get("access_token")
    account_id = tokens.get("account_id")
    if not access_token or not account_id:
        return None

    id_token = tokens.get("id_token") or ""
    id_payload = _decode_jwt_payload(id_token) if id_token else {}
    access_payload = _decode_jwt_payload(access_token)
    identity = _codex_identity_from_payloads(id_payload, access_payload)

    return {
        "source_kind": "codex",
        "access_token": access_token,
        "refresh_token": tokens.get("refresh_token"),
        "account_id": account_id,
        "id_token": id_token,
        "expires_ms": _jwt_expiry_ms(access_token),
        "email": identity["email"],
        "plan_type": identity["plan_type"],
        "user_id": identity["user_id"],
        "subject": identity["subject"],
        "org_ids": identity["org_ids"],
    }


def _codex_snapshot_from_pi(data: dict) -> dict | None:
    codex = data.get("openai-codex")
    if not isinstance(codex, dict):
        return None

    access_token = codex.get("access")
    account_id = codex.get("accountId")
    if not access_token or not account_id:
        return None

    access_payload = _decode_jwt_payload(access_token)
    identity = _codex_identity_from_payloads(access_payload)

    expires_ms = codex.get("expires")
    if not isinstance(expires_ms, int):
        expires_ms = _jwt_expiry_ms(access_token)

    return {
        "source_kind": "pi",
        "access_token": access_token,
        "refresh_token": codex.get("refresh"),
        "account_id": account_id,
        "id_token": "",
        "expires_ms": expires_ms,
        "email": identity["email"],
        "plan_type": identity["plan_type"],
        "user_id": identity["user_id"],
        "subject": identity["subject"],
        "org_ids": identity["org_ids"],
    }


def _build_codex_auth(snapshot: dict, template: dict | None = None) -> dict:
    template = template if isinstance(template, dict) else {}
    out = dict(template)

    tokens = out.get("tokens")
    if not isinstance(tokens, dict):
        tokens = {}
    else:
        tokens = dict(tokens)

    tokens["access_token"] = snapshot["access_token"]
    tokens["account_id"] = snapshot["account_id"]

    refresh_token = snapshot.get("refresh_token")
    if refresh_token:
        tokens["refresh_token"] = refresh_token
    else:
        tokens.pop("refresh_token", None)

    id_token = snapshot.get("id_token")
    if id_token:
        tokens["id_token"] = id_token
    else:
        tokens.pop("id_token", None)

    out["tokens"] = tokens
    out["auth_mode"] = out.get("auth_mode") or "chatgpt"
    out["last_refresh"] = out.get("last_refresh") or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    if "OPENAI_API_KEY" not in out:
        out["OPENAI_API_KEY"] = None
    return out


def _build_pi_entry(snapshot: dict, existing: dict | None = None) -> dict:
    existing = existing if isinstance(existing, dict) else {}
    out = dict(existing)
    out["type"] = out.get("type") or "oauth"
    out["access"] = snapshot["access_token"]
    out["accountId"] = snapshot["account_id"]

    refresh_token = snapshot.get("refresh_token")
    if refresh_token:
        out["refresh"] = refresh_token
    else:
        out.pop("refresh", None)

    expires_ms = snapshot.get("expires_ms")
    if isinstance(expires_ms, int):
        out["expires"] = expires_ms
    else:
        out.pop("expires", None)

    return out


# ---------------------------------------------------------------------------
# Provider
# ---------------------------------------------------------------------------

class CodexProvider(UsageProvider):
    def name(self) -> str:
        return "codex"

    # -- discovery --------------------------------------------------------

    def discover_accounts(self) -> list[dict]:
        """Find and load all auth.json files, deduplicated by stable user identity."""
        main_auth = CODEX_DIR / "auth.json"

        # Determine which account identity is currently active
        active_identity_key = None
        if main_auth.exists():
            try:
                with open(main_auth) as f:
                    active_data = json.load(f)
                active_snapshot = _codex_snapshot_from_auth(active_data)
                if active_snapshot:
                    active_identity_key = _account_identity_key(active_snapshot)
            except Exception:
                pass

        entries: list[dict] = []
        seen_accounts: set[tuple[str, str]] = set()

        for p in sorted(CODEX_DIR.glob("*.auth.json")):
            profile = p.stem.replace(".auth", "")
            try:
                with open(p) as f:
                    data = json.load(f)

                snapshot = _codex_snapshot_from_auth(data)
                if not snapshot:
                    continue

                identity_key = _account_identity_key(snapshot)
                if identity_key in seen_accounts:
                    continue
                seen_accounts.add(identity_key)

                entries.append({
                    "name": profile,
                    "path": str(p),
                    "email": snapshot["email"],
                    "plan_type": snapshot["plan_type"],
                    "account_id": snapshot["account_id"],
                    "user_id": snapshot.get("user_id"),
                    "subject": snapshot.get("subject"),
                    "org_ids": snapshot.get("org_ids", []),
                    "access_token": snapshot["access_token"],
                    "identity_key": identity_key,
                    "is_active": identity_key == active_identity_key,
                    "has_backup": True,
                    "source_kind": "codex",
                })
            except Exception as e:
                entries.append({"name": profile, "path": str(p), "error": str(e)})

        # If active account has no named backup, add from auth.json
        if active_identity_key and active_identity_key not in seen_accounts:
            try:
                with open(main_auth) as f:
                    data = json.load(f)
                snapshot = _codex_snapshot_from_auth(data)
                if not snapshot:
                    raise ValueError("invalid active auth.json")

                entries.insert(0, {
                    "name": "active",
                    "path": str(main_auth),
                    "email": snapshot["email"],
                    "plan_type": snapshot["plan_type"],
                    "account_id": snapshot["account_id"],
                    "user_id": snapshot.get("user_id"),
                    "subject": snapshot.get("subject"),
                    "org_ids": snapshot.get("org_ids", []),
                    "access_token": snapshot["access_token"],
                    "identity_key": active_identity_key,
                    "is_active": True,
                    "has_backup": False,
                    "source_kind": "codex",
                })
            except Exception:
                pass

        # Also check ~/.pi/agent/auth.json for an openai-codex entry
        self._add_pi_account(entries, seen_accounts, active_identity_key)

        _classify_discovered_entries(entries)

        # Sort: active first, then alphabetically by email
        entries.sort(key=lambda x: (not x.get("is_active", False), x.get("email", "")))
        return entries

    def interactive_login(self) -> int:
        entries = self.discover_accounts()
        print("=== Codex relogin helper ===")
        print()
        print(_format_codex_profile_rows(entries))
        print()
        print("Enter the profile names you want to relogin/save.")
        print("Examples: aione-dtech aitwo-dtech aithree-dtech")
        print("         or press Enter to use the suggested duplicate identities above.")
        print("         If there are no duplicate identities, nothing is auto-selected.")
        try:
            input_value = input("> ").strip()
        except KeyboardInterrupt:
            print()
            return 130

        if input_value:
            profiles = [part for part in input_value.split() if part]
        else:
            profiles = []
            seen: set[str] = set()
            for entry in entries:
                duplicates = entry.get("duplicate_identity_files", [])
                if len(duplicates) > 1:
                    profile = _profile_name_from_path(entry.get("path", ""))
                    if profile != "auth" and profile not in seen:
                        seen.add(profile)
                        profiles.append(profile)

        if not profiles:
            print("No profiles selected. Exiting.")
            return 0

        print()
        print("Profiles to refresh: " + " ".join(profiles))

        for profile in profiles:
            print()
            print("============================================================")
            print(f"Profile: {profile}")
            print("1) A browser/device login will open for the account you want")
            print("2) Complete login for the intended email")
            print(f"3) This command will save ~/.codex/auth.json to ~/.codex/{profile}.auth.json")
            print()
            try:
                input(f"Press Enter to run 'codex login' for {profile}...")
            except KeyboardInterrupt:
                print()
                return 130

            completed = subprocess.run(["codex", "login"])
            if completed.returncode != 0:
                print(f"codex login failed with exit code {completed.returncode}")
                return completed.returncode

            active_data = _read_json_file(CODEX_DIR / "auth.json")
            snapshot = _codex_snapshot_from_auth(active_data) if active_data else None
            if not snapshot:
                print(f"Failed to read {CODEX_DIR / 'auth.json'} after login")
                return 1

            print()
            print("Detected active login:")
            print(f"  email:      {snapshot.get('email', 'unknown')}")
            print(f"  plan:       {snapshot.get('plan_type', 'unknown')}")
            print(f"  account_id: {snapshot.get('account_id', 'unknown')}")
            if snapshot.get("user_id"):
                print(f"  user_id:    {snapshot['user_id']}")
            print()
            try:
                confirm = input(f"Save this login to {profile}.auth.json? [y/N] ").strip().lower()
            except KeyboardInterrupt:
                print()
                return 130

            if confirm not in ("y", "yes"):
                print(f"Skipped saving {profile}.")
                continue

            dest = CODEX_DIR / f"{profile}.auth.json"
            shutil.copy2(CODEX_DIR / "auth.json", dest)
            os.chmod(str(dest), 0o600)

            refreshed_entries = self.discover_accounts()
            matching = next((e for e in refreshed_entries if e.get("path") == str(dest)), None)
            print()
            print(f"Saved profile: {dest}")
            print(f"  email:      {snapshot.get('email', 'unknown')}")
            print(f"  plan:       {snapshot.get('plan_type', 'unknown')}")
            print(f"  account_id: {snapshot.get('account_id', 'unknown')}")
            if snapshot.get("user_id"):
                print(f"  user_id:    {snapshot['user_id']}")
            if matching:
                dupes = matching.get("duplicate_identity_files", [])
                shared = matching.get("shared_account_files", [])
                if dupes:
                    print("  matching identity files:")
                    for path in dupes:
                        print(f"    - {Path(path).name}")
                if len(shared) > 1:
                    print("  shared workspace account files:")
                    for path in shared:
                        print(f"    - {Path(path).name}")

        print()
        print("=== Final profile state ===")
        print(_format_codex_profile_rows(self.discover_accounts()))
        print()
        print("Done. You can now run: ai-usage --provider codex")
        return 0

    @staticmethod
    def _add_pi_account(
        entries: list[dict],
        seen_accounts: set[tuple[str, str]],
        active_identity_key: tuple[str, str] | None,
    ) -> None:
        """Add openai-codex account from ~/.pi/agent/auth.json if not already seen."""
        if not PI_AUTH_FILE.exists():
            return
        try:
            data = json.loads(PI_AUTH_FILE.read_text())
            snapshot = _codex_snapshot_from_pi(data)
            if not snapshot:
                return
            access_token = snapshot["access_token"]
            identity_key = _account_identity_key(snapshot)
            if identity_key in seen_accounts:
                return
            seen_accounts.add(identity_key)

            entries.append({
                "name": "pi",
                "path": str(PI_AUTH_FILE),
                "email": snapshot["email"],
                "plan_type": snapshot["plan_type"],
                "account_id": snapshot["account_id"],
                "user_id": snapshot.get("user_id"),
                "subject": snapshot.get("subject"),
                "org_ids": snapshot.get("org_ids", []),
                "access_token": access_token,
                "identity_key": identity_key,
                "is_active": identity_key == active_identity_key,
                "has_backup": True,
                "source_kind": "pi",
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

            response_email = usage.get("email") or account["email"]
            response_user_id = usage.get("user_id") or account.get("user_id")
            shared_account_files = account.get("shared_account_files", [])
            duplicate_identity_files = account.get("duplicate_identity_files", [])

            return AccountUsage(
                provider="codex",
                name=account["name"],
                email=response_email,
                plan_type=usage.get("plan_type", account["plan_type"]),
                is_active=account["is_active"],
                limit_reached=limit_reached,
                five_hour=UsageWindow(used_percent=fh_used, reset_seconds=fh_reset),
                seven_day=UsageWindow(used_percent=sd_used, reset_seconds=sd_reset),
                meta={
                    "path": account["path"],
                    "has_backup": account.get("has_backup", True),
                    "account_id": account["account_id"],
                    "user_id": response_user_id,
                    "subject": account.get("subject"),
                    "org_ids": account.get("org_ids", []),
                    "identity_key": account.get("identity_key"),
                    "source_kind": account.get("source_kind", "codex"),
                    "shared_account_files": shared_account_files,
                    "duplicate_identity_files": duplicate_identity_files,
                    "shared_account_count": len(shared_account_files),
                    "duplicate_identity_count": len(duplicate_identity_files),
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

    def already_active(
        self,
        best: AccountUsage,
        current_active: AccountUsage | None,
        results: list[AccountUsage],
    ) -> bool:
        expected_account_id = best.meta.get("account_id")
        if not expected_account_id:
            return current_active is not None and current_active.name == best.name

        codex_account_id = None
        codex_data = _read_json_file(CODEX_DIR / "auth.json")
        snapshot = _codex_snapshot_from_auth(codex_data) if codex_data else None
        if snapshot:
            codex_account_id = snapshot["account_id"]

        pi_account_id = None
        pi_data = _read_json_file(PI_AUTH_FILE)
        snapshot = _codex_snapshot_from_pi(pi_data) if pi_data else None
        if snapshot:
            pi_account_id = snapshot["account_id"]

        return codex_account_id == expected_account_id and pi_account_id == expected_account_id

    def switch_account(self, usage: AccountUsage) -> bool:
        source = Path(usage.meta["path"])
        if not source.exists():
            return False

        source_kind = usage.meta.get("source_kind", "codex")
        source_data = _read_json_file(source)
        if not source_data:
            return False

        if source_kind == "pi":
            snapshot = _codex_snapshot_from_pi(source_data)
            if not snapshot:
                return False
            current_codex = _read_json_file(CODEX_DIR / "auth.json")
            codex_auth = _build_codex_auth(snapshot, current_codex)
        else:
            snapshot = _codex_snapshot_from_auth(source_data)
            if not snapshot:
                return False
            codex_auth = source_data

        _write_json_file(CODEX_DIR / "auth.json", codex_auth)

        current_pi = _read_json_file(PI_AUTH_FILE) or {}
        existing_pi_entry = current_pi.get("openai-codex")
        current_pi["openai-codex"] = _build_pi_entry(snapshot, existing_pi_entry)
        _write_json_file(PI_AUTH_FILE, current_pi)
        return True

    def ensure_backup(self, quiet: bool = False) -> bool:
        self._ensure_codex_backup(quiet=quiet)
        self._ensure_pi_backup(quiet=quiet)
        return True

    def _ensure_codex_backup(self, quiet: bool = False) -> bool:
        """Ensure the current auth.json has a named *.auth.json backup."""
        main_auth = CODEX_DIR / "auth.json"
        if not main_auth.exists():
            return True

        try:
            data = json.loads(main_auth.read_text())
            snapshot = _codex_snapshot_from_auth(data)
            account_id = snapshot["account_id"] if snapshot else None
            if not account_id:
                return True
        except Exception:
            return True

        # Check if any named *.auth.json has this account_id
        for p in CODEX_DIR.glob("*.auth.json"):
            try:
                with open(p) as f:
                    named = json.load(f)
                named_snapshot = _codex_snapshot_from_auth(named)
                if named_snapshot and named_snapshot["account_id"] == account_id:
                    return True
            except Exception:
                continue

        # Not backed up — create one
        email = snapshot["email"] if snapshot else ""

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

    def _ensure_pi_backup(self, quiet: bool = False) -> bool:
        """Ensure the current ~/.pi/agent/auth.json has a backup for the active Codex account."""
        if not PI_AUTH_FILE.exists():
            return True

        data = _read_json_file(PI_AUTH_FILE)
        snapshot = _codex_snapshot_from_pi(data) if data else None
        if not snapshot:
            return True

        account_id = snapshot["account_id"]
        for p in sorted(PI_AUTH_FILE.parent.glob("auth.json.bak*")):
            backup_data = _read_json_file(p)
            backup_snapshot = _codex_snapshot_from_pi(backup_data) if backup_data else None
            if backup_snapshot and backup_snapshot["account_id"] == account_id:
                return True

        backup_path = PI_AUTH_FILE.parent / "auth.json.bak"
        suffix = 2
        while backup_path.exists():
            backup_path = PI_AUTH_FILE.parent / f"auth.json.bak.{suffix}"
            suffix += 1

        shutil.copy2(PI_AUTH_FILE, backup_path)
        os.chmod(str(backup_path), 0o600)

        if not quiet:
            display_name = snapshot["email"] or account_id[:12]
            print(f"  {CYAN}💾 Backed up current pi auth → {backup_path.name}{RESET}")
            print(f"    Account: {display_name}")
            print()

        return True
