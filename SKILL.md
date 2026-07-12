---
name: output-compress
description: 'Tiered output compression (caveman-derived): an explicitly opt-in, token-saving rewrite mode with a never-compress whitelist, model-tier compression caps, and a deterministic fidelity gate (no LLM self-judgment). Use when the user types output-compress, /compress, "compress lite|full|ultra", or asks to shorten/condense internal or scratch output. Do NOT use for: the final user-facing response language, safety/irreversible-action confirmations, contract fields (Goal/Non-goals/Done-when/Return), audit or review findings that must stay verbatim, or as a default/always-on behavior.'
metadata:
  version: 1.1.0
---

# Output-Compress — tiered, whitelist-safe, mechanically verified compression

> Origin: `github.com/juliusbrussee/caveman` compression syntax. This version fixes the
> upstream's two gaps — no fidelity scoring, no model-tier calibration — by adding a
> never-compress whitelist, a tier-capped compression matrix, and a deterministic
> fidelity-check script. **Compression is opt-in, never default.** **The fidelity check
> is deterministic — it is never model self-judged.**

## Usage

`/compress <level> [scope]` — `level` is one of `lite | full | ultra`. `scope` defaults
to internal output from this point forward (scratchpad text, sub-agent intermediate
output, the body of a mechanical report). If no level is given, use the cap for the
current reader tier (see §2). This skill is never applied automatically — it must be
explicitly invoked per turn or per scope.

## 1. Compression levels

| level | rule | expected savings |
|---|---|---|
| `lite` | strip filler words / pleasantries / throat-clearing only; keep full sentences and articles | low |
| `full` | + drop articles, allow sentence fragments, lead with the result then explain | medium — measured ~13% bytes saved on CJK prose (n=13, gate-verified organic sample; earlier n=4 informal sample showed 15-18%); upstream's ~65% self-report is for English-style text, which has more articles/filler words to strip than CJK does, so English-heavy output may land closer to the upstream figure than CJK does |
| `ultra` | + drop connective words (only in sentences outside the whitelist) | high — fidelity risk rises with it (see §4 gate) |

A stylized "telegraphic" mode that changes the response's natural language (e.g. classical-register rewrites) is explicitly out of scope for this skill — it conflicts with keeping the final user-facing response in the user's normal language. Add it as a separate opt-in mode if you need it.

**Two ledgers — output tokens vs input tokens.** Compression pays off in two distinct ways;
know which one you're buying before you spend the effort:

- **Disposable text** (a status note, an intermediate answer nobody will re-read): just
  *write it short in the first place* — that saves output tokens directly, and there is no
  original to diff so the fidelity gate doesn't apply.
- **Persisted text** (logs, memory files, reports, sub-agent prompts that future turns
  re-read): use the full three-step flow (compress → gate → persist). This *costs* a little
  output now but buys input-token savings on every future read — and the gate is mandatory
  because a lost detail here compounds across sessions.

Mixing these up wastes effort: gating disposable text adds overhead for nothing, and
skipping the gate on persisted text trades permanent fidelity for a one-time saving.

**Single-use dispatch prompt ruling (which ledger a one-shot sub-agent prompt belongs
to was previously undecided — decided here):** a prompt that will be sent once and not
re-read may be generated directly in already-compressed style, skipping the
original+compressed+gate flow, **only if every contract field
(Goal/Non-goals/Done-when/Return or this project's equivalent naming) is written
verbatim in full** — i.e. compress everything around the contract fields, never the
fields themselves. If you are not confident the contract fields will survive
untouched, use the full original+compressed+gate flow instead; do not skip the gate on
a prompt whose contract fields you also intend to compress.

## 2. Model-tier compression caps

Weaker models reconstruct meaning from fragments less reliably, so they get a lower cap; stronger models can absorb more aggressive compression and still recover intent.

| reader tier | cap | rationale |
|---|---|---|
| small models (Haiku-class) | `lite` | weak fragment/ellipsis reconstruction; these models generally need more explicit, step-level guidance, which compression works against |
| mid-tier (Sonnet-class) | `full` | this is the tier caveman's default level was originally calibrated against |
| frontier (Opus/GPT-5-class) | `ultra` | strong enough to recover elided connectives and implicit structure |
| any model not yet calibrated | start at `lite` | run 5-10 representative tasks and check the fidelity gate before raising the cap for that model |

"Reader" = who consumes this text, not who produces it. A sub-agent prompt's reader is
the worker model's tier; a log or memory file's reader is any future session at any tier,
so use the lowest common tier (`lite`) for anything long-lived. This matrix is derived
from general reasoning about model capability, not from local measurement — calibrate
it against your own fidelity-gate failure rate before trusting the caps as-is.

**Coverage 前置判斷 (pre-check before compressing):** before compressing an agent-dispatch
prompt or archival/log text, run `scripts/fidelity-check.py --coverage --original <file>`
and follow its recommendation (`skip` / `lite` / `tier-cap`, thresholds ≥40% / 20–40% /
<20% whitelist-covered bytes) rather than defaulting straight to the reader-tier cap.
These thresholds are provisional (n=2, 2026-07-12): whitelist-dense dispatch prompts
saved only 6.8% at `ultra` and 2.4% at `full` after gate retries — for that kind of
text, the expected saving is often smaller than the cost of a gate round-trip, so
checking coverage first can save you the compress→gate→retry cycle entirely.

## 3. Never-compress whitelist (fidelity gate's comparison baseline)

1. Code blocks / inline code / URLs / file paths (**including bare filenames** like `deploy.log` — card7 2026-07-12) / commands and their output / env vars (assignment form `NAME=` — the bare-acronym match was card1's false-FAIL source)
2. **All numbers** (percentages, amounts, version numbers, line numbers — embedded in prose too); **dates compared as whole tokens** and **repeated values must keep their occurrence count** (multiset, card2 2026-07-12)
3. **Negation words AND quantifier bounds, with their entire clause scope** (not/never/unless/except/no/cannot + at most/at least/up to/no more than, and local-language equivalents) — dropping a bound turns it into an exact value, the same silent flip as dropping a negation (card3 2026-07-12)
4. **Structured tags your project uses** for provenance/status/review markers (e.g. `[STATUS]`, ticket IDs, evidence-level flags) — list your own patterns in the fidelity-check script's `CUSTOM_TAG_PATTERNS`
5. Safety-critical statements (irreversible-action confirmations, incident/priority-0 report language) — never compress these at all; this whitelist entry is a second line of defense on top of the "do not use" rule above
6. Contract fields (Goal / Non-goals / Done-when / Return {} or equivalent
   handoff-contract structure) — **skip these as whole blocks, never compress them
   sentence-by-sentence.** Evidence: a field run compressed a Done-when block clause by
   clause and deleted an inline verification command embedded inside it; the gate
   caught it, but the failure mode was treating a contract field like ordinary prose
   instead of skipping the entire field untouched (2026-07-12).

## 4. Fidelity gate (mechanical, run after every compression)

```bash
python3 scripts/fidelity-check.py \
  --original /tmp/orig.txt --compressed /tmp/comp.txt
# exit 0 = every whitelisted element survived
# exit 1 = prints what was lost -> drop one compression level and retry
#          (max 2 retries; if it still fails, give up and use the original text)
# For output about to be persisted (a log, memory, or report file): if it still fails
# after retries, persist the ORIGINAL text, not the failing compressed version —
# but still re-run with --log so the failure gets recorded (see §5, failure
# patterns by level/context are useful calibration signal, not just noise).
```

The script extracts each whitelist category via regex from both versions and diffs the
two sets — it is **not an LLM judge**. This avoids the noise and inconsistency that
comes from asking a model "does this still mean the same thing" (which is exactly the
self-judgment pattern this skill is designed to replace). Before any compressed output
is persisted (a log, a memory file, a saved report), show the fidelity-check output
alongside it — don't persist compressed text on a bare claim of "still accurate."

**Compression is deletion, not rewriting.** The only allowed operations are: strip
filler words, drop articles, drop redundant connectives, cut sentence fragments. Never
rephrase a sentence to make it shorter — paraphrasing risks silently swapping which
literal words carry a whitelisted element, most often negations. Example: rewriting
"rather than X" into "does not include X" replaces one negation word with a different
one; the gate correctly flags this as a lost element (the original negation word's
count dropped), but the deeper problem is that the rewrite was unnecessary risk in the
first place — a pure deletion pass never has this failure mode. If a sentence needs to
be reworded (not just trimmed) to get shorter, leave that sentence uncompressed.

Rewrite ≠ delete, even for a single word: a field run rewrote `Provide at least:` into
`Min set:` — shorter, but it silently dropped the quantifier bound `at least`,
turning a minimum into an unqualified list. The gate correctly intercepted it
(2026-07-12). Compression may only *delete* tokens that are already there; substituting
a shorter phrase for a longer one is rewriting, not compression, even when it looks
like a trivial word swap.

Add `--log --level <L> --context <C>` to also append a JSONL record to
`compress-log.jsonl` (path configurable via `--log-file`) — see the script's `--help`
and §5 below for what the log is for.

**Gate efficacy (field run, 2026-07-12):** 2/2 true-positive fidelity FAILs caught (a
dropped quantifier, a deleted inline verification command), 0 false-positives, both
fixed and re-passed on the first retry — the mechanical (non-LLM-judge) approach held
up under real dispatch-prompt compression, not just synthetic test cases.

## 5. Calibration and known limits

- The 65% savings figure in §1 is the upstream project's self-reported number from a
  single model on English text; treat it as a rough prior, not a local guarantee, until
  you've run your own before/after comparison on a handful of representative tasks. The
  one local measurement so far (§1, CJK prose) landed well below it.
- A single lost number or negation typically costs several times its saved tokens once
  you account for the back-and-forth needed to catch and re-ask for it — reserve `ultra`
  for output you can afford to discard/regenerate, not for anything a decision depends on.
- Run compression with `--log` and periodically inspect `compress-log.jsonl`'s `pass`,
  `missing_keys`, and `saving_pct` fields: a level×context combination with a high
  failure rate is a signal to lower that combination's cap (§2); a `saving_pct` that
  stays low for a given context is a signal that compression isn't worth the fidelity
  risk there. This applies to both passing and failing runs, so keep logging failures
  instead of silently discarding them.

## Known limits (carried over from the source implementation)

- The `path` regex can miss bare directory names embedded in non-Latin-script prose
  (no file extension to anchor on) — spot-check path-heavy text manually before
  persisting compressed output at file scope.
- Negation fidelity is checked by "count does not decrease," which cannot detect a
  negation word surviving while its clause's scope silently changed — for anything
  where a negation carries real logical weight, use `lite` or skip compression.

## Auto-activation (optional)

See `USAGE.md` §7 for wiring this skill to run automatically every turn (a
UserPromptSubmit-style hook for Claude Code, an always-loaded AGENTS.md section for
Codex) instead of invoking it manually per conversation.

## Pace-aware level adjustment (optional)

If your environment exposes a usage-pacing signal (e.g. a hook that compares actual
quota burn against elapsed window time and emits AHEAD / ON_PACE / BEHIND verdicts),
you can couple it to compression levels:

- **AHEAD** (burning quota faster than the window is elapsing): bump the compression
  level by one, *within the reader-tier cap from §2* — never for small-model readers,
  whose cap is `lite` for fidelity reasons, not budget ones.
- **ON_PACE / BEHIND**: revert to the default level. Compression above the default is
  a budget lever, not a permanent setting.
- **Always log pace-driven bumps** (`--log`, keep the `level` field): periodically
  compare the fidelity-gate failure rate of pace-bumped runs against normal runs — if
  the bumped group fails noticeably more, drop the coupling; the budget saving isn't
  worth the fidelity loss.

A portable reference pacer ships as `scripts/usage-pacer.py` (bring your own usage
JSON — schema in its docstring; includes a once-per-window user-notification arm and a
self-test). Provider-specific official-usage feeders ship alongside it:
`scripts/claude-usage-fetch.py` for Claude subscriptions and
`scripts/codex-usage-fetch.py` for Codex/ChatGPT sessions. Each refreshes the same
neutral JSON file before the pacer runs, while the pacer itself stays provider-neutral.
Hook wiring with the injection-diet rules is in `USAGE.md` §7. Keep the two
responsibilities separate: the pacer *decides when*, this skill *decides how much*, and
the fidelity gate stays the final arbiter either way.

**Absolute-threshold escalation (orthogonal to pace verdicts)**: independent of the
AHEAD/ON_PACE/BEHIND burn-rate verdict above, the pacer also tracks raw quota level —
`used_pct >= 80%` bumps compression one level within the reader-tier cap; `>= 95%` jumps
straight to the cap. This fires even when `ON_PACE` (a window can be right on schedule
and still be at 83% used). Dedup on state change belongs to the injecting hook, not the
pacer — see `compress`/`compress_msg` in its output schema.

## Changelog

- **1.1.0** (2026-07-12): added `scripts/fidelity-check.py --coverage --original
  <file>` pre-check mode (§2); added the Coverage 前置判斷 note, the contract-fields
  whole-block hard rule (§3 item 6), the rewrite≠delete quantifier case (§4), and the
  single-use dispatch prompt ruling (§1) — all from a field run's fidelity-gate
  findings (see `USAGE.md` for a worked `--coverage` example).
- **1.0.0**: initial tiered compression + whitelist + fidelity gate + pace coupling.
