"""
Run the datacenter Flask server.

Executing ``python -m datacenter_manager`` will start the server using the
environment variable ``DATACENTER_NAME`` (required) and will
listen on the Tailscale hostname on port 9123.
"""

from .datacenter import main

if __name__ == "__main__":
    main()
