from types import SimpleNamespace
import oracle.oci_cloud_mcp_server.server as srv
from oracle.oci_cloud_mcp_server.server import _coerce_params_to_oci_models


def test_construct_model_from_mapping_parent_prefix_fallback_coerces_non_details_variant(monkeypatch):
    """
    Exercise the fallback path added to _construct_model_from_mapping where:
      - A target model class is resolved (e.g., InstanceDetails)
      - oci.util.from_dict raises, forcing the new fallback
      - Fallback coerces nested mapping values using a parent prefix hint and
        prefers the non-Details variant when available (e.g., InstanceShapeConfig)
      - The resolved class is constructed with the coerced mapping
    """

    class InstanceDetails:
        def __init__(self, **kwargs):
            self._data = dict(kwargs)

    class InstanceShapeConfig:
        def __init__(self, **kwargs):
            self._data = dict(kwargs)

    # Provide only InstanceDetails and the non-Details nested class to confirm selection
    fake_models = SimpleNamespace(
        InstanceDetails=InstanceDetails,
        InstanceShapeConfig=InstanceShapeConfig,
    )

    # Ensure the models module for any client FQN is our fake module
    monkeypatch.setattr(
        "oracle.oci_cloud_mcp_server.server._import_models_module_from_client_fqn",
        lambda fqn: fake_models,
    )

    # Force from_dict to fail so we take the parent-prefix fallback path
    def _boom_from_dict(cls, data):
        raise RuntimeError("boom")

    monkeypatch.setattr(srv.oci.util, "from_dict", _boom_from_dict, raising=False)

    params = {
        "instance_details": {
            "shape_config": {"ocpus": 1}
        }
    }

    out = _coerce_params_to_oci_models(
        "oci.any.Client",
        "launch_instance",  # not create_/update_, relies on parent prefix from 'InstanceDetails'
        params,
    )

    inst = out["instance_details"]
    assert isinstance(inst, InstanceDetails)
    assert "shape_config" in inst._data
    # Nested mapping should be coerced into InstanceShapeConfig (non-Details variant)
    assert isinstance(inst._data["shape_config"], InstanceShapeConfig)
    assert inst._data["shape_config"]._data["ocpus"] == 1
