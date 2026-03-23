"""
ai-usage — Unified AI subscription usage tracker.

Check rate-limit usage across all your AI coding subscriptions
(OpenAI Codex, Anthropic Claude, …) with one command.

Usage:
  ai-usage                               # all providers, all accounts
  ai-usage --provider codex              # only Codex accounts
  ai-usage --provider claude             # only Claude accounts
  ai-usage --json                        # machine-readable JSON
  ai-usage --fix                         # switch to best account (providers that support it)
  ai-usage --fix --provider codex        # only fix Codex
  ai-usage login --provider codex        # interactive Codex relogin helper
  ai-usage --compact                     # one-line-per-account table view
  ai-usage --workers 50                  # control concurrency (default: 20)
"""

from __future__ import annotations

import json
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed

from .core.models import AccountUsage
from .core.display import (
    display_detailed,
    display_compact,
    display_summary,
    display_header,
    show_progress,
    clear_progress,
    BOLD, CYAN, RED, RESET,
)
from .core.balancer import handle_fix
from .providers.base import UsageProvider
from .providers.codex import CodexProvider
from .providers.claude import ClaudeProvider

# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

ALL_PROVIDERS: list[UsageProvider] = [
    CodexProvider(),
    ClaudeProvider(),
]

DEFAULT_WORKERS = 20


# ---------------------------------------------------------------------------
# Concurrent fetching
# ---------------------------------------------------------------------------

def fetch_all(
    providers: list[UsageProvider],
    max_workers: int,
    progress: bool = False,
) -> list[AccountUsage]:
    """Discover accounts from all providers and fetch usage concurrently."""
    # Build work items: (provider, account_dict)
    work: list[tuple[UsageProvider, dict]] = []
    for prov in providers:
        for acct in prov.discover_accounts():
            work.append((prov, acct))

    if not work:
        return []

    n = len(work)
    results: list[AccountUsage | None] = [None] * n
    completed = 0

    with ThreadPoolExecutor(max_workers=min(max_workers, n)) as pool:
        future_to_idx = {
            pool.submit(prov.fetch_one, acct): i
            for i, (prov, acct) in enumerate(work)
        }

        for future in as_completed(future_to_idx):
            idx = future_to_idx[future]
            try:
                results[idx] = future.result()
            except Exception as e:
                prov, acct = work[idx]
                results[idx] = AccountUsage(
                    provider=prov.name(),
                    name=acct.get("name", "?"),
                    email=acct.get("email", "?"),
                    error=str(e),
                )
            completed += 1
            if progress and n > 3:
                show_progress(completed, n)

    if progress and n > 3:
        clear_progress()

    return [r for r in results if r is not None]


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args() -> dict:
    args = sys.argv[1:]
    opts: dict = {
        "json": False,
        "fix": False,
        "compact": False,
        "provider": None,     # None = all providers
        "workers": DEFAULT_WORKERS,
        "command": None,
    }
    if args and args[0] == "login":
        opts["command"] = "login"
        args = args[1:]
    i = 0
    while i < len(args):
        if args[i] == "--json":
            opts["json"] = True
        elif args[i] == "--fix":
            opts["fix"] = True
        elif args[i] == "--compact":
            opts["compact"] = True
        elif args[i] == "--provider" and i + 1 < len(args):
            i += 1
            opts["provider"] = args[i].lower()
        elif args[i] == "--workers" and i + 1 < len(args):
            i += 1
            opts["workers"] = max(1, min(200, int(args[i])))
        elif args[i] in ("-h", "--help"):
            print(__doc__.strip())
            sys.exit(0)
        i += 1
    return opts


def main() -> None:
    opts = parse_args()
    json_mode: bool = opts["json"]
    fix_mode: bool = opts["fix"]
    compact_mode: bool = opts["compact"]
    max_workers: int = opts["workers"]
    provider_filter: str | None = opts["provider"]
    command: str | None = opts["command"]

    if command == "login":
        if provider_filter != "codex":
            print(f"{RED}ai-usage login currently supports Codex only.{RESET}")
            print("Usage: ai-usage login --provider codex")
            sys.exit(1)
        provider = CodexProvider()
        sys.exit(provider.interactive_login())

    # Select providers
    if provider_filter:
        providers = [p for p in ALL_PROVIDERS if p.name() == provider_filter]
        if not providers:
            known = ", ".join(p.name() for p in ALL_PROVIDERS)
            if json_mode:
                print(json.dumps({"error": f"unknown provider '{provider_filter}'", "known": known}))
            else:
                print(f"{RED}Unknown provider '{provider_filter}'. Known: {known}{RESET}")
            sys.exit(1)
    else:
        providers = ALL_PROVIDERS

    # Discover + fetch
    provider_names = [p.name() for p in providers]
    results = fetch_all(providers, max_workers, progress=not json_mode)

    if not results:
        if json_mode:
            print(json.dumps({"error": "no accounts found", "providers": provider_names}))
        else:
            print(f"{RED}No accounts found for: {', '.join(provider_names)}{RESET}")
            print("  Codex:  need ~/.codex/*.auth.json  (run: codex login)")
            print("  Claude: need Claude Code OAuth     (run: claude login)")
        sys.exit(1)

    n = len(results)

    # Auto-compact for many accounts
    if n > 10 and not compact_mode and not json_mode:
        compact_mode = True

    # Display
    if not json_mode and not fix_mode:
        display_header(n, max_workers, provider_names)

    if json_mode and not fix_mode:
        print(json.dumps([r.to_dict() for r in results], indent=2))
    elif not json_mode:
        if compact_mode:
            display_compact(results)
        else:
            display_detailed(results)
        display_summary(results)

    # --fix: per-provider
    if fix_mode:
        for prov in providers:
            if not prov.supports_switching():
                continue
            prov_results = [r for r in results if r.provider == prov.name()]
            if not prov_results:
                continue
            if not json_mode:
                heading = f"{prov.name().title()} --fix"
                print(f"  {BOLD}{CYAN}── {heading} {'─' * max(1, 52 - len(heading))}{RESET}")
                print()
            handle_fix(
                prov_results,
                json_mode,
                switch_fn=prov.switch_account,
                backup_fn=prov.ensure_backup,
                already_active_fn=getattr(prov, "already_active", None),
            )


if __name__ == "__main__":
    main()
