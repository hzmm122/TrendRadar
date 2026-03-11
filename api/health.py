# coding=utf-8
"""Simple health check endpoint for Vercel."""

import json
import os
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler


def _json_response(handler: BaseHTTPRequestHandler, status: int, payload: dict) -> None:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def _has_env(key: str) -> bool:
    return bool(os.environ.get(key, "").strip())


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        payload = {
            "ok": True,
            "time_utc": datetime.now(timezone.utc).isoformat(),
            "env": {
                "has_GH_TOKEN": _has_env("GH_TOKEN"),
                "has_GH_REPO": _has_env("GH_REPO"),
                "has_CRON_SECRET": _has_env("CRON_SECRET"),
                "has_EMAIL_FROM": _has_env("EMAIL_FROM"),
                "has_EMAIL_PASSWORD": _has_env("EMAIL_PASSWORD"),
                "has_EMAIL_TO": _has_env("EMAIL_TO"),
                "has_AI_API_KEY": _has_env("AI_API_KEY"),
            },
        }
        _json_response(self, 200, payload)

    def do_POST(self):
        return self.do_GET()
