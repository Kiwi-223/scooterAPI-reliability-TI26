"""Behavioural tests for the permission policy in .claude/settings.json.

Instead of checking that particular rule *strings* exist, these tests run example commands
through a small matcher that implements Claude Code's documented Bash-rule semantics, and
check the outcome (deny / ask / allow / default). A rule edit that lets a dangerous
command through, or that blocks the normal workflow, fails here (AI_NOTES #14, #15).

Documented semantics implemented (https://code.claude.com/docs/en/permissions):
  - `*` matches any text, including spaces; a rule without `*` is an exact match
  - a trailing ` *` (when it is the only wildcard) also matches the bare command
  - a trailing `:*` is the same as ` *`
  - deny beats ask beats allow; the first match in that order decides
  - compound commands are split on && || ; | & and newlines: deny/ask apply if any part
    matches, allow needs every part allowed
  - `sandbox.excludedCommands` uses the same pattern syntax and needs every part to match
Not modelled (so not relied on here): wrapper stripping (`timeout`, `nice`, ...), the
built-in read-only command set, quote-aware splitting. "default" means no rule matched:
the session's permission mode decides, and sandboxed commands are auto-approved.
"""
import json
import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SETTINGS = json.loads((REPO / ".claude" / "settings.json").read_text())
PERM = SETTINGS["permissions"]
SANDBOX = SETTINGS["sandbox"]
R = "Kiwi-223/scooterAPI-reliability-TI26"
O = f"repos/{R}"


def _pattern_matches(body, command):
    if body.endswith(":*"):
        body = body[:-2] + " *"
    regex = "^" + ".*".join(re.escape(part) for part in body.split("*")) + "$"
    if re.match(regex, command, re.S):
        return True
    return body.endswith(" *") and body.count("*") == 1 and command == body[:-2]


def _bash_rules(rules):
    return [r[len("Bash("):-1] for r in rules if r.startswith("Bash(") and r.endswith(")")]


def _parts(command):
    return [p.strip() for p in re.split(r"\|&|&&|\|\||[;|&\n]", command) if p.strip()]


def decide(command):
    parts = _parts(command)
    for kind in ("deny", "ask"):
        if any(_pattern_matches(b, p) for b in _bash_rules(PERM[kind]) for p in parts):
            return kind
    if all(any(_pattern_matches(b, p) for b in _bash_rules(PERM["allow"])) for p in parts):
        return "allow"
    return "default"


def unsandboxed(command):
    return all(any(_pattern_matches(e, p) for e in SANDBOX["excludedCommands"]) for p in _parts(command))


# ---- the matcher itself --------------------------------------------------------------

@pytest.mark.parametrize("rule,command,expected", [
    ("ls *", "ls", True), ("ls *", "ls -la", True), ("ls *", "lsof", False),
    ("ls*", "lsof", True), ("npm run build", "npm run build --watch", False),
    ("git * main", "git -c x=y diff main", True), ("ls:*", "ls -l", True),
    ("* --help *", "npm --help", False),
])
def test_matcher_follows_documented_examples(rule, command, expected):
    assert _pattern_matches(rule, command) is expected


# ---- the policy ----------------------------------------------------------------------

WORKFLOW_ALLOWED = [
    "git push -u origin stage-7/records",
    "git push origin stage-7/records",
    "git push -q -u origin stage-7/records",
    "git push -u origin stage-8/fix-delete-flow",          # '-f'/'delete' inside a name is fine
    f"gh pr create --repo {R} --base master --title t --body-file b.md",
    f"gh pr comment 7 --repo {R} --body-file x.md",
    f"gh pr edit 7 --repo {R} --body-file x.md",
    f"gh pr review 7 --repo {R} --comment --body ok",
    f"gh pr merge 7 --squash --delete-branch --repo {R}",
    "gh pr merge --squash 7",
    f"gh pr view 7 --repo {R} --json comments",
    f"gh pr checks 7 --repo {R}",
    f"gh run view 123 --log-failed --repo {R}",
    f"gh api {O}/issues/7/comments --jq .[].id",
    "gh api repos/actions/checkout/git/ref/tags/v7.0.1",
    f"gh api {O}/branches/master --jq .protected",         # the way to check H2
    "make test", "make test ARGS=-q", "make setup", "make check-settings", "make clean",
    "git fetch origin", "git status --short", "git log --oneline -3",
]

DENIED = [
    # git push: mass / deleting / forced updates, in plain, reordered, abbreviated and quoted spellings
    "git push --mirror origin", "git push origin --mirror", "git push --mirr origin",
    "git push --all origin", "git push origin --all", "git push --branches origin",
    "git push --prune origin", "git push --prune origin refs/heads/*:refs/heads/*",
    "git push origin --delete stage-6/x", "git push --del origin x", "git push origin -d stage-6/x",
    "git push -d origin stage-6/x", "git push -ud origin x",
    "git push origin :stage-6/x", "git push origin ':stage-6/x'",
    "git push --force origin stage-7/x", "git push --forc origin stage-7/x",
    "git push --force-with-lease origin stage-7/x", "git push origin stage-7/x --force-if-includes",
    "git push -f origin stage-7/x", "git push origin stage-7/x -f", "git push -u origin stage-7/x -uf",
    "git push -fu origin stage-7/x", "git push -qf origin x",
    "git push origin +stage-7/x", "git push origin '+stage-7/x'", 'git push origin "+stage-7/x"',
    "git push origin master", "git push -u origin master", "git push origin HEAD:master",
    "git push origin 'HEAD:master'", 'git push origin "HEAD:master"', "git push origin HEAD:refs/heads/master",
    "git push origin 'refs/heads/*'", "git push origin HEAD:refs/heads/stage-7/x",
    "git push --receive-pack='sh -c id' origin x", "git push --exec=id origin x",
    "git -C . push origin HEAD:master", "git -C /elsewhere push origin stage-7/x",
    # gh: admin merges, deletes, branch protection, token
    "gh pr merge 7 --squash --admin", "gh pr merge --admin 7",
    f"gh repo delete {R} --yes", "gh release delete v1",
    f"gh api --method=DELETE {O}/git/refs/heads/stage-7/x", f"gh api -X DELETE {O}/git/refs/heads/x",
    f"gh api -XDELETE {O}/git/refs/heads/x", f"gh api --method DELETE {O}/git/refs/heads/x",
    f"gh api {O}/branches/master/protection", f"gh api -X PUT {O}/branches/master/protection --input p.json",
    f"gh api {O}/rulesets", "gh auth token", "gh auth status",
    # local destruction
    "rm -rf .venv", "rm -fr build",
]

ASKED = [
    # gh api writes (H10 covers push, PR creation, merge and comment, not arbitrary API writes)
    f"gh api -X PATCH {O}/git/refs/heads/stage-7/x -f sha=abc -F force=true",
    f"gh api {O}/merges -f base=master -f head=stage-7/x",      # -f alone makes it a POST
    f"gh api -X PUT {O}/contents/app.py -f message=m -f content=Zm9v",
    f"gh api -X PUT {O}/pulls/7/merge -f merge_method=merge",
    f"gh api -X PATCH {O} -f visibility=public",
    f"gh api --method=POST {O}/issues",
    f"gh api --method POST {O}/issues",
    f"gh api {O}/pulls --input body.json",
    f"gh api {O}/issues --field title=x",
    f"gh api {O}/issues --raw-field title=x",
    "gh api graphql -f query='{viewer{login}}'",
    # guardrails and destructive local git
    "git reset --hard HEAD~1", "git clean -fdx", "git branch -D stage-7/x",
    "git -c core.hooksPath=/tmp/x commit -m m",
]

DEFAULT = [
    # no rule: sandboxed and auto-approved by the sandbox, or decided by the permission mode
    "git -C /main fetch origin", "git -C /main worktree add /tmp/x origin/y",
    "git -C . -c core.fsmonitor='sh -c id' status",          # sandboxed; no allow rule any more
    "gh pr merge 7 --merge", "gh pr merge 7 --rebase",       # only squash merges are pre-approved
    "git push", "git push origin HEAD",                       # unknown push shapes: not pre-approved
]


@pytest.mark.parametrize("command", WORKFLOW_ALLOWED)
def test_normal_workflow_runs_without_a_prompt(command):
    assert decide(command) == "allow"


@pytest.mark.parametrize("command", DENIED)
def test_dangerous_commands_are_denied(command):
    assert decide(command) == "deny"


@pytest.mark.parametrize("command", ASKED)
def test_writes_and_guardrail_changes_ask(command):
    assert decide(command) == "ask"


@pytest.mark.parametrize("command", DEFAULT)
def test_unlisted_commands_fall_back_to_the_mode(command):
    assert decide(command) == "default"


# Short-flag clusters (git accepts `-fqu` for `-f -q -u`). PLAN PR 6b denies every cluster
# in which `f` (force) or `d` (delete) appears within the first three letters, combined with
# the harmless flags u/q/v/n, in front of or after the allowed `origin stage-*` shape.
_SAFE, _BAD = "uqvn", "fd"
_CLUSTERS = sorted(
    {b for b in _BAD}
    | {x + b for x in _SAFE + _BAD for b in _BAD}
    | {b + x for x in _SAFE + _BAD for b in _BAD}
    | {x + y + z for x in _SAFE + _BAD for y in _SAFE + _BAD for z in _SAFE + _BAD
       if _BAD[0] in x + y + z or _BAD[1] in x + y + z}
)


@pytest.mark.parametrize("cluster", _CLUSTERS)
def test_force_or_delete_short_flag_clusters_are_denied(cluster):
    assert decide(f"git push origin stage-7/x -{cluster}") == "deny"
    assert decide(f"git push -u origin stage-7/x -{cluster}") == "deny"
    assert decide(f"git push -{cluster} origin stage-7/x") == "deny"


def test_known_limitation_force_in_fourth_place_of_a_cluster_is_not_caught():
    # Documented gap (AGENTS.md): f/d first appearing at position 4+ of a cluster, e.g.
    # `-uqvf`, still matches the allowed push shape. Branch protection (H2) + review is
    # the real control. If a rule change closes this, flip this test.
    assert decide("git push origin stage-7/x -uqvf") == "allow"
    assert decide("git push origin stage-7/x -uqv") == "allow"   # harmless cluster stays allowed
    assert decide("git push origin stage-7/x -4uf") == "allow"   # -4/-6 mixed in: also a gap
    # Short-flag rules match raw text, so any quoting/escaping of the flag, or a tab before
    # it, defeats them (AGENTS.md; AI_NOTES #17). git still sees `-f` in every case.
    assert decide("git push origin stage-7/x '-f'") == "allow"
    assert decide('git push origin stage-7/x "-uf"') == "allow"
    assert decide('git push origin stage-7/x -"f"') == "allow"
    assert decide("git push origin stage-7/x -\\f") == "allow"
    assert decide("git push origin stage-7/x\t-f") == "allow"
    # ...but longer clusters with f/d in the first three letters are caught (prefix rules):
    assert decide("git push origin stage-7/x -fquv") == "deny"
    assert decide("git push origin stage-7/x -uqdv") == "deny"


def test_known_limitation_branch_names_containing_master_are_denied():
    # Documented trade-off of the `git push*master*` deny (AGENTS.md).
    assert decide("git push -u origin stage-9/remaster-db") == "deny"


@pytest.mark.parametrize("command,expected", [
    ("gh api repos/x/y", True), ("git push -u origin stage-7/x", True),
    ("git -C . push origin x", False), ("make test", False), ("cd .. && gh api x", False),
])
def test_only_gh_and_git_push_leave_the_sandbox(command, expected):
    assert unsandboxed(command) is expected


# ---- pinned values: widening any of these must be a deliberate, reviewed change --------

def test_excluded_commands_pinned():
    assert SANDBOX["excludedCommands"] == ["gh *", "git push *"]


def test_allowed_domains_pinned():
    assert SANDBOX["network"]["allowedDomains"] == [
        "pypi.org", "files.pythonhosted.org", "github.com", "api.github.com", "localhost",
        "code.claude.com", "docs.github.com", "endoflife.date", "raw.githubusercontent.com",
        "json.schemastore.org",
    ]
    assert not any("*" in d for d in SANDBOX["network"]["allowedDomains"])


def test_sandbox_fail_closed_and_no_bypass_mode():
    assert SANDBOX["enabled"] is True and SANDBOX["failIfUnavailable"] is True
    assert SANDBOX["autoAllowBashIfSandboxed"] is True
    assert PERM["disableBypassPermissionsMode"] == "disable"
    assert "allowUnsandboxedCommands" not in SANDBOX     # default true, gated by the ask rule below


@pytest.mark.parametrize("rule", [
    "Edit(/.claude/**)", "Edit(/.github/**)", "Bash(dangerouslyDisableSandbox:true)",
])
def test_guardrail_edits_and_unsandboxed_retries_ask(rule):
    assert rule in PERM["ask"]
    assert rule not in PERM["allow"]
    assert not any(r.startswith(("Edit(/.claude", "Edit(/.github")) for r in PERM["allow"])


@pytest.mark.parametrize("rule", [
    "Read(.env)", "Read(.env.*)", "Read(~/.ssh/**)", "Read(~/.aws/**)", "Read(~/.config/gh/**)",
    "Read(~/.git-credentials)", "Read(~/.netrc)",
])
def test_secret_reads_denied(rule):
    assert rule in PERM["deny"]


def test_no_rule_ends_in_colon_star():
    # A trailing `:*` is documented as an alias for ` *`, so `Bash(git push* :*)` would NOT
    # match a literal ':'. Rules that need a literal colon end in `:**` instead.
    offenders = [r for k in ("allow", "ask", "deny") for r in PERM[k] if r.endswith(":*)")]
    assert offenders == []


# ---- MCP tools (PLAN PR 16): read-only tools allowed, action tools ask --------------------------

def _mcp_rules(kind):
    return {r for r in PERM[kind] if r.startswith("mcp__")}


def test_mcp_server_is_enabled_for_fresh_sessions():
    import mcp_server
    assert SETTINGS["enabledMcpjsonServers"] == [mcp_server.NAME]
    assert "enableAllProjectMcpServers" not in SETTINGS          # only this server, by name


def test_every_read_only_mcp_tool_is_allowed_and_every_action_tool_asks():
    import mcp_server
    prefix = f"mcp__{mcp_server.NAME}__"
    assert _mcp_rules("allow") == {prefix + t for t in mcp_server.READ_ONLY_TOOLS}
    assert _mcp_rules("ask") == {prefix + t for t in mcp_server.ACTION_TOOLS}
    assert _mcp_rules("deny") == set()
    # no server-wide rule (mcp__scooter) that would allow the action tools too
    assert not any(r in (f"mcp__{mcp_server.NAME}", f"mcp__{mcp_server.NAME}__*") for k in ("allow", "ask", "deny")
                   for r in PERM[k])


def test_the_mcp_rules_name_tools_the_server_really_has():
    import anyio
    from mcp import Client

    import mcp_server

    async def names():
        async with Client(mcp_server.server) as client:
            return {t.name for t in (await client.list_tools()).tools}
    listed = anyio.run(names)
    named = {r.split("__", 2)[2] for k in ("allow", "ask") for r in _mcp_rules(k)}
    assert named == listed
