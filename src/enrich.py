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
import logging
from collections import Counter

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


def extract_findings(client, sourced_articles, batch_size):
    """MAP phase: run extraction over every batch, return merged raw findings."""
    all_incidents, all_vulns, all_actors, all_hunts = [], [], [], []
    industries = []

    batches = list(_batch(sourced_articles, batch_size))
    for i, batch in enumerate(batches, 1):
        log.info("Extracting findings: batch %d/%d (%d articles)", i, len(batches), len(batch))
        user_prompt = (
            "Articles:\n\n" + _format_batch_for_prompt(batch) +
            "\n\nExtract findings now as JSON."
        )
        try:
            result = client.chat_json(MAP_SYSTEM_PROMPT, user_prompt)
        except Exception as e:  # noqa: BLE001
            log.warning("Batch %d extraction failed, skipping: %s", i, e)
            continue

        valid_ids = {a["id"] for a in batch}

        def _clean_ids(ids):
            return [i for i in (ids or []) if i in valid_ids]

        for inc in result.get("incidents", []) or []:
            inc["source_ids"] = _clean_ids(inc.get("source_ids"))
            if inc["source_ids"]:
                all_incidents.append(inc)
        for vuln in result.get("vulnerabilities", []) or []:
            vuln["source_ids"] = _clean_ids(vuln.get("source_ids"))
            if vuln["source_ids"]:
                all_vulns.append(vuln)
        for actor in result.get("actors", []) or []:
            actor["source_ids"] = _clean_ids(actor.get("source_ids"))
            if actor["source_ids"]:
                all_actors.append(actor)
        for hunt in result.get("hunting_indicators", []) or []:
            hunt["source_ids"] = _clean_ids(hunt.get("source_ids"))
            if hunt["source_ids"]:
                all_hunts.append(hunt)
        industries.extend(result.get("targeted_industries", []) or [])

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