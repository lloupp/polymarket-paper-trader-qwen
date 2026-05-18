import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from llama_cpp import Llama

HOST = os.getenv("LLM_HOST", "0.0.0.0")
PORT = int(os.getenv("LLM_PORT", "8080"))
N_THREADS = int(os.getenv("LLM_THREADS", "8"))
N_CTX = int(os.getenv("LLM_CTX", "2048"))

MODEL_SPECS = {
    "fast": {
        "label": "Qwen2.5-0.5B q4_0",
        "repo_id": "Qwen/Qwen2.5-0.5B-Instruct-GGUF",
        "filename": "qwen2.5-0.5b-instruct-q4_0.gguf",
    },
    "balanced": {
        "label": "Qwen2.5-1.5B q4_0",
        "repo_id": "Qwen/Qwen2.5-1.5B-Instruct-GGUF",
        "filename": "qwen2.5-1.5b-instruct-q4_0.gguf",
    },
    "strong": {
        "label": "Qwen3-4B-Instruct-2507 Q4_K_M",
        "repo_id": "bartowski/Qwen_Qwen3-4B-Instruct-2507-GGUF",
        "filename": "Qwen3-4B-Instruct-2507-Q4_K_M.gguf",
    },
}
_models = {}
_model_lock = threading.Lock()


def get_llm(mode: str):
    normalized = mode if mode in MODEL_SPECS else "fast"
    with _model_lock:
        if normalized not in _models:
            spec = MODEL_SPECS[normalized]
            print(f"Carregando {normalized.upper()}: {spec['label']}...", flush=True)
            _models[normalized] = Llama.from_pretrained(
                repo_id=spec["repo_id"],
                filename=spec["filename"],
                n_ctx=N_CTX,
                n_threads=N_THREADS,
                verbose=False,
            )
            print(f"Modelo {normalized} carregado.", flush=True)
        return _models[normalized]


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
            self._json(200, {
                "ok": True,
                "available_models": [spec["label"] for spec in MODEL_SPECS.values()],
                "loaded_modes": sorted(_models.keys()),
            })
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

            llm = get_llm(mode)
            with _model_lock:
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
