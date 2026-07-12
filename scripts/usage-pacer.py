#!/usr/bin/env python3
"""usage-pacer.py — portable 5h-window pacing companion for output-compress.

Compares your provider-quota burn rate against elapsed window time and emits a
deterministic verdict the compression skill can couple to (SKILL.md "Pace-aware
level adjustment"):

  AHEAD   (burn > elapsed + 15pp)          : bump compression one level (within tier cap)
  ON_PACE (within +/-15pp)                 : default level, no injection (zero noise)
  BEHIND  (burn < elapsed - 15pp, <2h left): default level; headroom to spare

Each verdict also carries a machine-readable `fanout` field (AHEAD ->
"prefer-lower-tier", BEHIND -> "burst", ON_PACE/other -> "normal") so external
hooks/skills can consume model-allocation pacing guidance mechanically instead of
parsing the human-readable `message` text.

It also arms a one-shot user notification when burn first crosses NOTIFY_PCT
(default 80%) in a window — how you deliver it (push tool, desktop notify, email)
is up to your hook; this script only guarantees the once-per-window semantics.

Data source (decoupled from any provider): a small JSON file you refresh however
your provider exposes usage — a cron curl, a CLI wrapper, an SDK script:

    {"used_pct": 63.0, "resets_at": "2026-07-11T21:30:00Z"}

Absolute-threshold compression escalation (orthogonal to the pace verdict above -
fires even when ON_PACE, because it tracks raw quota level, not burn rate):

  used_pct >= OC_COMPRESS_WARN_PCT (default 80): bump compression one level
      (within the reader-tier cap, small-model readers excepted)
  used_pct >= OC_COMPRESS_URGE_PCT (default 95): jump to the reader-tier cap

Dedup on state change (warn/urge/"") is the injecting hook's job, not this
script's - compute() is stateless for this field.

Environment overrides:
    OC_USAGE_FILE         input JSON path          (default /tmp/oc-usage.json)
    OC_PACER_VERDICT      output verdict path      (default /tmp/oc-pacer-verdict.json)
    OC_WINDOW_H           window length in hours   (default 5)
    OC_NOTIFY_PCT         notify threshold percent (default 80; <=0 disables)
    OC_COMPRESS_WARN_PCT  compression bump threshold percent (default 80)
    OC_COMPRESS_URGE_PCT  compression cap threshold percent  (default 95)

Usage: usage-pacer.py [--json] ; --self-test runs the scenario asserts.
Exit 0 always — this is advisory; a broken pacer must never block work.
"""
import argparse
import datetime
import json
import os
import pathlib
import sys

USAGE_FILE = pathlib.Path(os.environ.get("OC_USAGE_FILE", "/tmp/oc-usage.json"))
VERDICT_FILE = pathlib.Path(os.environ.get("OC_PACER_VERDICT", "/tmp/oc-pacer-verdict.json"))
NOTIFY_FLAG = pathlib.Path(str(VERDICT_FILE) + ".notified")
WINDOW_H = float(os.environ.get("OC_WINDOW_H", "5"))
NOTIFY_PCT = float(os.environ.get("OC_NOTIFY_PCT", "80"))
AHEAD_DELTA = 15.0
BEHIND_DELTA = -15.0
BEHIND_MIN_LEFT_H = 2.0
COMPRESS_WARN_PCT = float(os.environ.get("OC_COMPRESS_WARN_PCT", "80"))
COMPRESS_URGE_PCT = float(os.environ.get("OC_COMPRESS_URGE_PCT", "95"))

FANOUT = {"AHEAD": "prefer-lower-tier", "BEHIND": "burst"}  # ON_PACE/other -> "normal"

MSG = {
    "AHEAD": ("PACE AHEAD ({d:+.0f}pp): burning quota faster than the window elapses - "
              "bump output-compress one level within the reader-tier cap (never for "
              "small-model readers), defer non-essential generation, prefer collecting "
              "finished work over starting new fan-outs (fanout=prefer-lower-tier: if "
              "you do fan out, prefer lower-tier workers)."),
    "BEHIND": ("PACE BEHIND ({d:+.0f}pp, {left:.1f}h left): quota headroom to spare - "
               "default compression level; parallelism can go up (fanout=burst) - burst "
               "fan-out remains subject to any external delegation/budget gate the host "
               "workspace may run (e.g. a fan-out concurrency cap or spend limiter)."),
}


def compute(used_pct: float, resets_at_iso: str, now: datetime.datetime | None = None) -> dict:
    now = now or datetime.datetime.now(datetime.timezone.utc)
    reset_at = datetime.datetime.fromisoformat(resets_at_iso.replace("Z", "+00:00"))
    left_h = max(0.0, (reset_at - now).total_seconds() / 3600)
    elapsed_frac = min(1.0, max(0.0, (WINDOW_H - left_h) / WINDOW_H))
    ideal = elapsed_frac * 100
    delta = used_pct - ideal
    if delta > AHEAD_DELTA:
        verdict = "AHEAD"
    elif delta < BEHIND_DELTA and left_h < BEHIND_MIN_LEFT_H:
        verdict = "BEHIND"
    else:
        verdict = "ON_PACE"
    fanout = FANOUT.get(verdict, "normal")
    msg = MSG.get(verdict, "").format(d=delta, left=left_h)
    # once-per-window notify arm: flag keyed by resets_at (changes every window)
    notify_user = ""
    if NOTIFY_PCT > 0 and used_pct >= NOTIFY_PCT:
        seen = ""
        try:
            seen = NOTIFY_FLAG.read_text()
        except Exception:
            pass
        if seen != resets_at_iso:
            notify_user = (f"NOTIFY: usage crossed {NOTIFY_PCT:.0f}% "
                           f"(now {used_pct:.0f}%, resets {resets_at_iso}) - "
                           f"notify the user once via your environment's channel.")
            try:
                NOTIFY_FLAG.write_text(resets_at_iso)
            except Exception:
                pass
    # absolute-threshold compression escalation: stateless, orthogonal to verdict above
    if used_pct >= COMPRESS_URGE_PCT:
        compress, compress_msg = "urge", (
            f"USAGE >= {COMPRESS_URGE_PCT:.0f}% (now {used_pct:.0f}%): jump output-compress "
            "to the reader-tier cap.")
    elif used_pct >= COMPRESS_WARN_PCT:
        compress, compress_msg = "warn", (
            f"USAGE >= {COMPRESS_WARN_PCT:.0f}% (now {used_pct:.0f}%): bump output-compress "
            "one level (within the reader-tier cap).")
    else:
        compress, compress_msg = "", ""
    return {"verdict": verdict, "fanout": fanout, "used_pct": used_pct,
            "ideal_pct": round(ideal, 1),
            "delta_pp": round(delta, 1), "window_left_h": round(left_h, 2),
            "message": msg, "notify_user": notify_user,
            "compress": compress, "compress_msg": compress_msg}


def self_test() -> int:
    now = datetime.datetime(2026, 1, 1, 12, 0, tzinfo=datetime.timezone.utc)
    NOTIFY_FLAG.unlink(missing_ok=True)
    a = compute(60, "2026-01-01T16:00:00Z", now)          # 20% elapsed, 60% burned
    assert a["verdict"] == "AHEAD", a
    assert a["fanout"] == "prefer-lower-tier", a
    b = compute(55, "2026-01-01T14:30:00Z", now)          # 50% elapsed, 55% burned
    assert b["verdict"] == "ON_PACE" and b["notify_user"] == "", b
    assert b["fanout"] == "normal", b
    c = compute(40, "2026-01-01T13:00:00Z", now)          # 80% elapsed, 40% burned
    assert c["verdict"] == "BEHIND", c
    assert c["fanout"] == "burst", c
    d1 = compute(82, "2026-01-01T14:30:00Z", now)         # first crossing -> armed
    assert d1["notify_user"], d1
    d2 = compute(87, "2026-01-01T14:30:00Z", now)         # same window -> silent
    assert d2["notify_user"] == "", d2
    d3 = compute(81, "2026-01-01T19:30:00Z", now)         # new window -> re-armed
    assert d3["notify_user"], d3
    NOTIFY_FLAG.unlink(missing_ok=True)
    # absolute-threshold compression escalation: orthogonal to verdict, boundary-inclusive
    e1 = compute(83, "2026-01-01T14:30:00Z", now)
    assert e1["compress"] == "warn" and e1["compress_msg"], e1
    e2 = compute(96, "2026-01-01T14:30:00Z", now)
    assert e2["compress"] == "urge" and e2["compress_msg"], e2
    e3 = compute(79, "2026-01-01T14:30:00Z", now)
    assert e3["compress"] == "" and e3["compress_msg"] == "", e3
    e4 = compute(80.0, "2026-01-01T14:30:00Z", now)
    assert e4["compress"] == "warn", e4
    e5 = compute(95.0, "2026-01-01T14:30:00Z", now)
    assert e5["compress"] == "urge", e5
    print("SELF-TEST PASS (AHEAD/ON_PACE/BEHIND/NOTIFY once-per-window/COMPRESS-threshold/FANOUT)")
    return 0


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--json", action="store_true")
    p.add_argument("--self-test", action="store_true")
    a = p.parse_args()
    if a.self_test:
        return self_test()
    try:
        cache = json.loads(USAGE_FILE.read_text())
        used = float(cache["used_pct"])
        reset = str(cache["resets_at"])
    except Exception as e:
        if a.json:
            print(json.dumps({"verdict": "NO_DATA", "fanout": "normal", "reason": str(e)}))
        return 0  # fail-open: advisory must never block work
    result = compute(used, reset)
    try:
        VERDICT_FILE.write_text(json.dumps(result))
    except Exception:
        pass
    if a.json:
        print(json.dumps(result))
    elif result["message"]:
        print(result["message"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
