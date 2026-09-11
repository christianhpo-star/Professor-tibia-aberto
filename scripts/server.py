#!/usr/bin/env python3
"""Minimal local server for Professor Tibia.

Serves the existing static application and adds the internal character lookup
route without introducing a web framework or changing the current local data
model.
"""

from __future__ import annotations

from collections import defaultdict, deque
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
import logging
import os
from pathlib import Path
import threading
import time
from urllib.parse import unquote, urlsplit

try:
    from modules.character_lookup import CharacterLookupError, TibiaDataClient
except ModuleNotFoundError:  # importable as scripts.server in tests
    from scripts.modules.character_lookup import CharacterLookupError, TibiaDataClient

BASE_DIR = Path(__file__).resolve().parent.parent
LOG_DIR = BASE_DIR / "logs"
LOG_DIR.mkdir(exist_ok=True)

logger = logging.getLogger("professor_tibia.character_lookup")
logger.setLevel(logging.INFO)
if not logger.handlers:
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    stream = logging.StreamHandler()
    stream.setFormatter(formatter)
    logger.addHandler(stream)
    try:
        file_handler = logging.FileHandler(LOG_DIR / "character_lookup.log", encoding="utf-8")
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)
    except OSError:
        pass


class RateLimiter:
    def __init__(self, limit: int = 30, window_seconds: int = 60):
        self.limit = limit
        self.window_seconds = window_seconds
        self._requests: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        cutoff = now - self.window_seconds
        with self._lock:
            bucket = self._requests[key]
            while bucket and bucket[0] <= cutoff:
                bucket.popleft()
            if len(bucket) >= self.limit:
                return False
            bucket.append(now)
            return True


CLIENT = TibiaDataClient(
    timeout_seconds=float(os.environ.get("TIBIADATA_TIMEOUT_SECONDS", "8")),
    cache_ttl_seconds=int(os.environ.get("TIBIADATA_CACHE_TTL_SECONDS", "300")),
    max_highscore_pages=int(os.environ.get("TIBIADATA_MAX_HIGHSCORE_PAGES", "3")),
)
RATE_LIMITER = RateLimiter(
    limit=int(os.environ.get("CHARACTER_LOOKUP_RATE_LIMIT", "30")),
    window_seconds=60,
)


class ProfessorTibiaHandler(SimpleHTTPRequestHandler):
    server_version = "ProfessorTibia/2.7"

    def log_message(self, format: str, *args) -> None:  # noqa: A002
        logger.info("http %s - %s", self.client_address[0], format % args)

    def _json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _client_key(self) -> str:
        return self.client_address[0]

    def _handle_character_lookup(self, encoded_name: str) -> None:
        if not RATE_LIMITER.allow(self._client_key()):
            self._json(429, {"ok": False, "error": {"code": "rate_limited", "message": "Muitas pesquisas em pouco tempo. Aguarde um pouco e tente novamente."}})
            return
        try:
            name = unquote(encoded_name)
            data, cache_hit = CLIENT.lookup(name, include_ranked_skills=True)
            self._json(200, {
                "ok": True,
                "data": data,
                "meta": {"cache_hit": cache_hit, "cache_ttl_seconds": CLIENT.cache_ttl_seconds},
            })
        except CharacterLookupError as exc:
            logger.warning("character_lookup code=%s", exc.code)
            self._json(exc.http_status, {"ok": False, "error": {"code": exc.code, "message": exc.public_message}})
        except Exception:
            logger.exception("character_lookup unexpected_error")
            self._json(500, {"ok": False, "error": {"code": "internal_error", "message": "Ocorreu um erro interno ao pesquisar o personagem."}})

    def do_GET(self) -> None:  # noqa: N802
        split = urlsplit(self.path)
        path = split.path
        prefix = "/api/characters/"
        if path == "/api/characters" or path == "/api/characters/":
            self._json(400, {"ok": False, "error": {"code": "invalid_name", "message": "Digite um nome de personagem válido."}})
            return
        if path.startswith(prefix):
            encoded_name = path[len(prefix):]
            self._handle_character_lookup(encoded_name)
            return
        if path == "/api/health":
            self._json(200, {"ok": True, "service": "professor-tibia", "character_lookup": True})
            return
        if path == "/":
            self.path = "/preview.html"
        super().do_GET()


def create_server(host: str = "127.0.0.1", port: int = 8765) -> ThreadingHTTPServer:
    handler = partial(ProfessorTibiaHandler, directory=str(BASE_DIR))
    return ThreadingHTTPServer((host, port), handler)


def main() -> None:
    host = os.environ.get("PROFESSOR_TIBIA_HOST", "127.0.0.1")
    port = int(os.environ.get("PROFESSOR_TIBIA_PORT", "8765"))
    server = create_server(host, port)
    print(f"Professor Tibia: http://{host}:{port}/")
    print("Pesquisa de personagens: GET /api/characters/{name}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nServidor encerrado.")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
