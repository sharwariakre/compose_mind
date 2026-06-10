"""Inspection of Docker containers, images, and networks."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from typing import Any, Optional

import docker
from docker.errors import DockerException, NotFound
from docker.models.containers import Container

# Compose labels Docker stamps on every container it creates.
LABEL_SERVICE = "com.docker.compose.service"
LABEL_PROJECT = "com.docker.compose.project"


def _get_client() -> docker.DockerClient:
    """Return a Docker client, or raise RuntimeError if the daemon is unreachable."""
    try:
        client = docker.from_env()
        client.ping()
        return client
    except DockerException as exc:
        raise RuntimeError(
            "Could not reach the Docker daemon. Is Docker running and is the "
            f"socket accessible? ({exc})"
        ) from exc


def find_container(service_name: str, project_name: str) -> Container:
    """Find a single container by compose service and project labels.

    Raises ValueError if no matching container exists.
    """
    client = _get_client()
    filters = {
        "label": [
            f"{LABEL_SERVICE}={service_name}",
            f"{LABEL_PROJECT}={project_name}",
        ]
    }
    try:
        containers = client.containers.list(all=True, filters=filters)
    except DockerException as exc:
        raise RuntimeError(f"Failed to list Docker containers: {exc}") from exc

    if not containers:
        raise ValueError(
            f"No container found for service '{service_name}' in project "
            f"'{project_name}'. Is the stack up? Check the project name "
            "(usually the directory name) and that the service is defined."
        )
    return containers[0]


def _list_project_containers(
    project_name: str, running_only: bool = False
) -> list[Container]:
    """List all containers belonging to a compose project."""
    client = _get_client()
    filters: dict[str, Any] = {"label": f"{LABEL_PROJECT}={project_name}"}
    if running_only:
        filters["status"] = "running"
    try:
        return client.containers.list(all=not running_only, filters=filters)
    except DockerException as exc:
        raise RuntimeError(f"Failed to list Docker containers: {exc}") from exc


def _service_of(container: Container) -> str:
    """Return the compose service name for a container (falls back to its name)."""
    return container.labels.get(LABEL_SERVICE, container.name)


def _image_of(container: Container) -> str:
    """Return a human-readable image reference for a container."""
    tags = getattr(container.image, "tags", None)
    if tags:
        return tags[0]
    image_attr = container.attrs.get("Config", {}).get("Image")
    if image_attr:
        return image_attr
    return getattr(container.image, "short_id", "") or "unknown"


def get_stack_health(project_name: str) -> list[dict]:
    """Return the status of all containers in a compose project.

    Each dict has: service, status, health, started_at, image.
    """
    containers = _list_project_containers(project_name)
    health: list[dict] = []
    for container in containers:
        state = container.attrs.get("State", {})
        health_info = state.get("Health", {})
        health.append(
            {
                "service": _service_of(container),
                "status": container.status,
                "health": health_info.get("Status") if health_info else None,
                "started_at": state.get("StartedAt"),
                "image": _image_of(container),
            }
        )
    health.sort(key=lambda item: item["service"])
    return health


def _compute_cpu_percent(stats: dict) -> float:
    """Compute CPU usage percentage from a one-shot stats payload."""
    cpu_stats = stats.get("cpu_stats", {})
    precpu_stats = stats.get("precpu_stats", {})

    cpu_total = cpu_stats.get("cpu_usage", {}).get("total_usage", 0)
    precpu_total = precpu_stats.get("cpu_usage", {}).get("total_usage", 0)
    cpu_delta = cpu_total - precpu_total

    system_cpu = cpu_stats.get("system_cpu_usage", 0)
    presystem_cpu = precpu_stats.get("system_cpu_usage", 0)
    system_delta = system_cpu - presystem_cpu

    online_cpus = cpu_stats.get("online_cpus")
    if not online_cpus:
        percpu = cpu_stats.get("cpu_usage", {}).get("percpu_usage") or []
        online_cpus = len(percpu) or 1

    if system_delta > 0 and cpu_delta > 0:
        return round((cpu_delta / system_delta) * online_cpus * 100.0, 2)
    return 0.0


def _compute_memory_mb(stats: dict) -> tuple[float, float]:
    """Return (usage_mb, limit_mb) from a one-shot stats payload."""
    mem_stats = stats.get("memory_stats", {})
    usage = mem_stats.get("usage", 0)
    # Exclude page cache from usage, matching `docker stats` behaviour.
    cache = mem_stats.get("stats", {}).get("inactive_file")
    if cache is None:
        cache = mem_stats.get("stats", {}).get("cache", 0)
    usage = max(usage - (cache or 0), 0)
    limit = mem_stats.get("limit", 0)

    mb = 1024 * 1024
    return round(usage / mb, 2), round(limit / mb, 2)


def get_service_stats(service_name: str, project_name: str) -> dict:
    """Return CPU% and memory usage/limit (MB) for a named service."""
    container = find_container(service_name, project_name)
    try:
        stats = container.stats(stream=False)
    except DockerException as exc:
        raise RuntimeError(
            f"Failed to read stats for service '{service_name}': {exc}"
        ) from exc

    cpu_percent = _compute_cpu_percent(stats)
    memory_mb, memory_limit_mb = _compute_memory_mb(stats)

    return {
        "service": service_name,
        "status": container.status,
        "cpu_percent": cpu_percent,
        "memory_mb": memory_mb,
        "memory_limit_mb": memory_limit_mb,
    }


def get_service_logs(
    service_name: str,
    project_name: str,
    tail: int = 50,
    level_filter: Optional[str] = None,
) -> list[str]:
    """Return the last ``tail`` log lines for a service.

    If ``level_filter`` is given, keep only lines containing it (case-insensitive).
    """
    container = find_container(service_name, project_name)
    try:
        raw = container.logs(tail=tail, timestamps=False)
    except DockerException as exc:
        raise RuntimeError(
            f"Failed to read logs for service '{service_name}': {exc}"
        ) from exc

    text = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else raw
    lines = [line for line in text.splitlines() if line]

    if level_filter:
        needle = level_filter.lower()
        lines = [line for line in lines if needle in line.lower()]

    return lines


def get_all_stats(project_name: str) -> list[dict]:
    """Return stats for every running container in the project, hottest CPU first.

    Stats are gathered concurrently with a ThreadPoolExecutor.
    """
    containers = _list_project_containers(project_name, running_only=True)
    services = [_service_of(container) for container in containers]
    if not services:
        return []

    results: list[dict] = []
    with ThreadPoolExecutor(max_workers=min(len(services), 8)) as executor:
        futures = {
            executor.submit(get_service_stats, service, project_name): service
            for service in services
        }
        for future in futures:
            service = futures[future]
            try:
                results.append(future.result())
            except (RuntimeError, ValueError, DockerException, NotFound) as exc:
                results.append(
                    {
                        "service": service,
                        "status": "error",
                        "cpu_percent": 0.0,
                        "memory_mb": 0.0,
                        "memory_limit_mb": 0.0,
                        "error": str(exc),
                    }
                )

    results.sort(key=lambda item: item.get("cpu_percent", 0.0), reverse=True)
    return results
