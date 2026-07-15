# TI Pipeline (Ollama edition)

Generates the current `gui/unified_report.json` file plus date-stamped daily
snapshots under `gui/reports/`, consumed by
[tidashboar.github.io](https://github.com/JouniMi/tidashboar.github.io),
using **local Ollama + qwen2.5:7b** instead of a hosted OpenAI API.

This repo is the *pipeline* (RSS collection + LLM enrichment). It writes
into, commits, and pushes to a separate clone of the dashboard repo - it
does not contain the dashboard frontend itself.

## How it works

```
RSS feeds  -->  fetch_feeds.py  -->  local article cache (data/articles.json)
                                            |
                                            v
                          enrich.py (MAP)  -- per-batch extraction via Ollama
                                            |
                                            v
                          enrich.py (REDUCE) -- final report synthesis via Ollama
                                            |
                                            v
                  pipeline.py writes <dashboard_repo>/gui/unified_report.json
                                            |
                                            v
                         git add / commit / push  (scripts/daily_run.sh)
```

The output JSON matches the exact shape the dashboard's Vue app expects:
`metadata`, `sources`, `executive_brief`, `threat_stories`, `actor_profiles`,
`critical_vulnerabilities`, `hunting_leads`, `statistics`. Every successful
run also updates `gui/reports/index.json`; the dashboard uses it to render a
blog-style Archive tab beside Statistics. Re-running on the same UTC date
updates that day's entry, while later dates create new permanent reports.

## 1. Prerequisites

- Python 3.10+
- [Ollama](https://ollama.com) installed and running locally
- Git, with push access (SSH key or token) to your dashboard repo

```bash
# install & start Ollama (if not already running)
ollama serve &

# pull the model
ollama pull qwen2.5:7b
```

## 2. Setup

```bash
# Clone this pipeline repo
git clone <this-repo-url> ti-pipeline
cd ti-pipeline

# Clone the dashboard repo as a SIBLING directory (default config expects this)
git clone https://github.com/JouniMi/tidashboar.github.io.git ../tidashboar.github.io

python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Edit `config.yaml`:
- `paths.dashboard_repo` - path to your cloned dashboard repo (default `../tidashboar.github.io`)
- `feeds` - add/remove RSS sources
- `ollama.model` - change if you use a different local model/tag
- `report.time_period_days` - reporting window (default 7 days)
- `paths.archive_relative_dir` - daily snapshot/index directory (default `gui/reports`)

Make sure `git push` works non-interactively from that dashboard repo clone
(SSH key with no passphrase prompt, or a credential helper / PAT configured
via `git config credential.helper`).

## 3. Run it once manually

```bash
python3 -m src.pipeline --config config.yaml
```

Add `--no-publish` to generate the JSON without committing/pushing, useful
the first time so you can eyeball `../tidashboar.github.io/gui/unified_report.json`
before it goes live.

## 4. Automate the daily push

`scripts/daily_run.sh` now does three explicit things, in order:
1. **Sync** - `git pull --rebase` in your dashboard repo clone (avoids drift if you ever edit it by hand)
2. **Generate** - runs the pipeline with `--no-publish`, writing the new `gui/unified_report.json`
3. **Push** - `git add` / `git commit` / `git push` the file to your GitHub Pages repo, so the live site picks it up

```bash
chmod +x scripts/daily_run.sh scripts/setup_cron.sh
./scripts/setup_cron.sh 6 30   # runs every day at 06:30 local time
```

This installs a crontab entry calling `scripts/daily_run.sh`, which logs
everything (including each git command's output) to `data/pipeline.log`.

**Push must work non-interactively** (cron has no terminal to type a
password into). Set this up once in your dashboard repo clone:
- **SSH remote + passphrase-less key** (recommended):
  `git remote set-url origin git@github.com:<you>/<your-pages-repo>.git`,
  with that key loaded in `ssh-agent` or added without a passphrase, or
- **HTTPS + a stored credential**: `git remote set-url origin https://github.com/<you>/<your-pages-repo>.git`
  then `git config credential.helper store` and do one manual `git push`
  to cache a Personal Access Token (repo scope) - cron will reuse it.

To remove the cron job later: `crontab -e` and delete the line tagged
`# ti-pipeline-daily`.

### Alternative: systemd timer (if you'd rather not use cron)
```ini
# /etc/systemd/system/ti-pipeline.service
[Service]
Type=oneshot
WorkingDirectory=/path/to/ti-pipeline
ExecStart=/path/to/ti-pipeline/scripts/daily_run.sh

# /etc/systemd/system/ti-pipeline.timer
[Timer]
OnCalendar=*-*-* 06:30:00
Persistent=true
[Install]
WantedBy=timers.target
```
Enable with `systemctl enable --now ti-pipeline.timer`.

## Notes / tuning

- **7b model + many articles**: the MAP phase batches articles
  (`ollama.batch_size`, default 6) so qwen2.5:7b's context isn't overloaded
  and JSON output stays reliable. Lower it further if you see JSON parse
  retries in the logs; raise it if your feeds are low-volume.
- **Reliability over speed**: `ollama_client.py` requests `format: json` and
  retries with regex-based JSON recovery on malformed output - a 7B local
  model is less consistent at strict JSON than GPT-4-class hosted models, so
  this matters more here than it would have with the original OpenAI setup.
- **No invented facts**: both prompts explicitly instruct the model to only
  use information present in the source articles. Treat the output the same
  way the dashboard's own README does - as AI-enriched OSINT to verify
  against primary sources, not ground truth.
- **Swapping models/providers later**: only `src/ollama_client.py` talks to
  the model. Point it at a different host, or replace it with an OpenAI
  client implementing the same `chat_json()` interface, and nothing else in
  the pipeline needs to change.
