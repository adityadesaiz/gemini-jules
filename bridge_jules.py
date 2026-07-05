#!/usr/bin/env python3
"""
bridge_jules.py — Gemini (Strategic Architect & Validator) <-> Jules
(Hands-On Execution Engineer) closed-loop pipeline. See PROTOCOL.md for
the full message-schema and state-machine spec this implements.

!! DANGER — READ BEFORE RUNNING !!
This script creates Jules sessions from instructions Gemini generates
with no human review, and on a PASS verdict runs `gh pr merge` against
your GitHub repo's default branch — also with no human review. There is
no checkpoint between "Gemini validates" and "code lands on main."

Safety nets that ARE in place (do not remove):
  - A hard iteration cap (--max-iterations, default 25).
  - A per-task retry cap (--max-retries-per-task, default 3) — a task
    that keeps failing validation halts the bridge instead of looping
    forever or merging on the last, possibly-still-broken, retry.
  - A STOP file (storage/STOP) you can create at any time to halt the
    loop cleanly after the current cycle finishes.
  - Ctrl+C (SIGINT) triggers a graceful shutdown after the current cycle.
  - Validation is gated on the bridge's OWN command execution
    (storage/master_profile.md verification commands / pipeline_config.json),
    never on Jules' self-reported success alone.
  - Every cycle's TaskSpec, ExecutionReport, VerificationResult and
    ValidationVerdict are persisted to storage/cycles/ for audit.

Nothing else is sandboxed. Run this only against a repo you have backed
up, with branch protection / CI you trust, and only after completing the
one-time setup in PROTOCOL.md (connect the GitHub source to Jules,
export GEMINI_API_KEY and JULES_API_KEY, `gh auth login`).

Usage:
    python3 bridge_jules.py
    python3 bridge_jules.py --max-iterations 10 --poll-interval 15
    python3 bridge_jules.py --max-iterations 0   # unbounded; STOP file / Ctrl+C only
"""

from __future__ import annotations

import argparse
import asyncio
import datetime
import json
import os
import signal
import subprocess
import sys
from pathlib import Path
from typing import Any, Optional

from google import genai

from jules_client import (
    AWAITING_STATES,
    JulesClient,
    TERMINAL_STATES,
    render_activity_transcript,
)

ROOT = Path(__file__).resolve().parent
STORAGE = ROOT / "storage"
CYCLES_DIR = STORAGE / "cycles"
MASTER_PROFILE = STORAGE / "master_profile.md"
PIPELINE_CONFIG = STORAGE / "pipeline_config.json"
LOG_FILE = STORAGE / "bridge.log"
STOP_FILE = STORAGE / "STOP"

MAX_CONTEXT_CHARS = 20_000
MAX_TRANSCRIPT_CHARS = 8_000
MAX_COMMAND_OUTPUT_CHARS = 4_000


def log(message: str) -> None:
    timestamp = datetime.datetime.now().isoformat(timespec="seconds")
    line = f"[{timestamp}] {message}"
    print(line, flush=True)
    with LOG_FILE.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    marker = "\n...[truncated]...\n"
    keep = limit - len(marker)
    return text[: keep // 2] + marker + text[-(keep - keep // 2):]


def load_config() -> dict:
    if not PIPELINE_CONFIG.exists():
        raise FileNotFoundError(
            f"{PIPELINE_CONFIG} is missing. Create it — see PROTOCOL.md / "
            "storage/pipeline_config.example.json for the shape."
        )
    return json.loads(PIPELINE_CONFIG.read_text(encoding="utf-8"))


def load_master_profile() -> str:
    if not MASTER_PROFILE.exists():
        raise FileNotFoundError(f"{MASTER_PROFILE} is missing.")
    return MASTER_PROFILE.read_text(encoding="utf-8")


def gemini_json(client: genai.Client, model: str, prompt: str) -> dict:
    response = client.models.generate_content(
        model=model,
        contents=prompt,
        config=genai.types.GenerateContentConfig(response_mime_type="application/json"),
    )
    text = (response.text or "").strip()
    if not text:
        raise RuntimeError("Gemini returned an empty response.")
    return json.loads(text)


def get_task_spec(client: genai.Client, model: str, profile: str, context: str, cycle_id: int) -> dict:
    prompt = f"""You are the Strategic Architect & Validator in an automated development
pipeline. An Execution Engineer (Jules) will carry out your instructions in a
cloud session against the connected GitHub repo, with no human review in
between. Because of that, your task must be precise, safe, and scoped to one
concrete, independently-verifiable increment.

# Master Profile
{profile}

# Prior cycle context (merges, failures, verdicts)
{context if context.strip() else "(This is the first cycle. There is no prior state yet.)"}

# Your task
Produce TaskSpec #{cycle_id} as a single JSON object with exactly these keys:
- "cycle_id": {cycle_id}
- "done": boolean — true only if you judge the Definition of Done in the
  Master Profile is ALREADY fully satisfied by prior merged cycles. If true,
  still provide a final verification-only task (e.g. re-run the full test
  suite) rather than leaving prompt_for_jules empty.
- "summary": short string
- "prompt_for_jules": the full, self-contained task text to hand to Jules
  verbatim. Jules has no memory of prior cycles except this text and the
  repo's own AGENTS.md — do not refer to "the previous step."
- "acceptance_criteria": array of short strings, independently checkable
- "verification_commands": array of shell commands to run after the change
  (e.g. ["npm test"]); omit or use [] to fall back to the pipeline defaults

Respond with ONLY the JSON object, no markdown fences, no commentary.
"""
    return gemini_json(client, model, prompt)


def get_validation_verdict(
    client: genai.Client,
    model: str,
    task_spec: dict,
    execution_report: dict,
    verification_result: dict,
) -> dict:
    prompt = f"""You are the Validator in an automated development pipeline. Judge this
increment strictly against its own acceptance criteria. The verification
results below were captured by the orchestrator running real commands
against a real checkout of Jules' branch — treat them as ground truth. The
activity transcript is Jules' own narrative — useful for diagnosis, not proof
of success.

# TaskSpec
{json.dumps(task_spec, indent=2)}

# ExecutionReport (Jules' session — narrative, not verified)
{json.dumps(execution_report, indent=2)}

# VerificationResult (orchestrator-run commands — ground truth)
{json.dumps(verification_result, indent=2)}

# Your task
Produce a ValidationVerdict as a single JSON object with exactly these keys:
- "cycle_id": {task_spec['cycle_id']}
- "verdict": "PASS" or "FAIL"
- "reasoning": short string explaining the verdict against the acceptance
  criteria specifically (not just "tests passed")
- "required_fixes": array of strings (empty if PASS) — concrete instructions
  for a follow-up Jules session, starting from this same branch, to fix what's
  wrong
- "definition_of_done_met": boolean — your judgment of whether the Master
  Profile's overall Definition of Done is satisfied after this verdict

Respond with ONLY the JSON object, no markdown fences, no commentary.
"""
    return gemini_json(client, model, prompt)


def answer_jules_question(client: genai.Client, model: str, task_spec: dict, question_context: str) -> str:
    prompt = f"""Jules (the Execution Engineer) is paused mid-session waiting on an answer.
You are the Architect who assigned this task — answer directly so the
session can continue unattended. Do not ask a clarifying question back.

# TaskSpec being executed
{json.dumps(task_spec, indent=2)}

# Jules' question / pause context
{question_context}

Respond with ONLY the JSON object: {{"answer": "..."}}
"""
    result = gemini_json(client, model, prompt)
    return result.get("answer", "Proceed using your best judgment.")


def run_command(cmd: str, cwd: Path) -> dict:
    proc = subprocess.run(
        cmd, shell=True, cwd=str(cwd), capture_output=True, text=True, timeout=1800
    )
    return {
        "cmd": cmd,
        "exit_code": proc.returncode,
        "output": truncate(proc.stdout + proc.stderr, MAX_COMMAND_OUTPUT_CHARS),
    }


def checkout_and_verify(branch: str, commands: list[str], cycle_id: int) -> dict:
    subprocess.run(["git", "fetch", "origin", branch], cwd=str(ROOT), check=True)
    subprocess.run(["git", "checkout", branch], cwd=str(ROOT), check=True)
    subprocess.run(["git", "pull", "origin", branch], cwd=str(ROOT), check=True)

    results = [run_command(cmd, ROOT) for cmd in commands]
    return {
        "cycle_id": cycle_id,
        "branch": branch,
        "commands": results,
        "all_passed": all(r["exit_code"] == 0 for r in results),
    }


def pr_branch_name(pr_url: str) -> str:
    out = subprocess.run(
        ["gh", "pr", "view", pr_url, "--json", "headRefName", "-q", ".headRefName"],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        check=True,
    )
    return out.stdout.strip()


def merge_pr(pr_url: str) -> None:
    subprocess.run(
        ["gh", "pr", "merge", pr_url, "--squash", "--delete-branch", "-y"],
        cwd=str(ROOT),
        check=True,
    )


async def poll_session(
    jules: JulesClient,
    gemini_client: genai.Client,
    model: str,
    task_spec: dict,
    session: dict,
    poll_interval: float,
) -> dict:
    """Poll until COMPLETED/FAILED, auto-answering any AWAITING_* pause via
    Gemini so the loop never blocks on a human."""
    session_name = session["name"]
    while True:
        session = jules.get_session(session_name)
        state = session.get("state")

        if state in AWAITING_STATES:
            activities = jules.list_all_activities(session_name)
            question_context = render_activity_transcript(activities[-3:], 2_000)
            log(f"Session {session_name} is {state} — routing to Gemini instead of blocking.")
            answer = answer_jules_question(gemini_client, model, task_spec, question_context)
            jules.send_message(session_name, answer)
        elif state in TERMINAL_STATES:
            return session

        await asyncio.sleep(poll_interval)


def build_execution_report(jules: JulesClient, session: dict, retries_used: int) -> dict:
    activities = jules.list_all_activities(session["name"])
    return {
        "session_name": session["name"],
        "session_url": session.get("url", ""),
        "final_state": session.get("state"),
        "pull_requests": session.get("outputs", []),
        "activity_transcript": render_activity_transcript(activities, MAX_TRANSCRIPT_CHARS),
        "retries_used": retries_used,
    }


def save_json(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


async def run_cycle(
    jules: JulesClient,
    gemini_client: genai.Client,
    config: dict,
    task_spec: dict,
    starting_branch: str,
    poll_interval: float,
) -> tuple[dict, dict]:
    """Runs one Jules session to completion and returns
    (execution_report, verification_result). Raises on unrecoverable
    session failure (e.g. FAILED with no PR to inspect)."""
    session = jules.create_session(
        prompt=task_spec["prompt_for_jules"],
        source=config["github_source"],
        starting_branch=starting_branch,
        title=task_spec.get("summary"),
    )
    log(f"Created Jules session {session['name']} for cycle {task_spec['cycle_id']}.")

    session = await poll_session(jules, gemini_client, config["gemini_model"], task_spec, session, poll_interval)
    execution_report = build_execution_report(jules, session, retries_used=0)

    prs = execution_report["pull_requests"]
    if session.get("state") != "COMPLETED" or not prs:
        # Nothing to verify — treat as an automatic FAIL rather than crashing,
        # so the retry path in main_loop can react to it.
        verification_result = {
            "cycle_id": task_spec["cycle_id"],
            "branch": starting_branch,
            "commands": [],
            "all_passed": False,
        }
        return execution_report, verification_result

    pr_url = prs[0]["url"]
    branch = pr_branch_name(pr_url)
    commands = task_spec.get("verification_commands") or config["verification_commands"]
    verification_result = checkout_and_verify(branch, commands, task_spec["cycle_id"])
    return execution_report, verification_result


async def main_loop(max_iterations: int, poll_interval: float, max_retries_per_task: int) -> None:
    gemini_client = genai.Client()  # reads GEMINI_API_KEY from env
    jules = JulesClient(api_key=os.environ["JULES_API_KEY"])
    config = load_config()

    stop_requested = asyncio.Event()

    def request_stop(*_args) -> None:
        log("SIGINT received — will stop after the current cycle finishes.")
        stop_requested.set()

    loop = asyncio.get_running_loop()
    try:
        loop.add_signal_handler(signal.SIGINT, request_stop)
    except NotImplementedError:
        signal.signal(signal.SIGINT, lambda *_: request_stop())

    context = ""
    cycle_id = 0
    default_branch = config["default_branch"]

    log(f"Bridge starting. max_iterations={max_iterations or 'unbounded'} "
        f"poll_interval={poll_interval}s max_retries_per_task={max_retries_per_task}")
    log(f"Create {STOP_FILE.relative_to(ROOT)} at any time to stop cleanly.")

    while True:
        cycle_id += 1
        if max_iterations and cycle_id > max_iterations:
            log(f"Reached max_iterations={max_iterations}. Stopping.")
            break
        if STOP_FILE.exists():
            log(f"STOP file detected. Stopping before cycle {cycle_id}.")
            break

        profile = load_master_profile()
        log(f"--- Cycle {cycle_id}: requesting TaskSpec from {config['gemini_model']} ---")
        task_spec = get_task_spec(gemini_client, config["gemini_model"], profile, context, cycle_id)
        save_json(CYCLES_DIR / f"{cycle_id:04d}_taskspec.json", task_spec)
        log(f"TaskSpec {cycle_id}: {task_spec.get('summary')!r} (done={task_spec.get('done')})")

        starting_branch = default_branch
        retries = 0
        verdict: Optional[dict] = None
        execution_report: Optional[dict] = None
        verification_result: Optional[dict] = None

        while retries <= max_retries_per_task:
            execution_report, verification_result = await run_cycle(
                jules, gemini_client, config, task_spec, starting_branch, poll_interval
            )
            execution_report["retries_used"] = retries
            save_json(CYCLES_DIR / f"{cycle_id:04d}_execution_report_{retries}.json", execution_report)
            save_json(CYCLES_DIR / f"{cycle_id:04d}_verification_{retries}.json", verification_result)

            verdict = get_validation_verdict(
                gemini_client, config["gemini_model"], task_spec, execution_report, verification_result
            )
            save_json(CYCLES_DIR / f"{cycle_id:04d}_verdict_{retries}.json", verdict)
            log(f"Cycle {cycle_id} attempt {retries}: verdict={verdict.get('verdict')}")

            if verdict.get("verdict") == "PASS":
                break

            retries += 1
            if retries > max_retries_per_task:
                log(f"Cycle {cycle_id} exceeded max_retries_per_task={max_retries_per_task}. Halting bridge.")
                return

            if execution_report["pull_requests"]:
                starting_branch = pr_branch_name(execution_report["pull_requests"][0]["url"])
            task_spec = dict(task_spec)
            task_spec["prompt_for_jules"] = (
                "Fix the following issues found during validation of your previous attempt "
                f"on this same branch:\n" + "\n".join(f"- {f}" for f in verdict.get("required_fixes", []))
            )

        assert verdict is not None and execution_report is not None
        pr_url = execution_report["pull_requests"][0]["url"]
        merge_pr(pr_url)
        log(f"Cycle {cycle_id} PASSED and merged: {pr_url}")

        context = truncate(
            f"## Cycle {cycle_id}\nTaskSpec: {json.dumps(task_spec)}\n"
            f"Verdict: {json.dumps(verdict)}\nMerged: {pr_url}\n\n{context}",
            MAX_CONTEXT_CHARS,
        )

        if task_spec.get("done") and verdict.get("definition_of_done_met"):
            log("Gemini confirmed Definition of Done met and verdict is PASS. Stopping.")
            break

        if stop_requested.is_set():
            log("Stop requested. Not starting another cycle.")
            break

    log("Bridge stopped.")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--max-iterations", type=int, default=25,
                         help="Hard cap on cycles. 0 = unbounded (STOP file / Ctrl+C only). Default: 25.")
    parser.add_argument("--poll-interval", type=float, default=15.0,
                         help="Seconds between Jules session status polls. Default: 15.")
    parser.add_argument("--max-retries-per-task", type=int, default=3,
                         help="Consecutive FAIL verdicts allowed per increment before halting. Default: 3.")
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    STORAGE.mkdir(parents=True, exist_ok=True)
    CYCLES_DIR.mkdir(parents=True, exist_ok=True)
    try:
        asyncio.run(main_loop(args.max_iterations, args.poll_interval, args.max_retries_per_task))
    except KeyboardInterrupt:
        log("Interrupted. Exiting.")
