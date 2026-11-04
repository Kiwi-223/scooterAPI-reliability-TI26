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

### 2. "Fix what breaks on Flask 3 / turn debug off"
- **Where:** PLAN.md PR 2, first draft (PR #1).
- **What the AI claimed:** upgrading would break the app on Flask 3, and debug mode was on
  and had to be turned off.
- **Reality:** every endpoint works unchanged on Flask 3.1 / Werkzeug 3.1 / geopy 2.5, and
  `app.debug` was already `False`. The claim came from trusting a stale
  `# TODO: Turn debug flag off` comment in `app.py`.
- **How it was caught:** the reviewer ran every README endpoint on the latest stack and
  checked `app.debug`.
- **What changed:** that PR was rescoped to config-from-env work (now PLAN PR 3). PR #2
  re-verified every endpoint on Python 3.15, both through the Flask dev server and through
  gunicorn.

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
- **What changed:** the attempt number was added to the outcome function (seed, key,
  attempt). Reconcile gained a terminal rule: after N attempts or T minutes with no charge,
  the ride ends with an outstanding balance.

### 4. Bash deny patterns "enforce" no network and no push to master
- **Where:** PLAN.md agent-boundaries PR (now PLAN PR 6), first draft (PR #1).
- **What the AI claimed:** permission deny patterns on Bash commands would enforce "no
  network except localhost" and "no pushes to master".
- **Reality:** pattern matching on command strings is easy to get around.
  `python -c 'import urllib...'` reaches the network, and `git push origin HEAD:master`
  dodges a `git push origin master` pattern.
- **How it was caught:** the adversarial review of the plan.
- **What changed:** enforcement moved to the harness **sandbox** (OS-level filesystem and
  network limits) plus GitHub **branch protection** on `master` (H2). Deny patterns are now
  only a first line.

### 5. "Decline → ride stays open" is safe for Outcome 3
- **Where:** PLAN.md human decision H3, first draft (PR #1).
- **What the AI claimed:** after a declined payment, keeping the ride open with no charge
  met Outcome 3 ("nothing gets corrupted").
- **Reality:** if the rider walks away after the decline, nothing ever ends the ride, so
  the scooter stays reserved forever, which Outcome 3 explicitly forbids.
- **How it was caught:** the adversarial review of the plan.
- **What changed:** the H3 default is now "end the ride, free the scooter, take no charge,
  record an outstanding balance". Keeping the ride open with an expiry is listed as the
  alternative.
