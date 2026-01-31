"""
Copyright (c) 2026, Oracle and/or its affiliates.
Licensed under the Universal Permissive License v1.0 as shown at
https://oss.oracle.com/licenses/upl.
"""

import importlib.metadata
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from fastmcp import Client
from oracle.oci_cloud_mcp_server import __project__
from oracle.oci_cloud_mcp_server.server import mcp

__version__ = importlib.metadata.version(__project__)
user_agent_name = __project__.split("oracle.", 1)[1].split("-server", 1)[0]
USER_AGENT = f"{user_agent_name}/{__version__}"


class TestCloudSdkTools:
    @pytest.mark.asyncio
    async def test_list_client_operations_with_fake_client(self):
        # build a fake client class with a couple of methods
        class FakeClient:
            def __init__(self, config, signer):
                self.config = config
                self.signer = signer

            def get_thing(self, id):
                return id

            def _hidden(self):
                return None

        fake_module = SimpleNamespace(FakeClient=FakeClient)

        with patch("oracle.oci_cloud_mcp_server.server.import_module") as mock_import:
            mock_import.return_value = fake_module

            async with Client(mcp) as client:
                result = (
                    await client.call_tool(
                        "list_client_operations",
                        {"client_fqn": "x.y.FakeClient"},
                    )
                ).data
                ops = (
                    result.get("operations", result)
                    if isinstance(result, dict)
                    else result or []
                )

                # only public callable functions should be listed
                names = [
                    op["name"] if isinstance(op, dict) else getattr(op, "name", None)
                    for op in ops
                ]
                names = [n for n in names if n]
                assert "get_thing" in names
                assert "_hidden" not in names

    @pytest.mark.asyncio
    async def test_invoke_oci_api_non_list_success(self):
        # fake OCI-like response
        class FakeResponse:
            def __init__(self, data):
                self.data = data
                self.headers = {"opc-request-id": "req-123"}

        class FakeClient:
            def __init__(self, config, signer):
                self.config = config
                self.signer = signer

            def get_thing(self, id):
                return FakeResponse({"id": id, "value": "ok"})

        fake_module = SimpleNamespace(FakeClient=FakeClient)

        with patch(
            "oracle.oci_cloud_mcp_server.server.import_module"
        ) as mock_import, patch(
            "oracle.oci_cloud_mcp_server.server._get_config_and_signer"
        ) as mock_cfg:
            mock_import.return_value = fake_module
            mock_cfg.return_value = ({}, object())

            async with Client(mcp) as client:
                result = (
                    await client.call_tool(
                        "invoke_oci_api",
                        {
                            "client_fqn": "x.y.FakeClient",
                            "operation": "get_thing",
                            "params": {"id": "abc"},
                        },
                    )
                ).data

                assert result["client"] == "x.y.FakeClient"
                assert result["operation"] == "get_thing"
                assert result["params"] == {"id": "abc"}
                assert result["opc_request_id"] == "req-123"
                assert result["data"] == {"id": "abc", "value": "ok"}

    @pytest.mark.asyncio
    async def test_invoke_oci_api_list_uses_paginator(self):
        class FakeResponse:
            def __init__(self, data):
                self.data = data
                self.headers = {"opc-request-id": "req-456"}

        class FakeClient:
            def __init__(self, config, signer):
                self.config = config
                self.signer = signer

            # existence of a list_* method is enough; paginator will be patched
            def list_things(self, compartment_id):
                return FakeResponse([{"name": "x"}])

        fake_module = SimpleNamespace(FakeClient=FakeClient)

        with patch(
            "oracle.oci_cloud_mcp_server.server.import_module"
        ) as mock_import, patch(
            "oracle.oci_cloud_mcp_server.server._get_config_and_signer"
        ) as mock_cfg, patch(
            "oracle.oci_cloud_mcp_server.server.oci.pagination.list_call_get_all_results"
        ) as mock_pager:
            mock_import.return_value = fake_module
            mock_cfg.return_value = ({}, object())
            mock_pager.return_value = FakeResponse(
                [{"name": "a"}, {"name": "b"}, {"name": "c"}]
            )

            async with Client(mcp) as client:
                result = (
                    await client.call_tool(
                        "invoke_oci_api",
                        {
                            "client_fqn": "x.y.FakeClient",
                            "operation": "list_things",
                            "params": {"compartment_id": "ocid1.compartment..xyz"},
                        },
                    )
                ).data

                assert result["client"] == "x.y.FakeClient"
                assert result["operation"] == "list_things"
                assert isinstance(result["data"], list)
                assert len(result["data"]) == 3

    @pytest.mark.asyncio
    async def test_invoke_oci_api_dns_get_zone_records_uses_paginator(self):
        class FakeResponse:
            def __init__(self, data):
                self.data = data
                self.headers = {"opc-request-id": "req-789"}

        class FakeClient:
            def __init__(self, config, signer):
                self.config = config
                self.signer = signer

            # non-list operation name but supports pagination via page/limit params (DNS style)
            def get_zone_records(self, zone_name, page=None, limit=None):
                return FakeResponse([{"name": "first"}])

        fake_module = SimpleNamespace(FakeClient=FakeClient)

        with patch(
            "oracle.oci_cloud_mcp_server.server.import_module"
        ) as mock_import, patch(
            "oracle.oci_cloud_mcp_server.server._get_config_and_signer"
        ) as mock_cfg, patch(
            "oracle.oci_cloud_mcp_server.server.oci.pagination.list_call_get_all_results"
        ) as mock_pager:
            mock_import.return_value = fake_module
            mock_cfg.return_value = ({}, object())
            mock_pager.return_value = FakeResponse(
                [{"name": "a"}, {"name": "b"}, {"name": "c"}, {"name": "d"}]
            )

            async with Client(mcp) as client:
                result = (
                    await client.call_tool(
                        "invoke_oci_api",
                        {
                            "client_fqn": "x.y.FakeClient",
                            "operation": "get_zone_records",
                            "params": {
                                "zone_name": "do-not-delete-me-testing-zone.example"
                            },
                        },
                    )
                ).data

                assert result["client"] == "x.y.FakeClient"
                assert result["operation"] == "get_zone_records"
                assert isinstance(result["data"], list)
                assert len(result["data"]) == 4

    @pytest.mark.asyncio
    async def test_invoke_oci_api_summarize_prefix_uses_paginator(self):
        class FakeResponse:
            def __init__(self, data):
                self.data = data
                self.headers = {"opc-request-id": "req-101"}

        class FakeClient:
            def __init__(self, config, signer):
                self.config = config
                self.signer = signer

            # summarize_* should trigger pagination even without page/limit
            def summarize_metrics(self, compartment_id):
                return FakeResponse([{"sum": 1}])

        fake_module = SimpleNamespace(FakeClient=FakeClient)

        with patch(
            "oracle.oci_cloud_mcp_server.server.import_module"
        ) as mock_import, patch(
            "oracle.oci_cloud_mcp_server.server._get_config_and_signer"
        ) as mock_cfg, patch(
            "oracle.oci_cloud_mcp_server.server.oci.pagination.list_call_get_all_results"
        ) as mock_pager:
            mock_import.return_value = fake_module
            mock_cfg.return_value = ({}, object())
            mock_pager.return_value = FakeResponse([{"sum": 10}, {"sum": 20}])

            async with Client(mcp) as client:
                result = (
                    await client.call_tool(
                        "invoke_oci_api",
                        {
                            "client_fqn": "x.y.FakeClient",
                            "operation": "summarize_metrics",
                            "params": {"compartment_id": "ocid1.compartment..abc"},
                        },
                    )
                ).data

                assert result["client"] == "x.y.FakeClient"
                assert result["operation"] == "summarize_metrics"
                assert isinstance(result["data"], list)
                assert len(result["data"]) == 2

    @pytest.mark.asyncio
    async def test_invoke_oci_api_dns_get_rr_set_allowlist_uses_paginator(self):
        class FakeResponse:
            def __init__(self, data):
                self.data = data
                self.headers = {"opc-request-id": "req-102"}

        class FakeClient:
            def __init__(self, config, signer):
                self.config = config
                self.signer = signer

            # known allowlisted non-list op name
            def get_rr_set(self, zone_name_or_id, domain, rtype):
                return FakeResponse([{"rr": "first"}])

        fake_module = SimpleNamespace(FakeClient=FakeClient)

        with patch(
            "oracle.oci_cloud_mcp_server.server.import_module"
        ) as mock_import, patch(
            "oracle.oci_cloud_mcp_server.server._get_config_and_signer"
        ) as mock_cfg, patch(
            "oracle.oci_cloud_mcp_server.server.oci.pagination.list_call_get_all_results"
        ) as mock_pager:
            mock_import.return_value = fake_module
            mock_cfg.return_value = ({}, object())
            mock_pager.return_value = FakeResponse([{"rr": 1}, {"rr": 2}, {"rr": 3}])

            async with Client(mcp) as client:
                result = (
                    await client.call_tool(
                        "invoke_oci_api",
                        {
                            "client_fqn": "x.y.FakeClient",
                            "operation": "get_rr_set",
                            "params": {
                                "zone_name_or_id": "do-not-delete-me-testing-zone.example",
                                "domain": "www.do-not-delete-me-testing-zone.example",
                                "rtype": "A",
                            },
                        },
                    )
                ).data

                assert result["client"] == "x.y.FakeClient"
                assert result["operation"] == "get_rr_set"
                assert isinstance(result["data"], list)
                assert len(result["data"]) == 3

    @pytest.mark.asyncio
    async def test_invoke_oci_api_non_paginated_does_not_use_paginator(self):
        class FakeResponse:
            def __init__(self, data):
                self.data = data
                self.headers = {"opc-request-id": "req-103"}

        class FakeClient:
            def __init__(self, config, signer):
                self.config = config
                self.signer = signer

            # non-list op, no page/limit params; should not paginate
            def get_config(self, id):
                return FakeResponse({"ok": True, "id": id})

        fake_module = SimpleNamespace(FakeClient=FakeClient)

        with patch(
            "oracle.oci_cloud_mcp_server.server.import_module"
        ) as mock_import, patch(
            "oracle.oci_cloud_mcp_server.server._get_config_and_signer"
        ) as mock_cfg, patch(
            "oracle.oci_cloud_mcp_server.server.oci.pagination.list_call_get_all_results"
        ) as mock_pager:
            mock_import.return_value = fake_module
            mock_cfg.return_value = ({}, object())

            async with Client(mcp) as client:
                result = (
                    await client.call_tool(
                        "invoke_oci_api",
                        {
                            "client_fqn": "x.y.FakeClient",
                            "operation": "get_config",
                            "params": {"id": "abc"},
                        },
                    )
                ).data

                # ensure paginator was not invoked
                mock_pager.assert_not_called()
                assert result["client"] == "x.y.FakeClient"
                assert result["operation"] == "get_config"
                assert result["data"] == {"ok": True, "id": "abc"}

    @pytest.mark.asyncio
    async def test_invoke_oci_api_dns_kwargs_records_uses_paginator(self):
        class FakeResponse:
            def __init__(self, data):
                self.data = data
                self.headers = {"opc-request-id": "req-104"}

        class FakeClient:
            def __init__(self, config, signer):
                self.config = config
                self.signer = signer

            # simulate DNS client where page/limit come via **kwargs only
            def get_zone_records(self, zone_name, **kwargs):  # noqa: ARG002
                return FakeResponse([{"name": "first"}])

        fake_module = SimpleNamespace(FakeClient=FakeClient)

        with patch(
            "oracle.oci_cloud_mcp_server.server.import_module"
        ) as mock_import, patch(
            "oracle.oci_cloud_mcp_server.server._get_config_and_signer"
        ) as mock_cfg, patch(
            "oracle.oci_cloud_mcp_server.server.oci.pagination.list_call_get_all_results"
        ) as mock_pager:
            mock_import.return_value = fake_module
            mock_cfg.return_value = ({}, object())
            mock_pager.return_value = FakeResponse(
                [{"name": "a"}, {"name": "b"}, {"name": "c"}]
            )

            async with Client(mcp) as client:
                result = (
                    await client.call_tool(
                        "invoke_oci_api",
                        {
                            "client_fqn": "x.y.FakeClient",
                            "operation": "get_zone_records",
                            "params": {"zone_name": "example.com."},
                        },
                    )
                ).data

                # should have used paginator due to **kwargs + 'records' name pattern
                assert result["client"] == "x.y.FakeClient"
                assert result["operation"] == "get_zone_records"
                assert isinstance(result["data"], list)
                assert len(result["data"]) == 3

    @pytest.mark.asyncio
    async def test_invoke_oci_api_var_kw_non_dns_does_not_use_paginator(self):
        class FakeResponse:
            def __init__(self, data):
                self.data = data
                self.headers = {"opc-request-id": "req-105"}

        class FakeClient:
            def __init__(self, config, signer):
                self.config = config
                self.signer = signer

            # accepts **kwargs but operation name doesn't indicate records/rrset
            def get_widget(self, widget_id, **kwargs):  # noqa: ARG002
                return FakeResponse({"id": widget_id, "ok": True})

        fake_module = SimpleNamespace(FakeClient=FakeClient)

        with patch(
            "oracle.oci_cloud_mcp_server.server.import_module"
        ) as mock_import, patch(
            "oracle.oci_cloud_mcp_server.server._get_config_and_signer"
        ) as mock_cfg, patch(
            "oracle.oci_cloud_mcp_server.server.oci.pagination.list_call_get_all_results"
        ) as mock_pager:
            mock_import.return_value = fake_module
            mock_cfg.return_value = ({}, object())

            async with Client(mcp) as client:
                result = (
                    await client.call_tool(
                        "invoke_oci_api",
                        {
                            "client_fqn": "x.y.FakeClient",
                            "operation": "get_widget",
                            "params": {"widget_id": "w1"},
                        },
                    )
                ).data

                # ensure paginator was not invoked for **kwargs-only non-DNS-like method
                mock_pager.assert_not_called()
                assert result["client"] == "x.y.FakeClient"
                assert result["operation"] == "get_widget"
                assert result["data"] == {"id": "w1", "ok": True}


class TestGetClientOperationDetailsTool:
    @pytest.mark.asyncio
    async def test_get_client_operation_details_success(self, monkeypatch):
        # define a method with Sphinx-style doc, explicit expected_kwargs, and basic annotations
        def list_things(self, compartment_id: str, page=None, limit=None) -> list:
            """
            List things.

            :param str compartment_id: OCID of compartment
            :returns: Items
            :rtype: list
            """
            expected_kwargs = ["page", "limit"]  # noqa: F841
            return []

        DetailClientX = type("DetailClientX", (), {"list_things": list_things})
        fake_module = SimpleNamespace(DetailClientX=DetailClientX)

        monkeypatch.setattr(
            "oracle.oci_cloud_mcp_server.server.import_module",
            lambda name: fake_module,
        )

        async with Client(mcp) as client:
            res = (
                await client.call_tool(
                    "get_client_operation_details",
                    {
                        "client_fqn": "detail.mod.DetailClientX",
                        "operation": "list_things",
                    },
                )
            ).data

        assert res["client"] == "detail.mod.DetailClientX"
        assert res["operation"] == "list_things"
        assert res["supports_pagination"] is True
        # parameter extraction includes 'compartment_id'
        param_names = {p["name"] for p in res["parameters"]}
        assert "compartment_id" in param_names
        # expected_kwargs should include page/limit
        assert isinstance(res["expected_kwargs"], list) and "page" in res["expected_kwargs"]
        # doc parsing captured params and returns
        assert "compartment_id" in res["doc"]["params"]
        assert res["doc"]["returns"]["type"] in (None, "list", "List")
        # source location best-effort fields present
        assert "source" in res and "file" in res["source"]

    @pytest.mark.asyncio
    async def test_get_client_operation_details_error_payload(self, monkeypatch):
        # class exists but operation missing -> error payload returned by tool
        fake_module = SimpleNamespace(Klass=type("Klass", (), {}))
        monkeypatch.setattr(
            "oracle.oci_cloud_mcp_server.server.import_module",
            lambda name: fake_module,
        )

        async with Client(mcp) as client:
            res = (
                await client.call_tool(
                    "get_client_operation_details",
                    {"client_fqn": "x.y.Klass", "operation": "missing"},
                )
            ).data

        assert "error" in res and "not found" in res["error"]

    @pytest.mark.asyncio
    async def test_get_client_operation_details_parses_google_style_doc(self, monkeypatch):
        # Google-style Args/Returns parsing and expected_kwargs extraction
        def getit(self, name):
            """
            Do it.

            Args:
                name (str): Resource name
            Returns:
                dict: Result mapping
            """
            expected_kwargs = ["limit"]  # noqa: F841
            return {}

        G = type("G", (), {"getit": getit})
        fake_module = SimpleNamespace(G=G)
        monkeypatch.setattr(
            "oracle.oci_cloud_mcp_server.server.import_module",
            lambda name: fake_module,
        )

        async with Client(mcp) as client:
            res = (
                await client.call_tool(
                    "get_client_operation_details",
                    {"client_fqn": "mod.G", "operation": "getit"},
                )
            ).data

        assert res["doc"]["params"]["name"]["description"]
        assert res["doc"]["returns"]["type"] in ("dict", None)
        assert res["supports_pagination"] is True


class TestListClientsTool:
    @pytest.mark.asyncio
    async def test_list_clients_discovers_and_filters(self, monkeypatch):
        # Build fake modules for pkgutil.walk_packages to discover
        foo_mod = SimpleNamespace(__name__="oci.foo")
        bar_mod = SimpleNamespace(__name__="oci.bar")
        models_mod = SimpleNamespace(__name__="oci.bar.models")  # should be skipped

        # Define client classes and assign __module__ so list_clients accepts them
        class FooClient:
            """Foo client summary."""
            pass

        class _PrivateClient:
            pass

        FooClient.__module__ = "oci.foo"
        _PrivateClient.__module__ = "oci.foo"
        setattr(foo_mod, "FooClient", FooClient)
        setattr(foo_mod, "_PrivateClient", _PrivateClient)

        class BarClient:
            """Bar client summary."""
            pass

        BarClient.__module__ = "oci.bar"
        setattr(bar_mod, "BarClient", BarClient)

        # Patch walk_packages to yield our fake module names (including a models package to skip)
        def fake_walk(path, prefix):
            assert prefix == "oci."
            yield SimpleNamespace(name="oci.foo")
            yield SimpleNamespace(name="oci.bar")
            yield SimpleNamespace(name="oci.bar.models")
            yield SimpleNamespace(name="oci._internal")  # private -> skip

        monkeypatch.setattr(
            "oracle.oci_cloud_mcp_server.server.pkgutil.walk_packages", fake_walk
        )

        # Patch import_module to return our fake modules
        def fake_import(name):
            if name == "oci.foo":
                return foo_mod
            if name == "oci.bar":
                return bar_mod
            if name == "oci.bar.models":
                return models_mod
            raise ImportError(name)

        monkeypatch.setattr("oracle.oci_cloud_mcp_server.server.import_module", fake_import)
        # Provide a minimal oci object with __path__ and __name__
        monkeypatch.setattr("oracle.oci_cloud_mcp_server.server.oci", SimpleNamespace(__path__=[], __name__="oci"))

        async with Client(mcp) as client:
            res = (await client.call_tool("list_clients", {})).data

        assert "clients" in res and isinstance(res["clients"], list)
        fqns = [c["fqn"] for c in res["clients"]]
        assert "oci.foo.FooClient" in fqns
        assert "oci.bar.BarClient" in fqns
        # Ensure models and private not included
        assert all("models" not in f for f in fqns)
        assert all(not f.endswith("._PrivateClient") for f in fqns)
        # Deterministic ordering by fqn
        assert fqns == sorted(fqns)

    @pytest.mark.asyncio
    async def test_list_clients_handles_import_errors(self, monkeypatch):
        # walk_packages yields one good, one bad module
        def fake_walk(path, prefix):
            yield SimpleNamespace(name="oci.good")
            yield SimpleNamespace(name="oci.bad")

        good_mod = SimpleNamespace(__name__="oci.good")

        class GoodClient:
            pass

        GoodClient.__module__ = "oci.good"
        setattr(good_mod, "GoodClient", GoodClient)

        monkeypatch.setattr(
            "oracle.oci_cloud_mcp_server.server.pkgutil.walk_packages", fake_walk
        )

        def fake_import(name):
            if name == "oci.good":
                return good_mod
            raise ImportError("boom")

        monkeypatch.setattr("oracle.oci_cloud_mcp_server.server.import_module", fake_import)
        monkeypatch.setattr("oracle.oci_cloud_mcp_server.server.oci", SimpleNamespace(__path__=[], __name__="oci"))

        async with Client(mcp) as client:
            res = (await client.call_tool("list_clients", {})).data

        fqns = [c["fqn"] for c in res.get("clients", [])]
        assert "oci.good.GoodClient" in fqns


class TestDocParseAndHelpers:
    def test_parse_docstring_parameters_and_type_lines(self):
        from oracle.oci_cloud_mcp_server.server import _parse_docstring

        doc = """Summary line.

        Parameters:
            alpha (int): first line
                continued details
        :param beta: beta description
        :type beta: str
        """
        info = _parse_docstring(doc)
        assert info["summary"] == "Summary line."
        assert "alpha" in info["params"]
        assert info["params"]["alpha"]["type"] in ("int", "Int")
        assert "continued details" in info["params"]["alpha"]["description"]
        assert "beta" in info["params"]
        assert info["params"]["beta"]["type"] in ("str", "String")

    def test_parse_docstring_returns_next_line_type_desc(self):
        from oracle.oci_cloud_mcp_server.server import _parse_docstring

        doc = """
        X.

        Returns:
            dict: result mapping
        """
        info = _parse_docstring(doc)
        assert info["returns"]["type"] in ("dict", "Dict")
        assert "result mapping" in info["returns"]["description"]

    def test_extract_expected_kwargs_none_and_empty(self):
        from oracle.oci_cloud_mcp_server.server import _extract_expected_kwargs_from_source

        # builtins generally don't have retrievable source -> None
        assert _extract_expected_kwargs_from_source(len) is None

        # user function without expected_kwargs -> set()
        def f():
            return 1

        out = _extract_expected_kwargs_from_source(f)
        assert isinstance(out, set) and len(out) == 0

    @pytest.mark.asyncio
    async def test_list_clients_top_level_error_payload(self, monkeypatch):
        # Force pkgutil.walk_packages to raise so outer try/except returns {"error": ...}
        def boom(*args, **kwargs):
            raise RuntimeError("explode")

        monkeypatch.setattr(
            "oracle.oci_cloud_mcp_server.server.pkgutil.walk_packages", boom
        )
        # oci.__path__ is still needed when list_clients starts; provide minimal oci
        monkeypatch.setattr(
            "oracle.oci_cloud_mcp_server.server.oci", SimpleNamespace(__path__=[], __name__="oci")
        )

        async with Client(mcp) as client:
            res = (await client.call_tool("list_clients", {})).data
        assert "error" in res and "explode" in res["error"]

    @pytest.mark.asyncio
    async def test_get_client_operation_details_attr_not_callable(self, monkeypatch):
        # Class exists but attribute is not a function/method
        class C:
            x = 1

        fake_module = SimpleNamespace(C=C)
        monkeypatch.setattr(
            "oracle.oci_cloud_mcp_server.server.import_module", lambda name: fake_module
        )

        async with Client(mcp) as client:
            res = (
                await client.call_tool(
                    "get_client_operation_details", {"client_fqn": "mod.C", "operation": "x"}
                )
            ).data

        assert "error" in res
        assert "not a function" in res["error"].lower() or "not a function/method" in res["error"].lower()
