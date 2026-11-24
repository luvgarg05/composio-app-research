# Composio app research agent: 100 apps, verified

**Live page:** https://luvgarg05.github.io/composio-app-research/ (source: `docs/index.html`)

An agent that researches whether Composio could build a toolkit for each of 100 apps, and then
**proves its answers**: every field carries an evidence URL, a verification loop re-checks failures, and
accuracy is measured on a stratified sample against the vendors' own docs.

## Results at a glance
| | |
|---|---|
| Apps researched | 100 (10 categories) |
| Buildable today | 88 (12 partial, 0 impossible) |
| Self-serve for a developer | 72% |
| Official MCP server | 86% |
| Accuracy, agent alone (pass 1) | **91%** |
| Accuracy after verification loop + audit | **96%** |

Accuracy is measured on 20 apps (2 per category, `random.Random(42)`), 5 fields each (auth methods, access,
API type, MCP, buildable) = 100 checked answers. The 4 misses are listed on the page and in `data/accuracy.json`.

## What each field means
- **auth_methods**: credentials the *main data API* accepts (OAuth2, API key, Basic, Bearer/PAT token, JWT/signed, None, Other). Side keys for app settings are noted, not counted.
- **access**: self-serve free / self-serve trial / paid plan / admin/app approval / partner/sales gated / open-source/self-host / unclear.
- **api_type / api_breadth**: REST, GraphQL, both, or SDK/CLI only, and how much of the product it covers.
- **mcp**: `official` only if the vendor's own site or GitHub org publishes it; otherwise community / none found.
- **buildable + blocker**: yes / partial / no, with the specific blocker.

## How the agent works
```
apps.csv ─► pass 1: research ─► pass 2: verify ─► audit ─► score ─► page
            (search + fetch     (7 automatic      (sourced     (20-app
             + LLM, schema       checks, critic    corrections)  sample)
             with evidence)      re-research)
```
1. **`agent/research.py`, pass 1.** For each app: fetches the official docs hint, runs targeted searches
   (auth, access/pricing, MCP) through the **Composio SDK** (`TAVILY_SEARCH` via a Composio session), fetches the
   best vendor-domain pages, and asks an LLM to fill a strict JSON schema with one evidence URL per field.
   The prompt anchors identity ("ignore pages about a different product with the same name").
2. **`agent/verify.py`, pass 2.** Automatic checks per row: evidence URL loads; is on the vendor's domain;
   the page actually mentions the claimed auth method; MCP evidence mentions MCP and "official" comes from the
   vendor's domain or GitHub org; consistency rules (no API or sales-gated means not buildable=yes; partial/no needs a blocker).
   Rows that fail are re-researched on the vendor's domain by a critic model. `--no-llm` runs only the checks.
3. **`data/audit_corrections.json`.** An independent audit of the risky claims; every correction carries a
   source URL and a reason. `python -m agent.score --final` merges it into `data/final.jsonl`.
4. **`agent/score.py`.** Builds the sample sheet and scores pass 1, pass 2 and final against it.
5. **`agent/build_page.py`.** Builds the single-page site plus `docs/results.json` / `results.csv` for other agents.

## Where a human was needed (and what went wrong, honestly)
- **Free-tier limits.** Groq and Gemini daily quotas and the Composio wallet ran out mid-run. The search layer
  has a circuit breaker that falls back to a free search backend and logs `search_backend` per row. The last
  18 apps were researched by the Claude agent with web search under the same schema; those rows are labelled
  `_model: "claude-agent (web search)"`. They received only the automatic checks in pass 2 (`llm_skipped`).
- **A verifier regression.** Pass 2 "fixed" *Paygent Connect* by matching an unrelated product (paygent.to, an
  AI-cost tool) and rated it free/buildable/high confidence. The audit caught and reverted it. The lesson:
  identity checks matter more than freshness, and a second model can make an answer *worse*.
- **Blocked sites.** Binance and PitchBook block automated fetches, so their checks failed for the wrong reason
  and were audited by hand.
- **Ground truth.** The 20-app sample was checked against vendor docs by a separate review pass, with
  judgement calls written in `notes` (e.g. HubSpot's developer API key only covers app settings).
  Lavanya spot-checked HubSpot and Google Ads.
- **Weakest field: auth_methods (85%).** The agent confuses the MCP's OAuth with the REST API's auth (Coda),
  and misses secondary methods (Asana OAuth).

## Run it
```bash
pip install -r requirements.txt
cp .env.example .env               # add COMPOSIO_API_KEY and GROQ_API_KEY (optional GEMINI_API_KEY)
python -m agent.smoke              # checks keys, Composio search and the LLM
python -m agent.research           # pass 1 (resumable, --workers N, --ids 1,2,3)
python -m agent.verify             # pass 2 (resumable; --no-llm = checks only)
python -m agent.score --make-sample
python -m agent.score --final      # final = pass 2 + audit corrections
python -m agent.score              # accuracy.json
python -m agent.build_page         # docs/index.html
```

## Files
- `apps.csv`: the 100 apps from the brief
- `data/pass1.jsonl`, `data/pass2.jsonl`, `data/final.jsonl`: each stage's output
- `data/pass1_trial_v1.jsonl`, `data/pass1_trial_v2.jsonl`: 5-app trials before and after the prompt fixes
- `data/audit_corrections.json`, `data/human_check.csv`, `data/accuracy.json`
- `docs/`: the deployed page plus machine-readable results
