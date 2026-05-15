import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from llama_cpp import Llama

HOST = os.getenv("LLM_HOST", "0.0.0.0")
PORT = int(os.getenv("LLM_PORT", "8080"))
N_THREADS = int(os.getenv("LLM_THREADS", "8"))
N_CTX = int(os.getenv("LLM_CTX", "2048"))

print("Carregando FAST: Qwen2.5-0.5B q4_0...", flush=True)
llm_fast = Llama.from_pretrained(
    repo_id="Qwen/Qwen2.5-0.5B-Instruct-GGUF",
    filename="qwen2.5-0.5b-instruct-q4_0.gguf",
    n_ctx=N_CTX,
    n_threads=N_THREADS,
    verbose=False,
)
print("Carregando BALANCED: Qwen2.5-1.5B q4_0...", flush=True)
llm_balanced = Llama.from_pretrained(
    repo_id="Qwen/Qwen2.5-1.5B-Instruct-GGUF",
    filename="qwen2.5-1.5b-instruct-q4_0.gguf",
    n_ctx=N_CTX,
    n_threads=N_THREADS,
    verbose=False,
)
print("Modelos carregados.", flush=True)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _json(self, code, payload):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/health":
            self._json(200, {"ok": True, "models": ["Qwen2.5-0.5B q4_0", "Qwen2.5-1.5B q4_0"]})
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self):
        if self.path != "/v1/chat/completions":
            self._json(404, {"error": "not found"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            req = json.loads(self.rfile.read(length).decode("utf-8"))
            mode = req.get("mode", "balanced")
            messages = req.get("messages", [])
            max_tokens = int(req.get("max_tokens", 180))
            temperature = float(req.get("temperature", 0.7))

            llm = llm_fast if mode == "fast" else llm_balanced
            out = llm.create_chat_completion(
                messages=messages,
                max_tokens=max_tokens,
                temperature=temperature,
            )
            self._json(200, out)
        except Exception as e:
            self._json(500, {"error": str(e)})


if __name__ == "__main__":
    print(f"Servidor LLM em http://{HOST}:{PORT}", flush=True)
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
