"""Fake TEI embeddings server: hashed bag-of-words vectors (384 dims), deterministic.

Stands in for the local embedding model, which this sandbox cannot download (HuggingFace is
blocked). Optional EMBED_DELAY_MS simulates model compute per request.
"""
import hashlib
import json
import math
import os
import re
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DIM = 384
DELAY = float(os.environ.get("EMBED_DELAY_MS", "0")) / 1000.0


def embed(text: str) -> list[float]:
    v = [0.0] * DIM
    for tok in re.findall(r"\w+", text.lower()):
        h = int.from_bytes(hashlib.blake2b(tok.encode(), digest_size=8).digest(), "little")
        v[h % DIM] += 1.0 if (h >> 32) & 1 else -1.0
    n = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / n for x in v]


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, obj):
        body = json.dumps(obj).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.startswith("/info"):
            self._send({"model_id": "fake-hash-384", "max_input_length": 512, "model_dtype": "float32"})
        else:
            self._send({"ok": True})

    def do_POST(self):
        data = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        inputs = data.get("inputs", [])
        if isinstance(inputs, str):
            inputs = [inputs]
        if DELAY:
            time.sleep(DELAY * max(1, len(inputs)) ** 0.5)
        self._send([embed(t) for t in inputs])


if __name__ == "__main__":
    ThreadingHTTPServer(("127.0.0.1", int(sys.argv[1])), H).serve_forever()
