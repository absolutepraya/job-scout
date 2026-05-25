#!/usr/bin/env bash
# Deploy job-scout on a Hermes-equipped VPS by symlinking skill/ and plugin/
# into the runtime paths Hermes + cron expect.
#
# Idempotent. Pre-existing real directories at the targets must be moved aside
# first so we don't silently overwrite live state. The script refuses to clobber.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

link_or_die() {
  local src="$1" dst="$2"
  if [ -L "$dst" ]; then
    ln -sfn "$src" "$dst"
    echo "  refreshed symlink $dst -> $src"
  elif [ ! -e "$dst" ]; then
    mkdir -p "$(dirname "$dst")"
    ln -sfn "$src" "$dst"
    echo "  created symlink $dst -> $src"
  else
    echo "ERROR: $dst exists and is not a symlink." >&2
    echo "Move it aside (e.g. mv $dst ${dst}.bak.\$(date +%s)) and re-run." >&2
    exit 1
  fi
}

echo "[job-scout] linking runtime dirs into $REPO_ROOT"
link_or_die "$REPO_ROOT/skill"  "$HOME/.agents/skills/job-watcher"
link_or_die "$REPO_ROOT/plugin" "$HOME/.hermes/plugins/job-manage-commands"

# Build the skill's venv on first install. Cron + run.py use this venv directly.
SKILL_VENV="$REPO_ROOT/skill/.venv"
if [ ! -d "$SKILL_VENV" ]; then
  echo "[job-scout] creating venv at $SKILL_VENV"
  if ! command -v uv >/dev/null 2>&1; then
    echo "ERROR: uv not found. Install with: curl -LsSf https://astral.sh/uv/install.sh | sh" >&2
    exit 1
  fi
  uv venv "$SKILL_VENV"
  uv pip install --python "$SKILL_VENV/bin/python" -r "$REPO_ROOT/requirements.txt"
fi

cat <<'EOF'

Done. Next steps:
  1. Ensure ~/.hermes/.env has DISCORD_BOT_TOKEN + the channel IDs (see .env.example).
  2. Restart hermes-gateway: sudo systemctl restart hermes-gateway
  3. Verify cron entry: crontab -l | grep job-watcher
     (The path stays the same since the symlink lives at ~/.agents/skills/job-watcher)
  4. Smoke test: /job in the management Discord channel.
EOF
