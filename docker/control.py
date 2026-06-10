"""Lifecycle control for Docker Compose services (up, down, restart)."""

from __future__ import annotations

import re
import subprocess
from typing import Any

from docker.errors import DockerException

from compose_mind.docker.inspect import (
    LABEL_PROJECT,
    LABEL_SERVICE,
    _get_client,
    find_container,
)

# Patterns that are never allowed to run inside a container via exec.
_DANGEROUS_PATTERNS = (
    r"rm\s+-rf",
    r"\bDROP\b",
    r"DELETE\s+FROM",
)


def _capture_state(container: Any) -> dict:
    """Snapshot a container's status and start time for later undo/reporting."""
    state = container.attrs.get("State", {})
    return {
        "status": container.status,
        "started_at": state.get("StartedAt"),
    }


def _run_compose(args: list[str], project_name: str) -> subprocess.CompletedProcess:
    """Run a `docker compose` subcommand with the given args."""
    cmd = ["docker", "compose", "-p", project_name] + args
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        check=False,
    )


def restart_service(service_name: str, project_name: str) -> dict:
    """Restart the named container, capturing its prior state for undo."""
    try:
        container = find_container(service_name, project_name)
    except (ValueError, RuntimeError) as exc:
        return {"success": False, "message": str(exc)}

    before = _capture_state(container)
    try:
        container.restart()
    except DockerException as exc:
        return {
            "success": False,
            "message": f"Failed to restart '{service_name}': {exc}",
            "before": before,
        }

    return {
        "success": True,
        "message": f"Restarted service '{service_name}'.",
        "before": before,
    }


def _count_replicas(service_name: str, project_name: str) -> int:
    """Count running/created containers for a compose service."""
    try:
        client = _get_client()
        containers = client.containers.list(
            all=True,
            filters={
                "label": [
                    f"{LABEL_SERVICE}={service_name}",
                    f"{LABEL_PROJECT}={project_name}",
                ]
            },
        )
        return len(containers)
    except (DockerException, RuntimeError):
        return 0


def scale_service(
    service_name: str,
    count: int,
    project_name: str,
    compose_file_path: str,
) -> dict:
    """Scale a service to ``count`` replicas via `docker compose up --scale`."""
    if count < 0:
        return {
            "success": False,
            "message": f"Replica count must be >= 0, got {count}.",
        }

    before = {"replicas": _count_replicas(service_name, project_name)}

    args = [
        "-f",
        compose_file_path,
        "up",
        "-d",
        "--scale",
        f"{service_name}={count}",
        "--no-recreate",
    ]
    try:
        result = _run_compose(args, project_name)
    except FileNotFoundError as exc:
        return {
            "success": False,
            "message": f"Could not run 'docker compose': {exc}",
            "before": before,
        }

    if result.returncode != 0:
        return {
            "success": False,
            "message": (
                f"Failed to scale '{service_name}' to {count}: "
                f"{result.stderr.strip() or result.stdout.strip()}"
            ),
            "before": before,
        }

    return {
        "success": True,
        "message": f"Scaled service '{service_name}' to {count} replica(s).",
        "before": before,
    }


def stop_service(service_name: str, project_name: str) -> dict:
    """Stop (but do not remove) the named container, capturing prior state."""
    try:
        container = find_container(service_name, project_name)
    except (ValueError, RuntimeError) as exc:
        return {"success": False, "message": str(exc)}

    before = _capture_state(container)
    try:
        container.stop()
    except DockerException as exc:
        return {
            "success": False,
            "message": f"Failed to stop '{service_name}': {exc}",
            "before": before,
        }

    return {
        "success": True,
        "message": f"Stopped service '{service_name}'.",
        "before": before,
    }


def stop_all(project_name: str, compose_file_path: str) -> dict:
    """Stop the entire stack via `docker compose stop`.

    High-risk: only call after guardrails have confirmed the action.
    """
    args = ["-f", compose_file_path, "stop"]
    try:
        result = _run_compose(args, project_name)
    except FileNotFoundError as exc:
        return {
            "success": False,
            "message": f"Could not run 'docker compose': {exc}",
        }

    if result.returncode != 0:
        return {
            "success": False,
            "message": (
                "Failed to stop the stack: "
                f"{result.stderr.strip() or result.stdout.strip()}"
            ),
        }

    return {
        "success": True,
        "message": f"Stopped all services in project '{project_name}'.",
    }


def _is_dangerous_command(command: str) -> bool:
    """Return True if the command matches a hard-blocked destructive pattern."""
    return any(
        re.search(pattern, command, flags=re.IGNORECASE)
        for pattern in _DANGEROUS_PATTERNS
    )


def exec_in_service(service_name: str, command: str, project_name: str) -> dict:
    """Run a command inside the service's container, blocking destructive ones."""
    if _is_dangerous_command(command):
        return {
            "success": False,
            "message": (
                "Refused to run a destructive command. Commands containing "
                "'rm -rf', 'DROP', or 'DELETE FROM' are hard-blocked for safety."
            ),
        }

    try:
        container = find_container(service_name, project_name)
    except (ValueError, RuntimeError) as exc:
        return {"success": False, "message": str(exc)}

    try:
        exit_code, output = container.exec_run(
            cmd=["sh", "-c", command],
            demux=True,
        )
    except DockerException as exc:
        return {
            "success": False,
            "message": f"Failed to exec in '{service_name}': {exc}",
        }

    stdout_bytes, stderr_bytes = output if output else (None, None)
    stdout = stdout_bytes.decode("utf-8", errors="replace") if stdout_bytes else ""
    stderr = stderr_bytes.decode("utf-8", errors="replace") if stderr_bytes else ""

    success = exit_code == 0
    message = (
        f"Command exited {exit_code} in service '{service_name}'."
        if not success
        else f"Command ran successfully in service '{service_name}'."
    )
    return {
        "success": success,
        "message": message,
        "exit_code": exit_code,
        "stdout": stdout,
        "stderr": stderr,
    }
