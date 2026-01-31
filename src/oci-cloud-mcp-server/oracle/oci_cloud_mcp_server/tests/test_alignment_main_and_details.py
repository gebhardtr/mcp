from types import SimpleNamespace
import os
import typing
import pytest
from fastmcp import Client

import oracle.oci_cloud_mcp_server.server as srv
from oracle.oci_cloud_mcp_server.server import _align_params_to_signature


def test_align_params_to_signature_maps_create_key():
    # method expects create_bucket_details; incoming uses bucket_details
    def create_bucket(create_bucket_details=None):  # noqa: ARG001
        return None

    params = {"bucket_details": {"name": "x"}}
    out = _align_params_to_signature(create_bucket, "create_bucket", params)
    assert "create_bucket_details" in out and "bucket_details" not in out
    assert out["create_bucket_details"] == {"name": "x"}


@pytest.mark.asyncio
async def test_get_client_operation_details_type_hints_failure(monkeypatch):
    # Build fake module and class
    def op(self, x):  # noqa: ARG002
        """Operation doc."""
        return 1

    C = type("C", (), {"op": op})
    fake_module = SimpleNamespace(C=C)

    # Patch import_module to return fake module
    monkeypatch.setattr(srv, "import_module", lambda name: fake_module)

    # Force typing.get_type_hints to raise inside the function
    orig_get_type_hints = typing.get_type_hints

    def boom_get_type_hints(obj):  # noqa: ARG001
        raise TypeError("no hints")

    monkeypatch.setattr(typing, "get_type_hints", boom_get_type_hints)

    try:
        async with Client(srv.mcp) as client:
            res = (
                await client.call_tool(
                    "get_client_operation_details",
                    {"client_fqn": "mod.C", "operation": "op"},
                )
            ).data
    finally:
        # restore typing.get_type_hints to avoid side effects on other tests
        monkeypatch.setattr(typing, "get_type_hints", orig_get_type_hints)

    assert res["client"] == "mod.C"
    assert res["operation"] == "op"
    # Parameters should still include 'x' even when type hints fail
    names = {p["name"] for p in res.get("parameters", [])}
    assert "x" in names


def test_list_client_operations_error_path(monkeypatch):
    # Ensure when target is not a class, list_client_operations logs and raises
    fake_module = SimpleNamespace(NotAClass=lambda: None)
    monkeypatch.setattr(srv, "import_module", lambda name: fake_module)
    with pytest.raises(Exception):
        srv.list_client_operations("mod.NotAClass")


def test_main_runs_http_transport_when_env_vars_present(monkeypatch):
    called = {}

    def fake_run(**kwargs):
        called.update(kwargs)

    monkeypatch.setattr(srv.mcp, "run", fake_run)
    monkeypatch.setenv("ORACLE_MCP_HOST", "127.0.0.1")
    monkeypatch.setenv("ORACLE_MCP_PORT", "8080")

    srv.main()

    assert called.get("transport") == "http"
    assert called.get("host") == "127.0.0.1"
    assert called.get("port") == 8080

    # cleanup
    monkeypatch.delenv("ORACLE_MCP_HOST", raising=False)
    monkeypatch.delenv("ORACLE_MCP_PORT", raising=False)
    called.clear()

    # no env -> default run() without args
    srv.main()
    # with no kwargs passed, our fake_run should see empty dict
    assert called == {}
