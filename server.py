"""Local web app for courtside-signals. Uses only Python's standard library.

    venv/bin/python server.py            # then open http://localhost:8000
    venv/bin/python server.py --port 9000

Routes:
    GET /                  the search page (web/index.html)
    GET /api/ask?q=...     JSON: {question, category, params, answer, columns, rows}
"""

import json
import math
import sys
import time
import traceback
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from engine.parser import ask

_signals_cache: dict = {"ts": 0.0, "data": []}
_SIGNALS_TTL = 900  # 15 minutes


def _get_signals(sort="hot", limit=25):
    if time.time() - _signals_cache["ts"] < _SIGNALS_TTL:
        return _signals_cache["data"]
    from engine.discourse import fetch_signals
    data = fetch_signals(sort=sort, limit=limit)
    _signals_cache.update(ts=time.time(), data=data)
    return data

WEB_DIR = Path(__file__).resolve().parent / "web"


def _plain(value):
    """numpy / pandas values -> JSON-safe Python values."""
    if hasattr(value, "item"):
        value = value.item()
    if isinstance(value, float) and math.isnan(value):
        return None
    return value


def answer_json(question):
    result = ask(question)
    table = result.get("table")
    columns, rows = [], []
    if table is not None and not table.empty:
        columns = [str(c) for c in table.columns]
        rows = [[_plain(v) for v in row] for row in table.itertuples(index=False)]
    return {
        "question": question,
        "category": result["category"],
        "params": result.get("params", {}),
        "answer": result["answer"],
        "columns": columns,
        "rows": rows,
        "chart": (result.get("data") or {}).get("chart"),
    }


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(WEB_DIR), **kwargs)

    def do_GET(self):
        url = urlparse(self.path)
        if url.path == "/api/signals":
            qs = parse_qs(url.query)
            sort = (qs.get("sort") or ["hot"])[0]
            try:
                limit = int((qs.get("limit") or ["25"])[0])
            except ValueError:
                limit = 25
            try:
                self._json(200, _get_signals(sort=sort, limit=min(limit, 50)))
            except Exception:
                traceback.print_exc()
                self._json(500, {"error": "Could not fetch Reddit signals."})
            return

        if url.path != "/api/ask":
            return super().do_GET()

        question = (parse_qs(url.query).get("q") or [""])[0].strip()
        if not question:
            return self._json(400, {"error": "Ask a question with ?q=..."})
        try:
            self._json(200, answer_json(question[:300]))
        except Exception:
            traceback.print_exc()
            self._json(500, {"error": "Something went wrong answering that."})

    def _json(self, status, body):
        data = json.dumps(body, ensure_ascii=False, default=str).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, fmt, *args):
        if "/api/ask" in self.path:
            sys.stderr.write(f"{unquote_q(self.path)}\n")


def unquote_q(path):
    q = parse_qs(urlparse(path).query).get("q", [""])[0]
    return f"  asked: {q}"


def main():
    port = 8000
    if "--port" in sys.argv:
        port = int(sys.argv[sys.argv.index("--port") + 1])
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"courtside-signals running at http://localhost:{port}  (Ctrl-C to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print()


if __name__ == "__main__":
    main()
