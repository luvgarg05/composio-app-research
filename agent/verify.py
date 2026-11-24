"""PASS 2 - verification loop.

Every pass-1 row goes through automatic checks:
  1. citation   - every evidence URL must be a page the agent actually read or saw in search
  2. live       - evidence URLs must load (HTTP 200)
  3. official   - auth/access evidence must come from the vendor's own domain (or a known docs host)
  4. keyword    - each claimed auth method must literally appear on its evidence page
  5. identity   - the official page/evidence must be about THIS app (name or hint domain appears)
  6. consistency rules between fields, e.g.
       no hosted API (api_type none / SDK-CLI only)  -> buildable cannot be "yes"
       access partner/sales gated                     -> buildable cannot be "yes"
       mcp "official"  -> MCP evidence must be on the vendor's domain or the vendor's GitHub org
  7. confidence - "low" or "unclear" answers are re-researched
Rows that fail get a targeted second round (vendor-domain searches, the evidence pages) and a
"critic" LLM call that sees exactly which checks failed. Changes are logged per field so the
report can show what verification fixed.

Usage:  python -m agent.verify [--workers 3]
"""
from __future__ import annotations

import argparse
import json
import re
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

from .common import DATA, append_jsonl, fetch_text, hint_domain, llm_json, read_jsonl, web_search
from .research import SYSTEM

IN, OUT = DATA / "pass1.jsonl", DATA / "pass2.jsonl"

AUTH_KEYWORDS = {
    "OAuth2": r"oauth",
    "API key": r"api[\s_-]?key|x-api-key|apikey|api token",
    "Basic": r"basic auth|basic authentication|authorization: basic|http basic",
    "Bearer/PAT token": r"bearer|access token|personal access token|api token|private app",
    "JWT/signed": r"jwt|json web token|signature|hmac|signed",
}
SHARED_DOCS = ("github.com", "githubusercontent.com", "stoplight.io", "readme.io", "postman.com",
               "learn.microsoft.com", "developers.google.com", "cloud.google.com",
               "developers.facebook.com", "developer.amazon.com", "developer-docs.amazon.com",
               "developer.atlassian.com", "developer.salesforce.com", "developer.adobe.com",
               "developer.intuit.com", "gitbook.io", "mintlify")


def _norm_list(v):
    if isinstance(v, str):
        return [v]
    return [x for x in (v or []) if x]


def checks(row: dict, cache: dict) -> list[str]:
    issues = []
    ev = row.get("evidence") or {}
    seen = set(row.get("sources_seen") or [])
    dom = hint_domain(row["hint"])
    name_tokens = [t.lower() for t in re.findall(r"[A-Za-z0-9]+", row["app"]) if len(t) > 2]

    def page(url):
        if url not in cache:
            cache[url] = fetch_text(url, limit=15000)
        return cache[url]

    for field in ("auth_methods", "access", "mcp", "api_type"):
        url = ev.get(field)
        if not isinstance(url, str) or not url.startswith("http"):
            issues.append(f"{field}: no evidence URL")
            continue
        status, text = page(url)
        if status != 200:
            issues.append(f"{field}: evidence URL does not load ({status})")
        if url not in seen:
            issues.append(f"{field}: cites a page the agent never saw")
        if field in ("auth_methods", "access") and dom not in url and not any(s in url for s in SHARED_DOCS):
            issues.append(f"{field}: evidence is not the vendor's own docs ({url})")
        if status == 200 and text and name_tokens and not any(t in text.lower() for t in name_tokens) \
                and dom not in url:
            issues.append(f"{field}: evidence page may be about a different product")
    url = ev.get("auth_methods")
    if isinstance(url, str) and url.startswith("http"):
        status, text = page(url)
        low = (text or "").lower()
        for m in _norm_list(row.get("auth_methods")):
            pat = AUTH_KEYWORDS.get(m)
            if status == 200 and pat and not re.search(pat, low):
                issues.append(f"auth_methods: '{m}' is not mentioned on its evidence page")
    # consistency rules
    if row.get("api_type") in ("none", "SDK/CLI only") and row.get("buildable") == "yes":
        issues.append("rule: no hosted API but buildable=yes (should be partial at most)")
    if row.get("access") == "partner/sales gated" and row.get("buildable") == "yes":
        issues.append("rule: partner/sales gated but buildable=yes")
    if row.get("buildable") in ("partial", "no") and not row.get("blocker"):
        issues.append("rule: buildable is not yes but no blocker given")
    if row.get("mcp") in ("official", "community"):
        u = ev.get("mcp") or ""
        if isinstance(u, str) and u.startswith("http"):
            status, text = page(u)
            if status == 200 and not re.search(r"\bmcp\b|model context protocol", (text or "").lower()):
                issues.append("mcp: evidence page does not mention MCP")
        if row.get("mcp") == "official":
            ul = (u or "").lower()
            gh_org = ul.split("github.com/")[1].split("/")[0] if "github.com/" in ul else ""
            vendor_gh = gh_org and any(t in gh_org for t in name_tokens + [dom.split(".")[0]])
            if dom not in ul and not vendor_gh and not any(s in ul for s in SHARED_DOCS if s != "github.com"):
                issues.append("mcp: 'official' but evidence is not from the vendor's site or GitHub org")
    if row.get("mcp") in ("community", "none found"):
        issues.append("mcp: re-check vendor site for an official MCP before accepting")
    if row.get("confidence") == "low":
        issues.append("agent reported low confidence")
    for f in ("auth_methods", "access", "api_type", "buildable"):
        v = row.get(f)
        if v in (None, "", "unclear", []) or v == ["unclear"]:
            issues.append(f"{f}: unclear")
    return issues


CRITIC = SYSTEM + """

You are now the VERIFIER for a previous answer. You get: the previous answer, the automatic checks
it FAILED, and fresh pages from the vendor's own domain. Fix what the sources show is wrong,
keep what they confirm. For MCP: if the vendor's own site/docs/GitHub org offers an MCP server,
answer "official" and cite that page. Every evidence URL must be one of the URLs you were given.
Also return "changes": a list of short strings, one per field you changed, "field: old -> new (why)"."""


NO_LLM = False


def verify(row: dict) -> dict:
    cache: dict = {}
    issues = checks(row, cache)
    out = {**row, "checks_failed_pass1": issues, "changes": [], "reresearched": False}
    hard = [i for i in issues if not i.startswith("mcp: re-check")]
    if not issues:
        out["checks_failed_pass2"] = []
        return out
    if NO_LLM:  # quota exhausted: keep the automatic checks, flag the row for the human audit
        out["checks_failed_pass2"] = hard
        out["llm_skipped"] = True
        return out
    dom = hint_domain(row["hint"])
    queries = [f"site:{dom} API authentication", f"site:{dom} MCP server",
               f"{row['app']} API access requirements developer account pricing plan"]
    if not hard:
        queries = [f"site:{dom} MCP server", f"{row['app']} official MCP server"]
    fresh = []
    for q in queries:
        fresh += web_search(q, max_results=4)
    pages, urls = [], set()
    for u, (s, t) in list(cache.items())[:3]:
        if s == 200 and t:
            pages.append({"url": u, "text": t[:1500]}); urls.add(u)
    for r in fresh:
        if len(pages) >= 6:
            break
        if r["url"] in urls:
            continue
        if dom in r["url"] or "github.com" in r["url"]:
            s, t = fetch_text(r["url"], limit=1500)
            if s == 200 and len(t) > 300:
                pages.append({"url": r["url"], "text": t}); urls.add(r["url"])
    prev = {k: row.get(k) for k in ("category_line", "auth_methods", "access", "api_type",
                                     "api_breadth", "mcp", "buildable", "blocker", "evidence", "confidence")}
    ans = llm_json(CRITIC, role="verify", user=json.dumps({
        "app": row["app"], "hint (official site/repo)": row["hint"], "previous_answer": prev,
        "failed_checks": issues, "fresh_pages": pages,
        "fresh_search_results": [{"url": f["url"], "snippet": f["snippet"][:250]} for f in fresh[:10]],
    }, ensure_ascii=False)[:9000], max_tokens=3500)
    if ans:
        out.update({k: v for k, v in ans.items() if k != "changes"})
        out["changes"] = ans.get("changes", [])
        out["sources_seen"] = list({*row.get("sources_seen", []), *urls, *[f["url"] for f in fresh]})
        out["reresearched"] = True
    out["checks_failed_pass2"] = [i for i in checks(out, cache) if not i.startswith("mcp: re-check")]
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--workers", type=int, default=3)
    ap.add_argument("--no-llm", action="store_true", help="only run the automatic checks")
    args = ap.parse_args()
    global NO_LLM
    NO_LLM = args.no_llm
    done = {r["id"] for r in read_jsonl(OUT)}
    todo = [r for r in read_jsonl(IN) if r["id"] not in done and r.get("access")]
    lock = threading.Lock()

    def work(row):
        v = verify(row)
        with lock:
            append_jsonl(OUT, v)
        return v

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futs = [pool.submit(work, r) for r in todo]
        for n, f in enumerate(as_completed(futs), 1):
            try:
                v = f.result()
                print(f"[{n:>3}/{len(todo)}] {v['app']}: {len(v['checks_failed_pass1'])} issues -> "
                      f"{len(v.get('checks_failed_pass2', []))} after verification")
            except Exception as e:
                print(f"   failed (re-run to retry): {e}")
    print(f"pass 2 done -> {OUT}")


if __name__ == "__main__":
    main()
