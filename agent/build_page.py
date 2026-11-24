"""Build the single-page case study: docs/index.html (+ docs/results.json, docs/results.csv).

Inputs: data/final.jsonl (or pass2/pass1), data/accuracy.json, data/insights.json (headline text)
Usage:  python -m agent.build_page
"""
from __future__ import annotations

import csv
import io
import json
from collections import Counter
from pathlib import Path

from .common import DATA, ROOT, read_jsonl

DOCS = ROOT / "docs"
DOCS.mkdir(exist_ok=True)
REPO = "https://github.com/luvgarg05/composio-app-research"


def load_rows():
    for name in ("final.jsonl", "pass2.jsonl", "pass1.jsonl"):
        rows = read_jsonl(DATA / name)
        if rows:
            return name, sorted(rows, key=lambda r: r["id"])
    raise SystemExit("no results yet")


def clean(r):
    ev = r.get("evidence") or {}
    auth = r.get("auth_methods") or []
    if isinstance(auth, str):
        auth = [auth]
    return {
        "id": r["id"], "app": r["app"], "category": r["category"], "hint": r["hint"],
        "what": r.get("category_line") or "", "auth": auth, "access": r.get("access") or "unclear",
        "api": r.get("api_type") or "unclear", "breadth": r.get("api_breadth") or "unclear",
        "mcp": r.get("mcp") or "unclear", "buildable": r.get("buildable") or "unclear",
        "blocker": r.get("blocker") or "", "confidence": r.get("confidence") or "",
        "evidence": {k: v for k, v in ev.items() if isinstance(v, str) and v.startswith("http")},
        "verified_by": r.get("verified_by", ""), "changes": r.get("changes") or [],
    }


def stats(rows):
    n = len(rows)
    auth = Counter(a for r in rows for a in r["auth"])
    access = Counter(r["access"] for r in rows)
    build = Counter(r["buildable"] for r in rows)
    mcp = Counter(r["mcp"] for r in rows)
    cats = sorted({r["category"] for r in rows}, key=lambda c: min(x["id"] for x in rows if x["category"] == c))
    by_cat = {c: Counter(r["access"] for r in rows if r["category"] == c) for c in cats}
    blockers = Counter(r["blocker"].split(";")[0].strip().rstrip(".") for r in rows if r["blocker"])
    return {"n": n, "auth": auth.most_common(), "access": access.most_common(), "build": build.most_common(),
            "mcp": mcp.most_common(), "cats": cats, "by_cat": {c: dict(v) for c, v in by_cat.items()},
            "blockers": blockers.most_common(8)}


def _noop(change: str) -> bool:
    """True for 'field: X -> X (...)' entries that changed nothing."""
    head = change.split(" (")[0]
    if ": " not in head or " -> " not in head:
        return False
    parts = head.split(": ", 1)[1].split(" -> ", 1)
    if len(parts) != 2:
        return False
    old, new = parts
    return old.strip() == new.strip()


def verification_log():
    """Summarise what the automatic checks flagged and what the loop changed."""
    p2 = read_jsonl(DATA / "pass2.jsonl")
    if not p2:
        return {}
    kinds = Counter()
    for r in p2:
        seen = set()
        for i in r.get("checks_failed_pass1", []):
            k = ("rule: " + i.split("rule: ")[1].split(" (")[0]) if i.startswith("rule:") else \
                i.split(": ", 1)[1].split(" (")[0].replace("'", "") if ": " in i else i
            k = k.split(" is not mentioned")[0] + (" not on evidence page" if "not mentioned" in i else "")
            if k not in seen:
                kinds[k] += 1
                seen.add(k)
    ex = [{"app": r["app"], "change": c} for r in p2 for c in (r.get("changes") or [])
          if isinstance(c, str) and "->" in c and not _noop(c)][:12]
    return {"checks": kinds.most_common(12), "examples": ex,
            "reresearched": sum(1 for r in p2 if r.get("reresearched"))}


def main():
    src, raw = load_rows()
    rows = [clean(r) for r in raw]
    acc = json.loads((DATA / "accuracy.json").read_text()) if (DATA / "accuracy.json").exists() else {}
    ins = json.loads((DATA / "insights.json").read_text()) if (DATA / "insights.json").exists() else {}
    vlog = verification_log()
    st = stats(rows)
    (DOCS / "results.json").write_text(json.dumps(rows, indent=1, ensure_ascii=False), encoding="utf-8")
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["id", "app", "category", "what", "auth", "access", "api", "breadth", "mcp", "buildable",
                "blocker", "auth_evidence", "access_evidence", "mcp_evidence"])
    for r in rows:
        w.writerow([r["id"], r["app"], r["category"], r["what"], "; ".join(r["auth"]), r["access"], r["api"],
                    r["breadth"], r["mcp"], r["buildable"], r["blocker"], r["evidence"].get("auth_methods", ""),
                    r["evidence"].get("access", ""), r["evidence"].get("mcp", "")])
    (DOCS / "results.csv").write_text(buf.getvalue(), encoding="utf-8")
    payload = json.dumps({"rows": rows, "stats": st, "acc": acc, "ins": ins, "vlog": vlog,
                          "source": src, "repo": REPO}, ensure_ascii=False)
    html = (ROOT / "agent" / "page_template.html").read_text(encoding="utf-8").replace("__DATA__", payload)
    (DOCS / "index.html").write_text(html, encoding="utf-8")
    print(f"built docs/index.html from {src} ({len(rows)} apps)")


if __name__ == "__main__":
    main()
