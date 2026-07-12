#!/usr/bin/env python3
"""codex-usage-fetch.py — optional Codex-specific feeder for usage-pacer.py.

The pacer (usage-pacer.py) is provider-neutral: it reads a small JSON file
(OC_USAGE_FILE) and never talks to any API. This companion refreshes that file
from Codex's usage endpoint using a Codex/ChatGPT session token, so Codex users
can feed the pacer real numbers instead of maintaining their own JSON refresh.

Token resolution chain (first hit wins):
    1. OC_CODEX_TOKEN_FILE          explicit token-file override
    2. CODEX_SESSION_TOKEN_FILE     session-token file from a host/runtime
    3. CODEX_ACCESS_TOKEN           token value directly (enterprise/headless)
    4. CODEX_OAUTH_TOKEN            token value directly (local automation)
    5. ${CODEX_HOME:-~/.codex}/auth.json -> tokens.access_token

Endpoint resolution:
    1. OC_CODEX_USAGE_URL override
    2. chatgpt_base_url in ${CODEX_HOME:-~/.codex}/config.toml
       - https://api.openai.com      -> /api/codex/usage
       - https://chatgpt.com/backend-api -> /wham/usage
    3. default: https://chatgpt.com/backend-api/wham/usage

No token found -> exit 0 silently (fail-open; --json prints fetched:false + reason).

TTL cache: if OC_USAGE_FILE is fresher than OC_FETCH_TTL_S (default 60s) AND was
written by this fetcher (has "fetched_at" and "source":"codex"), the network call is
skipped. Bring-your-own files are never counted as fresh cache; a successful fetch
overwrites them, because a wired official feed is authoritative.

Flags: --json (machine-readable result), --self-test (offline normalize() checks).
Exit 0 always — this is an advisory pipeline and must never block work.

Environment overrides:
    OC_USAGE_FILE          output JSON path        (default /tmp/oc-usage.json)
    OC_FETCH_TTL_S         cache freshness seconds (default 60)
    OC_CODEX_TOKEN_FILE    explicit token-file override (see chain above)
    OC_CODEX_USAGE_URL     explicit usage endpoint override
    CODEX_HOME             Codex config/auth dir   (default ~/.codex)
"""
import argparse
import json
import os
import pathlib
import re
import sys
import urllib.request
from datetime import datetime, timezone

CODEX_HOME = pathlib.Path(os.environ.get("CODEX_HOME", "~/.codex")).expanduser()
USAGE_FILE = pathlib.Path(os.environ.get("OC_USAGE_FILE", "/tmp/oc-usage.json"))
FETCH_TTL_S = float(os.environ.get("OC_FETCH_TTL_S", "60"))
DEFAULT_USAGE_URL = "https://chatgpt.com/backend-api/wham/usage"


def _parse_iso_utc(value):
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(float(value), timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        except Exception:
            return None
    if not isinstance(value, str) or not value.strip():
        return None
    txt = value.strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(txt)
    except ValueError:
        try:
            dt = datetime.fromisoformat(txt + ":00")
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _window_reset_at(window, now):
    for key in ("reset_at", "resets_at"):
        parsed = _parse_iso_utc(window.get(key))
        if parsed:
            return parsed
    try:
        after = float(window.get("reset_after_seconds"))
    except (TypeError, ValueError):
        return None
    return datetime.fromtimestamp(now.timestamp() + after, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _window_used_pct(window):
    for key in ("used_percent", "utilization", "used_pct"):
        if key not in window:
            continue
        try:
            used = float(window.get(key))
        except (TypeError, ValueError):
            return None
        if key == "utilization" and 0.0 <= used <= 1.0:
            used *= 100.0
        return used if 0.0 <= used <= 100.0 else None
    return None


def _normalize_window(window, now):
    if not isinstance(window, dict):
        return None
    used_pct = _window_used_pct(window)
    resets_at = _window_reset_at(window, now)
    if used_pct is None and resets_at is None:
        return None
    out = {}
    if used_pct is not None:
        out["used_pct"] = used_pct
    if resets_at is not None:
        out["resets_at"] = resets_at
    try:
        out["limit_window_seconds"] = int(window["limit_window_seconds"])
    except (KeyError, TypeError, ValueError):
        pass
    return out


def _normalize_limit_snapshot(snapshot, now):
    if not isinstance(snapshot, dict):
        return None
    out = {}
    for src, dst in (("primary_window", "primary"), ("secondary_window", "secondary")):
        win = _normalize_window(snapshot.get(src), now)
        if win is not None:
            out[dst] = win
    for key in ("allowed", "limit_reached", "limit_name", "limit_id", "individual_limit"):
        if key in snapshot and isinstance(snapshot[key], (bool, str, int, float, type(None))):
            out[key] = snapshot[key]
    return out or None


def normalize(payload, now):
    """Pure transform: Codex usage payload -> neutral pacer JSON record.

    used_pct  = primary 5h window used percentage
    resets_at = primary reset time normalized to full ISO UTC
    extras    = seven_day_pct/seven_day_resets_at/plan_type/source/fetched_at
    """
    rate = payload.get("rate_limit") if isinstance(payload, dict) else None
    if not isinstance(rate, dict):
        rate = payload if isinstance(payload, dict) else {}
    primary = rate.get("primary_window") or payload.get("primary_window")
    if not isinstance(primary, dict):
        return None
    used_pct = _window_used_pct(primary)
    resets_at = _window_reset_at(primary, now)
    if used_pct is None or resets_at is None:
        return None
    secondary = rate.get("secondary_window") or payload.get("secondary_window") or {}
    seven_pct = _window_used_pct(secondary) if isinstance(secondary, dict) else None
    seven_reset = _window_reset_at(secondary, now) if isinstance(secondary, dict) else None
    record = {
        "used_pct": used_pct,
        "resets_at": resets_at,
        "fetched_at": now.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source": "codex",
    }
    if seven_pct is not None:
        record["seven_day_pct"] = seven_pct
    if seven_reset:
        record["seven_day_resets_at"] = seven_reset
    plan_type = payload.get("plan_type") if isinstance(payload, dict) else None
    if isinstance(plan_type, str) and plan_type:
        record["plan_type"] = plan_type
    credits = payload.get("credits") if isinstance(payload, dict) else None
    if isinstance(credits, dict):
        record["credits"] = {
            k: credits[k] for k in (
                "has_credits", "unlimited", "balance", "overage_limit_reached",
                "approx_cloud_messages", "approx_local_messages",
            ) if k in credits
        }
    code_review = _normalize_limit_snapshot(payload.get("code_review_rate_limit"), now)
    if code_review is not None:
        record["code_review_rate_limit"] = code_review
    additional = payload.get("additional_rate_limits")
    if isinstance(additional, list):
        normalized = []
        for item in additional:
            snap = _normalize_limit_snapshot(item, now)
            if snap is not None:
                normalized.append(snap)
        if normalized:
            record["additional_rate_limits"] = normalized
    reset_credits = payload.get("rate_limit_reset_credits")
    if isinstance(reset_credits, dict):
        safe = {
            k: reset_credits[k]
            for k in ("available_count", "resets_available", "has_available")
            if k in reset_credits and isinstance(reset_credits[k], (bool, int, float, str))
        }
        if safe:
            record["rate_limit_reset_credits"] = safe
    return record


def _token_from_file(path):
    try:
        return (open(path, encoding="utf-8").read().strip() or None)
    except Exception:
        return None


def _token_from_auth_json(text):
    try:
        tokens = json.loads(text).get("tokens") or {}
        tok = tokens.get("access_token")
        return tok.strip() if tok else None
    except Exception:
        return None


def resolve_token():
    p = os.environ.get("OC_CODEX_TOKEN_FILE")
    if p:
        tok = _token_from_file(p)
        if tok:
            return tok, "OC_CODEX_TOKEN_FILE"
    p = os.environ.get("CODEX_SESSION_TOKEN_FILE")
    if p:
        tok = _token_from_file(p)
        if tok:
            return tok, "CODEX_SESSION_TOKEN_FILE"
    for name in ("CODEX_ACCESS_TOKEN", "CODEX_OAUTH_TOKEN"):
        tok = os.environ.get(name)
        if tok and tok.strip():
            return tok.strip(), name
    auth = CODEX_HOME / "auth.json"
    try:
        tok = _token_from_auth_json(auth.read_text(encoding="utf-8"))
        if tok:
            return tok, "auth.json"
    except Exception:
        pass
    return None, "no token found (env vars, token files, auth.json)"


def _read_chatgpt_base_url():
    config = CODEX_HOME / "config.toml"
    try:
        text = config.read_text(encoding="utf-8")
    except Exception:
        return None
    match = re.search(r'(?m)^\s*chatgpt_base_url\s*=\s*"([^"]+)"\s*$', text)
    return match.group(1).strip() if match else None


def resolve_usage_url():
    override = os.environ.get("OC_CODEX_USAGE_URL")
    if override and override.strip():
        return override.strip()
    base = (_read_chatgpt_base_url() or "").rstrip("/")
    if not base:
        return DEFAULT_USAGE_URL
    if base.endswith("/backend-api"):
        return base + "/wham/usage"
    if "chatgpt.com" in base or "chat.openai.com" in base:
        return base + "/backend-api/wham/usage"
    return base + "/api/codex/usage"


def _fresh_cache():
    import time
    try:
        if time.time() - os.stat(USAGE_FILE).st_mtime < FETCH_TTL_S:
            rec = json.loads(USAGE_FILE.read_text(encoding="utf-8"))
            if isinstance(rec, dict) and rec.get("source") == "codex" and "fetched_at" in rec:
                return rec
    except Exception:
        pass
    return None


def do_fetch():
    cached = _fresh_cache()
    if cached is not None:
        return cached, "cache fresh"
    tok, src = resolve_token()
    if not tok:
        return None, src
    url = resolve_usage_url()
    try:
        req = urllib.request.Request(
            url,
            headers={"Authorization": f"Bearer {tok}", "Accept": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=5) as response:
            payload = json.loads(response.read())
    except Exception as e:
        return None, f"fetch failed: {type(e).__name__}: {e}"
    rec = normalize(payload, datetime.now(timezone.utc))
    if rec is None:
        return None, "payload normalize failed"
    try:
        tmp = str(USAGE_FILE) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(rec, handle)
        os.replace(tmp, USAGE_FILE)
    except Exception as e:
        return None, f"write failed: {e}"
    return rec, f"fetched via {src}"


def self_test():
    now = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
    payload = {
        "plan_type": "plus",
        "rate_limit": {
            "primary_window": {
                "used_percent": 63,
                "limit_window_seconds": 18000,
                "reset_at": 1767283200,
            },
            "secondary_window": {
                "used_percent": 12.5,
                "reset_at": "2026-01-04T09:15:00Z",
            },
        },
        "credits": {"has_credits": True, "unlimited": False, "balance": "9.99"},
        "code_review_rate_limit": {
            "primary_window": {"used_percent": 5, "reset_after_seconds": 1800},
            "limit_name": "Code review",
        },
        "additional_rate_limits": [
            {"limit_id": "gpt-5.5", "primary_window": {
                "utilization": 0.5, "reset_after_seconds": 600}},
        ],
        "rate_limit_reset_credits": {"available_count": 2},
    }
    rec = normalize(payload, now)
    assert rec and rec["used_pct"] == 63.0, rec
    assert rec["resets_at"] == "2026-01-01T16:00:00Z", rec
    assert rec["seven_day_pct"] == 12.5, rec
    assert rec["seven_day_resets_at"] == "2026-01-04T09:15:00Z", rec
    assert rec["plan_type"] == "plus", rec
    assert rec["source"] == "codex", rec
    assert rec["code_review_rate_limit"]["primary"]["used_pct"] == 5.0, rec
    assert rec["additional_rate_limits"][0]["primary"]["used_pct"] == 50.0, rec
    assert rec["rate_limit_reset_credits"]["available_count"] == 2, rec
    # reset_after_seconds fallback and fractional utilization input.
    rec = normalize({"rate_limit": {"primary_window": {
        "utilization": 0.25, "reset_after_seconds": 3600}}}, now)
    assert rec["used_pct"] == 25.0 and rec["resets_at"] == "2026-01-01T13:00:00Z", rec
    # Missing primary or invalid percentages are rejected.
    assert normalize({"rate_limit": {}}, now) is None
    assert normalize({"rate_limit": {"primary_window": {
        "used_percent": 150, "reset_at": "2026-01-01T16:00:00Z"}}}, now) is None
    print("SELF-TEST PASS (normalize: codex windows/epoch reset/fallback reset/"
          "fractional utilization/reject invalid)")
    return 0


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    if args.self_test:
        return self_test()
    try:
        rec, reason = do_fetch()
    except Exception as e:
        rec, reason = None, f"unexpected error: {type(e).__name__}: {e}"
    if args.json:
        if rec is not None:
            print(json.dumps({"fetched": True, "cached": reason == "cache fresh",
                              "reason": reason, "record": rec}))
        else:
            print(json.dumps({"fetched": False, "reason": reason}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
