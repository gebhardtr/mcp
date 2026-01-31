from types import SimpleNamespace
import os
import io
import pytest

import oracle.oci_cloud_mcp_server.server as srv
from oracle.oci_cloud_mcp_server.server import _get_config_and_signer, _import_client


def test_get_config_and_signer_uses_security_token_signer_when_token_present(tmp_path, monkeypatch):
    # Create dummy key and token files
    key_file = tmp_path / "oci_api_key.pem"
    key_file.write_text("-----BEGIN PRIVATE KEY-----\nMIIB...\n-----END PRIVATE KEY-----\n")
    token_file = tmp_path / "security_token"
    token_file.write_text("dummy.session.token")

    # Mock OCI config to return our files
    def fake_from_file(profile_name=None):  # noqa: ARG001
        return {
            "tenancy": "ocid1.tenancy.oc1..aaaa",
            "user": "ocid1.user.oc1..bbbb",
            "fingerprint": "aa:bb:cc",
            "key_file": str(key_file),
            "security_token_file": str(token_file),
        }

    monkeypatch.setattr(srv.oci.config, "from_file", fake_from_file, raising=False)
    # Return a dummy private key object
    monkeypatch.setattr(
        srv.oci.signer, "load_private_key_from_file", lambda p: object(), raising=False
    )

    # Capture construction of SecurityTokenSigner and ensure API key Signer is not used
    constructed = {}

    class DummySTS:
        def __init__(self, token, private_key):
            constructed["sts_token"] = token
            constructed["sts_pk"] = private_key

    def boom_signer(*args, **kwargs):  # if called, test should fail
        raise AssertionError("API key Signer should not be used when token signer succeeds")

    monkeypatch.setattr(
        srv.oci.auth.signers, "SecurityTokenSigner", DummySTS, raising=False
    )
    monkeypatch.setattr(srv.oci.signer, "Signer", boom_signer, raising=False)

    cfg, signer = _get_config_and_signer()
    assert isinstance(cfg, dict)
    assert cfg.get("additional_user_agent")
    assert isinstance(signer, DummySTS)
    assert constructed.get("sts_token") == token_file.read_text()


def test_get_config_and_signer_falls_back_to_api_key_signer_when_token_signer_fails(tmp_path, monkeypatch):
    # Create dummy key and token files
    key_file = tmp_path / "oci_api_key.pem"
    key_file.write_text("-----BEGIN PRIVATE KEY-----\nMIIB...\n-----END PRIVATE KEY-----\n")
    token_file = tmp_path / "security_token"
    token_file.write_text("dummy.session.token")

    def fake_from_file(profile_name=None):  # noqa: ARG001
        return {
            "tenancy": "ocid1.tenancy.oc1..aaaa",
            "user": "ocid1.user.oc1..bbbb",
            "fingerprint": "aa:bb:cc",
            "key_file": str(key_file),
            "security_token_file": str(token_file),
            "pass_phrase": None,
        }

    monkeypatch.setattr(srv.oci.config, "from_file", fake_from_file, raising=False)
    monkeypatch.setattr(
        srv.oci.signer, "load_private_key_from_file", lambda p: object(), raising=False
    )

    # Make SecurityTokenSigner raise to force fallback
    def boom_sts(token, private_key):  # noqa: ARG001
        raise RuntimeError("sts failure")

    captured = {}

    class DummyAPIKeySigner:
        def __init__(self, tenancy, user, fingerprint, private_key_file_location, pass_phrase=None):
            captured["tenancy"] = tenancy
            captured["user"] = user
            captured["fp"] = fingerprint
            captured["key_file"] = private_key_file_location
            captured["pass_phrase"] = pass_phrase

    monkeypatch.setattr(srv.oci.auth.signers, "SecurityTokenSigner", boom_sts, raising=False)
    monkeypatch.setattr(srv.oci.signer, "Signer", DummyAPIKeySigner, raising=False)

    cfg, signer = _get_config_and_signer()
    assert isinstance(cfg, dict)
    # Fallback should yield API key signer
    assert isinstance(signer, DummyAPIKeySigner)
    assert captured["tenancy"].startswith("ocid1.tenancy")
    assert captured["user"].startswith("ocid1.user")
    assert captured["fp"] == "aa:bb:cc"
    assert captured["key_file"] == str(key_file)


def test_import_client_raises_when_attribute_is_not_class(monkeypatch):
    # import_module should return our fake module with non-class attribute
    fake_module = SimpleNamespace(notclass=lambda: None)

    monkeypatch.setattr(srv, "import_module", lambda name: fake_module)
    with pytest.raises(ValueError):
        _import_client("mod.notclass")
