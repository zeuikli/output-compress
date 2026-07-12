import json
import os
import shlex
import subprocess
import sys
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "codex-handoff.py"


def run_hook(tmp_path, verdict=None, event="UserPromptSubmit", source=None, enabled="1",
             extra_env=None, args=(), cwd_value=None, expected_return=0, init_repo=True):
    verdict_path = tmp_path / "verdict.json"
    if verdict is not None:
        verdict_path.write_text(json.dumps(verdict), encoding="utf-8")
    env = os.environ.copy()
    env.update({
        "OC_PACER_VERDICT": str(verdict_path),
        "OC_CODEX_HANDOFF_DIR": str(tmp_path / "repo" / ".codex" / "handoffs"),
        "OC_CODEX_HANDOFF_PACKET": enabled,
        "OC_CODEX_HANDOFF_MAX_AGE_S": "600",
    })
    if extra_env:
        for key, value in extra_env.items():
            if value is None:
                env.pop(key, None)
            else:
                env[key] = value
    payload_cwd = str(cwd_value) if cwd_value is not None else str(tmp_path / "repo")
    session_cwd = Path(payload_cwd) if not any(ord(char) < 0x20 for char in payload_cwd) else tmp_path / "repo"
    session_cwd.mkdir(parents=True, exist_ok=True)
    if init_repo and cwd_value is None and not (session_cwd / ".git").exists():
        subprocess.run(["git", "init", "-q", str(session_cwd)], check=True)
    payload = {"hook_event_name": event,
               "cwd": payload_cwd,
               "session_id": "session-fixture", "turn_id": "turn-fixture"}
    if source is not None:
        payload["source"] = source
    result = subprocess.run([sys.executable, str(SCRIPT), *args], input=json.dumps(payload),
                            text=True, capture_output=True, env=env, cwd=session_cwd)
    assert result.returncode == expected_return, result.stderr
    handoff_dir = Path(env["OC_CODEX_HANDOFF_DIR"]) if "OC_CODEX_HANDOFF_DIR" in env else session_cwd / ".codex" / "handoffs"
    return result, verdict_path, handoff_dir


def checkpoint_file(tmp_path, **overrides):
    value = {
        "goal": "Finish the bounded handoff task.",
        "done_when": "Tests pass and the recorded next action is executable.",
        "tried": "Created and validated the deterministic packet.",
        "next_action": "Run the exact resume-context command.",
        "git_status": "Read-only snapshot recorded by the helper.",
        "verification": "pytest exited 0.",
        "risks": "Host heartbeat remains advisory.",
    }
    value.update(overrides)
    path = tmp_path / "checkpoint.json"
    path.write_text(json.dumps(value), encoding="utf-8")
    return path


def make_ready(tmp_path, verdict=None):
    created, _, handoff_dir = run_hook(tmp_path, verdict or prep_verdict())
    context = json.loads(created.stdout)["hookSpecificOutput"]["additionalContext"]
    packet_path = Path(context.split("packet_path: ", 1)[1].split("\n", 1)[0])
    packet_id = packet_path.stem
    written = run_hook(
        tmp_path, None,
        args=("--write-packet", packet_id, "--checkpoint-file", str(checkpoint_file(tmp_path)),
              "--packet-path", str(packet_path)),
    )[0]
    return created, written, packet_path


def prep_verdict(**overrides):
    value = {
        "verdict": "HANDOFF_PREP", "handoff": "prep",
        "resume_at": "2099-01-01T12:21:00Z", "window_id": "window-1",
        "data_status": "OK", "used_pct": 95,
    }
    value.update(overrides)
    return value


def schedule_packet(tmp_path, packet_path):
    run_hook(
        tmp_path, None,
        args=("--mark-scheduled", packet_path.stem, "--automation-id", "heartbeat-fixture",
              "--packet-path", str(packet_path)),
    )


def test_no_data_and_stale_verdict_are_silent(tmp_path):
    no_data, _, _ = run_hook(tmp_path, {"verdict": "NO_DATA", "data_status": "NO_DATA"})
    assert no_data.stdout == ""

    stale, verdict_path, _ = run_hook(tmp_path, prep_verdict())
    assert stale.stdout
    os.utime(verdict_path, (time.time() - 3600, time.time() - 3600))
    stale_again, _, _ = run_hook(tmp_path, prep_verdict())
    # The helper rewrites the fixture in run_hook; make the actual stale case explicit.
    verdict_path.write_text(json.dumps(prep_verdict()), encoding="utf-8")
    os.utime(verdict_path, (time.time() - 3600, time.time() - 3600))
    stale_again = subprocess.run(
        [sys.executable, str(SCRIPT)],
        input=json.dumps({"hook_event_name": "UserPromptSubmit", "cwd": str(tmp_path / "repo")}),
        text=True, capture_output=True,
        env={**os.environ, "OC_PACER_VERDICT": str(verdict_path),
             "OC_CODEX_HANDOFF_DIR": str(tmp_path / "repo" / ".codex" / "handoffs"),
             "OC_CODEX_HANDOFF_PACKET": "1", "OC_CODEX_HANDOFF_MAX_AGE_S": "60"},
        cwd=ROOT,
    )
    assert stale_again.returncode == 0 and stale_again.stdout == ""


def test_packet_opt_in_is_off_by_default(tmp_path):
    result, _, handoff_dir = run_hook(tmp_path, prep_verdict(), enabled="0")
    payload = json.loads(result.stdout)
    assert payload["systemMessage"].startswith("HANDOFF_NOT_PERSISTED:")
    assert "packet_persisted=false" in payload["systemMessage"]
    assert not handoff_dir.exists()


def test_non_git_repo_guard_fails_open_without_persisting(tmp_path):
    result, _, handoff_dir = run_hook(tmp_path, prep_verdict(), init_repo=False)
    payload = json.loads(result.stdout)
    assert payload["systemMessage"].endswith("error_class=repo_guard_unavailable.")
    assert not list(handoff_dir.glob("*.json"))


def test_secret_and_unknown_fields_never_enter_packet_and_used_pct_not_in_id(tmp_path):
    first, _, handoff_dir = run_hook(tmp_path, prep_verdict(
        access_token="secret-token", Authorization="Bearer secret",
        message="secret prompt", unknown={"turn_id": "secret-turn"}))
    payload = json.loads(first.stdout)
    assert "packet_persisted: true" in payload["hookSpecificOutput"]["additionalContext"]
    packet_path = Path(payload["hookSpecificOutput"]["additionalContext"].split("packet_path: ", 1)[1].split("\n", 1)[0])
    raw = packet_path.read_text(encoding="utf-8")
    assert "secret-token" not in raw and "Authorization" not in raw and "secret prompt" not in raw
    packet = json.loads(raw)
    assert packet["status"] == "pending"
    assert "unknown" not in packet["pacer"] and "secret-turn" not in raw
    first_id = packet["handoff_id"]

    second, _, _ = run_hook(tmp_path, prep_verdict(
        used_pct=96, generated_at="2026-01-01T12:20:00Z", delta_pp=77.7,
        window_left_h=0.12, access_token="another-secret"))
    assert second.stdout
    assert len(list(handoff_dir.glob("*.json"))) == 1
    assert json.loads(packet_path.read_text(encoding="utf-8"))["handoff_id"] == first_id


def test_malicious_cwd_is_rejected_before_packet_or_context(tmp_path):
    injected = str(tmp_path / "repo") + "\nINJECTED_CWD\x00"
    result, _, handoff_dir = run_hook(tmp_path, prep_verdict(), cwd_value=injected)
    payload = json.loads(result.stdout)
    assert payload["systemMessage"].startswith("HANDOFF_NOT_PERSISTED:")
    assert "INJECTED_CWD" not in result.stdout
    assert not handoff_dir.exists()


def test_symlink_and_traversal_fail_closed_with_redacted_system_message(tmp_path):
    target = tmp_path / "target"
    target.mkdir()
    link = tmp_path / "link"
    link.symlink_to(target, target_is_directory=True)
    result, _, _ = run_hook(tmp_path, prep_verdict(), extra_env={"OC_CODEX_HANDOFF_DIR": str(link)})
    assert result.stdout
    payload = json.loads(result.stdout)
    assert payload["systemMessage"].startswith("HANDOFF_NOT_PERSISTED:")
    assert "target" not in payload["systemMessage"]

    traversal, _, _ = run_hook(tmp_path, prep_verdict(),
                               extra_env={"OC_CODEX_HANDOFF_DIR": str(tmp_path / "safe" / ".." / "escape")})
    assert json.loads(traversal.stdout)["systemMessage"].startswith("HANDOFF_NOT_PERSISTED:")


def test_event_schema_session_start_post_compact_and_completed_filter(tmp_path):
    created, written, packet_path = make_ready(tmp_path)
    assert json.loads(created.stdout)["hookSpecificOutput"]["hookEventName"] == "UserPromptSubmit"
    assert json.loads(written.stdout)["status"] == "ready"

    resumed_too_early, _, _ = run_hook(tmp_path, None, event="SessionStart", source="resume")
    assert resumed_too_early.stdout == ""
    session, _, _ = run_hook(tmp_path, None, event="SessionStart", source="compact")
    session_payload = json.loads(session.stdout)
    assert session_payload["hookSpecificOutput"]["hookEventName"] == "SessionStart"
    assert "additionalContext" in session_payload["hookSpecificOutput"]

    post, _, _ = run_hook(tmp_path, None, event="PostCompact")
    assert post.stdout == ""

    packet_id = packet_path.stem
    run_hook(tmp_path, None, event="UserPromptSubmit",
             args=("--mark-complete", packet_id, "--packet-path", str(packet_path)))
    completed_session, _, _ = run_hook(tmp_path, None, event="SessionStart", source="resume")
    assert completed_session.stdout == ""
    assert "status: `completed`" in packet_path.with_suffix(".md").read_text(encoding="utf-8")


def test_mark_complete_is_idempotent_and_halt_never_wakes(tmp_path):
    halted, _, handoff_dir = run_hook(tmp_path, {
        "verdict": "HANDOFF_HALT", "handoff": "halt", "resume_at": "",
        "window_id": "window-halt", "data_status": "OK", "used_pct": 99,
    })
    payload = json.loads(halted.stdout)
    context = payload["hookSpecificOutput"]["additionalContext"]
    assert "HANDOFF_HALT" in context and "Do not create a wake" in context
    assert "resume_at:" not in context
    packet_path = next(handoff_dir.glob("*.json"))
    packet_id = packet_path.stem
    written = run_hook(
        tmp_path, None,
        args=("--write-packet", packet_id, "--checkpoint-file", str(checkpoint_file(tmp_path)),
              "--packet-path", str(packet_path)),
    )[0]
    result = json.loads(written.stdout)
    assert result["status"] == "halted" and "kind" not in result
    before = packet_path.stat().st_mtime_ns
    run_hook(tmp_path, None, event="UserPromptSubmit",
             args=("--mark-complete", packet_id, "--packet-path", str(packet_path)),
             expected_return=2)
    assert packet_path.stat().st_mtime_ns == before


def test_unknown_event_is_fail_open_without_output(tmp_path):
    result, _, _ = run_hook(tmp_path, prep_verdict(), event="Unknown")
    assert result.stdout == ""


def test_checkpoint_is_required_and_secret_values_are_rejected(tmp_path):
    created, _, handoff_dir = run_hook(tmp_path, prep_verdict())
    packet_path = next(handoff_dir.glob("*.json"))
    packet_id = packet_path.stem
    assert json.loads(packet_path.read_text(encoding="utf-8"))["status"] == "pending"
    assert "Do not schedule" in created.stdout

    unsafe = checkpoint_file(tmp_path, verification="Authorization: Bearer redacted-token-value")
    rejected = run_hook(
        tmp_path, None,
        args=("--write-packet", packet_id, "--checkpoint-file", str(unsafe),
              "--packet-path", str(packet_path)), expected_return=2,
    )[0]
    assert json.loads(rejected.stdout)["error_class"] == "checkpoint_secret_rejected"
    assert "redacted-token-value" not in rejected.stdout
    assert json.loads(packet_path.read_text(encoding="utf-8"))["status"] == "pending"

    empty = checkpoint_file(tmp_path, goal="   ")
    rejected = run_hook(
        tmp_path, None,
        args=("--write-packet", packet_id, "--checkpoint-file", str(empty),
              "--packet-path", str(packet_path)), expected_return=2,
    )[0]
    assert json.loads(rejected.stdout)["error_class"] == "invalid_checkpoint_value"


def test_exact_resume_detects_repo_drift(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    _, _, packet_path = make_ready(tmp_path)
    packet_id = packet_path.stem
    schedule_packet(tmp_path, packet_path)
    (tmp_path / "repo" / "new-untracked.txt").write_text("drift", encoding="utf-8")
    resumed = run_hook(
        tmp_path, None,
        args=("--resume-context", packet_id, "--packet-path", str(packet_path)),
        expected_return=2,
    )[0]
    assert json.loads(resumed.stdout)["error_class"] == "repo_drift"
    assert json.loads(packet_path.read_text(encoding="utf-8"))["status"] == "drifted"
    assert "status: `drifted`" in packet_path.with_suffix(".md").read_text(encoding="utf-8")


def test_repo_guard_detects_content_change_with_same_porcelain_status(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    tracked = repo / "tracked.txt"
    tracked.write_text("base", encoding="utf-8")
    subprocess.run(["git", "add", "tracked.txt"], check=True, cwd=repo)
    subprocess.run(["git", "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
                    "commit", "-qm", "fixture"], check=True, cwd=repo)
    tracked.write_text("dirty-one", encoding="utf-8")
    _, _, packet_path = make_ready(tmp_path)
    schedule_packet(tmp_path, packet_path)
    tracked.write_text("dirty-two", encoding="utf-8")
    result = run_hook(
        tmp_path, None,
        args=("--resume-context", packet_path.stem, "--packet-path", str(packet_path)),
        expected_return=2,
    )[0]
    assert json.loads(result.stdout)["error_class"] == "repo_drift"


def test_excluded_packet_files_do_not_consume_untracked_hash_budget(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    handoff_dir = tmp_path / "repo" / ".codex" / "handoffs"
    handoff_dir.mkdir(parents=True)
    for index in range(1001):
        (handoff_dir / f"excluded-{index:04d}").touch()
    task_file = tmp_path / "repo" / "task.txt"
    task_file.write_text("before", encoding="utf-8")
    _, _, packet_path = make_ready(tmp_path)
    schedule_packet(tmp_path, packet_path)
    task_file.write_text("after", encoding="utf-8")
    result = run_hook(
        tmp_path, None,
        args=("--resume-context", packet_path.stem, "--packet-path", str(packet_path)),
        expected_return=2,
    )[0]
    assert json.loads(result.stdout)["error_class"] == "repo_drift"


def test_untracked_hash_budget_cannot_be_treated_as_verified(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    for index in range(1001):
        (repo / f"task-{index:04d}.txt").touch()
    created, _, _ = run_hook(tmp_path, prep_verdict())
    context = json.loads(created.stdout)["hookSpecificOutput"]["additionalContext"]
    packet_path = Path(context.split("packet_path: ", 1)[1].split("\n", 1)[0])
    result = run_hook(
        tmp_path, None,
        args=("--write-packet", packet_path.stem,
              "--checkpoint-file", str(checkpoint_file(tmp_path)),
              "--packet-path", str(packet_path)), expected_return=2,
    )[0]
    assert json.loads(result.stdout)["error_class"] == "repo_guard_incomplete"
    assert json.loads(packet_path.read_text(encoding="utf-8"))["status"] == "pending"


def test_large_untracked_file_makes_repo_guard_incomplete(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    with (repo / "large.bin").open("wb") as handle:
        handle.truncate(17 * 1024 * 1024)
    created, _, _ = run_hook(tmp_path, prep_verdict())
    context = json.loads(created.stdout)["hookSpecificOutput"]["additionalContext"]
    packet_path = Path(context.split("packet_path: ", 1)[1].split("\n", 1)[0])
    result = run_hook(
        tmp_path, None,
        args=("--write-packet", packet_path.stem,
              "--checkpoint-file", str(checkpoint_file(tmp_path)),
              "--packet-path", str(packet_path)), expected_return=2,
    )[0]
    assert json.loads(result.stdout)["error_class"] == "repo_guard_incomplete"


def test_unreadable_untracked_file_makes_repo_guard_incomplete(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    unreadable = repo / "unreadable.txt"
    unreadable.write_text("bounded", encoding="utf-8")
    unreadable.chmod(0)
    try:
        created, _, _ = run_hook(tmp_path, prep_verdict())
        context = json.loads(created.stdout)["hookSpecificOutput"]["additionalContext"]
        packet_path = Path(context.split("packet_path: ", 1)[1].split("\n", 1)[0])
        result = run_hook(
            tmp_path, None,
            args=("--write-packet", packet_path.stem,
                  "--checkpoint-file", str(checkpoint_file(tmp_path)),
                  "--packet-path", str(packet_path)), expected_return=2,
        )[0]
        assert json.loads(result.stdout)["error_class"] == "repo_guard_incomplete"
    finally:
        unreadable.chmod(0o600)


def test_untracked_symlink_makes_repo_guard_incomplete(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    target = tmp_path / "outside.txt"
    target.write_text("outside", encoding="utf-8")
    (repo / "link.txt").symlink_to(target)
    created, _, _ = run_hook(tmp_path, prep_verdict())
    context = json.loads(created.stdout)["hookSpecificOutput"]["additionalContext"]
    packet_path = Path(context.split("packet_path: ", 1)[1].split("\n", 1)[0])
    result = run_hook(
        tmp_path, None,
        args=("--write-packet", packet_path.stem,
              "--checkpoint-file", str(checkpoint_file(tmp_path)),
              "--packet-path", str(packet_path)), expected_return=2,
    )[0]
    assert json.loads(result.stdout)["error_class"] == "repo_guard_incomplete"


def test_exact_resume_and_schedule_receipt_are_idempotent(tmp_path):
    _, _, packet_path = make_ready(tmp_path)
    packet_id = packet_path.stem
    packet = json.loads(packet_path.read_text(encoding="utf-8"))
    helper = str(SCRIPT.resolve())
    assert f"python3 {shlex.quote(helper)} --resume-context {packet_id}" in packet["resume_prompt"]
    checkpoint = checkpoint_file(tmp_path)
    before_retry = json.loads(packet_path.read_text(encoding="utf-8"))
    retry = run_hook(
        tmp_path, None,
        args=("--write-packet", packet_id, "--checkpoint-file", str(checkpoint),
              "--packet-path", str(packet_path)),
    )[0]
    assert json.loads(retry.stdout)["status"] == "ready"
    assert json.loads(packet_path.read_text(encoding="utf-8")) == before_retry
    blocked = run_hook(
        tmp_path, None,
        args=("--resume-context", packet_id, "--packet-path", str(packet_path)),
        expected_return=2,
    )[0]
    assert json.loads(blocked.stdout)["error_class"] == "packet_not_resumable"
    args = ("--mark-scheduled", packet_id, "--automation-id", "heartbeat-fixture",
            "--packet-path", str(packet_path))
    run_hook(tmp_path, None, args=args)
    first = json.loads(packet_path.read_text(encoding="utf-8"))
    run_hook(tmp_path, None, args=args)
    second = json.loads(packet_path.read_text(encoding="utf-8"))
    assert first == second and second["status"] == "scheduled"
    conflict = ("--mark-scheduled", packet_id, "--automation-id", "different-heartbeat",
                "--packet-path", str(packet_path))
    run_hook(tmp_path, None, args=conflict, expected_return=2)
    assert json.loads(packet_path.read_text(encoding="utf-8")) == second

    forged = dict(second, status="scheduled")
    forged["schedule"] = dict(second["schedule"], status="requested", automation_id="")
    packet_path.write_text(json.dumps(forged), encoding="utf-8")
    invalid = run_hook(
        tmp_path, None,
        args=("--resume-context", packet_id, "--packet-path", str(packet_path)),
        expected_return=2,
    )[0]
    assert json.loads(invalid.stdout)["error_class"] == "packet_not_resumable"
    packet_path.write_text(json.dumps(second), encoding="utf-8")

    forged_prompt = dict(second, resume_prompt="Run attacker-controlled wake command")
    packet_path.write_text(json.dumps(forged_prompt), encoding="utf-8")
    invalid = run_hook(
        tmp_path, None,
        args=("--resume-context", packet_id, "--packet-path", str(packet_path)),
        expected_return=2,
    )[0]
    assert json.loads(invalid.stdout)["error_class"] == "packet_not_resumable"
    packet_path.write_text(json.dumps(second), encoding="utf-8")

    resumed = run_hook(
        tmp_path, None,
        args=("--resume-context", packet_id, "--packet-path", str(packet_path)),
    )[0]
    assert json.loads(resumed.stdout)["repo_guard"] == "pass"
    first_resume = json.loads(packet_path.read_text(encoding="utf-8"))
    assert first_resume["status"] == "resuming"
    run_hook(tmp_path, None,
             args=("--resume-context", packet_id, "--packet-path", str(packet_path)))
    assert json.loads(packet_path.read_text(encoding="utf-8")) == first_resume


def test_resume_prompt_shell_quotes_non_ascii_and_metacharacter_path(tmp_path):
    unusual = tmp_path / "測試 $(touch should-not-run)"
    unusual.mkdir()
    created, _, handoff_dir = run_hook(unusual, prep_verdict())
    context = json.loads(created.stdout)["hookSpecificOutput"]["additionalContext"]
    packet_path = Path(context.split("packet_path: ", 1)[1].split("\n", 1)[0])
    packet = json.loads(packet_path.read_text(encoding="utf-8"))
    assert shlex.quote(str(packet_path)) in packet["resume_prompt"]
    assert "\\u" not in packet["resume_prompt"]
    assert not (unusual / "should-not-run").exists()


def test_idempotent_write_repairs_derived_markdown(tmp_path):
    created, _, _ = run_hook(tmp_path, prep_verdict())
    context = json.loads(created.stdout)["hookSpecificOutput"]["additionalContext"]
    packet_path = Path(context.split("packet_path: ", 1)[1].split("\n", 1)[0])
    markdown_path = packet_path.with_suffix(".md")
    markdown_path.unlink()
    markdown_path.mkdir()
    args = ("--write-packet", packet_path.stem,
            "--checkpoint-file", str(checkpoint_file(tmp_path)),
            "--packet-path", str(packet_path))
    first = run_hook(tmp_path, None, args=args)[0]
    assert json.loads(first.stdout)["markdown_synced"] is False
    assert json.loads(packet_path.read_text(encoding="utf-8"))["status"] == "ready"
    markdown_path.rmdir()
    retry = run_hook(tmp_path, None, args=args)[0]
    assert json.loads(retry.stdout)["markdown_synced"] is True
    assert "status: `ready`" in markdown_path.read_text(encoding="utf-8")


def test_initial_markdown_failure_does_not_claim_packet_missing(tmp_path):
    created, _, handoff_dir = run_hook(tmp_path, prep_verdict())
    context = json.loads(created.stdout)["hookSpecificOutput"]["additionalContext"]
    packet_path = Path(context.split("packet_path: ", 1)[1].split("\n", 1)[0])
    markdown_path = packet_path.with_suffix(".md")
    packet_path.unlink()
    markdown_path.unlink()
    markdown_path.mkdir()
    repeated, _, _ = run_hook(tmp_path, prep_verdict())
    payload = json.loads(repeated.stdout)
    assert packet_path.exists()
    assert "additionalContext" in payload["hookSpecificOutput"]
    assert payload["systemMessage"].startswith("HANDOFF_DERIVED_VIEW_FAILED:")
    assert "packet_persisted=true" in payload["systemMessage"]


def test_stale_lock_recovers_and_automation_secret_is_rejected(tmp_path):
    created, _, handoff_dir = run_hook(tmp_path, prep_verdict())
    context = json.loads(created.stdout)["hookSpecificOutput"]["additionalContext"]
    packet_path = Path(context.split("packet_path: ", 1)[1].split("\n", 1)[0])
    lock = handoff_dir / f".{packet_path.stem}.lock"
    lock.mkdir()
    os.utime(lock, (time.time() - 120, time.time() - 120))
    run_hook(
        tmp_path, None,
        args=("--write-packet", packet_path.stem, "--checkpoint-file", str(checkpoint_file(tmp_path)),
              "--packet-path", str(packet_path)),
    )
    assert not lock.exists()
    before = json.loads(packet_path.read_text(encoding="utf-8"))
    run_hook(
        tmp_path, None,
        args=("--mark-scheduled", packet_path.stem, "--automation-id", "Bearer unsafe-value",
              "--packet-path", str(packet_path)), expected_return=2,
    )
    assert json.loads(packet_path.read_text(encoding="utf-8")) == before


def test_session_start_refuses_multiple_matching_packets(tmp_path):
    _, _, first = make_ready(tmp_path, prep_verdict(window_id="window-a"))
    _, _, second = make_ready(
        tmp_path, prep_verdict(window_id="window-b", resume_at="2099-01-01T17:21:00Z"))
    schedule_packet(tmp_path, first)
    run_hook(
        tmp_path, None,
        args=("--mark-scheduled", second.stem, "--automation-id", "heartbeat-second",
              "--packet-path", str(second)),
    )
    session, _, _ = run_hook(tmp_path, None, event="SessionStart", source="resume")
    payload = json.loads(session.stdout)
    assert payload["systemMessage"].startswith("HANDOFF_RESUME_AMBIGUOUS:")
    assert "additionalContext" not in payload["hookSpecificOutput"]


def test_session_start_and_exact_resume_reject_expired_packet(tmp_path):
    _, _, packet_path = make_ready(
        tmp_path, prep_verdict(resume_at="2000-01-01T00:00:00Z"))
    session, _, _ = run_hook(tmp_path, None, event="SessionStart", source="resume")
    assert session.stdout == ""
    schedule_packet(tmp_path, packet_path)
    resumed = run_hook(
        tmp_path, None,
        args=("--resume-context", packet_path.stem, "--packet-path", str(packet_path)),
        expected_return=2,
    )[0]
    assert json.loads(resumed.stdout)["error_class"] == "packet_expired"


def test_repo_root_is_stable_when_hook_starts_from_subdirectory(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subdir = repo / "nested"
    subdir.mkdir()
    result, _, _ = run_hook(
        tmp_path, prep_verdict(), cwd_value=subdir,
        extra_env={"OC_CODEX_HANDOFF_DIR": None},
    )
    context = json.loads(result.stdout)["hookSpecificOutput"]["additionalContext"]
    assert f"packet_path: {repo / '.codex' / 'handoffs'}" in context
    assert not (subdir / ".codex" / "handoffs").exists()


def test_refresh_request_rejects_unchanged_fresh_verdict(tmp_path):
    verdict_dir = tmp_path / "readonly"
    verdict_dir.mkdir()
    verdict_path = verdict_dir / "verdict.json"
    verdict_path.write_text(json.dumps(prep_verdict()), encoding="utf-8")
    usage_path = tmp_path / "usage.json"
    usage_path.write_text("{}", encoding="utf-8")
    handoff_dir = tmp_path / "repo" / ".codex" / "handoffs"
    (tmp_path / "repo").mkdir()
    verdict_dir.chmod(0o500)
    try:
        env = {**os.environ, "OC_PACER_VERDICT": str(verdict_path),
               "OC_USAGE_FILE": str(usage_path), "OC_CODEX_HANDOFF_DIR": str(handoff_dir),
               "OC_CODEX_HANDOFF_PACKET": "1"}
        payload = {"hook_event_name": "UserPromptSubmit", "cwd": str(tmp_path / "repo"),
                   "session_id": "session-fixture", "turn_id": "turn-fixture"}
        result = subprocess.run([sys.executable, str(SCRIPT), "--refresh"],
                                input=json.dumps(payload), text=True, capture_output=True,
                                env=env, cwd=tmp_path / "repo")
    finally:
        verdict_dir.chmod(0o700)
    assert result.returncode == 0 and result.stdout == ""
    assert not handoff_dir.exists()
