"""Return the result of one TCP handshake, not a later process-state sample."""
import argparse
import ipaddress
import socket

def probe(host, port):
    try:
        with socket.create_connection((host, port), timeout=3):
            print("tcp_connected=1")
        return 0
    except OSError as exc:
        print("tcp_connected=0")
        print("tcp_error=" + type(exc).__name__)
        return 1

def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("host")
    parser.add_argument("port", type=int)
    args = parser.parse_args(argv)
    try:
        ipaddress.IPv4Address(args.host)
        if not 1 <= args.port <= 65535:
            raise ValueError("port")
    except ValueError:
        parser.error("an exact IPv4 address and TCP port are required")
    return probe(args.host, args.port)

if __name__ == "__main__":
    raise SystemExit(main())
