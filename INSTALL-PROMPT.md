# INSTALL-PROMPT — paste this to any AI assistant to install output-compress for you

> **How to use this file:** copy everything below the `---` line into a chat with an AI
> assistant that has filesystem access to your machine or project (Claude Code, Codex,
> Cursor, or any agent that can run shell commands), with this `output-compress/`
> directory somewhere it can read. The assistant will detect your environment, install
> the skill, verify it mechanically, and hand you a working setup. No step requires
> network access.

---

(If I have already given you answers to any question below — install location,
language, the two Step 4 wiring choices — use them directly instead of asking again;
this document works both interactively and with pre-supplied answers.)

You are helping me install the **output-compress** skill — a tiered output-compression
discipline with a deterministic fidelity gate — from the `output-compress/` directory I
point you to. Follow these steps exactly, show me the output of every verification, and
do not skip a verification because something "looks right".

## Step 0 — Locate and inventory

Find the `output-compress/` directory (ask me for the path if you can't). Confirm it
contains: `SKILL.md`, `AGENTS.md`, `USAGE.md`, `README.md`,
`scripts/fidelity-check.py`, `scripts/usage-pacer.py`,
`scripts/claude-usage-fetch.py` (optional Claude usage feeder),
`scripts/codex-usage-fetch.py` (optional Codex usage feeder), and
`scripts/codex-handoff.py` (optional Codex packet-backed, memory-assisted helper). List what you
found. If files are missing, stop and tell me which.

## Step 0.5 — Read what you are about to install (do not skip)

Before copying anything into a privileged location (`~/.claude/`, hooks, settings),
**read `scripts/fidelity-check.py`, `scripts/usage-pacer.py`, and any hook helper you
plan to wire (`scripts/codex-handoff.py` on Codex) in full** and confirm to me in one
sentence each what they do and that they contain no secret printing, no writes outside
their documented log/flag/state paths, and no code unrelated to compression/pacing /
handoff. These scripts may later run on every prompt via a hook — treat them like any
third-party code you'd register into an agent's hot path. If anything looks unrelated
to what this document describes, STOP and show me.

## Step 1 — Detect my environment and install

Pick the FIRST matching case:

- **Claude Code** (a `~/.claude/` directory exists, or I tell you I use it): ask me
  first — **user-level** (`~/.claude/skills/output-compress`, all projects) or
  **project-level** (`<project>/.claude/skills/output-compress`, this repo only)?
  Then `cp -r <dir> <chosen>/`. The skill activates from `SKILL.md` frontmatter on
  the next session start.
- **Codex Skill** (`~/.codex/skills` or `$CODEX_HOME/skills` exists, or I tell you I
  use Codex skills): install this whole directory to
  `${CODEX_HOME:-~/.codex}/skills/output-compress`. Prefer the official installer when
  installing from GitHub:
  `python3 ~/.codex/skills/.system/skill-installer/scripts/install-skill-from-github.py --repo zeuikli/output-compress --path . --name output-compress`.
  If installing from a local checkout, copy the directory there only if the destination
  does not already exist. Validate with
  `python3 ~/.codex/skills/.system/skill-creator/scripts/quick_validate.py ${CODEX_HOME:-~/.codex}/skills/output-compress`.
- **Codex AGENTS.md fallback / always-loaded advisory**: append the skill section of
  `AGENTS.md` (everything after its first `---`) into my project's `AGENTS.md`, and
  copy the `scripts/` directory into the project (default expected path: `scripts/` at
  repo root — if you place it elsewhere, rewrite the `fidelity-check.py` paths inside
  the appended section to match).
- **Anything else**: copy `scripts/` into the project, and tell me to keep `SKILL.md`
  sections 1–4 in the agent's permanent context (system prompt or pinned doc). The
  gate script is plain Python 3 with zero dependencies.

## Step 2 — Language calibration (do not skip for non-English users)

Open the installed `scripts/fidelity-check.py`. The `NEGATIONS` list ships with
English **and Traditional Chinese** multi-character words already
(`"不能", "不要", "禁止", "沒有", "除非", "不可", "不得", "並非"`), and
`QUANTIFIER_BOUNDS` likewise ships English + Traditional Chinese — so a zh-TW agent
needs no change here. Ask me what language(s) my agent writes in, and add any that
aren't already covered. Prefer **multi-character** words — e.g. German
`"nicht", "kein"`; French `"pas", "jamais"`. CJK **single characters** (`不`, `非`, `未`, `勿`) match as substrings inside
many non-negation words (`不同`, `非常`, `未來`) because CJK has no word boundaries —
only add them if I accept the higher false-positive (extra gate FAIL) rate; Latin-script
words are boundary-matched automatically and don't have this problem. Also ask
whether my team uses structured markers (ticket IDs, status tags) and add them to
`CUSTOM_TAG_PATTERNS`. Show me the diff of what you changed.

## Step 3 — Mechanical verification (all four must pass; show raw output)

Run from wherever you installed it (`python3` ≥ 3.10):

1. **Gate PASS sentinel** — compression that only deletes filler must pass:
   ```bash
   printf 'It is worth noting that the deploy failed with exit code 3, and we should not retry it before 14:00.' > /tmp/oc-orig.txt
   printf 'Deploy failed exit code 3; do not retry before 14:00.' > /tmp/oc-comp.txt
   python3 <install-path>/scripts/fidelity-check.py --original /tmp/oc-orig.txt --compressed /tmp/oc-comp.txt
   echo "exit=$?"   # expect exit=0
   ```
2. **Gate FAIL sentinel** — a lost number and negation must be caught:
   ```bash
   printf 'Deploy failed; retry after lunch.' > /tmp/oc-bad.txt
   python3 <install-path>/scripts/fidelity-check.py --original /tmp/oc-orig.txt --compressed /tmp/oc-bad.txt
   echo "exit=$?"   # expect exit=1, with the lost elements listed on stdout
   ```
3. **Logging path** — re-run check 1 with
   `--log --log-file /tmp/oc-log.jsonl --level full --context install-test`, then
   `cat /tmp/oc-log.jsonl` and confirm one JSON line with `pass`, `level`, `context`,
   `saving_pct` fields.
4. **Pacer self-test** (run it regardless — it only proves the script executes;
   whether to actually wire pace coupling is decided in Step 4):
   ```bash
   python3 <install-path>/scripts/usage-pacer.py --self-test   # expect SELF-TEST PASS
   ```
   If `scripts/claude-usage-fetch.py` is present, self-test it too (offline, no network):
   ```bash
   python3 <install-path>/scripts/claude-usage-fetch.py --self-test   # expect SELF-TEST PASS
   ```
   If `scripts/codex-usage-fetch.py` is present, self-test it too (offline, no network):
   ```bash
   python3 <install-path>/scripts/codex-usage-fetch.py --self-test   # expect SELF-TEST PASS
   ```
   If `scripts/codex-handoff.py` is present, self-test it too (offline, no network):
   ```bash
   python3 <install-path>/scripts/codex-handoff.py --self-test   # expect SELF-TEST PASS
   ```

If any check fails, diagnose before proceeding — do not declare the install done.
Clean up the `/tmp/oc-*` files afterwards.

## Step 4 — Optional wiring (ask me; default = manual opt-in)

Ask me these two yes/no questions, then act:

1. **Auto-activation every turn?** If yes and I'm on Claude Code, create the
   UserPromptSubmit hook from `USAGE.md` §7 (full-rules-once-then-short-reminder
   pattern) and register it in my settings' hooks. If yes on Codex, prefer a Codex
   `UserPromptSubmit` command hook when I want per-turn injection; otherwise use the
   AGENTS.md fallback / always-loaded advisory section above. If no — I invoke it per
   conversation; nothing to do.
2. **Pace coupling / usage notification?** If yes, set up whatever refreshes the
   usage JSON (`OC_USAGE_FILE`, schema in `usage-pacer.py`'s docstring — I must tell
   you where my provider's usage numbers come from; if I don't have a source, skip
   this and note it as not-wired), then add the pace block from `USAGE.md` §7. **If
   I'm a Claude subscriber**, wire `scripts/claude-usage-fetch.py` instead of building
   my own feed — it refreshes the JSON from the official endpoint and finds my token
   automatically (chain documented in its docstring and `USAGE.md` §7); add its
   best-effort call two lines before the pacer. **If I'm a Codex user with ChatGPT
   auth/session token available**, wire `scripts/codex-usage-fetch.py` the same way;
   it reads `CODEX_SESSION_TOKEN_FILE`, `CODEX_ACCESS_TOKEN`, `CODEX_OAUTH_TOKEN`, or
   `${CODEX_HOME:-~/.codex}/auth.json` without printing token material. If no, skip —
   the skill works fully without it.

   Once pace coupling is wired, **handoff-aware pacing** comes with it: when the quota
   window is nearly exhausted (`used_pct >= 90%` and `< 0.5h` left), the pacer emits
   `HANDOFF_PREP` / `HANDOFF_HALT`. For Codex, packet persistence is explicitly opt-in
   with `OC_CODEX_HANDOFF_PACKET=1` (default off), defaults to `.codex/handoffs`, and
   may create untracked repo metadata. The JSON packet is authoritative; memory is
   advisory only and is not synchronized. The helper never writes `~/.codex/memories`
   or creates automation. A new packet remains pending until `--write-packet` validates
   its seven nonblank checkpoint fields and complete Git repository guard. PREP then gives the host
   `resume_at`, a stable thread-heartbeat name, and an exact-ID/path prompt;
   after `--mark-scheduled`, `--resume-context` rejects repo drift. `HANDOFF_HALT` never creates a wake.
   Tunables: `OC_HANDOFF_PCT` (90), `OC_HANDOFF_LEFT_H` (0.5),
   `OC_HANDOFF_MAX` (2), `OC_HANDOFF_RESUME_DELAY_MIN` (3), and
   `OC_CODEX_HANDOFF_MAX_AGE_S` (600).

   For Codex packet wiring, set `OC_CODEX_HANDOFF_PACKET=1` and register
   `scripts/codex-handoff.py --refresh` as the `UserPromptSubmit` hook plus the same
   helper as `SessionStart` with matcher `compact|resume`. `--refresh` accepts only a
   newly generated verdict. After writing the hook config, use `/hooks` to review and
   trust the exact definitions; project-local hooks also require a trusted project.

## Step 4.5 — Optional: advisory contract-field lint hook (recipe only — ask before installing)

If I'm on Claude Code and ask for it, this recipe wires a **PreToolUse** hook that
warns (never blocks) when a large agent-dispatch prompt is missing its contract
fields (Goal/Done-when/Return or this project's equivalent). It never edits or
rejects the prompt — it only prints a warning to stderr and always exits 0, so a
broken or over-eager lint can never stop a dispatch from going out.

A fuller reference implementation of this same idea (a stricter, project-tuned lint)
is known to exist in at least one parent workspace this package was derived from —
if you have access to one, prefer copying its lint logic over reimplementing from
scratch; this recipe is the portable, dependency-free fallback.

```json
{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "Agent",
        "hooks": [
          {
            "type": "command",
            "command": "python3 \"$CLAUDE_PROJECT_DIR/scripts/contract-field-lint.py\""
          }
        ]
      }
    ]
  }
}
```

Companion script (drop-in, zero dependencies — reads the tool-call JSON from stdin
per Claude Code's PreToolUse hook contract):

```python
#!/usr/bin/env python3
# scripts/contract-field-lint.py — advisory only: warns, never blocks, always exit 0.
import json, sys

MIN_BYTES = 500
REQUIRED = ("Goal", "Done-when", "Return")  # rename to your project's equivalent fields

try:
    payload = json.load(sys.stdin)
    prompt = payload.get("tool_input", {}).get("prompt", "")
except Exception:
    sys.exit(0)  # never block on a parse error — advisory only

if len(prompt.encode("utf-8")) >= MIN_BYTES:
    missing = [f for f in REQUIRED if f not in prompt]
    if missing:
        print(f"[output-compress advisory] dispatch prompt missing contract "
              f"field(s): {', '.join(missing)} — not blocking, just a heads-up.",
              file=sys.stderr)

sys.exit(0)
```

Adjust `REQUIRED` to match whatever contract-field names your project actually uses,
and the `matcher` if your agent-dispatch tool isn't named `Agent`.

## Step 5 — Hand-off summary (always produce this)

End with a short report: ① install location ② what you calibrated in Step 2
③ the four verification results (raw exit codes) ④ what optional wiring is on/off
⑤ the three rules I most need to remember day-1:

- Compression = **deletion, not rewriting** — if a sentence must be reworded to get
  shorter, leave it uncompressed.
- Never compress: the final user-facing reply, numbers, negations + their clause,
  paths/URLs/code, safety warnings, contract fields.
- Anything persisted (memory, logs, reports) goes through the fidelity gate; if the
  gate still fails after 2 level-drops, persist the ORIGINAL text and keep the
  `--log` record of the failure.
