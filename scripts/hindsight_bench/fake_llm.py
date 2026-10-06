"""Fake OpenAI-compatible chat server for driving a real Hermes AIAgent turn. LLM_DELAY_S simulates model time."""
import json
import os
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DELAY = float(os.environ.get("LLM_DELAY_S", "2"))
TEXT = "Certo, anotei. Resposta curta com evidência, como você prefere."


class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        self._json({"object": "list", "data": [{"id": "fake-model", "object": "model", "context_length": 128000}]})

    def do_POST(self):
        req = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        time.sleep(DELAY)
        model = req.get("model", "fake-model")
        usage = {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120}
        if req.get("stream"):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Connection", "close")
            self.end_headers()
            base = {"id": "c1", "object": "chat.completion.chunk", "created": int(time.time()), "model": model}
            for delta, fin in (({"role": "assistant", "content": TEXT}, None), ({}, "stop")):
                chunk = dict(base, choices=[{"index": 0, "delta": delta, "finish_reason": fin}])
                self.wfile.write(f"data: {json.dumps(chunk)}\n\n".encode())
            self.wfile.write(f"data: {json.dumps(dict(base, choices=[], usage=usage))}\n\n".encode())
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
            self.close_connection = True
            return
        self._json({"id": "c1", "object": "chat.completion", "created": int(time.time()), "model": model,
                    "choices": [{"index": 0, "message": {"role": "assistant", "content": TEXT}, "finish_reason": "stop"}],
                    "usage": usage})


if __name__ == "__main__":
    ThreadingHTTPServer(("127.0.0.1", int(sys.argv[1])), H).serve_forever()
