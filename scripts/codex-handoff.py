#!/usr/bin/env python3
"""Codex hook glue for output-compress handoff-aware pacing.

Run from a Codex UserPromptSubmit command hook, usually with --refresh:

    python3 scripts/codex-handoff.py --refresh

The script optionally refreshes Codex usage, runs usage-pacer.py, reads the pacer
verdict, and emits Codex hook JSON only when a handoff action is needed.

It deliberately does not write memory, run git, or create scheduled tasks itself:
those are Codex host/tool responsibilities. The hook injects exact developer
context that tells Codex to persist a checkpoint and, for HANDOFF_PREP, create a
thread scheduled task / heartbeat at resume_at. This keeps the deterministic part
in Python and the privileged host action in Codex.

Environment overrides:
    OC_PACER_VERDICT             pacer verdict JSON (default /tmp/oc-pacer-verdict.json)
    OC_CODEX_HANDOFF_STATE       dedup state path (default <verdict>.codex-handoff-state)
    OC_CODEX_HANDOFF_NAME        scheduled task name prefix
    OC_CODEX_HANDOFF_PROMPT      custom resume prompt body

Exit 0 always. Broken handoff glue must not block the user's prompt.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import pathlib
import subprocess
import sys
from typing import Any


HERE = pathlib.Path(__file__).resolve().parent
VERDICT_FILE = pathlib.Path(os.environ.get("OC_PACER_VERDICT", "/tmp/oc-pacer-verdict.json"))
STATE_FILE = pathlib.Path(
    os.environ.get("OC_CODEX_HANDOFF_STATE", str(VERDICT_FILE) + ".codex-handoff-state")
)
DEFAULT_NAME = os.environ.get("OC_CODEX_HANDOFF_NAME", "output-compress resume")


def _load_hook_input() -> dict[str, Any]:
    try:
        raw = sys.stdin.read()
    except Exception:
        return {}
    if not raw.strip():
        return {}
    try:
        data = json.loads(raw)
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _load_verdict(path: pathlib.Path = VERDICT_FILE) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def _parse_utc(value: str) -> _dt.datetime | None:
    if not value:
        return None
    try:
        dt = _dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_dt.timezone.utc)
    return dt.astimezone(_dt.timezone.utc)


def _one_shot_rrule(resume_at: str) -> str:
    dt = _parse_utc(resume_at)
    if dt is None:
        return ""
    stamp = dt.strftime("%Y%m%dT%H%M%SZ")
    return f"DTSTART:{stamp}\nRRULE:FREQ=MINUTELY;COUNT=1"


def _state_key(verdict: dict[str, Any], hook_input: dict[str, Any]) -> str:
    parts = [
        str(hook_input.get("session_id", "")),
        str(verdict.get("verdict", "")),
        str(verdict.get("handoff", "")),
        str(verdict.get("resume_at", "")),
        str(verdict.get("used_pct", "")),
    ]
    return "|".join(parts)


def _seen(key: str) -> bool:
    try:
        return STATE_FILE.read_text(encoding="utf-8") == key
    except Exception:
        return False


def _remember(key: str) -> None:
    try:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        STATE_FILE.write_text(key, encoding="utf-8")
    except Exception:
        pass


def _resume_prompt(verdict: dict[str, Any], hook_input: dict[str, Any]) -> str:
    custom = os.environ.get("OC_CODEX_HANDOFF_PROMPT")
    if custom:
        return custom
    cwd = hook_input.get("cwd") or os.getcwd()
    resume_at = verdict.get("resume_at") or "the quota reset time"
    return (
        "Resume the existing task after output-compress HANDOFF_PREP. "
        f"Worktree/cwd: {cwd}. First read the persisted handoff checkpoint, "
        "then continue only the recorded next action. Re-check current git status "
        "and latest user messages before editing. Stop if the checkpoint is missing, "
        "the user changed scope, or the pacer still reports HANDOFF_HALT. "
        f"This wake was scheduled for {resume_at}."
    )


def _prep_context(verdict: dict[str, Any], hook_input: dict[str, Any]) -> str:
    resume_at = str(verdict.get("resume_at", ""))
    rrule = _one_shot_rrule(resume_at)
    name = f"{DEFAULT_NAME} {resume_at}".strip()
    prompt = _resume_prompt(verdict, hook_input)
    message = str(verdict.get("message", "")).strip()
    cwd = hook_input.get("cwd") or os.getcwd()
    return (
        "output-compress CODEX AUTO-HANDOFF: pacer emitted HANDOFF_PREP.\n"
        f"cwd: {cwd}\n"
        f"resume_at: {resume_at}\n"
        f"one_shot_rrule: {rrule}\n"
        f"scheduled_task_name: {name}\n"
        f"scheduled_task_prompt: {prompt}\n\n"
        "Required action now, before continuing broad work:\n"
        "1. Persist a durable handoff checkpoint with Goal, Done-when, tried, "
        "current git status, verification, residual risks, and next action. Use the "
        "repo's memory/checkpoint convention when present; otherwise create or update "
        "a clear handoff note in the workspace. Do not compress contract fields.\n"
        "2. Preserve work in progress according to the user's authorization and repo "
        "policy. Commit/push only when already authorized; otherwise record exact "
        "uncommitted status in the checkpoint.\n"
        "3. If the Codex automation tool is available, call it now to create a "
        "thread heartbeat / scheduled task using the one_shot_rrule, "
        "scheduled_task_name, and scheduled_task_prompt above so Codex returns to "
        "this same task after the quota reset. Prefer a thread heartbeat over a "
        "standalone cron task. If no such tool is available, explicitly notify the "
        f"user to resume at {resume_at}.\n"
        "4. After checkpoint and scheduling/notification, stop starting new work in "
        "this turn unless the user explicitly overrides the handoff.\n\n"
        f"Original pacer message: {message}"
    )


def _halt_context(verdict: dict[str, Any], hook_input: dict[str, Any]) -> str:
    message = str(verdict.get("message", "")).strip()
    cwd = hook_input.get("cwd") or os.getcwd()
    return (
        "output-compress CODEX AUTO-HANDOFF: pacer emitted HANDOFF_HALT.\n"
        f"cwd: {cwd}\n"
        "Required action now: persist a durable handoff checkpoint with Goal, "
        "Done-when, tried, current git status, verification, residual risks, and "
        "next action; then stop. Do not create another scheduled task / heartbeat. "
        "The consecutive-window circuit breaker tripped, so wait for the user to "
        "confirm continuation.\n\n"
        f"Original pacer message: {message}"
    )


def build_context(verdict: dict[str, Any], hook_input: dict[str, Any]) -> str:
    if verdict.get("handoff") == "prep" and verdict.get("resume_at"):
        return _prep_context(verdict, hook_input)
    if verdict.get("handoff") == "halt":
        return _halt_context(verdict, hook_input)
    return ""


def hook_json(context: str) -> dict[str, Any]:
    return {
        "continue": True,
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": context,
        },
    }


def _run_best_effort(cmd: list[str]) -> None:
    try:
        subprocess.run(cmd, cwd=str(HERE.parent), stdin=subprocess.DEVNULL,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       check=False)
    except Exception:
        pass


def refresh() -> None:
    fetcher = HERE / "codex-usage-fetch.py"
    pacer = HERE / "usage-pacer.py"
    if fetcher.exists():
        _run_best_effort([sys.executable, str(fetcher)])
    if pacer.exists():
        _run_best_effort([sys.executable, str(pacer), "--json"])


def self_test() -> int:
    import tempfile

    old_state = globals()["STATE_FILE"]
    with tempfile.TemporaryDirectory() as td:
        globals()["STATE_FILE"] = pathlib.Path(td) / "state"
        hook_in = {"session_id": "s1", "cwd": "/repo", "turn_id": "t1"}
        prep = {
            "verdict": "HANDOFF_PREP",
            "handoff": "prep",
            "resume_at": "2026-01-01T12:21:00Z",
            "used_pct": 95,
            "message": "prep message",
        }
        ctx = build_context(prep, hook_in)
        assert "CODEX AUTO-HANDOFF" in ctx, ctx
        assert "DTSTART:20260101T122100Z" in ctx, ctx
        assert "RRULE:FREQ=MINUTELY;COUNT=1" in ctx, ctx
        assert "Codex automation tool" in ctx, ctx
        payload = hook_json(ctx)
        assert payload["hookSpecificOutput"]["hookEventName"] == "UserPromptSubmit", payload
        key = _state_key(prep, hook_in)
        assert not _seen(key)
        _remember(key)
        assert _seen(key)
        halt = {
            "verdict": "HANDOFF_HALT",
            "handoff": "halt",
            "resume_at": "",
            "used_pct": 95,
            "message": "halt message",
        }
        hctx = build_context(halt, hook_in)
        assert "HANDOFF_HALT" in hctx and "Do not create another scheduled task" in hctx, hctx
        none = {"verdict": "AHEAD", "handoff": "", "resume_at": "", "used_pct": 60}
        assert build_context(none, hook_in) == ""
    globals()["STATE_FILE"] = old_state
    print("SELF-TEST PASS (prep-json/halt-json/one-shot-rrule/dedup/noop)")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--refresh", action="store_true",
                        help="best-effort run codex-usage-fetch.py and usage-pacer.py first")
    parser.add_argument("--dry-run", action="store_true",
                        help="print output without updating dedup state")
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    if args.self_test:
        return self_test()
    if args.refresh:
        refresh()
    hook_input = _load_hook_input()
    verdict = _load_verdict()
    if not verdict:
        return 0
    context = build_context(verdict, hook_input)
    if not context:
        return 0
    key = _state_key(verdict, hook_input)
    if not args.dry_run and _seen(key):
        return 0
    if not args.dry_run:
        _remember(key)
    print(json.dumps(hook_json(context), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    if "--self-test" in sys.argv:
        raise SystemExit(main())
    try:
        raise SystemExit(main())
    except Exception:
        raise SystemExit(0)
