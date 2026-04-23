# Changelog

## Fork: Multi-Account Support (Phases 1-5)

### Multi-Account Configuration
- Added `config.py` with a minimal hand-written YAML parser (no PyYAML dependency)
- Added `accounts.yaml.example` showing multi-account, threshold, and webhook configuration
- Accounts support mixed WSL and Windows paths (`~/.claude-*` and `/mnt/c/Users/.../.claude-*`)
- Falls back to single default account (`~/.claude`) when no `accounts.yaml` is present

### Per-Account Scanning and DB Tagging
- Added `account` column to both `sessions` and `turns` tables with automatic schema migration
- `scan_all()` iterates configured accounts, calls `scan()` per account, tags all rows with account name
- Disjoint path validation prevents two accounts from scanning the same directory
- Summary table printed after multi-account scans

### Dashboard Account Filtering and Comparison Chart
- Account filter dropdown in the dashboard header (filter to single account or view all)
- "Compare Accounts" stacked bar chart with 5h / 24h / 7d window selectors
- Account progress strip in the header showing per-account usage fraction with color-coded bars
- `/api/accounts` and `/api/compare` API endpoints added to `dashboard.py`
- Account selection persisted in URL query parameters

### Webhook Alerts on Threshold Crossings
- Added `alerts.py` with rolling 5-hour window usage monitoring
- Plan-aware token limits (pro: 44K, max_5x: 88K, max_20x: 220K, api: unlimited)
- Fires webhook POST when usage crosses warn or critical thresholds upward
- `alert_state` table tracks last-fired level per account to prevent duplicate alerts
- `python cli.py alerts` command for manual threshold checks

### Tests and Documentation
- Added `tests/test_multi_account.py` covering schema migration, account tagging, deduplication, and empty-account handling
- Added `tests/test_alerts.py` covering threshold detection, duplicate suppression, escalation, and API plan skipping
- Test fixtures at `tests/fixtures/` with realistic multi-account JSONL session data
- Updated README.md with Multi-Account Setup section
- Updated CHANGELOG.md documenting all fork changes

---

## 2026-04-09

- Fix token counts inflated ~2x by deduplicating streaming events that share the same message ID
- Fix session cost totals that were inflated when sessions spanned multiple JSONL files
- Fix pricing to match current Anthropic API rates (Opus $5/$25, Sonnet $3/$15, Haiku $1/$5)
- Add CI test suite (84 tests) and GitHub Actions workflow running on every PR
- Add sortable columns to Sessions, Cost by Model, and new Cost by Project tables
- Add CSV export for Sessions and Projects (all filtered data, not just top 20)
- Add Rescan button to dashboard for full database rebuild
- Add Xcode project directory support and `--projects-dir` CLI option
- Non-Anthropic models (gemma, glm, etc.) no longer incorrectly charged at Sonnet rates
- CLI and dashboard now both compute costs per-turn for consistent results
