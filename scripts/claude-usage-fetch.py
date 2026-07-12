#!/usr/bin/env python3
"""claude-usage-fetch.py — optional Claude-specific feeder for usage-pacer.py.

The pacer (usage-pacer.py) is provider-neutral: it reads a small JSON file
(OC_USAGE_FILE) and never talks to any API. This companion refreshes that file
from the official Claude subscription usage endpoint, so Claude Code / Claude.ai
subscribers get real numbers instead of having to build their own feed. The split
is deliberate: fetcher (provider-specific) -> neutral JSON file -> pacer
(provider-neutral). Non-Claude users just skip this script and write the JSON
themselves (schema in usage-pacer.py's docstring).

Token resolution chain (first hit wins):
    1. OC_CLAUDE_TOKEN_FILE               explicit file-path override
    2. CLAUDE_SESSION_INGRESS_TOKEN_FILE  Claude Code remote/cloud sessions
    3. CLAUDE_CODE_OAUTH_TOKEN            token value directly (headless setups)
    4. ~/.claude/.credentials.json        JSON key claudeAiOauth.accessToken
                                          (local Claude Code on Linux/WSL)
    5. macOS Keychain (best-effort)       security find-generic-password
                                          -s "Claude Code-credentials" -w
No token found -> exit 0 silently (fail-open; --json prints fetched:false + reason).

TTL cache: if OC_USAGE_FILE is fresher than OC_FETCH_TTL_S (default 60s) AND was
written by this fetcher (has a "fetched_at" key), the network call is skipped.
Bring-your-own files without "fetched_at" are never counted as fresh cache; a
successful fetch does overwrite them, because a wired official feed is authoritative.

Flags: --json (machine-readable result), --self-test (offline normalize() checks).
Exit 0 always — this is an advisory pipeline and must never block work.

Environment overrides:
    OC_USAGE_FILE         output JSON path        (default /tmp/oc-usage.json)
    OC_FETCH_TTL_S        cache freshness seconds (default 60)
    OC_CLAUDE_TOKEN_FILE  explicit token-file override (see chain above)
"""
import argparse
import json
import os
import pathlib
import subprocess
import sys
import urllib.request
from datetime import datetime, timezone

USAGE_FILE = pathlib.Path(os.environ.get("OC_USAGE_FILE", "/tmp/oc-usage.json"))
FETCH_TTL_S = float(os.environ.get("OC_FETCH_TTL_S", "60"))
USAGE_URL = "https://api.anthropic.com/api/oauth/usage"


def _parse_iso_utc(s):
    """Normalize an ISO-ish timestamp to 'YYYY-MM-DDTHH:MM:SSZ' (UTC).

    Accepts full ISO with offset, a trailing 'Z', or minute-truncated
    'YYYY-MM-DDTHH:MM'. Naive input is assumed UTC. Returns None if unparseable.
    """
    if not isinstance(s, str) or not s.strip():
        return None
    txt = s.strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(txt)
    except ValueError:
        try:  # minute-truncated form on Python < 3.11
            dt = datetime.fromisoformat(txt + ":00")
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def normalize(payload, now):
    """Pure transform: official usage payload -> neutral pacer JSON record.

    used_pct   = float(five_hour.utilization), must be within [0,100] else None
    resets_at  = five_hour.resets_at normalized to full ISO UTC; unparseable -> None
    extras (harmless to the pacer): seven_day_pct (float, 0 on missing/bad),
    fetched_at (ISO UTC of now). Missing/blank five_hour -> None.
    """
    five = payload.get("five_hour")
    if not isinstance(five, dict):
        return None
    try:
        used_pct = float(five.get("utilization"))
    except (TypeError, ValueError):
        return None
    if not (0.0 <= used_pct <= 100.0):
        return None
    resets_at = _parse_iso_utc(five.get("resets_at"))
    if resets_at is None:
        return None
    seven = payload.get("seven_day") or {}
    try:
        seven_pct = float(seven.get("utilization"))
    except (TypeError, ValueError):
        seven_pct = 0.0
    return {
        "used_pct": used_pct,
        "resets_at": resets_at,
        "seven_day_pct": seven_pct,
        "fetched_at": now.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


def _token_from_file(path):
    try:
        return (open(path, encoding="utf-8").read().strip() or None)
    except Exception:
        return None


def _token_from_credentials_json(text):
    try:
        tok = (json.loads(text).get("claudeAiOauth") or {}).get("accessToken")
        return tok.strip() if tok else None
    except Exception:
        return None


def resolve_token():
    """Return (token, source) on success or (None, reason). Chain in module docstring."""
    p = os.environ.get("OC_CLAUDE_TOKEN_FILE")
    if p:
        tok = _token_from_file(p)
        if tok:
            return tok, "OC_CLAUDE_TOKEN_FILE"
    p = os.environ.get("CLAUDE_SESSION_INGRESS_TOKEN_FILE")
    if p:
        tok = _token_from_file(p)
        if tok:
            return tok, "CLAUDE_SESSION_INGRESS_TOKEN_FILE"
    tok = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN")
    if tok and tok.strip():
        return tok.strip(), "CLAUDE_CODE_OAUTH_TOKEN"
    cred = os.path.expanduser("~/.claude/.credentials.json")
    try:
        with open(cred, encoding="utf-8") as f:
            tok = _token_from_credentials_json(f.read())
        if tok:
            return tok, "credentials.json"
    except Exception:
        pass
    if sys.platform == "darwin":  # macOS Keychain, best-effort
        try:
            out = subprocess.run(
                ["security", "find-generic-password",
                 "-s", "Claude Code-credentials", "-w"],
                capture_output=True, text=True, timeout=5)
            if out.returncode == 0:
                tok = _token_from_credentials_json(out.stdout)
                if tok:
                    return tok, "keychain"
        except Exception:
            pass
    return None, "no token found (env vars, ~/.claude/.credentials.json, keychain)"


def _fresh_cache():
    """Return the cached record if OC_USAGE_FILE is our own and still fresh, else None."""
    import time
    try:
        if time.time() - os.stat(USAGE_FILE).st_mtime < FETCH_TTL_S:
            rec = json.loads(USAGE_FILE.read_text(encoding="utf-8"))
            if isinstance(rec, dict) and "fetched_at" in rec:
                return rec
    except Exception:
        pass
    return None


def do_fetch():
    """Refresh OC_USAGE_FILE. Return (record, reason); record is None on any miss."""
    cached = _fresh_cache()
    if cached is not None:
        return cached, "cache fresh"
    tok, src = resolve_token()
    if not tok:
        return None, src
    try:
        req = urllib.request.Request(
            USAGE_URL,
            headers={"Authorization": f"Bearer {tok}",
                     "anthropic-beta": "oauth-2025-04-20"})
        with urllib.request.urlopen(req, timeout=5) as r:
            payload = json.loads(r.read())
    except Exception as e:
        return None, f"fetch failed: {e}"
    rec = normalize(payload, datetime.now(timezone.utc))
    if rec is None:
        return None, "payload normalize failed"
    try:
        tmp = str(USAGE_FILE) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(rec, f)
        os.replace(tmp, USAGE_FILE)
    except Exception as e:
        return None, f"write failed: {e}"
    return rec, "fetched"


def self_test():
    now = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
    # good payload, full ISO with offset
    r = normalize({"five_hour": {"utilization": 63.0,
                                 "resets_at": "2026-01-01T21:30:00+00:00"},
                   "seven_day": {"utilization": 12.0}}, now)
    assert r and r["used_pct"] == 63.0, r
    assert r["resets_at"] == "2026-01-01T21:30:00Z", r
    assert r["seven_day_pct"] == 12.0, r
    assert r["fetched_at"] == "2026-01-01T12:00:00Z", r
    # minute-truncated resets_at
    r = normalize({"five_hour": {"utilization": 10, "resets_at": "2026-01-01T21:30"}}, now)
    assert r["resets_at"] == "2026-01-01T21:30:00Z", r
    # naive datetime (no offset, with seconds) -> assumed UTC
    r = normalize({"five_hour": {"utilization": 10, "resets_at": "2026-01-01T21:30:00"}}, now)
    assert r["resets_at"] == "2026-01-01T21:30:00Z", r
    # 'Z' form
    r = normalize({"five_hour": {"utilization": 10, "resets_at": "2026-01-01T21:30:00Z"}}, now)
    assert r["resets_at"] == "2026-01-01T21:30:00Z", r
    # utilization out of range -> None
    assert normalize({"five_hour": {"utilization": 150,
                                    "resets_at": "2026-01-01T21:30:00Z"}}, now) is None
    assert normalize({"five_hour": {"utilization": -1,
                                    "resets_at": "2026-01-01T21:30:00Z"}}, now) is None
    # unparseable resets_at -> None
    assert normalize({"five_hour": {"utilization": 10, "resets_at": "not-a-date"}}, now) is None
    # missing five_hour -> None
    assert normalize({"seven_day": {"utilization": 5}}, now) is None
    # seven_day missing -> extras default to 0
    r = normalize({"five_hour": {"utilization": 10, "resets_at": "2026-01-01T21:30:00Z"}}, now)
    assert r["seven_day_pct"] == 0.0, r
    print("SELF-TEST PASS (normalize: full-ISO/minute-trunc/naive/Z/"
          "out-of-range/unparseable/missing-5h/7d-default)")
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args()
    if args.self_test:
        return self_test()
    try:
        rec, reason = do_fetch()
    except Exception as e:  # belt-and-braces: advisory pipeline must never block
        rec, reason = None, f"unexpected error: {e}"
    if args.json:
        if rec is not None:
            print(json.dumps({"fetched": True, "cached": reason == "cache fresh",
                              "reason": reason, "record": rec}))
        else:
            print(json.dumps({"fetched": False, "reason": reason}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
