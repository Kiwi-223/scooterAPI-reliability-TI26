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

