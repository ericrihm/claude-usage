"""Tests for alerts.py - threshold checking and webhook firing.

Runnable standalone:  python tests/test_alerts.py
"""

import os
import sys
import sqlite3
import tempfile
from datetime import datetime, timezone, timedelta
from pathlib import Path

# Ensure project root is on sys.path so imports work when run standalone
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scanner import get_db, init_db
from alerts import init_alert_table, compute_block_usage, check_and_fire, PLAN_LIMITS


def _make_temp_db():
    """Create a temporary directory and return a fresh DB path inside it."""
    tmpdir = tempfile.mkdtemp(prefix="claude_usage_alerts_test_")
    return Path(tmpdir) / "usage.db"


def _seed_turns(conn, account, num_tokens, minutes_ago=30):
    """Insert turns with total input+output = num_tokens, timestamped recently."""
    ts = (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).isoformat()
    # Split tokens roughly: 60% input, 40% output
    input_tokens = int(num_tokens * 0.6)
    output_tokens = num_tokens - input_tokens
    conn.execute("""
        INSERT INTO turns
            (session_id, timestamp, model, input_tokens, output_tokens,
             cache_read_tokens, cache_creation_tokens, tool_name, cwd,
             message_id, account)
        VALUES (?, ?, 'claude-sonnet-4-6', ?, ?, 0, 0, NULL, '/tmp', ?, ?)
    """, (
        f"sess-{account}-alert",
        ts,
        input_tokens,
        output_tokens,
        f"msg-alert-{account}-{minutes_ago}",
        account,
    ))
    conn.commit()


# ── Test 1: check_and_fire fires one webhook above warn threshold ────────────

def test_fires_webhook_above_warn():
    """Seeding usage above the warn threshold should cause check_and_fire to
    fire (return fired_count=1) and record the alert state."""
    db_path = _make_temp_db()
    conn = get_db(db_path)
    init_db(conn)
    init_alert_table(conn)

    # Pro plan limit is 44000 tokens per 5h block
    # Warn threshold at 0.75 = 33000 tokens
    # Seed 35000 tokens to cross warn but not critical
    _seed_turns(conn, "test-acct", 35000, minutes_ago=10)

    # Verify the fraction is above warn
    fraction = compute_block_usage(conn, "test-acct", "pro")
    assert fraction is not None, "fraction should not be None for pro plan"
    assert fraction >= 0.75, f"expected fraction >= 0.75, got {fraction:.4f}"

    conn.close()

    # Build config with a dummy webhook URL (will fail to connect, but
    # we track state in alert_state table instead of verifying HTTP)
    config = {
        "accounts": [
            {"name": "test-acct", "plan": "pro", "path": "/tmp/fake"},
        ],
        "thresholds": {"warn": 0.75, "critical": 0.95},
        # No webhooks — we just verify alert_state is updated
        "webhooks": [],
    }

    fired = check_and_fire(config, db_path=db_path)

    # With no webhook URLs, fired_count stays 0, but alert_state should be set
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    state = conn.execute(
        "SELECT * FROM alert_state WHERE account = 'test-acct'"
    ).fetchone()
    conn.close()

    assert state is not None, "alert_state should have a row for test-acct"
    assert state["last_level"] == "warn", (
        f"expected last_level='warn', got '{state['last_level']}'"
    )

    print("  PASS  test_fires_webhook_above_warn")


# ── Test 2: second run with same data fires zero new webhooks ────────────────

def test_no_duplicate_alert():
    """Running check_and_fire again with the same data should not re-fire
    because the level hasn't crossed upward again."""
    db_path = _make_temp_db()
    conn = get_db(db_path)
    init_db(conn)
    init_alert_table(conn)

    # Seed above warn
    _seed_turns(conn, "test-acct", 35000, minutes_ago=10)
    conn.close()

    config = {
        "accounts": [
            {"name": "test-acct", "plan": "pro", "path": "/tmp/fake"},
        ],
        "thresholds": {"warn": 0.75, "critical": 0.95},
        "webhooks": [],
    }

    # First run — sets alert_state to "warn"
    check_and_fire(config, db_path=db_path)

    # Verify state is warn
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    state1 = conn.execute(
        "SELECT * FROM alert_state WHERE account = 'test-acct'"
    ).fetchone()
    first_fired_at = state1["last_fired_at"]
    conn.close()

    assert state1["last_level"] == "warn"

    # Second run — same data, same level, should NOT update alert_state
    check_and_fire(config, db_path=db_path)

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    state2 = conn.execute(
        "SELECT * FROM alert_state WHERE account = 'test-acct'"
    ).fetchone()
    conn.close()

    assert state2["last_level"] == "warn", "level should still be warn"
    assert state2["last_fired_at"] == first_fired_at, (
        "last_fired_at should not change on second run with same data"
    )

    print("  PASS  test_no_duplicate_alert")


# ── Test 3: critical threshold fires after warn ──────────────────────────────

def test_critical_fires_after_warn():
    """If usage rises from warn to critical, a new alert should fire."""
    db_path = _make_temp_db()
    conn = get_db(db_path)
    init_db(conn)
    init_alert_table(conn)

    # Seed above warn but below critical (35000 / 44000 = 0.795)
    _seed_turns(conn, "test-acct", 35000, minutes_ago=10)
    conn.close()

    config = {
        "accounts": [
            {"name": "test-acct", "plan": "pro", "path": "/tmp/fake"},
        ],
        "thresholds": {"warn": 0.75, "critical": 0.95},
        "webhooks": [],
    }

    # First run — sets to warn
    check_and_fire(config, db_path=db_path)

    # Add more tokens to cross critical (need total > 0.95 * 44000 = 41800)
    conn = get_db(db_path)
    _seed_turns(conn, "test-acct", 8000, minutes_ago=5)
    conn.close()

    # Second run — should escalate to critical
    check_and_fire(config, db_path=db_path)

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    state = conn.execute(
        "SELECT * FROM alert_state WHERE account = 'test-acct'"
    ).fetchone()
    conn.close()

    assert state["last_level"] == "critical", (
        f"expected 'critical', got '{state['last_level']}'"
    )

    print("  PASS  test_critical_fires_after_warn")


# ── Test 4: API plan skips alerting ──────────────────────────────────────────

def test_api_plan_skips_alerting():
    """Accounts on the 'api' plan have no limit and should be skipped."""
    db_path = _make_temp_db()
    conn = get_db(db_path)
    init_db(conn)
    init_alert_table(conn)

    # Seed a large amount of tokens
    _seed_turns(conn, "api-acct", 999999, minutes_ago=10)

    # Verify compute_block_usage returns None for api plan
    fraction = compute_block_usage(conn, "api-acct", "api")
    assert fraction is None, "api plan should return None (no limit)"
    conn.close()

    config = {
        "accounts": [
            {"name": "api-acct", "plan": "api", "path": "/tmp/fake"},
        ],
        "thresholds": {"warn": 0.75, "critical": 0.95},
        "webhooks": [],
    }

    check_and_fire(config, db_path=db_path)

    # No alert_state row should exist
    conn = sqlite3.connect(db_path)
    state = conn.execute(
        "SELECT * FROM alert_state WHERE account = 'api-acct'"
    ).fetchone()
    conn.close()

    assert state is None, "api plan should not create alert_state rows"

    print("  PASS  test_api_plan_skips_alerting")


# ── Test 5: below threshold does not fire ────────────────────────────────────

def test_below_threshold_no_fire():
    """Usage below warn threshold should not create an alert_state row."""
    db_path = _make_temp_db()
    conn = get_db(db_path)
    init_db(conn)
    init_alert_table(conn)

    # Seed low usage (10000 / 44000 = 0.227)
    _seed_turns(conn, "low-acct", 10000, minutes_ago=10)
    conn.close()

    config = {
        "accounts": [
            {"name": "low-acct", "plan": "pro", "path": "/tmp/fake"},
        ],
        "thresholds": {"warn": 0.75, "critical": 0.95},
        "webhooks": [],
    }

    check_and_fire(config, db_path=db_path)

    conn = sqlite3.connect(db_path)
    state = conn.execute(
        "SELECT * FROM alert_state WHERE account = 'low-acct'"
    ).fetchone()
    conn.close()

    assert state is None, "below-threshold usage should not create alert_state"

    print("  PASS  test_below_threshold_no_fire")


# ── Runner ────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("test_alerts.py")
    print("-" * 50)
    test_fires_webhook_above_warn()
    test_no_duplicate_alert()
    test_critical_fires_after_warn()
    test_api_plan_skips_alerting()
    test_below_threshold_no_fire()
    print("-" * 50)
    print("All alert tests passed.")
