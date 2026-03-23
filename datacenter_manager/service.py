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
        - docker-compose.yml
        - docker-compose.yaml
        - compose.yml
        - compose.yaml
        """
        # Common Docker Compose file names
        compose_files = [
            "docker-compose.yml",
            "docker-compose.yaml", 
            "compose.yml",
            "compose.yaml"
        ]
        
        compose_path = None
        for filename in compose_files:
            potential_path = os.path.join(self.service_dir, filename)
            if os.path.isfile(potential_path):
                compose_path = potential_path
                break
        
        if not compose_path:
            # No Docker Compose file found, leave attributes as empty lists
            return
            
        try:
            with open(compose_path, "r", encoding="utf-8") as f:
                compose_config = yaml.safe_load(f) or {}
                
            # Extract services from the compose file
            services = compose_config.get("services", {})
            
            # Parse each service for ports and volumes
            for service_name, service_config in services.items():
                if service_config:
                    # Extract ports
                    ports = service_config.get("ports", [])
                    for port_mapping in ports:
                        if isinstance(port_mapping, str):
                            # Parse string format like "8080:80"
                            parts = port_mapping.split(":")
                            if len(parts) >= 2:
                                host_port = parts[0]
                                self.external_ports.append(host_port)
                        elif isinstance(port_mapping, dict):
                            # Parse dict format
                            host_port = port_mapping.get("published", "")
                            if host_port:
                                self.external_ports.append(str(host_port))
                        elif isinstance(port_mapping, list) and len(port_mapping) >= 2:
                            # Parse list format like ["8080", "80"]
                            host_port = str(port_mapping[0])
                            self.external_ports.append(host_port)
                    
                    # Extract volumes
                    volumes = service_config.get("volumes", [])
                    for volume_mapping in volumes:
                        if isinstance(volume_mapping, str):
                            # Parse string format like "/host/path:/container/path"
                            parts = volume_mapping.split(":")
                            if len(parts) >= 2:
                                host_path = parts[0]
                                self.mounted_volumes.append(host_path)
                        elif isinstance(volume_mapping, dict):
                            # Parse dict format
                            host_path = volume_mapping.get("source", "")
                            if host_path:
                                self.mounted_volumes.append(host_path)
                            
        except (yaml.YAMLError, IOError) as exc:
            print(f"Warning: Could not parse Docker Compose file {compose_path}: {exc}")
            # Leave attributes as empty lists if parsing fails
            self.external_ports = []
            self.mounted_volumes = []

    # --------------------------------------------------------------------- #
    # Helper methods for interacting with datacenter endpoints
    # --------------------------------------------------------------------- #

    def _fetch_datacenter_info(self, datacenter: str) -> Dict[str, Any]:
        """
        Retrieve the datacenter information from its Flask endpoint.

        Args:
            datacenter: The tailscale hostname of the datacenter (e.g. ``bigboy``).

        Returns:
            A dictionary parsed from the YAML response.

        Raises:
            RuntimeError: If the endpoint cannot be reached or returns a non‑200 status.
        """
        url = f"http://{datacenter}:9123/"
        try:
            response = requests.get(url, timeout=3)
            response.raise_for_status()
        except requests.RequestException as exc:
            raise RuntimeError(f"Unable to reach datacenter {datacenter} on port 9123: {exc}")

        try:
            data = yaml.safe_load(response.text) or {}
        except yaml.YAMLError as exc:
            raise RuntimeError(f"Failed to parse YAML from datacenter {datacenter}: {exc}")

        return data

    # --------------------------------------------------------------------- #
    # Helper methods for DNS updates (Python implementation, no .sh scripts)
    # --------------------------------------------------------------------- #

    def _porkbun_update(self, domain: str, ip: str, subdomain: Optional[str] = None) -> None:
        """
        Perform a DNS A‑record update via Porkbun's API.

        Args:
            domain: The apex domain (e.g. ``example.com``).
            ip: The IPv4 address to set.
            subdomain: Optional sub‑domain (e.g. ``www``). If ``None`` the apex is updated.

        Raises:
            RuntimeError: If the API call fails or returns an error.
        """
        api_key = os.getenv("API_KEY")
        secret_key = os.getenv("SECRET_KEY")
        if not api_key or not secret_key:
            raise RuntimeError("Porkbun API credentials (API_KEY, SECRET_KEY) not set in environment")

        record_type = "A"
        if subdomain:
            url = f"https://api.porkbun.com/api/json/v3/dns/editByNameType/{domain}/{record_type}/{subdomain}"
        else:
            url = f"https://api.porkbun.com/api/json/v3/dns/editByNameType/{domain}/{record_type}"

        payload = {
            "apikey": api_key,
            "secretapikey": secret_key,
            "content": ip,
            "ttl": "600",
        }

        try:
            # Increased timeout from 5 to 30 seconds to handle potential network latency
            resp = requests.post(url, json=payload, timeout=30)
            resp.raise_for_status()
            result = resp.json()
        except requests.RequestException as exc:
            raise RuntimeError(f"Porkbun API request failed: {exc}")

        if result.get("status") != "SUCCESS":
            raise RuntimeError(f"Porkbun API error: {result}")

    def is_port_up(self, port: int, host: str = "localhost", timeout: int = 3) -> bool:
        """
        Check if a specific port is up and accepting connections.

        Args:
            port: The port number to check.
            host: The hostname or IP address to check (default: "localhost").
            timeout: Connection timeout in seconds (default: 3).

        Returns:
            True if the port is up and accepting connections, False otherwise.

        Raises:
            ValueError: If port is not a valid port number (1-65535).
        """
        # Validate port number
        if not (1 <= port <= 65535):
            raise ValueError(f"Invalid port number: {port}. Port must be between 1 and 65535.")

        try:
            # Create a socket connection to test if port is open
            with socket.create_connection((host, port), timeout=timeout):
                return True
        except (socket.timeout, ConnectionRefusedError, OSError):
            return False

    def is_domain_up(self, domain: str, port: int = 80, timeout: int = 5) -> bool:
        """
        Check if a domain is up and responding to HTTP/HTTPS requests.

        Args:
            domain: The domain name to check (e.g., "example.com").
            port: The port to check (default: 80 for HTTP).
            timeout: Connection timeout in seconds (default: 5).

        Returns:
            True if the domain is up and responding, False otherwise.

        Raises:
            ValueError: If port is not a valid port number (1-65535).
        """
        # Validate port number
        if not (1 <= port <= 65535):
            raise ValueError(f"Invalid port number: {port}. Port must be between 1 and 65535.")

        # Determine protocol based on port
        protocol = "https://" if port == 443 else "http://"
        url = f"{protocol}{domain}"
        
        # If a non-standard port is specified, include it in the URL
        if port not in [80, 443]:
            url = f"{protocol}{domain}:{port}"

        try:
            # Try to make a HEAD request first (faster, no body download)
            response = requests.head(url, timeout=timeout, allow_redirects=True)
            return response.status_code < 500  # Consider 2xx, 3xx, 4xx as "up"
        except requests.RequestException:
            try:
                # Fall back to GET request if HEAD is not supported
                response = requests.get(url, timeout=timeout, stream=True)
                return response.status_code < 500
            except requests.RequestException:
                return False

    def get_service_ip(self, domain: Optional[str] = None) -> str:
        """
        Get the IP address of the service host via DNS resolution.

        Args:
            domain: The domain name to resolve. If None, uses the service's configured domain.

        Returns:
            The IPv4 address as a string.

        Raises:
            RuntimeError: If DNS resolution fails for the domain.
        """
        # Use the service's domain if none is provided
        target_domain = domain if domain is not None else self.domain

        try:
            # Perform DNS resolution to get the IP address
            ip_address = socket.gethostbyname(target_domain)
            return ip_address
        except socket.gaierror as exc:
            raise RuntimeError(f"Failed to resolve DNS for domain {target_domain}: {exc}")

    # --------------------------------------------------------------------- #
    # Public API
    # --------------------------------------------------------------------- #

    def get_datacenter(self) -> Dict[str, Any]:
        """
        Iterate over the configured ``data_centers`` and return the first reachable
        datacenter's information.

        Returns:
            The parsed YAML dictionary from the reachable datacenter.

        Raises:
            RuntimeError: If none of the configured datacenters are reachable.
        """
        last_error: Optional[Exception] = None
        for dc in self.data_centers:
            try:
                return self._fetch_datacenter_info(dc)
            except RuntimeError as exc:
                # Remember the error but continue trying the next datacenter.
                last_error = exc
                continue

        # If we exit the loop, all attempts failed.
        raise RuntimeError(
            f"All configured datacenters are unreachable: {self.data_centers}"
        ) from (last_error if last_error else None)

    def dns_update(self, datacenter: Optional[str] = None) -> None:
        """
        Compare the DNS A‑record for the service's domain with the public IP reported
        by a datacenter. If they differ, trigger an update using the Python Porkbun
        implementation (no external shell scripts).

        Args:
            datacenter: Optional explicit datacenter hostname to query. If omitted,
                        ``get_datacenter`` is used to discover the first reachable one.

        Raises:
            RuntimeError: If the specified datacenter is unreachable or if all
                          datacenters are unreachable when ``datacenter`` is ``None``.
        """
        # -----------------------------------------------------------------
        # Resolve the datacenter information
        # -----------------------------------------------------------------
        if datacenter:
            # Verify the supplied datacenter is reachable.
            dc_info = self._fetch_datacenter_info(datacenter)
        else:
            # Auto‑discover the first reachable datacenter.
            dc_info = self.get_datacenter()

        public_ip = dc_info.get("public_ip")
        if not public_ip:
            raise RuntimeError(
                f"Datacenter response does not contain a 'public_ip' field: {dc_info}"
            )

        # -----------------------------------------------------------------
        # Resolve the current DNS IP for the service's domain (apex)
        # -----------------------------------------------------------------
        try:
            current_dns_ip = socket.gethostbyname(self.domain)
        except socket.gaierror as exc:
            raise RuntimeError(f"Failed to resolve DNS for domain {self.domain}: {exc}")

        # -----------------------------------------------------------------
        # Compare and act for the apex domain
        # -----------------------------------------------------------------
        if current_dns_ip != public_ip:
            print(
                f"[INFO] DNS mismatch for {self.domain}: DNS={current_dns_ip} vs "
                f"Datacenter={public_ip}. Updating apex record..."
            )
            self._porkbun_update(self.domain, public_ip)
        else:
            print(f"[INFO] DNS for {self.domain} is up‑to‑date ({current_dns_ip}).")

        # -----------------------------------------------------------------
        # If subdomains are defined, repeat the check/update for each.
        # -----------------------------------------------------------------
        for sub in self.subdomains:
            full_name = f"{sub}.{self.domain}"
            try:
                sub_dns_ip = socket.gethostbyname(full_name)
            except socket.gaierror as exc:
                raise RuntimeError(f"Failed to resolve DNS for subdomain {full_name}: {exc}")

            if sub_dns_ip != public_ip:
                print(
                    f"[INFO] DNS mismatch for {full_name}: DNS={sub_dns_ip} vs "
                    f"Datacenter={public_ip}. Updating subdomain record..."
                )
                self._porkbun_update(self.domain, public_ip, subdomain=sub)
            else:
                print(f"[INFO] DNS for {full_name} is up‑to‑date ({sub_dns_ip}).")
