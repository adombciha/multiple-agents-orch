"""Prepare or apply a reversible local-priority Hindsight container replacement."""

import argparse
from copy import deepcopy
from datetime import datetime, timezone
import http.client
import ipaddress
import json
import os
from pathlib import Path
import socket
from urllib.parse import quote, urlsplit


class DockerConnection(http.client.HTTPConnection):

    def __init__(self):
        super().__init__("localhost", timeout=180)

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect("/var/run/docker.sock")


def docker(method, path, body=None):
    connection = DockerConnection()
    try:
        payload = None if body is None else json.dumps(body)
        connection.request(method, path, payload, {"Content-Type": "application/json"})
        response = connection.getresponse()
        data = response.read()
        if response.status >= 400:
            raise RuntimeError(f"Docker {method} failed with HTTP {response.status}")
        return json.loads(data) if data else None
    finally:
        connection.close()


def replacement_payload(original, source, resource_dir):
    if original["State"]["Running"]:
        raise ValueError(
            "Stop the original Hindsight container before applying replacement"
        )
    config = deepcopy(original["Config"])
    host = deepcopy(original["HostConfig"])
    if any(
        mount.get("Destination", "").startswith("/opt/hindsight-local")
        for mount in original.get("Mounts", [])
    ):
        raise ValueError("Hindsight already has the local guard deployment")
    networks = original.get("NetworkSettings", {}).get("Networks", {})
    if len(networks) > 1:
        raise ValueError(
            "Multiple Docker networks require a separately reviewed deployment"
        )
    config["Image"] = original["Image"]
    config["Entrypoint"] = [
        "python3",
        "/opt/hindsight-local/scripts/hindsight_local_entrypoint.py",
    ]
    config["Cmd"] = []
    config["StopTimeout"] = 360
    environment = dict(value.split("=", 1) for value in config.get("Env", []))
    base_url = environment.get("HINDSIGHT_API_LLM_BASE_URL", "")
    model = environment.get("HINDSIGHT_API_LLM_MODEL", "")
    address = urlsplit(base_url)
    local_address = address.hostname in ("localhost", "host.docker.internal")
    if address.hostname:
        try:
            local_address = (
                local_address or ipaddress.ip_address(address.hostname).is_private
            )
        except ValueError:
            pass
    if (
        environment.get("HINDSIGHT_API_LLM_PROVIDER") != "ollama"
        or not model
        or model.lower().endswith(("-cloud", ":cloud"))
        or address.scheme not in ("http", "https")
        or not local_address
        or address.username
        or address.password
    ):
        raise ValueError(
            "The existing deployment must use an explicit local Ollama endpoint and model"
        )
    environment["HINDSIGHT_API_WORKER_ENABLED"] = "false"
    environment["HINDSIGHT_API_WORKER_MAX_SLOTS"] = "1"
    environment["HINDSIGHT_API_LLM_MAX_CONCURRENT"] = "1"
    for operation in (
        "",
        "RETAIN_",
        "REFLECT_",
        "CONSOLIDATION_",
        "MENTAL_MODEL_REFRESH_",
    ):
        prefix = f"HINDSIGHT_API_{operation}LLM_"
        environment.update(
            {
                prefix + "PROVIDER": "ollama",
                prefix + "BASE_URL": base_url,
                prefix + "MODEL": model,
                prefix + "MAX_CONCURRENT": "1",
            }
        )
    for key in list(environment):
        if key.startswith("HINDSIGHT_API_WORKER_") and key.endswith(
            ("_RESERVED_SLOTS", "_MAX_SLOTS")
        ):
            if key != "HINDSIGHT_API_WORKER_MAX_SLOTS":
                environment[key] = "0"
    environment["HINDSIGHT_API_WORKER_CONSOLIDATION_RESERVED_SLOTS"] = "0"
    environment["HINDSIGHT_API_EMBEDDINGS_PROVIDER"] = "local"
    environment["HINDSIGHT_API_RERANKER_PROVIDER"] = "local"
    config["Env"] = [f"{key}={value}" for key, value in environment.items()]
    mounts = host.setdefault("Mounts", [])
    for relative in (
        "scripts/hindsight_local_entrypoint.py",
        "scripts/hindsight_worker_guard.py",
        "orchestrator/core/local_resources.py",
    ):
        path = source / relative
        if not path.is_file():
            raise ValueError(f"Required deployment source missing: {relative}")
        mounts.append(
            {
                "Type": "bind",
                "Source": str(path),
                "Target": f"/opt/hindsight-local/{relative}",
                "ReadOnly": True,
            }
        )
    mounts.append(
        {
            "Type": "bind",
            "Source": str(resource_dir),
            "Target": "/opt/hindsight-coordination",
        }
    )
    config["HostConfig"] = host
    if networks and host.get("NetworkMode") not in (
        "default",
        "bridge",
        "host",
        "none",
    ):
        config["NetworkingConfig"] = {
            "EndpointsConfig": {
                name: {"Aliases": value.get("Aliases") or []}
                for name, value in networks.items()
            }
        }
    return config


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument(
        "--resource-dir",
        type=Path,
        default=Path.home() / ".local/state/ollama-coordination",
    )
    args = parser.parse_args()
    source = Path(__file__).resolve().parents[1]
    original = docker("GET", "/containers/hindsight/json")
    payload = replacement_payload(original, source, args.resource_dir.resolve())
    backup = "hindsight-before-local-priority-" + datetime.now(timezone.utc).strftime(
        "%Y%m%dT%H%M%SZ"
    )
    print(f"Image: {original['Image']}")
    print(
        f"Keep existing mounts, ports, network, restart policy and private environment; backup: {backup}"
    )
    print(f"Shared lock directory: {args.resource_dir.resolve()}")
    if not args.apply:
        print("Read-only preview. Use --apply after reviewing the deployment.")
        return
    args.resource_dir.mkdir(parents=True, exist_ok=True)
    if args.resource_dir.stat().st_uid != os.getuid():
        raise ValueError("Shared lock directory must belong to the invoking user")
    docker("POST", f"/containers/hindsight/rename?name={quote(backup)}")
    created = None
    try:
        created = docker("POST", "/containers/create?name=hindsight", payload)
        docker("POST", f"/containers/{created['Id']}/start")
    except Exception:
        original_name = backup
        if created is not None:
            replacement = docker("GET", f"/containers/{created['Id']}/json")
            if not replacement["State"]["Running"]:
                docker("DELETE", f"/containers/{created['Id']}")
                docker("POST", f"/containers/{backup}/rename?name=hindsight")
                original_name = "hindsight"
        else:
            docker("POST", f"/containers/{backup}/rename?name=hindsight")
            original_name = "hindsight"
        print(f"Replacement failed; original container preserved as {original_name}.")
        raise
    print(f"Replacement started; original remains stopped as {backup}.")


if __name__ == "__main__":
    main()
