from types import SimpleNamespace
import pytest
from fastmcp import Client

import oracle.oci_cloud_mcp_server.server as srv


@pytest.mark.asyncio
async def test_list_clients_discovers_public_client_and_skips_models_and_private(monkeypatch):
    # Simulate pkgutil.walk_packages over oci.* modules
    fake_mods = [
        SimpleNamespace(name="oci.foo"),
        SimpleNamespace(name="oci.foo.models"),  # should be skipped
        SimpleNamespace(name="oci._private"),    # should be skipped (private)
        SimpleNamespace(name="oci.bar"),         # will fail import -> skipped
    ]
    monkeypatch.setattr(srv.pkgutil, "walk_packages", lambda path, prefix: fake_mods, raising=False)

    # Build a fake 'oci.foo' module with a proper client class defined in it
    FooModule = SimpleNamespace()
    FooModule.__name__ = "oci.foo"

    class FooClient:
        """Foo client."""

        pass

    # Ensure the class appears to be defined in the foo module (not a re-export)
    FooClient.__module__ = "oci.foo"
    # Attach the class as an attribute of the module
    setattr(FooModule, "FooClient", FooClient)

    # Provide a dummy models module and make 'oci.bar' raise to exercise best-effort import handling
    FooModelsModule = SimpleNamespace(__name__="oci.foo.models")

    def fake_import_module(name: str):
        if name == "oci.foo":
            return FooModule
        if name == "oci.foo.models":
            return FooModelsModule
        # simulate an import failure for others
        raise ImportError(name)

    monkeypatch.setattr(srv, "import_module", fake_import_module)

    async with Client(srv.mcp) as client:
        out = (await client.call_tool("list_clients", {})).data
    assert "clients" in out
    clients = out["clients"]

    # Only FooClient from oci.foo should be discovered
    fqns = [c["fqn"] for c in clients]
    assert "oci.foo.FooClient" in fqns
    # Ensure the entry includes expected keys
    entry = next(c for c in clients if c["fqn"] == "oci.foo.FooClient")
    assert entry["name"] == "FooClient"
    assert entry["module"] == "oci.foo"
    # Description comes from the first line of docstring (may be empty if not present)
    assert isinstance(entry.get("description", ""), str)
