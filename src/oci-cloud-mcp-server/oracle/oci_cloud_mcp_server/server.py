"""
Copyright (c) 2026, Oracle and/or its affiliates.
Licensed under the Universal Permissive License v1.0 as shown at
https://oss.oracle.com/licenses/upl.
"""

import inspect
import json
import os
import re
import pkgutil
from importlib import import_module
from logging import Logger
from typing import Annotated, Any, Callable, Dict, List, Optional, Tuple

import oci
from fastmcp import FastMCP

from . import __project__, __version__
from .utils import initAuditLogger

logger = Logger(__name__, level="INFO")

initAuditLogger(logger)

mcp = FastMCP(
    name=__project__,
    instructions="""
        This server provides tools to interact directly with the OCI Python SDK,
        invoking API clients and operations in-process (no CLI).
        - invoke_oci_api: Call any OCI SDK client operation by FQN and method.
        - list_client_operations: Discover available operations on a client.
        - list_clients: List available OCI SDK clients and their short descriptions.
        - get_client_operation_details: Inspect a client's operation to return signature, parameter details, docstrings, return info, pagination support, and source location.
    """,
)

_user_agent_name = __project__.split("oracle.", 1)[1].split("-server", 1)[0]
_ADDITIONAL_UA = f"{_user_agent_name}/{__version__}"


def _get_config_and_signer() -> Tuple[Dict[str, Any], Any]:
    """
    Load OCI config and build an appropriate signer.

    Preference order:
    - If a security_token_file exists, use SecurityTokenSigner (session auth).
    - Otherwise, fall back to API key Signer from config.
    """
    config = oci.config.from_file(
        profile_name=os.getenv("OCI_CONFIG_PROFILE", oci.config.DEFAULT_PROFILE)
    )
    config["additional_user_agent"] = _ADDITIONAL_UA

    # try security token
    token_file = os.path.expanduser(config.get("security_token_file", "") or "")
    try:
        private_key = oci.signer.load_private_key_from_file(config["key_file"])
    except Exception as e:
        logger.error(f"Failed loading private key: {e}")
        raise

    signer = None
    if token_file and os.path.exists(token_file):
        try:
            with open(token_file, "r") as f:
                token = f.read()
            signer = oci.auth.signers.SecurityTokenSigner(token, private_key)
            logger.info("Using SecurityTokenSigner from security_token_file.")
        except Exception as e:
            logger.warning(
                f"Failed to build SecurityTokenSigner from token file, will try API key signer: {e}"
            )

    if signer is None:
        # fall back to API key signer
        try:
            signer = oci.signer.Signer(
                tenancy=config["tenancy"],
                user=config["user"],
                fingerprint=config["fingerprint"],
                private_key_file_location=config["key_file"],
                pass_phrase=config.get("pass_phrase"),
            )
            logger.info("Using API key Signer.")
        except Exception as e:
            logger.error(f"Failed to build API key Signer: {e}")
            raise

    return config, signer


def _import_client(client_fqn: str) -> Any:
    """
    Import and instantiate an OCI SDK Client given a fully-qualified class name.
    Example: 'oci.core.ComputeClient'
    """
    if "." not in client_fqn:
        raise ValueError(
            "client_fqn must be a fully-qualified class name like 'oci.core.ComputeClient'"
        )
    module_name, class_name = client_fqn.rsplit(".", 1)
    module = import_module(module_name)
    cls = getattr(module, class_name)
    if not inspect.isclass(cls):
        raise ValueError(f"{client_fqn} is not a class")
    config, signer = _get_config_and_signer()
    instance = cls(config, signer=signer)
    return instance


def _snake_to_camel(name: str) -> str:
    parts = name.split("_")
    return "".join(p.capitalize() for p in parts if p)


def _import_models_module_from_client_fqn(client_fqn: str):
    try:
        pkg = client_fqn.rsplit(".", 1)[0]
        return import_module(f"{pkg}.models")
    except Exception:
        return None


def _resolve_model_class(models_module: Any, class_name: str):
    try:
        return getattr(models_module, class_name)
    except Exception:
        return None


def _coerce_mapping_values(
    mapping: Dict[str, Any], models_module: Any, parent_prefix: Optional[str] = None
) -> Dict[str, Any]:
    """
    Recursively coerce nested dict/list values inside a mapping into OCI SDK model instances.
    Additionally, when a parent model prefix is known (e.g., 'LaunchInstance' for
    LaunchInstanceDetails), attempt prefixed candidate class names for nested fields,
    such as 'LaunchInstance' + CamelCase(key) + 'Details'. This helps resolve cases like
    shape_config -> LaunchInstanceShapeConfigDetails.
    """
    suffixes = ("_details", "_config", "_configuration", "_source_details")
    out: Dict[str, Any] = {}
    for key, val in mapping.items():
        if isinstance(val, dict):
            candidates: List[str] = []
            for s in suffixes:
                if key.endswith(s):
                    base_camel = _snake_to_camel(key)
                    candidates.append(base_camel)
                    # also try parent-prefixed variants like 'LaunchInstanceShapeConfigDetails'
                    if parent_prefix:
                        candidates.append(f"{parent_prefix}{base_camel}Details")
                        candidates.append(f"{parent_prefix}{base_camel}")
                    break
            out[key] = _construct_model_from_mapping(val, models_module, candidates)
        elif isinstance(val, list):
            new_list: List[Any] = []
            for item in val:
                if isinstance(item, dict):
                    new_list.append(
                        _construct_model_from_mapping(item, models_module, [])
                    )
                else:
                    new_list.append(item)
            out[key] = new_list
        else:
            out[key] = val
    return out


def _construct_model_from_mapping(
    mapping: Dict[str, Any], models_module: Any, candidate_classnames: List[str]
):
    # explicit override via magic keys
    fqn = mapping.get("__model_fqn") or mapping.get("__class_fqn")
    class_name = mapping.get("__model") or mapping.get("__class")
    clean = {k: v for k, v in mapping.items() if not k.startswith("__")}

    # First attempt: resolve a target model class (without coercing nested values),
    # then let oci.util.from_dict handle nested construction and polymorphic subclasses.
    resolved_cls = None

    # try explicit FQN first
    if isinstance(fqn, str):
        try:
            mod_name, cls_name = fqn.rsplit(".", 1)
            mod = import_module(mod_name)
            cand_cls = getattr(mod, cls_name)
            if inspect.isclass(cand_cls):
                resolved_cls = cand_cls
        except Exception:
            resolved_cls = None

    # try explicit simple class name within models module
    if resolved_cls is None and models_module and isinstance(class_name, str):
        cand_cls = _resolve_model_class(models_module, class_name)
        if inspect.isclass(cand_cls):
            resolved_cls = cand_cls

    # try candidates derived from param name
    if resolved_cls is None and models_module:
        for cand in candidate_classnames:
            cand_cls = _resolve_model_class(models_module, cand)
            if inspect.isclass(cand_cls):
                resolved_cls = cand_cls
                break

    if resolved_cls is not None:
        # filter unknown keys via swagger_types when available,
        # then rely on from_dict to recursively build nested models and select subclasses via get_subtype
        filtered_clean = clean
        try:
            swagger_types = getattr(resolved_cls, "swagger_types", None)
            if isinstance(swagger_types, dict):
                filtered_clean = {k: v for k, v in clean.items() if k in swagger_types}
        except Exception:
            filtered_clean = clean
        try:
            return oci.util.from_dict(resolved_cls, filtered_clean)
        except Exception:
            # If from_dict fails (e.g., no swagger_types to guide nested coercion),
            # coerce nested mapping values using parent prefix hints before calling the constructor.
            parent_prefix_hint: Optional[str] = None
            for cand in candidate_classnames:
                if isinstance(cand, str) and cand:
                    if cand.endswith("Details"):
                        parent_prefix_hint = cand[: -len("Details")]
                        break
                    if parent_prefix_hint is None:
                        parent_prefix_hint = cand
            coerced_filtered = _coerce_mapping_values(filtered_clean, models_module, parent_prefix=parent_prefix_hint)
            try:
                return resolved_cls(**coerced_filtered)
            except Exception:
                pass

    # If we could not resolve the target class yet, fall back to heuristic coercion for nested values,
    # then retry candidate resolution on the coerced mapping.
    # derive a parent model prefix hint from candidate classnames (e.g., 'LaunchInstance' from 'LaunchInstanceDetails')
    parent_prefix_hint: Optional[str] = None
    for cand in candidate_classnames:
        if isinstance(cand, str) and cand:
            if cand.endswith("Details"):
                parent_prefix_hint = cand[: -len("Details")]
                break
            if parent_prefix_hint is None:
                parent_prefix_hint = cand

    coerced_clean = _coerce_mapping_values(clean, models_module, parent_prefix=parent_prefix_hint)

    # Retry FQN
    if isinstance(fqn, str):
        try:
            mod_name, cls_name = fqn.rsplit(".", 1)
            mod = import_module(mod_name)
            cand_cls = getattr(mod, cls_name)
            if inspect.isclass(cand_cls):
                try:
                    return oci.util.from_dict(cand_cls, coerced_clean)
                except Exception:
                    return cand_cls(**coerced_clean)
        except Exception:
            pass

    # Retry simple class name
    if models_module and isinstance(class_name, str):
        cand_cls = _resolve_model_class(models_module, class_name)
        if inspect.isclass(cand_cls):
            filtered_clean = coerced_clean
            try:
                swagger_types = getattr(cand_cls, "swagger_types", None)
                if isinstance(swagger_types, dict):
                    filtered_clean = {k: v for k, v in coerced_clean.items() if k in swagger_types}
            except Exception:
                filtered_clean = coerced_clean
            try:
                return oci.util.from_dict(cand_cls, filtered_clean)
            except Exception:
                try:
                    return cand_cls(**filtered_clean)
                except Exception:
                    pass

    # Retry candidate list
    if models_module:
        for cand in candidate_classnames:
            cand_cls = _resolve_model_class(models_module, cand)
            if inspect.isclass(cand_cls):
                filtered_clean = coerced_clean
                try:
                    swagger_types = getattr(cand_cls, "swagger_types", None)
                    if isinstance(swagger_types, dict):
                        filtered_clean = {k: v for k, v in coerced_clean.items() if k in swagger_types}
                except Exception:
                    filtered_clean = coerced_clean
                try:
                    return oci.util.from_dict(cand_cls, filtered_clean)
                except Exception:
                    try:
                        return cand_cls(**filtered_clean)
                    except Exception:
                        continue

    # fall back to original mapping
    return mapping


def _coerce_params_to_oci_models(
    client_fqn: str, operation: str, params: Dict[str, Any]
) -> Dict[str, Any]:
    """
    Convert plain dict/list params to OCI model instances when appropriate.
    Heuristics:
      - If a dict contains '__model_fqn' or '__model'/'__class', build that model.
      - If a param name ends with typical model suffixes (e.g., *_details), attempt to
        construct the corresponding CamelCase model class from the client's models module.
    """
    if not params:
        return {}
    models_module = _import_models_module_from_client_fqn(client_fqn)
    suffixes = ("_details", "_config", "_configuration", "_source_details")
    out: Dict[str, Any] = {}
    for key, val in params.items():
        if isinstance(val, dict):
            candidates: List[str] = []
            dest_key = key
            for s in suffixes:
                if key.endswith(s):
                    candidates.append(_snake_to_camel(key))
                    if s == "_details":
                        base = _snake_to_camel(key[: -len(s)])
                        # try verb-specific model classes if this is a create_/update_ op
                        if operation.startswith("create_"):
                            candidates.append(f"Create{base}Details")
                        elif operation.startswith("update_"):
                            candidates.append(f"Update{base}Details")
                        # rename "<resource>_details" to the SDK's expected
                        # "create_<resource>_details"/"update_<resource>_details"
                        if operation.startswith("create_") or operation.startswith(
                            "update_"
                        ):
                            _, _, op_rest = operation.partition("_")
                            if key == f"{op_rest}_details":
                                # only rename to SDK-expected key when destination not already
                                # provided by caller
                                alt_key = f"{operation}_details"
                                if alt_key not in params:
                                    dest_key = alt_key
                    break
            out[dest_key] = _construct_model_from_mapping(
                val, models_module, candidates
            )
        elif isinstance(val, list):
            new_list: List[Any] = []
            for item in val:
                if isinstance(item, dict):
                    # only construct list items if explicit model hint is present
                    if any(
                        hint in item
                        for hint in ("__model_fqn", "__model", "__class_fqn", "__class")
                    ):
                        new_list.append(
                            _construct_model_from_mapping(item, models_module, [])
                        )
                    else:
                        new_list.append(item)
                else:
                    new_list.append(item)
            out[key] = new_list
        else:
            out[key] = val
    # final aliasing: if caller used "<resource>_details" and op is create_/update_,
    # rename to the SDK-expected "create_<resource>_details"/"update_<resource>_details".
    if operation.startswith("create_") or operation.startswith("update_"):
        _, _, op_rest = operation.partition("_")
        src = f"{op_rest}_details"
        dst = f"{operation}_details"
        if src in out and dst not in out:
            out[dst] = out.pop(src)
    return out


def _align_params_to_signature(
    method: Callable[..., Any], operation_name: str, params: Dict[str, Any]
) -> Dict[str, Any]:
    """
    Align incoming params to the method's expected signature names.
    Specifically, for create_/update_ operations remap '<resource>_details' to
    'create_<resource>_details'/'update_<resource>_details' when the latter exists
    in the target method signature.
    """
    try:
        sig = inspect.signature(method)
        param_names = set(sig.parameters.keys())
    except Exception:
        return params

    aligned = dict(params)
    if operation_name.startswith("create_") or operation_name.startswith("update_"):
        _, _, op_rest = operation_name.partition("_")
        src = f"{op_rest}_details"
        dst = f"{operation_name}_details"
        if src in aligned and dst not in aligned and dst in param_names:
            aligned[dst] = aligned.pop(src)
    return aligned


def _serialize_oci_data(data: Any) -> Any:
    """
    Convert OCI SDK model objects or collections into JSON-serializable structures.
    Ensures the final result is JSON-serializable even if oci.util.to_dict returns the original object.
    """

    def ensure_jsonable(obj: Any) -> Any:
        if obj is None or isinstance(obj, (str, int, float, bool)):
            return obj
        if isinstance(obj, (list, tuple)):
            return [ensure_jsonable(x) for x in obj]
        if isinstance(obj, dict):
            return {k: ensure_jsonable(v) for k, v in obj.items()}
        try:
            json.dumps(obj)
            return obj
        except Exception:
            return str(obj)

    try:
        converted = oci.util.to_dict(data)
    except Exception:
        converted = data
    return ensure_jsonable(converted)


def _extract_expected_kwargs_from_source(method: Callable[..., Any]) -> Optional[set]:
    """
    Best-effort extraction of the SDK generator's 'expected_kwargs' list from the method source.
    Returns a set of kwarg names if found, None if source cannot be retrieved, or empty set if
    the pattern isn't present.
    """
    try:
        src = inspect.getsource(method)
    except Exception:
        return None
    try:
        m = re.search(r"expected_kwargs\s*=\s*\[\s*(.*?)\s*\]", src, re.DOTALL)
        if not m:
            return set()
        body = m.group(1)
        kws = set(re.findall(r"['\"]([a-zA-Z0-9_]+)['\"]", body))
        return kws
    except Exception:
        return None


def _docstring_mentions_pagination(method: Callable[..., Any]) -> bool:
    """
    Inspect a method's docstring to detect pagination-related parameters
    that may be documented even if only accepted via **kwargs, such as
    ':param int limit:' or ':param str page:'.
    """
    try:
        doc = inspect.getdoc(method) or ""
    except Exception:
        return False
    # look for common Sphinx/ReST patterns or plain mentions of parameter names
    return bool(re.search(r"\b(page|limit)\b", doc))


def _supports_pagination(method: Callable[..., Any], operation_name: str) -> bool:
    """
    Determine if an operation is paginated and should use the OCI paginator.
    Heuristics:
      - Operation name starts with 'list_' (standard OCI pattern).
      - Operation name starts with 'summarize_' (many summarize ops are paginated).
      - Method signature includes 'page' or 'limit' kwargs (explicit params).
      - Method accepts **kwargs and operation name indicates record/rrset style (DNS-like).
      - Explicit allowlist for known paginated non-list ops in services like DNS.
    """
    # known paginated non-list operations (e.g., DNS)
    known_paginated = {
        "get_zone_records",
        "get_domain_records",
        "get_rr_set",
        "get_rr_set",
    }
    try:
        if operation_name.startswith("list_"):
            return True
        if operation_name.startswith("summarize_"):
            return True

        # detect SDK-generated kwargs list that includes pagination tokens even when only exposed via **kwargs
        ek = _extract_expected_kwargs_from_source(method)
        if ek and (("page" in ek) or ("limit" in ek)):
            return True

        # inspect docstring for pagination-related params exposed in docs
        # (e.g., ':param int limit:' or ':param str page:')
        try:
            if _docstring_mentions_pagination(method):
                return True
        except Exception:
            pass

        sig = inspect.signature(method)
        param_names = set(sig.parameters.keys())
        if "page" in param_names or "limit" in param_names:
            return True
        # some SDKs (e.g., DNS) expose 'page'/'limit' via **kwargs; detect via VAR_KEYWORD and name pattern
        has_var_kw = any(
            p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()
        )
        if has_var_kw:
            if operation_name in known_paginated:
                return True
            # generic DNS-like pattern: operations dealing with records/rrsets tend to paginate
            if re.search(r"(records|rrset)s?$", operation_name):
                return True
    except Exception:
        # if we cannot introspect, fall through to explicit allowlist
        pass

    return operation_name in known_paginated


def _call_with_pagination_if_applicable(
    method: Callable[..., Any], params: Dict[str, Any], operation_name: str
) -> Tuple[Any, Optional[str]]:
    """
    If the operation appears to be paginated, use the OCI paginator to get all results.
    Returns (data, opc_request_id).
    """
    if _supports_pagination(method, operation_name):
        logger.info(f"Using paginator for operation {operation_name}")
        response = oci.pagination.list_call_get_all_results(method, **params)
        opc_request_id = None
        try:
            opc_request_id = response.headers.get("opc-request-id")
        except Exception:
            opc_request_id = None
        return response.data, opc_request_id

    # non-list operation; pre-alias known kwarg patterns and invoke, with fallback aliasing
    call_params = dict(params)
    if operation_name.startswith("create_") or operation_name.startswith("update_"):
        _, _, op_rest = operation_name.partition("_")
        src = f"{op_rest}_details"
        dst = f"{operation_name}_details"
        if src in call_params and dst not in call_params:
            call_params[dst] = call_params.pop(src)

    try:
        logger.debug(
            f"_call_with_pagination_if_applicable call_params keys: {list(call_params.keys())}"
        )
        logger.debug(f"op: {operation_name}")
        response = method(**call_params)
    except TypeError as e:
        # fallback: if user passed "<resource>_details" for a create_/update_ op,
        # retry with "create_<resource>_details"/"update_<resource>_details"
        msg = str(e)
        if "unexpected keyword argument" in msg and (
            operation_name.startswith("create_") or operation_name.startswith("update_")
        ):
            try:
                bad_kw = msg.split("'")[1]
                _, _, op_rest = operation_name.partition("_")
                expected_src = f"{op_rest}_details"
                if bad_kw == expected_src and expected_src in call_params:
                    alt_key = f"{operation_name}_details"
                    new_params = dict(call_params)
                    new_params[alt_key] = new_params.pop(expected_src)
                    response = method(**new_params)
                else:
                    raise
            except Exception:
                raise
        else:
            raise

    # most OCI SDK methods return oci.response.Response with .data and .headers
    if hasattr(response, "data"):
        data = response.data
    else:
        data = response
    opc_request_id = None
    try:
        opc_request_id = response.headers.get("opc-request-id")
    except Exception:
        opc_request_id = None
    return data, opc_request_id


def _iter_strings(obj):
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(k, str):
                yield k
            yield from _iter_strings(v)
    elif isinstance(obj, (list, tuple, set)):
        for item in obj:
            yield from _iter_strings(item)


def _region_from_code(code: str) -> Optional[str]:
    code = (code or "").lower()
    mapping = {
        "iad": "us-ashburn-1",
        "phx": "us-phoenix-1",
        "sjc": "us-sanjose-1",
        "lhr": "uk-london-1",
        "fra": "eu-frankfurt-1",
        "ams": "eu-amsterdam-1",
        "bom": "ap-mumbai-1",
        "hyd": "ap-hyderabad-1",
        "nrt": "ap-tokyo-1",
        "icn": "ap-seoul-1",
        "yny": "ap-chuncheon-1",
        "mel": "ap-melbourne-1",
        "syd": "ap-sydney-1",
        "gru": "sa-saopaulo-1",
        "scl": "sa-santiago-1",
        "jed": "me-jeddah-1",
        "auh": "me-abudhabi-1",
        "zrh": "eu-zurich-1",
        "cdg": "eu-paris-1",
        "mad": "eu-madrid-1",
    }
    return mapping.get(code)


def _infer_region_from_params(params: Dict[str, Any]) -> Optional[str]:
    # 1) Look for region code embedded in OCIDs like 'oc1.<regioncode>.' e.g., '.iad.'
    for s in _iter_strings(params):
        m = re.search(r"\.([a-z]{3})\.", s)
        if m:
            region = _region_from_code(m.group(1))
            if region:
                return region
    # 2) Look for availability domain names e.g., 'US-ASHBURN-AD-1'
    ad = params.get("availability_domain")
    if isinstance(ad, str):
        up = ad.upper()
        if "ASHBURN" in up:
            return "us-ashburn-1"
        if "PHOENIX" in up:
            return "us-phoenix-1"
        if "SANJOSE" in up or "SAN JOSE" in up:
            return "us-sanjose-1"
    return None


def _maybe_set_client_region(client: Any, params: Dict[str, Any]) -> None:
    try:
        region = _infer_region_from_params(params or {})
        if region:
            base = getattr(client, "base_client", None)
            if base and hasattr(base, "set_region"):
                base.set_region(region)
    except Exception:
        # best-effort only
        pass


@mcp.tool(description="Invoke an OCI Python SDK API via client and operation name.")
def invoke_oci_api(
    client_fqn: Annotated[
        str, "Fully-qualified client class, e.g. 'oci.core.ComputeClient'"
    ],
    operation: Annotated[
        str, "Client method/operation name, e.g. 'list_instances' or 'get_instance'"
    ],
    params: Annotated[
        Dict[str, Any],
        "Keyword arguments for the operation (JSON object). Use snake_case keys as in SDK.",
    ] = {},
) -> dict:
    """
    Example:
      client_fqn='oci.core.ComputeClient'
      operation='list_instances'
      params={'compartment_id': '<ocid>', 'availability_domain': '...'}
    """
    try:
        client = _import_client(client_fqn)
        # Best-effort: set client region based on hints in params (OCIDs or availability domain)
        try:
            _maybe_set_client_region(client, params or {})
        except Exception:
            pass
        if not hasattr(client, operation):
            raise AttributeError(
                f"Operation '{operation}' not found on client '{client_fqn}'"
            )

        method = getattr(client, operation)
        if not callable(method):
            raise AttributeError(
                f"Attribute '{operation}' on client '{client_fqn}' is not callable"
            )

        params = params or {}
        # pre-normalize parameter key to the SDK-expected kw for create_/update_ ops,
        # so downstream coercion and invocation consistently use the correct key.
        normalized_params = dict(params)
        if operation.startswith("create_") or operation.startswith("update_"):
            _, _, op_rest = operation.partition("_")
            src = f"{op_rest}_details"
            dst = f"{operation}_details"
            if src in normalized_params and dst not in normalized_params:
                normalized_params[dst] = normalized_params.pop(src)

        coerced_params = _coerce_params_to_oci_models(
            client_fqn, operation, normalized_params
        )
        # final kwarg aliasing at the top-level prior to invocation to ensure correct SDK kw
        final_params = dict(coerced_params)

        final_params = _align_params_to_signature(method, operation, final_params)
        logger.debug(f"invoke_oci_api final_params keys: {list(final_params.keys())}")
        logger.debug(f"op: {operation}")
        try:
            data, opc_request_id = _call_with_pagination_if_applicable(
                method, final_params, operation
            )
        except TypeError as e:
            msg = str(e)
            if "unexpected keyword argument" in msg and (
                operation.startswith("create_") or operation.startswith("update_")
            ):
                # last-chance aliasing retry at top level
                _, _, op_rest = operation.partition("_")
                src = f"{op_rest}_details"
                dst = f"{operation}_details"
                if src in final_params and dst not in final_params:
                    alt_params = dict(final_params)
                    alt_params[dst] = alt_params.pop(src)
                    data, opc_request_id = _call_with_pagination_if_applicable(
                        method, alt_params, operation
                    )
                else:
                    raise
            else:
                raise

        result = {
            "client": client_fqn,
            "operation": operation,
            "params": params,
            "opc_request_id": opc_request_id,
            "data": _serialize_oci_data(data),
        }
        logger.info(
            f"invoke_oci_api success: client={client_fqn} op={operation} opc_request_id={opc_request_id}"
        )
        return result

    except Exception as e:
        logger.error(f"Error invoking OCI API {client_fqn}.{operation}: {e}")
        return {
            "client": client_fqn,
            "operation": operation,
            "params": params or {},
            "error": str(e),
        }


@mcp.tool(description="List public callable operations for a given OCI client class.")
def list_client_operations(
    client_fqn: Annotated[
        str, "Fully-qualified client class, e.g. 'oci.core.ComputeClient'"
    ],
) -> dict:
    try:
        module_name, class_name = client_fqn.rsplit(".", 1)
        module = import_module(module_name)
        cls = getattr(module, class_name)
        if not inspect.isclass(cls):
            raise ValueError(f"{client_fqn} is not a class")

        ops: List[dict] = []
        for name, member in inspect.getmembers(cls, predicate=inspect.isfunction):
            if name.startswith("_"):
                continue
            try:
                doc = (inspect.getdoc(member) or "").strip()
                first_line = doc.splitlines()[0] if doc else ""
                params = inspect.signature(member)
            except Exception:
                first_line = ""
                params = ""
            ops.append({"name": name, "summary": first_line, "params": str(params)})

        logger.info(f"Found {len(ops)} operations on {client_fqn}")
        # return a mapping to avoid Pydantic RootModel list-wrapping
        return {"operations": ops}
    except Exception as e:
        logger.error(f"Error listing operations for {client_fqn}: {e}")
        raise


@mcp.tool(description="List available OCI Python SDK client classes and their short descriptions.")
def list_clients() -> dict:
    """
    Returns:
      {
        "clients": [
          {
            "fqn": "oci.core.ComputeClient",
            "name": "ComputeClient",
            "module": "oci.core",
            "description": "One-line summary from the class docstring if available"
          },
          ...
        ]
      }
    """
    try:
        seen = set()
        clients: List[Dict[str, str]] = []
        # Walk all subpackages/modules in the OCI SDK
        for modinfo in pkgutil.walk_packages(oci.__path__, oci.__name__ + "."):
            mod_name = modinfo.name

            # Skip private modules and 'models' packages
            if any(part.startswith("_") for part in mod_name.split(".")):
                continue
            if mod_name.endswith(".models") or ".models." in mod_name:
                continue

            try:
                module = import_module(mod_name)
            except Exception:
                # best-effort import; ignore modules that fail to import
                continue

            # Find client classes defined in this module
            for attr_name, obj in inspect.getmembers(module, inspect.isclass):
                if not attr_name.endswith("Client"):
                    continue
                if attr_name.startswith("_"):
                    continue

                # Prefer classes actually defined in this module to avoid duplicates/re-exports
                obj_mod = getattr(obj, "__module__", "")
                if obj_mod != module.__name__:
                    continue

                fqn = f"{obj.__module__}.{obj.__name__}"
                if fqn in seen:
                    continue
                seen.add(fqn)

                doc = inspect.getdoc(obj) or ""
                summary = doc.splitlines()[0].strip() if doc else ""
                clients.append(
                    {
                        "fqn": f"{obj.__module__}.{obj.__name__}",
                        "name": obj.__name__,
                        "module": obj.__module__,
                        "description": summary,
                    }
                )

        # Provide deterministic ordering
        clients.sort(key=lambda x: x["fqn"])
        logger.info(f"Found {len(clients)} OCI SDK clients")
        return {"clients": clients}
    except Exception as e:
        logger.error(f"Error listing OCI SDK clients: {e}")
        return {"error": str(e)}

def _parse_docstring(doc: str) -> Dict[str, Any]:
    """
    Best-effort parser supporting common Sphinx/reST and Google-style docstrings.
    Returns a mapping with summary, description, params[name] -> {type, description},
    and returns {type, description}.
    """
    info: Dict[str, Any] = {
        "summary": "",
        "description": "",
        "params": {},
        "returns": {"type": None, "description": ""},
    }
    if not doc:
        return info

    lines = doc.splitlines()
    info["summary"] = lines[0].strip() if lines else ""

    i = 1
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()

        # Sphinx/reST :param type name: desc
        m = re.match(r":param\s+([A-Za-z0-9_\[\],\.\s]+)\s+([A-Za-z0-9_]+)\s*:\s*(.*)", stripped)
        if m:
            typ = (m.group(1) or "").strip() or None
            name = m.group(2).strip()
            desc = (m.group(3) or "").strip()
            info["params"].setdefault(name, {})
            if typ:
                info["params"][name]["type"] = typ
            info["params"][name]["description"] = desc
            i += 1
            # continuation lines (indented, not starting with ':')
            while i < len(lines) and lines[i].startswith("    ") and not lines[i].lstrip().startswith(":"):
                info["params"][name]["description"] += " " + lines[i].strip()
                i += 1
            continue

        # Sphinx/reST :param name: desc  (type may be specified separately via :type)
        m = re.match(r":param\s+([A-Za-z0-9_]+)\s*:\s*(.*)", stripped)
        if m:
            name = m.group(1).strip()
            desc = (m.group(2) or "").strip()
            info["params"].setdefault(name, {})
            info["params"][name]["description"] = desc
            i += 1
            while i < len(lines) and lines[i].startswith("    ") and not lines[i].lstrip().startswith(":"):
                info["params"][name]["description"] += " " + lines[i].strip()
                i += 1
            continue

        # Sphinx/reST :type name: type
        m = re.match(r":type\s+([A-Za-z0-9_]+)\s*:\s*(.*)", stripped)
        if m:
            name = m.group(1).strip()
            typ = (m.group(2) or "").strip()
            info["params"].setdefault(name, {})
            if typ:
                info["params"][name]["type"] = typ
            i += 1
            continue

        # Sphinx/reST :return: / :returns:
        m = re.match(r":return[s]?\s*:\s*(.*)", stripped)
        if m:
            info["returns"]["description"] = (m.group(1) or "").strip()
            i += 1
            while i < len(lines) and lines[i].startswith("    ") and not lines[i].lstrip().startswith(":"):
                info["returns"]["description"] += " " + lines[i].strip()
                i += 1
            continue

        # Sphinx/reST :rtype:
        m = re.match(r":rtype\s*:\s*(.*)", stripped)
        if m:
            info["returns"]["type"] = (m.group(1) or "").strip()
            i += 1
            continue

        # Google-style Args/Parameters section
        if stripped in ("Args:", "Arguments:", "Parameters:"):
            section_indent = len(line) - len(line.lstrip())
            i += 1
            while i < len(lines):
                l = lines[i]
                if l.strip() == "":
                    i += 1
                    continue
                indent = len(l) - len(l.lstrip())
                if indent <= section_indent:
                    break
                mm = re.match(r"\s*([A-Za-z0-9_]+)\s*(?:\(([^)]+)\))?\s*:\s*(.*)", l)
                if mm:
                    name = mm.group(1).strip()
                    typ = (mm.group(2) or "").strip() or None
                    desc = (mm.group(3) or "").strip()
                    info["params"].setdefault(name, {})
                    if typ:
                        info["params"][name]["type"] = typ
                    info["params"][name]["description"] = desc
                    i += 1
                    # consume continuation lines more indented than current param line
                    while i < len(lines) and (len(lines[i]) - len(lines[i].lstrip())) > indent:
                        info["params"][name]["description"] += " " + lines[i].strip()
                        i += 1
                    continue
                else:
                    i += 1
            continue

        # Google-style Returns:
        if stripped.startswith("Returns:"):
            # Inline "Returns: type: desc" or "Returns:" then next line
            rest = stripped[len("Returns:"):].strip()
            if rest:
                # sometimes "type: desc" inline
                mm = re.match(r"([A-Za-z0-9_\[\],\.\s]+)\s*:\s*(.*)", rest)
                if mm:
                    info["returns"]["type"] = (mm.group(1) or "").strip()
                    info["returns"]["description"] = (mm.group(2) or "").strip()
                else:
                    info["returns"]["description"] = rest
            i += 1
            # If next line looks like "type: desc"
            if i < len(lines) and lines[i].strip():
                mm = re.match(r"\s*([A-Za-z0-9_\[\],\.\s]+)\s*:\s*(.*)", lines[i])
                if mm:
                    info["returns"]["type"] = (mm.group(1) or "").strip()
                    info["returns"]["description"] = (
                        (info["returns"]["description"] + " " + (mm.group(2) or "").strip()).strip()
                    )
                    i += 1
            continue

        i += 1

    # Description as the remainder (best-effort)
    try:
        rest = doc.splitlines()[1:]
        info["description"] = "\n".join(rest).strip()
    except Exception:
        pass
    return info


@mcp.tool(description="Get detailed metadata for a specific OCI SDK client operation.")
def get_client_operation_details(
    client_fqn: Annotated[str, "Fully-qualified client class, e.g. 'oci.core.ComputeClient'"],
    operation: Annotated[str, "Client method/operation name, e.g. 'list_instances' or 'get_instance'"],
) -> dict:
    """
    Returns a structured description of the client's operation including:
    - signature and per-parameter details (name, kind, required, default, annotation)
    - parsed docstring (summary, params, returns) and raw docstring
    - expected_kwargs (from generated SDK method source, if available)
    - return type annotation (if available)
    - source file and line numbers (if available)
    - pagination support heuristic
    """
    try:
        module_name, class_name = client_fqn.rsplit(".", 1)
        module = import_module(module_name)
        cls = getattr(module, class_name)
        if not inspect.isclass(cls):
            raise ValueError(f"{client_fqn} is not a class")

        if not hasattr(cls, operation):
            raise AttributeError(f"Operation '{operation}' not found on client '{client_fqn}'")

        method = getattr(cls, operation)
        if not (inspect.isfunction(method) or inspect.ismethod(method)):
            raise AttributeError(f"Attribute '{operation}' on client '{client_fqn}' is not a function/method")

        # Signature and type hints
        sig = inspect.signature(method)
        try:
            from typing import get_type_hints as _get_type_hints
            type_hints = _get_type_hints(method)
        except Exception:
            type_hints = {}

        params_list: List[Dict[str, Any]] = []
        for name, p in sig.parameters.items():
            if name == "self":
                continue
            ann = type_hints.get(name)
            if ann is None and p.annotation is not inspect._empty:
                ann = p.annotation
            if ann is not None:
                try:
                    ann_str = getattr(ann, "__name__", str(ann))
                except Exception:
                    ann_str = str(ann)
            else:
                ann_str = None
            default = None if p.default is inspect._empty else repr(p.default)
            required = p.default is inspect._empty and p.kind not in (
                inspect.Parameter.VAR_POSITIONAL,
                inspect.Parameter.VAR_KEYWORD,
            )
            params_list.append(
                {
                    "name": name,
                    "kind": str(p.kind),
                    "required": required,
                    "default": default,
                    "annotation": ann_str,
                }
            )

        # Return annotation
        ret_ann = None
        try:
            ra = type_hints.get("return")
            if ra is None and sig.return_annotation is not inspect._empty:
                ra = sig.return_annotation
            if ra is not None:
                ret_ann = getattr(ra, "__name__", str(ra))
        except Exception:
            ret_ann = None

        # Docstring and parsing
        try:
            doc = inspect.getdoc(method) or ""
        except Exception:
            doc = ""
        parsed_doc = _parse_docstring(doc)
        summary = parsed_doc.get("summary") or (doc.splitlines()[0] if doc else "")

        # Source location
        try:
            src_file = inspect.getsourcefile(method)
            src_lines, start_line = inspect.getsourcelines(method)
            end_line = start_line + len(src_lines) - 1
        except Exception:
            src_file, start_line, end_line = None, None, None

        # expected_kwargs from SDK-generated method body (if present)
        expected_kwargs = _extract_expected_kwargs_from_source(method)

        # Pagination heuristic
        supports_pagination = _supports_pagination(method, operation)

        result = {
            "client": client_fqn,
            "operation": operation,
            "signature": str(sig),
            "parameters": params_list,
            "expected_kwargs": sorted(expected_kwargs) if isinstance(expected_kwargs, set) else expected_kwargs,
            "doc": {
                "summary": summary,
                "params": parsed_doc.get("params", {}),
                "returns": parsed_doc.get("returns", {"type": None, "description": ""}),
                "description": parsed_doc.get("description", ""),
                "raw": doc,
            },
            "return_annotation": ret_ann,
            "source": {"file": src_file, "line_start": start_line, "line_end": end_line},
            "supports_pagination": supports_pagination,
        }
        logger.info(f"get_client_operation_details success: client={client_fqn} op={operation}")
        return result
    except Exception as e:
        logger.error(f"Error getting operation details for {client_fqn}.{operation}: {e}")
        return {"client": client_fqn, "operation": operation, "error": str(e)}


def main():
    host = os.getenv("ORACLE_MCP_HOST")
    port = os.getenv("ORACLE_MCP_PORT")

    if host and port:
        mcp.run(transport="http", host=host, port=int(port))
    else:
        mcp.run()


if __name__ == "__main__":
    main()
