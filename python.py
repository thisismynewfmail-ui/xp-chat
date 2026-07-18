#!/usr/bin/env python3
"""TACHI-COM — Section 9 intranet chat terminal.

XP-styled web chat interface for any OpenAI-compatible endpoint
(llama.cpp server, LM Studio, vLLM, ollama, ...).

Run:  python python.py  [--host 127.0.0.1] [--port 8484] [--no-browser]

Everything is Python standard library — no pip installs needed, works on
Windows and Linux alike. Defaults are seeded from the bundled config file
(`.config` or `config.json`) on first run and persisted in ./data/.
"""

import argparse
import os
import sys
import threading
import webbrowser

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)

from xpchat.config import AppConfig          # noqa: E402
from xpchat.server import create_server      # noqa: E402

BANNER = r"""
  _____  _    ____ _   _ ___       ____ ___  __  __
 |_   _|/ \  / ___| | | |_ _|     / ___/ _ \|  \/  |
   | | / _ \| |   | |_| || |_____| |  | | | | |\/| |
   | |/ ___ \ |___|  _  || |_____| |__| |_| | |  | |
   |_/_/   \_\____|_| |_|___|     \____\___/|_|  |_|
        SECTION 9 // STAND ALONE COMPLEX // v2.1
"""


def main():
    ap = argparse.ArgumentParser(
        description="TACHI-COM: XP-themed web chat for an OpenAI-compatible endpoint.")
    ap.add_argument("--host", default="127.0.0.1", help="bind address (default 127.0.0.1)")
    ap.add_argument("--port", type=int, default=8484, help="UI port (default 8484)")
    ap.add_argument("--data-dir", default=None, help="state directory (default ./data)")
    ap.add_argument("--no-browser", action="store_true", help="do not open a browser window")
    args = ap.parse_args()

    data_dir = args.data_dir or os.path.join(BASE_DIR, "data")
    cfg = AppConfig(BASE_DIR, data_dir)

    httpd = create_server(args.host, args.port, cfg)
    url = f"http://{args.host}:{args.port}/"

    print(BANNER)
    print(f"  uplink   : {cfg.settings.get('endpoint')}")
    print(f"  console  : {url}")
    print(f"  state    : {data_dir}")
    print("  Ctrl-C to power down.\n")

    if not args.no_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\npower down. the net is vast and infinite.")
        httpd.shutdown()


if __name__ == "__main__":
    main()
