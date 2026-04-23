"""
alerts.py - Threshold alerts for Claude Code usage.

Monitors token usage per 5-hour rolling window and fires webhook
notifications when warn/critical thresholds are crossed.
"""

import json
import sqlite3
import urllib.request
from datetime import datetime, timezone, timedelta


# Token limits per 5-hour block, by plan name.
# api plan has no limit — alerting is skipped.
PLAN_LIMITS = {
    "pro":      44000,
    "max_5x":   88000,
    "max_20x":  220000,
    "api":      None,
}


def init_alert_table(conn):
    """Create the alert_state table if it doesn't exist."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS alert_state (
            account         TEXT PRIMARY KEY,
            last_level      TEXT,
            last_fired_at   TEXT
        )
    """)
    conn.commit()


def compute_block_usage(conn, account, plan, window_seconds=5 * 3600):
    """Return fraction of plan limit used in the last window_seconds.

    Returns None if the plan has no limit (e.g. api).
    """
    limit = PLAN_LIMITS.get(plan)
    if limit is None:
        return None

    cutoff = (datetime.now(timezone.utc) - timedelta(seconds=window_seconds)).isoformat()

    row = conn.execute("""
        SELECT COALESCE(SUM(input_tokens + output_tokens), 0) as total
        FROM turns
        WHERE account = ? AND timestamp >= ?
    """, (account, cutoff)).fetchone()

    total = row[0] if row else 0
    return total / limit


def check_and_fire(config, db_path):
    """Check all accounts for threshold crossings and fire webhooks.

    Iterates accounts from config, computes rolling-window usage,
    and sends webhook POSTs when a threshold is crossed upward.
    Tracks last-fired level in alert_state to avoid duplicate alerts.
    """
    from scanner import get_db, init_db

    conn = get_db(db_path)
    init_db(conn)
    init_alert_table(conn)

    thresholds = config.get("thresholds", {})
    warn_threshold = thresholds.get("warn", 0.75)
    critical_threshold = thresholds.get("critical", 0.95)
    webhooks = config.get("webhooks", [])
    accounts = config.get("accounts", [])

    fired_count = 0

    for acct in accounts:
        name = acct.get("name", "default")
        plan = acct.get("plan", "pro")

        fraction = compute_block_usage(conn, name, plan)
        if fraction is None:
            # No limit for this plan — skip alerting
            continue

        # Determine current level
        if fraction >= critical_threshold:
            current_level = "critical"
        elif fraction >= warn_threshold:
            current_level = "warn"
        else:
            current_level = "ok"

        # Look up last-fired level
        row = conn.execute(
            "SELECT last_level FROM alert_state WHERE account = ?",
            (name,)
        ).fetchone()
        last_level = row[0] if row else "ok"

        # Only fire when crossing UP a threshold
        level_order = {"ok": 0, "warn": 1, "critical": 2}
        if level_order.get(current_level, 0) <= level_order.get(last_level, 0):
            continue

        # Compute block reset time (end of current 5h window)
        block_reset_at = (
            datetime.now(timezone.utc) + timedelta(hours=5)
        ).isoformat()

        payload = {
            "account": name,
            "level": current_level,
            "usage_fraction": round(fraction, 4),
            "block_reset_at": block_reset_at,
        }

        # Fire matching webhooks
        for wh in webhooks:
            url = wh.get("url", "")
            on_levels = wh.get("on", ["warn", "critical"])
            if not url or current_level not in on_levels:
                continue

            try:
                data = json.dumps(payload).encode("utf-8")
                req = urllib.request.Request(
                    url,
                    data=data,
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                urllib.request.urlopen(req, timeout=10)
                fired_count += 1
            except Exception as e:
                print(f"  Warning: webhook failed for {name} -> {url}: {e}")

        # Update alert_state
        now_iso = datetime.now(timezone.utc).isoformat()
        conn.execute("""
            INSERT INTO alert_state (account, last_level, last_fired_at)
            VALUES (?, ?, ?)
            ON CONFLICT(account) DO UPDATE SET
                last_level = excluded.last_level,
                last_fired_at = excluded.last_fired_at
        """, (name, current_level, now_iso))
        conn.commit()

        print(f"  Alert: {name} -> {current_level} ({fraction:.1%} of {plan} limit)")

    conn.close()
    return fired_count
