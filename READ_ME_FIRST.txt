HOW TO APPLY THIS FIX
======================

1. In your LOCAL clone of TI-Pipeline (not the GitHub web UI):

   cd /path/to/TI-Pipeline

2. Delete the stray zip that got committed by mistake:

   git rm files.zip

3. Copy these 3 files from this package into your repo, overwriting
   the existing ones at the same paths:

     .gitignore              -> repo root
     config.yaml              -> repo root
     data/articles.json       -> repo root /data/ (create the data/ folder if needed)

4. Force-add articles.json since git previously had it ignored:

   git add -f data/articles.json .gitignore config.yaml
   git add -u   (stages the files.zip deletion)

5. Commit and push:

   git commit -m "Fix repo: remove stray zip, publish data/articles.json, fix config paths"
   git push origin main

After this push, https://raw.githubusercontent.com/pbmundas/TI-Pipeline/main/data/articles.json
should return real JSON (not 404), and the dashboard's Sources tab / metrics
strip will populate.

NOTE: index.html, static/js/app.js, static/css/dashboard.css, and
scripts/daily_run.sh in your repo are ALREADY correct - no changes needed
to those, they are not included in this package.

------------------------------------------------------------------
TO POPULATE THE AI ANALYSIS TABS (Stories/Actors/Vulns/Hunting/Stats)
------------------------------------------------------------------
This requires your local Ollama instance - it cannot be done from here.
Once the above is pushed and confirmed working:

   cd /path/to/TI-Pipeline
   ollama serve &                      # if not already running
   ollama pull qwen2.5:7b              # if not already pulled
   python3 -m src.pipeline --config config.yaml --no-publish

This writes gui/unified_report.json with real executive_brief,
threat_stories, actor_profiles, critical_vulnerabilities, hunting_leads,
and statistics, extracted ONLY from your actual articles (the prompts
explicitly forbid inventing facts). Then:

   git add gui/unified_report.json data/articles.json
   git commit -m "Publish AI-enriched report"
   git push origin main

app.js already tries gui/unified_report.json first and will pick this up
automatically - no further code changes needed.
