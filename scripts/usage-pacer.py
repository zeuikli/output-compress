#!/usr/bin/env python3
"""usage-pacer.py — portable 5h-window pacing companion for output-compress.

Compares your provider-quota burn rate against elapsed window time and emits a
deterministic verdict the compression skill can couple to (SKILL.md "Pace-aware
level adjustment"):

  AHEAD   (burn > elapsed + 15pp)          : bump compression one level (within tier cap)
  ON_PACE (within +/-15pp)                 : default level, no injection (zero noise)
  BEHIND  (burn < elapsed - 15pp, <2h left): default level; headroom to spare

Handoff states (fire before AHEAD/BEHIND when the window is nearly exhausted — the
quota is about to run out with almost no window time left, so the priority shifts from
pacing to *not losing work*):

  HANDOFF_PREP (used >= 90% AND < 0.5h left in window): persist a handoff to your
      agent's memory now (task goal, Done-when, what's tried, next action) and commit/
      push, then — if your platform supports it — schedule a self-wake shortly after
      the window resets to resume; otherwise notify the user to resume then. The memory
      write and the scheduling are DELEGATED to your environment's own auto-memory /
      handoff skill / scheduler — this signal only decides WHEN to hand off.
  HANDOFF_HALT (the handoff threshold is hit in >MAX consecutive windows): only wrap
      up and persist the handoff — do NOT schedule another self-wake. Repeatedly
      burning consecutive windows is a runaway / goal-drift signal, so this circuit
      breaker stops the auto-wake loop and waits for the user.

Each verdict also carries a machine-readable `fanout` field (AHEAD ->
"prefer-lower-tier", BEHIND -> "burst", ON_PACE/handoff/other -> "normal") so external
hooks/skills can consume model-allocation pacing guidance mechanically instead of
parsing the human-readable `message` text. Handoff states additionally carry a
`handoff` field ("prep" | "halt" | "") and, for "prep", a `resume_at` field (ISO UTC of
when a self-wake should fire) so a hook can wire the handoff/self-wake mechanically.

It also arms a one-shot user notification when burn first crosses NOTIFY_PCT
(default 80%) in a window — how you deliver it (push tool, desktop notify, email)
is up to your hook; this script only guarantees the once-per-window semantics.

Data source (decoupled from any provider): a small JSON file you refresh however
your provider exposes usage — a cron curl, a CLI wrapper, an SDK script:

    {"used_pct": 63.0, "resets_at": "2026-07-11T21:30:00Z"}

On Claude subscriptions or Codex/ChatGPT-auth sessions, bundled companions
(`claude-usage-fetch.py`, `codex-usage-fetch.py`) can populate this file for you from
the provider usage endpoint (run one best-effort right before this pacer);
bring-your-own JSON stays the general, provider-agnostic mechanism.

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
    OC_HANDOFF_PCT        handoff used% threshold            (default 90; <=0 disables)
    OC_HANDOFF_LEFT_H     handoff max window-hours-left      (default 0.5)
    OC_HANDOFF_MAX        consecutive handoff windows before the HALT circuit breaker (default 2)
    OC_HANDOFF_RESUME_DELAY_MIN  minutes after reset to schedule the self-wake (default 3)

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
# Handoff state machine (fires before AHEAD/BEHIND — priority shifts to not losing work)
HANDOFF_PCT = float(os.environ.get("OC_HANDOFF_PCT", "90"))
HANDOFF_MIN_LEFT_H = float(os.environ.get("OC_HANDOFF_LEFT_H", "0.5"))
HANDOFF_MAX_CONSECUTIVE = int(float(os.environ.get("OC_HANDOFF_MAX", "2")))
HANDOFF_RESUME_DELAY_MIN = float(os.environ.get("OC_HANDOFF_RESUME_DELAY_MIN", "3"))
# Circuit-breaker counter keyed by resets_at (a new window -> a new resets_at -> +1)
HANDOFF_COUNT_FILE = pathlib.Path(str(VERDICT_FILE) + ".handoff-count")

FANOUT = {"AHEAD": "prefer-lower-tier", "BEHIND": "burst"}  # ON_PACE/handoff/other -> "normal"

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
    "HANDOFF_PREP": (
        "PACE HANDOFF-PREP (used {u:.0f}%, {left_m:.0f} min left in window, consecutive "
        "#{n}): quota is nearly exhausted with almost no window time left. Persist a "
        "handoff NOW — write the task goal, Done-when, what's been tried, and the next "
        "action into your agent's memory (delegate to your auto-memory / handoff skill) "
        "and commit + push work in progress. Then hand off: if your platform can schedule "
        "a self-wake, schedule one shortly after the window resets ({resume}) to resume; "
        "otherwise notify the user to resume then. This signal decides WHEN — the memory "
        "write and the scheduling are done by your environment's own mechanisms."),
    "HANDOFF_HALT": (
        "PACE HANDOFF-HALT (the handoff threshold was hit in {n} consecutive windows, "
        "over the circuit-breaker limit of {max}): only wrap up and persist the handoff "
        "— do NOT schedule another self-wake. Repeatedly burning through consecutive "
        "windows is a runaway / goal-drift signal; wait for the user to confirm before "
        "continuing."),
}


def compute(used_pct: float, resets_at_iso: str, now: datetime.datetime | None = None) -> dict:
    now = now or datetime.datetime.now(datetime.timezone.utc)
    reset_at = datetime.datetime.fromisoformat(resets_at_iso.replace("Z", "+00:00"))
    left_h = max(0.0, (reset_at - now).total_seconds() / 3600)
    elapsed_frac = min(1.0, max(0.0, (WINDOW_H - left_h) / WINDOW_H))
    ideal = elapsed_frac * 100
    delta = used_pct - ideal
    handoff = ""
    handoff_n = 0
    resume_at = ""
    if HANDOFF_PCT > 0 and used_pct >= HANDOFF_PCT and left_h < HANDOFF_MIN_LEFT_H:
        # circuit-breaker count keyed by resets_at (new window -> new resets_at -> +1)
        try:
            prev = HANDOFF_COUNT_FILE.read_text().split("|")
            handoff_n = int(prev[1]) + 1 if prev[0] != resets_at_iso else int(prev[1])
        except Exception:
            handoff_n = 1
        handoff_n = max(handoff_n, 1)
        try:
            HANDOFF_COUNT_FILE.write_text(f"{resets_at_iso}|{handoff_n}")
        except Exception:
            pass
        if handoff_n >= HANDOFF_MAX_CONSECUTIVE + 1:
            verdict, handoff = "HANDOFF_HALT", "halt"
        else:
            verdict, handoff = "HANDOFF_PREP", "prep"
            resume_at = (reset_at + datetime.timedelta(minutes=HANDOFF_RESUME_DELAY_MIN)
                         ).astimezone(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    elif delta > AHEAD_DELTA:
        verdict = "AHEAD"
    elif delta < BEHIND_DELTA and left_h < BEHIND_MIN_LEFT_H:
        verdict = "BEHIND"
    else:
        verdict = "ON_PACE"
    fanout = FANOUT.get(verdict, "normal")
    msg = MSG.get(verdict, "").format(d=delta, left=left_h, u=used_pct,
                                      left_m=left_h * 60, resume=resume_at,
                                      n=handoff_n, max=HANDOFF_MAX_CONSECUTIVE)
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
            "compress": compress, "compress_msg": compress_msg,
            "handoff": handoff, "resume_at": resume_at}


def _validate(used_pct: float, resets_at_iso: str,
              now: datetime.datetime | None = None) -> tuple[float, str]:
    """Sanity-gate the loaded usage record before it drives a verdict.

    A dead container or a failed refresh cron can leave a stale usage file whose
    numbers now mis-verdict AHEAD/BEHIND; an unusable record is better treated as
    NO_DATA than trusted. Also normalizes resets_at to an aware UTC 'Z' string so a
    naive timestamp (which would otherwise crash compute() on an aware/naive
    comparison, breaking the "exit 0 always" promise) is handled cleanly.

    Returns (used_pct, normalized_resets_at_iso); raises ValueError on rejection
    (message carries the reason, surfaced verbatim in main()'s --json output).
    """
    now = now or datetime.datetime.now(datetime.timezone.utc)
    if not (0.0 <= used_pct <= 100.0):
        raise ValueError(f"used_pct out of [0,100]: {used_pct} (stale/implausible used_pct)")
    txt = resets_at_iso.strip().replace("Z", "+00:00")
    try:
        dt = datetime.datetime.fromisoformat(txt)
    except ValueError:
        try:  # minute-truncated form on Python < 3.11
            dt = datetime.datetime.fromisoformat(txt + ":00")
        except ValueError:
            raise ValueError(f"unparseable resets_at: {resets_at_iso}")
    if dt.tzinfo is None:  # naive -> assume UTC
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    dt = dt.astimezone(datetime.timezone.utc)
    if dt < now - datetime.timedelta(hours=1) or \
            dt > now + datetime.timedelta(hours=WINDOW_H + 1):
        raise ValueError(f"resets_at outside plausible window "
                         f"(now-1h .. now+{WINDOW_H + 1:.0f}h): {resets_at_iso} "
                         f"(stale/implausible resets_at)")
    return used_pct, dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def self_test() -> int:
    now = datetime.datetime(2026, 1, 1, 12, 0, tzinfo=datetime.timezone.utc)
    NOTIFY_FLAG.unlink(missing_ok=True)
    HANDOFF_COUNT_FILE.unlink(missing_ok=True)
    a = compute(60, "2026-01-01T16:00:00Z", now)          # 20% elapsed, 60% burned
    assert a["verdict"] == "AHEAD", a
    assert a["fanout"] == "prefer-lower-tier", a
    assert a["handoff"] == "" and a["resume_at"] == "", a
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
    # handoff state machine: PREP when used>=90 AND <0.5h left; takes priority over AHEAD
    HANDOFF_COUNT_FILE.unlink(missing_ok=True)
    hp = compute(95, "2026-01-01T12:18:00Z", now)         # 18 min left, used 95
    assert hp["verdict"] == "HANDOFF_PREP" and hp["handoff"] == "prep", hp
    assert hp["resume_at"] == "2026-01-01T12:21:00Z", hp  # reset + 3 min
    assert "12:21:00Z" in hp["message"], hp
    assert hp["fanout"] == "normal", hp                   # handoff -> normal fan-out
    hp2 = compute(95, "2026-01-01T17:18:00Z", now + datetime.timedelta(hours=5))
    assert hp2["verdict"] == "HANDOFF_PREP", hp2          # 2nd consecutive window -> still PREP
    hp3 = compute(95, "2026-01-01T22:18:00Z", now + datetime.timedelta(hours=10))
    assert hp3["verdict"] == "HANDOFF_HALT" and hp3["handoff"] == "halt", hp3
    assert hp3["resume_at"] == "", hp3                    # HALT -> circuit breaker, no self-wake
    HANDOFF_COUNT_FILE.unlink(missing_ok=True)
    # handoff needs BOTH conditions: high used% but ample time left is NOT handoff
    hn = compute(95, "2026-01-01T14:30:00Z", now)         # used 95 but 2.5h left
    assert hn["verdict"] != "HANDOFF_PREP" and hn["handoff"] == "", hn
    # sanity gate: out-of-range used_pct rejected
    try:
        _validate(150, "2026-01-01T14:30:00Z", now)
        assert False, "out-of-range used_pct should be rejected"
    except ValueError as ex:
        assert "stale/implausible used_pct" in str(ex), ex
    # sanity gate: implausible future resets_at rejected (now+10h > now+WINDOW_H+1h)
    try:
        _validate(50, "2026-01-01T22:00:00Z", now)
        assert False, "implausible future resets_at should be rejected"
    except ValueError as ex:
        assert "stale/implausible resets_at" in str(ex), ex
    # sanity gate: naive resets_at accepted and treated as UTC; plausible value passes
    v_used, v_reset = _validate(50, "2026-01-01T14:30:00", now)
    assert v_used == 50.0 and v_reset == "2026-01-01T14:30:00Z", (v_used, v_reset)
    print("SELF-TEST PASS (AHEAD/ON_PACE/BEHIND/NOTIFY once-per-window/"
          "COMPRESS-threshold/HANDOFF-PREP/HALT-circuit-breaker/FANOUT/sanity-gate)")
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
        used, reset = _validate(used, reset)
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
