# ai-sub-usage-tracker

Check OpenAI Codex usage limits (5h & 7d windows) across all your accounts, with automatic load-balancing. Scales to 1000+ accounts.

## Install

### Download binary (no Python needed)

```bash
# macOS (Apple Silicon)
curl -fSL https://github.com/SamuelLHuber/ai-sub-usage-tracker/releases/latest/download/codex-usage-macos-arm64 -o codex-usage
chmod +x codex-usage && mv codex-usage /usr/local/bin/

# macOS (Intel)
curl -fSL https://github.com/SamuelLHuber/ai-sub-usage-tracker/releases/latest/download/codex-usage-macos-amd64 -o codex-usage
chmod +x codex-usage && mv codex-usage /usr/local/bin/

# Linux (x86_64)
curl -fSL https://github.com/SamuelLHuber/ai-sub-usage-tracker/releases/latest/download/codex-usage-linux-amd64 -o codex-usage
chmod +x codex-usage && sudo mv codex-usage /usr/local/bin/
```

### From source (Python 3.10+, no dependencies)

```bash
git clone https://github.com/SamuelLHuber/ai-sub-usage-tracker.git
ln -sf "$(pwd)/ai-sub-usage-tracker/codex-usage" ~/.local/bin/codex-usage
```

## Usage

```bash
codex-usage                # show all accounts (auto-compact at >10)
codex-usage --compact      # force one-line-per-account table view
codex-usage --json         # machine-readable JSON
codex-usage --fix          # switch to the best available account
codex-usage --fix --json   # switch + machine-readable output
codex-usage --workers 50   # control concurrency (default: 20)
```

### `--fix` load-balancing

Automatically switches `~/.codex/auth.json` to the best account:

1. If any accounts are **not rate-limited** → picks the one with the most 7-day headroom
2. If **all are limited** → picks the one that resets soonest
3. **Safety**: always backs up `auth.json` before overwriting if the current account has no named `*.auth.json` backup
4. Idempotent — won't switch if already on the best account

### Scaling

| Accounts | Time (approx) | Notes |
|----------|---------------|-------|
| 4        | ~2s           | All concurrent |
| 100      | ~3–5s         | 20 workers default |
| 1000     | ~15–30s       | Use `--workers 50`+ |

- Concurrent HTTP via thread pool (default 20, up to 200 with `--workers`)
- Progress indicator for >5 accounts
- Auto-compact table at >10 accounts

## How it works

Reads all `*.auth.json` files from `~/.codex/` and queries the ChatGPT usage API to display:

- **5h window** — short-term rate limit with visual bar
- **7d window** — weekly rate limit with visual bar
- Reset countdowns
- Rate-limited / OK status
- Plan type (PLUS, TEAM, etc.)
- Active account (● ACTIVE) and unbacked warnings (⚠ UNBACKED)

## Auth files

The tool expects Codex auth files in `~/.codex/`:

- `auth.json` — currently active account (overwritten by `--fix`)
- `<name>.auth.json` — named account profiles

Created by `codex login`.

## License

MIT
