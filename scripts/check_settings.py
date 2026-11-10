"""Validate Claude Code settings files against the vendored SchemaStore schema.

    make check-settings                 # checks .claude/settings.json
    .venv/bin/python scripts/check_settings.py path/to/settings.json ...

Why this exists: `claude doctor` only reports some problems (it flags malformed JSON and
wrong value types, but stays silent on unknown or misspelled keys, and on many malformed
permission rules). This check fails on:
  - any key the schema doesn't define, including at the top level (the published schema
    allows unknown top-level keys; this script makes the root strict, while `permissions`,
    `sandbox`, `sandbox.network` and `sandbox.filesystem` are already strict in the schema)
  - wrong value types and enum values
  - permission rules that don't match the schema's rule pattern (unknown tool name,
    unbalanced parentheses)
  - MCP entries that don't match the checked-in `.mcp.json` (PLAN PR 16): an
    `enabledMcpjsonServers` name or an `mcp__<server>__...` rule for a server `.mcp.json`
    doesn't define, or a stdio server whose command doesn't exist. (Which *tools* exist is
    checked by tests/test_settings_policy.py, which imports the server.)

Schema: schemas/claude-code-settings.schema.json, vendored so CI needs no network.
  Source:  https://json.schemastore.org/claude-code-settings.json
  Fetched: 2026-11-04 (sha256 f26f028ad94a1e2a28e9dad1f80832ab196a8e080bd3a7d7a32e6428a15e0611)
SchemaStore can lag behind new CLI releases. If a key that the Claude Code docs document is
rejected, re-fetch the schema (same URL), update the date and hash above, and re-run.
"""
import copy
import json
import sys
from pathlib import Path

from jsonschema import Draft7Validator

REPO = Path(__file__).resolve().parent.parent
SCHEMA_PATH = REPO / "schemas" / "claude-code-settings.schema.json"
DEFAULT_TARGETS = [REPO / ".claude" / "settings.json"]
MCP_JSON = REPO / ".mcp.json"


def strict_schema():
    schema = json.loads(SCHEMA_PATH.read_text())
    strict = copy.deepcopy(schema)
    strict["additionalProperties"] = False  # unknown / misspelled top-level keys fail
    return strict


def check(path, validator):
    try:
        data = json.loads(Path(path).read_text())
    except (OSError, json.JSONDecodeError) as exc:
        return [f"{path}: cannot read JSON: {exc}"]
    errors = sorted(validator.iter_errors(data), key=lambda e: list(e.absolute_path))
    return [f"{path}: {'.'.join(str(p) for p in e.absolute_path) or '<root>'}: {e.message}"
            for e in errors]


def check_mcp(settings, mcp_config, base=REPO):
    """Settings' MCP entries against a parsed .mcp.json -> list of problems."""
    servers = (mcp_config or {}).get("mcpServers") or {}
    problems = []
    for name in settings.get("enabledMcpjsonServers") or []:
        if name not in servers:
            problems.append(f"enabledMcpjsonServers: {name!r} isn't defined in .mcp.json")
    perms = settings.get("permissions") or {}
    for kind in ("allow", "ask", "deny"):
        for rule in perms.get(kind) or []:
            if rule.startswith("mcp__"):
                server = rule[len("mcp__"):].split("__", 1)[0]
                if server not in servers:
                    problems.append(f"permissions.{kind}: {rule!r} names server {server!r}, which .mcp.json doesn't define")
    for name, server in servers.items():
        command = server.get("command")
        if server.get("type", "stdio") == "stdio" and command and "/" in command and not (base / command).exists():
            problems.append(f".mcp.json: server {name!r} runs {command!r}, which doesn't exist (run make setup)")
    return problems


def main(argv):
    targets = argv or DEFAULT_TARGETS
    validator = Draft7Validator(strict_schema())
    problems = [msg for t in targets for msg in check(t, validator)]
    if not argv:              # the repo's own settings: also cross-check .mcp.json
        try:
            mcp_config = json.loads(MCP_JSON.read_text())
            settings = json.loads(DEFAULT_TARGETS[0].read_text())
        except (OSError, json.JSONDecodeError) as exc:
            problems.append(f"cannot read .mcp.json or settings for the MCP check: {exc}")
        else:
            problems += [f"{DEFAULT_TARGETS[0]}: {p}" for p in check_mcp(settings, mcp_config)]
    for msg in problems:
        print(msg, file=sys.stderr)
    if problems:
        print(f"check-settings: {len(problems)} problem(s)", file=sys.stderr)
        return 1
    print(f"check-settings: OK ({', '.join(str(t) for t in targets)})")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
