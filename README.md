# ai-sub-usage-tracker

Check AI subscription usage limits (5h & 7d windows) across **all your accounts and providers**, with automatic load-balancing.

Supports:
- **OpenAI Codex** — reads `~/.codex/*.auth.json`, queries ChatGPT usage API
- **Anthropic Claude** — reads Claude Code OAuth from Keychain / `~/.claude/.credentials.json`, queries Anthropic usage API

Zero dependencies (Python stdlib only). Scales to 1000+ accounts.

## Install

### Download binary (no Python needed)

```bash
# macOS (Apple Silicon)
curl -fSL https://github.com/SamuelLHuber/ai-sub-usage-tracker/releases/latest/download/ai-usage-macos-arm64 -o ai-usage
chmod +x ai-usage && mv ai-usage /usr/local/bin/

# Linux (x86_64)
curl -fSL https://github.com/SamuelLHuber/ai-sub-usage-tracker/releases/latest/download/ai-usage-linux-amd64 -o ai-usage
chmod +x ai-usage && sudo mv ai-usage /usr/local/bin/

# Windows (PowerShell)
Invoke-WebRequest -Uri https://github.com/SamuelLHuber/ai-sub-usage-tracker/releases/latest/download/ai-usage-windows-amd64.exe -OutFile ai-usage.exe
```

Single-provider binaries (`codex-usage`, `claude-usage`) are also available in releases.

### From source (Python 3.10+, no dependencies)

```bash
git clone https://github.com/SamuelLHuber/ai-sub-usage-tracker.git
cd ai-sub-usage-tracker

# Unified (all providers)
ln -sf "$(pwd)/ai-usage" ~/.local/bin/ai-usage

# Provider-specific shortcuts
ln -sf "$(pwd)/codex-usage" ~/.local/bin/codex-usage
ln -sf "$(pwd)/claude-usage" ~/.local/bin/claude-usage
```

## Usage

### Unified (all providers)

```bash
ai-usage                          # all providers, all accounts
ai-usage --provider codex         # only Codex accounts
ai-usage --provider claude        # only Claude accounts
ai-usage --json                   # machine-readable JSON
ai-usage --fix                    # switch to best account (Codex)
ai-usage --fix --provider codex   # only fix Codex
ai-usage --compact                # one-line-per-account table view
ai-usage --workers 50             # control concurrency (default: 20)
ai-usage --help
```

### Provider shortcuts (backward-compatible)

```bash
codex-usage              # same as: ai-usage --provider codex
codex-usage --fix        # auto-switch to best Codex account
codex-usage --json       # JSON output

claude-usage             # same as: ai-usage --provider claude
claude-usage --json      # JSON output
```

### Output example

```
  ⚡ AI Usage Monitor  (Codex (OpenAI) + Claude (Anthropic) · 5 accounts, 20 workers)
  2026-03-17 14:32:00

  ── Codex (OpenAI) ──────────────────────────────────────

  sam@example.com (sam) [TEAM] ● ACTIVE
  Status: ✓

    5h window  [████████░░░░░░░░░░░░]   42.3% used
               resets in 2h 15m
    7d window  [██████████████░░░░░░]   71.0% used
               resets in 3d 5h

  ── Claude (Anthropic) ──────────────────────────────────

  claude [MAX]
  Status: ✓

    5h window  [██████░░░░░░░░░░░░░░]   33.2% used
               resets in 3h 10m
    7d window  [████████████░░░░░░░░]   58.7% used
               resets in 4d 12h

    Per model:
      claude-sonnet-4                [██████████░░░░░]   55.0% used
      claude-opus-4                  [██████████████░]   92.0% used

  5 accounts (1 claude, 4 codex) · 2 available · 3 rate-limited
```

### `--fix` load-balancing (Codex)

Automatically switches the active Codex account everywhere it is used:

- `~/.codex/auth.json`
- `~/.pi/agent/auth.json` (`openai-codex`)

1. If any accounts are **not rate-limited** → picks the one with the most 7-day headroom
2. If **all are limited** → picks the one that resets soonest
3. Idempotent — won't switch if both Codex and Pi are already on the best account

### Backup safety

The `--fix` command **never overwrites an unbacked auth file**. Before switching:

1. Reads the current `auth.json` and checks if its `account_id` exists in any `*.auth.json`
2. If no backup exists, **automatically creates one** (e.g. `samuel.auth.json`)
3. Also creates a backup of `~/.pi/agent/auth.json` for the current Pi Codex account when needed
4. Only then overwrites the active auth files with the new account

## Auth setup

### Codex (OpenAI)

| Source | Location |
|--------|----------|
| Codex profiles | `~/.codex/*.auth.json` (named account profiles) |
| Codex active | `~/.codex/auth.json` (currently active, overwritten by `--fix`) |
| Pi agent | `~/.pi/agent/auth.json` (`openai-codex` entry) |

```bash
codex login              # logs in, writes ~/.codex/auth.json
cp ~/.codex/auth.json ~/.codex/my-account.auth.json
```

### Claude (Anthropic)

Credentials are read automatically from Claude Code's auth:

| Source | Location |
|--------|----------|
| macOS Keychain | `"Claude Code-credentials"` (preferred) |
| File | `~/.claude/.credentials.json` |
| Pi agent | `~/.pi/agent/auth.json` (`anthropic` entry) |

```bash
claude login             # authenticate Claude Code
```

Expired tokens are automatically refreshed using the stored refresh_token.

## Architecture

```
ai-usage                     # unified entry point
codex-usage                  # backward-compatible wrapper (→ ai-usage --provider codex)
claude-usage                 # convenience wrapper (→ ai-usage --provider claude)
ai_usage/
  cli.py                     # CLI arg parsing, orchestration
  core/
    models.py                # Shared data models (AccountUsage, UsageWindow, etc.)
    display.py               # ANSI terminal formatting (bars, tables, colors)
    balancer.py              # Multi-account selection & switching logic
  providers/
    base.py                  # Abstract UsageProvider interface
    codex.py                 # OpenAI Codex provider
    claude.py                # Anthropic Claude provider
```

### Adding a new provider

1. Create `ai_usage/providers/myservice.py`
2. Implement `UsageProvider` (see `base.py`):
   - `name()` → `"myservice"`
   - `discover_accounts()` → find auth files / credentials
   - `fetch_one(account)` → query the API, return `AccountUsage`
   - Optionally: `supports_switching()`, `switch_account()`, `ensure_backup()`
3. Register in `ai_usage/cli.py`:
   ```python
   ALL_PROVIDERS = [CodexProvider(), ClaudeProvider(), MyServiceProvider()]
   ```

### Scaling

| Accounts | Time (approx) | Notes |
|----------|---------------|-------|
| 5        | ~2s           | All concurrent |
| 100      | ~3–5s         | 20 workers default |
| 1000     | ~15–30s       | Use `--workers 50`+ |

## Development

### Releasing a new version

1. Make changes and push to `main`
2. Tag and push:
   ```bash
   git tag v0.3.0 main
   git push origin v0.3.0
   ```
3. GitHub Actions builds standalone binaries for all three entry points

### CI

Every push to `main` and every PR runs:
- Syntax check on all Python files
- `--help` smoke test for all entry points
- `--json` graceful-failure test
- Provider filter validation

## Requirements

- **Binary**: none (self-contained PyInstaller build)
- **From source**: Python 3.10+, no external dependencies (stdlib only)

## License

MIT
