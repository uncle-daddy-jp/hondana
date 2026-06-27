"""Drift guard for the MCP tool surface.

tools/list (_MCP_TOOLS), the GET manifest, the JSON-RPC dispatch table, and the REST routes
must all expose exactly the same set of tool names. This catches the class of bug where the
manifest was hand-maintained and silently omitted a tool (append_to_article).
"""

from __future__ import annotations

from fastapi.routing import APIRoute

from routers import mcp
from routers.ask import ReqSearch

EXPECTED_TOOLS = {
    "search_knowledge",
    "ingest_new",
    "save_answer",
    "get_article",
    "get_recent",
    "search_by_tag",
    "list_tables",
    "list_tags",
    "append_to_article",
    "delete_knowledge",
}


def test_tools_list_names_match_expected():
    assert {t["name"] for t in mcp._MCP_TOOLS} == EXPECTED_TOOLS


def test_tools_list_entries_well_formed():
    for t in mcp._MCP_TOOLS:
        assert t["name"] and t["description"]
        assert t["inputSchema"]["type"] == "object"


def test_manifest_is_derived_and_covers_every_tool():
    manifest = mcp._manifest_tools()
    assert {t["name"] for t in manifest} == EXPECTED_TOOLS  # incl. append_to_article (the prior drift)
    for t in manifest:
        assert t["endpoint"] == f"/mcp/{t['name']}"
        assert t["description"]


def test_dispatch_table_keys_match_tools():
    # request is captured by the lambdas but not invoked while only building the table
    assert set(mcp._build_dispatch_table(None).keys()) == EXPECTED_TOOLS


def test_rest_routes_cover_every_tool():
    paths = {r.path for r in mcp.router.routes if isinstance(r, APIRoute)}
    for name in EXPECTED_TOOLS:
        assert f"/mcp/{name}" in paths, f"missing REST route /mcp/{name}"


def test_search_knowledge_top_k_default_matches_request_model():
    sk = next(t for t in mcp._MCP_TOOLS if t["name"] == "search_knowledge")
    schema_default = sk["inputSchema"]["properties"]["top_k"]["default"]
    assert schema_default == ReqSearch.model_fields["top_k"].default
