# Activity: Make it debuggable by an agent that has never seen it

You are going to use AI to build almost all of this. That's expected. The question this activity asks is different:

> **When something breaks, can an AI agent that has never seen your project figure out why, using only what your system exposes?**

AI can only debug what it can observe. A log line printed to a terminal, an error shown on a screen, or a fact that lives in your head is invisible to an agent that shows up later. This activity is about building the things that make your system legible to the next contributor, whether that's a human on day one or an agent with no chat history.

There's a second half: you'll be running agents with real power over your machine and your repo. You'll also decide what your agent is **not** allowed to do.

## The starting point

This repo is a small scooter-rental API written in 2019 (Python + Flask, JSON file as the database). It is old, untested, and has never been run by anyone in your cohort. That's on purpose. Inheriting code nobody remembers is the normal state of the job.

**Bring your own app instead (optional).** You can do this activity on a project you've already built (for example, your app from the full-stack sessions) instead of the scooter API. It qualifies if it has (a) state that changes over time and (b) at least one step that can fail, such as a payment, an external API call, or a write that can partially complete. The outcomes below are the same; translate "scooter" and "payment" to your app's equivalents.

No specific language, framework, test library, or tool is required. Pick whatever you and your agent are fastest in. You can rewrite the whole thing if you want.

## The acceptance test: the cold agent

Everything below is graded by one test, run live in the demo session:

1. Your repo gets a bug planted in it that you haven't seen.
2. A **fresh** agent session is started in your repo: no chat history, no hints from you. It gets only a one-sentence symptom report, like a user would file: *"I ended my ride on scooter 18 but it still shows as reserved."*
3. The agent can use anything your repo provides (docs, scripts, tests, tools, the data your system recorded).
4. It passes if, within the time limit, it:
   - names the root cause,
   - backs that up with **evidence from what the system recorded**, not just a guess from reading code, and
   - writes a failing test that reproduces the bug.

You can't help it once it starts. If it gets lost, ask yourself what it was missing.

## Required outcomes

These describe *what* your system must be able to do, not how to build it.

**0. It runs, and onboarding is written down.**
Get the app running on a current toolchain. As you go, have your agent maintain an `AGENTS.md` (or `CLAUDE.md`) that tells the next agent how to set up, run, test, and find the recorded data. Write it for a reader with zero context.

**1. Every request leaves a record you can query later.**
After a request finishes, an agent must be able to find out what happened: what was asked, what the system did, what it decided, and how it ended. The record is persisted (it survives a restart), machine-readable, and lets you follow one request or one scooter end-to-end.

**2. Failures have names.**
Every way the system can fail maps to one of a fixed set of failure kinds with stable identifiers. Every failed response and every failure record carries one. An agent should be able to ask "how many of each kind in the last hour?" without parsing English.

**3. Payment can fail, and nothing gets corrupted when it does.**
Right now payment always succeeds. Make it able to fail (declines, timeouts, whatever you think is realistic), and make it **reproducible on demand** so you or an agent can trigger the exact same failure again. When payment fails, the system never ends up in a bad state: no scooter stuck reserved forever, no charge for a ride that didn't end. Prove it.

**4. An agent can ask your system questions.**
Give agents a tool interface over your recorded data and system state, so they can ask things like "what failed recently?", "what happened to scooter 18?", or "run the tests" without grepping raw files. An MCP server is the obvious choice. Anything an agent can call as a tool counts.

**5. One command proves it works.**
A test suite that runs with a single documented command and covers the failure paths, not just the happy path.

**6. Your agent has boundaries.**
Decide what your agent is allowed to do in this repo, and enforce it with configuration, not good intentions. Check the config into the repo and add a section to `AGENTS.md` explaining your choices. Think about:
- **Blast radius:** what could it delete, push, overwrite, or spend? What's the worst thing a confused agent could do here?
- **Permissions:** what commands and tools are allowed, denied, or need approval?
- **Isolation:** does it run in a container, a sandbox, or a throwaway branch?
- **Reversibility:** if it does something wrong, how do you undo it?
- **Budget:** how do you cap tokens, time, or money for one task?

If you've been running with `--dangerously-skip-permissions`, this is where you replace that with something you'd trust.

**7. `AI_NOTES.md`: what the AI got wrong.**
A short, honest log of where your agent was confidently wrong and how you caught it (a test, a log, a review, a gut feeling). Three good entries beat ten vague ones.

## Stretch goals

There's no ceiling here. Nobody is expected to finish all of these.

- **Self-healing loop.** Symptom report in, pull request out: the agent investigates, writes the failing test, fixes it, gets tests green, and opens a PR. A human only reviews.
- **Fault injection beyond payment.** Make other things fail on purpose (slow responses, a write that dies halfway, a dependency that's down) with a fixed random seed so every failure can be replayed exactly.
- **Concurrency.** Two people try to reserve the same scooter at the same instant: exactly one wins, every time. Prove it with a test.
- **Safe retries.** A client retries "end ride" because the network dropped: they're charged exactly once.
- **Agent check in CI.** Every pull request runs your tests plus an automated agent review, and the result shows up on the PR.
- **An AI feature with an eval harness.** Add something that uses an LLM, such as triaging free-text damage reports ("scratched fender, still rideable") into categories and severity. Then build an evaluation that tells you whether a prompt change made it better or worse, when there are many acceptable answers. This is the hardest problem here and an open one in industry.

## What to bring to the demo session

- Your repo URL, shared ahead of time so a bug can be planted.
- Be ready to start a cold agent session on screen.
- 3 minutes on your most interesting `AI_NOTES.md` entry or guardrail decision.
