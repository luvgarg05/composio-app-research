"""Quick check that both keys work before the long run:  python -m agent.smoke"""
from .common import SEARCH_LOG, SEARCH_TOOL, composio_session, fetch_text, llm_json, web_search

print(f"1) Composio search ({SEARCH_TOOL}) ...")
try:
    raw = composio_session().execute(SEARCH_TOOL, arguments={"query": "Stripe API authentication"})
    print("   raw:", str(raw)[:400])
except Exception as e:
    print("   COMPOSIO ERROR:", str(e)[:400])
hits = web_search("Stripe API authentication")
print("   urls found:", [h["url"] for h in hits][:5], "| used:", SEARCH_LOG)

print("2) Page fetch ...")
status, text = fetch_text("https://docs.stripe.com/keys")
print("   status:", status, "| chars:", len(text))

print("3) LLM (Groq) ...")
print("  ", llm_json('Return JSON {"ok": true, "model_says": "<one word>"}', "say hello"))
