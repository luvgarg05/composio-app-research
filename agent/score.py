"""Human check + accuracy scoring.

1) python -m agent.score --make-sample   -> writes data/human_check.csv (20 apps, 2 per category)
2) A human opens each app's docs and fills the *_truth columns (and notes).
3) python -m agent.score --final         -> builds data/final.jsonl = pass 2 + audited corrections
                                            from data/audit_corrections.json (each with a source URL)
4) python -m agent.score                 -> compares pass 1, pass 2 and final against the human
                                            answers, writes data/accuracy.json
"""
from __future__ import annotations

import argparse
import csv
import json
import random

from .common import DATA, read_jsonl

SAMPLE = DATA / "human_check.csv"
CHECKED = ["auth_methods", "access", "api_type", "mcp", "buildable"]


def norm(field, v):
    if field == "auth_methods":
        if isinstance(v, str):
            v = [x.strip() for x in v.replace(";", ",").split(",")]
        return frozenset(x.strip().lower() for x in (v or []) if x and x.strip())
    return str(v or "").strip().lower()


def make_sample():
    p2 = {r["id"]: r for r in read_jsonl(DATA / "pass2.jsonl")}
    by_cat: dict[str, list] = {}
    for r in p2.values():
        by_cat.setdefault(r["category"], []).append(r)
    rnd = random.Random(42)
    rows = []
    for cat, items in by_cat.items():
        for r in rnd.sample(items, min(2, len(items))):
            row = {"id": r["id"], "app": r["app"], "docs_hint": r["hint"]}
            for f in CHECKED:
                val = r.get(f)
                row[f"{f}_agent"] = ", ".join(val) if isinstance(val, list) else val
                row[f"{f}_truth"] = ""
            row["auth_evidence_url"] = (r.get("evidence") or {}).get("auth_methods", "")
            row["notes"] = ""
            rows.append(row)
    rows.sort(key=lambda x: x["id"])
    with open(SAMPLE, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    print(f"wrote {SAMPLE} ({len(rows)} apps). Fill every *_truth column from the vendor docs "
          f"(copy the agent value if it is right).")


def score():
    truth = list(csv.DictReader(open(SAMPLE, encoding="utf-8")))
    passes = {"pass1": {r["id"]: r for r in read_jsonl(DATA / "pass1.jsonl")},
              "pass2": {r["id"]: r for r in read_jsonl(DATA / "pass2.jsonl")},
              "final": {r["id"]: r for r in read_jsonl(DATA / "final.jsonl")}}
    passes = {k: v for k, v in passes.items() if v}
    result = {"sample_size": len(truth), "fields": CHECKED, "per_field": {}, "overall": {}, "misses": []}
    for name, rows in passes.items():
        hit = tot = 0
        for f in CHECKED:
            h = t = 0
            for tr in truth:
                if not tr[f"{f}_truth"].strip():
                    continue
                got = rows.get(int(tr["id"]), {}).get(f)
                ok = norm(f, got) == norm(f, tr[f"{f}_truth"])
                h, t = h + ok, t + 1
                if not ok and name == list(passes)[-1]:
                    result["misses"].append({"app": tr["app"], "field": f, "agent": got,
                                             "truth": tr[f"{f}_truth"], "note": tr.get("notes", "")})
            result["per_field"].setdefault(f, {})[name] = round(100 * h / t, 1) if t else None
            hit, tot = hit + h, tot + t
        result["overall"][name] = round(100 * hit / tot, 1) if tot else None
    (DATA / "accuracy.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps(result["overall"], indent=2))


def build_final():
    """final = pass 2 + audited corrections. Each correction must carry a source URL."""
    corr = json.loads((DATA / "audit_corrections.json").read_text(encoding="utf-8")) \
        if (DATA / "audit_corrections.json").exists() else {}
    out = DATA / "final.jsonl"
    rows = []
    for r in read_jsonl(DATA / "pass2.jsonl"):
        c = corr.get(str(r["id"]))
        r = {**r, "verified_by": "agent + verifier"}
        if c:
            for k, v in c.get("set", {}).items():
                r[k] = v
            r.setdefault("evidence", {}).update(c.get("evidence", {}))
            r["changes"] = (r.get("changes") or []) + [f"audit: {c.get('why', '')}"]
            r["verified_by"] = "agent + verifier + audit"
        rows.append(r)
    out.write_text("".join(json.dumps(x, ensure_ascii=False) + "\n" for x in sorted(rows, key=lambda x: x["id"])),
                   encoding="utf-8")
    print(f"final: {len(rows)} rows, {len(corr)} audited corrections -> {out}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--make-sample", action="store_true")
    ap.add_argument("--final", action="store_true")
    a = ap.parse_args()
    if a.make_sample:
        make_sample()
    elif a.final:
        build_final()
    else:
        score()
