"""
Multi-account load-balancer logic for ai_usage.

Extracted from the original codex-usage --fix logic and generalized so that
any provider that supports account switching can use it.
"""

from __future__ import annotations

import json
import sys

from .models import AccountUsage
from .display import (
    RESET, BOLD, DIM, RED, GREEN, YELLOW,
    format_time_remaining,
)


# ---------------------------------------------------------------------------
# Selection strategy
# ---------------------------------------------------------------------------

def pick_best_account(results: list[AccountUsage]) -> AccountUsage | None:
    """Pick the best account to switch to.

    Strategy:
      1. Among non-limited accounts, pick the one with the most 7d headroom.
      2. If ALL accounts are limited, pick the one that resets soonest.
    """
    valid = [r for r in results if r.ok]
    if not valid:
        return None

    available = [r for r in valid if not r.limit_reached]
    if available:
        available.sort(key=lambda r: (r.seven_day.used_percent, r.five_hour.used_percent))
        return available[0]

    # All limited — pick soonest reset
    def soonest_reset(r: AccountUsage) -> float:
        if r.seven_day.used_percent >= 100:
            return r.seven_day.reset_seconds
        if r.five_hour.used_percent >= 100:
            return r.five_hour.reset_seconds
        return min(r.five_hour.reset_seconds, r.seven_day.reset_seconds)

    valid.sort(key=soonest_reset)
    return valid[0]


def binding_reset_seconds(r: AccountUsage) -> float:
    """Return the reset seconds for the binding (maxed-out) window."""
    if r.seven_day.used_percent >= 100:
        return r.seven_day.reset_seconds
    return r.five_hour.reset_seconds


# ---------------------------------------------------------------------------
# Fix handler (provider-agnostic)
# ---------------------------------------------------------------------------

def handle_fix(
    results: list[AccountUsage],
    json_mode: bool,
    switch_fn,             # callable(AccountUsage) -> bool
    backup_fn=None,        # callable(quiet: bool) -> bool
    already_active_fn=None,  # callable(AccountUsage, AccountUsage | None, list[AccountUsage]) -> bool
) -> None:
    """Handle --fix: switch to the best account within a single provider group.

    *switch_fn*  — provider-specific function to make the switch.
    *backup_fn*  — optional; ensures the current active auth is backed up first.
    """
    best = pick_best_account(results)
    if not best:
        if not json_mode:
            print(f"  {RED}✗ No usable accounts found{RESET}")
            print()
        sys.exit(1)

    current_active = next((r for r in results if r.is_active), None)
    if already_active_fn is not None:
        already_active = already_active_fn(best, current_active, results)
    else:
        already_active = current_active is not None and current_active.name == best.name
    is_limited = best.limit_reached
    same_account = current_active is not None and current_active.name == best.name

    if already_active:
        if is_limited:
            reset_secs = binding_reset_seconds(best)
            if json_mode:
                print(json.dumps({
                    "action": "none",
                    "reason": "already_best_but_limited",
                    "provider": best.provider,
                    "account": best.name,
                    "email": best.email,
                    "resets_in": format_time_remaining(reset_secs),
                }))
            else:
                print(f"  {YELLOW}⚠ Already on best account:{RESET} {BOLD}{best.email}{RESET} {DIM}({best.name}){RESET}")
                print(f"    All accounts are rate-limited. Resets in {BOLD}{format_time_remaining(reset_secs)}{RESET}")
                print()
        else:
            if json_mode:
                print(json.dumps({
                    "action": "none",
                    "reason": "already_best",
                    "provider": best.provider,
                    "account": best.name,
                    "email": best.email,
                }))
            else:
                pct_left = 100 - best.seven_day.used_percent
                print(f"  {GREEN}✓ Already on best account:{RESET} {BOLD}{best.email}{RESET} {DIM}({best.name}){RESET}")
                print(f"    7d: {pct_left:.0f}% left")
                print()
        return

    # Need to switch
    if backup_fn is not None:
        backup_fn(quiet=json_mode)

    if switch_fn(best):
        if json_mode:
            out: dict = {
                "action": "synced" if same_account else "switched",
                "provider": best.provider,
                "account": best.name,
                "email": best.email,
                "limited": is_limited,
                "seven_day_used": best.seven_day.used_percent,
                "five_hour_used": best.five_hour.used_percent,
            }
            if is_limited:
                out["resets_in"] = format_time_remaining(binding_reset_seconds(best))
            print(json.dumps(out))
        else:
            if same_account:
                print(f"  {GREEN}{BOLD}⚡ Synchronized active account{RESET}")
                print(f"    {BOLD}{best.email}{RESET} {DIM}({best.name}){RESET}")
            else:
                prev_name = current_active.email if current_active else "unknown"
                print(f"  {GREEN}{BOLD}⚡ Switched active account{RESET}")
                print(f"    {DIM}{prev_name}{RESET} → {BOLD}{best.email}{RESET} {DIM}({best.name}){RESET}")
            if is_limited:
                print(f"    {YELLOW}⚠ Still rate-limited{RESET} (best available, resets in {format_time_remaining(binding_reset_seconds(best))})")
            else:
                print(f"    5h: {GREEN}{100 - best.five_hour.used_percent:.0f}% left{RESET}  ·  7d: {GREEN}{100 - best.seven_day.used_percent:.0f}% left{RESET}")
            print()
    else:
        if json_mode:
            print(json.dumps({
                "action": "error",
                "reason": "switch_failed",
                "provider": best.provider,
                "account": best.name,
            }))
        else:
            print(f"  {RED}✗ Failed to switch to {best.email}{RESET}")
            print()
        sys.exit(1)
