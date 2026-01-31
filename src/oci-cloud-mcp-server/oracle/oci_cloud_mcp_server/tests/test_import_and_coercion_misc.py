from types import SimpleNamespace

import pytest

import oracle.oci_cloud_mcp_server.server as srv
from oracle.oci_cloud_mcp_server.server import (
    _import_client,
    _import_models_module_from_client_fqn,
    _resolve_model_class,
    _coerce_params_to_oci_models,
    _call_with_pagination_if_applicable,
)


def test_import_client_requires_fqn_dot():
    with pytest.raises(ValueError):
        _import_client("ComputeClient")  # must be fully-qualified


def test_import_models_module_from_client_fqn_returns_none(monkeypatch):
    # Make importing the models module fail to hit the except -> None path
    def fake_import(name):
        raise ImportError(name)

    monkeypatch.setattr(srv, "import_module", fake_import)
    out = _import_models_module_from_client_fqn("x.y.Client")
    assert out is None


def test_resolve_model_class_none_when_missing():
    fake_mod = SimpleNamespace()  # no attribute
    out = _resolve_model_class(fake_mod, "MissingClass")
    assert out is None


def test_coerce_params_list_items_only_construct_when_hint_present(monkeypatch):
    # Provide a fake models module with class X
    class X:
        def __init__(self, **kwargs):
            self._data = dict(kwargs)

    fake_models = SimpleNamespace(X=X)

    # Ensure model resolution uses our fake models module
    monkeypatch.setattr(
        "oracle.oci_cloud_mcp_server.server._import_models_module_from_client_fqn",
        lambda fqn: fake_models,
    )

    params = {
        "items": [
            {"a": 1},  # no explicit hint -> should stay a dict
            {"__model": "X", "b": 2},  # has explicit hint -> construct X
        ]
    }

    out = _coerce_params_to_oci_models("x.y.Client", "op_name", params)
    assert isinstance(out["items"][0], dict) and out["items"][0]["a"] == 1
    assert isinstance(out["items"][1], X)
    assert out["items"][1]._data == {"b": 2}


def test_call_with_pagination_if_applicable_non_data_response():
    # Operation not paginated; method returns plain list (no .data attr)
    def get_widget(widget_id=None):  # noqa: ARG001
        return [{"id": 1}]

    data, opc = _call_with_pagination_if_applicable(
        get_widget, {}, operation_name="get_widget"
    )
    assert isinstance(data, list) and data == [{"id": 1}]
    assert opc is None


def test_call_with_pagination_typeerror_alias_retry_for_create_ops():
    # Method signature only accepts create_thing_details (SDK expected kw)
    class Response:
        def __init__(self, data):
            self.data = data
            self.headers = {"opc-request-id": "alias-req"}

    class C:
        def create_thing(self, create_thing_details):
            return Response({"ok": True, "x": create_thing_details.get("x")})

    c = C()
    # Pass user-style "thing_details" to trigger TypeError then alias retry
    data, opc = _call_with_pagination_if_applicable(
        c.create_thing, {"thing_details": {"x": 5}}, operation_name="create_thing"
    )
    assert data == {"ok": True, "x": 5}
    assert opc == "alias-req"
