# ruff: noqa
class HookResult(TypedDict, total=False):
    """Raw result from run_hook()."""

    returncode: int
    stdout: str
    stderr: str
    output_json: dict[str, Any] | None
    output_files: list[dict[str, Any]]
    duration_ms: int
    hook: str
    plugin: str  # Plugin name (directory name, e.g., 'wget', 'screenshot')
    hook_name: str  # Full hook filename (e.g., 'on_Snapshot__50_wget.py')
    # New fields for JSONL parsing
    records: list[dict[str, Any]]  # Parsed JSONL records with 'type' field


def get_config_defaults_from_plugins() -> dict[str, Any]:
    """
    Get default values for all plugin config options.

    Returns:
        Dict mapping config keys to their default values.
        e.g., {'SAVE_WGET': True, 'WGET_TIMEOUT': 60, ...}
    """
    plugin_configs = discover_plugin_configs()
    defaults = {}

    for plugin_name, schema in plugin_configs.items():
        properties = schema.get("properties", {})
        for key, prop_schema in properties.items():
            if "default" in prop_schema:
                defaults[key] = prop_schema["default"]

    return defaults
