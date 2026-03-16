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
import re
import os
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytz
import requests

from trendradar.core import load_config
from trendradar.ai.formatter import render_ai_analysis_html_rich
from trendradar.__main__ import main as run_trendradar
from trendradar.__main__ import NewsAnalyzer
from trendradar.core.scheduler import ResolvedSchedule

AI_SECTION_START = "<!-- AI_SECTION_START -->"
AI_SECTION_END = "<!-- AI_SECTION_END -->"


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


def _get_data_dir() -> Path:
    return Path(os.environ.get("TRENDRADAR_DATA_DIR", "output"))


def _collect_upload_paths(date_str: str) -> list[Path]:
    paths: list[Path] = []
    base_dir = _get_data_dir()

    # Only sync latest reports to keep commits small.
    candidates = [
        base_dir / "index.html",
    ]

    latest_dir = base_dir / "html" / "latest"
    if latest_dir.exists():
        candidates.extend(latest_dir.glob("*.html"))

    snapshot_dir = base_dir / "html" / date_str
    if os.environ.get("PUSH_HISTORY", "").strip().lower() in ("1", "true", "yes"):
        if snapshot_dir.exists():
            candidates.extend(snapshot_dir.glob("*.html"))

    # SQLite daily DBs (for de-dup + once-per-period)
    candidates.extend(
        [
            base_dir / "news" / f"{date_str}.db",
            base_dir / "rss" / f"{date_str}.db",
        ]
    )

    for p in candidates:
        if p.exists() and p.is_file():
            paths.append(p)

    return paths


def _relative_repo_path(path: Path, base_dir: Path) -> str:
    rel = path.relative_to(base_dir)
    return f"output/{rel.as_posix()}"


def _sync_from_github(repo: str, branch: str, token: str, date_str: str) -> int:
    # Pull today's DBs if present so schedule/dup checks work
    pulled = 0
    base_dir = _get_data_dir()
    for db_rel in (
        Path("news") / f"{date_str}.db",
        Path("rss") / f"{date_str}.db",
    ):
        repo_path = f"output/{db_rel.as_posix()}"
        dest = base_dir / db_rel
        if _download_if_exists(repo, branch, repo_path, token, dest):
            pulled += 1

    # Pull latest HTMLs so AI-only mode can patch AI section + email the report reliably.
    for html_rel in (
        Path("index.html"),
        Path("html") / "latest" / "daily.html",
        Path("html") / "latest" / "current.html",
        Path("html") / "latest" / "incremental.html",
    ):
        repo_path = f"output/{html_rel.as_posix()}"
        dest = base_dir / html_rel
        if _download_if_exists(repo, branch, repo_path, token, dest):
            pulled += 1
    return pulled

def _sync_to_github(repo: str, branch: str, token: str, date_str: str) -> dict:
    updated: list[str] = []
    skipped: list[str] = []
    base_dir = _get_data_dir()
    for path in _collect_upload_paths(date_str):
        repo_path = _relative_repo_path(path, base_dir)
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


def _replace_ai_section(html: str, ai_html: str) -> str:
    if not ai_html:
        return html
    wrapped = f"{AI_SECTION_START}{ai_html}{AI_SECTION_END}"
    if AI_SECTION_START in html and AI_SECTION_END in html:
        pre, rest = html.split(AI_SECTION_START, 1)
        _, post = rest.split(AI_SECTION_END, 1)
        return pre + wrapped + post

    # Fallback: try to replace existing ai-section block just before footer
    pattern = r"<div class=\"ai-section\">[\s\S]*?</div>\s*(?=<div class=\"footer\">)"
    if re.search(pattern, html):
        return re.sub(pattern, ai_html + "\n", html, count=1)

    # Final fallback: insert before footer
    marker = '<div class="footer">'
    if marker in html:
        return html.replace(marker, ai_html + marker, 1)
    return html


def _update_ai_section_files(data_dir: str, report_mode: str, ai_html: str) -> list[str]:
    updated = []
    if not ai_html:
        return updated
    base_dir = Path(data_dir)
    candidates = [
        base_dir / "index.html",
        base_dir / "html" / "latest" / f"{report_mode}.html",
    ]
    for path in candidates:
        if not path.exists():
            _log(f"AI-only: target not found, skip {path}")
            continue
        try:
            content = path.read_text(encoding="utf-8")
            new_content = _replace_ai_section(content, ai_html)
            if new_content != content:
                path.write_text(new_content, encoding="utf-8")
                updated.append(str(path))
        except Exception as exc:
            _log(f"AI-only: failed to update {path}: {exc}")
    return updated

class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        started_at = time.time()
        if not _require_secret(self):
            return

        query = parse_qs(urlparse(self.path).query)
        fast = (query.get("fast") or [""])[0].strip().lower() in ("1", "true", "yes")
        ai_only = (query.get("ai_only") or [""])[0].strip().lower() in ("1", "true", "yes")
        notify_raw = (query.get("notify") or [""])[0].strip().lower()
        notify = notify_raw not in ("0", "false", "no")
        ai_max_raw = (query.get("ai_max") or [""])[0].strip()
        if fast:
            # Fast mode skips AI analysis to keep request under cron-job.org timeout.
            os.environ["AI_ANALYSIS_ENABLED"] = "false"
        if ai_only:
            # Force AI analysis on in AI-only mode.
            os.environ["AI_ANALYSIS_ENABLED"] = "true"

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
            os.environ["TRENDRADAR_SKIP_ROOT_INDEX"] = "true"

            data_dir = os.environ.get("TRENDRADAR_DATA_DIR", "").strip()
            if not data_dir:
                if os.environ.get("VERCEL") == "1" or os.environ.get("VERCEL_REGION"):
                    data_dir = "/tmp/trendradar"
                else:
                    data_dir = "output"
                os.environ["TRENDRADAR_DATA_DIR"] = data_dir
            _log(f"Data dir: {data_dir}")

            config = load_config()
            config.setdefault("STORAGE", {})
            config["STORAGE"].setdefault("LOCAL", {})
            config["STORAGE"]["LOCAL"]["DATA_DIR"] = data_dir
            config["STORAGE"]["BACKEND"] = "local"
            if not notify:
                # Per-request override: allow running "collect only" jobs without sending any notification.
                config["ENABLE_NOTIFICATION"] = False
            if ai_only and ai_max_raw:
                try:
                    ai_max = max(1, int(ai_max_raw))
                    config.setdefault("AI_ANALYSIS", {})
                    config["AI_ANALYSIS"]["MAX_NEWS_FOR_ANALYSIS"] = ai_max
                    _log(f"AI-only max news override: {ai_max}")
                except ValueError:
                    _log(f"Invalid ai_max value: {ai_max_raw}")
            tz_name = config.get("TIMEZONE", "Asia/Shanghai")
            date_str = _get_today_str(tz_name)

            pulled = _sync_from_github(gh_repo, gh_branch, gh_token, date_str)
            _log(f"Pulled {pulled} DB file(s) from GitHub")

            if ai_only:
                # AI-only: use stored data, skip crawling.
                _log("Running AI-only analysis (no crawling)...")
                analyzer = NewsAnalyzer(config=config)
                analysis_data = analyzer._load_analysis_data()
                if not analysis_data:
                    raise RuntimeError("No data for AI analysis (today's DB missing or empty)")

                (
                    all_results,
                    id_to_name,
                    title_info,
                    new_titles,
                    word_groups,
                    filter_words,
                    global_filters,
                ) = analysis_data

                standalone_data = analyzer._prepare_standalone_data(
                    all_results, id_to_name, title_info, None
                )

                # Always analyze + push in AI-only mode (ignore schedule windows).
                schedule = ResolvedSchedule(
                    period_key=None,
                    period_name=None,
                    day_plan="ai_only",
                    collect=False,
                    analyze=True,
                    push=True,
                    report_mode=analyzer.report_mode,
                    ai_mode=analyzer.report_mode,
                    once_analyze=False,
                    once_push=False,
                )

                config.setdefault("STORAGE", {}).setdefault("FORMATS", {})

                config["STORAGE"]["FORMATS"]["HTML"] = False

                stats, html_file, ai_result = analyzer._run_analysis_pipeline(
                    all_results,
                    analyzer.report_mode,
                    title_info,
                    new_titles,
                    word_groups,
                    filter_words,
                    id_to_name,
                    failed_ids=[],
                    global_filters=global_filters,
                    rss_items=None,
                    rss_new_items=None,
                    standalone_data=standalone_data,
                    schedule=schedule,
                )

                # Update AI section in existing HTML without rewriting other sections
                ai_html = render_ai_analysis_html_rich(ai_result) if ai_result else ""
                _update_ai_section_files(data_dir, analyzer.report_mode, ai_html)

                latest_file = Path(data_dir) / "html" / "latest" / f"{analyzer.report_mode}.html"
                index_file = Path(data_dir) / "index.html"
                if latest_file.exists():
                    html_file = str(latest_file)
                elif index_file.exists():
                    html_file = str(index_file)
                else:
                    html_file = None

                mode_strategy = analyzer._get_mode_strategy()
                analyzer._send_notification_if_needed(
                    stats,
                    mode_strategy["report_type"],
                    analyzer.report_mode,
                    failed_ids=[],
                    new_titles=new_titles,
                    id_to_name=id_to_name,
                    html_file_path=html_file,
                    rss_items=None,
                    rss_new_items=None,
                    standalone_data=standalone_data,
                    ai_result=ai_result,
                    current_results=all_results,
                    schedule=schedule,
                )
                analyzer.ctx.cleanup()
            else:
                # Full run: crawl + analyze + notify
                _log("Running TrendRadar...")
                analyzer = NewsAnalyzer(config=config)
                analyzer.run()
                analyzer.ctx.cleanup()

            result = _sync_to_github(gh_repo, gh_branch, gh_token, date_str)
            duration_ms = int((time.time() - started_at) * 1000)
            _log(f"Sync complete. Updated: {len(result['updated'])}, skipped: {len(result['skipped'])}")

            _json_response(
                self,
                200,
                {
                    "ok": True,
                    "date": date_str,
                    "fast": fast,
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

















