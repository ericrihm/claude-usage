"""
config.py - Loads multi-account configuration from accounts.yaml.

Uses a minimal handwritten YAML parser (no PyYAML dependency).
Supports key:value pairs, lists with '- ' prefix, and two-space indent.
"""

import warnings
from pathlib import Path


def _parse_yaml(text):
    """Minimal YAML parser for accounts.yaml structure.

    Supports:
      - Top-level key: value
      - Top-level key: (followed by indented list of '- ' items)
      - Two-space indented key: value inside list items
      - Inline lists like [warn, critical]
      - Numeric values (int and float)
    """
    root = {}
    lines = text.splitlines()
    i = 0

    while i < len(lines):
        line = lines[i]
        stripped = line.rstrip()

        # Skip blank lines and comments
        if not stripped or stripped.lstrip().startswith("#"):
            i += 1
            continue

        # Top-level key (no leading whitespace)
        if not line[0].isspace() and ":" in stripped:
            key, _, rest = stripped.partition(":")
            key = key.strip()
            rest = rest.strip()

            if rest:
                # Inline value
                root[key] = _parse_scalar(rest)
                i += 1
            else:
                # Block value — peek at next non-blank line to determine type
                j = i + 1
                while j < len(lines) and not lines[j].strip():
                    j += 1

                if j < len(lines) and lines[j].lstrip().startswith("- "):
                    # List of items
                    items, i = _parse_list(lines, j)
                    root[key] = items
                elif j < len(lines) and lines[j][0].isspace():
                    # Mapping block
                    mapping, i = _parse_mapping(lines, j)
                    root[key] = mapping
                else:
                    root[key] = None
                    i = j
        else:
            i += 1

    return root


def _parse_list(lines, start):
    """Parse a YAML list starting at 'start'. Returns (list, next_index)."""
    items = []
    i = start
    while i < len(lines):
        line = lines[i]
        stripped = line.rstrip()

        if not stripped:
            i += 1
            continue

        # Check if we're still in an indented block
        if not line[0].isspace():
            break

        indent = len(line) - len(line.lstrip())
        content = stripped.lstrip()

        if content.startswith("- "):
            item_text = content[2:].strip()

            # Check if this list item has a key:value (mapping item)
            if ":" in item_text:
                # Start of a mapping item in a list
                mapping = {}
                k, _, v = item_text.partition(":")
                k = k.strip()
                v = v.strip()
                mapping[k] = _parse_scalar(v) if v else None
                i += 1

                # Collect subsequent indented key:value pairs for this item
                item_indent = indent + 2
                while i < len(lines):
                    sub = lines[i]
                    sub_stripped = sub.rstrip()

                    if not sub_stripped:
                        i += 1
                        continue

                    sub_indent = len(sub) - len(sub.lstrip())
                    if sub_indent < item_indent:
                        break

                    sub_content = sub_stripped.strip()
                    if ":" in sub_content and not sub_content.startswith("- "):
                        sk, _, sv = sub_content.partition(":")
                        sk = sk.strip()
                        sv = sv.strip()
                        mapping[sk] = _parse_scalar(sv) if sv else None
                    i += 1

                items.append(mapping)
            else:
                items.append(_parse_scalar(item_text))
                i += 1
        else:
            break

    return items, i


def _parse_mapping(lines, start):
    """Parse an indented YAML mapping block. Returns (dict, next_index)."""
    mapping = {}
    i = start
    base_indent = None

    while i < len(lines):
        line = lines[i]
        stripped = line.rstrip()

        if not stripped:
            i += 1
            continue

        if not line[0].isspace():
            break

        indent = len(line) - len(line.lstrip())
        if base_indent is None:
            base_indent = indent

        if indent < base_indent:
            break

        content = stripped.strip()
        if ":" in content:
            k, _, v = content.partition(":")
            k = k.strip()
            v = v.strip()
            mapping[k] = _parse_scalar(v) if v else None
        i += 1

    return mapping, i


def _parse_scalar(text):
    """Parse a scalar YAML value: number, inline list, or string."""
    if not text:
        return None

    # Inline list: [warn, critical]
    if text.startswith("[") and text.endswith("]"):
        inner = text[1:-1].strip()
        if not inner:
            return []
        return [_parse_scalar(item.strip()) for item in inner.split(",")]

    # Boolean
    if text.lower() in ("true", "yes"):
        return True
    if text.lower() in ("false", "no"):
        return False

    # Null
    if text.lower() in ("null", "~"):
        return None

    # Quoted string
    if (text.startswith('"') and text.endswith('"')) or \
       (text.startswith("'") and text.endswith("'")):
        return text[1:-1]

    # Number
    try:
        if "." in text:
            return float(text)
        return int(text)
    except ValueError:
        pass

    return text


def _default_config():
    """Return a single-account config pointing to ~/.claude."""
    return {
        "accounts": [
            {
                "name": "default",
                "path": str(Path.home() / ".claude"),
                "plan": "pro",
            }
        ],
        "thresholds": {
            "warn": 0.75,
            "critical": 0.95,
        },
        "webhooks": [],
    }


def load_config(path="accounts.yaml"):
    """Load multi-account config from a YAML file.

    If the file doesn't exist, returns a default single-account config
    pointing to ~/.claude.  Paths are expanded via Path.expanduser() and
    validated with warnings (never crashes).
    """
    config_path = Path(path)

    if not config_path.exists():
        return _default_config()

    text = config_path.read_text(encoding="utf-8")
    raw = _parse_yaml(text)

    # Normalise accounts
    accounts = raw.get("accounts", [])
    if not isinstance(accounts, list):
        accounts = []

    for acct in accounts:
        if not isinstance(acct, dict):
            continue
        # Expand ~ in path
        acct_path = acct.get("path", "")
        if acct_path:
            expanded = Path(str(acct_path)).expanduser()
            acct["path"] = str(expanded)
            if not expanded.exists():
                warnings.warn(
                    f"Account path does not exist: {expanded} "
                    f"(account: {acct.get('name', '?')})"
                )

    # Normalise thresholds
    thresholds = raw.get("thresholds", {})
    if not isinstance(thresholds, dict):
        thresholds = {}
    thresholds.setdefault("warn", 0.75)
    thresholds.setdefault("critical", 0.95)

    # Normalise webhooks
    webhooks = raw.get("webhooks", [])
    if not isinstance(webhooks, list):
        webhooks = []

    return {
        "accounts": accounts if accounts else _default_config()["accounts"],
        "thresholds": thresholds,
        "webhooks": webhooks,
    }
