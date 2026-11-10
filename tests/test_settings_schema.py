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


# ---- policy invariants (AGENTS.md "Agent boundaries" / PLAN.md H10) --------------------
# These pin the escalation policy so a loosening edit to .claude/settings.json fails CI.
PERM = SETTINGS["permissions"]


@pytest.mark.parametrize("rule", [
    "Bash(git push --force *)", "Bash(git push -f *)", "Bash(git push * --force)",
    "Bash(git push * --force-with-lease*)", "Bash(git push * +*)",
    "Bash(git push * master)", "Bash(git push *:master)", "Bash(git -C * push*)",
    "Bash(gh pr merge * --admin*)", "Bash(gh pr merge --admin*)",
    "Bash(gh repo delete *)", "Bash(gh release delete *)",
    "Bash(gh api *protection*)", "Bash(gh api *rulesets*)", "Bash(gh auth *)",
    "Bash(rm -rf *)", "Read(.env)", "Read(~/.ssh/**)", "Read(~/.config/gh/**)",
])
def test_required_denies_present(rule):
    assert rule in PERM["deny"]


@pytest.mark.parametrize("rule", [
    "Edit(/.claude/**)", "Edit(/.github/**)", "Bash(dangerouslyDisableSandbox:true)",
    "Bash(git reset --hard*)", "Bash(git clean *)", "Bash(git branch -D *)",
])
def test_guardrail_changes_still_ask(rule):
    assert rule in PERM["ask"]
    assert rule not in PERM["allow"]


def test_sandbox_and_bypass_settings():
    assert SETTINGS["sandbox"]["enabled"] is True
    assert SETTINGS["sandbox"]["failIfUnavailable"] is True
    assert PERM["disableBypassPermissionsMode"] == "disable"
    assert not any(r.startswith("Edit(/.claude") or r.startswith("Edit(/.github") for r in PERM["allow"])
