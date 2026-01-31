import oracle.oci_cloud_mcp_server.server as srv
from oracle.oci_cloud_mcp_server.server import _supports_pagination


def test_supports_pagination_docstring_triggers_true():
    """
    When the method's docstring mentions pagination-related params (page/limit),
    _supports_pagination should return True even if the operation name doesn't match other patterns.
    """

    def method():
        """
        Get something.

        :param int limit: max items
        """
        return None

    assert _supports_pagination(method, "get_something") is True
