"""
Executor management tools for Hummingbot MCP Server.

This module provides business logic for managing trading executors including
creation, viewing, stopping, and position management with progressive disclosure.
"""
import logging
from typing import Any

from mcp_servers.hummingbot_api.executor_preferences import executor_preferences
from mcp_servers.hummingbot_api.formatters.executors import (
    format_executor_detail,
    format_executor_schema_table,
    format_executors_table,
    format_positions_held_table,
    format_positions_summary,
)
from mcp_servers.hummingbot_api.schemas import ManageExecutorsRequest

logger = logging.getLogger("hummingbot-mcp")

# Internal fields injected by the MCP layer, not user-supplied
_INTERNAL_FIELDS = {"type", "executor_type", "id"}


def validate_executor_config(config: dict[str, Any], schema: dict[str, Any]) -> list[str]:
    """Validate config keys against the backend schema properties.

    Returns a list of error strings. An empty list means the config is valid.
    """
    errors: list[str] = []
    _validate_level(config, schema, "", errors)
    return errors


def _validate_level(config: dict[str, Any], schema: dict[str, Any], path: str, errors: list[str]) -> None:
    """Recursively validate config keys against schema properties."""
    properties = schema.get("properties", {})
    if not properties:
        return

    allowed = set(properties.keys())

    for key in config:
        if not path and key in _INTERNAL_FIELDS:
            continue
        if key not in allowed:
            field_list = ", ".join(sorted(allowed - _INTERNAL_FIELDS))
            location = f" inside '{path}'" if path else ""
            errors.append(f"Unknown field '{key}'{location}. Allowed fields: {field_list}")
            continue
        # Recurse into nested objects
        prop_schema = properties[key]
        if isinstance(prop_schema, dict) and isinstance(config[key], dict) and "properties" in prop_schema:
            _validate_level(config[key], prop_schema, key, errors)


async def manage_executors(client: Any, request: ManageExecutorsRequest) -> dict[str, Any]:
    """
    Manage executors with progressive disclosure.

    Args:
        client: Hummingbot API client
        request: ManageExecutorsRequest with action and parameters

    Returns:
        Dictionary containing results and formatted output
    """
    flow_stage = request.get_flow_stage()

    if flow_stage == "list_types":
        # Brief static response — full descriptions are in the tool docstring
        formatted = (
            "Available Executor Types:\n\n"
            "- **position_executor** — Directional trading with entry, stop-loss, and take-profit\n"
            "- **dca_executor** — Dollar-cost averaging for gradual position building\n"
            "- **grid_executor** — Grid trading across multiple price levels in ranging markets\n"
            "- **order_executor** — Simple BUY/SELL order with execution strategy\n"
            "- **lp_executor** — Liquidity provision on CLMM DEXs (Meteora, Raydium)\n\n"
            "Provide `executor_type` to see the configuration schema."
        )

        return {
            "action": "list_types",
            "formatted_output": formatted,
            "next_step": "Call again with 'executor_type' to see the configuration schema",
            "example": "manage_executors(executor_type='position_executor')",
        }

    elif flow_stage == "show_schema":
        # Stage 2: Show config schema with user defaults
        try:
            schema = await client.executors.get_executor_config_schema(request.executor_type)
        except Exception as e:
            return {
                "action": "show_schema",
                "error": f"Failed to get schema for {request.executor_type}: {e}",
                "formatted_output": f"Error: Failed to get schema for {request.executor_type}: {e}",
            }

        # Get user defaults
        user_defaults = executor_preferences.get_defaults(request.executor_type)

        # Get the guide from the markdown file
        executor_guide = executor_preferences.get_executor_guide(request.executor_type)

        formatted = f"Configuration Schema for {request.executor_type}\n\n"
        if executor_guide:
            formatted += f"{executor_guide}\n\n"

        formatted += format_executor_schema_table(schema, user_defaults)

        if user_defaults:
            formatted += f"\n\nYour saved defaults for {request.executor_type}:\n"
            for key, value in user_defaults.items():
                formatted += f"  {key}: {value}\n"
            formatted += f"\nPreferences file: {executor_preferences.get_preferences_path()}"

        return {
            "action": "show_schema",
            "executor_type": request.executor_type,
            "schema": schema,
            "user_defaults": user_defaults,
            "formatted_output": formatted,
            "next_step": "Call with action='create' and executor_config to create an executor",
            "example": f"manage_executors(action='create', executor_type='{request.executor_type}', executor_config={{...}})",
        }

    elif flow_stage == "create":
        # Stage 3: Create executor
        executor_type = request.executor_type or request.executor_config.get("type") or request.executor_config.get("executor_type")

        if not executor_type:
            return {
                "action": "create",
                "error": "executor_type is required for creating an executor",
                "formatted_output": "Error: Please provide executor_type",
            }

        # Merge with defaults
        merged_config = executor_preferences.merge_with_defaults(executor_type, request.executor_config)

        # Ensure type is set in config
        if "type" not in merged_config and "executor_type" not in merged_config:
            merged_config["type"] = executor_type

        # Validate config fields against backend schema before sending
        try:
            schema = await client.executors.get_executor_config_schema(executor_type)
            validation_errors = validate_executor_config(merged_config, schema)
