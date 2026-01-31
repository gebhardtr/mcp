import oracle.oci_cloud_mcp_server.server as srv
from oracle.oci_cloud_mcp_server.server import _coerce_params_to_oci_models


def test_coerce_params_final_aliasing_create():
    """
    For create_* operations, ensure trailing aliasing renames '<resource>_details'
    to 'create_<resource>_details' even when no models module is available.
    """
    params = {"bucket_details": {"name": "n"}}
    out = _coerce_params_to_oci_models("x.y.Client", "create_bucket", params)
    assert "create_bucket_details" in out and "bucket_details" not in out
    assert out["create_bucket_details"] == {"name": "n"}


def test_extract_expected_kwargs_from_source_absent_returns_empty_set():
    """
    When method source is available but contains no 'expected_kwargs' pattern,
    the helper should return an empty set.
    """

    def f(a, b):  # noqa: ARG001
        """No expected kwargs here."""
        return None

    res = srv._extract_expected_kwargs_from_source(f)
    assert isinstance(res, set)
    assert res == set()


def test_parse_docstring_returns_inline_type_and_desc():
    """
    Google-style 'Returns: type: desc' on the same line should be parsed into type and description.
    """
    doc = """Summary line

Returns: List[str]: a list of names
"""
    out = srv._parse_docstring(doc)
    assert out["summary"] == "Summary line"
    assert out["returns"]["type"] == "List[str]"
    assert out["returns"]["description"] == "a list of names"


def test_parse_docstring_returns_next_line_type_and_desc():
    """
    Google-style 'Returns:' on one line and 'type: desc' on the next line should be parsed correctly.
    """
    doc = """Another summary

Returns:
    Dict[str, int]: mapping of counts
"""
    out = srv._parse_docstring(doc)
    assert out["summary"] == "Another summary"
    assert out["returns"]["type"] == "Dict[str, int]"
    assert out["returns"]["description"] == "mapping of counts"
