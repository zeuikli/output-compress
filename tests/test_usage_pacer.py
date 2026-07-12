import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "usage-pacer.py"


def test_no_data_atomically_replaces_old_verdict_with_metadata(tmp_path):
    usage = tmp_path / "usage.json"
    verdict = tmp_path / "verdict.json"
    usage.write_text("{}", encoding="utf-8")
    verdict.write_text(json.dumps({"verdict": "HANDOFF_PREP", "secret": "old"}), encoding="utf-8")
    env = {**os.environ, "OC_USAGE_FILE": str(usage), "OC_PACER_VERDICT": str(verdict)}
    result = subprocess.run([sys.executable, str(SCRIPT), "--json"], capture_output=True,
                            text=True, env=env, cwd=ROOT)
    assert result.returncode == 0
    payload = json.loads(verdict.read_text(encoding="utf-8"))
    assert payload["verdict"] == "NO_DATA"
    assert payload["data_status"] == "NO_DATA"
    assert payload["window_id"] == ""
    assert "secret" not in verdict.read_text(encoding="utf-8")


def test_valid_verdict_has_freshness_metadata(tmp_path):
    usage = tmp_path / "usage.json"
    verdict = tmp_path / "verdict.json"
    reset = (datetime.now(timezone.utc) + timedelta(hours=4)).strftime("%Y-%m-%dT%H:%M:%SZ")
    usage.write_text(json.dumps({"used_pct": 95, "resets_at": reset}), encoding="utf-8")
    env = {**os.environ, "OC_USAGE_FILE": str(usage), "OC_PACER_VERDICT": str(verdict)}
    result = subprocess.run([sys.executable, str(SCRIPT), "--json"], capture_output=True,
                            text=True, env=env, cwd=ROOT)
    assert result.returncode == 0
    payload = json.loads(verdict.read_text(encoding="utf-8"))
    assert payload["data_status"] == "OK"
    assert payload["window_id"] == reset
    assert payload["generated_at"].endswith("Z")
