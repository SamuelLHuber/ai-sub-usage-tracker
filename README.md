# ai-sub-usage-tracker

Check OpenAI Codex usage limits (5h & 7d windows) across all your accounts, with automatic load-balancing. Scales to 1000+ accounts.

## Install

### Download binary (no Python needed)

```bash
# macOS (Apple Silicon)
curl -fSL https://github.com/SamuelLHuber/ai-sub-usage-tracker/releases/latest/download/codex-usage-macos-arm64 -o codex-usage
chmod +x codex-usage && mv codex-usage /usr/local/bin/

# Linux (x86_64)
curl -fSL https://github.com/SamuelLHuber/ai-sub-usage-tracker/releases/latest/download/codex-usage-linux-amd64 -o codex-usage
chmod +x codex-usage && sudo mv codex-usage /usr/local/bin/

# Windows (PowerShell)
Invoke-WebRequest -Uri https://github.com/SamuelLHuber/ai-sub-usage-tracker/releases/latest/download/codex-usage-windows-amd64.exe -OutFile codex-usage.exe
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
codex-usage --help         # show help
```

### `--fix` load-balancing

Automatically switches `~/.codex/auth.json` to the best account:

1. If any accounts are **not rate-limited** → picks the one with the most 7-day headroom
2. If **all are limited** → picks the one that resets soonest
3. Idempotent — won't switch if already on the best account

### Backup safety

The `--fix` command **never overwrites an unbacked auth file**. Before switching:

1. Reads the current `auth.json` and checks if its `account_id` exists in any `*.auth.json` file
2. If no backup exists, **automatically creates one** (e.g. `samuel.auth.json`) derived from the JWT email
3. Only then overwrites `auth.json` with the new account

Accounts that only exist in `auth.json` without a named backup are flagged with `⚠ UNBACKED` in the display.

### Scaling

| Accounts | Time (approx) | Notes |
|----------|---------------|-------|
| 4        | ~2s           | All concurrent |
| 100      | ~3–5s         | 20 workers default |
| 1000     | ~15–30s       | Use `--workers 50`+ |

- Concurrent HTTP via thread pool (default 20, up to 200 with `--workers`)
- Progress indicator on stderr for >5 accounts
- Auto-compact table at >10 accounts
- ANSI colors disabled when stdout is not a TTY

## How it works

Reads all `*.auth.json` files from `~/.codex/` and queries the ChatGPT usage API (`/backend-api/wham/usage`) to display:

- **5h window** — short-term rate limit with visual bar
- **7d window** — weekly rate limit with visual bar
- Reset countdowns for each window
- Rate-limited / OK status
- Plan type (PLUS, TEAM, etc.)
- Active account (● ACTIVE) and unbacked warnings (⚠ UNBACKED)

## Auth files

The tool expects Codex auth files in `~/.codex/`:

| File | Purpose |
|------|---------|
| `auth.json` | Currently active account (read by `codex`, overwritten by `--fix`) |
| `<name>.auth.json` | Named account profiles (e.g. `sam-hupf.auth.json`) |

Auth files are created by `codex login`. To add multiple accounts, log in and copy `auth.json` to a named file:

```bash
codex login              # logs in, writes ~/.codex/auth.json
cp ~/.codex/auth.json ~/.codex/my-account.auth.json
```

Repeat for each account.

## Development

### Releasing a new version

1. Make changes and push to `main`
2. Tag and push:
   ```bash
   git tag v0.2.0 main
   git push origin v0.2.0
   ```
3. GitHub Actions will automatically:
   - Build standalone binaries via PyInstaller (Linux amd64, macOS arm64, Windows amd64)
   - Create a GitHub Release with all binaries + SHA256 checksums

### CI

Every push to `main` and every PR runs:
- Python syntax check (`py_compile`)
- `--help` smoke test

### Project structure

```
codex-usage                        # single-file Python script (stdlib only)
.github/workflows/
  ci.yml                           # lint + smoke test
  release.yml                      # PyInstaller builds on tag push
```

## Requirements

- **Binary**: none (self-contained PyInstaller build)
- **From source**: Python 3.10+, no external dependencies (stdlib only: `concurrent.futures`, `urllib`, `json`, `base64`)

## License

MIT
