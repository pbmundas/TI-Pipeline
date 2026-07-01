#!/usr/bin/env bash
# Daily pipeline run:
#   1) fetch RSS -> enrich via local Ollama -> write unified_report.json
#   2) push the updated file to the GitHub Pages repo that serves the dashboard
#
# Intended to be called from cron (see scripts/setup_cron.sh).
# Logs to data/pipeline.log.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$SCRIPT_DIR"

LOG_FILE="$SCRIPT_DIR/data/pipeline.log"
mkdir -p "$SCRIPT_DIR/data"

# Activate venv if present
if [ -f "$SCRIPT_DIR/.venv/bin/activate" ]; then
    # shellcheck disable=SC1091
    source "$SCRIPT_DIR/.venv/bin/activate"
fi

{
echo "===== Run started: $(date -u '+%Y-%m-%dT%H:%M:%SZ') ====="

# ---------------------------------------------------------------------------
# 0. Preflight: Ollama must be up before we burn time on RSS fetches.
# ---------------------------------------------------------------------------
if ! curl -fsS --max-time 5 "http://localhost:11434/api/tags" > /dev/null 2>&1; then
    echo "ERROR: Ollama is not reachable on localhost:11434. Aborting."
    exit 1
fi

# ---------------------------------------------------------------------------
# 1. Sync the dashboard repo first, so we never generate the new file on top
#    of a stale working tree (and so `git pull --rebase` never trips over
#    the unstaged file we're about to write in step 2).
# ---------------------------------------------------------------------------
eval "$(python3 - <<'PYEOF'
import yaml
cfg = yaml.safe_load(open("config.yaml"))
print(f'DASHBOARD_REPO={cfg["paths"]["dashboard_repo"]!r}')
print(f'OUTPUT_REL_PATH={cfg["paths"]["output_relative_path"]!r}')
print(f'ARTICLES_REL_PATH={cfg["paths"]["articles_file"]!r}')
print(f'GIT_ENABLED={str(cfg["git"].get("enabled", True)).lower()!r}')
print(f'GIT_BRANCH={cfg["git"]["branch"]!r}')
print(f'GIT_REMOTE={cfg["git"]["remote"]!r}')
print(f'COMMIT_PREFIX={cfg["git"]["commit_message_prefix"]!r}')
PYEOF
)"

if [ "$GIT_ENABLED" == "true" ]; then
    if [ ! -d "$DASHBOARD_REPO/.git" ]; then
        echo "ERROR: $DASHBOARD_REPO is not a git repo. Clone it first, e.g.:"
        echo "  git clone https://github.com/<you>/<your-pages-repo>.git \"$DASHBOARD_REPO\""
        exit 1
    fi
    echo "--- Step 1: syncing dashboard repo ($DASHBOARD_REPO) ---"
    cd "$DASHBOARD_REPO"

    # If there are any local changes already sitting in the working tree
    # (leftover generated output from a previous run that never got pushed,
    # or manual edits), commit them FIRST so `git pull --rebase` never fails
    # with "you have unstaged changes." The whole point of this script is to
    # run unattended - it should never require a human to manually resolve
    # git state before it can proceed.
    git add -A
    if ! git diff --cached --quiet; then
        echo "Found pre-existing local changes - committing them before syncing..."
        git commit -m "$COMMIT_PREFIX (pre-sync) $(date -u '+%Y-%m-%d %H:%M UTC')"
    fi

    git pull --rebase "$GIT_REMOTE" "$GIT_BRANCH"
    cd "$SCRIPT_DIR"
fi

# ---------------------------------------------------------------------------
# 2. DATA GENERATION: fetch feeds, run local LLM enrichment, write the JSON.
#    --no-publish: pipeline.py only writes the file here; the git push below
#    is handled explicitly in this script, not inside the Python code.
# ---------------------------------------------------------------------------
echo "--- Step 2: generating data ---"
python3 -m src.pipeline --config config.yaml --no-publish

if [ "$GIT_ENABLED" != "true" ]; then
    echo "Git publishing disabled in config.yaml (git.enabled: false). Skipping push."
    echo "===== Run finished: $(date -u '+%Y-%m-%dT%H:%M:%SZ') ====="
    exit 0
fi

# ---------------------------------------------------------------------------
# 3. GITHUB PUSH: commit the freshly generated file and push to the
#    GitHub Pages repo so the live dashboard picks it up.
# ---------------------------------------------------------------------------
echo "--- Step 3: pushing to GitHub Pages repo ($DASHBOARD_REPO) ---"
cd "$DASHBOARD_REPO"

# Stage EVERYTHING changed in the working tree (respecting .gitignore), not
# just the two known output files - so any other local edits get published
# too, not only data/articles.json and gui/unified_report.json.
git add -A

if git diff --cached --quiet; then
    echo "No changes to publish - dashboard data is already up to date."
else
    COMMIT_MSG="$COMMIT_PREFIX $(date -u '+%Y-%m-%d %H:%M UTC')"
    git commit -m "$COMMIT_MSG"

    # NOTE: under `set -e`, a plain failing command aborts the script
    # immediately at that line - `$?` on the NEXT line never runs. To catch
    # the failure without aborting, we use `command || VAR=$?`, which lets
    # `set -e` treat the failure as "handled" while still capturing the
    # real exit code.
    PUSH_STATUS=0
    git push "$GIT_REMOTE" "$GIT_BRANCH" || PUSH_STATUS=$?

    if [ "$PUSH_STATUS" -ne 0 ]; then
        echo "WARNING: push rejected (exit $PUSH_STATUS), likely remote has commits we don't. Retrying once with rebase..."
        REBASE_STATUS=0
        git pull --rebase "$GIT_REMOTE" "$GIT_BRANCH" || REBASE_STATUS=$?
        if [ "$REBASE_STATUS" -ne 0 ]; then
            echo "ERROR: rebase failed, likely a real conflict. Manual intervention required."
            echo "===== Run finished (FAILED): $(date -u '+%Y-%m-%dT%H:%M:%SZ') ====="
            exit 1
        fi
        PUSH_STATUS=0
        git push "$GIT_REMOTE" "$GIT_BRANCH" || PUSH_STATUS=$?
    fi

    if [ "$PUSH_STATUS" -ne 0 ]; then
        echo "ERROR: push failed after retry (exit $PUSH_STATUS). Dashboard was NOT updated."
        echo "===== Run finished (FAILED): $(date -u '+%Y-%m-%dT%H:%M:%SZ') ====="
        exit 1
    fi

    echo "Pushed update to $GIT_REMOTE/$GIT_BRANCH - GitHub Pages will redeploy automatically."
fi

echo "===== Run finished: $(date -u '+%Y-%m-%dT%H:%M:%SZ') ====="
} >> "$LOG_FILE" 2>&1
