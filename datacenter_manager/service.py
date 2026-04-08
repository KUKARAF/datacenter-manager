import os
import yaml
import socket
import subprocess
from typing import Optional, Dict, Any

import requests


class Service:
    """
    Represents a service configuration loaded from a YAML file.

    The constructor validates that all required parameters are present.
    Optional parameters are exposed with sensible defaults.
    """

    # Required top‑level keys in every service‑config.yaml
    REQUIRED_PARAMS = {"data_centers", "domain"}

    def __init__(self, yaml_path: str):
        """
        Initialise the Service instance.

        Args:
            yaml_path: Path to the YAML configuration file.

        Raises:
            FileNotFoundError: If the supplied path does not exist.
            AssertionError:    If any required parameter is missing.
            yaml.YAMLError:    If the file cannot be parsed as valid YAML.
        """
        if not os.path.isfile(yaml_path):
            raise FileNotFoundError(f"YAML configuration file not found: {yaml_path}")

        with open(yaml_path, "r", encoding="utf-8") as f:
            try:
                self.config = yaml.safe_load(f) or {}
            except yaml.YAMLError as exc:
                raise yaml.YAMLError(f"Error parsing YAML file {yaml_path}: {exc}")

        # Validate required keys
        missing = self.REQUIRED_PARAMS - self.config.keys()
        if missing:
            raise AssertionError(
                f"Missing required parameter(s) in {yaml_path}: {', '.join(sorted(missing))}"
            )

        # Store the YAML path for Docker Compose parsing
        self.yaml_path = yaml_path
        self.service_dir = os.path.dirname(yaml_path)

        # Expose configuration values
        self.data_centers = self.config["data_centers"]
        self.domain = self.config["domain"]
        self.subdomains = self.config.get("subdomains", [])
        self.auto_update = self.config.get("auto_update", False)

        # Initialize Docker Compose related attributes
        self.external_ports = []
        self.mounted_volumes = []

        # Parse Docker Compose file if it exists
        self._parse_docker_compose()

    def __repr__(self) -> str:
        return (
            f"Service(domain={self.domain!r}, data_centers={self.data_centers!r}, "
            f"subdomains={self.subdomains!r}, auto_update={self.auto_update!r}, "
            f"external_ports={self.external_ports!r}, mounted_volumes={self.mounted_volumes!r})"
        )

    def _parse_docker_compose(self) -> None:
        """
        Parse Docker Compose files to extract external ports and mounted volumes.

        Looks for common Docker Compose file names in the service directory:
        - docker-compose.yml / docker-compose.yaml
        - compose.yml / compose.yaml
        """
        compose_path = None
        for filename in ("docker-compose.yml", "docker-compose.yaml", "compose.yml", "compose.yaml"):
            candidate = os.path.join(self.service_dir, filename)
            if os.path.isfile(candidate):
                compose_path = candidate
                break

        if not compose_path:
            return

        try:
            with open(compose_path, "r", encoding="utf-8") as f:
                compose_config = yaml.safe_load(f) or {}

            for service_config in (compose_config.get("services") or {}).values():
                if not service_config:
                    continue

                for port_mapping in service_config.get("ports", []):
                    if isinstance(port_mapping, str):
                        parts = port_mapping.split(":")
                        if len(parts) >= 2:
                            self.external_ports.append(parts[0])
                    elif isinstance(port_mapping, dict):
                        host_port = port_mapping.get("published", "")
                        if host_port:
                            self.external_ports.append(str(host_port))

                for volume_mapping in service_config.get("volumes", []):
                    if isinstance(volume_mapping, str):
                        parts = volume_mapping.split(":")
                        if len(parts) >= 2:
                            self.mounted_volumes.append(parts[0])
                    elif isinstance(volume_mapping, dict):
                        host_path = volume_mapping.get("source", "")
                        if host_path:
                            self.mounted_volumes.append(host_path)

        except (yaml.YAMLError, IOError) as exc:
            print(f"Warning: Could not parse Docker Compose file {compose_path}: {exc}")
            self.external_ports = []
            self.mounted_volumes = []

    # --------------------------------------------------------------------- #
    # Port access
    # --------------------------------------------------------------------- #

    def get_port(self) -> int:
        """
        Return the first external port exposed by the service's Docker Compose file.

        Raises:
            RuntimeError: If no external ports are found in the Docker Compose file.
        """
        if not self.external_ports:
            raise RuntimeError(
                f"No external ports found for service {self.domain!r}. "
                "Ensure a Docker Compose file with a 'ports' mapping exists alongside service-config.yaml."
            )
        return int(self.external_ports[0])

    # --------------------------------------------------------------------- #
    # Health checks
    # --------------------------------------------------------------------- #

    def is_port_up(self, port: int, host: str = "localhost", timeout: int = 3) -> bool:
        """
        Check if a specific port is up and accepting connections.

        Args:
            port: The port number to check.
            host: The hostname or IP address to check (default: "localhost").
            timeout: Connection timeout in seconds (default: 3).

        Returns:
            True if the port is accepting connections, False otherwise.
        """
        if not (1 <= port <= 65535):
            raise ValueError(f"Invalid port number: {port}.")

        try:
            with socket.create_connection((host, port), timeout=timeout):
                return True
        except (socket.timeout, ConnectionRefusedError, OSError):
            return False

    def is_domain_up(self, domain: str, port: int = 80, timeout: int = 5) -> bool:
        """Check if a domain is responding to HTTP/HTTPS requests."""
        if not (1 <= port <= 65535):
            raise ValueError(f"Invalid port number: {port}.")

        protocol = "https://" if port == 443 else "http://"
        url = f"{protocol}{domain}" if port in (80, 443) else f"{protocol}{domain}:{port}"

        try:
            response = requests.head(url, timeout=timeout, allow_redirects=True)
            return response.status_code < 500
        except requests.RequestException:
            try:
                response = requests.get(url, timeout=timeout, stream=True)
                return response.status_code < 500
            except requests.RequestException:
                return False
