# output-compress

Tiered, opt-in output compression with a deterministic fidelity gate. See `SKILL.md`
for the full rules (Claude Code / Agent Skills format) or `AGENTS.md` for the same
rules in Codex's format. `USAGE.md` covers day-to-day workflows, log analysis, and
auto-activation wiring (§7).

## Install

**Fastest path:** paste `INSTALL-PROMPT.md` (below the `---`) into any AI assistant
with filesystem access — it detects your environment, installs, calibrates the
negation list for your language, and runs the mechanical verification for you.

Manual paths:

**Claude Code (Agent Skills):**

```bash
cp -r output-compress ~/.claude/skills/
```

Claude Code will pick up `SKILL.md`'s frontmatter automatically on next session start.

**Codex Skill:**

Install the repo root as a Codex skill into `$CODEX_HOME/skills`:

```bash
python3 ~/.codex/skills/.system/skill-installer/scripts/install-skill-from-github.py \
  --repo zeuikli/output-compress --path . --name output-compress
```

Codex will make the `SKILL.md` frontmatter available on the next turn or new session.
If the destination already exists, remove or rename the old
`$CODEX_HOME/skills/output-compress` directory only after confirming you do not need
that copy.

**Codex AGENTS.md fallback / always-loaded advisory:**

Append the section from `AGENTS.md` (everything after the `---`) into your project's
own `AGENTS.md` file, or copy this whole directory into your project and add a pointer
line to your `AGENTS.md`, e.g. `See output-compress/AGENTS.md for the compression
advisory.`

For the AGENTS.md fallback, also copy `scripts/fidelity-check.py` into your project so
the gate command in the docs resolves to a real path. With a Codex skill install, use
the installed skill path, e.g.
`$CODEX_HOME/skills/output-compress/scripts/fidelity-check.py`. `scripts/usage-pacer.py`
is optional — only needed for the pace-coupling described in `SKILL.md` / `USAGE.md`
§7.

## Quickstart: fidelity gate demo

The gate is the deterministic script in `scripts/fidelity-check.py`. It diffs
whitelisted elements (numbers, negations, code, paths, URLs, tags) between an original
and a compressed file. Both examples below use `--log` so you can see the JSONL trail
it leaves — a failed (exit 1) attempt is logged too, not just discarded, because the
failure pattern itself is useful signal (see `SKILL.md` §5).

**GOOD — compression that preserves every whitelisted element (exit 0):**

```bash
cat > /tmp/orig-good.txt <<'EOF'
The deploy failed 3 times. Do not retry without checking `deploy.log` first.
See https://example.com/runbook for the rollback steps.
EOF

cat > /tmp/comp-good.txt <<'EOF'
Deploy failed 3 times. Do not retry without checking `deploy.log`.
Rollback: https://example.com/runbook
EOF

rm -f ./compress-log.jsonl
python3 scripts/fidelity-check.py \
  --original /tmp/orig-good.txt --compressed /tmp/comp-good.txt \
  --log --level full --context report
echo "exit=$?"
wc -l ./compress-log.jsonl
```

Expect `exit=0` and `./compress-log.jsonl` to contain exactly 1 line with `"pass": true`.

**BAD — compression that drops the retry count and the negation (exit 1):**

```bash
cat > /tmp/orig-bad.txt <<'EOF'
The deploy failed 3 times. Do not retry without checking `deploy.log` first.
EOF

cat > /tmp/comp-bad.txt <<'EOF'
Deploy failed. Check deploy.log then retry.
EOF

python3 scripts/fidelity-check.py \
  --original /tmp/orig-bad.txt --compressed /tmp/comp-bad.txt \
  --log --level full --context report
echo "exit=$?"
wc -l ./compress-log.jsonl
```

The BAD example strips both the number (`3`) and the negation (`Do not retry`), and
also drops the inline-code backticks around `deploy.log` — the script reports all three
categories as missing and exits 1, which per the skill's rule means: drop one
compression level and retry, or fall back to the original text. `./compress-log.jsonl`
now has 2 lines — the first `"pass": true` entry from the GOOD run above, and a second
`"pass": false` entry recording exactly what this failed run lost.

## Customizing the whitelist

If your project uses its own structured tags (ticket IDs, custom status markers,
evidence-level flags), add regex patterns to `CUSTOM_TAG_PATTERNS` at the top of
`scripts/fidelity-check.py` rather than editing the built-in categories.

## Attribution

Compression syntax and the three-tier (`lite`/`full`/`ultra`) idea are derived from
[`github.com/juliusbrussee/caveman`](https://github.com/juliusbrussee/caveman). This
version differs from upstream in two ways: (1) a deterministic fidelity gate replaces
upstream's reliance on model self-judgment of "meaning stays clear", and (2)
model-tier compression caps replace a single fixed compression level for all readers.

## License

MIT — see [LICENSE](LICENSE).
