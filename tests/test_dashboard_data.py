"""Tests for dashboard.data — read-only data access layer."""

import json
import sqlite3
import time
from pathlib import Path

import pytest


@pytest.fixture
def hermes_home(tmp_path, monkeypatch):
    """Set up a fake HERMES_HOME with state.db and supporting files."""
    home = tmp_path / "hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))

    # Create directories
    (home / "logs").mkdir()
    (home / "cron").mkdir()
    (home / "cron" / "output").mkdir()
    (home / "memories").mkdir()

    # Create state.db with sessions and messages tables
    db_path = home / "state.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute("""CREATE TABLE sessions (
        id TEXT PRIMARY KEY,
        source TEXT,
        user_id TEXT,
        model TEXT,
        started_at REAL,
        ended_at REAL,
        message_count INTEGER DEFAULT 0,
        tool_call_count INTEGER DEFAULT 0,
        input_tokens INTEGER DEFAULT 0,
        output_tokens INTEGER DEFAULT 0,
        cache_read_tokens INTEGER DEFAULT 0,
        cache_write_tokens INTEGER DEFAULT 0,
        reasoning_tokens INTEGER DEFAULT 0,
        estimated_cost_usd REAL DEFAULT 0,
        cost_status TEXT,
        billing_provider TEXT,
        billing_mode TEXT,
        title TEXT,
        end_reason TEXT,
        parent_session_id TEXT
    )""")
    conn.execute("""CREATE TABLE messages (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id TEXT,
        role TEXT,
        content TEXT,
        tool_call_id TEXT,
        tool_calls TEXT,
        tool_name TEXT,
        timestamp REAL,
        token_count INTEGER,
        finish_reason TEXT
    )""")
    conn.execute("""CREATE VIRTUAL TABLE messages_fts USING fts5(content, content=messages, content_rowid=id)""")

    # Insert test data
    now = time.time()
    conn.execute(
        "INSERT INTO sessions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("sess_001", "telegram", "user1", "claude-opus-4-6", now - 3600, now - 3500,
         5, 2, 1000, 200, 500, 100, 0, 0.01, None, None, None, "Test Session", None, None),
    )
    conn.execute(
        "INSERT INTO sessions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("sess_002", "cli", "user1", "claude-opus-4-6", now - 7200, now - 7100,
         3, 1, 500, 100, 200, 50, 0, 0.005, None, None, None, "CLI Session", None, None),
    )
    conn.execute(
        "INSERT INTO sessions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("cron_abc123_20260322_120000", "cron", None, "claude-opus-4-6", now - 1800, now - 1700,
         2, 0, 300, 50, 100, 0, 0, 0.002, None, None, None, None, None, None),
    )

    conn.execute(
        "INSERT INTO messages (session_id, role, content, timestamp) VALUES (?, ?, ?, ?)",
        ("sess_001", "user", "Hello there", now - 3600),
    )
    conn.execute(
        "INSERT INTO messages (session_id, role, content, timestamp) VALUES (?, ?, ?, ?)",
        ("sess_001", "assistant", "Hi! How can I help?", now - 3590),
    )
    conn.execute(
        "INSERT INTO messages (session_id, role, content, timestamp, tool_name) VALUES (?, ?, ?, ?, ?)",
        ("sess_001", "tool", "search result here", now - 3580, "search_files"),
    )
    # Populate FTS
    conn.execute("INSERT INTO messages_fts(messages_fts) VALUES('rebuild')")
    conn.commit()
    conn.close()

    # Reset cached schema detection
    import dashboard.data as dd
    dd._sessions_columns = None
    dd.HERMES_HOME = home
    dd.DB_PATH = home / "state.db"
    dd.LOG_PATH = home / "logs" / "gateway.log"
    dd.CRON_DIR = home / "cron"
    dd.JOBS_FILE = home / "cron" / "jobs.json"
    dd.OUTPUT_DIR = home / "cron" / "output"
    dd.MEMORIES_DIR = home / "memories"
    dd.PID_PATH = home / "gateway.pid"
    dd.CHECKPOINT_BASE = home / "checkpoints"

    return home


class TestGetGatewayStatus:
    def test_no_pid_file(self, hermes_home):
        from dashboard.data import get_gateway_status
        status = get_gateway_status()
        assert status["running"] is False
        assert status["pid"] is None
        assert status["session_count"] == 3
        assert status["message_count"] == 3

    def test_with_stale_pid(self, hermes_home):
        from dashboard.data import get_gateway_status
        (hermes_home / "gateway.pid").write_text("99999999")
        status = get_gateway_status()
        assert status["running"] is False


class TestGetSessions:
    def test_all_sessions(self, hermes_home):
        from dashboard.data import get_sessions
        sessions = get_sessions()
        assert len(sessions) == 3

    def test_filter_by_source(self, hermes_home):
        from dashboard.data import get_sessions
        sessions = get_sessions(source="telegram")
        assert len(sessions) == 1
        assert sessions[0]["id"] == "sess_001"

    def test_limit_and_offset(self, hermes_home):
        from dashboard.data import get_sessions
        sessions = get_sessions(limit=1)
        assert len(sessions) == 1
        sessions = get_sessions(limit=1, offset=1)
        assert len(sessions) == 1

    def test_includes_optional_columns(self, hermes_home):
        from dashboard.data import get_sessions
        sessions = get_sessions()
        assert "title" in sessions[0]
        assert "cache_read_tokens" in sessions[0]


class TestGetSession:
    def test_found(self, hermes_home):
        from dashboard.data import get_session
        s = get_session("sess_001")
        assert s is not None
        assert s["source"] == "telegram"

    def test_not_found(self, hermes_home):
        from dashboard.data import get_session
        assert get_session("nonexistent") is None


class TestGetMessages:
    def test_returns_messages(self, hermes_home):
        from dashboard.data import get_messages
        msgs = get_messages("sess_001")
        assert len(msgs) == 3
        assert msgs[0]["role"] == "user"
        assert msgs[1]["role"] == "assistant"

    def test_empty_session(self, hermes_home):
        from dashboard.data import get_messages
        msgs = get_messages("nonexistent")
        assert msgs == []


class TestGetUsage:
    def test_returns_summary(self, hermes_home):
        from dashboard.data import get_usage
        usage = get_usage(days=30)
        assert usage["summary"]["total_sessions"] == 3
        assert usage["summary"]["total_input_tokens"] == 1800
        assert usage["summary"]["total_output_tokens"] == 350

    def test_filter_by_source(self, hermes_home):
        from dashboard.data import get_usage
        usage = get_usage(days=30, source="telegram")
        assert usage["summary"]["total_sessions"] == 1

    def test_narrow_time_window(self, hermes_home):
        from dashboard.data import get_usage
        # Sessions are within last 2 hours — a 1-day window should include all
        usage = get_usage(days=1)
        assert usage["summary"]["total_sessions"] == 3


class TestCronJobs:
    def test_no_jobs_file(self, hermes_home):
        from dashboard.data import get_cron_jobs
        assert get_cron_jobs() == []

    def test_with_jobs(self, hermes_home):
        from dashboard.data import get_cron_jobs, get_cron_status
        jobs_data = {"jobs": [
            {"id": "abc123", "name": "Daily report", "cron": "0 9 * * *", "enabled": True},
            {"id": "def456", "name": "Cleanup", "cron": "0 0 * * 0", "enabled": False},
        ]}
        (hermes_home / "cron" / "jobs.json").write_text(json.dumps(jobs_data))
        jobs = get_cron_jobs()
        assert len(jobs) == 2
        status = get_cron_status()
        assert status["total"] == 2
        assert status["enabled"] == 1
        assert status["disabled"] == 1

    def test_cron_runs(self, hermes_home):
        from dashboard.data import get_cron_runs
        runs = get_cron_runs(days=7)
        assert "abc123" in runs["runs_by_job_id"]
        entries = runs["runs_by_job_id"]["abc123"]["entries"]
        assert len(entries) == 1

    def test_cron_outputs(self, hermes_home):
        from dashboard.data import list_cron_outputs, read_cron_output
        assert list_cron_outputs("abc123") == []
        # Create an output file
        out_dir = hermes_home / "cron" / "output" / "abc123"
        out_dir.mkdir(parents=True)
        (out_dir / "2026-03-22.md").write_text("# Report\nAll good")
        outputs = list_cron_outputs("abc123")
        assert len(outputs) == 1
        content = read_cron_output("abc123", "2026-03-22.md")
        assert "All good" in content

    def test_path_traversal_blocked(self, hermes_home):
        from dashboard.data import read_cron_output
        assert read_cron_output("abc123", "../../../etc/passwd") is None


class TestLogs:
    def test_no_log_file(self, hermes_home):
        from dashboard.data import tail_log
        assert tail_log() == ""

    def test_with_log(self, hermes_home):
        from dashboard.data import tail_log
        log_path = hermes_home / "logs" / "gateway.log"
        log_path.write_text("line1\nline2\nline3\n")
        result = tail_log(2)
        assert "line2" in result
        assert "line3" in result


class TestSearch:
    def test_search_finds_content(self, hermes_home):
        from dashboard.data import search_transcripts
        results = search_transcripts("Hello")
        assert len(results) >= 1
        assert results[0]["session_id"] == "sess_001"

    def test_empty_query(self, hermes_home):
        from dashboard.data import search_transcripts
        assert search_transcripts("") == []

    def test_no_results(self, hermes_home):
        from dashboard.data import search_transcripts
        results = search_transcripts("xyznonexistent")
        assert results == []


class TestFeed:
    def test_returns_messages(self, hermes_home):
        from dashboard.data import get_feed
        feed = get_feed()
        assert len(feed) >= 1
        # Only user/assistant messages from gateway platforms
        for msg in feed:
            assert msg["role"] in ("user", "assistant")

    def test_filter_by_source(self, hermes_home):
        from dashboard.data import get_feed
        feed = get_feed(source="telegram")
        assert all(m["source"] == "telegram" for m in feed)


class TestMemory:
    def test_reads_memory_files(self, hermes_home):
        from dashboard.data import get_memory
        (hermes_home / "memories" / "MEMORY.md").write_text("# Memory\nTest content")
        (hermes_home / "config.yaml").write_text("model: claude-opus-4-6")
        mem = get_memory()
        assert "memory" in mem
        assert "Test content" in mem["memory"]
        assert "config" in mem

    def test_redacts_env_secrets(self, hermes_home):
        from dashboard.data import get_memory
        (hermes_home / ".env").write_text("API_KEY=sk-1234567890abcdef")
        mem = get_memory()
        assert "env" in mem
        assert "1234567890abcdef" not in mem["env"]
        assert "****" in mem["env"]


class TestSchemaDetection:
    def test_detects_columns(self, hermes_home):
        from dashboard.data import _get_session_columns
        cols = _get_session_columns()
        assert "id" in cols
        assert "title" in cols
        assert "cache_read_tokens" in cols

    def test_missing_column_graceful(self, hermes_home):
        """Sessions should still work even if optional columns are missing."""
        import dashboard.data as dd
        # Create a minimal DB without optional columns
        minimal_db = hermes_home / "minimal.db"
        conn = sqlite3.connect(str(minimal_db))
        conn.execute("""CREATE TABLE sessions (
            id TEXT PRIMARY KEY,
            source TEXT,
            user_id TEXT,
            model TEXT,
            started_at REAL,
            ended_at REAL,
            message_count INTEGER DEFAULT 0,
            tool_call_count INTEGER DEFAULT 0,
            input_tokens INTEGER DEFAULT 0,
            output_tokens INTEGER DEFAULT 0
        )""")
        conn.execute("""CREATE TABLE messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT,
            role TEXT,
            content TEXT,
            tool_call_id TEXT,
            tool_calls TEXT,
            tool_name TEXT,
            timestamp REAL,
            token_count INTEGER,
            finish_reason TEXT
        )""")
        now = time.time()
        conn.execute(
            "INSERT INTO sessions (id, source, model, started_at, input_tokens, output_tokens) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("s1", "cli", "gpt-4", now, 100, 50),
        )
        conn.commit()
        conn.close()

        dd._sessions_columns = None
        dd.DB_PATH = minimal_db
        from dashboard.data import get_sessions
        sessions = get_sessions()
        assert len(sessions) == 1
        assert sessions[0]["id"] == "s1"
        # Optional columns should not appear
        assert "title" not in sessions[0]
        assert "cache_read_tokens" not in sessions[0]


class TestInsights:
    def test_returns_empty_on_import_error(self, hermes_home):
        from dashboard.data import get_insights
        result = get_insights(days=7)
        assert result.get("empty") is True or "days" in result


class TestPlugins:
    def test_no_plugins_dir(self, hermes_home):
        from dashboard.data import get_plugins
        plugins = get_plugins()
        assert plugins == []

    def test_with_plugin(self, hermes_home, monkeypatch):
        import dashboard.data as dd
        monkeypatch.setattr(dd, "PLUGINS_DIRS", [hermes_home / "plugins"])
        plugin_dir = hermes_home / "plugins" / "test-plugin"
        plugin_dir.mkdir(parents=True)
        (plugin_dir / "plugin.yaml").write_text(
            "name: test-plugin\nversion: 1.0\ndescription: A test plugin"
        )
        from dashboard.data import get_plugins
        plugins = get_plugins()
        assert len(plugins) == 1
        assert plugins[0]["name"] == "test-plugin"


class TestCheckpoints:
    def test_no_checkpoints(self, hermes_home):
        from dashboard.data import get_checkpoints
        assert get_checkpoints() == []


class TestGatewayStatusJsonPid:
    def test_json_pid_format(self, hermes_home):
        from dashboard.data import get_gateway_status
        # PID file with JSON format (gateway uses this)
        (hermes_home / "gateway.pid").write_text('{"pid": 99999999}')
        status = get_gateway_status()
        assert status["running"] is False  # PID won't exist
        assert status["pid"] is None
