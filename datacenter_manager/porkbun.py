"""
Porkbun DNS client for osmosis.page infrastructure.

Handles:
- Rate-limited API calls (1 req/s, sequential)
- Parsing intranet.osmosis.page TXT → WireGuard IP map + node list
- Reading current A records for any node/service
- Updating A records (upsert via editByNameType)

Secrets loaded from .env (API_KEY, SECRET_KEY).
Everything else is hardcoded per spec.
"""

import os
import time
import dns.resolver
from typing import Optional
from dotenv import load_dotenv
import requests

load_dotenv()

_BASE_URL = "https://api.porkbun.com/api/json/v3"
_INTRANET_RECORD = "intranet"
_INTRANET_DOMAIN = "osmosis.page"
_TTL = "600"
_MIN_REQUEST_INTERVAL = 1.0  # seconds — Porkbun: sequential only, ~60 req/min


class PorkbunClient:
    """
    Rate-limited Porkbun DNS API client.

    All mutating calls are sequential and spaced at least 1 second apart.
    Read calls (DNS resolver, not API) are not rate-limited.
    """

    def __init__(self):
        self._api_key = os.getenv("API_KEY")
        self._secret_key = os.getenv("SECRET_KEY")
        if not self._api_key or not self._secret_key:
            raise RuntimeError(
                "API_KEY and SECRET_KEY must be set (e.g. in .env)"
            )
        self._last_request_at: float = 0.0

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _auth(self) -> dict:
        return {"apikey": self._api_key, "secretapikey": self._secret_key}

    def _post(self, path: str, extra: Optional[dict] = None) -> dict:
        """Rate-limited POST to Porkbun API."""
        elapsed = time.monotonic() - self._last_request_at
        if elapsed < _MIN_REQUEST_INTERVAL:
            time.sleep(_MIN_REQUEST_INTERVAL - elapsed)

        payload = {**self._auth(), **(extra or {})}
        resp = requests.post(f"{_BASE_URL}{path}", json=payload, timeout=30)
        self._last_request_at = time.monotonic()
        resp.raise_for_status()

        result = resp.json()
        if result.get("status") != "SUCCESS":
            raise RuntimeError(f"Porkbun API error on {path}: {result}")
        return result

    # ------------------------------------------------------------------
    # Node topology (DNS TXT — no API quota used)
    # ------------------------------------------------------------------

    def get_wg_ips(self) -> dict[str, str]:
        """
        Query intranet.osmosis.page TXT and return a {node: wg_ip} dict.

        Example TXT value:
            bigboy=10.10.0.1,malinowa=10.10.0.2,vultr=10.10.0.3,backup=10.10.0.4
        """
        fqdn = f"{_INTRANET_RECORD}.{_INTRANET_DOMAIN}"
        answers = dns.resolver.resolve(fqdn, "TXT")
        # TXT records may be split across multiple strings — join them
        txt = "".join(
            part.decode() if isinstance(part, bytes) else part
            for rdata in answers
            for part in rdata.strings
        )
        return dict(kv.split("=") for kv in txt.split(",") if "=" in kv)

    def get_nodes(self) -> list[str]:
        """Return sorted list of node names from intranet.osmosis.page TXT."""
        return sorted(self.get_wg_ips().keys())

    # ------------------------------------------------------------------
    # Node public IPs (DNS A — no API quota used)
    # ------------------------------------------------------------------

    def get_node_ip(self, node: str) -> str:
        """
        Resolve <node>.osmosis.page A record → current public IP.
        Uses system DNS resolver (not Porkbun API).
        """
        fqdn = f"{node}.{_INTRANET_DOMAIN}"
        answers = dns.resolver.resolve(fqdn, "A")
        return answers[0].address

    def get_all_node_ips(self) -> dict[str, str]:
        """Return {node: public_ip} for every node in the intranet TXT."""
        return {node: self.get_node_ip(node) for node in self.get_nodes()}

    # ------------------------------------------------------------------
    # DNS A-record updates (Porkbun API — rate limited)
    # ------------------------------------------------------------------

    def update_record(self, domain: str, ip: str, subdomain: str = "") -> None:
        """
        Upsert an A record via Porkbun editByNameType.

        Args:
            domain:    Apex domain, e.g. "osmosis.page" or "aglu.pl".
            ip:        IPv4 address to set.
            subdomain: Subdomain label, e.g. "bigboy" or "jellyfin".
                       Empty string updates the apex record.
        """
        path = f"/dns/editByNameType/{domain}/A/{subdomain}".rstrip("/")
        self._post(path, {"content": ip, "ttl": _TTL})

    def update_node_ip(self, node: str, ip: str) -> None:
        """Update <node>.osmosis.page A record to ip."""
        self.update_record(_INTRANET_DOMAIN, ip, subdomain=node)

    def update_service_ip(self, subdomain: str, ip: str) -> None:
        """Update <subdomain>.osmosis.page A record to ip."""
        self.update_record(_INTRANET_DOMAIN, ip, subdomain=subdomain)

    # ------------------------------------------------------------------
    # Convenience: ping (returns this machine's public IP, no quota cost)
    # ------------------------------------------------------------------

    def get_my_public_ip(self) -> str:
        """Call Porkbun /ping — returns the caller's current public IP."""
        result = self._post("/ping")
        return result["yourIp"]
