"""Tool definitions exposed to the agent."""

from __future__ import annotations

# Read-only tools that only inspect stack state.
DIAGNOSTIC_TOOL_NAMES = {
    "get_stack_health",
    "get_service_stats",
    "get_service_logs",
    "get_all_stats",
}

# Tools that mutate the running stack.
MUTATION_TOOL_NAMES = {
    "restart_service",
    "scale_service",
    "stop_service",
    "stop_all",
    "exec_in_service",
}

# The complete set of registered tool names. Imported by guardrails/guard.py
# as the source of truth for the unknown-tool hard block.
TOOL_NAMES = DIAGNOSTIC_TOOL_NAMES | MUTATION_TOOL_NAMES


def get_tools() -> list:
    """Return the list of tool schemas available to the agent."""
    pass
