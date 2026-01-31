import re
import pytest

import oracle.oci_cloud_mcp_server.server as srv
from oracle.oci_cloud_mcp_server.server import _extract_expected_kwargs_from_source, _call_with_pagination_if_applicable


def test_extract_expected_kwargs_from_source_regex_exception(monkeypatch):
    """
    Force the regex search in _extract_expected_kwargs_from_source to raise so the
    exception handler returns None (distinct from empty set()).
    """

    def f():
        return 1

    # Replace re.search within the module to raise an exception
    def boom_search(pattern, string, flags=0):  # noqa: ARG001
        raise RuntimeError("regex engine failure")

    monkeypatch.setattr(srv, "re", srv.re)
    monkeypatch.setattr(srv.re, "search", boom_search)
    out = _extract_expected_kwargs_from_source(f)
    assert out is None


def test_call_with_pagination_typeerror_unexpected_kw_raises():
    """
    Exercise the except TypeError path in _call_with_pagination_if_applicable where the
    unexpected keyword does not match the expected '<resource>_details', causing a re-raise.
    """

    class C:
        def create_thing(self, create_thing_details):
            return {"ok": True, "x": create_thing_details.get("x")}  # pragma: no cover

    c = C()
    with pytest.raises(TypeError):
        _call_with_pagination_if_applicable(
            c.create_thing, {"other_details": {"x": 1}}, operation_name="create_thing"
        )
