"""Deterministic transport fixture, not an AI agent or a quality benchmark.

Run with no arguments for stdin/stdout, or --http for a loopback HTTP endpoint.
The fixture demonstrates two unrelated native output formats.
"""

import argparse
import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def answer(value):
    if isinstance(value, dict):
        widespread = value.get("affected_users") == "all"
        return {"severity": "critical" if widespread else "low", "escalate": widespread}
    if value == "When is support available?":
        return "Support is available Monday to Friday, 09:00 to 17:00 UTC."
    return "I don't have that information."


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def do_POST(self):
        value = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        raw = json.dumps(answer(value)).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--http", action="store_true")
    parser.add_argument("--port", type=int, default=8099)
    args = parser.parse_args()
    if args.http:
        print(f"Smoke fixture listening on http://127.0.0.1:{args.port}", flush=True)
        ThreadingHTTPServer(("127.0.0.1", args.port), Handler).serve_forever()
    else:
        raw = sys.stdin.read()
        try:
            value = json.loads(raw)
        except ValueError:
            value = raw
        result = answer(value)
        sys.stdout.write(result if isinstance(result, str) else json.dumps(result))
