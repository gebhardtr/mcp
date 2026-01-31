import inspect
import pytest

import oracle.oci_cloud_mcp_server.server as srv
from oracle.oci_cloud_mcp_server.server import (
    _supports_pagination,
    _align_params_to_signature,
)


def test_supports_pagination_fallback_allowlist_introspection_error(monkeypatch):
    """
    Force inspect.signature to raise so _supports_pagination hits its exception path
    and falls back to the known_paginated allowlist for the decision.
    """

    def method():
        """No pagination hints here."""
        return None

    orig_signature = inspect.signature

    def fake_signature(obj):
        if obj is method:
            raise RuntimeError("sig boom")
        return orig_signature(obj)

    monkeypatch.setattr(inspect, "signature", fake_signature)

    # In the fallback, only allowlist names return True
    assert _supports_pagination(method, "get_domain_records") is True
    assert _supports_pagination(method, "not_paginated") is False


def test_align_params_to_signature_signature_failure_passthrough(monkeypatch):
    """
    When inspect.signature fails, _align_params_to_signature should return params unchanged.
    """

    def op(x, y):  # noqa: ARG001
        return None

    orig_signature = inspect.signature

    def fake_signature(obj):
        if obj is op:
            raise ValueError("no sig")
        return orig_signature(obj)

    monkeypatch.setattr(inspect, "signature", fake_signature)
    params = {"some_key": 123, "other": "abc"}
    out = _align_params_to_signature(op, "create_resource", params)
    # passthrough exact object when signature introspection fails
    assert out is params


def test_docstring_mentions_pagination_false():
    """
    _docstring_mentions_pagination should return False when docstring lacks page/limit keywords.
    """

    def f():
        """No pagination hints here."""
        return None

    assert srv._docstring_mentions_pagination(f) is False
