"""Local stand-in for API Gateway: serves the Lambda handler over HTTP without AWS.

    uv run --frozen python -m loopcheck.local_server --port 8000

Each request is converted into an API Gateway HTTP API (payload 2.0) event and passed to
`loopcheck.aws_lambda.handler`, so the exact deployed code path can be exercised locally
(including from the Streamlit app in API mode). Binds to 127.0.0.1 by default.
"""

from __future__ import annotations

import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .aws_lambda import MAX_BODY_BYTES, handler


class _Handler(BaseHTTPRequestHandler):
    def _dispatch(self, method: str) -> None:
        length = int(self.headers.get("content-length") or 0)
        if length > MAX_BODY_BYTES:
            self.send_response(413)
            self.end_headers()
            return
        body = self.rfile.read(length).decode("utf-8", errors="replace") if length else ""
        event = {"version": "2.0", "rawPath": self.path.split("?")[0], "body": body, "isBase64Encoded": False,
                 "requestContext": {"http": {"method": method, "path": self.path}}}
        out = handler(event, None)
        payload = out["body"].encode("utf-8")
        self.send_response(out["statusCode"])
        for k, v in out["headers"].items():
            self.send_header(k, v)
        self.send_header("content-length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):  # noqa: N802
        self._dispatch("GET")

    def do_POST(self):  # noqa: N802
        self._dispatch("POST")

    def log_message(self, fmt, *args):  # quieter default logging
        print(f"[local_server] {self.address_string()} {fmt % args}")


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8000)
    args = p.parse_args(argv)
    srv = ThreadingHTTPServer((args.host, args.port), _Handler)
    print(f"LoopCheck local API on http://{args.host}:{args.port}  (GET /health, POST /inspect)")
    srv.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
