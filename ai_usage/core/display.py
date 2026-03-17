"""
ANSI terminal display for ai_usage.

Extracted & generalized from the original codex-usage display code.
Works with the unified AccountUsage model from any provider.
"""

from __future__ import annotations

import sys
from datetime import datetime

from .models import AccountUsage

# ---------------------------------------------------------------------------
# ANSI colors
# ---------------------------------------------------------------------------

RESET = "\033[0m"
BOLD = "\033[1m"
DIM = "\033[2m"
RED = "\033[31m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
BLUE = "\033[34m"
MAGENTA = "\033[35m"
CYAN = "\033[36m"
WHITE = "\033[37m"
BG_RED = "\033[41m"
CLEAR_LINE = "\033[2K\r"

# Disable colors when output is not a TTY (piped, redirected, etc.)
if not sys.stdout.isatty():
    RESET = BOLD = DIM = RED = GREEN = YELLOW = BLUE = MAGENTA = CYAN = WHITE = BG_RED = CLEAR_LINE = ""


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------

def format_time_remaining(seconds: float) -> str:
    if seconds <= 0:
        return "now"
    days = int(seconds // 86400)
    hours = int((seconds % 86400) // 3600)
    minutes = int((seconds % 3600) // 60)
    if days > 0:
        return f"{days}d {hours}h"
    if hours > 0:
        return f"{hours}h {minutes}m"
    if minutes > 0:
        return f"{minutes}m"
    return f"{int(seconds)}s"


def colorize_percent_used(used_pct: float) -> str:
    left_pct = 100 - used_pct
    if left_pct <= 0:
        return f"{BG_RED}{WHITE}{BOLD} {used_pct:5.1f}% used {RESET}"
    elif left_pct <= 10:
        return f"{RED}{BOLD}{used_pct:5.1f}% used{RESET}"
    elif left_pct <= 25:
        return f"{YELLOW}{used_pct:5.1f}% used{RESET}"
    elif left_pct <= 50:
        return f"{WHITE}{used_pct:5.1f}% used{RESET}"
    else:
        return f"{GREEN}{used_pct:5.1f}% used{RESET}"


def make_bar(used_pct: float, width: int = 20) -> str:
    filled = int(round(used_pct / 100 * width))
    filled = max(0, min(width, filled))
    empty = width - filled
    if used_pct >= 100:
        color = BG_RED + WHITE
    elif used_pct >= 75:
        color = RED
    elif used_pct >= 50:
        color = YELLOW
    else:
        color = GREEN
    bar = f"{color}{'█' * filled}{DIM}{'░' * empty}{RESET}"
    return f"[{bar}]"


def make_mini_bar(used_pct: float, width: int = 10) -> str:
    filled = int(round(used_pct / 100 * width))
    filled = max(0, min(width, filled))
    empty = width - filled
    if used_pct >= 100:
        color = RED
    elif used_pct >= 75:
        color = YELLOW
    else:
        color = GREEN
    return f"{color}{'█' * filled}{DIM}{'░' * empty}{RESET}"


def print_separator(char: str = "─", width: int = 60) -> None:
    print(f"{DIM}{char * width}{RESET}")


# ---------------------------------------------------------------------------
# Provider header / label helpers
# ---------------------------------------------------------------------------

PROVIDER_LABELS = {
    "codex": ("Codex", "OpenAI"),
    "claude": ("Claude", "Anthropic"),
}


def _provider_heading(provider: str) -> str:
    label, vendor = PROVIDER_LABELS.get(provider, (provider.title(), ""))
    tag = f" ({vendor})" if vendor else ""
    return f"{label}{tag}"


def _plan_color(plan_type: str) -> str:
    plan_lower = plan_type.lower()
    if plan_lower in ("team",):
        return MAGENTA
    if plan_lower in ("max",):
        return CYAN
    return BLUE


# ---------------------------------------------------------------------------
# Detailed view
# ---------------------------------------------------------------------------

def display_detailed(results: list[AccountUsage]) -> None:
    """Full multi-line-per-account view, grouped by provider."""
    providers_seen: list[str] = []
    for r in results:
        if r.provider not in providers_seen:
            providers_seen.append(r.provider)

    for prov in providers_seen:
        prov_results = [r for r in results if r.provider == prov]
        heading = _provider_heading(prov)
        print(f"  {BOLD}{CYAN}── {heading} {'─' * max(1, 52 - len(heading))}{RESET}")
        print()

        for r in prov_results:
            if not r.ok:
                label = r.email or r.name or r.provider
                print(f"  {RED}✗ {label}: {r.error}{RESET}")
                print()
                continue

            active_badge = f" {GREEN}● ACTIVE{RESET}" if r.is_active else ""
            unbacked_badge = ""
            if r.meta.get("has_backup") is False:
                unbacked_badge = f" {YELLOW}⚠ UNBACKED{RESET}"
            plan_color = _plan_color(r.plan_type)
            plan_badge = f"{plan_color}{r.plan_type.upper()}{RESET}"

            status_icon = (
                f"{RED}🚫 RATE LIMITED{RESET}" if r.limit_reached
                else f"{GREEN}✓{RESET}"
            )

            if r.email:
                name_tag = f" {DIM}({r.name}){RESET}" if r.name and r.name != r.email else ""
                identity = f"{BOLD}{r.email}{RESET}{name_tag}"
            else:
                identity = f"{BOLD}{r.name or r.provider}{RESET}"
            print(f"  {identity} [{plan_badge}]{active_badge}{unbacked_badge}")
            print(f"  Status: {status_icon}")
            print()

            fh = r.five_hour
            sd = r.seven_day
            fh_reset = format_time_remaining(fh.reset_seconds)
            sd_reset = format_time_remaining(sd.reset_seconds)
            print(f"    {BOLD}5h window{RESET}  {make_bar(fh.used_percent)}  {colorize_percent_used(fh.used_percent)}")
            print(f"    {DIM}           resets in {fh_reset}{RESET}")
            print(f"    {BOLD}7d window{RESET}  {make_bar(sd.used_percent)}  {colorize_percent_used(sd.used_percent)}")
            print(f"    {DIM}           resets in {sd_reset}{RESET}")

            # Per-model breakdown (Claude, etc.)
            if r.model_quotas:
                print()
                print(f"    {BOLD}Per model:{RESET}")
                for mq in r.model_quotas:
                    print(f"      {mq.model_name:<30} {make_bar(mq.used_percent, width=15)}  {colorize_percent_used(mq.used_percent)}")

            print()
            print_separator()
            print()


# ---------------------------------------------------------------------------
# Compact table view
# ---------------------------------------------------------------------------

def display_compact(results: list[AccountUsage]) -> None:
    """One-line-per-account table, grouped by provider."""
    providers_seen: list[str] = []
    for r in results:
        if r.provider not in providers_seen:
            providers_seen.append(r.provider)

    for prov in providers_seen:
        prov_results = [r for r in results if r.provider == prov]
        heading = _provider_heading(prov)
        print(f"  {BOLD}{CYAN}── {heading} {'─' * max(1, 52 - len(heading))}{RESET}")
        print(f"  {BOLD}{'Account':<35} {'Plan':<6} {'5h':<16} {'7d':<16} {'Reset':<10} {'Status':<10}{RESET}")
        print_separator("─", 95)

        for r in prov_results:
            if not r.ok:
                email = (r.email or r.name or r.provider)[:33]
                print(f"  {email:<35} {'?':<6} {'?':<16} {'?':<16} {'?':<10} {RED}ERR{RESET}")
                continue

            email = (r.email or r.name or r.provider)[:33]
            active = "●" if r.is_active else " "
            plan = r.plan_type[:5]

            fh_pct = r.five_hour.used_percent
            sd_pct = r.seven_day.used_percent

            fh_col = f"{make_mini_bar(fh_pct)} {fh_pct:5.1f}%"
            sd_col = f"{make_mini_bar(sd_pct)} {sd_pct:5.1f}%"

            if sd_pct >= 100:
                reset = format_time_remaining(r.seven_day.reset_seconds)
            elif fh_pct >= 100:
                reset = format_time_remaining(r.five_hour.reset_seconds)
            else:
                reset = "—"

            status = f"{RED}LIMITED{RESET}" if r.limit_reached else f"{GREEN}OK{RESET}"
            unbacked = f" {YELLOW}!{RESET}" if r.meta.get("has_backup") is False else ""

            print(f"  {active} {email:<33} {plan:<6} {fh_col} {sd_col} {reset:<10} {status}{unbacked}")

        print()


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------

def display_summary(results: list[AccountUsage]) -> None:
    ok = [r for r in results if r.ok]
    limited = [r for r in ok if r.limit_reached]
    available = len(ok) - len(limited)
    errors = [r for r in results if not r.ok]

    providers = sorted(set(r.provider for r in results))
    prov_counts = ", ".join(
        f"{sum(1 for r in results if r.provider == p)} {p}"
        for p in providers
    )

    acct_word = "account" if len(ok) == 1 else "accounts"
    parts = [f"{BOLD}{len(ok)} {acct_word}{RESET} ({prov_counts})"]
    if available > 0:
        parts.append(f"{GREEN}{available} available{RESET}")
    if limited:
        parts.append(f"{RED}{len(limited)} rate-limited{RESET}")
    if errors:
        parts.append(f"{YELLOW}{len(errors)} errors{RESET}")
    print(f"  {' · '.join(parts)}")
    print()


# ---------------------------------------------------------------------------
# Header
# ---------------------------------------------------------------------------

def display_header(total_accounts: int, max_workers: int, providers: list[str]) -> None:
    prov_str = " + ".join(_provider_heading(p) for p in providers)
    print()
    acct_word = "account" if total_accounts == 1 else "accounts"
    print(f"  {BOLD}{CYAN}⚡ AI Usage Monitor{RESET}  {DIM}({prov_str} · {total_accounts} {acct_word}, {max_workers} workers){RESET}")
    print(f"  {DIM}{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}{RESET}")
    print()


# ---------------------------------------------------------------------------
# Progress
# ---------------------------------------------------------------------------

def show_progress(completed: int, total: int) -> None:
    pct = completed * 100 // total
    sys.stderr.write(f"{CLEAR_LINE}  Fetching usage... {completed}/{total} ({pct}%)")
    sys.stderr.flush()


def clear_progress() -> None:
    sys.stderr.write(f"{CLEAR_LINE}")
    sys.stderr.flush()
