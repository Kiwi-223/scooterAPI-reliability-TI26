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

### 10. "`--max-turns` isn't in `--help`, so there is no turn cap"
- **Where:** `scripts/agent.sh` and AGENTS.md "Budget", first version (PR #7). The idea
  came in from an earlier review note, and the writer adopted it.
- **What the AI claimed:** the installed CLI has no `--max-turns` flag, because
  `claude --help` doesn't list it. So the wrapper could only cap money and time.
- **Reality:** the flag exists and is supported. It's in the official CLI reference, which
  the writer had **already downloaded** while researching this PR and saw listed. The
  2.1.310 binary defines it with `.hideHelp()`: "Maximum number of agentic turns in
  non-interactive mode". It's hidden, not missing.
- **How it was caught:** the reviewer checked the docs and grepped the CLI binary for the
  option definition.
- **What changed:** `MAX_TURNS` (default 40) is now passed to `--max-turns`, and the docs
  are corrected. Lesson: `--help` isn't the full interface. When `--help` and the official
  reference disagree, check the binary or the reference before treating a flag as absent.

### 11. `claude doctor` was presented as validating the settings file
- **Where:** AGENTS.md "Agent boundaries" and the PR #7 description, first version.
- **What the AI claimed:** `claude doctor` "reports no invalid entries", which implied the
  settings were validated. The writer's two negative tests seemed to back this up: doctor
  flagged a wrong type and one malformed MCP rule.
- **Reality:** doctor stays silent on unknown or misspelled keys, both top-level and inside
  `sandbox`, and on many malformed rules. The writer's own negative test had already shown
  it ignoring `Bash(command:rm *)`, and the writer generalised from the two cases it did
  catch. A misspelled `sandbox.enabeld` would have passed "validation" and silently left
  the sandbox off.
- **How it was caught:** the reviewer planted bad entries (unknown keys, malformed rules)
  and watched doctor say nothing.
- **What changed:** a new `make check-settings` (`scripts/check_settings.py`) validates
  against the vendored SchemaStore schema, with the top level made strict. CI runs it, and
  `tests/test_settings_schema.py` plants nine kinds of mistake and checks that each is
  rejected. AGENTS.md now says what doctor does and doesn't check. Lesson: a negative test
  has to cover the failure you care about (here, a typo that disables a guardrail), not
  just *a* failure.

### 12. AGENTS.md recommended a command that the same PR denies
- **Where:** AGENTS.md Setup and Reversibility, PR #7.
- **What the AI claimed:** "to reset the venv, run `rm -rf .venv && make setup`".
- **Reality:** the same PR added a `Bash(rm -rf *)` deny rule, so an agent following the
  onboarding doc would be blocked by the repo's own policy.
- **How it was caught:** the reviewer ran the documented commands against the new rules.
- **What changed:** a new `make clean` target removes `.venv`, and the docs now say
  `make clean && make setup`. Lesson: after changing a policy, re-run the documented
  workflow under it. Docs and guardrails drift apart when they're written in separate
  passes.

### 13. An unquoted heredoc ran documentation text as shell commands
- **Where:** the writer, while updating the PR #7 description (2026-11-04). It ran a Python
  string-replacement script through `python3 - <<EOF ... EOF`.
- **What the AI assumed:** the replacement text (Markdown full of backticked commands)
  would reach Python unchanged.
- **Reality:** with an *unquoted* `EOF`, the shell does command substitution inside the
  heredoc, so every `` `...` `` in the Markdown ran as a shell command in the main
  checkout, in order:
  - `claude --help` printed its help into the string.
  - `--max-turns`, `MAX_TURNS`, `--max-turns` and `--help` failed with
    `command not found`.
  - `python3 -m json.tool` then blocked waiting on stdin.

  Further down the text were `make check-settings`, `make clean && make test`, `rm -rf`
  (no operands) and `claude doctor`. They never ran, because the hang stopped the script
  first. The main checkout was on `master`, where `clean` and `check-settings` didn't even
  exist yet.
- **How it was caught:** the `command not found: --max-turns` lines in the output, and the
  command hanging until the harness moved it to the background. The writer stopped the
  task before the rest of the substitutions ran, then checked:
  - no processes were left
  - the main checkout was unchanged (`master`, clean, `.venv` intact)
  - the worktree was clean and the PR text file untouched
- **What changed:** the edit was redone with a quoted heredoc (`<<'EOF'`), with values
  passed in through environment variables. Lesson: any heredoc whose body contains
  backticks or `$` must be quoted (`<<'EOF'`). Text you mean as data must never pass
  through shell expansion. That matters more here because the denied commands (`rm -rf`)
  and the allowed ones (`make clean`) were sitting in that very text.

### 14. `gh api *` allowed, with a deny list "covering the dangerous calls"
- **Where:** `.claude/settings.json` and AGENTS.md, PR #7 commit `c12c17c` (the H10 update).
- **What the AI claimed:** allowing `Bash(gh api *)` was fine, because deny rules for
  `DELETE`, `*protection*` and `*rulesets*` covered the dangerous calls. The only listed
  bypasses were exotic ones (`--input`, GraphQL, a URL in a variable).
- **Reality:** the *ordinary* spellings of API writes were all auto-approved, unsandboxed,
  using the owner's admin token:
  - `gh api -X PATCH …/git/refs/heads/master -F force=true` (force-moves `master`)
  - `gh api …/merges -f base=master …`, where `-f` alone makes it a POST (merges into
    `master` without a PR)
  - `PUT …/contents/…` (a direct commit) and `PUT …/pulls/N/merge` (a non-squash merge)
  - `PATCH repos/O/R` (visibility, default branch)

  `--method=DELETE` also slipped past all four DELETE deny rules. And none of this was even
  in the owner's H10 list (push, PR creation, merge, comment).
- **How it was caught:** the reviewer simulated 37 commands against the settings using the
  documented matching rules.
- **What changed:**
  - `gh api` **reads** stay allowed. Every write spelling (`-X`/`--method` anything,
    `-f`/`-F`/`--field`/`--raw-field`, `--input`) is now an `ask` rule, which beats the
    allow.
  - Any `gh api` command containing `DELETE` is denied.
  - `tests/test_settings_policy.py` runs these commands through a matcher and checks the
    outcome.

  Lesson: enumerate the tool's write and danger flags, and test the *behaviour* of the rule
  set on real command lines, not the presence of rule strings.

### 15. "Force-push denied in every spelling we know" missed `--mirror`
- **Where:** AGENTS.md "Denied" row and the push deny rules, PR #7 commit `c12c17c`.
- **What the AI claimed:** force-pushes and pushes to `master` were denied "in every
  spelling we know".
- **Reality:**
  - `git push --mirror origin` force-updates every remote ref, `master` included, and
    deletes the other agents' branches.
  - `git push --all origin` pushes local `master`.
  - `git push --prune …` deletes remote branches.

  None of them contains `--force`, `+` or `master`, so all three were allowed, unsandboxed,
  with no prompt. The writer had never read `git push -h`. That listing also shows
  `--branches`, `--delete`/`-d` and `--receive-pack`/`--exec` (which run a program). Git
  also accepts unique abbreviations (`--mirr`), and `'HEAD:master'` in quotes defeated the
  `*:master` rule.
- **How it was caught:** the reviewer's simulation of `--mirror`, `--all` and `--prune`.
- **What changed:**
  - Every dangerous flag from `git push -h` (git 2.54) is denied by its shortest unique
    prefix, anywhere in the command.
  - `-f`/`-d` and their two-letter clusters are denied, as are `+`, `:branch` and `refs/`
    refspecs and any push mentioning `master`.
  - Only the `git push [-u|-q] origin stage-…` shape is pre-approved.
  - AGENTS.md now lists what still isn't caught: 3+ letter clusters, quote-split
    spellings, aliases and scripts.
  - The behaviour tests include the plain, abbreviated and quoted spellings.

  Lesson: same as #14. Start from the tool's full option list, not from the spellings you
  remember.

### 16. A deny rule ending in `:*` would never have matched a colon
- **Where:** the writer's first fix for #15 (not pushed): `Bash(git push* :*)`, meant to
  deny `git push origin :branch` (delete a remote branch).
- **What the AI assumed:** that `:*` at the end of a rule is a literal colon followed by a
  wildcard.
- **Reality:** the docs define a trailing `:*` as an alias for ` *`. So the rule really
  meant `git push*  *` (two spaces) and would never catch a `:branch` refspec.
- **How it was caught:** the new behavioural tests. The matcher implements the documented
  `:*` rule, and the `git push origin :stage-6/x` rows came back `default` instead of
  `deny`.
- **What changed:** those rules now end in `:**`, which isn't the special suffix, and
  `test_no_rule_ends_in_colon_star` guards against regressions. This is the first catch
  made by the behavioural tests themselves, before review.

### 17. Text-matched deny rules kept leaking, one spelling at a time
- **Where:** the `git push` and `gh api` deny rules across PRs #7 and #8.
- **What the AI claimed:** each round of fixes presented the deny list as covering the
  dangerous spellings, first "every spelling we know" and then "whole-token quotes are
  caught".
- **Reality:** every review found another variant that got through. In turn these were:
  - `--mirror`, `--all`, `--prune` and git's abbreviations (#15)
  - `--method=DELETE` (#14)
  - three-letter short-flag clusters such as `-fqu` (PR #8)
  - quoting and escaping of short flags (`'-f'`, `"-uf"`, `-"f"`, `-\f`) and a tab
    instead of a space

  A rule matches command *text*, and the shell offers unboundedly many texts for the same
  argv. Each fix covered the spellings someone had thought of, and the next reviewer found
  one nobody had.
- **How it was caught:** the reviewer's adversarial probes in each round. The last ones
  were the quoted short flags in PR #8.
- **What changed:** no more rules for this class. The remaining gaps are pinned as
  "known gap" tests (expected `allow`) and written up honestly in AGENTS.md. Lesson:
  enforce at the layer that sees the real effect, not the text.
  - **Branch protection (H2):** the server rejects a force-push or a push to `master`
    however it was spelled.
  - **The OS sandbox:** it limits what any process can write or reach.

  Deny rules are a cheap first line against the usual spelling. Don't extend them to chase
  completeness, and don't present them as a boundary. This matters more because the
  sandbox itself was then observed **not** running in the agents' subagent sessions, which
  is unverified and with the human.

### 18. "A missing or corrupt DB gives an HTML 500" was wrong for one endpoint
- **Where:** AGENTS.md "Known behaviour", written in PR #4 (PLAN PR 3) and kept until
  PLAN PR 7.
- **What the AI claimed:** a missing or corrupt `scooter_db.json` gives an HTML 500.
- **Reality:** on `/reservation/end`, a corrupt file returned **422 "Lat/Lng values must
  be numbers"**. `init_db()` ran inside the `try` that catches `ValueError` for the
  `float()` parsing, and `json.JSONDecodeError` is a `ValueError`. A cold agent seeing
  that message would have chased a client bug, not a broken database. The claim had been
  checked on one endpoint and generalised to all of them.
- **How it was caught:** while writing PLAN PR 7's storage tests, the writer parametrized
  "corrupt DB" over **every** endpoint instead of one. Confirmed against `master`:
  `/reservation/end` gave 422 with that msg, and `/view_all_available` gave an HTML 500.
- **What changed:** `init_db`/`write_db` now turn every read, parse and write problem into
  `StorageError`, which becomes `storage_error` (500). `test_corrupt_db_is_storage_error_on_every_endpoint`
  covers 3 kinds of corruption × 4 endpoints. Lesson: when documenting "X happens", test
  it on every entry point, not the first one you try.

### 19. "Extra fields are tolerated" held for reads but not for writes
- **Where:** `init_db` shape validation, and the PR #9 review fix that claimed it (PLAN PR 7).
- **What the AI claimed:** extra per-scooter fields in `scooter_db.json` are "tolerated",
  for forward compatibility with PLAN PR 12's reservation fields.
- **Reality:** they were tolerated only on **read**. `write_db` serialises through
  `Scooter.to_dict()`, which knows only `id/lat/lng/is_reserved`, so the first reservation
  silently dropped every extra field from the file. The tests only loaded a DB with an
  extra field and checked for a 200; they never wrote one back.
- **How it was caught:** the reviewer ran a round-trip probe on PR #9: add a `note` field,
  reserve the scooter, and the note is gone.
- **What changed:** this is deferred to PLAN PR 12, which rewrites the DB code. Row 12's
  scope now says to preserve or reject extra fields. Lesson: when a claim is about data,
  test the write path as well as the read path. A round trip (read, mutate, write, read)
  is the minimal honest check.

### 20. Concurrency tests that pass without the lock they're meant to prove
- **Where:** `tests/test_records.py`, first version (PLAN PR 9).
- **What the AI claimed:** the threaded test (8 threads × 25 requests) and the
  multi-process test (4 processes appending 70 KB lines) proved that records are written
  "atomically under an exclusive `fcntl.flock`".
- **Reality:** with the `flock` call deleted, both tests still passed. On macOS (and Linux)
  a single `write()` to an `O_APPEND` file usually lands whole anyway, so the tests showed
  that lines didn't interleave *in practice*. They didn't show that the lock is taken. The
  lock is what still protects the line if a write is ever split, or if a future change
  adds a second write per record.
- **How it was caught:** the writer's own mutation run. "no flock" was the only mutation
  out of 15 that survived.
- **What changed:** `test_append_waits_for_the_exclusive_lock` holds the flock from the
  test and checks that `append_line` blocks until it's released. Removing the lock now
  fails it. Lesson: a stress test that "never saw a problem" isn't evidence that the
  mechanism exists. Test the mechanism directly, and mutation-test the tests.

### 21. `scooter_after` was documented as "the state afterwards" but was the code's in-memory object
- **Where:** `records.py` / `app.py` and AGENTS.md "Request records", PR #11 (PLAN PR 9),
  first version.
- **What the AI claimed:** `scooter_after` is the scooter's "state afterwards". Any reader,
  and a cold agent above all, takes that to mean the persisted state.
- **Reality:** routes set it from `scooter.to_dict()` after calling `write_db`, which is the
  object in memory, not what was written. With a planted bug that calls `write_db`
  **before** changing the scooter, the response was 200 "ended" and the record said
  `db_write: ok` and `scooter_after: {is_reserved: false, …new location}`. The file still
  held the scooter reserved at the old location. The record was confidently wrong about
  exactly the kind of bug the cold-agent test plants.
- **How it was caught:** the reviewer planted that ordering bug and compared the record
  with the DB file.
- **What changed:**
  - `scooter_after` is now taken from the exact data `write_db` serialised
    (`records.written(db_list)`). It equals `scooter_before` when the request chose not to
    write, and is `null` when the write failed.
  - A separate `scooter_in_memory` keeps the code's view, so the discrepancy itself is
    evidence. There's a jq recipe for it.
  - `test_planted_write_ordering_bug_is_visible_in_the_record` plants the bug in a copy of
    `app.py` and checks the record.

  Lesson: a record field must say where its value comes from. Evidence about persistence
  has to be read from what was persisted.

### 22. The reviewer's near-miss: stale bytecode kept running a reverted bug
- **Where:** the reviewer's own probe of PR #11.
- **What happened:** the reviewer planted a bug in `app.py` and then reverted it within the
  same second, leaving the file the same size. Python's `__pycache__` check (mtime + size)
  saw no change, so the server kept running the planted bug's bytecode while the source,
  and `git_dirty: false`, said the code was clean.
- **How it was caught:** the reviewer disassembled the cached `.pyc` and found the planted
  logic still in it.
- **What changed:** AGENTS.md now says that `git_dirty` describes the source tree at
  startup, not the code that's actually running. Delete `__pycache__/` after planting or
  reverting bugs, and restart the server. The planted-bug test runs its subprocess with
  `PYTHONDONTWRITEBYTECODE=1`. Lesson: "the files are clean" and "the running code is
  clean" are different claims. The cold-agent playbook (PLAN PR 17) should include clearing
  `__pycache__`.

### 23. "528 storage_error on master" understated the damage: the DB was left corrupt
- **Where:** the PR #13 description (PLAN PR 11), "same hammer against master's code".
- **What the AI claimed:** running the gunicorn hammer against the old, unlocked code gave
  "528 × `storage_error`". That was presented as the measure of what the PR fixes.
- **Reality:** the count was the smaller half of the story. The old in-place writes left
  the **DB file permanently corrupt**: the final `scooter_db.json` didn't even parse
  (`JSONDecodeError`), so every later request would fail. The writer's own check script
  had in fact crashed on that final file and printed nothing, and the writer reported the
  counts without noticing. The reviewer's independent rerun saw **1,816** errors, so the
  number also varies a lot from run to run.
- **How it was caught:** the reviewer's rerun inspected the DB file after the load test,
  not just the response codes.
- **What changed:** the finding is recorded here, and the PR 11 verification already
  checks the final DB state (start/end invariant and an intact file). Lesson: after a load
  or chaos test, check the **end state** (does the data still load, is it consistent),
  not only the error counts. And when a verification script prints nothing, treat that
  as a failure.


### 24. A replayed idempotency key was framed as a legacy-only problem
- **Where:** PR #15 (PLAN PR 12). See the AGENTS.md "Database schema" section and the PLAN
  change log.
- **What the AI claimed:** the restore hazard was "a legacy key isn't unique over time":
  after a `git restore`, ending scooter 33 reuses `legacy-33`. The note was written as a
  property of synthesised `legacy-<id>` reservations, and PR 13 was told to "account for
  `legacy-<id>` keys repeating".
- **Reality:** restoring **any** DB snapshot replays **every** reservation in it, with the
  same `reservation_id` and `idempotency_key`, uuid ones included. With PR 13's ledger, the
  second end of a restored ride could find "already charged" and let the rider go free.
  The hazard comes from restoring state, not from the legacy shape.
- **How it was caught:** the reviewer started scooter 18 (a real uuid reservation), took a
  snapshot of the DB, ended the ride, restored the snapshot and ended it again. The same
  uuid `reservation_id` was cleared twice.
- **What changed:**
  - AGENTS.md and the change log now describe the general hazard.
  - PLAN row 13 requires ledger hits to match the ride (scooter, reservation, amount),
    treats anything else as a conflict, and adds a restore test.
  - `test_a_restored_reservation_is_charged_again_not_ridden_free` pins today's behaviour
    for both a uuid and a legacy reservation.
  - Lesson: when a hazard turns up in one shape of data, check whether it applies to every
    shape before scoping the note to the one where it was noticed.

### 25. A one-off test failure was set aside as "probably flaky" without checking for outside writers
- **Where:** PR #15 (PLAN PR 12), the writer's report of the review fixes.
- **What the AI claimed:** one `make test` run failed once (its output had been cut to the
  last line), 15 later runs were clean, and it was reported as "probably a timing-sensitive
  test or another agent; I don't think it's this change", i.e. treated as a flake.
- **Reality:** the reviewer reproduced the exact signature. Any other process that writes
  the live `scooter_db.json` or `data/records.jsonl` during `make test` (a dev server in
  the same checkout, `make run` plus `curl`, `git restore`, another agent) makes the autouse
  guard fixtures fail exactly one test, chosen at random, with "… changed during this
  test". The output was discarded, so this can't be confirmed, but it is the **likely**
  cause: other agents work in this shared checkout.
- **How it was caught:** in the re-review, the reviewer reproduced the single-error
  signature with another process writing the shared files during `make test`.
- **What changed:**
  - AGENTS.md's Test section documents the cause and says not to run the server from the
    same checkout during tests (use a scratch DB and records path, or another checkout).
  - The writer now keeps full test logs (`> log 2>&1`) instead of `tail -1`.
  - Lesson: a one-off failure followed by clean reruns is evidence about the environment,
    not proof of a flaky test. Check what else touched the shared files before calling it
    flaky, and never throw away the only failing output.

### 26. A new persisted file was added before its test isolation, and the first run wrote to the real one
- **Where:** PLAN PR 13 (`payments.py`), during development. It was caught before any
  commit.
- **What the AI did:** wrote the gateway with a default ledger path of
  `data/payments_ledger.jsonl` and ran the whole suite, confident that "the tests are
  isolated". The suite's isolation fixtures only knew about the DB and the records file.
- **Reality:** that run appended 118 test charges to the **real**
  `data/payments_ledger.jsonl`. Tests also shared one ledger, so later tests saw earlier
  tests' charges (a burst of `already_charged`/`conflict` failures that looked like logic
  bugs).
- **How it was caught:** 57 failures with odd payment outcomes led to listing `data/`, where
  a ledger the feature had only just created already had 118 lines.
- **What changed:** the stray file was moved out of `data/`. `tests/conftest.py` got an
  autouse `ledger_path` fixture (a temp ledger per test, all `PAYMENT_*` env cleared) and a
  `_guard_real_ledger` guard. AGENTS.md lists them. Lesson: whenever a change adds a new
  file the app writes, add the per-test redirect and the guard **before** the first test
  run, the way `records_path` and `_guard_real_records` were.

### 27. "A different end location gives `payment_conflict`" was claimed, but the code matched on the amount
- **Where:** PR #16 (PLAN PR 13): its description, AGENTS.md "Payments & fault
  injection", and `payments._matches`.
- **What the AI claimed:** a restored ride ended "the same way" settles with the original
  charge, and "ending it a different way (e.g. another end location, so another amount)
  is `payment_conflict`". The docs and the PR body presented this as how the code behaves.
- **Reality:** the match compared (scooter, reservation, amount), and the amount is just
  the rounded distance ridden. Two different end points at the same distance give the same
  amount. The reviewer started scooter 18 at (23, 19), took a snapshot, ended at
  (23.009, 19) (charged 997), restored, and ended at (22.991, 19), about 2 km away. The
  result was 200 `already_charged` with the first ride's `txn_id`, and the scooter saved at
  the south point: a different ride, closed for free. The tests only ever moved the end
  point in ways that also changed the distance, so they couldn't see this.
- **How it was caught:** the reviewer tried two equidistant end points after a restore.
- **What changed:** each ledger entry stores `end_lat`/`end_lng`, and ride identity is
  scooter, reservation, exact end location and amount. A test now covers the reviewer's
  equidistant pair, and mutations that drop either coordinate (or go back to amount-only)
  are caught. AGENTS.md, the PR body and PLAN row 13 are corrected. Lesson: when a claim
  says "X differs, so Y happens", test a case where X differs but the proxy the code
  actually compares does not.

### 28. The match rule itself came from a review recommendation that used a derived value
- **Where:** PR #15's review (PLAN PR 12) and the PLAN row 13 text it produced: "a ledger hit
  … counts as already charged only if it matches the ride (scooter id, reservation id,
  amount)".
- **What the AI claimed:** the reviewer agent recommended (scooter, reservation, amount) as
  the identity of a ride. The writer then implemented it verbatim, as a settled rule.
- **Reality:** the amount is a value **derived** from the ride (the rounded distance), not
  part of the ride's identity. Matching on it is what let the free ride in #27 through. The
  reviewer said so in PR #16's review: end location is what it should have said.
- **How it was caught:** the same reviewer, testing PR #16 adversarially, with equidistant
  end points.
- **What changed:** PLAN row 13 now reads "ride identity = scooter, reservation, end
  location (and amount)". Lesson for both roles: decide "is this the same transaction?"
  from the event's identity (who, which reservation, where it ended), never from a value
  computed from it, and don't carry a review's wording into code without testing its edge
  cases.

### 29. "Invariants + anomalies prove Outcome 3" overclaimed what was checked
- **Where:** PR #17 (PLAN PR 14): its description and AGENTS.md "Payment state machine".
- **What the AI claimed:** the PR presented the invariant checker (I1–I4) plus a clean
  `anomalies` run after every fault scenario and a soak as the proof that "when payment
  fails, the system never ends up in a bad state" (ACTIVITY Outcome 3).
- **Reality:** the *test suite* caught all 8 bugs the reviewer planted, but the evidence
  tools a cold agent actually has were weaker:
  - `make records ARGS="anomalies"` was silent on four of the bugs: a timeout ending the
    ride as paid, the phase-1 write removed, a retry re-pricing the ride, and a decline
    recorded as a 0-amount balance.
  - The reviewer's scenario didn't exercise a fifth (a conflict ending the ride). Running
    the old rules on this PR's scenarios, `anomalies` named only 3 of the 8.
  - The invariants checked that a charge or balance *existed*, never its amount, so a
    0-amount balance for a 1 km ride passed I1–I4.
  - "Proof" was true of the suite, not of what an agent without the code can see.
- **How it was caught:** the reviewer planted each bug and ran three checks separately:
  the suite, the invariants and `anomalies`.
- **What changed (PLAN PR 14b):**
  - New rules `ended_without_payment`, `charged_before_pending_persisted` and
    `pending_location_changed`. `decline_not_settled` now checks the amount.
  - Invariant I5 checks amounts.
  - `payment-orphans` finds requests that died mid-payment.
  - `tests/test_planted_payment_bugs.py` requires `anomalies` to name each of the 8 bugs.
  - AGENTS.md has a "What `anomalies` can't see" list.
  - Lesson: when claiming proof, say what was checked (presence, ordering, amounts) and
    with which tool. A suite that catches a bug doesn't mean the operator's tool can see
    it. Test the evidence tool against the same planted bugs as the code.

### 30. "All 8 planted bugs flagged" was presented as coverage, but the worst failure wasn't in the list
- **Where:** PR #18 (PLAN PR 14b): its description, AGENTS.md's planted-bug table, and
  `tests/test_planted_payment_bugs.py`.
- **What the AI claimed:** with every one of PR #17's 8 planted bugs named by `anomalies`,
  the cold agent's evidence tool now covered the end-ride state machine's failure modes.
- **Reality:** the reviewer planted a ninth, plausible bug: a new idempotency key per
  attempt. A charged timeout followed by a retry then charged **one ride twice**, the
  worst outcome Outcome 3 exists to rule out.
  - `anomalies` was silent.
  - The invariants misreported it as "I2 ended without a charge", because the charges
    were under keys no reservation owned.
  - The 8-bug list had been treated as the spec for coverage, rather than the failure
    classes that matter.
- **How it was caught:** the reviewer went beyond the listed bugs and asked what bug would
  produce a double charge.
- **What changed:**
  - Payment results record the key and reservation the gateway actually charged under.
  - New anomalies `payment_key_mismatch` and `double_charge` (checked across records and
    windows).
  - I3 now counts charges per `reservation_id` and flags keys no reservation owned.
  - `ledger-check` finds double charges and foreign keys straight from the ledger.
  - The bug is now planted as M9, and a decline balance under the wrong key as M10.
  - AGENTS.md names "double charge" as the failure class.
  - Lesson: start from the failure class that matters most (here, charging a ride twice).
    Plant every way you can think of to cause it, not only the bugs someone else listed.

### 31. A test sorted by request start time and called it "lock order"
- **Where:** `tests/test_db_transactions.py::test_concurrent_cycles_across_processes`
  (PLAN PR 11), the assertion "replay them in lock order".
- **What the AI claimed:** sorting scooter 18's successful writes by `ts` replays them in lock
  order, so the last one's `scooter_after` must equal the final DB.
- **Reality:** `ts` is when a request *started*. Waiters poll the lock every 5 ms, so after a
  release whichever waiter polls first wins, and a request that started later often gets
  the lock first. The reviewer measured this: with the lock held, a start followed 20 ms
  later by an end gave a start order different from the lock order in 17 of 30 trials. The
  assertion flaked on CI in PR #18.
- **How it was caught:** a CI failure on an unrelated PR. The writer diagnosed it from the
  code without fixing it on the side, and the reviewer confirmed it experimentally.
- **What changed (PLAN PR 11c):**
  - The `db_lock` decision records `acquired_at`.
  - The test and `make records ARGS="scooter <id>"` order by it.
  - `test_lock_order_not_start_order_decides_what_is_on_disk` reproduces the difference
    deterministically: a request that started first gets the lock second, and only lock
    order explains the final DB.
  - The fixed test passed 50 of 50 local runs.
  - Lesson: a comment that names a property ("lock order") isn't evidence that the code
    computes it. Order by the event you mean, and record it if it isn't recorded.

### 32. The deterministic repro delayed the request outside the code whose timing it tested
- **Where:** PR #19 (PLAN PR 11c), `test_lock_order_not_start_order_decides_what_is_on_disk`.
- **What the AI claimed:** the new test pinned the fix. Lock order is recorded by
  `acquired_at`, and ordering by it explains the final DB.
- **Reality:** the test made request A late by wrapping `acquire_db_lock` and waiting
  *before* calling it, so A never waited inside the real polling loop. The test therefore
  couldn't tell where `acquired_at` was taken. The reviewer moved the timestamp to the start
  of `acquire_db_lock`, before the wait, and all 155 relevant tests still passed. That
  mutation brings the original flake back.
- **How it was caught:** the reviewer's mutation of the timestamp's position.
- **What changed:**
  - A's process now makes every non-blocking `flock` attempt in `app` report "busy" until
    the test says go, so A really polls inside `acquire_db_lock` (`wait_ms` ≥ 50).
  - The test asserts that A's `acquired_at` is after B's, and after A's wait.
  - The timestamp-before-the-wait mutation is now caught. The test passed 30 of 30 looped
    runs.
  - Lesson: a deterministic repro must put the delay where the real race happens, inside
    the code under test, or it proves only the part downstream of the delay.

### 33. A soak test assumed random faults would always leave stuck rides
- **Where:** `tests/test_reconcile.py`, the extended soak (PLAN PR 15), while it was being
  written.
- **What the AI claimed:** after a 500-operation random-fault soak there would always be
  `payment_pending` rides left for reconcile to settle. The test asserted it as a
  precondition.
- **Reality:** riders retry pending rides too, and which ride is still pending at the end
  depends on how the processes interleave. On the second run the soak left none, so the
  test failed its own precondition. A test that sometimes has nothing to prove would have
  passed vacuously if the assertion had been weaker.
- **How it was caught:** the test's precondition assertion failed on a rerun.
- **What changed:** after the soak, the test deliberately leaves every free scooter with a
  failed end (charged and uncharged timeouts, errors) before running reconcile under the
  same random faults. Lesson: when a test's evidence depends on randomness producing a
  situation, create the situation explicitly and assert it exists.

### 34. Pattern: integrity checks keyed on a derived identifier miss bugs that change it
- **Where:** three PRs, one mistake.
  - PR #16: ride identity checked by the amount (AI_NOTES #27 and #28).
  - PR #18: double charges checked per idempotency key, not per reservation (AI_NOTES #30).
  - PR #20 (PLAN PR 15): invariant I1's "charged and also owes a balance" checked per
    idempotency key.
- **What the AI claimed, each time:** the check "covers" the property: one charge per
  ride, no paid ride that also owes.
- **Reality:** each check was keyed on a value **derived** from the business entity (the
  rounded distance, the idempotency key) rather than on the entity itself (the ride or
  reservation). A bug that changes the derived value escapes the check, and those are the
  worst bugs.
  - In PR #20 the reviewer planted "settle under a new key". Ride 18 ended both charged
    (key K) and owing a `payment_unresolved` balance (key K-r).
  - The invariants said nothing, and `anomalies` showed only the indirect hint
    `payment_key_mismatch`.
- **How it was caught:** adversarial planted bugs that change the identifier, three times
  by the same reviewer.
- **What changed:**
  - I1's charge-plus-balance check is keyed on `reservation_id`.
  - A new `charged_and_owed` anomaly works across records and keys.
  - The bug is planted as R1.
  - Lesson (the pattern): **key every "same thing?" or "at most one?" check on business
    identity (the reservation or ride), never on a value derived from it**. Then plant a
    bug that changes the derived value, to prove the check still fires.

### 35. "A charge always wins" held per timeline, not across a restore's replays
- **Where:** PLAN PR 15 (`reconcile.py`'s terminal rule) and PR 13's gateway; fixed in PLAN PR 15b.
- **What the AI claimed:** a ride is never both charged and owing: the ledger is read under
  the DB lock before any balance is recorded, so "a charge always wins".
- **Reality:** that holds inside one timeline. A DB restore replays a reservation with its
  idempotency key. A ride that was **declined** (ended with a balance), then restored and
  ended again, was **charged** by the gateway, which only remembered charges per key. A
  later restore of a snapshot taken after the decline brought the balance back: one
  reservation charged **and** owing. `anomalies`' `charged_and_owed` saw it in the records,
  but couldn't say whether the DB still held the balance.
- **How it was caught:** review of PR #20's follow-ups: the "at most one outcome per
  reservation" rule was argued for a single timeline only.
- **What changed:**
  - The gateway replays a decline (a conflict for another ride), so a replay is never charged.
  - Reconcile's `clear_paid_balance` marks a paid balance settled (kept for the audit trail).
  - `charged_and_owed` says `still_in_db` / `resolved`.
  - Tests build both variants (balance still in the DB, and wiped) with the pre-15b gateway,
    plus seeded restore-heavy random histories that must end clean after reconcile.
  - Lesson: **restores replay identities, so check every "at most one" rule across replays,
    not only within a timeline.** For each identity a restore can bring back (reservation,
    key), ask what happens when its *earlier* outcome (decline, charge, balance) meets a
    *new* attempt.
