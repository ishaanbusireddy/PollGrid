#!/usr/bin/env python3
"""PollGrid — real-time US political intelligence platform.

Zero-install build: Python 3.10+ standard library only.
No PostgreSQL, no pip install, no npm.

    python run.py            start everything (DB created, sources seeded, ingestion running)
    python run.py --no-browser
    python run.py --port 8811
"""
import argparse
import sys
import webbrowser

if sys.version_info < (3, 10):
    sys.exit("PollGrid requires Python 3.10+")

sys.path.insert(0, __file__.rsplit("/", 1)[0])


def main() -> None:
    ap = argparse.ArgumentParser(description="PollGrid")
    ap.add_argument("--port", type=int, default=None)
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--no-ingest", action="store_true", help="serve only; do not start source threads")
    args = ap.parse_args()

    from api.server import create_app

    app = create_app(port=args.port, start_ingestion=not args.no_ingest)
    url = f"http://localhost:{app.port}/"
    print(f"PollGrid up at {url}")
    if not args.no_browser:
        try:
            webbrowser.open(url)
        except Exception:
            pass
    try:
        app.serve_forever()
    except KeyboardInterrupt:
        print("\nshutting down…")
        app.shutdown()


if __name__ == "__main__":
    main()
