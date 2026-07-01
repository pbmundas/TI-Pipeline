"""
pipeline.py
Orchestrates: fetch RSS -> filter to reporting window -> assign source IDs
-> map/reduce LLM enrichment via Ollama -> write gui/unified_report.json
-> (optionally) git commit + push.

Run directly:  python -m src.pipeline
"""
import argparse
import json
import logging
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone

import yaml

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from src import fetch_feeds, enrich  # noqa: E402
from src.ollama_client import OllamaClient  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("ti_pipeline")


def load_config(path="config.yaml"):
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def within_window(article, days):
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    try:
        pub = datetime.fromisoformat(article["published_date"])
    except (ValueError, TypeError):
        return False
    return pub >= cutoff


def assign_source_ids(articles):
    sourced = []
    for idx, art in enumerate(articles, start=1):
        a = dict(art)
        a["id"] = idx
        sourced.append(a)
    return sourced


def build_sources_section(sourced_articles):
    return [
        {
            "id": a["id"],
            "title": a["title"],
            "url": a["url"],
            "published_date": a["published_date"],
            "source_type": a.get("source_type", ""),
        }
        for a in sourced_articles
    ]


def run_pipeline(config):
    report_cfg = config["report"]

    # 1. Collect
    all_articles = fetch_feeds.collect_and_store(config)

    # 2. Filter to reporting window
    recent = [a for a in all_articles if within_window(a, report_cfg["time_period_days"])]
    if not recent:
        log.warning("No articles found in the last %d days - report will be sparse.",
                    report_cfg["time_period_days"])
    sourced = assign_source_ids(recent)
    log.info("Building report from %d articles (of %d cached total)", len(sourced), len(all_articles))

    # 3. LLM enrichment via local Ollama, incrementally - articles already
    #    enriched in a previous run are read back from data/findings_cache.json
    #    instead of being re-sent to the model, so daily runs only pay for
    #    genuinely new articles.
    findings_cache_path = config["paths"].get("findings_cache_file", "data/findings_cache.json")
    findings_cache = enrich.load_findings_cache(findings_cache_path)

    client = OllamaClient(
        host=config["ollama"]["host"],
        model=config["ollama"]["model"],
        temperature=config["ollama"].get("temperature", 0.2),
        timeout=config["ollama"].get("request_timeout_seconds", 300),
    )
    findings, findings_cache = enrich.extract_findings(
        client, sourced, config["ollama"]["batch_size"], cache=findings_cache
    )

    # Bound the cache's growth: only keep entries for articles that could
    # still land inside a *future* reporting window, then persist right
    # away so this progress survives even if synthesis below fails.
    retention_days = report_cfg.get("findings_cache_retention_days", report_cfg["time_period_days"] * 2)
    keep_urls = {a["url"] for a in all_articles if within_window(a, retention_days)}
    enrich.prune_findings_cache(findings_cache, keep_urls)
    enrich.save_findings_cache(findings_cache_path, findings_cache)

    synthesized = enrich.synthesize_report(client, findings, report_cfg)

    # 4. Fallback statistics if the model under-delivers
    stats = synthesized.get("statistics") or {}
    if not stats.get("top_actors") or not stats.get("top_targeted_industries"):
        actor_counts, industry_counts = enrich.compute_actor_industry_counts(findings)
        stats.setdefault("top_actors", [list(t) for t in actor_counts])
        stats.setdefault("top_targeted_industries", [list(t) for t in industry_counts])
        stats.setdefault("emerging_trends", [])
        stats.setdefault("declining_threats", [])
        stats.setdefault("key_changes", "")
    stats["documents_analyzed"] = len(sourced)
    stats["time_period_days"] = report_cfg["time_period_days"]

    # 5. Assemble final unified_report.json
    report = {
        "metadata": {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "report_version": "1.0",
            "format": "unified_narrative",
            "reading_time_minutes": max(3, round(len(synthesized.get("executive_brief", "")) / 800)),
            "documents_analyzed": len(sourced),
            "time_period_days": report_cfg["time_period_days"],
            "generated_by": f"ollama:{config['ollama']['model']}",
        },
        "sources": build_sources_section(sourced),
        "executive_brief": synthesized.get("executive_brief", ""),
        "threat_stories": synthesized.get("threat_stories", [])[:report_cfg["max_threat_stories"]],
        "actor_profiles": synthesized.get("actor_profiles", [])[:report_cfg["max_actor_profiles"]],
        "critical_vulnerabilities": synthesized.get("critical_vulnerabilities", [])[:report_cfg["max_critical_vulnerabilities"]],
        "hunting_leads": synthesized.get("hunting_leads", [])[:report_cfg["max_hunting_leads"]],
        "statistics": stats,
    }
    return report


def write_report(report, config):
    dashboard_repo = config["paths"]["dashboard_repo"]
    rel_path = config["paths"]["output_relative_path"]
    out_path = os.path.join(dashboard_repo, rel_path)
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, ensure_ascii=False)
    log.info("Wrote report to %s", out_path)
    return out_path


def git_publish(config):
    if not config["git"].get("enabled", True):
        log.info("Git publishing disabled in config, skipping.")
        return
    repo_dir = config["paths"]["dashboard_repo"]
    branch = config["git"]["branch"]
    remote = config["git"]["remote"]
    rel_path = config["paths"]["output_relative_path"]
    msg = f"{config['git']['commit_message_prefix']} {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}"

    def run(cmd):
        log.info("$ %s", " ".join(cmd))
        subprocess.run(cmd, cwd=repo_dir, check=True)

    run(["git", "add", rel_path])
    status = subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=repo_dir)
    if status.returncode == 0:
        log.info("No changes to commit, dashboard already up to date.")
        return
    run(["git", "commit", "-m", msg])
    run(["git", "push", remote, branch])
    log.info("Pushed update to %s/%s", remote, branch)


def main():
    parser = argparse.ArgumentParser(description="Run the TI dashboard pipeline")
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--no-publish", action="store_true", help="Write the file but skip git commit/push")
    args = parser.parse_args()

    config = load_config(args.config)
    report = run_pipeline(config)
    write_report(report, config)
    if not args.no_publish:
        git_publish(config)
    log.info("Done.")


if __name__ == "__main__":
    main()
