"""
Read-only data access for the Hermes Dashboard.

All functions open their own DB connections (read-only mode) and close them
when done. No persistent state — safe for a single-user polling UI.
"""

import json
import os
import re
import sqlite3
import subprocess
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional


HERMES_HOME = Path(os.getenv("HERMES_HOME", Path.home() / ".hermes"))
DB_PATH = HERMES_HOME / "state.db"
LOG_PATH = HERMES_HOME / "logs" / "gateway.log"
CRON_DIR = HERMES_HOME / "cron"
JOBS_FILE = CRON_DIR / "jobs.json"
OUTPUT_DIR = CRON_DIR / "output"
MEMORIES_DIR = HERMES_HOME / "memories"
PID_PATH = HERMES_HOME / "gateway.pid"


def _ro_connect() -> sqlite3.Connection:
    """Open a read-only SQLite connection."""
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, timeout=5.0)
    conn.row_factory = sqlite3.Row
    return conn


# Schema detection — the sessions table gains columns across versions.
# Cache the column set per process to avoid repeated PRAGMA queries.
_sessions_columns: Optional[set] = None


def _get_session_columns() -> set:
    """Return the set of column names on the sessions table."""
    global _sessions_columns
    if _sessions_columns is not None:
        return _sessions_columns
    if not DB_PATH.exists():
        return set()
    conn = _ro_connect()
    try:
        rows = conn.execute("PRAGMA table_info(sessions)").fetchall()
        _sessions_columns = {r["name"] for r in rows}
    finally:
        conn.close()
    return _sessions_columns


def _safe_col(col: str) -> bool:
    """Check whether a column exists in the sessions table."""
    return col in _get_session_columns()


# =========================================================================
# Gateway status
# =========================================================================

def get_gateway_status() -> Dict[str, Any]:
    """Check if the gateway is running and basic stats."""
    running = False
    pid = None

    if PID_PATH.exists():
        try:
            raw = PID_PATH.read_text().strip()
            if raw.startswith("{"):
                pid = json.loads(raw).get("pid")
            else:
                pid = int(raw)
            if pid:
                pid = int(pid)
                os.kill(pid, 0)
                running = True
        except (ValueError, ProcessLookupError, PermissionError,
                json.JSONDecodeError, TypeError):
            pid = None

    session_count = 0
    message_count = 0
    if DB_PATH.exists():
        try:
            conn = _ro_connect()
            try:
                session_count = conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
                message_count = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
            finally:
                conn.close()
        except Exception:
            pass

    # Gateway platforms — parse from log
    platforms = []
    if running and LOG_PATH.exists():
        try:
            with open(LOG_PATH, "r", errors="replace") as f:
                lines = f.readlines()
            last_start = None
            for i, line in enumerate(lines):
                if "Starting Hermes Gateway" in line:
                    last_start = i
            if last_start is not None:
                for line in lines[last_start:]:
                    if "connected" in line and "\u2713" in line:
                        parts = line.split("\u2713")
                        if len(parts) > 1:
                            name = parts[1].strip().split()[0] if parts[1].strip() else ""
                            if name and name not in platforms:
                                platforms.append(name)
        except Exception:
            pass

    return {
        "running": running,
        "pid": pid,
        "session_count": session_count,
        "message_count": message_count,
        "services": {
            "gateway": {"running": running, "pid": pid, "platforms": platforms},
            "dashboard": {"running": True},
        },
    }


# =========================================================================
# Sessions
# =========================================================================

def get_sessions(
    source: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
) -> List[Dict[str, Any]]:
    """List sessions, optionally filtered by source."""
    if not DB_PATH.exists():
        return []
    conn = _ro_connect()
    try:
        # Build column list based on what exists in the schema
        base_cols = ["id", "source", "user_id", "model", "started_at", "ended_at",
                     "message_count", "tool_call_count", "input_tokens", "output_tokens"]
        optional_cols = ["cache_read_tokens", "cache_write_tokens", "reasoning_tokens",
                         "estimated_cost_usd", "cost_status", "billing_provider",
                         "billing_mode", "title", "end_reason", "parent_session_id"]
        cols = base_cols + [c for c in optional_cols if _safe_col(c)]
        col_str = ", ".join(cols)

        if source:
            rows = conn.execute(
                f"SELECT {col_str} FROM sessions WHERE source = ? "
                "ORDER BY started_at DESC LIMIT ? OFFSET ?",
                (source, limit, offset),
            ).fetchall()
        else:
            rows = conn.execute(
                f"SELECT {col_str} FROM sessions ORDER BY started_at DESC LIMIT ? OFFSET ?",
                (limit, offset),
            ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_session(session_id: str) -> Optional[Dict[str, Any]]:
    """Get a single session by ID."""
    if not DB_PATH.exists():
        return None
    conn = _ro_connect()
    try:
        row = conn.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def get_messages(session_id: str) -> List[Dict[str, Any]]:
    """Get all messages for a session."""
    if not DB_PATH.exists():
        return []
    conn = _ro_connect()
    try:
        rows = conn.execute(
            "SELECT id, session_id, role, content, tool_call_id, tool_calls, "
            "tool_name, timestamp, token_count, finish_reason "
            "FROM messages WHERE session_id = ? ORDER BY timestamp, id",
            (session_id,),
        ).fetchall()
        result = []
        for row in rows:
            msg = dict(row)
            if msg.get("tool_calls"):
                try:
                    msg["tool_calls"] = json.loads(msg["tool_calls"])
                except (json.JSONDecodeError, TypeError):
                    pass
            result.append(msg)
        return result
    finally:
        conn.close()


# =========================================================================
# Token usage
# =========================================================================

def get_usage(days: int = 30, source: Optional[str] = None) -> Dict[str, Any]:
    """Get usage data from DB sessions."""
    cutoff = time.time() - (days * 86400)
    result: Dict[str, Any] = {"days": days, "sessions": [], "summary": {}}

    if DB_PATH.exists():
        conn = _ro_connect()
        try:
            base_cols = ["id", "source", "model", "started_at", "ended_at",
                         "message_count", "tool_call_count", "input_tokens", "output_tokens"]
            optional_cols = ["cache_read_tokens", "cache_write_tokens", "reasoning_tokens",
                             "estimated_cost_usd", "cost_status", "billing_provider",
                             "billing_mode"]
            cols = base_cols + [c for c in optional_cols if _safe_col(c)]
            col_str = ", ".join(cols)

            if source:
                rows = conn.execute(
                    f"SELECT {col_str} FROM sessions WHERE started_at >= ? AND source = ? "
                    "ORDER BY started_at DESC",
                    (cutoff, source),
                ).fetchall()
            else:
                rows = conn.execute(
                    f"SELECT {col_str} FROM sessions WHERE started_at >= ? "
                    "ORDER BY started_at DESC",
                    (cutoff,),
                ).fetchall()
            result["sessions"] = [dict(r) for r in rows]
        finally:
            conn.close()

    sessions = result["sessions"]
    total_input = sum(s.get("input_tokens") or 0 for s in sessions)
    total_output = sum(s.get("output_tokens") or 0 for s in sessions)
    total_cache_read = sum(s.get("cache_read_tokens") or 0 for s in sessions)
    total_cache_write = sum(s.get("cache_write_tokens") or 0 for s in sessions)
    total_reasoning = sum(s.get("reasoning_tokens") or 0 for s in sessions)
    total_cost = sum(s.get("estimated_cost_usd") or 0 for s in sessions)

    result["summary"] = {
        "total_sessions": len(sessions),
        "total_input_tokens": total_input,
        "total_output_tokens": total_output,
        "total_tokens": total_input + total_output + total_cache_read + total_cache_write,
        "total_cache_read_tokens": total_cache_read,
        "total_cache_write_tokens": total_cache_write,
        "total_reasoning_tokens": total_reasoning,
        "total_estimated_cost_usd": round(total_cost, 4),
    }
    return result


# =========================================================================
# Insights
# =========================================================================

def get_insights(days: int = 30, source: Optional[str] = None) -> Dict[str, Any]:
    """Generate insights report using InsightsEngine."""
    if not DB_PATH.exists():
        return {"empty": True, "days": days}
    try:
        from hermes_state import SessionDB
        from agent.insights import InsightsEngine
        db = SessionDB()
        engine = InsightsEngine(db)
        report = engine.generate(days=days, source=source)
        db.close()
        return report
    except Exception:
        return {"empty": True, "days": days}


# =========================================================================
# Cron jobs
# =========================================================================

def get_cron_jobs() -> List[Dict[str, Any]]:
    """Load all cron jobs from jobs.json."""
    if not JOBS_FILE.exists():
        return []
    try:
        with open(JOBS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data.get("jobs", [])
    except (json.JSONDecodeError, IOError):
        return []


def get_cron_status() -> Dict[str, Any]:
    """Summary counts for cron jobs."""
    jobs = get_cron_jobs()
    total = len(jobs)
    enabled = sum(1 for j in jobs if j.get("enabled", True) and j.get("state") != "paused")
    paused = sum(1 for j in jobs if j.get("state") == "paused")
    disabled = total - enabled - paused

    next_wake = None
    for j in jobs:
        if not j.get("enabled", True):
            continue
        nra = j.get("next_run_at")
        if nra:
            try:
                dt = datetime.fromisoformat(str(nra))
                ms = int(dt.timestamp() * 1000)
                if next_wake is None or ms < next_wake:
                    next_wake = ms
            except (ValueError, TypeError):
                pass

    return {
        "total": total,
        "enabled": enabled,
        "disabled": disabled,
        "paused": paused,
        "nextWakeAtMs": next_wake,
    }


def get_cron_runs(days: int = 7) -> Dict[str, Any]:
    """Build run history from cron sessions.

    Returns runs grouped by job_id.
    """
    cutoff = time.time() - (days * 86400)
    runs_by_job_id: Dict[str, Dict[str, Any]] = {}

    if not DB_PATH.exists():
        return {"runs_by_job_id": runs_by_job_id}

    conn = _ro_connect()
    try:
        rows = conn.execute(
            "SELECT id, source, model, started_at, ended_at, "
            "input_tokens, output_tokens "
            "FROM sessions WHERE source = 'cron' AND started_at >= ? "
            "ORDER BY started_at DESC",
            (cutoff,),
        ).fetchall()
        sessions = [dict(r) for r in rows]
    finally:
        conn.close()

    for s in sessions:
        sid = s["id"]
        parts = sid.split("_")
        if len(parts) < 4 or parts[0] != "cron":
            continue
        job_id = parts[1]
        ts_ms = int((s.get("started_at") or 0) * 1000)
        total_tokens = (s.get("input_tokens") or 0) + (s.get("output_tokens") or 0)

        status = "unknown"
        job_output_dir = OUTPUT_DIR / job_id
        if job_output_dir.exists() and any(job_output_dir.iterdir()):
            status = "ok"

        entry = {
            "ts": ts_ms,
            "status": status,
            "model": s.get("model", ""),
            "totalTokens": total_tokens,
            "sessionId": s["id"],
        }
        if job_id not in runs_by_job_id:
            runs_by_job_id[job_id] = {"entries": []}
        runs_by_job_id[job_id]["entries"].append(entry)

    return {"runs_by_job_id": runs_by_job_id}


def list_cron_outputs(job_id: str) -> List[str]:
    """List output files for a cron job."""
    job_dir = OUTPUT_DIR / job_id
    if not job_dir.exists():
        return []
    files = sorted(job_dir.glob("*.md"), reverse=True)
    return [f.name for f in files]


def read_cron_output(job_id: str, filename: str) -> Optional[str]:
    """Read a single cron output file. Returns None if not found."""
    safe_name = Path(filename).name
    filepath = OUTPUT_DIR / job_id / safe_name
    if not filepath.exists() or not filepath.is_file():
        return None
    try:
        return filepath.read_text(encoding="utf-8")
    except Exception:
        return None


# =========================================================================
# Logs
# =========================================================================

def tail_log(lines: int = 200) -> str:
    """Tail the gateway log file. Returns the last N lines."""
    if not LOG_PATH.exists():
        return ""
    try:
        from collections import deque
        with open(LOG_PATH, "r", errors="replace") as f:
            return "".join(deque(f, maxlen=lines))
    except Exception:
        return ""


# =========================================================================
# Search
# =========================================================================

def search_transcripts(
    query: str,
    source: Optional[str] = None,
    limit: int = 20,
) -> List[Dict[str, Any]]:
    """Full-text search across session messages using FTS5."""
    if not query or not query.strip() or not DB_PATH.exists():
        return []

    conn = _ro_connect()
    try:
        safe_query = '"' + query.replace('"', '""') + '"'
        where_clauses = ["messages_fts MATCH ?"]
        params: list = [safe_query]

        if source:
            where_clauses.append("s.source = ?")
            params.append(source)

        params.extend([limit, 0])
        where_sql = " AND ".join(where_clauses)

        sql = f"""
            SELECT
                m.id,
                m.session_id,
                m.role,
                snippet(messages_fts, 0, '>>>', '<<<', '...', 40) AS snippet,
                m.timestamp,
                m.tool_name,
                s.source,
                s.model,
                s.started_at AS session_started
            FROM messages_fts
            JOIN messages m ON m.id = messages_fts.rowid
            JOIN sessions s ON s.id = m.session_id
            WHERE {where_sql}
            ORDER BY rank
            LIMIT ? OFFSET ?
        """
        rows = conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]
    except Exception:
        return []
    finally:
        conn.close()


# =========================================================================
# Feed — recent messages across platforms
# =========================================================================

def get_feed(
    source: Optional[str] = None,
    limit: int = 100,
    offset: int = 0,
) -> List[Dict[str, Any]]:
    """Get recent user/assistant messages across gateway platforms."""
    if not DB_PATH.exists():
        return []
    conn = _ro_connect()
    try:
        if source:
            sources = [source]
        else:
            # All known messaging platforms
            sources = [
                "telegram", "whatsapp", "discord", "slack", "signal",
                "homeassistant", "email", "webhook", "matrix", "mattermost",
                "dingtalk", "sms",
            ]
        placeholders = ",".join("?" for _ in sources)
        params: list = list(sources)
        params.extend([limit, offset])

        rows = conn.execute(
            f"""SELECT m.id, m.session_id, m.role, m.content, m.timestamp,
                       m.tool_name, s.source
                FROM messages m
                JOIN sessions s ON s.id = m.session_id
                WHERE s.source IN ({placeholders})
                  AND m.role IN ('user', 'assistant')
                ORDER BY m.timestamp DESC, m.id DESC
                LIMIT ? OFFSET ?""",
            params,
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


# =========================================================================
# Plugins
# =========================================================================

PLUGINS_DIRS = [
    HERMES_HOME / "plugins",
]


def get_plugins() -> List[Dict[str, Any]]:
    """Discover plugins from disk and return their manifests."""
    try:
        import yaml
    except ImportError:
        yaml = None

    plugins: List[Dict[str, Any]] = []
    seen: set = set()

    for pdir in PLUGINS_DIRS:
        if not pdir.is_dir():
            continue
        source_label = "user" if pdir == PLUGINS_DIRS[0] else "project"
        for child in sorted(pdir.iterdir()):
            if not child.is_dir():
                continue
            manifest_path = child / "plugin.yaml"
            if not manifest_path.exists():
                manifest_path = child / "plugin.yml"
            if not manifest_path.exists():
                continue
            name = child.name
            if name in seen:
                continue
            seen.add(name)
            try:
                if yaml is None:
                    raise ImportError("pyyaml not installed")
                manifest = yaml.safe_load(manifest_path.read_text())
            except Exception as e:
                plugins.append({
                    "name": name, "source": source_label, "path": str(child),
                    "enabled": False, "error": str(e),
                })
                continue
            plugins.append({
                "name": manifest.get("name", name),
                "version": manifest.get("version", ""),
                "description": manifest.get("description", ""),
                "author": manifest.get("author", ""),
                "source": source_label,
                "path": str(child),
                "provides_tools": manifest.get("provides_tools", []),
                "provides_hooks": manifest.get("provides_hooks", []),
                "enabled": True,
                "error": None,
            })

    try:
        from importlib.metadata import entry_points
        eps = entry_points()
        group = (eps.get("hermes_agent.plugins", [])
                 if isinstance(eps, dict) else eps.select(group="hermes_agent.plugins"))
        for ep in group:
            if ep.name not in seen:
                seen.add(ep.name)
                plugins.append({
                    "name": ep.name,
                    "version": "",
                    "description": f"entrypoint: {ep.value}",
                    "source": "entrypoint",
                    "path": ep.value,
                    "provides_tools": [],
                    "provides_hooks": [],
                    "enabled": True,
                    "error": None,
                })
    except Exception:
        pass

    return plugins


# =========================================================================
# Checkpoints
# =========================================================================

CHECKPOINT_BASE = HERMES_HOME / "checkpoints"


def get_checkpoints() -> List[Dict[str, Any]]:
    """Enumerate checkpoint directories and list snapshots for each."""
    if not CHECKPOINT_BASE.is_dir():
        return []

    results: List[Dict[str, Any]] = []
    for child in sorted(CHECKPOINT_BASE.iterdir()):
        if not child.is_dir():
            continue
        workdir_file = child / "HERMES_WORKDIR"
        if not workdir_file.exists():
            continue
        try:
            working_dir = workdir_file.read_text().strip()
        except Exception:
            continue

        snapshots: List[Dict[str, Any]] = []
        try:
            result = subprocess.run(
                ["git", "log", "--format=%H|%h|%aI|%s", "-n", "50"],
                cwd=str(child), capture_output=True, text=True, timeout=5,
                env={**os.environ, "GIT_DIR": str(child)},
            )
            if result.returncode == 0:
                for line in result.stdout.strip().splitlines():
                    parts = line.split("|", 3)
                    if len(parts) < 4:
                        continue
                    full_hash, short_hash, timestamp, reason = parts

                    files_changed = insertions = deletions = 0
                    try:
                        stat = subprocess.run(
                            ["git", "diff", "--shortstat", f"{full_hash}~1", full_hash],
                            cwd=str(child), capture_output=True, text=True, timeout=5,
                            env={**os.environ, "GIT_DIR": str(child)},
                        )
                        if stat.returncode == 0 and stat.stdout.strip():
                            s = stat.stdout.strip()
                            m = re.search(r"(\d+) file", s)
                            if m:
                                files_changed = int(m.group(1))
                            m = re.search(r"(\d+) insertion", s)
                            if m:
                                insertions = int(m.group(1))
                            m = re.search(r"(\d+) deletion", s)
                            if m:
                                deletions = int(m.group(1))
                    except Exception:
                        pass

                    snapshots.append({
                        "hash": short_hash,
                        "full_hash": full_hash,
                        "timestamp": timestamp,
                        "reason": reason,
                        "files_changed": files_changed,
                        "insertions": insertions,
                        "deletions": deletions,
                    })
        except Exception:
            pass

        results.append({
            "working_dir": working_dir,
            "dir_hash": child.name,
            "count": len(snapshots),
            "snapshots": snapshots,
        })

    return results


# =========================================================================
# Memory
# =========================================================================

def get_memory() -> Dict[str, Optional[str]]:
    """Read memory, soul, and config files from ~/.hermes/."""
    result: Dict[str, Optional[str]] = {}
    files = [
        ("memory", MEMORIES_DIR / "MEMORY.md"),
        ("user", MEMORIES_DIR / "USER.md"),
        ("soul", HERMES_HOME / "SOUL.md"),
        ("config", HERMES_HOME / "config.yaml"),
    ]
    for key, path in files:
        if path.exists():
            try:
                result[key] = path.read_text(encoding="utf-8")
            except Exception:
                pass
    # .env with secrets redacted
    env_path = HERMES_HOME / ".env"
    if env_path.exists():
        try:
            lines = env_path.read_text(encoding="utf-8").splitlines()
            redacted = []
            for line in lines:
                if "=" in line and not line.lstrip().startswith("#"):
                    k, _, v = line.partition("=")
                    v = v.strip().strip("'\"")
                    if len(v) > 4:
                        v = v[:2] + "****" + v[-2:]
                    elif v:
                        v = "****"
                    redacted.append(f"{k}={v}")
                else:
                    redacted.append(line)
            result["env"] = "\n".join(redacted)
        except Exception:
            pass
    return result
