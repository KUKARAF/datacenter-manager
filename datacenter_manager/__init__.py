"""
datacenter_manager package
--------------------------

Provides :class:`Datacenter`, :class:`Service`, and :class:`PorkbunClient`
for coordinating multi-node infrastructure: DNS management, WireGuard topology,
and service health checks.

Install with: ``uv pip install .``
"""

from ._version import __version__
from .datacenter import Datacenter, main as datacenter_main
from .porkbun import PorkbunClient
from .service import Service

__all__ = ["__version__", "Datacenter", "PorkbunClient", "Service", "datacenter_main"]
