"""The committed .claude/settings.json is schema-valid, and the checker catches planted mistakes.

`claude doctor` stays silent on unknown keys and many malformed rules (AI_NOTES #11), so
scripts/check_settings.py is the real check; these tests prove it actually fails.
"""
import copy
import importlib.util
import json
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("check_settings", REPO / "scripts" / "check_settings.py")
check_settings = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(check_settings)

SETTINGS = json.loads((REPO / ".claude" / "settings.json").read_text())


def problems(data, tmp_path):
    path = tmp_path / "settings.json"
    path.write_text(json.dumps(data))
    from jsonschema import Draft7Validator
    return check_settings.check(path, Draft7Validator(check_settings.strict_schema()))


def test_committed_settings_are_valid(tmp_path):
    assert problems(SETTINGS, tmp_path) == []


def test_main_exit_codes(tmp_path, capsys):
    assert check_settings.main([]) == 0
    bad = tmp_path / "bad.json"
    bad.write_text('{"sandbox": {"enabeld": true}}')
    assert check_settings.main([str(bad)]) == 1
    assert "enabeld" in capsys.readouterr().err


def _mutate(path, value):
    data = copy.deepcopy(SETTINGS)
    node = data
    for key in path[:-1]:
        node = node[key]
    if path[-1] is None:          # append to a list
        node.append(value)
    else:
        node[path[-1]] = value
    return data


@pytest.mark.parametrize("path,value", [
    (["permisions"], {}),                                  # misspelled top-level key
    (["sandbox", "enabeld"], True),                        # misspelled sandbox key
    (["sandbox", "network", "alowedDomains"], ["x.org"]),  # misspelled network key
    (["sandbox", "filesystem"], {"denyReads": ["~/x"]}),   # misspelled filesystem key
    (["permissions", "allowed"], []),                      # misspelled permissions key
    (["sandbox", "enabled"], "yes"),                       # wrong type
    (["permissions", "disableBypassPermissionsMode"], "true"),  # not in the enum
    (["permissions", "deny", None], "Bsh(rm -rf *)"),      # unknown tool in a rule
    (["permissions", "ask", None], "Bash(git push *"),     # unbalanced parenthesis
])
def test_planted_mistakes_are_rejected(tmp_path, path, value):
    assert problems(_mutate(path, value), tmp_path), f"{path}={value!r} was not rejected"


# ---- MCP entries against .mcp.json (PLAN PR 16) ------------------------------------------------

MCP = json.loads((REPO / ".mcp.json").read_text())


def test_committed_mcp_entries_match_mcp_json():
    assert check_settings.check_mcp(SETTINGS, MCP) == []


@pytest.mark.parametrize("path,value,says", [
    (["enabledMcpjsonServers", None], "scooterr", "isn't defined in .mcp.json"),
    (["permissions", "allow", None], "mcp__scoter__recent_failures", "names server 'scoter'"),
    (["permissions", "ask", None], "mcp__other__run_tests", "names server 'other'"),
])
def test_planted_mcp_mistakes_are_rejected(path, value, says):
    found = check_settings.check_mcp(_mutate(path, value), MCP)
    assert len(found) == 1 and says in found[0]


def test_a_missing_server_command_is_rejected():
    broken = {"mcpServers": {"scooter": dict(MCP["mcpServers"]["scooter"], command=".venv/bin/pythonn")}}
    assert "doesn't exist" in check_settings.check_mcp(SETTINGS, broken)[0]
