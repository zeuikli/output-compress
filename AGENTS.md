# Output-Compress (AGENTS.md section)

> Codex reads `AGENTS.md`, not `SKILL.md` — this file restates the same rules for that
> workflow. Append the section below into your project's own `AGENTS.md` (or keep this
> file alongside your code and point your `AGENTS.md` at it). Full rationale and the
> fidelity-check script live in this same directory; see `README.md` for install steps.

---

## Output-Compress: tiered, whitelist-safe, mechanically verified compression

Trigger: user says "compress", "output-compress", "compress lite|full|ultra", or asks to
shorten/condense scratchpad or intermediate output. **Never apply this automatically —
it is opt-in only, per turn or per scope**, unless you've deliberately made this section
always-loaded advisory (see "Always-on" below).

Do NOT use for: the final user-facing response's language, safety/irreversible-action
confirmations, contract fields (Goal/Non-goals/Done-when/Return), or audit/review
findings that must stay verbatim.

### Levels

- `lite` — strip filler words and pleasantries only; keep full sentences and articles.
- `full` — also drop articles, allow sentence fragments, lead with the result before the
  explanation. Expected savings vary by language: an informal local measurement on CJK
  prose landed around 15-18% bytes saved (n=4), well below the upstream project's
  self-reported ~65% average on English text (CJK has fewer articles/filler words to
  strip). Measure your own before quoting a figure.
- `ultra` — also drop connective words outside the whitelist. Highest savings, highest
  fidelity risk — pair with the gate below every time.

### Model-tier caps

- Small/cheap models (Haiku-class equivalents): cap at `lite`.
- Mid-tier models (Sonnet-class equivalents): cap at `full`.
- Frontier models (Opus/GPT-5-class equivalents): cap at `ultra`.
- Any model you haven't calibrated yet: start at `lite`, raise only after 5-10 tasks
  pass the fidelity gate cleanly.

"Reader" = who consumes the text (a sub-agent prompt's reader is the worker model; a
saved log's reader is any future session, so use `lite` for anything long-lived).

### Never-compress whitelist

1. Code blocks, inline code, URLs, file paths, commands and their output, env vars
2. All numbers, including ones embedded in prose (percentages, amounts, versions, line
   numbers, dates)
3. Negation words and their entire clause (not/never/unless/except/no/cannot, and
   local-language equivalents) — keep the clause whole
4. Any structured tags your project uses for provenance/status (list your own regex
   patterns in `scripts/fidelity-check.py`'s `CUSTOM_TAG_PATTERNS`)
5. Safety-critical statements (irreversible-action confirmations, incident/priority-0
   report language) — never compress at all
6. Contract fields (Goal / Non-goals / Done-when / Return {} or equivalent)
7. Hedges/qualifiers (only/may/provisional/unverified/tentative/estimated/caveat/assume/
   subject to, and local-language equivalents) — occurrence count must not decrease;
   stripping the caveat while keeping the evidence ("decontextualization") flips
   meaning the same way a dropped negation does. Word list = `HEDGES` in
   `scripts/fidelity-check.py`.

### Compression is deletion, not rewriting

Only strip filler words, articles, and redundant connectives — never rephrase a
sentence to make it shorter. Rewriting risks silently swapping which literal words
carry a whitelisted element, most often negations: turning "rather than X" into "does
not include X" replaces one negation word with a different one. The fidelity gate will
usually flag this as a lost element, but the real fix is not attempting the rewrite in
the first place — a pure deletion pass never has this failure mode. If a sentence needs
rewording (not just trimming) to get shorter, leave it uncompressed.

Negation-first ordering: before deleting anything, enumerate the negation/quantifier
clauses (whitelist item 3) and lock those sentences whole, then compress the rest —
this eliminates the most common first-round gate failure (`negation_counts`).

### Fidelity gate — run after every compression, before persisting anything

```bash
python3 "${CODEX_HOME:-$HOME/.codex}/skills/output-compress/scripts/fidelity-check.py" --original /tmp/orig.txt --compressed /tmp/comp.txt \
  --log --level full --context report
```

Exit 0 = every whitelisted element survived. Exit 1 = prints what was lost — drop one
compression level and retry (max 2 retries, then give up and keep the original text).
This is a deterministic regex/set-diff script, **not an LLM judge** — the whole point is
to avoid asking a model to self-certify "the meaning is still the same." When output is
about to be persisted and the gate still fails after retries, persist the original text
— but keep the `--log` flag on so the failure is recorded; a log of which level/context
combinations keep failing is useful signal for lowering that cap later, not just noise
to discard. `--log` appends JSONL to `--log-file` (default `./compress-log.jsonl`);
each record includes `grounded_pct` (fraction of compressed tokens present in the
original — should sit at ~100 under deletion-only compression; a drop means rewriting
crept in).

### Delegating compression to a cheaper model (optional)

Deciding *which sentences are deletable* is a judgment call and may be delegated to a
cheap/small worker model; the fidelity gate is deterministic and **must always be run
by the orchestrating agent itself, never by the worker** — a worker's "gate passed"
claim is not evidence. Prefer a cheap executor and avoid frontier-model self-compression
of long text: larger compressor models measure as *less* faithful to the source
(arXiv 2602.09789, "scaling paradox"). Delegate only when the text will be persisted,
the original is ≥4KB, and the `--coverage` pre-check does not say `skip`; on gate
failure fall back to the original text instead of re-delegating. Tag delegated runs'
`--context` as `delegated-<original context>` so they can be compared against inline runs.

### Known limits

- The `path` regex can miss bare directory names in non-Latin-script prose with no file
  extension — spot-check manually before persisting compressed output.
- Negation fidelity is checked by "count did not decrease," which can't catch a
  negation word surviving while its clause's scope silently changed — use `lite` (or
  skip compression) wherever a negation carries real logical weight.
- The hedge list (whitelist item 7) is deliberately conservative to keep false FAILs
  low; lowercase modal `may` is protected, while month-name `May` is ignored to avoid
  date false positives. Extend `HEDGES` with your language's qualifier words,
  preferring multi-word phrases.
- Savings figures are the upstream project's self-report on one model; verify on your
  own tasks before treating them as a guarantee. Your own logged numbers are also a
  self-selected sample (only runs where compression was attempted *and* logged appear)
  — quote them with that qualifier, never as a commitment.

### Always-on (optional)

The portable way to make this advisory always-present is to keep this section
permanently in your project's `AGENTS.md`, since that file is loaded into context every
session. Codex also supports lifecycle hooks, so a project can additionally inject a
short pacer/advisory line from a `UserPromptSubmit` command hook if it wants per-turn
automation. In either setup, actual invocation (deciding to compress a given piece of
output) should still be gated by an explicit phrase like "when I say compress," unless
you've deliberately decided this should be continuously applied rather than opt-in.

## Pace coupling (optional)

A portable pacing companion ships as `scripts/usage-pacer.py` (bring-your-own usage
JSON; deterministic AHEAD/ON_PACE/BEHIND verdicts + a once-per-window notification
arm). In Codex, run it from a lifecycle hook, a wrapper, cron, or another scheduler and
inject or paste its one-line verdict into the conversation when it changes. The rule it
drives is in `SKILL.md` ("Pace-aware level adjustment"): AHEAD means bump compression
one level within the reader-tier cap, anything else means default level. The fidelity
gate is unaffected either way.

The pacer also emits an absolute-threshold `compress`/`compress_msg` pair — orthogonal
to the burn-rate verdict, fires even when `ON_PACE` — that bumps one level at 80% used
and jumps to the reader-tier cap at 95%; see `SKILL.md` §"Absolute-threshold escalation".

It also emits **handoff states** when the window is nearly exhausted (`used_pct >= 90%`
and `< 0.5h` left): `HANDOFF_PREP` and the circuit-breaker `HANDOFF_HALT`. These are
advisory verdicts only; do not treat hook execution or scheduling as deterministic.

Codex has an opt-in packet-backed, memory-assisted helper at
`scripts/codex-handoff.py`. With `OC_CODEX_HANDOFF_PACKET=1`, a fresh handoff verdict
is persisted as an atomic pending JSON packet under the git root's `.codex/handoffs` (override with
`OC_CODEX_HANDOFF_DIR`), with a derived Markdown view. The JSON packet is authoritative;
Codex memory is advisory and the helper never writes `~/.codex/memories`. Packet files
may be untracked repo metadata. `--refresh` accepts only a newly generated verdict;
without refresh, the default verdict freshness window is 600 seconds. The agent must
use `--write-packet` to validate seven nonblank checkpoint fields and a complete Git
repository guard before PREP becomes ready. The helper then provides `resume_at`, a stable name, and an exact-ID/path prompt
for a same-task thread heartbeat; it does not create automation. Resume through
record host receipts with `--mark-scheduled`, resume the scheduled packet with
`--resume-context`, and finish with
`--mark-complete`. `HANDOFF_HALT` never provides a wake instruction. See `USAGE.md` §7.
