# coding=utf-8
"""
Vercel Cron handler:
- Runs TrendRadar
- Syncs required artifacts to GitHub repo as "cloud storage"

Required env:
- GH_TOKEN: GitHub token with repo contents write permission
- GH_REPO:  owner/repo
Optional env:
- GH_BRANCH: default 'master'
- CRON_SECRET: shared secret to protect endpoint
- PUSH_HISTORY: 'true' to upload today's snapshot HTMLs
"""

import base64
import json
import os
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytz
import requests

from trendradar.core import load_config
from trendradar.__main__ import main as run_trendradar


def _log(message: str) -> None:
    print(f"[cron] {message}", flush=True)


def _json_response(handler: BaseHTTPRequestHandler, status: int, payload: dict) -> None:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def _get_header(handler: BaseHTTPRequestHandler, name: str) -> str:
    return handler.headers.get(name, "") if handler.headers else ""


def _require_secret(handler: BaseHTTPRequestHandler) -> bool:
    secret = os.environ.get("CRON_SECRET", "").strip()
    if not secret:
        return True

    query = parse_qs(urlparse(handler.path).query)
    token = (query.get("token") or [""])[0]
    header_token = _get_header(handler, "x-cron-secret") or _get_header(handler, "x-vercel-cron-secret")

    if token == secret or header_token == secret:
        return True

    _json_response(handler, 401, {"ok": False, "error": "unauthorized"})
    return False


def _github_headers(token: str) -> dict:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "User-Agent": "trendradar-vercel-cron",
    }


def _github_get_file(repo: str, branch: str, path: str, token: str) -> dict | None:
    url = f"https://api.github.com/repos/{repo}/contents/{path}"
    resp = requests.get(url, headers=_github_headers(token), params={"ref": branch}, timeout=30)
    if resp.status_code == 200:
        return resp.json()
    if resp.status_code == 404:
        return None
    raise RuntimeError(f"GitHub GET failed: {path} ({resp.status_code}) {resp.text}")


def _same_content(existing: dict | None, content: bytes) -> bool:
    if not existing or "content" not in existing:
        return False
    try:
        remote_bytes = base64.b64decode(existing["content"])
        return remote_bytes == content
    except Exception:
        return False


def _github_put_file(repo: str, branch: str, path: str, content: bytes, message: str, token: str) -> str:
    url = f"https://api.github.com/repos/{repo}/contents/{path}"
    payload = {
        "message": message,
        "content": base64.b64encode(content).decode("utf-8"),
        "branch": branch,
    }

    existing = _github_get_file(repo, branch, path, token)
    if _same_content(existing, content):
        return "skipped"
    if existing and "sha" in existing:
        payload["sha"] = existing["sha"]

    resp = requests.put(url, headers=_github_headers(token), json=payload, timeout=60)
    if resp.status_code not in (200, 201):
        raise RuntimeError(f"GitHub PUT failed: {path} ({resp.status_code}) {resp.text}")
    return "updated"


def _download_if_exists(repo: str, branch: str, path: str, token: str, dest: Path) -> bool:
    existing = _github_get_file(repo, branch, path, token)
    if not existing or "content" not in existing:
        return False

    content = base64.b64decode(existing["content"])
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(content)
    return True


def _get_today_str(tz_name: str) -> str:
    tz = pytz.timezone(tz_name)
    return datetime.now(tz).strftime("%Y-%m-%d")


def _collect_upload_paths(date_str: str) -> list[Path]:
    paths: list[Path] = []

    candidates = [
        Path("index.html"),
        Path("output") / "index.html",
    ]

    latest_dir = Path("output") / "html" / "latest"
    if latest_dir.exists():
        candidates.extend(latest_dir.glob("*.html"))

    snapshot_dir = Path("output") / "html" / date_str
    if os.environ.get("PUSH_HISTORY", "").strip().lower() in ("1", "true", "yes"):
        if snapshot_dir.exists():
            candidates.extend(snapshot_dir.glob("*.html"))

    # SQLite daily DBs (for de-dup + once-per-period)
    candidates.extend(
        [
            Path("output") / "news" / f"{date_str}.db",
            Path("output") / "rss" / f"{date_str}.db",
        ]
    )

    for p in candidates:
        if p.exists() and p.is_file():
            paths.append(p)

    return paths


def _relative_repo_path(path: Path) -> str:
    return path.as_posix()


def _sync_from_github(repo: str, branch: str, token: str, date_str: str) -> int:
    # Pull today's DBs if present so schedule/dup checks work
    pulled = 0
    for db_path in (
        Path("output") / "news" / f"{date_str}.db",
        Path("output") / "rss" / f"{date_str}.db",
    ):
        if _download_if_exists(repo, branch, _relative_repo_path(db_path), token, db_path):
            pulled += 1
    return pulled


def _sync_to_github(repo: str, branch: str, token: str, date_str: str) -> dict:
    updated: list[str] = []
    skipped: list[str] = []
    for path in _collect_upload_paths(date_str):
        repo_path = _relative_repo_path(path)
        status = _github_put_file(
            repo=repo,
            branch=branch,
            path=repo_path,
            content=path.read_bytes(),
            message=f"chore: update TrendRadar artifacts ({date_str})",
            token=token,
        )
        if status == "updated":
            updated.append(repo_path)
        else:
            skipped.append(repo_path)

    return {"updated": updated, "skipped": skipped}


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        started_at = time.time()
        if not _require_secret(self):
            return

        gh_token = os.environ.get("GH_TOKEN", "").strip()
        gh_repo = os.environ.get("GH_REPO", "").strip()
        gh_branch = os.environ.get("GH_BRANCH", "master").strip() or "master"

        if not gh_token or not gh_repo:
            _json_response(
                self,
                500,
                {
                    "ok": False,
                    "error": "Missing GH_TOKEN or GH_REPO",
                    "env_debug": {
                        "has_GH_TOKEN": bool(gh_token),
                        "has_GH_REPO": bool(gh_repo),
                        "branch": gh_branch,
                    },
                },
            )
            return

        try:
            # Avoid opening browser inside serverless
            os.environ["GITHUB_ACTIONS"] = "true"

            config = load_config()
            tz_name = config.get("TIMEZONE", "Asia/Shanghai")
            date_str = _get_today_str(tz_name)

            pulled = _sync_from_github(gh_repo, gh_branch, gh_token, date_str)
            _log(f"Pulled {pulled} DB file(s) from GitHub")

            # Run TrendRadar
            _log("Running TrendRadar...")
            run_trendradar()

            result = _sync_to_github(gh_repo, gh_branch, gh_token, date_str)
            duration_ms = int((time.time() - started_at) * 1000)
            _log(f"Sync complete. Updated: {len(result['updated'])}, skipped: {len(result['skipped'])}")

            _json_response(
                self,
                200,
                {
                    "ok": True,
                    "date": date_str,
                    "updated": result.get("updated", []),
                    "skipped": result.get("skipped", []),
                    "duration_ms": duration_ms,
                },
            )
        except Exception as exc:
            _log(f"Error: {exc}")
            _json_response(self, 500, {"ok": False, "error": str(exc)})

    def do_POST(self):
        return self.do_GET()
