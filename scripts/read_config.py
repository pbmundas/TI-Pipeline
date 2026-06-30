import yaml

cfg = yaml.safe_load(open("config.yaml"))
print(f'DASHBOARD_REPO={cfg["paths"]["dashboard_repo"]}')
print(f'OUTPUT_REL_PATH={cfg["paths"]["output_relative_path"]}')
print(f'ARTICLES_REL_PATH={cfg["paths"]["articles_file"]}')
print(f'GIT_ENABLED={str(cfg["git"].get("enabled", True)).lower()}')
print(f'GIT_BRANCH={cfg["git"]["branch"]}')
print(f'GIT_REMOTE={cfg["git"]["remote"]}')
print(f'COMMIT_PREFIX={cfg["git"]["commit_message_prefix"]}')