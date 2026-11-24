"""List the search/fetch tools Composio offers this account:  python -m agent.discover"""
import json

from .common import composio_client

c = composio_client()
tools = None
for how in ("raw", "client"):
    try:
        if how == "raw":
            tools = c.tools.get_raw_composio_tools(toolkits=["composio_search"])
        else:
            tools = c._client.tools.list(toolkit_slug="composio_search", limit=100).items
        break
    except Exception as e:
        print(f"({how} listing failed: {str(e)[:150]})")

for t in tools or []:
    d = t if isinstance(t, dict) else t.model_dump()
    params = (d.get("input_parameters") or {}).get("properties", {})
    req = (d.get("input_parameters") or {}).get("required", [])
    print(f"{d.get('slug')}  | version={d.get('version')} | params={list(params)} | required={req}")
print(f"\n{len(tools or [])} tools found")
