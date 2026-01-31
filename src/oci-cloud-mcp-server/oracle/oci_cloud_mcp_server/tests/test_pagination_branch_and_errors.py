import pytest
from fastmcp import Client

import oracle.oci_cloud_mcp_server.server as srv
from oracle.oci_cloud_mcp_server.server import _call_with_pagination_if_applicable


def test_call_with_pagination_uses_paginator_and_reads_headers(monkeypatch):
    """
    Exercise the True branch of _supports_pagination (via 'list_' prefix) so that
    _call_with_pagination_if_applicable uses oci.pagination.list_call_get_all_results.
    Ensure it returns response.data and opc-request-id from headers.
    """

    called = {}

    def list_things(page=None, limit=None, compartment_id=None):  # noqa: ARG001
        # signature includes page/limit but will not be called directly in this test
        return None  # pragma: no cover

    class Response:
        def __init__(self, data):
            self.data = data
            self.headers = {"opc-request-id": "req-123"}

    def fake_list_call_get_all_results(method, **kwargs):
        called["method"] = method
        called["kwargs"] = kwargs
        return Response([{"id": 1}, {"id": 2}])

    # Force paginator path
    monkeypatch.setattr(srv.oci.pagination, "list_call_get_all_results", fake_list_call_get_all_results, raising=False)

    data, opc = _call_with_pagination_if_applicable(
        list_things, {"compartment_id": "ocid1.compartment.oc1..xyz"}, "list_things"
    )
    assert data == [{"id": 1}, {"id": 2}]
    assert opc == "req-123"
    assert called["method"] is list_things
    assert called["kwargs"]["compartment_id"].startswith("ocid1.compartment")


@pytest.mark.asyncio
async def test_invoke_oci_api_missing_operation_returns_error(monkeypatch):
    """
    Ensure invoke_oci_api returns an error dict when the specified operation is not found.
    Patch _import_client to return a dummy client without the requested operation.
    """

    class DummyClient:
        def existing(self):
            return None  # pragma: no cover

    monkeypatch.setattr(srv, "_import_client", lambda fqn: DummyClient())

    async with Client(srv.mcp) as client:
        res = (
            await client.call_tool(
                "invoke_oci_api",
                {
                    "client_fqn": "oci.any.Client",
                    "operation": "missing_operation",
                    "params": {"x": 1},
                },
            )
        ).data

    assert res["client"] == "oci.any.Client"
    assert res["operation"] == "missing_operation"
    assert "error" in res and "not found" in res["error"].lower()
