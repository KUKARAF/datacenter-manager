import glob
import subprocess
import threading
import time
import yaml
from flask import Flask, Response

from .porkbun import PorkbunClient, _INTRANET_DOMAIN
from .service import Service

_CYCLE_INTERVAL = 300  # seconds — roughly half the DNS TTL

# Latest datacenter snapshot, updated each cycle, served by Flask.
_snapshot: dict = {}
_snapshot_lock = threading.Lock()


class Datacenter:
    """
    Snapshot of this node's identity, services, and public IP.

    Attributes:
        name (str): Tailscale hostname of this node.
        services (list[Service]): Services discovered on this node.
        public_ip (str | None): Current public IP reported by Porkbun /ping.
    """

    def __init__(self, public_ip: str, services: list):
        self.name = _get_tailscale_hostname()
        self.public_ip = public_ip
        self.services = services

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "public_ip": self.public_ip,
            "services": [repr(s) for s in self.services],
        }


# ---------------------------------------------------------------------------
# Service discovery
# ---------------------------------------------------------------------------

def _discover_services() -> list[Service]:
    """Search cwd recursively for service-config.yaml files and return Service list."""
    found = set()
    for pattern in ("**/service-config.yaml", "*/service-config.yaml"):
        try:
            found.update(glob.glob(pattern, recursive=True))
        except Exception as exc:
            print(f"[WARN] Service discovery pattern {pattern!r} failed: {exc}")

    services = []
    for path in sorted(found):
        try:
            svc = Service(path)
            print(f"[INFO] Discovered service: {svc.domain} ({path})")
            services.append(svc)
        except Exception as exc:
            print(f"[WARN] Could not load service from {path}: {exc}")
    return services


# ---------------------------------------------------------------------------
# Service lifecycle
# ---------------------------------------------------------------------------

def _start_service(svc: Service) -> None:
    """Run `docker compose up -d` in the service directory."""
    subprocess.run(
        ["docker", "compose", "up", "-d"],
        cwd=svc.service_dir,
        check=True,
    )
    print(f"[INFO] Started {svc.domain} via docker compose")


def _stop_service(svc: Service) -> None:
    """Run `docker compose down` in the service directory."""
    subprocess.run(
        ["docker", "compose", "down"],
        cwd=svc.service_dir,
        check=True,
    )
    print(f"[INFO] Stopped {svc.domain} via docker compose")


# ---------------------------------------------------------------------------
# Coordinator cycle
# ---------------------------------------------------------------------------

def _run_cycle(pb: PorkbunClient) -> None:
    my_node = _get_tailscale_hostname()
    my_ip = pb.get_my_public_ip()
    wg_ips = pb.get_wg_ips()

    # 1. Keep this node's own A record current.
    pb.update_node_ip(my_node, my_ip)
    print(f"[INFO] Updated {my_node}.{_INTRANET_DOMAIN} → {my_ip}")

    # 2. Discover services each cycle so newly added ones are picked up.
    services = _discover_services()

    # 3. Update the Flask snapshot.
    with _snapshot_lock:
        _snapshot.update(Datacenter(my_ip, services).to_dict())

    # 4. For each service this node is listed in, handle failover and DNS.
    for svc in services:
        if my_node not in svc.data_centers:
            continue

        _handle_service(pb, svc, my_node, my_ip, wg_ips)


def _handle_service(
    pb: PorkbunClient,
    svc: Service,
    my_node: str,
    my_ip: str,
    wg_ips: dict[str, str],
) -> None:
    """Decide whether this node should own the service, start it, or stand by."""

    # Derive subdomain + apex — only osmosis.page supported for now.
    if svc.domain == _INTRANET_DOMAIN:
        subdomain, apex = "", _INTRANET_DOMAIN
    elif svc.domain.endswith(f".{_INTRANET_DOMAIN}"):
        subdomain = svc.domain.removesuffix(f".{_INTRANET_DOMAIN}")
        apex = _INTRANET_DOMAIN
    else:
        print(f"[WARN] {svc.domain}: non-osmosis domain not yet supported, skipping")
        return

    try:
        port = svc.get_port()
    except RuntimeError as exc:
        print(f"[WARN] {svc.domain}: {exc}")
        return

    my_index = svc.data_centers.index(my_node)
    higher_priority_nodes = svc.data_centers[:my_index]

    # Check if any higher-priority node is already running the service.
    for node in higher_priority_nodes:
        wg_ip = wg_ips.get(node)
        if not wg_ip:
            print(f"[WARN] {svc.domain}: no WireGuard IP known for {node!r}, skipping it")
            continue
        if svc.is_port_up(port, host=wg_ip):
            print(f"[INFO] {svc.domain} is healthy on {node} ({wg_ip}:{port}) — standing by")
            return

    # No higher-priority node is healthy. This node should own the service.
    local_healthy = svc.is_port_up(port)

    if local_healthy:
        pb.update_record(apex, my_ip, subdomain=subdomain)
        print(f"[INFO] {svc.domain} healthy locally — A → {my_ip}")
        return

    # Service is not running locally — start it.
    print(f"[INFO] {svc.domain} is down and no higher-priority node is healthy — starting locally")
    try:
        _start_service(svc)
    except subprocess.CalledProcessError as exc:
        print(f"[ERROR] {svc.domain}: docker compose up failed: {exc}")
        return

    # Update DNS optimistically; next cycle confirms health via is_port_up.
    pb.update_record(apex, my_ip, subdomain=subdomain)
    print(f"[INFO] {svc.domain} started — A → {my_ip} (health confirmed next cycle)")


def coordinator_loop() -> None:
    """Run _run_cycle every _CYCLE_INTERVAL seconds, logging but not dying on errors."""
    pb = PorkbunClient()
    while True:
        try:
            _run_cycle(pb)
        except Exception as exc:
            print(f"[ERROR] Coordinator cycle failed: {exc}")
        time.sleep(_CYCLE_INTERVAL)


# ---------------------------------------------------------------------------
# Flask introspection server
# ---------------------------------------------------------------------------

def _create_flask_app() -> Flask:
    app = Flask(__name__)

    @app.route("/", methods=["GET"])
    def get_datacenter_yaml():
        with _snapshot_lock:
            content = yaml.safe_dump(dict(_snapshot))
        return Response(content, mimetype="application/x-yaml")

    return app


# ---------------------------------------------------------------------------
# Tailscale hostname helper
# ---------------------------------------------------------------------------

def _get_tailscale_hostname() -> str:
    try:
        result = subprocess.run(
            ["tailscale", "status", "--self"],
            capture_output=True, text=True, check=True,
        )
        return result.stdout.split()[1]
    except Exception as exc:
        print(f"[WARN] Could not determine Tailscale hostname ({exc}); using 'localhost'")
        return "localhost"


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    """
    Start the coordinator loop and the Flask introspection server.

    The coordinator loop runs in the main thread.
    Flask runs in a daemon thread on the Tailscale IP, port 9123.
    """
    flask_app = _create_flask_app()
    host = _get_tailscale_hostname()

    flask_thread = threading.Thread(
        target=lambda: flask_app.run(host=host, port=9123),
        daemon=True,
        name="flask-introspection",
    )
    flask_thread.start()
    print(f"[INFO] Introspection server listening on http://{host}:9123/")

    coordinator_loop()


if __name__ == "__main__":
    main()
