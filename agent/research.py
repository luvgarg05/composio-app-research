"""PASS 1 - research agent.

For each app the agent:
  1. anchors on the official site/repo given in the hint (always read first),
  2. runs three targeted web searches through Composio (TAVILY_SEARCH):
       auth  -> how API requests authenticate
       access-> how a developer gets credentials (free/trial/paid/approval/partner)
       mcp   -> whether an MCP server exists
  3. reads the best official page for each (vendor domain first),
  4. asks an LLM to fill a strict schema, citing a URL it actually read for every field.

Usage:  python -m agent.research            # all 100 apps (resumes where it stopped)
        python -m agent.research --ids 1,2  # just some apps
        python -m agent.research --ids 1,2 --redo   # re-run those apps
"""
from __future__ import annotations

import argparse
import json
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

from .common import (ACCESS_VALUES, AUTH_VALUES, DATA, append_jsonl, fetch_text, hint_domain,
                     llm_json, load_apps, read_jsonl, web_search)

OUT = DATA / "pass1.jsonl"

SYSTEM = f"""You research SaaS apps so an AI-agent platform (Composio) can build tool integrations.
Use ONLY the provided pages and search snippets. Accuracy matters more than completeness.

IDENTITY: the app is the one at the given hint (official site/repo). Ignore any page about a
different product that shares the name (e.g. other companies called "Sherlock").

Return JSON with exactly these keys:
- category_line: what the app does, one short line (take it from the official page)
- auth_methods: list from {AUTH_VALUES}. Include a method ONLY if the vendor's docs explicitly
  describe it for authenticating API requests. "Basic" only if the docs say HTTP Basic auth.
  Personal access tokens / bearer tokens / private-app tokens = "Bearer/PAT token".
  Open-source CLI/library with no hosted API = ["None"].
- access: one of {ACCESS_VALUES}
    self-serve free  = any developer can sign up and get working API credentials at no cost
                       (free tier, free developer account/sandbox/dev store, free bot token)
    self-serve trial = credentials only during a free trial, then a paid plan is needed
    paid plan        = API is only available on a paid plan (no free tier/trial with API access)
    admin/app approval = credentials need a review/approval (app review, developer program approval,
                       workspace admin approval) before real use
    partner/sales gated = need a partnership agreement, contact sales or an enterprise contract
    open-source/self-host = free open-source software you run yourself
- api_type: "REST", "GraphQL", "REST+GraphQL", "SDK/CLI only", "none" or "unclear"
- api_breadth: "broad" (many resources, full CRUD), "moderate", "narrow" (a few endpoints), "none", "unclear"
- mcp: "official" (vendor-built or vendor-hosted MCP server), "community" (third-party MCP on
  GitHub etc.), "none found", or "unclear"
- buildable: "yes" (a developer could build an agent toolkit today), "partial" (possible but limited,
  e.g. read-only, CLI wrapper, approval needed), or "no"
- blocker: the main blocker if buildable is not "yes", else ""
- evidence: object mapping EVERY key above to the single URL (from the pages you were given) that
  best supports it. Prefer the vendor's own docs. Never invent URLs.
- confidence: "high", "medium" or "low"
If the sources do not settle a field, answer "unclear" for it and lower confidence."""


def _best(results: list[dict], dom: str, prefer: tuple[str, ...] = ()) -> list[dict]:
    def score(r):
        u = r["url"].lower()
        return (dom not in u, not any(p in u for p in prefer), "blog" in u)
    return sorted(results, key=score)


def gather(app: dict) -> tuple[list[dict], list[dict]]:
    name, hint = app["app"], app["hint"]
    dom = hint_domain(hint)
    official = "https://" + hint.split()[0]
    plan = [
        ("auth", f"{name} ({hint}) API authentication", ("auth", "docs", "developer", "api")),
        ("access", f"{name} API access how to get API key developer account free plan pricing",
         ("getting-started", "docs", "developer", "pricing", "api")),
        ("mcp", f"{name} official MCP server {dom}", ("mcp",)),
        ("mcp", f"{name} MCP server github", ("mcp", "github.com")),
    ]
    pages, seen, results = [], set(), []
    status, text = fetch_text(official, limit=2500)
    if status == 200 and len(text) > 200:
        pages.append({"purpose": "official site from hint", "url": official, "text": text})
        seen.add(official)
    for purpose, q, prefer in plan:
        hits = web_search(q)
        results += [{**h, "purpose": purpose} for h in hits]
        for h in _best(hits, dom, prefer)[:3]:
            if h["url"] in seen:
                continue
            seen.add(h["url"])
            s, t = fetch_text(h["url"], limit=3000)
            if s == 200 and len(t) > 300:
                pages.append({"purpose": purpose, "url": h["url"], "text": t})
                break
    return results, pages


def research(app: dict) -> dict:
    results, pages = gather(app)
    context = {
        "app": app["app"], "hint (official site/repo)": app["hint"], "category": app["category"],
        "pages_read": pages,
        "search_snippets": [{"purpose": r["purpose"], "url": r["url"], "snippet": r["snippet"][:300]}
                            for r in results[:15]],
    }
    ans = llm_json(SYSTEM, json.dumps(context, ensure_ascii=False)[:18000])
    return {"id": int(app["id"]), "app": app["app"], "category": app["category"], "hint": app["hint"],
            **ans, "sources_seen": [p["url"] for p in pages] + [r["url"] for r in results],
            "search_backend": "fallback" if any(r.get("via") == "fallback" for r in results) else "composio"}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ids", help="comma-separated app ids")
    ap.add_argument("--redo", action="store_true", help="re-research ids even if already done")
    ap.add_argument("--workers", type=int, default=4, help="apps researched in parallel")
    args = ap.parse_args()
    rows = read_jsonl(OUT)
    empty = [r for r in rows if not r.get("access")]  # failed LLM calls: research them again
    if empty:
        print(f"re-doing {len(empty)} apps whose earlier answer came back empty")
        rows = [r for r in rows if r.get("access")]
        OUT.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")
    apps = load_apps()
    if args.ids:
        want = {int(i) for i in args.ids.split(",")}
        apps = [a for a in apps if int(a["id"]) in want]
        if args.redo:
            rows = [r for r in rows if r["id"] not in want]
            OUT.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")
    done = {r["id"] for r in rows}
    todo = [a for a in apps if int(a["id"]) not in done]
    lock = threading.Lock()

    def work(a):
        row = research(a)
        with lock:
            append_jsonl(OUT, row)
        return a

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futs = [pool.submit(work, a) for a in todo]
        for n, f in enumerate(as_completed(futs), 1):
            try:
                a = f.result()
                print(f"[{n:>3}/{len(todo)}] done: {a['app']}")
            except Exception as e:
                print(f"   failed (will be retried on next run): {e}")
    print(f"pass 1 done -> {OUT}")


if __name__ == "__main__":
    main()
