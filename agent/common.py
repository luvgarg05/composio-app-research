"""Shared helpers: config, Composio search, page fetching, LLM calls, JSON I/O."""
from __future__ import annotations

import csv
import json
import os
import re
import time
from pathlib import Path
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from openai import OpenAI

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
DATA.mkdir(exist_ok=True)
load_dotenv(ROOT / ".env")

LLM_MODEL = os.getenv("LLM_MODEL", "")  # empty = pick the best available model automatically
LLM_BASE_URL = os.getenv("LLM_BASE_URL", "https://api.groq.com/openai/v1")  # any OpenAI-compatible API
LLM_API_KEY = os.getenv("LLM_API_KEY") or os.getenv("GROQ_API_KEY")
LLM_PAUSE = float(os.getenv("LLM_PAUSE_SECONDS", "4"))  # stay under free-tier tokens/minute
SEARCH_TOOL = os.getenv("COMPOSIO_SEARCH_TOOL", "TAVILY_SEARCH")
USER_ID = os.getenv("COMPOSIO_USER_ID", "app-research-agent")
UA = {"User-Agent": "Mozilla/5.0 (app-research-agent; +github.com/luvgarg05/composio-app-research)"}

# ---------------------------------------------------------------- apps

def load_apps() -> list[dict]:
    with open(ROOT / "apps.csv", newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def hint_domain(hint: str) -> str:
    """'docs.apify.com' -> 'apify.com'; 'zoho.com/crm' -> 'zoho.com'."""
    token = hint.split()[0].strip()
    host = urlparse(token if "://" in token else "https://" + token).netloc.lower()
    parts = host.split(".")
    return ".".join(parts[-2:]) if len(parts) >= 2 else host

# ---------------------------------------------------------------- search (Composio)

_session = None


def composio_session():
    """Composio session (current SDK pattern). Tool versions are handled by the session."""
    global _session
    if _session is None:
        from composio import Composio
        _session = Composio(api_key=os.environ["COMPOSIO_API_KEY"]).create(user_id=USER_ID)
    return _session


def _walk_urls(obj, out: list[dict]):
    """Search tools return nested JSON; collect anything that has a url."""
    if hasattr(obj, "model_dump"):
        obj = obj.model_dump()
    if isinstance(obj, dict):
        url = obj.get("url") or obj.get("link")
        if isinstance(url, str) and url.startswith("http"):
            out.append({"url": url, "title": str(obj.get("title", "")),
                        "snippet": str(obj.get("content") or obj.get("snippet") or obj.get("text") or "")})
        for v in obj.values():
            _walk_urls(v, out)
    elif isinstance(obj, list):
        for v in obj:
            _walk_urls(v, out)


def _ddg(query: str, n: int) -> list[dict]:
    """Fallback search (no key), used when Composio is unavailable; counted separately."""
    from ddgs import DDGS
    for q in (query, query.replace("site:", "")):
        try:
            hits = DDGS().text(q, max_results=n)
            if hits:
                return [{"url": r["href"], "title": r.get("title", ""), "snippet": r.get("body", "")} for r in hits]
        except Exception:
            time.sleep(2)
    return []


SEARCH_LOG = {"composio": 0, "fallback": 0}
_composio_off = {"why": ""}


def web_search(query: str, max_results: int = 6) -> list[dict]:
    """Web search through Composio (TAVILY_SEARCH in a Composio session); free fallback if
    Composio is out of credits (circuit breaker: stop calling it after a 402)."""
    urls: list[dict] = []
    if not _composio_off["why"] and os.getenv("SEARCH_BACKEND", "composio") == "composio":
        for attempt in range(3):
            try:
                res = composio_session().execute(SEARCH_TOOL, arguments={"query": query, "max_results": max_results})
                data = res.model_dump() if hasattr(res, "model_dump") else res
                if isinstance(data, dict) and data.get("successful") is False:
                    raise RuntimeError(data.get("error"))
                _walk_urls(data, urls)
                SEARCH_LOG["composio"] += 1
                break
            except Exception as e:
                msg = str(e)
                if "402" in msg or "balance" in msg.lower():
                    _composio_off["why"] = "Composio credits exhausted"
                    print("   Composio credits exhausted -> using fallback search from now on")
                    break
                print(f"   composio search retry {attempt + 1}: {msg[:160]}")
                time.sleep(3 * (attempt + 1))
    if not urls:
        urls = _ddg(query, max_results)
        if urls:
            SEARCH_LOG["fallback"] += 1
            for u in urls:
                u["via"] = "fallback"
    seen, uniq = set(), []
    for u in urls:
        if u["url"] not in seen:
            seen.add(u["url"])
            uniq.append(u)
    return uniq[:max_results]

# ---------------------------------------------------------------- fetching

def fetch_text(url: str, limit: int = 7000) -> tuple[int, str]:
    """Return (http_status, visible text) for a URL. Status 0 = network error."""
    try:
        r = requests.get(url, headers=UA, timeout=20)
        if "html" not in r.headers.get("content-type", "html"):
            return r.status_code, r.text[:limit]
        soup = BeautifulSoup(r.text, "html.parser")
        for t in soup(["script", "style", "nav", "footer", "svg", "noscript"]):
            t.decompose()
        text = re.sub(r"\s+", " ", soup.get_text(" ")).strip()
        return r.status_code, text[:limit]
    except Exception:
        return 0, ""

# ---------------------------------------------------------------- LLM (Groq, OpenAI-compatible)

PROVIDERS = {
    # name: (base_url, key env, model preference order)
    "gemini": ("https://generativelanguage.googleapis.com/v1beta/openai/", "GEMINI_API_KEY",
               ["gemini-3-flash", "gemini-2.5-flash", "gemini-flash"]),
    "groq": ("https://api.groq.com/openai/v1", "GROQ_API_KEY",
             ["gpt-oss-120b", "kimi-k2", "llama-4-maverick", "qwen3", "70b", "gpt-oss-20b"]),
}
# both roles use Groq (gpt-oss-120b) by default; set VERIFY_PROVIDER=gemini to make the verifier a second model -
# OTHER model, so two different models check each other.
ROLE_ORDER = {"research": ["groq", "gemini"], "verify": ["groq", "gemini"]}
if os.getenv("RESEARCH_PROVIDER"):
    ROLE_ORDER["research"] = [os.getenv("RESEARCH_PROVIDER")] + [x for x in ROLE_ORDER["research"] if x != os.getenv("RESEARCH_PROVIDER")]
if os.getenv("VERIFY_PROVIDER"):
    ROLE_ORDER["verify"] = [os.getenv("VERIFY_PROVIDER")] + [x for x in ROLE_ORDER["verify"] if x != os.getenv("VERIFY_PROVIDER")]
_clients: dict = {}
_init_lock = __import__("threading").Lock()


def _client(role: str):
    with _init_lock:
        if role in _clients:
            return _clients[role]
        for prov in ROLE_ORDER[role]:
            base, env, prefs = PROVIDERS[prov]
            key = os.getenv(env)
            if not key:
                continue
            cli = OpenAI(api_key=key, base_url=base)
            model = os.getenv(f"{role.upper()}_MODEL") or os.getenv(f"{prov.upper()}_MODEL") or LLM_MODEL
            if not model:
                ids = sorted(m.id.replace("models/", "") for m in cli.models.list().data)
                model = next((i for p in prefs for i in ids
                              if p in i and not any(x in i for x in ("guard", "whisper", "tts", "image", "lite", "embed", "audio", "preview-tts"))),
                             ids[0])
            print(f"   [{role}] using {prov}: {model}")
            _clients[role] = (cli, model, prov)
            return _clients[role]
    raise RuntimeError("no LLM key found in .env")


def llm_json(system: str, user: str, max_tokens: int = 6000, role: str = "research") -> dict:
    cli, model, prov = _client(role)
    for attempt in range(10):
        try:
            r = cli.chat.completions.create(
                model=model,
                temperature=0,
                max_tokens=max_tokens,
                response_format={"type": "json_object"},
                messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
            )
            time.sleep(LLM_PAUSE)
            content = (r.choices[0].message.content or "").strip()
            if content.startswith("```"):
                content = content.strip("`").removeprefix("json").strip()
            if not content:
                raise RuntimeError("empty reply")
            out = json.loads(content)
            out["_model"] = f"{prov}:{model}"
            return out
        except Exception as e:  # free-tier rate limits: back off and retry
            wait = min(60, 15 * (attempt + 1))
            print(f"   llm retry {attempt + 1} in {wait}s: {str(e)[:120]}")
            time.sleep(wait)
    return {}

# ---------------------------------------------------------------- jsonl helpers

def read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]


def append_jsonl(path: Path, row: dict):
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


FIELDS = ["category_line", "auth_methods", "access", "api_type", "api_breadth",
          "mcp", "buildable", "blocker"]
ACCESS_VALUES = ["self-serve free", "self-serve trial", "paid plan", "admin/app approval",
                 "partner/sales gated", "open-source/self-host", "unclear"]
AUTH_VALUES = ["OAuth2", "API key", "Basic", "Bearer/PAT token", "JWT/signed", "None", "Other"]
