"""Resolve local Ollama endpoints without persisting a WSL gateway address."""

import ipaddress
import subprocess
from urllib.parse import urlsplit

import requests


def get_windows_host_ip() -> str | None:
    try:
        result = subprocess.run(
            ["ip", "-4", "route", "show", "default"],
            capture_output=True,
            text=True,
            check=True,
            timeout=2,
        )
        for line in result.stdout.splitlines():
            parts = line.split()
            if parts and parts[0] == "default" and "via" in parts:
                index = parts.index("via") + 1
                if index < len(parts):
                    return str(ipaddress.IPv4Address(parts[index]))
    except (OSError, subprocess.SubprocessError, ValueError):
        pass
    return None


def available(url: str) -> bool:
    try:
        response = requests.get(
            f"{url.rstrip('/')}/api/tags",
            timeout=2,
            allow_redirects=False,
            proxies={"http": None, "https": None, "all": None},
        )
        try:
            data = response.json()
            return (
                response.status_code == 200
                and isinstance(data, dict)
                and isinstance(data.get("models"), list)
            )
        finally:
            response.close()
    except (requests.RequestException, ValueError):
        return False


def invalidate(orchestrator) -> None:
    for name in (
        "_resolved_ollama_url",
        "_installed_ollama_models_cache",
        "_ollama_chat_models_cache",
        "_ollama_no_think_models",
        "_tev1_routing_cache",
    ):
        vars(orchestrator).pop(name, None)


def resolve(orchestrator) -> str:
    configured = orchestrator.config.get("ollama_url", "auto").rstrip("/")
    previous = getattr(orchestrator, "_ollama_endpoint_setting", None)
    if previous is not None and previous != configured:
        invalidate(orchestrator)
    orchestrator._ollama_endpoint_setting = configured
    if configured != "auto":
        return configured
    cached = getattr(orchestrator, "_resolved_ollama_url", None)
    if cached:
        return cached

    local_url = "http://localhost:11434"
    if available(local_url):
        orchestrator._resolved_ollama_url = local_url
        return local_url
    gateway = get_windows_host_ip()
    if gateway:
        windows_url = f"http://{gateway}:11434"
        if available(windows_url):
            orchestrator._resolved_ollama_url = windows_url
            return windows_url
    raise requests.ConnectionError(
        "No Ollama endpoint detected on loopback or the current default gateway."
    )


def request(orchestrator, send, url: str, **kwargs):
    automatic = orchestrator.config.get("ollama_url", "auto").rstrip("/") == "auto"
    path = urlsplit(url).path
    # Inference may already have run when a connection drops; only retry metadata.
    attempts = 2 if automatic and path in {"/api/tags", "/api/show"} else 1
    for attempt in range(attempts):
        target = f"{resolve(orchestrator)}{path}" if automatic else url
        if automatic:
            kwargs.setdefault("allow_redirects", False)
            kwargs.setdefault("proxies", {"http": None, "https": None, "all": None})
        try:
            return send(target, **kwargs)
        except (requests.ConnectionError, requests.Timeout):
            if automatic:
                invalidate(orchestrator)
            if attempt + 1 == attempts:
                raise
