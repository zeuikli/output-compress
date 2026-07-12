import json
import os
import subprocess
import sys
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "codex-handoff.py"


def run_hook(tmp_path, verdict=None, event="UserPromptSubmit", source=None, enabled="1",
             extra_env=None, args=(), cwd_value=None):
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
        env.update(extra_env)
    payload = {"hook_event_name": event,
               "cwd": cwd_value if cwd_value is not None else str(tmp_path / "repo"),
               "session_id": "session-fixture", "turn_id": "turn-fixture"}
    if source is not None:
        payload["source"] = source
    result = subprocess.run([sys.executable, str(SCRIPT), *args], input=json.dumps(payload),
                            text=True, capture_output=True, env=env, cwd=ROOT)
    assert result.returncode == 0, result.stderr
    return result, verdict_path, Path(env["OC_CODEX_HANDOFF_DIR"])


def prep_verdict(**overrides):
    value = {
        "verdict": "HANDOFF_PREP", "handoff": "prep",
        "resume_at": "2026-01-01T12:21:00Z", "window_id": "window-1",
        "data_status": "OK", "used_pct": 95,
    }
    value.update(overrides)
    return value


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
    created, _, handoff_dir = run_hook(tmp_path, prep_verdict())
    assert json.loads(created.stdout)["hookSpecificOutput"]["hookEventName"] == "UserPromptSubmit"

    session, _, _ = run_hook(tmp_path, None, event="SessionStart", source="compact")
    session_payload = json.loads(session.stdout)
    assert session_payload["hookSpecificOutput"]["hookEventName"] == "SessionStart"
    assert "additionalContext" in session_payload["hookSpecificOutput"]

    post, _, _ = run_hook(tmp_path, None, event="PostCompact")
    post_payload = json.loads(post.stdout)
    assert post_payload["hookSpecificOutput"]["hookEventName"] == "PostCompact"
    assert "additionalContext" not in post_payload["hookSpecificOutput"]

    packet_id = next(handoff_dir.glob("*.json")).stem
    run_hook(tmp_path, None, event="UserPromptSubmit", args=("--mark-complete", packet_id))
    completed_session, _, _ = run_hook(tmp_path, None, event="SessionStart", source="resume")
    assert completed_session.stdout == ""


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
    before = packet_path.stat().st_mtime_ns
    packet_id = packet_path.stem
    first = run_hook(tmp_path, None, event="UserPromptSubmit", args=("--mark-complete", packet_id))[0]
    second = run_hook(tmp_path, None, event="UserPromptSubmit", args=("--mark-complete", packet_id))[0]
    assert first.returncode == second.returncode == 0
    assert packet_path.stat().st_mtime_ns == before


def test_unknown_event_is_fail_open_without_output(tmp_path):
    result, _, _ = run_hook(tmp_path, prep_verdict(), event="Unknown")
    assert result.stdout == ""
