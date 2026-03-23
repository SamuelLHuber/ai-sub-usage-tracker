#!/usr/bin/env bash
set -eu

CODEX_DIR="${HOME}/.codex"

decode_auth_info() {
  python3 - "$1" <<'PY'
import base64
import json
import sys
from pathlib import Path

path = Path(sys.argv[1])
data = json.loads(path.read_text())
tokens = data.get("tokens", {}) if isinstance(data, dict) else {}
account_id = tokens.get("account_id") or ""
access = tokens.get("access_token") or ""

email = "unknown"
plan = "unknown"
for token_name in ("id_token", "access_token"):
    token = tokens.get(token_name) or ""
    if not token:
        continue
    try:
        parts = token.split(".")
        if len(parts) < 2:
            continue
        payload = parts[1] + "=" * (-len(parts[1]) % 4)
        decoded = base64.urlsafe_b64decode(payload)
        obj = json.loads(decoded)
        if email == "unknown":
            email = obj.get("email", "unknown")
        auth = obj.get("https://api.openai.com/auth", {})
        if isinstance(auth, dict) and plan == "unknown":
            plan = auth.get("chatgpt_plan_type", "unknown")
    except Exception:
        pass

print(account_id)
print(email)
print(plan)
PY
}

list_profiles() {
  python3 - <<'PY'
import base64
import glob
import json
import os
from pathlib import Path

codex_dir = Path.home() / ".codex"
files = sorted(glob.glob(str(codex_dir / "*.auth.json")))
active = codex_dir / "auth.json"
if active.exists():
    files.append(str(active))

rows = []
for p in files:
    try:
        data = json.loads(Path(p).read_text())
        tokens = data.get("tokens", {}) if isinstance(data, dict) else {}
        account_id = tokens.get("account_id") or ""
        email = "unknown"
        plan = "unknown"
        for token_name in ("id_token", "access_token"):
            token = tokens.get(token_name) or ""
            if not token:
                continue
            try:
                parts = token.split(".")
                if len(parts) < 2:
                    continue
                payload = parts[1] + "=" * (-len(parts[1]) % 4)
                decoded = base64.urlsafe_b64decode(payload)
                obj = json.loads(decoded)
                if email == "unknown":
                    email = obj.get("email", "unknown")
                auth = obj.get("https://api.openai.com/auth", {})
                if isinstance(auth, dict) and plan == "unknown":
                    plan = auth.get("chatgpt_plan_type", "unknown")
            except Exception:
                pass
        rows.append((os.path.basename(p), account_id, email, plan))
    except Exception as e:
        rows.append((os.path.basename(p), f"ERROR: {e}", "?", "?"))

widths = [0, 0, 0, 0]
headers = ["file", "account_id", "email", "plan"]
for i, h in enumerate(headers):
    widths[i] = len(h)
for row in rows:
    for i, value in enumerate(row):
        widths[i] = max(widths[i], len(str(value)))

print("Current ~/.codex profiles:\n")
print("  " + "  ".join(h.ljust(widths[i]) for i, h in enumerate(headers)))
print("  " + "  ".join("-" * widths[i] for i in range(len(headers))))
for row in rows:
    print("  " + "  ".join(str(v).ljust(widths[i]) for i, v in enumerate(row)))

seen = {}
for file_name, account_id, email, plan in rows:
    seen.setdefault(account_id, []).append(file_name)

dupes = {k: v for k, v in seen.items() if k and not str(k).startswith('ERROR:') and len(v) > 1}
print()
if dupes:
    print("Duplicate account_ids found:")
    for account_id, names in sorted(dupes.items()):
        print(f"  {account_id}: {', '.join(names)}")
else:
    print("No duplicate account_ids found.")
PY
}

save_current_auth_to_profile() {
  profile="$1"
  src="$CODEX_DIR/auth.json"
  dest="$CODEX_DIR/${profile}.auth.json"

  if [[ ! -f "$src" ]]; then
    echo "No active auth file found at $src"
    return 1
  fi

  cp "$src" "$dest"
  chmod 600 "$dest"

  info="$(decode_auth_info "$dest")"
  account_id="$(printf '%s\n' "$info" | sed -n '1p')"
  email="$(printf '%s\n' "$info" | sed -n '2p')"
  plan="$(printf '%s\n' "$info" | sed -n '3p')"

  echo
  echo "Saved profile: $dest"
  echo "  email:      $email"
  echo "  plan:       $plan"
  echo "  account_id: $account_id"

  echo "  matching files:"
  python3 - "$account_id" <<'PY'
import glob
import json
import os
import sys
from pathlib import Path

account_id = sys.argv[1]
codex_dir = Path.home() / ".codex"
for p in sorted(glob.glob(str(codex_dir / "*.auth.json"))):
    try:
        data = json.loads(Path(p).read_text())
        aid = data.get("tokens", {}).get("account_id")
        if aid == account_id:
            print(f"    - {os.path.basename(p)}")
    except Exception:
        pass

active = codex_dir / "auth.json"
if active.exists():
    try:
        data = json.loads(active.read_text())
        aid = data.get("tokens", {}).get("account_id")
        if aid == account_id:
            print("    - auth.json")
    except Exception:
        pass
PY
}

prompt_profiles() {
  echo "Enter the profile names you want to relogin/save."
  echo "Examples: aione-dtech aitwo-dtech aithree-dtech"
  echo "         or press Enter to use the suggested duplicates above."
  printf "> "
}

main() {
  mkdir -p "$CODEX_DIR"

  echo "=== Codex relogin helper ==="
  echo
  list_profiles
  echo

  prompt_profiles
  IFS= read -r input
  if [[ -n "$input" ]]; then
    profiles_raw="$(printf '%s\n' "$input" | tr ' ' '\n' | awk 'NF')"
  else
    profiles_raw="$(python3 - <<'PY'
import glob
import json
from collections import defaultdict
from pathlib import Path

codex_dir = Path.home() / '.codex'
files = sorted(glob.glob(str(codex_dir / '*.auth.json')))
by_id = defaultdict(list)
for p in files:
    try:
        data = json.loads(Path(p).read_text())
        aid = data.get('tokens', {}).get('account_id')
        if aid:
            by_id[aid].append(Path(p).stem.replace('.auth', ''))
    except Exception:
        pass

for aid, names in by_id.items():
    if len(names) > 1:
        for name in names:
            print(name)
PY
)"
  fi
  if [[ -z "$profiles_raw" ]]; then
    echo "No profiles selected. Exiting."
    exit 0
  fi

  echo
  echo "Profiles to refresh: $(printf '%s ' $profiles_raw)"

  profiles_file="$(mktemp)"
  printf '%s\n' "$profiles_raw" > "$profiles_file"
  old_stty="$(stty -g 2>/dev/null || true)"
  exec 3<&0
  while IFS= read -r profile; do
    echo
    echo "============================================================"
    echo "Profile: $profile"
    echo "1) A browser/device login will open for the account you want"
    echo "2) Complete login for the intended email"
    echo "3) This script will save ~/.codex/auth.json to ~/.codex/${profile}.auth.json"
    echo
    printf "Press Enter to run 'codex login' for %s..." "$profile"
    IFS= read -r _ <&3

    codex login
    if [[ -n "${old_stty:-}" ]]; then
      stty "$old_stty" 2>/dev/null || true
    fi

    echo
    live_info="$(decode_auth_info "$CODEX_DIR/auth.json")"
    live_account_id="$(printf '%s\n' "$live_info" | sed -n '1p')"
    live_email="$(printf '%s\n' "$live_info" | sed -n '2p')"
    live_plan="$(printf '%s\n' "$live_info" | sed -n '3p')"
    echo "Detected active login:"
    echo "  email:      ${live_email:-unknown}"
    echo "  plan:       ${live_plan:-unknown}"
    echo "  account_id: ${live_account_id:-unknown}"
    echo
    printf "Save this login to %s.auth.json? [y/N] " "$profile"
    IFS= read -r confirm <&3
    case "$confirm" in
      y|Y|yes|YES)
        save_current_auth_to_profile "$profile"
        ;;
      *)
        echo "Skipped saving ${profile}."
        ;;
    esac
  done < "$profiles_file"
  exec 3<&-
  rm -f "$profiles_file"

  echo
  echo "=== Final profile state ==="
  list_profiles
  echo
  echo "Done. You can now run: ai-usage --provider codex"
}

main "$@"
