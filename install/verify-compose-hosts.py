"""Validate required reader host mappings in normalized Compose JSON."""
import ipaddress
import json
import sys

REQUIRED = ("api", "worker", "ssrf_proxy")
HOST = "host.docker.internal"

def validate(document, gateway):
    if ipaddress.ip_address(gateway).version != 4:
        raise ValueError("IPv4 gateway required")
    services = document.get("services") if isinstance(document, dict) else None
    if not isinstance(services, dict):
        raise ValueError("missing services")
    for name in REQUIRED:
        service = services.get(name)
        if not isinstance(service, dict):
            raise ValueError("missing required service " + name)
        hosts = service.get("extra_hosts")
        if isinstance(hosts, dict):
            matches = [hosts[HOST]] if HOST in hosts else []
        elif isinstance(hosts, list):
            matches = []
            for row in hosts:
                if not isinstance(row, str):
                    raise ValueError("invalid host row in " + name)
                key, separator, value = row.partition("=")
                if not separator:
                    key, separator, value = row.partition(":")
                if key == HOST and separator:
                    matches.append(value)
        else:
            matches = []
        if matches != [gateway]:
            raise ValueError("missing or mismatched reader host in " + name)
    return len(REQUIRED)

def main():
    try:
        if len(sys.argv) != 2:
            raise ValueError("expected gateway")
        count = validate(json.load(sys.stdin), sys.argv[1])
    except (ValueError, TypeError):
        print("not_installed")
        print("refuse: composed config lacks an exact reader host mapping for every required service")
        return 2
    print("composed_host_docker_internal=" + str(count))
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
