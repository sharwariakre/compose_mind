"""Parsing and handling of docker-compose files."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Optional

import yaml

# Filenames searched for, in order of precedence (matches Docker Compose's own order).
COMPOSE_FILENAMES = (
    "docker-compose.yml",
    "docker-compose.yaml",
    "compose.yml",
    "compose.yaml",
)


@dataclass
class ServiceConfig:
    """A single service parsed from a compose file."""

    name: str
    image: Optional[str] = None
    ports: list[str] = field(default_factory=list)
    depends_on: list[str] = field(default_factory=list)
    volumes: list[str] = field(default_factory=list)
    environment: dict[str, Optional[str]] = field(default_factory=dict)
    restart_policy: Optional[str] = None


@dataclass
class ComposeConfig:
    """A parsed compose file: its services plus the raw parsed YAML."""

    services: dict[str, ServiceConfig] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict)
    path: Optional[str] = None

    def service_names(self) -> list[str]:
        """Return the names of all services, in declaration order."""
        return list(self.services.keys())

    def get_service(self, name: str) -> Optional[ServiceConfig]:
        """Return the service with the given name, or None if absent."""
        return self.services.get(name)

    def dependents_of(self, name: str) -> list[str]:
        """Return the names of services that depend on ``name``."""
        return [
            svc_name
            for svc_name, svc in self.services.items()
            if name in svc.depends_on
        ]

    def to_prompt_context(self) -> str:
        """Render a clean text summary for injection into a system prompt."""
        if not self.services:
            return "No services defined in the compose file."

        lines: list[str] = []
        source = self.path or "docker-compose.yml"
        lines.append(f"Compose file: {source}")
        lines.append(f"Services ({len(self.services)}): {', '.join(self.service_names())}")
        lines.append("")

        for name, svc in self.services.items():
            lines.append(f"Service: {name}")
            lines.append(f"  image: {svc.image or '(none / build)'}")
            if svc.ports:
                lines.append(f"  ports: {', '.join(svc.ports)}")
            if svc.depends_on:
                lines.append(f"  depends_on: {', '.join(svc.depends_on)}")
            dependents = self.dependents_of(name)
            if dependents:
                lines.append(f"  depended on by: {', '.join(dependents)}")
            if svc.volumes:
                lines.append(f"  volumes: {', '.join(svc.volumes)}")
            if svc.environment:
                env_keys = ", ".join(sorted(svc.environment.keys()))
                lines.append(f"  environment keys: {env_keys}")
            if svc.restart_policy:
                lines.append(f"  restart: {svc.restart_policy}")
            lines.append("")

        return "\n".join(lines).rstrip()


def _normalize_depends_on(value: Any) -> list[str]:
    """Normalize ``depends_on`` (list form or v3 condition-dict form) to names."""
    if value is None:
        return []
    if isinstance(value, dict):
        return list(value.keys())
    if isinstance(value, list):
        return [str(item) for item in value]
    # Defensive: a lone string.
    return [str(value)]


def _normalize_environment(value: Any) -> dict[str, Optional[str]]:
    """Normalize ``environment`` (``KEY=val`` list or mapping) to a dict."""
    if value is None:
        return {}
    if isinstance(value, dict):
        return {str(k): (None if v is None else str(v)) for k, v in value.items()}
    if isinstance(value, list):
        result: dict[str, Optional[str]] = {}
        for item in value:
            text = str(item)
            if "=" in text:
                key, val = text.split("=", 1)
                result[key] = val
            else:
                result[text] = None
        return result
    return {}


def _normalize_ports(value: Any) -> list[str]:
    """Normalize ``ports`` entries (short string or long-form mapping) to strings."""
    if value is None:
        return []
    if not isinstance(value, list):
        value = [value]

    result: list[str] = []
    for item in value:
        if isinstance(item, dict):
            published = item.get("published")
            target = item.get("target")
            if published is not None and target is not None:
                result.append(f"{published}:{target}")
            elif target is not None:
                result.append(str(target))
            elif published is not None:
                result.append(str(published))
        else:
            result.append(str(item))
    return result


def _normalize_volumes(value: Any) -> list[str]:
    """Normalize ``volumes`` entries (short string or long-form mapping) to strings."""
    if value is None:
        return []
    if not isinstance(value, list):
        value = [value]

    result: list[str] = []
    for item in value:
        if isinstance(item, dict):
            source = item.get("source")
            target = item.get("target")
            if source is not None and target is not None:
                result.append(f"{source}:{target}")
            elif target is not None:
                result.append(str(target))
            elif source is not None:
                result.append(str(source))
        else:
            result.append(str(item))
    return result


def _normalize_restart(service: dict[str, Any]) -> Optional[str]:
    """Extract the restart policy, preferring the short ``restart`` key."""
    if "restart" in service and service["restart"] is not None:
        return str(service["restart"])
    # Long form: deploy.restart_policy.condition
    deploy = service.get("deploy")
    if isinstance(deploy, dict):
        policy = deploy.get("restart_policy")
        if isinstance(policy, dict) and policy.get("condition") is not None:
            return str(policy["condition"])
    return None


def _parse_service(name: str, service: dict[str, Any]) -> ServiceConfig:
    """Build a ServiceConfig from a raw service mapping."""
    if not isinstance(service, dict):
        service = {}
    return ServiceConfig(
        name=name,
        image=service.get("image"),
        ports=_normalize_ports(service.get("ports")),
        depends_on=_normalize_depends_on(service.get("depends_on")),
        volumes=_normalize_volumes(service.get("volumes")),
        environment=_normalize_environment(service.get("environment")),
        restart_policy=_normalize_restart(service),
    )


def _find_compose_file(path: str = ".") -> str:
    """Return the path to the first compose file found under ``path``.

    If ``path`` points directly at a file, that file is used as-is.
    """
    if os.path.isfile(path):
        return path

    for filename in COMPOSE_FILENAMES:
        candidate = os.path.join(path, filename)
        if os.path.isfile(candidate):
            return candidate

    searched = ", ".join(COMPOSE_FILENAMES)
    raise FileNotFoundError(
        f"No compose file found in '{os.path.abspath(path)}'. "
        f"Looked for: {searched}."
    )


def load_compose_file(path: str = ".") -> ComposeConfig:
    """Find, read, and parse a compose file into a ComposeConfig.

    ``path`` may be a directory to search or a direct path to a compose file.
    Raises FileNotFoundError if no compose file is found.
    """
    compose_path = _find_compose_file(path)

    with open(compose_path, "r", encoding="utf-8") as handle:
        raw = yaml.safe_load(handle) or {}

    if not isinstance(raw, dict):
        raise ValueError(
            f"Compose file '{compose_path}' did not parse to a mapping."
        )

    raw_services = raw.get("services") or {}
    services: dict[str, ServiceConfig] = {}
    for name, service in raw_services.items():
        services[str(name)] = _parse_service(str(name), service)

    return ComposeConfig(services=services, raw=raw, path=compose_path)
