"""
fetch_feeds.py

Fetches articles from configured RSS/Atom feeds, normalizes them, and merges
them into a local on-disk article cache (deduplicated by URL).

Features:
- Browser-like HTTP headers
- Requests session with retries
- Concurrent feed downloads
- Handles malformed feeds gracefully
- Skips HTML/error pages
- Continues when individual feeds fail
"""

import json
import logging
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

import feedparser
import requests
from dateutil import parser as dateparser
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

log = logging.getLogger("ti_pipeline.fetch")


# ----------------------------------------------------------------------
# HTTP Session
# ----------------------------------------------------------------------

def _create_session():
    session = requests.Session()

    retries = Retry(
        total=3,
        connect=3,
        read=3,
        backoff_factor=1,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET"],
    )

    adapter = HTTPAdapter(max_retries=retries)

    session.mount("http://", adapter)
    session.mount("https://", adapter)

    session.headers.update(
        {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/137.0 Safari/537.36"
            ),
            "Accept": (
                "application/rss+xml,"
                "application/atom+xml,"
                "application/xml,"
                "text/xml;q=0.9,*/*;q=0.8"
            ),
            "Accept-Encoding": "gzip, deflate, br",
            "Accept-Language": "en-US,en;q=0.9",
            "Connection": "keep-alive",
        }
    )

    return session


SESSION = _create_session()


# ----------------------------------------------------------------------
# Date parsing
# ----------------------------------------------------------------------

def _parse_date(entry):
    for key in ("published", "updated", "created"):
        value = entry.get(key)

        if not value:
            continue

        try:
            dt = dateparser.parse(value)

            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)

            return dt.astimezone(timezone.utc).isoformat()

        except Exception:
            pass

    return datetime.now(timezone.utc).isoformat()


# ----------------------------------------------------------------------
# Fetch one feed
# ----------------------------------------------------------------------

def _fetch_feed(feed_cfg, timeout):

    url = feed_cfg["url"]
    source_type = feed_cfg.get("source_type", "")
    source_note = feed_cfg.get("note", "")

    articles = []

    try:
        response = SESSION.get(
            url,
            timeout=timeout,
            allow_redirects=True,
        )

        response.raise_for_status()

        content_type = response.headers.get("Content-Type", "").lower()

        # Reject obvious HTML pages (Cloudflare/login pages)
        if (
            "text/html" in content_type
            and "xml" not in content_type
            and "rss" not in content_type
            and "atom" not in content_type
        ):
            log.warning(
                "Feed returned HTML instead of RSS: %s (%s)",
                url,
                content_type,
            )
            return articles

        parsed = feedparser.parse(response.content)
        publisher = (parsed.feed.get("title") or feed_cfg.get("publisher") or "").strip()

        if parsed.bozo:
            log.debug(
                "Bozo feed %s: %s",
                url,
                parsed.bozo_exception,
            )

        if not parsed.entries:
            log.warning("No entries found in %s", url)
            return articles

        for entry in parsed.entries:

            title = entry.get("title", "").strip()
            link = entry.get("link", "").strip()

            if not title or not link:
                continue

            summary = (
                entry.get("summary")
                or entry.get("description")
                or ""
            )

            articles.append(
                {
                    "title": title,
                    "url": link,
                    "published_date": _parse_date(entry),
                    "source_type": source_type,
                    "summary": summary[:1200],
                    "feed_source": url,
                    "publisher": publisher,
                    "source_note": source_note,
                }
            )

        log.info("Fetched %d entries from %s", len(articles), url)

    except requests.exceptions.RequestException as e:
        log.warning("Network error for %s: %s", url, e)

    except Exception as e:
        log.warning("Feed failed to parse, skipping: %s (%s)", url, e)

    return articles


# ----------------------------------------------------------------------
# Fetch all feeds
# ----------------------------------------------------------------------

def fetch_all(feed_configs, request_timeout=20, max_workers=10):
    """
    Fetch all configured feeds concurrently.

    Returns:
        list[dict]: normalized articles
    """

    collected = []

    with ThreadPoolExecutor(max_workers=max_workers) as executor:

        futures = [
            executor.submit(
                _fetch_feed,
                feed,
                request_timeout,
            )
            for feed in feed_configs
        ]

        for future in as_completed(futures):
            try:
                collected.extend(future.result())
            except Exception as e:
                log.warning("Worker failed: %s", e)

    return collected


# ----------------------------------------------------------------------
# Cache handling
# ----------------------------------------------------------------------

def load_cache(path):

    if not os.path.exists(path):
        return []

    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)

    except json.JSONDecodeError:
        log.warning(
            "Article cache at %s was corrupt, starting fresh",
            path,
        )
        return []


def save_cache(path, articles):

    os.makedirs(os.path.dirname(path), exist_ok=True)

    with open(path, "w", encoding="utf-8") as f:
        json.dump(
            articles,
            f,
            indent=2,
            ensure_ascii=False,
        )


# ----------------------------------------------------------------------
# Deduplication
# ----------------------------------------------------------------------

def merge_and_dedupe(existing, new_articles):
    """
    Merge new articles into cache.
    Deduplicates by article URL.
    """

    by_url = {article["url"]: article for article in existing}

    added = 0

    for article in new_articles:

        if article["url"] not in by_url:
            by_url[article["url"]] = article
            added += 1

    merged = list(by_url.values())

    log.info(
        "Merged cache: %d total articles (%d new)",
        len(merged),
        added,
    )

    return merged


# ----------------------------------------------------------------------
# Main collection entrypoint
# ----------------------------------------------------------------------

def collect_and_store(config):

    cache_path = config["paths"]["articles_file"]

    existing = load_cache(cache_path)

    new_articles = fetch_all(
        config["feeds"],
        request_timeout=20,
        max_workers=10,
    )

    merged = merge_and_dedupe(
        existing,
        new_articles,
    )

    save_cache(
        cache_path,
        merged,
    )

    return merged
