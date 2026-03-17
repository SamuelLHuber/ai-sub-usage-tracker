"""
Shared data models for ai_usage.

Every provider normalizes its API response into these structures so the
display / balancer layers don't need to know which provider produced them.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


# ---------------------------------------------------------------------------
# Usage windows
# ---------------------------------------------------------------------------

@dataclass
class UsageWindow:
    """A single rate-limit window (e.g. 5-hour or 7-day)."""
    used_percent: float = 0.0
    left_percent: float = 100.0
    reset_seconds: float = 0.0
    reset_time: datetime | None = None   # absolute; Codex gives relative, Claude gives absolute

    def __post_init__(self):
        self.left_percent = round(100.0 - self.used_percent, 1)


# ---------------------------------------------------------------------------
# Per-model quota (Claude-specific today, but generic enough for others)
# ---------------------------------------------------------------------------

@dataclass
class ModelQuota:
    model_name: str = ""
    used_percent: float = 0.0


# ---------------------------------------------------------------------------
# Unified account usage result
# ---------------------------------------------------------------------------

@dataclass
class AccountUsage:
    """One account's usage, regardless of provider."""
    provider: str = ""              # "codex" | "claude"
    name: str = ""                  # short display name / profile name
    email: str = ""
    plan_type: str = ""             # "plus", "team", "pro", "max", …
    is_active: bool = False
    limit_reached: bool = False
    five_hour: UsageWindow = field(default_factory=UsageWindow)
    seven_day: UsageWindow = field(default_factory=UsageWindow)
    model_quotas: list[ModelQuota] = field(default_factory=list)
    error: str | None = None
    meta: dict = field(default_factory=dict)   # provider-specific extras

    # Convenience -------------------------------------------------------
    @property
    def ok(self) -> bool:
        return self.error is None

    def to_dict(self) -> dict:
        """JSON-friendly dict."""
        d: dict = {
            "provider": self.provider,
            "name": self.name,
            "email": self.email,
            "plan_type": self.plan_type,
            "is_active": self.is_active,
        }
        if self.error:
            d["error"] = self.error
            return d

        d["limit_reached"] = self.limit_reached
        d["five_hour"] = {
            "used_percent": self.five_hour.used_percent,
            "left_percent": self.five_hour.left_percent,
            "reset_seconds": self.five_hour.reset_seconds,
        }
        d["seven_day"] = {
            "used_percent": self.seven_day.used_percent,
            "left_percent": self.seven_day.left_percent,
            "reset_seconds": self.seven_day.reset_seconds,
        }
        if self.model_quotas:
            d["model_quotas"] = [
                {"model_name": m.model_name, "used_percent": m.used_percent}
                for m in self.model_quotas
            ]
        d["meta"] = self.meta
        return d
