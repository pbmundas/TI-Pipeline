"""
fetch_feeds.py
Pulls articles from the configured RSS feeds, normalizes them, and merges
them into a local on-disk article cache (deduplicated by URL).

No LLM calls happen here - this module is purely collection/storage.
"""
import json
import logging
import os
from datetime import datetime, timezone

import feedparser
from dateutil import parser as dateparser

log = logging.getLogger("ti_pipeline.fetch")


def _parse_date(entry):
    for key in ("published", "updated", "created"):
        val = entry.get(key)
        if val:
            try:
                dt = dateparser.parse(val)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                return dt.astimezone(timezone.utc).isoformat()
            except (ValueError, OverflowError):
                continue
    return datetime.now(timezone.utc).isoformat()


def fetch_all(feed_configs, request_timeout=20):
    """Fetch every configured feed. Returns a list of normalized article dicts.
    Failures on individual feeds are logged and skipped (a dead feed should
    not break the whole pipeline)."""
    collected = []
    for feed_cfg in feed_configs:
        url = feed_cfg["url"]
        source_type = feed_cfg.get("source_type", "")
        try:
            parsed = feedparser.parse(url)
            if parsed.bozo and not parsed.entries:
                log.warning("Feed failed to parse, skipping: %s (%s)", url, parsed.bozo_exception)
                continue
            for entry in parsed.entries:
                title = entry.get("title", "").strip()
                link = entry.get("link", "").strip()
                if not title or not link:
                    continue
                summary = entry.get("summary", "") or entry.get("description", "")
                collected.append({
                    "title": title,
                    "url": link,
                    "published_date": _parse_date(entry),
                    "source_type": source_type,
                    "summary": summary[:1200],  # keep prompts/context small
                    "feed_source": url,
                })
            log.info("Fetched %d entries from %s", len(parsed.entries), url)
        except Exception as e:  # noqa: BLE001 - a single bad feed must not kill the run
            log.warning("Error fetching feed %s: %s", url, e)
    return collected


def load_cache(path):
    if not os.path.exists(path):
        return []
    with open(path, "r", encoding="utf-8") as f:
        try:
            return json.load(f)
        except json.JSONDecodeError:
            log.warning("Article cache at %s was corrupt, starting fresh", path)
            return []


def save_cache(path, articles):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(articles, f, indent=2, ensure_ascii=False)


def merge_and_dedupe(existing, new_articles):
    """Merge new articles into existing cache, deduped by URL. Existing
    entries win (keeps first-seen published_date stable)."""
    by_url = {a["url"]: a for a in existing}
    added = 0
    for art in new_articles:
        if art["url"] not in by_url:
            by_url[art["url"]] = art
            added += 1
    merged = list(by_url.values())
    log.info("Merged cache: %d total articles (%d new)", len(merged), added)
    return merged


def collect_and_store(config):
    cache_path = config["paths"]["articles_file"]
    existing = load_cache(cache_path)
    new_articles = fetch_all(config["feeds"])
    merged = merge_and_dedupe(existing, new_articles)
    save_cache(cache_path, merged)
    return merged
