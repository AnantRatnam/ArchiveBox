# ruff: noqa
def get_host_immutable_info(host_info: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in host_info.items() if key in ["guid", "net_mac", "os_family", "cpu_arch"]}
