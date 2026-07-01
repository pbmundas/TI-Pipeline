"""
enrich.py
Map-reduce style enrichment of raw RSS articles into the dashboard's
unified_report.json shape, using a local Ollama model instead of OpenAI.

MAP phase:  batch articles -> ask the model to extract candidate incidents,
            vulnerabilities, actors, and hunting indicators referencing
            source IDs.
REDUCE phase: merge/dedupe extractions across batches, then ask the model
            to write the polished narrative sections (executive brief,
            threat stories, actor profiles, etc.) from the merged findings.
"""
import hashlib
import json
import logging
import os
from collections import Counter
from datetime import datetime, timezone

log = logging.getLogger("ti_pipeline.enrich")

NO_INVENT_RULE = (
    "Use ONLY the information given below - never invent CVEs, actor names, "
    "dates, or facts that are not present in it."
)

MAP_SYSTEM_PROMPT = """You are a cybersecurity threat intelligence analyst.
You will be given a numbered batch of recent security news article summaries.
Extract structured findings ONLY based on what is stated in the articles -
never invent CVEs, actor names, or facts that are not present in the text.

Respond with ONLY a JSON object (no prose, no markdown fences) matching this
exact shape:
{
  "incidents": [
    {"headline": str, "summary": str, "source_ids": [int]}
  ],
  "vulnerabilities": [
    {"cve": str, "product": str, "vendor": str, "severity": "CRITICAL|HIGH|MEDIUM|LOW",
     "cvss": number_or_null, "description": str, "exploitation_status": str,
     "remediation": str, "source_ids": [int]}
  ],
  "actors": [
    {"name": str, "aliases": [str], "motivation": str, "recent_activity": str,
     "targets": [str], "ttps": [str], "source_ids": [int]}
  ],
  "hunting_indicators": [
    {"title": str, "context": str, "indicators": [str], "source_ids": [int]}
  ],
  "targeted_industries": [str]
}
Omit a category entirely (empty list) if nothing relevant is present. Keep
text concise. "ttps" should be MITRE ATT&CK technique IDs (e.g. T1190) only
if explicitly identifiable, otherwise omit the field/leave empty.
"""

BRIEF_SYSTEM_PROMPT = f"""You are a senior cybersecurity threat intelligence
analyst. {NO_INVENT_RULE}

Respond with ONLY a JSON object: {{"executive_brief": str}}
"executive_brief" should be a 3-5 paragraph markdown summary of the most
important incidents, vulnerabilities, and actor activity for the period,
written for an executive audience.
"""

STORIES_SYSTEM_PROMPT = f"""You are a senior cybersecurity threat intelligence
analyst writing deep-dive threat stories. {NO_INVENT_RULE}

Respond with ONLY a JSON object matching this exact shape:
{{
  "threat_stories": [
    {{
      "headline": str,
      "narrative": str,
      "timeline": [{{"date": "YYYY-MM-DD", "event": str, "significance": str, "sources": [int]}}],
      "impact_assessment": str,
      "action_required": str,
      "sources": [int]
    }}
  ]
}}
Select the TOP __MAX_STORIES__ most significant, distinct incidents and
write a 2-4 paragraph narrative for each. "sources" must only contain
source_ids actually present in the incidents given to you.
"""

ACTORS_SYSTEM_PROMPT = f"""You are a cybersecurity threat intelligence
analyst profiling threat actors. {NO_INVENT_RULE}

Respond with ONLY a JSON object matching this exact shape:
{{
  "actor_profiles": [
    {{"name": str, "aliases": [str], "motivation": str, "recent_activity": str,
     "targets": [str], "ttps": [str], "sources": [int]}}
  ]
}}
Select the TOP __MAX_ACTORS__ most significant actors. "sources" must only
contain source_ids actually present in the actor data given to you.
"""

VULNS_SYSTEM_PROMPT = f"""You are a cybersecurity threat intelligence analyst
prioritizing vulnerabilities for remediation. {NO_INVENT_RULE}

Respond with ONLY a JSON object matching this exact shape:
{{
  "critical_vulnerabilities": [
    {{"cve": str, "product": str, "vendor": str, "severity": str, "cvss": number,
     "description": str, "exploitation_status": str, "remediation": str, "sources": [int]}}
  ]
}}
Select the TOP __MAX_VULNS__ vulnerabilities, favoring CRITICAL/HIGH severity
and active exploitation. "sources" must only contain source_ids actually
present in the vulnerability data given to you.
"""

HUNTING_SYSTEM_PROMPT = f"""You are a threat hunter writing detection leads
for a SOC team. {NO_INVENT_RULE}

Respond with ONLY a JSON object matching this exact shape:
{{
  "hunting_leads": [
    {{"title": str, "context": str, "query": str, "indicators": [str], "sources": [int]}}
  ]
}}
Select the TOP __MAX_HUNTS__ leads. For "query", write a plausible SIEM-style
search expression (Splunk/SPL style) reflecting the described indicators.
"sources" must only contain source_ids actually present in the data given to you.
"""

TRENDS_SYSTEM_PROMPT = f"""You are a cybersecurity threat intelligence
analyst spotting trends across a reporting period. {NO_INVENT_RULE}

Respond with ONLY a JSON object matching this exact shape:
{{
  "emerging_trends": [str],
  "declining_threats": [str],
  "key_changes": str
}}
List up to 5 emerging_trends and up to 5 declining_threats as short phrases.
"key_changes" is 1-2 sentences summarizing what shifted versus typical
activity. Base all of this only on the data given to you.
"""


def _batch(items, size):
    for i in range(0, len(items), size):
        yield items[i:i + size]


def _format_batch_for_prompt(batch):
    lines = []
    for art in batch:
        lines.append(
            f'[id={art["id"]}] "{art["title"]}" '
            f'({art["published_date"]}, {art.get("source_type") or "news"})\n'
            f'{art.get("summary", "").strip()}'
        )
    return "\n\n".join(lines)


def _new_cache_entry():
    return {
        "enriched_at": None,
        "incidents": [],
        "vulnerabilities": [],
        "actors": [],
        "hunting_indicators": [],
        "industries": [],
    }


def load_findings_cache(path):
    """Load the per-article findings cache (keyed by article URL) written
    by a previous run. Missing/corrupt cache just means "nothing enriched
    yet" - it never blocks a run."""
    if not os.path.exists(path):
        return {}
    with open(path, "r", encoding="utf-8") as f:
        try:
            return json.load(f)
        except json.JSONDecodeError:
            log.warning("Findings cache at %s was corrupt, starting fresh", path)
            return {}


def save_findings_cache(path, cache):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(cache, f, indent=2, ensure_ascii=False)


def prune_findings_cache(cache, keep_urls):
    """Drop cache entries for articles that can no longer fall inside any
    future reporting window, so the cache file doesn't grow forever as the
    article store accumulates history. `keep_urls` is the set of article
    URLs still worth retaining (caller decides the retention window)."""
    stale = [u for u in cache if u not in keep_urls]
    for u in stale:
        del cache[u]
    if stale:
        log.info("Pruned %d aged-out entries from findings cache", len(stale))
    return cache


def _item_key(item, fields):
    """Stable identity for an extracted item, used to dedupe copies that
    the caching scheme below stores under more than one source URL."""
    basis = "|".join(str(item.get(f, "")).strip().lower() for f in fields)
    return hashlib.sha1(basis.encode("utf-8")).hexdigest()


def extract_findings(client, sourced_articles, batch_size, cache=None):
    """MAP phase, now incremental: articles whose URL is already present in
    `cache` are skipped entirely (no LLM call) and their previously-stored
    findings are reused. Only articles not yet in the cache get batched and
    sent to Ollama. Returns (findings, updated_cache) - the caller is
    responsible for persisting `cache` (e.g. via save_findings_cache)."""
    if cache is None:
        cache = {}

    to_process = [a for a in sourced_articles if a["url"] not in cache]
    log.info(
        "Findings cache: %d/%d articles already enriched (reused), %d new to process via Ollama",
        len(sourced_articles) - len(to_process), len(sourced_articles), len(to_process),
    )

    batches = list(_batch(to_process, batch_size))
    for i, batch in enumerate(batches, 1):
        log.info("Extracting findings: batch %d/%d (%d new articles)", i, len(batches), len(batch))
        id_to_url = {a["id"]: a["url"] for a in batch}
        user_prompt = (
            "Articles:\n\n" + _format_batch_for_prompt(batch) +
            "\n\nExtract findings now as JSON."
        )

        # Reserve cache entries up front so a crash mid-run can't leave an
        # article half-processed and silently skipped forever.
        for a in batch:
            cache[a["url"]] = _new_cache_entry()

        try:
            result = client.chat_json(MAP_SYSTEM_PROMPT, user_prompt)
        except Exception as e:  # noqa: BLE001
            log.warning("Batch %d extraction failed, will retry next run: %s", i, e)
            for a in batch:
                del cache[a["url"]]  # not enriched - eligible for retry next run
            continue

        valid_ids = {a["id"] for a in batch}

        def _clean_ids(ids):
            return [j for j in (ids or []) if j in valid_ids]

        def _store(item, category):
            ids = _clean_ids(item.get("source_ids"))
            if not ids:
                return
            urls = sorted({id_to_url[j] for j in ids})
            entry = dict(item)
            entry.pop("source_ids", None)
            entry["source_urls"] = urls
            for u in urls:
                cache[u][category].append(entry)

        for inc in result.get("incidents", []) or []:
            _store(inc, "incidents")
        for vuln in result.get("vulnerabilities", []) or []:
            _store(vuln, "vulnerabilities")
        for actor in result.get("actors", []) or []:
            _store(actor, "actors")
        for hunt in result.get("hunting_indicators", []) or []:
            _store(hunt, "hunting_indicators")

        industries = result.get("targeted_industries", []) or []
        now = datetime.now(timezone.utc).isoformat()
        for u in id_to_url.values():
            cache[u]["industries"].extend(industries)
            cache[u]["enriched_at"] = now

    return _assemble_from_cache(sourced_articles, cache), cache


def _assemble_from_cache(sourced_articles, cache):
    """Rebuild the merged findings dict for the *current* reporting window
    purely by reading cached per-article results - no LLM calls happen
    here, regardless of whether those results came from this run or a
    previous one."""
    url_to_id = {a["url"]: a["id"] for a in sourced_articles}
    window_urls = set(url_to_id)

    all_incidents, all_vulns, all_actors, all_hunts, industries = [], [], [], [], []
    seen_incidents, seen_hunts = set(), set()

    def _resolve_ids(urls):
        return sorted({url_to_id[u] for u in urls if u in url_to_id})

    for url in window_urls:
        entry = cache.get(url)
        if not entry:
            continue

        for inc in entry.get("incidents", []):
            key = _item_key(inc, ("headline", "summary"))
            if key in seen_incidents:
                continue
            ids = _resolve_ids(inc.get("source_urls", []))
            if not ids:
                continue
            seen_incidents.add(key)
            clean = {k: v for k, v in inc.items() if k != "source_urls"}
            clean["source_ids"] = ids
            all_incidents.append(clean)

        for vuln in entry.get("vulnerabilities", []):
            ids = _resolve_ids(vuln.get("source_urls", []))
            if not ids:
                continue
            clean = {k: v for k, v in vuln.items() if k != "source_urls"}
            clean["source_ids"] = ids
            all_vulns.append(clean)

        for actor in entry.get("actors", []):
            ids = _resolve_ids(actor.get("source_urls", []))
            if not ids:
                continue
            clean = {k: v for k, v in actor.items() if k != "source_urls"}
            clean["source_ids"] = ids
            all_actors.append(clean)

        for hunt in entry.get("hunting_indicators", []):
            key = _item_key(hunt, ("title", "context"))
            if key in seen_hunts:
                continue
            ids = _resolve_ids(hunt.get("source_urls", []))
            if not ids:
                continue
            seen_hunts.add(key)
            clean = {k: v for k, v in hunt.items() if k != "source_urls"}
            clean["source_ids"] = ids
            all_hunts.append(clean)

        industries.extend(entry.get("industries", []))

    return {
        "incidents": all_incidents,
        "vulnerabilities": _merge_vulns(all_vulns),
        "actors": _merge_actors(all_actors),
        "hunting_indicators": all_hunts,
        "industries": industries,
    }


def _merge_vulns(vulns):
    merged = {}
    for v in vulns:
        key = (v.get("cve") or v.get("product") or "").upper().strip()
        if not key:
            continue
        if key not in merged:
            merged[key] = v
        else:
            merged[key]["source_ids"] = sorted(set(merged[key]["source_ids"]) | set(v["source_ids"]))
    return list(merged.values())


def _merge_actors(actors):
    merged = {}
    for a in actors:
        key = (a.get("name") or "").strip().lower()
        if not key:
            continue
        if key not in merged:
            merged[key] = a
        else:
            merged[key]["source_ids"] = sorted(set(merged[key]["source_ids"]) | set(a["source_ids"]))
            merged[key]["aliases"] = sorted(set(merged[key].get("aliases") or []) | set(a.get("aliases") or []))
            merged[key]["targets"] = sorted(set(merged[key].get("targets") or []) | set(a.get("targets") or []))
    return list(merged.values())


def _cap(items, n):
    """Send at most n items into a synthesis prompt - keeps input context
    (and therefore latency) bounded even if a busy week produces a lot of
    raw findings."""
    return items[:n] if items else items


def _call_section(client, label, system_prompt, payload, result_key, default):
    """Run one focused REDUCE sub-call. Never lets a single section's
    failure kill the whole report - falls back to `default` and logs."""
    log.info("Synthesizing section '%s' from %d input item(s)", label, len(payload))
    if not payload:
        return default
    user_prompt = f"Data (JSON):\n\n{payload}\n\nWrite the JSON now."
    try:
        result = client.chat_json(system_prompt, user_prompt)
        return result.get(result_key, default)
    except Exception as e:  # noqa: BLE001
        log.warning("Section '%s' synthesis failed, using fallback: %s", label, e)
        return default


def _call_trends(client, payload):
    """Trends prompt returns top-level keys directly (no wrapper key), so it
    gets its own small helper rather than reusing _call_section."""
    default = {"emerging_trends": [], "declining_threats": [], "key_changes": ""}
    if not payload.get("incidents") and not payload.get("vulnerabilities"):
        return default
    log.info("Synthesizing section 'trends'")
    user_prompt = f"Data (JSON):\n\n{payload}\n\nWrite the JSON now."
    try:
        result = client.chat_json(TRENDS_SYSTEM_PROMPT, user_prompt)
        return {
            "emerging_trends": result.get("emerging_trends", []),
            "declining_threats": result.get("declining_threats", []),
            "key_changes": result.get("key_changes", ""),
        }
    except Exception as e:  # noqa: BLE001
        log.warning("Section 'trends' synthesis failed, using fallback: %s", e)
        return default


def synthesize_report(client, findings, report_cfg):
    """REDUCE phase: turn merged findings into the final narrative sections,
    via several small calls rather than one large one."""
    incidents = _cap(findings["incidents"], max(20, report_cfg["max_threat_stories"] * 4))
    vulns = _cap(findings["vulnerabilities"], max(20, report_cfg["max_critical_vulnerabilities"] * 3))
    actors = _cap(findings["actors"], max(15, report_cfg["max_actor_profiles"] * 3))
    hunts = _cap(findings["hunting_indicators"], max(15, report_cfg["max_hunting_leads"] * 3))

    stories_prompt = STORIES_SYSTEM_PROMPT.replace("__MAX_STORIES__", str(report_cfg["max_threat_stories"]))
    actors_prompt = ACTORS_SYSTEM_PROMPT.replace("__MAX_ACTORS__", str(report_cfg["max_actor_profiles"]))
    vulns_prompt = VULNS_SYSTEM_PROMPT.replace("__MAX_VULNS__", str(report_cfg["max_critical_vulnerabilities"]))
    hunts_prompt = HUNTING_SYSTEM_PROMPT.replace("__MAX_HUNTS__", str(report_cfg["max_hunting_leads"]))

    executive_brief = _call_section(
        client, "executive_brief", BRIEF_SYSTEM_PROMPT,
        {"incidents": incidents[:15], "vulnerabilities": vulns[:10], "actors": actors[:10]},
        "executive_brief", "",
    )
    threat_stories = _call_section(
        client, "threat_stories", stories_prompt, incidents, "threat_stories", [],
    )
    actor_profiles = _call_section(
        client, "actor_profiles", actors_prompt, actors, "actor_profiles", [],
    )
    critical_vulnerabilities = _call_section(
        client, "critical_vulnerabilities", vulns_prompt, vulns, "critical_vulnerabilities", [],
    )
    hunting_leads = _call_section(
        client, "hunting_leads", hunts_prompt, hunts, "hunting_leads", [],
    )
    trends = _call_trends(client, {"incidents": incidents[:15], "vulnerabilities": vulns[:10]})

    return {
        "executive_brief": executive_brief if isinstance(executive_brief, str) else "",
        "threat_stories": threat_stories,
        "actor_profiles": actor_profiles,
        "critical_vulnerabilities": critical_vulnerabilities,
        "hunting_leads": hunting_leads,
        "statistics": {
            "emerging_trends": trends.get("emerging_trends", []),
            "declining_threats": trends.get("declining_threats", []),
            "key_changes": trends.get("key_changes", ""),
        },
    }


def compute_actor_industry_counts(findings):
    """Fallback statistics computed directly from extracted data, used if
    the model's own statistics section is missing/incomplete."""
    actor_counts = Counter(a["name"] for a in findings["actors"] if a.get("name"))
    industry_counts = Counter(i for i in findings["industries"] if i)
    return (
        actor_counts.most_common(10),
        industry_counts.most_common(10),
    )