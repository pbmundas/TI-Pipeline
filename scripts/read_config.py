"""
read_config.py
Reads config.yaml and prints the values daily_run.bat needs as plain
KEY=value lines (no quotes), one per line, so they can be parsed with
Windows batch's `for /f "tokens=1,* delims=="`.

Run from the repo root (daily_run.bat cd's there before calling this).
"""
import yaml

with open("config.yaml", encoding="utf-8") as f:
    cfg = yaml.safe_load(f)

paths = cfg["paths"]
git_cfg = cfg["git"]

print(f'DASHBOARD_REPO={paths["dashboard_repo"]}')
print(f'OUTPUT_REL_PATH={paths["output_relative_path"]}')
print(f'ARTICLES_REL_PATH={paths["articles_file"]}')
print(f'FINDINGS_CACHE_REL_PATH={paths.get("findings_cache_file", "data/findings_cache.json")}')
print(f'ARCHIVE_REL_PATH={paths.get("archive_relative_dir", "reports")}')
print(f'GIT_ENABLED={str(git_cfg.get("enabled", True)).lower()}')
print(f'GIT_BRANCH={git_cfg["branch"]}')
print(f'GIT_REMOTE={git_cfg["remote"]}')
print(f'COMMIT_PREFIX={git_cfg["commit_message_prefix"]}')
