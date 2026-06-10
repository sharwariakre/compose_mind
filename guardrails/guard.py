"""Guardrail checks that validate or block agent-proposed actions."""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import Optional

from rich.console import Console

from compose_mind.agent.tools import DIAGNOSTIC_TOOL_NAMES, TOOL_NAMES
from compose_mind.docker.compose import ComposeConfig

console = Console()

# Tools the agent is allowed to invoke, sourced from the tool registry.
# Anything not in TOOL_NAMES is hard-blocked as an unknown tool.
DIAGNOSTIC_TOOLS = DIAGNOSTIC_TOOL_NAMES
KNOWN_TOOLS = TOOL_NAMES

# Commands that may never run inside a container, regardless of confirmation.
_DANGEROUS_EXEC_PATTERNS = (
    r"rm\s+-rf",
    r"DROP\s+TABLE",
    r"DELETE\s+FROM",
    r"\btruncate\b",
)

# Service names treated as stateful databases for the volume hard-block.
_DATABASE_NAMES = ("postgres", "mysql", "mongo")

# stop_all is blocked outright once it would take down this many services.
_STOP_ALL_BLOCK_THRESHOLD = 4


class RiskLevel(Enum):
    """Risk classification for a proposed operation."""

    SAFE = "safe"
    WARN = "warn"
    CONFIRM_BY_NAME = "confirm_by_name"
    BLOCKED = "blocked"


@dataclass
class OpRisk:
    """The assessed risk of an operation."""

    level: RiskLevel
    message: str
    confirm_target: Optional[str] = None


class GuardrailsError(Exception):
    """Raised when an operation is hard-blocked and must not run."""


def _command_is_dangerous(command: str) -> bool:
    """Return True if a command matches a hard-blocked destructive pattern."""
    return any(
        re.search(pattern, command, flags=re.IGNORECASE)
        for pattern in _DANGEROUS_EXEC_PATTERNS
    )


def _declared_replicas(service_name: str, compose_config: ComposeConfig) -> int:
    """Return a service's declared replica count (deploy.replicas), default 1."""
    raw_services = compose_config.raw.get("services") or {}
    service = raw_services.get(service_name) or {}
    deploy = service.get("deploy") if isinstance(service, dict) else None
    if isinstance(deploy, dict):
        replicas = deploy.get("replicas")
        try:
            return int(replicas)
        except (TypeError, ValueError):
            return 1
    return 1


def _stateful_db_with_volumes(compose_config: ComposeConfig) -> Optional[str]:
    """Return the name of a database service that has volumes, if any."""
    for name, service in compose_config.services.items():
        lowered = name.lower()
        is_db = any(db in lowered for db in _DATABASE_NAMES)
        if is_db and service.volumes:
            return name
    return None


def classify_operation(
    tool_name: str,
    tool_args: dict,
    compose_config: ComposeConfig,
) -> OpRisk:
    """Classify an operation's risk, hard-blocking the most dangerous cases.

    Raises GuardrailsError immediately for operations that must never run:
    unknown tools, destructive exec commands, and stopping a stack with a
    stateful database that has volumes attached.
    """
    # --- Hard blocks: raise immediately, no prompt -----------------------
    if tool_name not in KNOWN_TOOLS:
        raise GuardrailsError(
            f"Unknown tool '{tool_name}'. Refusing to run an unrecognized "
            "operation."
        )

    if tool_name == "exec_in_service":
        command = str(tool_args.get("command", ""))
        if _command_is_dangerous(command):
            raise GuardrailsError(
                "Refused: exec command contains a destructive pattern "
                "(rm -rf, DROP TABLE, DELETE FROM, or truncate)."
            )

    if tool_name == "stop_all":
        db_service = _stateful_db_with_volumes(compose_config)
        if db_service is not None:
            raise GuardrailsError(
                f"Refused: stopping the stack would take down database service "
                f"'{db_service}' which has volumes attached. This is too risky "
                "to perform automatically."
            )

    # --- Graded risk levels ---------------------------------------------
    if tool_name in DIAGNOSTIC_TOOLS:
        return OpRisk(RiskLevel.SAFE, f"Diagnostic operation '{tool_name}'.")

    if tool_name == "stop_all":
        service_count = len(compose_config.service_names())
        if service_count >= _STOP_ALL_BLOCK_THRESHOLD:
            return OpRisk(
                RiskLevel.BLOCKED,
                f"stop_all would stop {service_count} services "
                f"({_STOP_ALL_BLOCK_THRESHOLD}+). Blocked.",
            )
        project = str(tool_args.get("project_name", "")) or "the project"
        return OpRisk(
            RiskLevel.CONFIRM_BY_NAME,
            f"Stopping the entire stack ({service_count} service(s)).",
            confirm_target=project,
        )

    if tool_name == "stop_service":
        service = str(tool_args.get("service_name", ""))
        return OpRisk(
            RiskLevel.CONFIRM_BY_NAME,
            f"Stopping service '{service}'.",
            confirm_target=service,
        )

    if tool_name == "scale_service":
        service = str(tool_args.get("service_name", ""))
        baseline = _declared_replicas(service, compose_config)
        # Treat scaling down (fewer replicas) as a WARN; scaling up is SAFE.
        try:
            target = int(tool_args.get("count"))
        except (TypeError, ValueError):
            target = baseline
        if target < baseline:
            return OpRisk(
                RiskLevel.WARN,
                f"Scaling '{service}' down from {baseline} to {target} replica(s).",
            )
        return OpRisk(
            RiskLevel.SAFE,
            f"Scaling '{service}' up to {target} replica(s).",
        )

    if tool_name == "restart_service":
        service = str(tool_args.get("service_name", ""))
        return OpRisk(
            RiskLevel.WARN,
            f"Restarting service '{service}'.",
        )

    if tool_name == "exec_in_service":
        # Destructive commands were already hard-blocked above; any remaining
        # exec is still arbitrary command execution, so warn before running.
        service = str(tool_args.get("service_name", ""))
        command = str(tool_args.get("command", ""))
        return OpRisk(
            RiskLevel.WARN,
            f"Running command in '{service}': {command}",
        )

    # Should be unreachable given KNOWN_TOOLS, but stay safe.
    raise GuardrailsError(
        f"Tool '{tool_name}' is known but has no risk classification."
    )


def confirm_with_user(op_risk: OpRisk) -> bool:
    """Prompt the user according to the risk level. Return True if confirmed.

    SAFE operations are confirmed automatically with no prompt. BLOCKED
    operations are never confirmable and always return False.
    """
    if op_risk.level is RiskLevel.SAFE:
        return True

    if op_risk.level is RiskLevel.BLOCKED:
        console.print(f"[bold red]BLOCKED:[/bold red] {op_risk.message}")
        return False

    if op_risk.level is RiskLevel.WARN:
        console.print(f"[yellow]⚠  {op_risk.message}[/yellow]")
        answer = input("Proceed? [y/N] ").strip().lower()
        return answer in ("y", "yes")

    if op_risk.level is RiskLevel.CONFIRM_BY_NAME:
        target = op_risk.confirm_target or ""
        console.print(f"[bold red]⚠  {op_risk.message}[/bold red]")
        console.print(
            f"[red]This is a high-risk action. Type "
            f"[bold]{target}[/bold] to confirm.[/red]"
        )
        answer = input("Confirm name: ").strip()
        if answer == target:
            return True
        console.print("[red]Name did not match. Aborted.[/red]")
        return False

    return False
