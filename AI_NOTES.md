# AI_NOTES.md: what the AI got wrong

This is an honest log of times an AI agent (writer, planner or reviewer) was **confidently
wrong**, how we caught it, and what changed because of it (ACTIVITY.md Outcome 7). Add an
entry in the PR where the mistake is caught, or in the next one. PLAN PR 18 curates the
strongest entries.

## Entry format

```md
### N. <short title>
- **Where:** <PR / file / plan section>
- **What the AI claimed:** <the confident statement or design, as it was presented>
- **Reality:** <what was actually true, with evidence>
- **How it was caught:** <test, log, review, command run, gut feeling>
- **What changed:** <the fix, plan edit or process change>
```

## Entries

### 1. "Python 3.14 is the current stable release"
- **Where:** PLAN.md toolchain section, first draft (PR #1).
- **What the AI claimed:** Python 3.14 was "current stable", so it was the right target.
- **Reality:** Python 3.15 came out on 2026-10-09 (latest 3.15.2). The machine's 3.14.7
  wasn't even the latest 3.14 patch (3.14.10).
- **How it was caught:** the adversarial reviewer checked endoflife.date and PyPI instead
  of trusting the claim.
- **What changed:** the plan was retargeted to Python 3.15. PR #2 shipped
  `.python-version` = 3.15 and a Makefile that checks the venv's version.

### 2. "Turn debug off", plus a whole PR for unchecked breakage
- **Where:** PLAN.md PR 2, first draft (PR #1).
- **What the AI claimed:** the first draft's PR 2 was "`fix`: app runs on modern stack". It
  said to "Fix anything that breaks on Flask 3 / geopy 2", which was hedged, and to "Turn
  debug off", which was stated as a task.
- **Reality:** debug was never on: `app.debug` is `False`, and `app.run()` doesn't pass
  `debug=True`. That task came from trusting a stale `# TODO: Turn debug flag off` comment
  in `app.py`. A whole PR was planned around breakage nobody had checked for. Every
  endpoint works unchanged on Flask 3.1 / Werkzeug 3.1 / geopy 2.5.
- **How it was caught:** the reviewer ran every README endpoint on the latest stack and
  checked `app.debug`.
- **What changed:** that PR was rescoped in PLAN.md to config-from-env work (now PLAN
  PR 3). PR #2 re-verified every endpoint on Python 3.15, both through the Flask dev server
  and through gunicorn.

### 3. "Deterministic outcome per idempotency key" + "retry with the same key" is safe
- **Where:** PLAN.md payment design (PRs 13/14/15), first revision of PR #1.
- **What the AI claimed:** making each fake-gateway outcome a deterministic function of
  the idempotency key, and retrying with that same key, gave safe and reproducible payments.
- **Reality:** combined, the two rules **guarantee** a stuck ride. If the first attempt
  times out without a charge, every retry and every reconcile hits the same timeout, so
  the ride stays `payment_pending` and the scooter stays reserved forever. Outcome 3
  forbids exactly that.
- **How it was caught:** the reviewer traced PRs 13, 14 and 15 against each other instead
  of reading each one on its own.
- **What changed:** PLAN.md was revised. The planned outcome function now takes the
  attempt number (seed, key, attempt), and the planned reconcile has a terminal rule: after
  N attempts or T minutes with no charge, the ride ends with an outstanding balance. None
  of this is built yet; it lands in PLAN PRs 13–15.

### 4. Bash deny patterns "enforce" no network and no push to master
- **Where:** PLAN.md agent-boundaries PR (now PLAN PR 6), first draft (PR #1).
- **What the AI claimed:** permission deny patterns on Bash commands would enforce "no
  network except localhost" and "no pushes to master".
- **Reality:** pattern matching on command strings is easy to get around.
  `python -c 'import urllib...'` reaches the network, and `git push origin HEAD:master`
  dodges a `git push origin master` pattern.
- **How it was caught:** the adversarial review of the plan.
- **What changed:** the plan now relies on the harness **sandbox** (OS-level filesystem
  and network limits, PLAN PR 6) plus GitHub **branch protection** on `master` (H2) for
  enforcement. Deny patterns are planned as only a first line. Neither control is in place
  yet: PR 6 hasn't landed, and branch protection is still waiting on a human (H2).

### 5. "Decline → ride stays open" is safe for Outcome 3
- **Where:** PLAN.md human decision H3, first draft (PR #1).
- **What the AI claimed:** after a declined payment, keeping the ride open with no charge
  met Outcome 3 ("nothing gets corrupted").
- **Reality:** if the rider walks away after the decline, nothing ever ends the ride, so
  the scooter stays reserved forever, which Outcome 3 explicitly forbids.
- **How it was caught:** the adversarial review of the plan.
- **What changed:** PLAN.md's H3 default is now "end the ride, free the scooter, take no
  charge, record an outstanding balance". Keeping the ride open with an expiry is listed as
  the alternative. The policy is implemented in PLAN PR 14; it doesn't exist in code yet.

### 6. Request traces are "in the server's stdout access log"
- **Where:** AGENTS.md "Where data lives", first version (PR #3).
- **What the AI claimed:** the only trace of a request today is the server's stdout access
  log.
- **Reality:** the Flask dev server prints only its banner to stdout; its request lines go
  to **stderr**. gunicorn writes **no** request log at all by default; you need
  `--access-logfile -`. A cold agent told to check stdout would have found nothing.
- **How it was caught:** the reviewer ran each server with stdout and stderr captured
  separately. The writer reproduced it: 0 request lines on either stream from default
  gunicorn, and request lines only on stderr from `python app.py`.
- **What changed:** AGENTS.md now says where each server logs requests, and how to turn
  gunicorn's access log on.

### 7. This log overstated past claims and described planned guardrails as already in place
- **Where:** AI_NOTES.md, first version (PR #3), entries 2–4.
- **What the AI claimed:** entry 2 said the plan had claimed "upgrading would break the
  app"; the plan had actually hedged ("fix anything that breaks"). Entries 3–4 described
  the sandbox, branch protection and the reconcile terminal rule in the present tense, as
  if they existed. They were only plan edits.
- **Reality:** a log of AI mistakes was itself dramatising the mistakes and overstating
  the fixes. That's the same kind of confident inaccuracy it exists to record.
- **How it was caught:** the reviewer compared each entry with the original PLAN.md wording
  (commit `10b5b2f` in PR #1) and with what was actually merged.
- **What changed:** the entries were reworded to quote what was written and to say what
  only changed in the plan. Lesson: the log needs review too. Check each entry against the
  source text, not against memory of it.

### 8. A known-bug test that "asserts the fixed behaviour" passed for the wrong reason
- **Where:** `tests/test_known_bugs.py::test_failed_payment_returns_json_error`, first
  version (PR #5).
- **What the AI claimed:** the xfail test pinned the payment-failure bug (PLAN PR 13) by
  asserting the fixed behaviour: end-ride after a failed payment returns JSON, not a 500.
- **Reality:** the assertions were loose, only "JSON and not 500". The reviewer's mutation
  "start doesn't persist the reservation" made the test **XPASS**: end returned the
  ordinary "no reservation" 422 without ever calling the gateway. That response is JSON and
  isn't a 500, so the test passed while the payment path never ran. Once PR 13 removed the
  xfail marker, that regression would have gone unnoticed.
- **How it was caught:** the reviewer's mutation testing. Strict xfail turned the
  unexpected pass into a failure this time. The writer's own mutation run had marked the
  same mutation "caught" without checking *why* that test failed.
- **What changed:** the test now asserts each step:
  - start returned 200, and the reservation was persisted
  - the gateway was actually called exactly once
  - the response isn't the no-reservation message and carries no `txn_id`
  - the scooter is either still reserved where it was, or freed at the end location, never
    half-written, and other scooters are untouched

  AGENTS.md now has the rule. Lesson: known-bug tests need specific assertions, not just
  "no 500". When a mutation is caught, check which assertion caught it.

### 9. The reviewer's probe "contradicted" AGENTS.md, but the probe was wrong
- **Where:** re-review of PR #5, checking AGENTS.md's Flask 3 `TESTING` / 500-handler table.
- **What the AI claimed:** the reviewer agent's own probe seemed to show that the
  **`500`-registered** handler returns HTML, not JSON, with `TESTING=False`. That would
  contradict the "JSON 500" cell in AGENTS.md's table.
- **Reality:** the doc was right. Flask passes a `500` handler an `InternalServerError`, not
  the original exception, and the probe's handler returned every `HTTPException`
  unchanged. `InternalServerError` is itself an `HTTPException` (`issubclass` is `True`),
  so the handler handed back Flask's HTML page. The probe's `Exception` handler gave JSON
  with `TESTING` on and off.

  The writer re-checked on Flask 3.1.3, with an unhandled `KeyError` and a handler that
  passes `HTTPException` through:

  | Handler registered for | `TESTING=True` | `TESTING=False` |
  |---|---|---|
  | `500` | `KeyError` raised; handler not called | HTML 500 (handler got `InternalServerError`) |
  | `Exception` | JSON 500 (handler got `KeyError`) | JSON 500 (handler got `KeyError`) |

  An `Exception` handler of that shape still returns HTML for an explicit `abort(500)`,
  with `TESTING` on or off:

  | Route | Response |
  |---|---|
  | raises `KeyError` | JSON 500 |
  | calls `abort(500)` | HTML 500 |
- **How it was caught:** the reviewer re-checked its own probe before posting, fixed the
  pass-through condition, re-ran it, and got the result AGENTS.md documents. No false
  finding was posted.
- **What changed:** nothing in the docs. Lesson: the reviewer can be confidently wrong too.
  Before you call something a bug, check that the probe tests what you think it tests.
  This also matters for PLAN PR 7: a JSON `internal_error` handler that passes
  `HTTPException` through must not let `InternalServerError` through as HTML.
- **This entry was itself first written wrong.** The first version (PR #6) said the
  `Exception` handler was the one returning HTML. It was written from a paraphrase of the
  review, not from the probe's recorded output, and the reviewer caught it. Lesson:
  fact-check log entries against recorded output, not against someone's summary of it.
