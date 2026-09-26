from __future__ import annotations

import json
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


port = int(sys.argv[1])
root = Path(sys.argv[2])
model = sys.argv[3]
root.mkdir(parents=True, exist_ok=True)
counter = root / "starts.txt"
starts = int(counter.read_text(encoding="utf-8") or "0") if counter.exists() else 0
counter.write_text(str(starts + 1), encoding="utf-8")
(root / "server.pid").write_text(str(os.getpid()), encoding="utf-8")


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        if self.path == "/health":
            payload = {"status": "ok"}
        elif self.path == "/v1/models":
            payload = {"data": [{"id": model}]}
        else:
            self.send_error(404)
            return
        body = json.dumps(payload).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args: object) -> None:
        pass


ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()
