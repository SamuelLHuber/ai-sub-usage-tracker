"""
Abstract base for usage providers.

Each provider knows how to:
  1. Discover configured accounts on disk / keychain.
  2. Fetch usage for a single account (blocking, suitable for thread pool).
  3. Optionally switch the active account (multi-account load balancing).
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from ..core.models import AccountUsage


class UsageProvider(ABC):
    """Base class all providers implement."""

    @abstractmethod
    def name(self) -> str:
        """Short identifier, e.g. 'codex', 'claude'."""
        ...

    @abstractmethod
    def discover_accounts(self) -> list[dict]:
        """Return raw account dicts (provider-specific) for every configured profile."""
        ...

    @abstractmethod
    def fetch_one(self, account: dict) -> AccountUsage:
        """Fetch usage for *account* (blocking).  Must not raise — return error in AccountUsage.error."""
        ...

    # -- optional multi-account support ----------------------------------

    def supports_switching(self) -> bool:
        """Return True if this provider can switch the active account."""
        return False

    def switch_account(self, usage: AccountUsage) -> bool:
        """Switch to *usage*'s account.  Only called when supports_switching() is True."""
        raise NotImplementedError

    def ensure_backup(self, quiet: bool = False) -> bool:
        """Back up the current active auth before switching.  Optional."""
        return True
