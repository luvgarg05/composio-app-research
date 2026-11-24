"""Show which Gemini/Groq models answer right now, with the full error if not:  python -m agent.llmcheck"""
import os
from openai import OpenAI
from .common import PROVIDERS, ROOT  # noqa: F401  (loads .env)

for prov, (base, env, prefs) in PROVIDERS.items():
    key = os.getenv(env)
    if not key:
        continue
    cli = OpenAI(api_key=key, base_url=base)
    ids = sorted(m.id.replace("models/", "") for m in cli.models.list().data)
    cands = [i for i in ids if any(p in i for p in prefs) and not any(x in i for x in ("tts", "image", "embed", "audio", "guard", "whisper"))]
    print(f"\n== {prov}: {len(ids)} models, testing {cands[:8]}")
    for m in cands[:8]:
        try:
            r = cli.chat.completions.create(model=m, max_tokens=20, messages=[{"role": "user", "content": "say ok"}])
            print(f"  OK    {m}")
        except Exception as e:
            print(f"  FAIL  {m}: {str(e)[:260]}")
