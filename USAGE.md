# output-compress — Usage Guide

> How to actually use this skill day-to-day. Install steps live in `README.md`; this file covers invocation, level selection, the fidelity gate workflow, and customization.

## 1. Activating

Compression is **opt-in per conversation** — the skill never rewrites your output silently.

| Agent | How to activate |
|---|---|
| Claude Code | Type `/compress full` (or `output-compress full`) — the skill loads and applies from the next reply onward |
| Codex / AGENTS.md agents | Say `compress: full` in your instruction, or keep the AGENTS.md section permanently and gate it with "when I say compress" |

Deactivate any time: `compress off` / "stop compressing".

## 2. Choosing a level

| Level | What gets removed | Use when |
|---|---|---|
| `lite` | Fillers, pleasantries, preambles only; full sentences kept | Weaker/smaller models will read the text, or the content carries dense logic |
| `full` | + articles (a/an/the), fragment sentences allowed, result-first reordering | Default for mid-tier models; day-to-day technical Q&A |
| `ultra` | + conjunctions dropped where whitelist allows | Frontier models only; throwaway intermediate output |

Rule of thumb: **pick the level by who reads the text, not who writes it.** Notes for a future session of unknown model → `lite`. Scratch output only you will skim → `ultra`.

Savings vary a lot by language: an informal local measurement of `full`-level
compression on CJK prose landed around 15-18% bytes saved (n=4) — much lower than
upstream's ~65% self-reported figure, because CJK has fewer articles and filler words
to strip in the first place. English-heavy output tends to land closer to the upstream
number. Measure your own before quoting a savings figure to anyone.

## 3. The fidelity gate workflow

Every time compressed text will be **persisted** (memory files, reports, handoff notes), verify it mechanically:

```bash
# 1. keep the original           2. compress it            3. verify
cp draft.md /tmp/orig.txt        # (agent produces comp)   python3 scripts/fidelity-check.py \
                                                             --original /tmp/orig.txt \
                                                             --compressed /tmp/comp.txt
```

- `exit 0` → safe to persist.
- `exit 1` → the printed list shows exactly which numbers / negations / paths / tags were lost. Retry one level lower (`ultra`→`full`→`lite`, max 2 retries), else keep the original.
- If you're persisting and it still fails after retries: write the **original** text to the file, not the failing compressed version — but keep the `--log --level <L> --context <C>` flags on that last check so the failure gets recorded anyway (see §7 and `SKILL.md` §5 — a failed attempt is calibration signal, not just a rejected draft).
- Never let the model judge "the meaning is still clear" — that self-assessment is the failure mode this gate exists to replace.

Throwaway chat replies don't need the gate; persisted artifacts always do.

### Pre-check: `--coverage` (is this text even worth compressing?)

Before spending effort compressing an agent-dispatch prompt or archival/log text, ask
the script how much of the file is already whitelist material — path/code/number/tag
density that can't be removed anyway:

```bash
python3 scripts/fidelity-check.py --coverage --original /tmp/dispatch-prompt.txt
```

Sample output (one-line JSON, always exit 0):

```json
{"coverage_pct": 58.9, "recommendation": "skip"}
```

Decision table (thresholds are provisional — n=2, 2026-07-12 field run; see
`SKILL.md` §2 for the underlying measurement):

| `coverage_pct` | `recommendation` | meaning |
|---|---|---|
| ≥ 40 | `skip` | text is whitelist-dense; compress and you'll likely just trigger gate retries for single-digit-percent savings — don't bother |
| 20–40 | `lite` | some whitelist density; cap the level at `lite` rather than reaching for `full`/`ultra` |
| < 20 | `tier-cap` | low whitelist density; safe to use the full reader-tier cap from §2 |

`--coverage` is a pre-check, not the fidelity gate — it never replaces
`--original --compressed` verification once you do compress; it only tells you
whether compressing is likely worth the round-trip in the first place.

### Deletion, not rewriting

The gate only checks whether whitelisted words/phrases *survive*, and compression is
only supposed to *delete* redundant material (fillers, articles, connectives) — not
rephrase sentences. Rewriting for brevity risks silently swapping out which literal
words carry meaning, most commonly negations:

- **OK (deletion):** `"the deploy failed, and we should not retry it right now"` →
  `"deploy failed; don't retry now"` — same negation word survives, just trimmed.
- **Risky (rewriting):** `"the deploy failed, rather than a config error"` →
  `"deploy failed, not a config error"` — this swaps the negation-bearing phrase
  entirely. The gate will likely still catch it (the specific negation words it's
  tracking won't match up), but the underlying issue is that rewording was attempted
  at all — a pure deletion pass never has this failure mode.

If a sentence needs to be reworded (not just trimmed) to get shorter, leave it
uncompressed.

## 4. What is never compressed (whitelist)

Numbers, negation words **and their whole clause** (not/never/unless/except…), file paths, URLs, code, structured tags, safety warnings, contract fields (Goal / Done-when / Return). If a paragraph is mostly whitelist material, skip compression — there is nothing safe to remove.

## 5. Customizing for your language / tags

`scripts/fidelity-check.py` ships with English negation words and generic tag patterns. Edit the two lists at the top of the file:

```python
NEGATIONS = ["not", "never", "unless", "except", ...]   # add yours: "不", "禁止", "nicht", "pas"…
CUSTOM_TAG_PATTERNS = [r"\[TODO[^\]]*\]"]                # add your team's markers
```

Non-English users: add your language's negation words **before first use** — the gate can only protect what it knows to look for.

## 6. Known limits

- Fidelity gate checks element **presence**, not scope: "not X unless Y" mangled into "not X, Y" passes the counter but changed meaning — keep negation-heavy logic at `lite` or uncompressed.
- Upstream token-saving figures (caveman's ~65% average) are self-reported on one model; measure your own before quoting savings.
- Compression stacks badly with other brevity system prompts — if your agent already has a "be terse" rule, start at `lite` and compare.

## 7. Auto-activation (optional)

By default this skill is manual, per-turn opt-in (§1). If you want it to apply every
turn without re-typing `/compress` each time, wire it into your agent's per-turn
context instead of relying on a one-off invocation.

**Claude Code — UserPromptSubmit hook:** inject a short advisory line before each
prompt reaches the model. Show the full rules once (so the model has them at least
once in context), then switch to a one-line reminder on subsequent turns so you're not
re-spending tokens on the same paragraph every time — this is itself the "token diet"
the skill is trying to achieve, applied to the hook's own output:

```bash
#!/usr/bin/env bash
# .claude/hooks/compress-advisory.sh — register as a UserPromptSubmit hook
STATE_FILE="${CLAUDE_PROJECT_DIR:-.}/.output-compress-advisory-shown"
if [ ! -f "$STATE_FILE" ]; then
  cat <<'EOF'
output-compress AUTO: compress internal/scratch output (scratchpad, sub-agent prompts,
report bodies) to the cap for your model tier (see the output-compress skill, SKILL.md
S2). Never compress the final user-facing reply, safety/irreversible-action
confirmations, or contract fields (Goal/Non-goals/Done-when/Return).
EOF
  touch "$STATE_FILE"
else
  echo "output-compress AUTO: tier cap still applies to internal output."
fi
```

Register the script in your settings' hooks (`.claude/settings.json` at project level
or `~/.claude/settings.json`) — the shell script alone does nothing until Claude Code
knows to run it:

```json
{
  "hooks": {
    "UserPromptSubmit": [
      { "hooks": [ { "type": "command",
          "command": "bash \"$CLAUDE_PROJECT_DIR/.claude/hooks/compress-advisory.sh\"" } ] }
    ]
  }
}
```

(Merge into an existing `hooks` block if you have one; use an absolute path instead of
`$CLAUDE_PROJECT_DIR` for a user-level install.)

Delete `$STATE_FILE` (or change its path) to reset back to the full-text reminder,
e.g. after a model/tier change that invalidates the cached cap. **Also reset it after
context compaction**: compaction can summarize away the full rules you showed once,
leaving only the short reminder pointing at rules the model no longer has. On Claude
Code, use a **`SessionStart` hook** — it fires on startup, resume, `/clear`, and after a
compaction (`source: "compact"`), so an `rm -f "$STATE_FILE"` there re-shows the full
text once on the next turn after compaction — and after every fresh session/clear, which
is what you want anyway:

```json
{
  "hooks": {
    "SessionStart": [
      { "hooks": [ { "type": "command",
          "command": "rm -f \"$CLAUDE_PROJECT_DIR/.output-compress-advisory-shown\"" } ] }
    ]
  }
}
```

Add `"matcher": "compact"` to that `SessionStart` entry to reset *only* after compaction
and not on every startup/clear. (Claude Code also has a dedicated **`PostCompact`** hook
that fires only after a compaction; use it instead if you want to reset on compaction
alone — `SessionStart` is recommended here because re-showing the full rules on a fresh
session/clear is desirable too.) On agents without compaction/session events, delete the
file whenever you manually condense the conversation.

### Pace coupling (optional, needs `scripts/usage-pacer.py`)

If you also want the "Pace-aware level adjustment" from `SKILL.md`, extend the same
hook with the bundled portable pacer. You supply the usage data (a tiny JSON your
cron/CLI refreshes — schema in the pacer's docstring); the pacer supplies deterministic
verdicts and once-per-window notification arming.

**Claude subscribers — skip building your own feed.** The bundled companion
`scripts/claude-usage-fetch.py` refreshes the usage JSON from the official Claude
subscription endpoint. Wire it two lines before the pacer (best-effort — it always
exits 0, network or not) and the neutral pacer gets real numbers:

```bash
python3 "$(dirname "$0")/../skills/output-compress/scripts/claude-usage-fetch.py" >/dev/null 2>&1  # best-effort refresh
# ... then the pacer block below reads the freshened OC_USAGE_FILE
```

It finds your token via this chain (first hit wins), so most setups need zero config:

1. `OC_CLAUDE_TOKEN_FILE` — explicit token-file path override
2. `CLAUDE_SESSION_INGRESS_TOKEN_FILE` — Claude Code remote/cloud sessions
3. `CLAUDE_CODE_OAUTH_TOKEN` — token value directly (headless setups)
4. `~/.claude/.credentials.json` → `claudeAiOauth.accessToken` (local Linux/WSL)
5. macOS Keychain (`security find-generic-password -s "Claude Code-credentials" -w`)

No token found → it exits silently and the pacer just sees no data. Env overrides:
`OC_CLAUDE_TOKEN_FILE` (token path), `OC_FETCH_TTL_S` (cache seconds, default 60),
`OC_USAGE_FILE` (shared with the pacer). Bring-your-own JSON stays the provider-agnostic
alternative for non-Claude quotas — just write the `{used_pct, resets_at}` file yourself
and skip the fetcher.

**Codex / ChatGPT-auth users — same pacer, Codex feed.** The bundled companion
`scripts/codex-usage-fetch.py` refreshes the same neutral usage JSON from Codex's usage
endpoint using a Codex session token. Wire it in the same place as the Claude fetcher,
right before the pacer:

```bash
python3 "$(dirname "$0")/../skills/output-compress/scripts/codex-usage-fetch.py" >/dev/null 2>&1  # best-effort refresh
# ... then the pacer block below reads the freshened OC_USAGE_FILE
```

It finds your token via this chain (first hit wins), so normal local CLI installs
usually need no config:

1. `OC_CODEX_TOKEN_FILE` — explicit token-file path override
2. `CODEX_SESSION_TOKEN_FILE` — host/runtime-provided session-token file
3. `CODEX_ACCESS_TOKEN` — token value directly (enterprise/headless setups)
4. `CODEX_OAUTH_TOKEN` — token value directly (local automation)
5. `${CODEX_HOME:-~/.codex}/auth.json` → `tokens.access_token`

Endpoint resolution is configurable: `OC_CODEX_USAGE_URL` wins; otherwise
`chatgpt_base_url` in `${CODEX_HOME:-~/.codex}/config.toml` selects the matching Codex
API style; the default is `https://chatgpt.com/backend-api/wham/usage`. No token found or
fetch failure → it exits silently and the pacer sees no data. Env overrides:
`OC_CODEX_TOKEN_FILE`, `OC_CODEX_USAGE_URL`, `OC_FETCH_TTL_S`, `OC_USAGE_FILE`, and
`CODEX_HOME`. Treat `${CODEX_HOME:-~/.codex}/auth.json` like a password; never commit
it or paste token material into chats/logs.

The pacer only needs the primary five-hour window, but the Codex feeder also preserves
non-PII usage extras when the endpoint returns them: seven-day usage, `credits`,
`code_review_rate_limit`, `additional_rate_limits`, and `rate_limit_reset_credits`.
It deliberately does not write account identifiers such as email, user id, or account id
into `OC_USAGE_FILE`.

Three injection-diet rules keep the hook itself cheap (the same discipline the skill
preaches, applied to the hook):

1. **Recompute at most every 10 minutes** (verdict file mtime check) — not every prompt.
2. **Inject the pace line only when the verdict changes** — ON_PACE is silent by design,
   and repeating an unchanged AHEAD line every turn is pure injection tax.
3. **Inject the notify line at most once** — the pacer already arms it once per window;
   the hook-side content-compare guards against the verdict file's TTL window re-serving it.

```bash
# append inside compress-advisory.sh, after the advisory block above
PACER="$(dirname "$0")/../skills/output-compress/scripts/usage-pacer.py"  # adjust path
FETCH="$(dirname "$0")/../skills/output-compress/scripts/claude-usage-fetch.py"  # optional, Claude
# FETCH="$(dirname "$0")/../skills/output-compress/scripts/codex-usage-fetch.py"  # optional, Codex
VERDICT="${OC_PACER_VERDICT:-/tmp/oc-pacer-verdict.json}"
if [ -f "$PACER" ]; then
  AGE=$(( $(date +%s) - $(stat -c%Y "$VERDICT" 2>/dev/null || echo 0) ))
  # Optional provider feed: refresh OC_USAGE_FILE from the provider endpoint (best-effort,
  # its own TTL cache means the real network call is at most once per OC_FETCH_TTL_S).
  [ "$AGE" -gt 600 ] && [ -f "$FETCH" ] && python3 "$FETCH" >/dev/null 2>&1
  [ "$AGE" -gt 600 ] && python3 "$PACER" >/dev/null 2>&1
  LINE=$(python3 - "$VERDICT" "$STATE_FILE" <<'PY'
import json, pathlib, sys
v, state = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2] + ".pace")
try: d = json.loads(v.read_text())
except Exception: sys.exit()
out = []
prev = state.read_text() if state.exists() else ""
if d.get("message") and d.get("verdict") != prev.split("|")[0]:
    out.append(d["message"])
notify = d.get("notify_user", "")
if notify and notify not in prev:
    out.append(notify)
state.write_text(d.get("verdict", "") + "|" + notify)
print("\n".join(out))
PY
)
  [ -n "$LINE" ] && printf '%s\n' "$LINE"
fi
```

The verdict JSON also carries `compress`/`compress_msg` — an absolute-threshold escalation
(`used_pct >= 80%` → `"warn"`, `>= 95%` → `"urge"`) that is orthogonal to the AHEAD/BEHIND
burn-rate verdict above and fires even when `ON_PACE`; extend the same dedup pattern
(compare against a stored previous state, only inject on change) if you want to couple it.

It also carries a `fanout` field — `"prefer-lower-tier"` on `AHEAD`, `"burst"` on
`BEHIND`, `"normal"` otherwise (including `ON_PACE`) — so a delegation hook/skill can
decide model-tier allocation for new sub-agent fan-out mechanically, without parsing
the human-readable `message` string. Example verdict JSON:

```json
{"verdict": "AHEAD", "fanout": "prefer-lower-tier", "used_pct": 60.0, "ideal_pct": 20.0,
 "delta_pp": 40.0, "window_left_h": 4.0, "message": "PACE AHEAD (+40pp): ...",
 "notify_user": "", "compress": "", "compress_msg": "", "handoff": "", "resume_at": ""}
```

| `verdict` | `fanout` | meaning |
|---|---|---|
| `AHEAD` | `prefer-lower-tier` | if you do fan out new sub-agents, prefer lower-tier workers |
| `ON_PACE` | `normal` | no fan-out guidance change |
| `BEHIND` | `burst` | quota headroom to spare, parallelism can go up — still subject to any external delegation/budget gate the host workspace runs (e.g. a fan-out concurrency cap or spend limiter) |

#### Handoff-aware pacing (`handoff` / `resume_at`)

When the window is nearly exhausted — `used_pct >= 90%` **and** `< 0.5h` left — the
pacer overrides the burn-rate verdict with a **handoff** state, because at that point
the priority is *not losing work* rather than pacing it:

| `verdict` | `handoff` | `resume_at` | what the injected `message` tells the model to do |
|---|---|---|---|
| `HANDOFF_PREP` | `"prep"` | ISO UTC (reset + 3 min) | persist a handoff to memory (task / Done-when / tried / next-action) + commit & push, then schedule a self-wake at `resume_at` — or notify the user to resume then |
| `HANDOFF_HALT` | `"halt"` | `""` | wrap up + persist the handoff only; do **not** schedule another self-wake (circuit breaker tripped) |

The **memory write and the self-wake scheduling are delegated to your environment** —
the pacer only decides *when* to hand off and *when* the wake should fire. On Claude
Code that means the model runs its normal cross-session persistence (Auto Memory, a
handoff skill, `git commit && push`) and schedules the wake with `/schedule` or a
`send_later`-style tool; where none of those exist, the `message` degrades to "notify
the user to resume after `resume_at`". The existing hook already injects `message` on
any verdict change, so `HANDOFF_PREP` / `HANDOFF_HALT` flow through with **no extra
wiring** — read the machine-readable `handoff` / `resume_at` fields only if you want to
wire the self-wake mechanically rather than letting the model act on the text.

**Circuit breaker.** The pacer counts consecutive windows that hit the handoff
threshold (keyed by `resets_at`). After `OC_HANDOFF_MAX` (default 2) consecutive windows
it emits `HANDOFF_HALT` instead of `HANDOFF_PREP` and stops arming the self-wake:
repeatedly burning through whole windows unattended is a runaway / goal-drift signal, so
it hands control back to you. Thresholds are tunable via `OC_HANDOFF_PCT` (90),
`OC_HANDOFF_LEFT_H` (0.5), `OC_HANDOFF_MAX` (2), `OC_HANDOFF_RESUME_DELAY_MIN` (3); set
`OC_HANDOFF_PCT=0` to disable handoff states entirely.

What stays environment-specific (deliberately not shipped): *how* the notify line
reaches the user (push tool, `osascript`, `notify-send`, a chat webhook), and the
**execution** of a handoff — the actual memory write and self-wake scheduling behind
`HANDOFF_PREP` / `HANDOFF_HALT`. The pacer decides *when* to notify, compress, and hand
off; delivering the notification and performing the handoff/self-wake are your
platform's job. The pacer's contract ends at "emit each signal exactly once,
deterministically."

**Codex / AGENTS.md agents:** Codex can use lifecycle hooks for automatic pacer
injection, including a `UserPromptSubmit` command hook for per-turn advisory text and
`PostCompact` / `SessionStart` hooks for compaction/session resets. For the portable
fallback, keep the `AGENTS.md` section permanently in your project's `AGENTS.md`: it is
loaded into context every session, so it behaves as always-present advisory without
extra hook wiring. Gate actual compression with a phrase like "when I say compress" if
you still want opt-in behavior.

For `HANDOFF_PREP`, Codex can wire `resume_at` to a scheduled task / heartbeat that
returns to the same task after the quota window resets. The pacer still does not create
that automation by itself: the hook or host workflow must read `handoff` / `resume_at`,
persist the handoff summary, and ask Codex to schedule the follow-up. If that host
automation is not available, degrade to notifying the user to resume at `resume_at`.
