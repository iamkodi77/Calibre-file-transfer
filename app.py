#!/usr/bin/env python3
"""Run the web dashboard and Calibre-compatible wireless server together."""
import argparse
import ipaddress
import logging
import os
import signal

from leafline.storage import Library
from leafline.web import create_app
from leafline.wireless import WirelessService


def main():
    parser = argparse.ArgumentParser(description="Leafline — a local wireless library for e-readers")
    parser.add_argument("--host", default=os.environ.get("LEAFLINE_BIND", "0.0.0.0"), help="Bind address (default: 0.0.0.0)")
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8000")), help="Web/OPDS port (default: 8000)")
    parser.add_argument("--wireless-port", type=int, default=int(os.environ.get("LEAFLINE_WIRELESS_PORT", "9090")), help="Calibre wireless port (default: 9090)")
    parser.add_argument("--data-dir", default=os.environ.get("LEAFLINE_DATA_DIR", "data"), help="Persistent library directory")
    parser.add_argument("--advertise-host", default=os.environ.get("LEAFLINE_ADVERTISE_HOST"), help="LAN IPv4 address to advertise if auto-detection is wrong")
    parser.add_argument("--no-mdns", action="store_true", help="Disable mDNS advertisements (UDP and manual connections still work)")
    args = parser.parse_args()
    if not 1 <= args.port <= 65535 or not 1 <= args.wireless_port <= 65535:
        parser.error("Ports must be between 1 and 65535.")
    if args.port == args.wireless_port:
        parser.error("The web and wireless servers need different TCP ports.")
    if args.advertise_host:
        try:
            ipaddress.IPv4Address(args.advertise_host)
        except ipaddress.AddressValueError:
            parser.error("The advertised host must be an IPv4 address.")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    library = Library(args.data_dir)
    wireless = WirelessService(library, host=args.host, port=args.wireless_port, http_port=args.port,
                               advertise_host=args.advertise_host, password=os.environ.get("LEAFLINE_WIRELESS_PASSWORD", ""),
                               publish_mdns=not args.no_mdns)
    wireless_started = wireless.start()
    library.log_event("server", "Server started", "Your library is ready on the network.")
    app = create_app(library, wireless, args.port, os.environ.get("LEAFLINE_HTTP_USER", "leafline"),
                     os.environ.get("LEAFLINE_HTTP_PASSWORD", ""))
    print(f"\nLeafline dashboard: http://{wireless.advertise_host}:{args.port}")
    print(f"OPDS catalog:        http://{wireless.advertise_host}:{args.port}/opds")
    if wireless_started:
        print(f"Wireless reader:    {wireless.advertise_host}:{wireless.port}\n", flush=True)
    else:
        print(f"Wireless reader:    unavailable ({wireless.error})\n", flush=True)
    # Service managers commonly use SIGTERM instead of Ctrl+C. Unpublish mDNS
    # and close device sockets for both shutdown paths.
    previous_sigterm = signal.getsignal(signal.SIGTERM)

    def handle_sigterm(_signum, _frame):
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, handle_sigterm)
    try:
        app.run(host=args.host, port=args.port, threaded=True, use_reloader=False)
    finally:
        wireless.stop()
        signal.signal(signal.SIGTERM, previous_sigterm)


if __name__ == "__main__":
    main()
