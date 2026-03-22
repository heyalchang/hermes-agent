"""
Hermes Dashboard — aiohttp web server.

Serves the SPA at / and JSON API endpoints at /api/*.
All data access is read-only.
"""

import json
import os
from pathlib import Path

from aiohttp import web

from dashboard import data

STATIC_DIR = Path(__file__).parent / "static"


def json_response(obj, status=200):
    return web.Response(
        text=json.dumps(obj, default=str),
        content_type="application/json",
        status=status,
    )


def _int_param(request, name, default):
    """Parse an integer query parameter, returning the default on bad input."""
    try:
        return int(request.query.get(name, default))
    except (ValueError, TypeError):
        return default


# =========================================================================
# Route handlers
# =========================================================================

async def handle_index(request):
    return web.FileResponse(STATIC_DIR / "index.html")


async def handle_health(request):
    return json_response({"status": "ok"})


async def handle_status(request):
    return json_response(data.get_gateway_status())


async def handle_sessions(request):
    source = request.query.get("source")
    limit = _int_param(request, "limit", 50)
    offset = _int_param(request, "offset", 0)
    return json_response(data.get_sessions(source=source, limit=limit, offset=offset))


async def handle_session_detail(request):
    session_id = request.match_info["id"]
    session = data.get_session(session_id)
    if not session:
        return json_response({"error": "not found"}, status=404)
    return json_response(session)


async def handle_session_messages(request):
    session_id = request.match_info["id"]
    return json_response(data.get_messages(session_id))


async def handle_tokens(request):
    days = _int_param(request, "days", 30)
    source = request.query.get("source")
    return json_response(data.get_usage(days=days, source=source))


async def handle_cron(request):
    return json_response(data.get_cron_jobs())


async def handle_cron_status(request):
    return json_response(data.get_cron_status())


async def handle_cron_runs(request):
    days = min(_int_param(request, "days", 7), 90)
    return json_response(data.get_cron_runs(days=days))


def _safe_id(raw: str) -> str:
    """Strip path separators from URL path parameters."""
    return raw.replace("/", "").replace("..", "").replace("\\", "")


async def handle_cron_outputs(request):
    job_id = _safe_id(request.match_info["id"])
    return json_response(data.list_cron_outputs(job_id))


async def handle_cron_output_file(request):
    job_id = _safe_id(request.match_info["id"])
    filename = request.match_info["file"]
    content = data.read_cron_output(job_id, filename)
    if content is None:
        return json_response({"error": "not found"}, status=404)
    return json_response({"filename": filename, "content": content})


async def handle_logs(request):
    lines = min(_int_param(request, "lines", 200), 1000)
    return json_response({"log": data.tail_log(lines)})


async def handle_search(request):
    q = request.query.get("q", "")
    source = request.query.get("source")
    limit = _int_param(request, "limit", 20)
    return json_response(data.search_transcripts(q, source=source, limit=limit))


async def handle_feed(request):
    source = request.query.get("source")
    limit = min(_int_param(request, "limit", 100), 500)
    offset = _int_param(request, "offset", 0)
    return json_response(data.get_feed(source=source, limit=limit, offset=offset))


async def handle_memory(request):
    return json_response(data.get_memory())


async def handle_insights(request):
    days = _int_param(request, "days", 30)
    source = request.query.get("source")
    return json_response(data.get_insights(days=days, source=source))


async def handle_plugins(request):
    return json_response(data.get_plugins())


async def handle_checkpoints(request):
    return json_response(data.get_checkpoints())


# =========================================================================
# App factory
# =========================================================================

def create_app() -> web.Application:
    app = web.Application()

    # SPA
    app.router.add_get("/", handle_index)

    # API
    app.router.add_get("/api/health", handle_health)
    app.router.add_get("/api/status", handle_status)
    app.router.add_get("/api/sessions", handle_sessions)
    app.router.add_get("/api/sessions/{id}", handle_session_detail)
    app.router.add_get("/api/sessions/{id}/messages", handle_session_messages)
    app.router.add_get("/api/tokens", handle_tokens)
    app.router.add_get("/api/cron", handle_cron)
    app.router.add_get("/api/cron/status", handle_cron_status)
    app.router.add_get("/api/cron/runs", handle_cron_runs)
    app.router.add_get("/api/cron/{id}/outputs", handle_cron_outputs)
    app.router.add_get("/api/cron/{id}/outputs/{file}", handle_cron_output_file)
    app.router.add_get("/api/logs", handle_logs)
    app.router.add_get("/api/search", handle_search)
    app.router.add_get("/api/feed", handle_feed)
    app.router.add_get("/api/memory", handle_memory)
    app.router.add_get("/api/insights", handle_insights)
    app.router.add_get("/api/plugins", handle_plugins)
    app.router.add_get("/api/checkpoints", handle_checkpoints)

    # Static files
    app.router.add_static("/static/", STATIC_DIR, name="static")

    return app


def run_server(host: str = "127.0.0.1", port: int = 18808):
    """Start the dashboard server."""
    app = create_app()
    print(f"Hermes Dashboard starting on http://{host}:{port}")
    web.run_app(app, host=host, port=port, print=None)
