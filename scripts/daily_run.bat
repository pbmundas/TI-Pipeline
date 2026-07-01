@echo off
setlocal enabledelayedexpansion

:: Daily pipeline run:
::   1) fetch RSS -> enrich via local Ollama -> write unified_report.json
::   2) push the updated file(s) to the GitHub Pages repo that serves the dashboard
::
:: Intended to be called from Windows Task Scheduler.
:: Logs to data\pipeline.log.

:: SCRIPT_DIR is the parent of the folder containing this script (mirrors "../" in original)
set "SCRIPT_DIR=%~dp0.."
for %%I in ("%SCRIPT_DIR%") do set "SCRIPT_DIR=%%~fI"
cd /d "%SCRIPT_DIR%"

set "LOG_FILE=%SCRIPT_DIR%\data\pipeline.log"
if not exist "%SCRIPT_DIR%\data" mkdir "%SCRIPT_DIR%\data"

:: Activate venv if present
if exist "%SCRIPT_DIR%\.venv\Scripts\activate.bat" (
    call "%SCRIPT_DIR%\.venv\Scripts\activate.bat"
)

:: Get current UTC timestamp helper (calls PowerShell, since native batch has no UTC support)
for /f %%T in ('powershell -NoProfile -Command "[DateTime]::UtcNow.ToString(\"yyyy-MM-ddTHH:mm:ssZ\")"') do set "START_TS=%%T"

(
echo ===== Run started: %START_TS% =====

:: ---------------------------------------------------------------------------
:: 0. Preflight: Ollama must be up before we burn time on RSS fetches.
:: ---------------------------------------------------------------------------
curl -fsS --max-time 5 "http://localhost:11434/api/tags" >nul 2>&1
if errorlevel 1 (
    echo ERROR: Ollama is not reachable on localhost:11434. Aborting.
    exit /b 1
)

:: ---------------------------------------------------------------------------
:: 1. Sync the dashboard repo first, so we never generate the new file on top
::    of a stale working tree.
:: ---------------------------------------------------------------------------
for /f "usebackq tokens=1,* delims==" %%A in (`python "%~dp0read_config.py"`) do (
    set "%%A=%%B"
)

if /i "!GIT_ENABLED!"=="true" (
    if not exist "!DASHBOARD_REPO!\.git" (
        echo ERROR: !DASHBOARD_REPO! is not a git repo. Clone it first, e.g.:
        echo   git clone https://github.com/^<you^>/^<your-pages-repo^>.git "!DASHBOARD_REPO!"
        exit /b 1
    )
    echo --- Step 1: syncing dashboard repo ^(!DASHBOARD_REPO!^) ---
    pushd "!DASHBOARD_REPO!"

    :: If there are any local changes already sitting in the working tree
    :: leftover generated output from a previous run that never got
    :: pushed, or manual edits - commit them FIRST so `git pull --rebase`
    :: never fails with unstaged changes. This script is meant to run
    :: unattended - it should never need a human to fix git state first.
    git add -A
    git diff --cached --quiet
    if errorlevel 1 (
        echo Found pre-existing local changes - committing them before syncing...
        for /f %%T in ('powershell -NoProfile -Command "[DateTime]::UtcNow.ToString(\"yyyy-MM-dd HH:mm\")"') do set "PRESYNC_TS=%%T UTC"
        git commit -m "!COMMIT_PREFIX! (pre-sync) !PRESYNC_TS!"
        if errorlevel 1 (
            echo ERROR: pre-sync commit failed. Dashboard was NOT updated.
            popd
            exit /b 1
        )
    )

    git pull --rebase "!GIT_REMOTE!" "!GIT_BRANCH!"
    if errorlevel 1 (
        echo ERROR: initial pull --rebase failed after pre-sync commit. This likely means a real conflict - manual intervention required.
        popd
        exit /b 1
    )
    popd
)

:: ---------------------------------------------------------------------------
:: 2. DATA GENERATION: fetch feeds, run local LLM enrichment, write the JSON.
:: ---------------------------------------------------------------------------
echo --- Step 2: generating data ---
python -m src.pipeline --config config.yaml --no-publish
if errorlevel 1 (
    echo ERROR: pipeline run failed. Dashboard was NOT updated.
    exit /b 1
)

if /i not "!GIT_ENABLED!"=="true" (
    echo Git publishing disabled in config.yaml ^(git.enabled: false^). Skipping push.
    for /f %%T in ('powershell -NoProfile -Command "[DateTime]::UtcNow.ToString(\"yyyy-MM-ddTHH:mm:ssZ\")"') do set "END_TS=%%T"
    echo ===== Run finished: !END_TS! =====
    exit /b 0
)

:: ---------------------------------------------------------------------------
:: 3. GITHUB PUSH: commit the freshly generated files and push to the
::    GitHub Pages repo so the live dashboard picks it up.
::
::    IMPORTANT: `git push` failures are checked EXPLICITLY below via
::    `if errorlevel 1` immediately after the command, rather than assuming
::    a non-zero exit silently stops the script. If the remote has commits
::    this local clone doesn't have yet - e.g. from a manual push, or another
::    scheduled run - the push is rejected - we now detect that, pull
::    --rebase, and retry once automatically instead of reporting a false
::    "Pushed update" success.
:: ---------------------------------------------------------------------------
echo --- Step 3: pushing to GitHub Pages repo ^(!DASHBOARD_REPO!^) ---
cd /d "!DASHBOARD_REPO!"

:: Stage EVERYTHING changed in the working tree ^(respecting .gitignore^),
:: not just the two known output files - so any other local edits get
:: published too, not only data\articles.json and gui\unified_report.json.
git add -A

git diff --cached --quiet
if errorlevel 1 (
    for /f %%T in ('powershell -NoProfile -Command "[DateTime]::UtcNow.ToString(\"yyyy-MM-dd HH:mm\")"') do set "COMMIT_TS=%%T UTC"
    set "COMMIT_MSG=!COMMIT_PREFIX! !COMMIT_TS!"
    git commit -m "!COMMIT_MSG!"
    if errorlevel 1 (
        echo ERROR: git commit failed. Dashboard was NOT updated.
        exit /b 1
    )

    git push "!GIT_REMOTE!" "!GIT_BRANCH!"
    if errorlevel 1 (
        echo WARNING: push rejected ^(remote likely has commits we do not have locally^). Retrying once with rebase...
        git pull --rebase "!GIT_REMOTE!" "!GIT_BRANCH!"
        if errorlevel 1 (
            echo ERROR: rebase failed, likely a real conflict. Manual intervention required.
            echo   cd "!DASHBOARD_REPO!"
            echo   git status
            for /f %%T in ('powershell -NoProfile -Command "[DateTime]::UtcNow.ToString(\"yyyy-MM-ddTHH:mm:ssZ\")"') do set "END_TS=%%T"
            echo ===== Run finished ^(FAILED^): !END_TS! =====
            exit /b 1
        )
        git push "!GIT_REMOTE!" "!GIT_BRANCH!"
        if errorlevel 1 (
            echo ERROR: push failed even after retry. Dashboard was NOT updated.
            for /f %%T in ('powershell -NoProfile -Command "[DateTime]::UtcNow.ToString(\"yyyy-MM-ddTHH:mm:ssZ\")"') do set "END_TS=%%T"
            echo ===== Run finished ^(FAILED^): !END_TS! =====
            exit /b 1
        )
        echo Pushed update to !GIT_REMOTE!/!GIT_BRANCH! - GitHub Pages will redeploy automatically. ^(after rebase retry^)
    ) else (
        echo Pushed update to !GIT_REMOTE!/!GIT_BRANCH! - GitHub Pages will redeploy automatically.
    )
) else (
    echo No changes to publish - dashboard data is already up to date.
)

for /f %%T in ('powershell -NoProfile -Command "[DateTime]::UtcNow.ToString(\"yyyy-MM-ddTHH:mm:ssZ\")"') do set "END_TS=%%T"
echo ===== Run finished: !END_TS! =====

) >> "%LOG_FILE%" 2>&1

endlocal
