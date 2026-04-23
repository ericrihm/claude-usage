"""Tests for multi-account scanning, DB tagging, and deduplication.

Runnable standalone:  python tests/test_multi_account.py
"""

import os
import sys
import sqlite3
import tempfile
from pathlib import Path

# Ensure project root is on sys.path so imports work when run standalone
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scanner import get_db, init_db, scan, scan_all


# ── Paths to fixture data ────────────────────────────────────────────────────
FIXTURES = Path(__file__).resolve().parent / "fixtures"
ACCT1_PROJECTS = FIXTURES / "acct1" / "projects"
ACCT2_PROJECTS = FIXTURES / "acct2" / "projects"


def _make_temp_db():
    """Create a temporary directory and return a fresh DB path inside it."""
    tmpdir = tempfile.mkdtemp(prefix="claude_usage_test_")
    return Path(tmpdir) / "usage.db"


# ── Test 1: Migration adds account column to existing DB ─────────────────────

def test_migration_adds_account_column():
    """init_db should add the 'account' column to tables missing it."""
    db_path = _make_temp_db()
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    # Create the old schema WITHOUT the account column
    conn.executescript("""
        CREATE TABLE sessions (
            session_id      TEXT PRIMARY KEY,
            project_name    TEXT,
            first_timestamp TEXT,
            last_timestamp  TEXT,
            git_branch      TEXT,
            total_input_tokens      INTEGER DEFAULT 0,
            total_output_tokens     INTEGER DEFAULT 0,
            total_cache_read        INTEGER DEFAULT 0,
            total_cache_creation    INTEGER DEFAULT 0,
            model           TEXT,
            turn_count      INTEGER DEFAULT 0
        );
        CREATE TABLE turns (
            id                      INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id              TEXT,
            timestamp               TEXT,
            model                   TEXT,
            input_tokens            INTEGER DEFAULT 0,
            output_tokens           INTEGER DEFAULT 0,
            cache_read_tokens       INTEGER DEFAULT 0,
            cache_creation_tokens   INTEGER DEFAULT 0,
            tool_name               TEXT,
            cwd                     TEXT,
            message_id              TEXT
        );
        CREATE TABLE processed_files (
            path    TEXT PRIMARY KEY,
            mtime   REAL,
            lines   INTEGER
        );
    """)
    conn.commit()

    # Insert a pre-existing row so we can verify it survives migration
    conn.execute("""
        INSERT INTO sessions (session_id, project_name, total_input_tokens)
        VALUES ('old-sess', 'legacy/project', 999)
    """)
    conn.commit()

    # Run init_db — should add account columns without crashing
    init_db(conn)

    # Verify account column exists on both tables
    session_cols = {r[1] for r in conn.execute("PRAGMA table_info(sessions)").fetchall()}
    turn_cols = {r[1] for r in conn.execute("PRAGMA table_info(turns)").fetchall()}

    assert "account" in session_cols, "account column missing from sessions table"
    assert "account" in turn_cols, "account column missing from turns table"

    # Verify the pre-existing row still has its data and got the default
    row = conn.execute("SELECT * FROM sessions WHERE session_id = 'old-sess'").fetchone()
    assert row is not None, "pre-existing session lost during migration"
    assert row["total_input_tokens"] == 999
    assert row["account"] == "default", f"expected 'default', got '{row['account']}'"

    conn.close()
    print("  PASS  test_migration_adds_account_column")


# ── Test 2: scan_all tags rows with correct account names ────────────────────

def test_scan_all_tags_accounts():
    """scan_all should tag sessions and turns with the correct account name."""
    db_path = _make_temp_db()

    config = {
        "accounts": [
            {"name": "acct1", "path": str(FIXTURES / "acct1"), "plan": "pro"},
            {"name": "acct2", "path": str(FIXTURES / "acct2"), "plan": "pro"},
        ],
        "thresholds": {"warn": 0.75, "critical": 0.95},
        "webhooks": [],
    }

    results = scan_all(config, db_path=db_path, verbose=False)

    # Both accounts should have scanned successfully
    assert len(results) == 2, f"expected 2 results, got {len(results)}"

    acct1_result = next(r for r in results if r["account"] == "acct1")
    acct2_result = next(r for r in results if r["account"] == "acct2")

    assert acct1_result["turns"] > 0, "acct1 should have scanned turns"
    assert acct2_result["turns"] > 0, "acct2 should have scanned turns"

    # Verify DB contents
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    # Check sessions are tagged
    acct1_sessions = conn.execute(
        "SELECT * FROM sessions WHERE account = 'acct1'"
    ).fetchall()
    acct2_sessions = conn.execute(
        "SELECT * FROM sessions WHERE account = 'acct2'"
    ).fetchall()

    assert len(acct1_sessions) >= 1, "acct1 should have at least 1 session"
    assert len(acct2_sessions) >= 1, "acct2 should have at least 1 session"

    # Check turns are tagged
    acct1_turns = conn.execute(
        "SELECT * FROM turns WHERE account = 'acct1'"
    ).fetchall()
    acct2_turns = conn.execute(
        "SELECT * FROM turns WHERE account = 'acct2'"
    ).fetchall()

    assert len(acct1_turns) == 4, f"acct1 should have 4 turns, got {len(acct1_turns)}"
    assert len(acct2_turns) == 3, f"acct2 should have 3 turns, got {len(acct2_turns)}"

    # Verify models are correct per account
    acct1_models = {t["model"] for t in acct1_turns}
    acct2_models = {t["model"] for t in acct2_turns}

    assert "claude-opus-4-6" in acct1_models, "acct1 fixture uses opus"
    assert "claude-sonnet-4-6" in acct2_models, "acct2 fixture uses sonnet"

    # Verify no cross-contamination
    assert all(t["account"] == "acct1" for t in acct1_turns)
    assert all(t["account"] == "acct2" for t in acct2_turns)

    conn.close()
    print("  PASS  test_scan_all_tags_accounts")


# ── Test 3: Re-running scan doesn't duplicate turns ──────────────────────────

def test_rescan_no_duplicates():
    """Running scan_all twice should not produce duplicate turn rows."""
    db_path = _make_temp_db()

    config = {
        "accounts": [
            {"name": "acct1", "path": str(FIXTURES / "acct1"), "plan": "pro"},
            {"name": "acct2", "path": str(FIXTURES / "acct2"), "plan": "pro"},
        ],
        "thresholds": {"warn": 0.75, "critical": 0.95},
        "webhooks": [],
    }

    # First scan
    scan_all(config, db_path=db_path, verbose=False)

    conn = sqlite3.connect(db_path)
    first_turn_count = conn.execute("SELECT COUNT(*) FROM turns").fetchone()[0]
    first_session_count = conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
    conn.close()

    # Second scan — should be a no-op (files not modified)
    results2 = scan_all(config, db_path=db_path, verbose=False)

    conn = sqlite3.connect(db_path)
    second_turn_count = conn.execute("SELECT COUNT(*) FROM turns").fetchone()[0]
    second_session_count = conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
    conn.close()

    assert second_turn_count == first_turn_count, (
        f"turns duplicated: {first_turn_count} -> {second_turn_count}"
    )
    assert second_session_count == first_session_count, (
        f"sessions duplicated: {first_session_count} -> {second_session_count}"
    )

    # Verify second scan skipped all files
    for r in results2:
        assert r["new"] == 0, f"account {r['account']} should have no new files"
        assert r["turns"] == 0, f"account {r['account']} should report 0 new turns"

    # Force rescan (delete processed_files) and verify unique index prevents dupes
    conn = sqlite3.connect(db_path)
    conn.execute("DELETE FROM processed_files")
    conn.commit()
    conn.close()

    scan_all(config, db_path=db_path, verbose=False)

    conn = sqlite3.connect(db_path)
    third_turn_count = conn.execute("SELECT COUNT(*) FROM turns").fetchone()[0]
    conn.close()

    assert third_turn_count == first_turn_count, (
        f"turns duplicated after forced rescan: {first_turn_count} -> {third_turn_count}"
    )

    print("  PASS  test_rescan_no_duplicates")


# ── Test 4: Accounts with no data produce zero rows ──────────────────────────

def test_empty_account_produces_zero_rows():
    """An account pointing to a nonexistent directory should produce zero rows."""
    db_path = _make_temp_db()
    empty_dir = tempfile.mkdtemp(prefix="claude_usage_empty_")

    config = {
        "accounts": [
            {"name": "empty-acct", "path": empty_dir, "plan": "pro"},
            {"name": "acct1", "path": str(FIXTURES / "acct1"), "plan": "pro"},
        ],
        "thresholds": {"warn": 0.75, "critical": 0.95},
        "webhooks": [],
    }

    results = scan_all(config, db_path=db_path, verbose=False)

    empty_result = next(r for r in results if r["account"] == "empty-acct")
    assert empty_result["turns"] == 0, "empty account should have 0 turns"
    assert empty_result["sessions"] == 0, "empty account should have 0 sessions"
    assert empty_result["new"] == 0, "empty account should have 0 new files"

    # Verify no rows in DB for the empty account
    conn = sqlite3.connect(db_path)
    empty_turns = conn.execute(
        "SELECT COUNT(*) FROM turns WHERE account = 'empty-acct'"
    ).fetchone()[0]
    empty_sessions = conn.execute(
        "SELECT COUNT(*) FROM sessions WHERE account = 'empty-acct'"
    ).fetchone()[0]
    conn.close()

    assert empty_turns == 0, "empty account should have 0 turn rows in DB"
    assert empty_sessions == 0, "empty account should have 0 session rows in DB"

    # Meanwhile acct1 should have data
    acct1_result = next(r for r in results if r["account"] == "acct1")
    assert acct1_result["turns"] > 0, "acct1 should have turns"

    print("  PASS  test_empty_account_produces_zero_rows")


# ── Runner ────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("test_multi_account.py")
    print("-" * 50)
    test_migration_adds_account_column()
    test_scan_all_tags_accounts()
    test_rescan_no_duplicates()
    test_empty_account_produces_zero_rows()
    print("-" * 50)
    print("All multi-account tests passed.")
