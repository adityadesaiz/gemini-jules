# Gemini ↔ Jules Closed-Loop Protocol (v1)

## Roles

- **Gemini — Strategic Architect & Validator.** Never touches code or a
  filesystem. Reads state, writes two kinds of structured messages:
  a `TaskSpec` before an increment starts, and a `ValidationVerdict` after
  it finishes. Also answers ad-hoc questions Jules raises mid-session so
  the loop never stalls waiting on a human.
- **Jules — Hands-On Execution Engineer.** A cloud session-based agent
  (`jules.googleapis.com/v1alpha`) that works against a connected GitHub
  repo, on its own VM, and returns a branch/PR. It is not local — "local
  filesystem and terminal workspace" in this pipeline refers to the
  **bridge process**, not Jules itself. See "Correction" below.
- **Bridge (`bridge_jules.py`)** — the only component holding credentials
  for both APIs. It is the sole source of truth for state: every message
  either side produces is persisted to `storage/cycles/` before being
  forwarded. It also performs **independent verification** — it never
  forwards Jules' own claim of success to Gemini as fact; it checks out
  the resulting branch locally and runs the verification commands itself.

### Correction to the original framing

Jules has no "local filesystem" mode — it always executes in a cloud VM
against a GitHub source and hands back a diff/PR. The bridge is what runs
locally (this machine, this terminal). That's fine: it doesn't change the
"no manual intervention" goal, it just means the bridge — not Jules — is
the thing polling, verifying, and merging.

## Prerequisites (one-time, human, not part of the loop)

1. `git init` this repo (or point the bridge at an existing repo) and push
   it to GitHub — Jules can only work against a connected source.
2. Connect that GitHub repo as a Jules **source** once via
   [jules.google.com](https://jules.google.com) (OAuth to a repo is not
   exposed over the API, per Google's docs — this is the one human step
   that can't be automated away, done once, not per-cycle).
3. `export GEMINI_API_KEY=...` and `export JULES_API_KEY=...`.
4. `gh auth login` (bridge shells out to `gh pr merge`).
5. Fill in `storage/master_profile.md`: project goal, constraints,
   Definition of Done, repo slug/branch, and the verification commands
   the bridge should run independently on every increment (test/build/lint).

## State machine (one "cycle" = one increment)

```
 ┌─────────┐   TaskSpec    ┌──────────┐  session create   ┌───────┐
 │ Gemini  │──────────────▶│  Bridge  │──────────────────▶│ Jules │
 │ (PLAN)  │                │          │                    │(EXEC) │
 └─────────┘                │          │◀──poll get/list────│       │
                             │          │   activities       └───────┘
                             │          │      │
                     AWAITING_* state?   │      │ COMPLETED / FAILED
                     ask Gemini, relay   │◀─────┘
                     via sendMessage     │
                             │  checkout PR branch, run
                             │  verification commands
                             │  (ground truth, not trusted
                             │   from Jules' own narrative)
                             ▼
                     ExecutionReport + VerificationResult
                             │
                             ▼
 ┌─────────┐  ValidationVerdict
 │ Gemini  │◀────────────────┘
 │(VALIDATE)│
 └─────────┘
      │ PASS                      │ FAIL (retries < max)
      ▼                            ▼
 gh pr merge (auto)         new Jules session, prompt =
 next cycle's TaskSpec      required_fixes, starting_branch =
 built on top               the failed PR's branch (not main)
      │
      ▼ (Gemini sets done=true AND verdict=PASS)
   loop terminates
```

Hard stops that don't need a human but do halt the loop safely:
`storage/STOP` file, `--max-iterations`, `--max-retries-per-task`
(a task that fails validation N times in a row halts the bridge instead
of looping forever — logged to `storage/bridge.log` for later inspection,
not a silent merge).

## Message schemas

All messages are JSON, persisted verbatim to
`storage/cycles/<cycle>_<name>.json`. Gemini is called with
`response_mime_type: application/json` so its replies are parsed, not
scraped.

### 1. `TaskSpec` (Gemini → Bridge → Jules)

```json
{
  "cycle_id": 7,
  "done": false,
  "summary": "Add rate limiting middleware to the ingestion route",
  "prompt_for_jules": "Full task text, handed to Jules verbatim as the session prompt. Must be self-contained: Jules has no memory of prior cycles except what's in this prompt and the repo's AGENTS.md.",
  "acceptance_criteria": [
    "POST /ingestion returns 429 after 10 req/min per IP",
    "Existing ingestion tests still pass"
  ],
  "verification_commands": ["npm test", "npm run build"]
}
```
- `done: true` means Gemini judges the Definition of Done already met by
  prior merged cycles. The bridge still requires one final PASS verdict
  before it will actually stop — `done` is a claim, not a shortcut.
- `verification_commands` overrides the defaults in `master_profile.md`
  for this increment only (e.g. a cycle that touches only docs may skip
  the test suite).

### 2. `ExecutionReport` (assembled by Bridge from Jules' session)

```json
{
  "cycle_id": 7,
  "session_name": "sessions/abc123",
  "session_url": "https://jules.google.com/session/abc123",
  "final_state": "COMPLETED",
  "pull_requests": [
    {"url": "https://github.com/org/repo/pull/42", "title": "...", "description": "..."}
  ],
  "activity_transcript": "truncated concatenation of sessions.activities.list, newest last",
  "retries_used": 0
}
```

### 3. `VerificationResult` (Bridge only — ground truth, never authored by either model)

```json
{
  "cycle_id": 7,
  "branch": "jules/rate-limiting-abc123",
  "commands": [
    {"cmd": "npm test", "exit_code": 0, "output": "truncated stdout+stderr"},
    {"cmd": "npm run build", "exit_code": 0, "output": "..."}
  ],
  "all_passed": true
}
```
This is what actually gates merges. Jules' own narrative
(`ExecutionReport.activity_transcript`) is given to Gemini for context and
diagnosis, but `all_passed` here is computed by the bridge running real
commands against a real checkout — the one part of the loop that isn't
"another model's word for it."

### 4. `ValidationVerdict` (Gemini → Bridge)

```json
{
  "cycle_id": 7,
  "verdict": "FAIL",
  "reasoning": "Tests pass but the rate limit is keyed on a global counter, not per-IP, contradicting acceptance criterion 1.",
  "required_fixes": [
    "Key the limiter by request IP (req.ip), not a single shared counter"
  ],
  "definition_of_done_met": false
}
```
On `FAIL`, `required_fixes` becomes the `prompt_for_jules` for a **new**
Jules session whose `startingBranch` is the failed attempt's own branch
(not `main`) — so the fix session continues from the broken code instead
of restarting blind. On `PASS`, the bridge runs `gh pr merge --squash` and
the next `TaskSpec` is generated with context = "cycle 7 merged".

### 5. Mid-flight questions (Jules → Gemini, doesn't wait for a human)

If `sessions.get` ever returns `AWAITING_USER_FEEDBACK` or
`AWAITING_PLAN_APPROVAL`, the bridge extracts the latest activity, asks
Gemini a small structured question (`{"question": "...", "session_context": "..."}`
→ `{"answer": "..."}`), and relays the answer via
`sessions.sendMessage`. This is what makes the loop actually
unattended — without it, any clarifying question from Jules would hang
forever waiting on a human.

## Storage layout

```
storage/
  master_profile.md       # standing brief: goal, constraints, DoD, repo, verify cmds
  STOP                     # create to halt after current cycle
  bridge.log               # human-readable running log
  cycles/
    0007_taskspec.json
    0007_execution_report.json
    0007_verification.json
    0007_verdict.json
```

## Why this satisfies "no manual intervention"

- Plan approval is disabled (`requirePlanApproval: false` and
  `automationMode: AUTO_CREATE_PR`) so Jules never blocks on a human
  approving its plan.
- Mid-session questions are routed to Gemini, not a human.
- Validation is a real gate (independent command execution), not a
  rubber stamp — so "no manual intervention" doesn't degrade into "no
  intervention at all," which would just auto-merge whatever Jules wrote.
- The only human steps are the one-time bootstrap (connect the GitHub
  source, set two API keys) — everything inside the loop is
  machine-to-machine.
- Runaway loops are bounded (`max-iterations`, `max-retries-per-task`,
  `STOP` file) so failure mode is "halts and logs," not "loops forever
  burning API quota" or "merges broken code because retries ran out."
