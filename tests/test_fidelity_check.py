import json
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "fidelity-check.py"


def run_gate(tmp_path, original, compressed, *extra_args):
    orig = tmp_path / "orig.txt"
    comp = tmp_path / "comp.txt"
    orig.write_text(original, encoding="utf-8")
    comp.write_text(compressed, encoding="utf-8")
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--original", str(orig), "--compressed", str(comp), *extra_args],
        capture_output=True,
        text=True,
        cwd=ROOT,
    )


def test_modal_may_is_a_protected_hedge(tmp_path):
    result = run_gate(tmp_path, "The result may change.", "The result change.", "--json")
    assert result.returncode == 1
    payload = json.loads(result.stdout)
    assert payload["missing"]["hedge_counts"]["may"] == [1, 0]


def test_month_name_may_is_not_counted_as_modal_hedge(tmp_path):
    result = run_gate(tmp_path, "The result changed in May 2026.", "Changed in May 2026.", "--json")
    assert result.returncode == 0
    assert json.loads(result.stdout)["pass"] is True


def test_cjk_deletion_only_grounded_pct_stays_grounded(tmp_path):
    log_file = tmp_path / "log.jsonl"
    result = run_gate(
        tmp_path,
        "這是一個重要結果。",
        "這是重要結果。",
        "--log",
        "--log-file",
        str(log_file),
        "--level",
        "full",
        "--context",
        "cjk",
    )
    assert result.returncode == 0
    entry = json.loads(log_file.read_text(encoding="utf-8"))
    assert entry["grounded_pct"] == 100.0


def test_log_file_directory_is_usage_error(tmp_path):
    result = run_gate(tmp_path, "The result may change.", "The result may change.", "--log", "--log-file", str(tmp_path))
    assert result.returncode == 2
    assert "USAGE ERROR" in result.stderr
