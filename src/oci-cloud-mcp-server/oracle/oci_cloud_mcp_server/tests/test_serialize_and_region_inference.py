import typing
from types import SimpleNamespace

import pytest

import oracle.oci_cloud_mcp_server.server as srv
from oracle.oci_cloud_mcp_server.server import (
    _serialize_oci_data,
    _maybe_set_client_region,
)


def test_serialize_oci_data_to_dict_raises_then_str_fallback(monkeypatch):
    """
    When oci.util.to_dict raises, _serialize_oci_data should fall back to ensure_jsonable,
    which converts non-JSON-serializable objects to str(obj).
    """

    class Bad:
        def __str__(self):
            return "<Bad>"

    def boom_to_dict(_obj):
        raise RuntimeError("to_dict failed")

    # Force to_dict to raise to exercise the exception path
    monkeypatch.setattr(srv.oci.util, "to_dict", boom_to_dict, raising=False)

    b = Bad()
    out = _serialize_oci_data(b)
    assert out == "<Bad>"


def test_maybe_set_client_region_infers_from_ocid_code():
    """
    _maybe_set_client_region should infer region from OCID's short region code (e.g., '.iad.')
    and call base_client.set_region with the mapped full region name.
    """

    called = {}

    class Base:
        def set_region(self, region):
            called["region"] = region

    class FakeClient:
        def __init__(self):
            self.base_client = Base()

    client = FakeClient()

    # OCID string containing '.iad.' -> us-ashburn-1
    params = {
        "compartment_id": "ocid1.compartment.oc1.iad.someuniqueid",
    }
    _maybe_set_client_region(client, params)
    assert called.get("region") == "us-ashburn-1"


def test_maybe_set_client_region_infers_from_availability_domain_name():
    """
    _maybe_set_client_region should map an availability domain name like 'US-ASHBURN-AD-1'
    to the corresponding region and call set_region.
    """

    called = {}

    class Base:
        def set_region(self, region):
            called["region"] = region

    class FakeClient:
        def __init__(self):
            self.base_client = Base()

    client = FakeClient()

    params = {"availability_domain": "US-ASHBURN-AD-1"}
    _maybe_set_client_region(client, params)
    assert called.get("region") == "us-ashburn-1"


def test_maybe_set_client_region_noop_when_no_base_client_or_region_hints():
    """
    If client lacks base_client/set_region or no hints exist, _maybe_set_client_region is a no-op.
    Ensure it does not raise and does not attempt to call anything.
    """

    class ClientNoBase:
        pass

    c = ClientNoBase()
    _maybe_set_client_region(c, {"unrelated": "value"})  # should not raise
