from __future__ import annotations

import base64
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ai_usage.core.models import AccountUsage
from ai_usage.providers import codex as codex_module
from ai_usage.providers.codex import CodexProvider


def _jwt(payload: dict) -> str:
    encoded = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    return f"x.{encoded}.y"


class CodexFixTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.codex_dir = self.root / ".codex"
        self.pi_auth = self.root / ".pi" / "agent" / "auth.json"
        self.codex_dir.mkdir(parents=True)
        self.pi_auth.parent.mkdir(parents=True)

        self.account_one = "acct-one"
        self.account_two = "acct-two"

        self.access_one = _jwt({
            "email": "one@example.com",
            "exp": 2_000_000_000,
            "sub": "auth0|one",
            "https://api.openai.com/auth": {
                "chatgpt_account_id": self.account_one,
                "chatgpt_plan_type": "plus",
                "chatgpt_user_id": "user-one",
            },
        })
        self.access_two = _jwt({
            "email": "two@example.com",
            "exp": 2_000_001_234,
            "sub": "auth0|two",
            "https://api.openai.com/auth": {
                "chatgpt_account_id": self.account_two,
                "chatgpt_plan_type": "pro",
                "chatgpt_user_id": "user-two",
            },
        })
        self.id_one = _jwt({
            "email": "one@example.com",
            "sub": "auth0|one",
            "https://api.openai.com/auth": {
                "chatgpt_plan_type": "plus",
                "chatgpt_user_id": "user-one",
            },
        })

        self.patcher = patch.multiple(
            codex_module,
            CODEX_DIR=self.codex_dir,
            PI_AUTH_FILE=self.pi_auth,
        )
        self.patcher.start()
        self.provider = CodexProvider()

    def tearDown(self) -> None:
        self.patcher.stop()
        self.tempdir.cleanup()

    def _write_codex_auth(
        self,
        path: Path,
        *,
        access: str,
        refresh: str,
        account_id: str,
        id_token: str = "",
        extra: dict | None = None,
    ) -> None:
        data = {
            "OPENAI_API_KEY": None,
            "auth_mode": "chatgpt",
            "last_refresh": "2026-03-18T00:00:00Z",
            "tokens": {
                "access_token": access,
                "refresh_token": refresh,
                "account_id": account_id,
            },
        }
        if id_token:
            data["tokens"]["id_token"] = id_token
        if extra:
            data.update(extra)
        path.write_text(json.dumps(data))

    def _write_pi_auth(self, providers: dict) -> None:
        self.pi_auth.write_text(json.dumps(providers))

    def test_discover_accounts_deduplicates_same_identity_from_pi(self) -> None:
        self._write_codex_auth(
            self.codex_dir / "work.auth.json",
            access=self.access_one,
            refresh="refresh-one",
            account_id=self.account_one,
            id_token=self.id_one,
        )
        self._write_codex_auth(
            self.codex_dir / "auth.json",
            access=self.access_one,
            refresh="refresh-one",
            account_id=self.account_one,
            id_token=self.id_one,
        )
        self._write_pi_auth({
            "openai-codex": {
                "type": "oauth",
                "access": self.access_one,
                "refresh": "refresh-one",
                "expires": 2_000_000_000_000,
                "accountId": self.account_one,
            },
        })

        accounts = self.provider.discover_accounts()

        self.assertEqual(len(accounts), 1)
        self.assertEqual(accounts[0]["name"], "work")
        self.assertEqual(accounts[0]["source_kind"], "codex")
        self.assertEqual(accounts[0]["user_id"], "user-one")

    def test_discover_accounts_keeps_distinct_users_on_same_account(self) -> None:
        access_same_account_other_user = _jwt({
            "email": "other@example.com",
            "exp": 2_000_002_222,
            "sub": "auth0|other",
            "https://api.openai.com/auth": {
                "chatgpt_account_id": self.account_one,
                "chatgpt_plan_type": "team",
                "chatgpt_user_id": "user-other",
            },
        })
        id_same_account_other_user = _jwt({
            "email": "other@example.com",
            "sub": "auth0|other",
            "https://api.openai.com/auth": {
                "chatgpt_plan_type": "team",
                "chatgpt_user_id": "user-other",
            },
        })

        self._write_codex_auth(
            self.codex_dir / "one.auth.json",
            access=self.access_one,
            refresh="refresh-one",
            account_id=self.account_one,
            id_token=self.id_one,
        )
        self._write_codex_auth(
            self.codex_dir / "other.auth.json",
            access=access_same_account_other_user,
            refresh="refresh-other",
            account_id=self.account_one,
            id_token=id_same_account_other_user,
        )

        accounts = self.provider.discover_accounts()

        self.assertEqual(len(accounts), 2)
        self.assertEqual({a["email"] for a in accounts}, {"one@example.com", "other@example.com"})
        self.assertEqual({a["user_id"] for a in accounts}, {"user-one", "user-other"})
        for account in accounts:
            self.assertEqual(len(account["shared_account_files"]), 2)
            self.assertEqual(len(account["duplicate_identity_files"]), 1)

    def test_switch_from_codex_profile_updates_pi_entry_only(self) -> None:
        self._write_codex_auth(
            self.codex_dir / "work.auth.json",
            access=self.access_one,
            refresh="refresh-one",
            account_id=self.account_one,
            id_token=self.id_one,
        )
        self._write_codex_auth(
            self.codex_dir / "auth.json",
            access=self.access_two,
            refresh="refresh-two",
            account_id=self.account_two,
        )
        self._write_pi_auth({
            "anthropic": {"type": "oauth", "access": "ant", "refresh": "r", "expires": 1},
            "openai-codex": {
                "type": "oauth",
                "access": self.access_two,
                "refresh": "refresh-two",
                "expires": 2_000_001_234_000,
                "accountId": self.account_two,
            },
        })

        usage = AccountUsage(
            provider="codex",
            name="work",
            email="one@example.com",
            meta={
                "path": str(self.codex_dir / "work.auth.json"),
                "account_id": self.account_one,
                "source_kind": "codex",
            },
        )

        self.assertTrue(self.provider.switch_account(usage))

        pi_data = json.loads(self.pi_auth.read_text())
        self.assertEqual(pi_data["anthropic"]["access"], "ant")
        self.assertEqual(pi_data["openai-codex"]["accountId"], self.account_one)
        self.assertEqual(pi_data["openai-codex"]["refresh"], "refresh-one")

    def test_switch_from_pi_account_builds_valid_codex_auth(self) -> None:
        self._write_codex_auth(
            self.codex_dir / "auth.json",
            access=self.access_one,
            refresh="refresh-one",
            account_id=self.account_one,
            id_token=self.id_one,
            extra={"custom": "keep-me"},
        )
        self._write_pi_auth({
            "anthropic": {"type": "oauth", "access": "ant", "refresh": "r", "expires": 1},
            "openai-codex": {
                "type": "oauth",
                "access": self.access_two,
                "refresh": "refresh-two",
                "expires": 2_000_001_234_000,
                "accountId": self.account_two,
            },
        })

        usage = AccountUsage(
            provider="codex",
            name="pi",
            email="two@example.com",
            meta={
                "path": str(self.pi_auth),
                "account_id": self.account_two,
                "source_kind": "pi",
            },
        )

        self.assertTrue(self.provider.switch_account(usage))

        codex_data = json.loads((self.codex_dir / "auth.json").read_text())
        self.assertEqual(codex_data["tokens"]["account_id"], self.account_two)
        self.assertEqual(codex_data["tokens"]["refresh_token"], "refresh-two")
        self.assertNotIn("id_token", codex_data["tokens"])
        self.assertEqual(codex_data["custom"], "keep-me")

    def test_already_active_requires_codex_and_pi_to_match(self) -> None:
        self._write_codex_auth(
            self.codex_dir / "auth.json",
            access=self.access_one,
            refresh="refresh-one",
            account_id=self.account_one,
            id_token=self.id_one,
        )
        self._write_pi_auth({
            "openai-codex": {
                "type": "oauth",
                "access": self.access_two,
                "refresh": "refresh-two",
                "expires": 2_000_001_234_000,
                "accountId": self.account_two,
            },
        })

        best = AccountUsage(
            provider="codex",
            name="work",
            email="one@example.com",
            meta={"account_id": self.account_one},
        )
        current = AccountUsage(
            provider="codex",
            name="work",
            email="one@example.com",
            is_active=True,
            meta={"account_id": self.account_one},
        )

        self.assertFalse(self.provider.already_active(best, current, []))

        self._write_pi_auth({
            "openai-codex": {
                "type": "oauth",
                "access": self.access_one,
                "refresh": "refresh-one",
                "expires": 2_000_000_000_000,
                "accountId": self.account_one,
            },
        })

        self.assertTrue(self.provider.already_active(best, current, []))

    def test_ensure_backup_creates_codex_named_backup_and_pi_backup(self) -> None:
        self._write_codex_auth(
            self.codex_dir / "auth.json",
            access=self.access_one,
            refresh="refresh-one",
            account_id=self.account_one,
            id_token=self.id_one,
        )
        self._write_pi_auth({
            "github-copilot": {"type": "oauth", "access": "gh", "refresh": "r", "expires": 1},
            "openai-codex": {
                "type": "oauth",
                "access": self.access_two,
                "refresh": "refresh-two",
                "expires": 2_000_001_234_000,
                "accountId": self.account_two,
            },
        })

        self.assertTrue(self.provider.ensure_backup(quiet=True))

        self.assertTrue((self.codex_dir / "one.auth.json").exists())
        self.assertTrue((self.pi_auth.parent / "auth.json.bak").exists())


if __name__ == "__main__":
    unittest.main()
