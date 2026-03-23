import os
import subprocess
import sys
import yaml
from flask import Flask, Response

# Direct import of the Service class from the same package.
# The Service implementation resides in datacenter_manager/service.py.
from .service import Service


class Datacenter:
    """
    Represents a datacenter configuration.

    Attributes:
        name (str): Name of the datacenter (obtained from Tailscale).
        services (list[Service]): List of Service instances running in this datacenter.
        public_ip (str): Current public IP address of the datacenter.
    """

    def __init__(self):
        """
        Initialise the Datacenter.
        The datacenter name is automatically obtained from Tailscale.
        """
        # Get the datacenter name from Tailscale
        self.name = _get_tailscale_hostname()

        # Load running services for this datacenter
        self.services = self._discover_services()

        # Determine the current public IP using icanhazip.com as fallback
        self.public_ip = self._get_public_ip()

    def _discover_services(self) -> list:
        """
        Discover all available services by searching for service-config.yaml files.
        
        Returns:
            list: List of Service instances found in the system.
        """
        import glob
        
        services = []
        
        # Search for service-config.yaml files in common locations
        # Start with the current directory and common service directories
        search_patterns = [
            "**/service-config.yaml",
            "*/service-config.yaml",
        ]
        
        found_files = []
        for pattern in search_patterns:
            try:
                # Use glob to find files recursively
                files = glob.glob(pattern, recursive=True)
                found_files.extend(files)
            except Exception as exc:
                print(f"Warning: Could not search for services with pattern {pattern}: {exc}")
                continue
        
        # Remove duplicates while preserving order
        unique_files = []
        seen = set()
        for file in found_files:
            if file not in seen:
                seen.add(file)
                unique_files.append(file)
        
        # Create Service instances for each found config file
        for config_file in unique_files:
            try:
                # Import Service class here to avoid circular imports
                from .service import Service
                service = Service(config_file)
                services.append(service)
                print(f"Discovered service: {service.domain} from {config_file}")
            except Exception as exc:
                print(f"Warning: Could not load service from {config_file}: {exc}")
                continue
        
        return services

    def _get_public_ip(self) -> str:
        """
        Get the current public IP address.
        
        Returns:
            str: The public IP address, or None if it cannot be determined.
        """
        try:
            import requests
            # Try to get the public IP using icanhazip.com
            response = requests.get("https://icanhazip.com", timeout=10)
            response.raise_for_status()
            # Strip whitespace from the response
            return response.text.strip()
        except Exception as exc:
            print(f"Warning: could not determine public IP ({exc}); using None")
            return None

    def to_dict(self):
        """Return a dictionary representation suitable for YAML serialization."""
        return {
            "name": self.name,
            "public_ip": self.public_ip,
            "services": [repr(s) for s in self.services],
        }


def _create_flask_app(datacenter: Datacenter) -> Flask:
    """
    Create a Flask application that serves the datacenter information as YAML.
    """
    app = Flask(__name__)

    @app.route("/", methods=["GET"])
    def get_datacenter_yaml():
        yaml_content = yaml.safe_dump(datacenter.to_dict())
        return Response(yaml_content, mimetype="application/x-yaml")

    return app


def _get_tailscale_hostname() -> str:
    """
    Retrieve the Tailscale hostname for the current machine.

    Returns:
        str: The hostname obtained from `tailscale status --self`.
    """
    try:
        # Run the tailscale command and capture its output.
        result = subprocess.run(
            ["tailscale", "status", "--self"],
            capture_output=True,
            text=True,
            check=True,
        )
        # The output format is: "<self-id> <hostname> ..."
        # We extract the second whitespace‑separated field.
        hostname = result.stdout.split()[1]
        return hostname
    except Exception as exc:
        # If anything goes wrong, fall back to localhost and log the error.
        print(f"Warning: could not determine Tailscale hostname ({exc}); using '127.0.0.1'")
        return "127.0.0.1"


def main() -> None:
    """
    Entry‑point used by the console script and ``python -m datacenter_manager``.
    Creates a Datacenter instance and runs the Flask development server.
    """
    # Example usage: create a Datacenter instance and serve its YAML representation.
    # The datacenter name is passed via an environment variable.
    # This is a required parameter - there should be no fallback to a default value.
    dc_name = os.getenv("DATACENTER_NAME")
    if not dc_name:
        raise RuntimeError(
            "DATACENTER_NAME environment variable is not set. "
            "Please set DATACENTER_NAME to the name of your datacenter before running this service."
        )
    dc = Datacenter()

    flask_app = _create_flask_app(dc)

    # Determine host and port as per the new requirements.
    host = _get_tailscale_hostname()
    port = 9123

    # Run the Flask development server; in production you would use a proper WSGI server.
    flask_app.run(host=host, port=port)


if __name__ == "__main__":
    # When executed directly, invoke the same entry‑point.
    main()
