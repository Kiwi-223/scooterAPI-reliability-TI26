# Modernisation & Reliability Plan

This plan covers getting the 2019 Scooter Reservation API running as described in
[README.md](README.md) and meeting the **Required outcomes (0–7)** in
[ACTIVITY.md](ACTIVITY.md). Dependencies and tooling move to current releases so the app
is ready to deploy later. **We are not deploying it as part of this plan.**

This is a living document. If the plan changes, update this file in the same PR that
changes it, and add a line to the [Change log](#change-log).

---

## How the work runs

| Rule | Detail |
|---|---|
| One problem per PR | Each PR fixes one focused problem. No direct commits to `master`. |
| Target repo | PRs open against **`Kiwi-223/scooterAPI-reliability-TI26:master`** (this fork), never the upstream `KaranErry/scooterAPI-reliability`. Always pass `--repo Kiwi-223/scooterAPI-reliability-TI26 --base master`. |
| Branch names | `stage-<N>/<short-slug>` (e.g. `stage-1/upgrade-deps`). |
| Writer agent | Implements the PR: code, tests and docs (`AGENTS.md`, this plan's status table), then pushes and opens the PR. |
| Reviewer agent | Acts as an adversary. Checks out the PR in a separate worktree, runs the tests, tries to break the change and checks it against ACTIVITY.md. Posts a review comment that ends in a verdict: `VERDICT: APPROVE` or `VERDICT: REQUEST_CHANGES`. |
| Merge | Only after `VERDICT: APPROVE` and green tests/CI. Squash-merge, then delete the branch. |
| Formal approval | GitHub does not let a PR author approve their own PR. Both agents act as the same account (`Kiwi-223`), so the reviewer's verdict goes in a PR comment and is not a formal "Approve" review. See [Human decisions](#human-decisions). |
| AI mistakes | When the reviewer catches the writer being confidently wrong, it gets logged for `AI_NOTES.md` (Outcome 7). |

**Toolchain chosen:** Python 3.14 (current stable on this machine), `venv` + `pip`
with pinned `requirements*.txt`, `pytest`, and a `Makefile` so every common task is one
command. No extra installer (like uv) is needed, which keeps setup simple for a cold agent.

---

## Stages and PRs

Status: ⬜ not started · 🟡 in progress / in review · ✅ merged · ⏸ blocked on human

### Stage 0: Plan
| # | PR | Outcome | Status |
|---|---|---|---|
| 0 | `docs`: add this PLAN.md | n/a | 🟡 |

### Stage 1: It runs on a current toolchain (Outcome 0)
| # | PR | Scope | Status |
|---|---|---|---|
| 1 | `build`: upgrade dependencies | Pin current Flask / Werkzeug / geopy / gunicorn. Drop unused pins (`tornado`, `tabulate`, `requests`, `six`, …). Add `.python-version` (3.14) and stop ignoring it in `.gitignore`. Add `requirements-dev.txt`. | ⬜ |
| 2 | `fix`: app runs on modern stack | Fix anything that breaks on Flask 3 / geopy 2. Read host/port/DB path from env with the README defaults (`localhost:8080`, `scooter_db.json`). Close file handles. Turn debug off. Manually check every README endpoint. | ⬜ |
| 3 | `test`: pytest harness + baseline tests | Fixture with an isolated temp DB (never touches the real `scooter_db.json`). Happy path plus existing validation errors for every README endpoint. `Makefile` with `setup`, `run`, `test`. | ⬜ |
| 4 | `ci`: GitHub Actions runs tests | Run `make test` on every PR and on push to `master`. | ⬜ |
| 5 | `docs`: AGENTS.md onboarding + README refresh | Zero-context guide: setup, run, test, where data lives, repo map. README: replace the dead Heroku URL and outdated local-setup steps. | ⬜ |

### Stage 2: Failures have names (Outcome 2)
| # | PR | Scope | Status |
|---|---|---|---|
| 6 | `feat`: stable failure kinds | A fixed enum of failure kinds with stable string ids (e.g. `missing_param`, `invalid_coordinate`, `scooter_not_found`, `scooter_already_reserved`, `no_active_reservation`, `payment_declined`, …). Every error response carries `"error": {"kind": ...}`. `msg` stays for README compatibility. Kinds are documented. Tests cover each kind. | ⬜ |

### Stage 3: Every request leaves a record (Outcome 1)
| # | PR | Scope | Status |
|---|---|---|---|
| 7 | `feat`: persisted request/event records | Each request gets a `request_id`, which is also returned as the `X-Request-ID` header. Records are append-only JSON Lines under `data/` and persist across restarts. Each record holds: what was asked (method, path, params), the decisions made (e.g. scooter found / already reserved / payment result), how it ended (status, failure kind, duration) and `scooter_id`. A small query module lets you follow one request or one scooter end-to-end and count failure kinds by time window. Tests included. | ⬜ |

### Stage 4: Payment can fail without corrupting state (Outcome 3)
| # | PR | Scope | Status |
|---|---|---|---|
| 8 | `fix`: atomic, locked DB writes | Write to a temp file, then `os.replace`, under a file lock, so a crash mid-write or two concurrent workers can't corrupt `scooter_db.json`. Tests included. | ⬜ |
| 9 | `feat`: simulated payment gateway with reproducible faults | Replace the always-succeeds stub with a fake gateway that can `succeed`, `decline`, `timeout` or `error`. Faults can be reproduced on demand through config/env or a request header, with a fixed seed for random mode. Each fault maps to a failure kind and is recorded. | ⬜ |
| 10 | `fix`: no bad state when payment fails | End-ride is all-or-nothing. A failed payment never frees a scooter without a charge and never charges for a ride that didn't end. Charges use an idempotency key per reservation, so a retry after a timeout is charged once. There is a way to reconcile or resolve a ride stuck in an unknown payment state. Tests prove each failure path leaves consistent state. | ⬜ |

### Stage 5: Agents can ask the system questions (Outcome 4)
| # | PR | Scope | Status |
|---|---|---|---|
| 11 | `feat`: MCP server | An MCP server (official `mcp` Python SDK) over the recorded data and state. Tools: `recent_failures`, `failure_counts` (by kind, time window), `scooter_history`, `request_trace`, `scooter_state`, `run_tests`. Registered in a checked-in `.mcp.json` and documented in AGENTS.md. Tests included. | ⬜ |

### Stage 6: The agent has boundaries (Outcome 6)
| # | PR | Scope | Status |
|---|---|---|---|
| 12 | `chore`: agent permission config | Checked-in `.claude/settings.json`. Allow: `make`/pytest/read-only git. Ask: `git push`, `gh pr` writes. Deny: force-push, pushes to `master`, `rm -rf`, reading `.env`/secrets, editing `.claude/` itself, and network calls except localhost. An AGENTS.md "Agent boundaries" section covers blast radius, permissions, isolation (branches/worktrees), reversibility and budget. | ⬜ |

### Stage 7: What the AI got wrong (Outcome 7)
| # | PR | Scope | Status |
|---|---|---|---|
| 13 | `docs`: AI_NOTES.md | Short, honest entries taken from real reviewer catches during this plan. Each says what was wrong, how it was caught and what changed. | ⬜ |

### Stage 8: Ready to deploy later (not deployed)
| # | PR | Scope | Status |
|---|---|---|---|
| 14 | `chore`: deploy readiness | Procfile binds gunicorn to `$PORT`. Add a health endpoint. Update `app.json`. README "Deploying" notes, including the limits of a JSON-file DB on an ephemeral filesystem. **No deploy.** | ⬜ |

### Stage 9: Verification (no PR unless gaps are found)
| # | Step | Status |
|---|---|---|
| 15 | Cold-agent rehearsal: plant a bug on a throwaway local branch (never pushed), give a fresh agent only a one-line symptom, and check that it finds the root cause with evidence from recorded data and writes a failing test. Gaps found become new PRs here. | ⬜ |

---

## Human decisions

These need a person, and the defaults below are what the agents will do unless told
otherwise.

| # | Decision | Default the agents will use |
|---|---|---|
| H1 | **Formal PR approval.** One GitHub account can't approve its own PRs. | The reviewer posts a verdict comment. The orchestrator merges on `VERDICT: APPROVE`. For real approvals, add a second account or a GitHub App reviewer. |
| H2 | **Branch protection on `master`** (require a PR and passing CI). This is a repo setting. | Not changed by agents. Recommended that you turn it on after PR 4 (CI) lands. |
| H3 | **Payment-decline policy.** When a card is declined at end-of-ride, does the ride stay open (scooter stays reserved; the rider retries with a different payment) or end with an outstanding balance? | The ride stays open and nothing is charged, so it can be retried. A ride in an *unknown* state (timeout) is reconciled through the idempotency key, so no scooter is stuck forever. |
| H4 | **State-changing `GET` endpoints.** `/reservation/start` and `/reservation/end` are GETs in the README. | Keep them as GET to match the README contract, and note it. Moving to POST would be a breaking change. |
| H5 | **Deploy target & persistence.** Heroku's filesystem is ephemeral, so the JSON DB and event log would reset on every dyno restart. | Not solved now (no deploy). Documented in PR 14. |
| H6 | **Event-log contents.** Records include ride coordinates (location data). | Stored locally and git-ignored, with no retention policy yet. |

---

## Change log
- 2026-11-04: Initial plan.
