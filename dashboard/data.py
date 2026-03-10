"""
Read-only data access for the Hermes Dashboard.

All functions open their own DB connections (read-only mode) and close them
when done. No persistent state — safe for a single-user polling UI.
"""

import json
import os
import sqlite3
import time
from pathlib import Path
from typing import Any, Dict, List, Optional


HERMES_HOME = Path(os.getenv("HERMES_HOME", Path.home() / ".hermes"))
DB_PATH = HERMES_HOME / "state.db"
JSONL_PATH = HERMES_HOME / "token_usage.jsonl"
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


# =========================================================================
# Gateway status
# =========================================================================

def get_gateway_status() -> Dict[str, Any]:
    """Check if the gateway is running and basic stats."""
    running = False
    pid = None

    if PID_PATH.exists():
        try:
            pid = int(PID_PATH.read_text().strip())
            os.kill(pid, 0)
            running = True
        except (ValueError, ProcessLookupError, PermissionError):
            pid = None

    session_count = 0
    message_count = 0
    if DB_PATH.exists():
        try:
            conn = _ro_connect()
            session_count = conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
            message_count = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
            conn.close()
        except Exception:
            pass

    return {
        "running": running,
        "pid": pid,
        "session_count": session_count,
        "message_count": message_count,
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
        if source:
            rows = conn.execute(
                "SELECT id, source, user_id, model, started_at, ended_at, "
                "message_count, tool_call_count, input_tokens, output_tokens "
                "FROM sessions WHERE source = ? ORDER BY started_at DESC LIMIT ? OFFSET ?",
                (source, limit, offset),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT id, source, user_id, model, started_at, ended_at, "
                "message_count, tool_call_count, input_tokens, output_tokens "
                "FROM sessions ORDER BY started_at DESC LIMIT ? OFFSET ?",
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
# Token usage (InsightsEngine + JSONL)
# =========================================================================

def get_usage(days: int = 30, source: Optional[str] = None) -> Dict[str, Any]:
    """Get merged usage data from DB sessions and JSONL token log."""
    cutoff = time.time() - (days * 86400)
    result: Dict[str, Any] = {
        "days": days,
        "sessions": [],
        "jsonl_entries": [],
        "summary": {},
    }

    # DB session-level aggregates
    if DB_PATH.exists():
        conn = _ro_connect()
        try:
            if source:
                rows = conn.execute(
                    "SELECT id, source, model, started_at, ended_at, "
                    "message_count, tool_call_count, input_tokens, output_tokens "
                    "FROM sessions WHERE started_at >= ? AND source = ? "
                    "ORDER BY started_at DESC",
                    (cutoff, source),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT id, source, model, started_at, ended_at, "
                    "message_count, tool_call_count, input_tokens, output_tokens "
                    "FROM sessions WHERE started_at >= ? ORDER BY started_at DESC",
                    (cutoff,),
                ).fetchall()
            result["sessions"] = [dict(r) for r in rows]
        finally:
            conn.close()

    # JSONL per-call detail
    if JSONL_PATH.exists():
        try:
            with open(JSONL_PATH, "r") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                        # Filter by time if we can
                        ts = entry.get("ts", "")
                        if ts:
                            from datetime import datetime
                            try:
                                dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                                if dt.timestamp() < cutoff:
                                    continue
                            except (ValueError, TypeError):
                                pass
                        result["jsonl_entries"].append(entry)
                    except json.JSONDecodeError:
                        continue
        except Exception:
            pass

    # Summary
    total_input = sum(s.get("input_tokens") or 0 for s in result["sessions"])
    total_output = sum(s.get("output_tokens") or 0 for s in result["sessions"])
    total_cached = sum(e.get("cached_tokens", 0) for e in result["jsonl_entries"])
    total_cache_write = sum(e.get("cache_write_tokens", 0) for e in result["jsonl_entries"])

    result["summary"] = {
        "total_sessions": len(result["sessions"]),
        "total_input_tokens": total_input,
        "total_output_tokens": total_output,
        "total_tokens": total_input + total_output,
        "total_cached_tokens": total_cached,
        "total_cache_write_tokens": total_cache_write,
        "jsonl_calls": len(result["jsonl_entries"]),
    }

    return result


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


def list_cron_outputs(job_id: str) -> List[str]:
    """List output files for a cron job."""
    job_dir = OUTPUT_DIR / job_id
    if not job_dir.exists():
        return []
    files = sorted(job_dir.glob("*.md"), reverse=True)
    return [f.name for f in files]


def read_cron_output(job_id: str, filename: str) -> Optional[str]:
    """Read a single cron output file. Returns None if not found."""
    # Sanitize filename to prevent path traversal
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
        with open(LOG_PATH, "r", errors="replace") as f:
            all_lines = f.readlines()
            return "".join(all_lines[-lines:])
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
        where_clauses = ["messages_fts MATCH ?"]
        params: list = [query]

        sources = [source] if source else ["cli", "telegram", "discord", "whatsapp", "slack"]
        placeholders = ",".join("?" for _ in sources)
        where_clauses.append(f"s.source IN ({placeholders})")
        params.extend(sources)

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
# Memory
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
        sources = [source] if source else ["telegram", "whatsapp", "discord", "slack"]
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


def get_activity(limit: int = 100, offset: int = 0) -> List[Dict[str, Any]]:
    """Get recent per-call activity from JSONL token log (includes tool names)."""
    if not JSONL_PATH.exists():
        return []
    try:
        entries: List[Dict[str, Any]] = []
        with open(JSONL_PATH, "r") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        # Reverse for newest-first, then paginate
        entries.reverse()
        return entries[offset:offset + limit]
    except Exception:
        return []


def get_memory() -> Dict[str, Optional[str]]:
    """Read MEMORY.md and USER.md from ~/.hermes/memories/."""
    result: Dict[str, Optional[str]] = {"memory": None, "user": None}
    for key, filename in [("memory", "MEMORY.md"), ("user", "USER.md")]:
        path = MEMORIES_DIR / filename
        if path.exists():
            try:
                result[key] = path.read_text(encoding="utf-8")
            except Exception:
                pass
    return result
