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
| Branch names | `stage-<N>/<short-slug>` (e.g. `stage-1/upgrade-deps`). PR 0 used `docs/plan` before this rule existed. |
| Writer agent | Implements the PR: code, tests and docs, then pushes and opens the PR. Every PR marks its own row ✅ in this file, because it only lands once merged. From PR 2 on, every PR keeps `AGENTS.md` accurate. |
| Reviewer agent | Acts as an adversary. Checks out the PR in a separate worktree, runs the tests, tries to break the change (including mutating code to see whether the tests catch it) and checks it against ACTIVITY.md. Posts a PR comment that ends in `VERDICT: APPROVE` or `VERDICT: REQUEST_CHANGES`. |
| Merge | Only after `VERDICT: APPROVE` and green tests/CI. Squash-merge, then delete the branch. |
| Formal approval | GitHub does not let a PR author approve their own PR. Both agents act as the same account (`Kiwi-223`), so the verdict goes in a comment and is not a formal "Approve" review. See H1. |
| AI mistakes | Every time the reviewer catches the writer, or the plan, being confidently wrong, it gets logged in `AI_NOTES.md` (Outcome 7) in that PR or the next one. |

**Toolchain:** Python **3.15** (current stable, 3.15.2, released 2026-10-09), `venv` +
`pip`. `requirements.in` lists the top-level dependencies, and the fully resolved
`requirements.txt` lock pins every transitive dependency. The same split applies to the dev
deps. The lock is generated with `pip-compile` (pip-tools). `pytest`, plus a `Makefile` so every common task is one command
(`make setup | run | test | lock | …`). No extra installer is required, which keeps setup
simple for a cold agent.

**Storage:** keep the JSON file DB (the README's stated stack). A file lock covers each
whole read, check and write, so concurrent workers stay consistent. Request records are
append-only JSON Lines (see H8).

---

## Stages and PRs

Status: ⬜ not started · 🟡 in progress / in review · ✅ merged · ⏸ blocked on human

### Stage 0: Plan
| # | PR | Status |
|---|---|---|
| 0 | `docs`: add this PLAN.md | ✅ |

### Stage 1: It runs, is testable and has guardrails (Outcomes 0, 5, 6)
| # | PR | Scope | Status |
|---|---|---|---|
| 1 | `build`: Python 3.15 + current dependencies, fully locked | Pin the latest stable Flask / Werkzeug / geopy / gunicorn. Drop unused pins (`tornado`, `tabulate`, `requests`, `six`, …). `requirements.in` → locked `requirements.txt`, plus a dev equivalent. `.python-version` = 3.15, no longer ignored in `.gitignore`. `Makefile` with `setup`, `run` and `lock`. Verify every README endpoint on the new stack. | ✅ |
| 2 | `docs`: agent onboarding skeleton + README refresh | `AGENTS.md` for a zero-context reader covering setup, run, repo map and where data lives. `CLAUDE.md` that imports `@AGENTS.md`. An `AI_NOTES.md` skeleton seeded with the plan-review catches. README: remove the dead Heroku URL and update the local-setup steps. | ✅ |
| 3 | `refactor`: configurable DB path / host / port | Read them from env (`SCOOTER_DB_PATH`, `HOST`, `PORT`) with the README defaults (`scooter_db.json`, `localhost:8080`). Close file handles. This is a prerequisite for isolated tests. | ✅ |
| 4 | `test`: pytest harness + baseline tests | Fixture with isolated temp data (DB and, later, records). It never touches the real files. Happy path plus the existing validation errors for every README endpoint. `make test` is the single command. | ✅ |
| 5 | `ci`: GitHub Actions runs `make test` | On every PR and on push to `master`, on Python 3.15. | ✅ |
| 6 | `chore`: agent boundaries (Outcome 6) | Checked-in `.claude/settings.json`: harness **sandbox** turned on (OS-level filesystem and network limits, so a pattern deny can't be bypassed with `python -c`). Permissions: allow, ask and deny lists. Edits to `.claude/**` and `.github/**` need approval. Per H10, feature-branch pushes, PR writes, squash merges and `gh api` reads are allowed without a prompt. `gh api` writes, edits to `.claude/**` and `.github/**`, unsandboxed retries and destructive local git need approval. Force-push, pushes to `master`, branch-protection API calls and deletes are denied. The sandbox network allowlist must include `pypi.org`, `files.pythonhosted.org`, `github.com` and `api.github.com`, or the agent locks itself out of its own workflow. Branch protection is the real control on `master` (H2). A committed `make agent` wrapper always starts headless runs with `--max-budget-usd` and a wall-clock timeout. PR 6 checks whether a turn cap is actually supported by the installed CLI before using one. AGENTS.md "Agent boundaries" section covers blast radius, permissions, isolation (branch/worktree), reversibility (squash PRs, revert) and budget (concrete caps such as headless `--max-turns` / `--max-budget-usd`, checked against current Claude Code docs). | ✅ |
| 6b | `fix`: deny force/delete in 3-letter short-flag clusters | Follow-up to PR 6 (reviewer MINOR). git accepts clustered short flags, so `git push origin stage-x -fqu` matched the allowed `origin stage-*` shape and would force-push another agent's PR branch with no prompt. Deny every cluster in which `f` or `d` appears within the first three letters, combined with `u`/`q`/`v`/`n`. The rules are generated by position, as prefix rules. Behaviour-table rows cover every such cluster in three positions, and a pinned test documents the remaining gap (f/d at the 4th letter or later). | ✅ |

### Stage 2: Failures have names (Outcome 2)
| # | PR | Scope | Status |
|---|---|---|---|
| 7 | `feat`: stable failure kinds + JSON error envelope | A fixed registry of kinds with stable string ids (`missing_param`, `invalid_number`, `invalid_coordinate`, `scooter_not_found`, `scooter_already_reserved`, `no_active_reservation`, `route_not_found`, `method_not_allowed`, `storage_error`, `internal_error`, …). **Every** error, including Flask's own 404/405/500 pages, returns JSON with `msg` (kept for README compatibility) and `"error": {"kind": ...}`. A test enumerates the registry and exercises every kind. Kinds are documented in AGENTS.md. | ✅ |
| 8 | `fix`: validate `radius` | Reject a negative or non-finite (`inf`, `nan`) radius with a named kind (`invalid_radius`). A missing radius stays `missing_param`. | ✅ |

### Stage 3: Every request leaves a record (Outcome 1)
| # | PR | Scope | Status |
|---|---|---|---|
| 9 | `feat`: persisted request records | Every request gets a `request_id`, returned as the `X-Request-ID` header. Each request appends one JSON line to `data/records.jsonl` (configurable path, git-ignored, survives restarts). A record holds: UTC ISO-8601 timestamp, git commit plus a `git_dirty` flag (a planted bug may be uncommitted), method, path, params, `scooter_id`, the decisions made, scooter state **before and after**, the DB write result, the payment result once Stage 4 lands, status, failure kind and duration. Each record is written atomically as a single line, so lines from several workers never interleave. Tests isolate the log. Log rotation is noted as future work. | ✅ |
| 10 | `feat`: records query CLI | `python -m …records` / `make records ARGS=…` with recent failures, failure counts by kind since a time, scooter history and request trace. This is the fallback that works without MCP approval, and the MCP server in Stage 5 reuses the same module. | ✅ |

### Stage 4: State stays consistent and payment can fail safely (Outcome 3)
| # | PR | Scope | Status |
|---|---|---|---|
| 11 | `fix`: transactional DB access | A file lock covers each whole read, check and write. The write goes to a temp file, then `os.replace`. A concurrency test with two simultaneous reservations checks that exactly one wins. | ✅ |
| 11b | `fix`: PR 11 review follow-ups | A failed directory fsync after a successful `os.replace` is a successful write: log a WARNING, record `db_write.ok=true` plus `dir_fsync_error`. Make the timing-sensitive tests robust: a generous lock-timeout bound, and an event-based start for the process races. Document the gunicorn `--timeout` requirement and the changed error precedence on `/reservation/end`. | ✅ |
| 11c | `test`: fix flaky lock-order assertion | `test_concurrent_cycles_across_processes` sorted records by `ts` (request start) and called it lock order. Under contention a later-started request can get the lock first, so the "last write = final DB" assertion flaked on CI (PR #18). Record `acquired_at` in the `db_lock` decision, order by it in the test and in `records_query`'s `scooter` history, and add a deterministic test showing that start order and lock order differ. | ✅ |
| 12 | `feat`: persisted reservation state | Each scooter carries an active reservation (`reservation_id`, `started_at`, `payment_state`, idempotency key) saved with the scooter. Existing `scooter_db.json` files keep loading. Also fix three DB-validation gaps from the PR 7 review (#9), since this PR rewrites that code: (a) range-check `lat`/`lng` read from the DB. `lat: 1000` currently crashes in geopy on `/search` as `internal_error` and should be `storage_error`. (b) Extra fields are tolerated on read but silently dropped on write; a round-trip probe (add a field, reserve, field gone) proves it. Preserve them, or reject them. (c) Detect duplicate scooter ids. | ✅ |
| 13 | `feat`: simulated payment gateway with reproducible faults | Fake gateway outcomes: `success`, `decline`, `timeout_not_charged`, `timeout_charged` and `error`. The gateway's ledger is **persisted to a file** under the data dir (shared across workers and restarts) and keyed by idempotency key, so a charge can be looked up. Under a lock it allows **at most one charge per key**, so a user retry racing `reconcile` can't double-charge. Each outcome is a deterministic function of (seed, idempotency key, **attempt number**), so replays don't depend on worker or request order, and a retry isn't guaranteed to fail the same way forever. Configured by env. A per-request override header works only when `ENABLE_FAULT_INJECTION=1`. Fixes the current `KeyError` → HTML 500 when payment fails. `txn_id` becomes a string, as the README documents. Each outcome maps to a failure kind and is recorded. **Restored snapshots (from PR 12's review):** restoring a DB snapshot (`git restore`, a backup) replays its reservations with the same `reservation_id` and idempotency key, legacy (`legacy-<id>`) or uuid. A ledger hit for a key therefore counts as "already charged" only if it matches the ride. Ride identity = scooter, reservation, end location (and amount); the amount alone is a rounded distance and can't tell two end points apart (PR #16 review). Anything else is a conflict, recorded and surfaced, never a free ride. Test: restore the DB, end the ride, and the rider is still charged correctly, with no silent free ride (extends `test_a_restored_reservation_is_charged_again_not_ridden_free`). | ✅ |
| 14 | `fix`: all-or-nothing end-ride | Save `payment_pending` and the idempotency key **before** calling the gateway. Success: free the scooter, set its location and store the `txn_id`. Decline: H3 policy. The response says the ride ended with an outstanding balance (H7). Timeout or error: the ride stays `payment_pending`. A retry with the same key first checks the ledger, so the rider is charged at most once. Tests prove the invariants for every fault: no charge without an ended ride, and no ended ride without a charge or a recorded outstanding balance. Add a `charged_without_persisted_end` anomaly rule: a payment `ok` followed by a failed or refused `db_write` in the same record (this exposes today's xfail scenario directly). **From PR 13:** persist the end location **and** the amount with `payment_pending` at the first attempt, and have retries reuse them rather than the client's new coordinates. PR 13's ledger matches rides on the exact end location, so a retry from a slightly different spot (GPS jitter) would otherwise be `payment_conflict` and leave a charged ride stuck open; and give `success_without_write` an exemption only if an idempotent end really skips the write. | ✅ |
| 14b | `feat`: payment anomaly coverage + amount invariant | From PR #17's review. The records-side `anomalies` missed some planted end-ride bugs that the tests catch, and the invariants checked only that a charge or balance existed, not its amount. New rules `ended_without_payment`, `charged_before_pending_persisted` and `pending_location_changed`. `decline_not_settled` now checks the balance amount. Invariant I5 checks amounts. `make records ARGS="ledger-check"` checks the ledger against the records: double charges per reservation, charges under foreign keys, and open or resolved orphans (requests that died mid-payment). **Double charges** across two keys are covered by the new `double_charge` and `payment_key_mismatch` rules and by I3 per reservation (PR #18 review). Each of the 10 planted bugs is shown to be flagged, with no false positives on clean flows or the soak. AGENTS.md says what `anomalies` can't see. | ✅ |
| 15 | `feat`: reconcile stuck rides | `make reconcile` (and later an MCP tool) settles every `payment_pending` ride. If the ledger shows a charge, the ride completes with that `txn_id`. Otherwise it retries (a new attempt, same key). After **N attempts (default 3) or T minutes (default 15)** with no charge, the ride ends with an outstanding balance (H3). Each pending ride therefore ends in a terminal state and no scooter stays stuck forever. Tests cover each branch. **From PR 14:** pending rides carry `payment_attempts`, `payment_requested_at` and the stored end location/amount, which is what the N/T rule and the retries need. Reconcile must also find rides that a **restored DB** left `active` while the ledger has a charge for their key (invariant I1, "charged but active"). It should settle them from the ledger's ride (its end location and `txn_id`), or report them; and it can reuse `tests/invariants.py`'s checks as its acceptance test. **From PR 14b review (NIT):** label a double charge per scooter too: two charges between one successful start and the scooter becoming free (catches a bug that swaps in a new reservation id and key before charging, which today shows only as `payment_key_mismatch` and I3). | ✅ |
| 15b | `fix`: settle restore-replayed rides that are charged and owed | From PR #20's follow-up. "A charge always wins" held within one timeline, but a DB restore replayed a **declined** ride: the gateway charged the replay, and a later restore brought the balance back, so one reservation was **charged and owed**. (a) Resolve the double state: the gateway replays a decline for the same ride (a conflict for another ride), so a declined ride is never charged; reconcile's `clear_paid_balance` marks a balance whose reservation the ledger shows charged as settled (`settled_by_txn_id`/`settled_at`/`settled_reason`, kept for the audit trail), and settles restored declined rides from the ledger (`settle_declined`, `settle_declined_from_ledger`); invariants and `anomalies` treat a settled balance as not owed. (b) `charged_and_owed` says whether the current DB still holds the balance (`still_in_db`, `settled_by`, `resolved`), and `anomalies` exits 1 only for unresolved findings. Deterministic restore-replay tests (balance still present / wiped), seeded restore-heavy random histories, planted bugs and a mutation check. AGENTS.md: how a cold agent confirms a restored DB against I1–I5 (`tests/invariants.py`). | ✅ |
| 15c | `feat`: recover what a DB rollback erases | From PR #21's review. `git restore scooter_db.json` is the documented reset, and a whole-DB rollback erases more than "a lost declined balance": every balance and every ride in progress since the snapshot (dozens of I2 hits per random history). (1) Reconcile's `rebuild_lost_balance` re-creates a balance the ledger proves (a decline, or only uncharged attempts) for a reservation with no charge and no trace in the DB (never for a live or later-charged reservation; idempotent), with `reason: rebuilt_from_ledger`. (2) `make records ARGS="rollback-check"` lists what was erased: lost balances, rides erased in progress (free rides, unrecoverable) and records-only balances. (3) AGENTS.md describes everything a rollback erases and what an I2 hit after a restore means, with a runbook. (4) NIT: `charged_and_owed` names the DB it consulted. Whole-DB rollback scenarios per loss type, seeded whole-DB rollback histories, mutation-checked guards. | ✅ |

### Stage 5: Agents can ask the system questions (Outcome 4)
| # | PR | Scope | Status |
|---|---|---|---|
| 16 | `feat`: MCP server | Built on the official `mcp` Python SDK (v2.x: `mcp.server.mcpserver.MCPServer`; the 1.x `mcp.server.fastmcp` import no longer exists) and reusing the PR 10 query module. Tools: `recent_failures`, `failure_counts`, `scooter_history`, `request_trace`, `scooter_state`, `run_tests` and `replay_fault`. Registered in a checked-in `.mcp.json` and enabled for fresh sessions through `enabledMcpjsonServers` in `.claude/settings.json`. `run_tests` and `replay_fault` run only against an isolated temp instance; if MCP tools run outside the Bash sandbox, they're set to need approval. AGENTS.md documents it and the CLI fallback. Tests included. | ✅ |

### Stage 6: Cold-agent readiness (acceptance test)
| # | PR | Scope | Status |
|---|---|---|---|
| 17 | `docs`/`tooling`: debugging playbook + reproducible scenarios | A fresh checkout has no records, so `make scenario NAME=…` drives realistic traffic and faults against an isolated instance to produce evidence. AGENTS.md "Debugging a symptom" playbook: reproduce, then query records, then compare scooter state before and after, then write a failing test. | ✅ |

### Stage 7: What the AI got wrong (Outcome 7)
| # | PR | Scope | Status |
|---|---|---|---|
| 18 | `docs`: curate AI_NOTES.md | Entries are added throughout. This PR picks the strongest three or more and makes sure each one says what was wrong, how it was caught and what changed. | ⬜ |

### Stage 8: Ready to deploy later (not deployed)
| # | PR | Scope | Status |
|---|---|---|---|
| 19 | `chore`: deploy readiness | Explicit gunicorn `--bind 0.0.0.0:$PORT` in the Procfile. `/health` endpoint. Updated `app.json`. README "Deploying" notes, including the limits of a JSON-file DB and log on an ephemeral filesystem (H5). gunicorn 26 control socket (`--no-control-socket` or configure its path). Confirm Heroku supports Python 3.15 before any deploy. Consider `pip-sync` in `make setup` so the venv exactly matches the lock. **No deploy.** | ⬜ |

### Stage 9: Verification (no PR unless gaps are found)
| # | Step | Status |
|---|---|---|
| 20 | Cold-agent rehearsal: plant a bug on a throwaway local branch (never pushed), give a fresh agent only a one-line symptom, and check that it finds the root cause with evidence from recorded data and writes a failing test. Gaps found become new PRs here. | ⬜ |

---

## Human decisions

These need a person. The agents use the default shown unless told otherwise.

| # | Decision | Default the agents will use |
|---|---|---|
| H1 | **Formal PR approval.** One GitHub account can't approve its own PRs. | The reviewer posts a verdict comment, and the orchestrator merges on `VERDICT: APPROVE`. For real approvals, add a second account or a GitHub App reviewer. |
| H2 | **Branch protection on `master`** (require a PR and passing CI, block force-push). This is the real push control for Outcome 6 and a repo setting agents won't change. | Not changed by agents. The `gh api` calls that would change it are denied. **Since H10 makes merges autonomous, this is the control on `master`: turn it on (require the `test` check, block force-push) before relying on autonomous merges.** |
| H3 | **Payment-decline policy**, plus the terminal rule for unresolved payments. | When a card is *definitively declined* at end-of-ride: the ride ends, the scooter is freed at its end location, **no charge** is taken, and an *outstanding balance* is recorded against the reservation. Timeouts and errors stay pending until a retry or `reconcile` settles them, and the idempotency key means they're charged at most once. **If there is still no charge after 3 attempts or 15 minutes, the ride ends with an outstanding balance.** No scooter is stuck forever. Alternative: keep the ride open after a decline and auto-release it after an expiry. |
| H4 | **State-changing `GET` endpoints** (`/reservation/start`, `/reservation/end`). | Keep them as GET to match the README contract, and note it. POST would be a breaking change. |
| H5 | **Deploy target & persistence.** Heroku's filesystem is ephemeral. | Not solved now (no deploy). Documented in PR 19. |
| H6 | **Record contents.** Records include ride coordinates (location data). | Stored locally and git-ignored, with no retention policy yet. |
| H7 | **Status codes for payment failures.** The README documents only 422 for errors. | Decline → **422** with `error.kind = payment_declined`. The body states that the ride **ended** (`ride_ended: true`) and gives the `outstanding_balance`, so clients don't retry. Gateway timeout → **504**, gateway error → **502**. Both leave the ride `payment_pending` until a retry or reconcile. |
| H8 | **Storage engine.** | Keep the JSON file plus a file lock (README stack). SQLite is the noted alternative if concurrency needs grow. |
| H9 | **Demo-day data.** Will the cold agent find pre-recorded data, or must it reproduce the bug? | Plan for both: records accumulate during any use, and `make scenario` (PR 17) can regenerate evidence from a clean checkout. |
| H10 | **Escalation policy** (decided by the repo owner, 2026-11-09). | "I don't want every push, PR creation, merge, and comment to need human intervention; only when a decision is unclear from documentation or if agents disagree on the solution do I want human intervention." Agents act autonomously within `.claude/settings.json`. They escalate only when README/ACTIVITY/PLAN/AGENTS don't settle a decision, or when the writer and reviewer disagree. |
| H11 | **OS-sandbox the MCP server?** (PR #23 review.) Claude Code runs MCP servers outside the Bash sandbox. The `scooter` server's 12 auto-approved read-only tools import and run working-tree modules (`app.py`, `records_query.py`, `reconcile.py`, `payments.py`, `records.py`, `failures.py`, `mcp_server.py`, and `tests/invariants.py` on each `check_invariants` call), so an ordinary, unprompted edit to one of them runs with full user access the next time the server starts (or the next `check_invariants` call). Options: wrap the server in `sandbox-exec` (macOS) / `bwrap` (Linux) via `.mcp.json`; ask on every tool instead of allowing the read-only ones; or accept and document the risk. | **Open: accept and document** (AGENTS.md "Agent tools (MCP)" and "Agent boundaries"). The `.mcp.json` entry point itself is guarded (ask rules on `.mcp.json` and `mcp_server.py`, sandbox `denyWrite` on `.mcp.json`). No wrapper is implemented until a human decides. |

---

## Change log
- 2026-11-04: Initial plan.
- 2026-11-04: Revised after the adversarial review of PR #1. Python 3.14 → 3.15. Fully locked deps. Onboarding docs (AGENTS.md/CLAUDE.md/AI_NOTES.md) created early. Boundaries moved to Stage 1 and given sandbox enforcement. Failure kinds cover framework errors. Radius validation added. Records gained before/after state, a CLI fallback and isolation in tests. The payment work is split into transactional DB, persisted reservation state, gateway, all-or-nothing end-ride and reconcile. H3 default changed to "end ride + outstanding balance". Added H7–H9 and a cold-agent readiness stage.
- 2026-11-04: Second review. Gateway outcome now also depends on the attempt number. The ledger is persisted. Reconcile has a terminal rule (N attempts / T minutes → outstanding balance). H7 decline body says the ride ended; timeouts 504, errors 502. Added the sandbox network allowlist, the `make agent` budget wrapper, `git_dirty`, atomic log lines, the mcp 2.x API note and MCP enablement.
- 2026-11-04: PR 2. PR 19's scope gained three findings from PR 1 (#2): the gunicorn 26 control socket (`~/.gunicorn/gunicorn.ctl` by default), confirming Heroku supports Python 3.15 before any deploy, and `pip-sync` in `make setup` so the venv exactly matches the lock.
- 2026-11-09: PR 6. The owner set the escalation policy (H10). Feature-branch pushes (`git push [-u] origin stage-…`), PR create/comment/edit/review, squash merges, `gh api` reads and edits to `Makefile`/`requirements*`/`scripts/**` no longer need approval. `gh api` writes, `.claude/**` and `.github/**` edits, unsandboxed retries and destructive local git still do. The `gh api` writes follow the reviewer's reading of H10, which the writer agreed with: the owner listed push, PR creation, merge and comment, and API writes aren't on that list. Deny rules were kept and extended to:
  - every dangerous `git push` flag, including abbreviations (`--mirror`, `--all`, `--branches`, `--prune`, `--delete`, `--receive-pack`/`--exec`)
  - `+`, `:branch` and `refs/` refspecs
  - any push mentioning `master`
  - branch-protection, ruleset and `DELETE` API calls
  - `gh auth`

  The policy is now tested by behaviour (`tests/test_settings_policy.py`). Docs hosts were added to the sandbox allowlist. H2 is now the control on `master`.
- 2026-11-09: Added PR 6b. Short-flag clusters with `f`/`d` in their first three letters (e.g. `-fqu`, `-uqd`) are denied for `git push`. Clusters with `f`/`d` at the 4th letter or later are documented as a gap, covered only by H2 plus review.
- 2026-11-09: PR 8 done. PR 12's scope gained three DB-validation gaps from the PR 7 review (#9): lat/lng ranges in the DB, extra fields dropped on write, and duplicate ids.
- 2026-11-09: PR 9 done. Row 8's text corrected: a missing radius is `missing_param`, not `invalid_radius`, matching what shipped.
- 2026-11-09: Added PR 11b (PR 11 review follow-ups). PR 11 changed the error precedence on `/reservation/end`: parameters are now validated before the DB is read, so a corrupt DB plus a bad `lat` returns 422 `invalid_coordinate` instead of 500 `storage_error`, matching `/search`.
- 2026-11-09: PR 12 done. Decisions that later PRs build on: `idempotency_key` equals `reservation_id`; a legacy reserved scooter (no `reservation` key, e.g. scooter 33) loads with a deterministic `legacy-<id>` reservation; unknown top-level fields are preserved (not rejected); an unknown `payment_state` is `storage_error` (scoped after review, see below); and `write_db` refuses to write a DB that breaks the reservation invariant (`internal_error`).
- 2026-11-09: PR 12 review fixes.
  - Correction: the earlier line said only `legacy-<id>` keys repeat after a restore. In fact any restored snapshot replays every reservation id and idempotency key, uuid ones included. PR 13's scope now requires ledger hits to match the ride (scooter, reservation, amount) and a restore test.
  - Legacy ids stay deterministic: a random id per load would break retry recognition, and wouldn't fix restores.
  - Reservation ids and idempotency keys must be unique across scooters (`storage_error`, and refused by the write guard).
  - An unknown `payment_state` now affects only its scooter: reads show it as reserved, start/end on it are `storage_error`, and the write guard never changes it.
  - PR 14's scope gains a `charged_without_persisted_end` anomaly rule.
- 2026-11-09: PR 12 second review fix: `write_db` also refuses a write whose set of scooter ids differs from the set the transaction loaded. Before this, a write that dropped a whole entry passed the guard and the scooter vanished.
- 2026-11-09: PR 13 done. Decisions:
  - Every payment failure still leaves the ride open, **including a decline**. H3/H7's "decline ends the ride with an outstanding balance (`ride_ended: true`)" is PR 14's, so `payment_declined` (422) currently means "declined, ride open".
  - `payment_failed` (502) now means "gateway error" (outcome `error`, or an unreadable ledger). The pass-through of a gateway's own `msg`/`code` is gone with the dummy gateway: each outcome maps to a fixed kind, msg and status.
  - New kinds: `payment_declined` 422, `payment_timeout` 504, and `payment_conflict` 409 for a ledger hit that doesn't match the ride.
  - Restored snapshots: a matching end returns the original charge (`already_charged`, same `txn_id`, no new charge). A restored reservation is the same ride, already paid. A non-matching end is `payment_conflict`. `test_a_restored_reservation_is_charged_again_not_ridden_free` became `…_is_not_charged_twice_or_ridden_free`.
  - The ledger lives in `data/payments_ledger.jsonl` and survives `git restore`.
  - The PR 13 `txn_id` xfail is resolved: `txn_id` is a 32-hex-char string.
  - PR 14's row gains: store the amount at the first attempt.
- 2026-11-10: PR 13 review fixes.
  - Ride matching now uses the end location. The first version matched on (scooter, reservation, amount), and the amount is a rounded distance. So a restored ride ended at a different place the same distance away closed with the first ride's charge: a free ride. Ride identity is now scooter, reservation, exact end location and amount. Row 13's rule text is corrected.
  - Row 14 now persists the end location and amount at the first attempt, so a retry isn't a GPS-jitter conflict.
  - Added `make payments ARGS="verify"` and an AGENTS.md runbook for an unreadable ledger.
  - Narrowing `payment_failed` to "gateway error" and removing the gateway msg/code pass-through didn't break any live contract. The old dummy gateway always succeeded, so neither could happen without a monkeypatched gateway.
- 2026-11-10: PR 14 done. Decisions:
  - The gateway call stays under the DB lock (phase 1 → charge → phase 2): correctness over lock-hold time, bounded by the simulated delay. Splitting is for deploy readiness (PR 19).
  - A decline ends the ride (H3). The balance goes in the scooter's `outstanding_balances`, keyed by reservation, in the same atomic write. The 422 body adds `ride_ended: true` and `outstanding_balance` (H7).
  - Timeouts and errors stay `payment_pending` (`ride_ended: false`). Every payment-failure body now has `ride_ended`, which is additive.
  - Retries reuse the stored end location and amount, and ignore the retry's own coordinates.
  - A conflict reverts to `active` with nothing charged, because staying pending would conflict forever. A restored DB can still leave I1 broken, so PR 15 picks that up.
  - Start on a pending scooter is the new kind `scooter_payment_pending` (422).
  - The `txn_id` is kept in the ledger and records, not the DB; there's no ride history.
  - No `success_without_write` exemption was needed, because every end writes.
  - The PR 14 xfail is resolved.
  - H3/H7 left no decision open. Two consequences, though: there's no endpoint to pay a balance, and no rider identity, so balances are per scooter and reservation.
- 2026-11-10: Added and completed PR 14b (payment anomaly coverage + amount invariant), from PR #17's review. `anomalies` now names all 8 planted end-ride bugs. Before, run on the same scenarios, it named 3 (decline without a balance, a decline left pending, a ride ended at the request's coordinates). Invariant I5 checks amounts. `payment-orphans` covers requests that died mid-payment. AGENTS.md lists what `anomalies` can't see.
- 2026-11-10: PR 14b review fixes (PR #18). A planted "new key per attempt" bug charged one ride twice, and nothing saw it. Fixes: payment results now record the key and reservation the gateway charged under; new anomalies `payment_key_mismatch` and `double_charge`; I3 counts charges per reservation and flags keys no reservation owned; `payment-orphans` becomes `ledger-check` (double charges, foreign keys, open vs resolved orphans); `decline_not_settled` checks the balance's key. Planted bugs M9 and M10 were added.
- 2026-11-10: Added and completed PR 11c (fix the flaky lock-order assertion). The `db_lock` decision gained `acquired_at`; the test and the `scooter` history now order by it. Row 15 gained the per-scooter double-charge check (PR #18 NIT).
- 2026-11-10: PR 11c review fix. The deterministic lock-order test now makes the slow request wait inside `acquire_db_lock`'s real polling loop (every non-blocking attempt is "busy" until go), so it also pins where `acquired_at` is taken. Taking it before the wait is caught.
- 2026-11-10: PR 15 done (`make reconcile`). Decisions:
  - **Retries:** one gateway attempt per ride per run; scheduling (e.g. cron) spreads attempts.
  - **The terminal rule:** "attempts" is the larger of the DB's count and the ledger's entries for the key. "T minutes" is measured from `payment_requested_at`. A ledger charge always wins over the rule.
  - **Restore-induced charged-but-active rides** are settled from the ledger: identity (scooter and reservation) plus an amount re-check against the scooter's position. Anything that doesn't fit is reported.
  - **Unresolved balances** carry `reason: payment_unresolved`.
  - **Records:** every action is a `/reconcile` request record.
  - **New anomaly rules:** `reconcile_not_settled` and `scooter_double_charge` (#18's NIT).
- 2026-11-10: PR 15 review fixes (PR #20). I1's "charged and owes a balance" is now checked per `reservation_id`, and there's a new anomaly rule `charged_and_owed` (a key switch had hidden it). `end_location_not_persisted` now covers `/reconcile` records, whose decisions carry their end coordinates. A reconcile `report` is recorded as the new failure kind `reconcile_unsettled`. The reviewer's two tool-silent reconcile bugs are now planted tests (R1, R2). There's a new `decide()` row for "a charge wins even with ≥ N ledger entries".
- 2026-11-10: Added and completed PR 15b (restore-replayed rides that are charged and owed). Decisions:
  - **Never charge a declined ride again:** the gateway replays a decline for the same ride (H3's "definitive"; like a real gateway's idempotency), and is a conflict for another ride. So the end path needs no ledger check of its own: never a double charge, never a free ride (the replay ends with a balance), never a declined rider charged on a replay.
  - **A paid balance is marked settled, not deleted** (`settled_by_txn_id`, `settled_at`, `settled_reason`): the audit trail keeps both the decline and the charge that voided it. Reconcile does it (`clear_paid_balance`, first, on any scooter) in up to 3 passes per run.
  - **`charged_and_owed` reads the current DB** (`--db`) to say whether the balance is still owed; resolved findings are labelled historical and don't make `anomalies` exit 1.
  - **Not fixed (reported):** a restore from before a ride started loses that ride's balance; I2 flags it, nothing rebuilds it from the ledger.
- 2026-11-10: Added and completed PR 15c (recover what a DB rollback erases; PR #21 review follow-up). Decisions:
  - **Rebuild from the ledger only**, never from the records: the ledger is the gateway's own evidence; records are history. Guards: no charge for the reservation (a charge always wins), no trace of it anywhere in the DB (live reservation, or balance open or settled, by id or key).
  - **`payment_unresolved` is rebuilt too** when the ledger has uncharged attempts: nobody can retry a ride the DB no longer has, so H3's terminal rule applies at once. An `end_unpaid` by age with 0 attempts has no ledger evidence and is reported.
  - **Rides erased in progress are reported, not settled** (`rollback-check`): there's no end location or cost anywhere. These are free rides.
  - `scooter_double_charge` no longer counts a restored older ride's charge against the scooter's latest ride (a false positive under whole-DB rollbacks).
- 2026-11-10: PR 16 done (MCP server). Decisions:
  - **mcp 2.3.0 in the dev lock**, not the runtime one: it's an agent tool, not part of the deployed app. Verified on Python 3.15 (macOS install; Linux cp315 wheels for its compiled deps) and in CI.
  - **Tools beyond the row's list:** `anomalies`, `ledger_check`, `rollback_check`, `check_invariants`, `payment_lookup`, `payment_draw`, `reconcile_plan` (read-only, allowed) and `reconcile_run` (changes the live DB, asks). `run_tests`, `replay_fault` and `reconcile_run` ask, because MCP servers run outside the Bash sandbox.
  - **`replay_fault`** rebuilds the scooter from the record's `scooter_before` and the key's earlier ledger entries in a temp instance, and re-applies the record's fault provenance; it never writes a live file.
- 2026-11-10: PR 16 review fixes (PR #23). `.mcp.json` was an auto-approved, unsandboxed entry point by name: edits to `.mcp.json` and `mcp_server.py` now ask, and `.mcp.json` is in the sandbox's `denyWrite` (it's also a built-in protected path). `run_tests` drops every `PYTEST_*` / `PYTHON*` variable from pytest's environment. Docs: `run_tests` runs working-tree test code unsandboxed (prefer `make test`). Added H11 (OS-sandbox the MCP server?), open.
- 2026-11-10: PR 17 done (cold-agent playbook). `make scenario NAME=…` (8 scenarios, an isolated instance per run under `data/scenarios/`, working-tree code with a private bytecode cache, per-step expectations, evidence summary and query commands; MCP tools take `scenario=`). AGENTS.md "Debugging a symptom" moved to the top as a 7-step playbook with three worked examples of the ACTIVITY symptom. `make reset-local` (PR #22 review MINOR): backs up and resets the DB, records, ledger and scenario runs together, deleting nothing; every reset instruction now points at it, and `git restore scooter_db.json` alone is described as a rollback. Proved with planted bugs (two causes of the ACTIVITY symptom, a payment bug, a validation bug, and stale bytecode) in `tests/test_playbook.py`, plus a fresh-clone walkthrough.
- 2026-11-10: PR 17 review fixes (PR #24). `started_keys` counts every reservation a record saw, so the documented invariants CLI and MCP `check_invariants` no longer report a false I3 for `legacy-33` on clean data; a test runs every documented step-5 command on every clean scenario. Playbook step 3 is now "run the test suite" (the fastest signal). `payment-faults` checks a pending scooter isn't listed or found; `reconcile` exercises the N-attempts rule. `filter`/`search` decisions record `listed`/`matches` and `unavailable` reasons; new anomaly `listed_unavailable`. `reset-local` restores `data/.gitkeep`.
