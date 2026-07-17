#!/usr/bin/env python3
"""Fidelity gate: deterministic diff of must-keep elements before/after compression.

No LLM self-judgment is involved — this is a pure regex/set-diff check.

Usage: fidelity-check.py --original ORIG --compressed COMP [--json]
                          [--log [--log-file PATH] [--level L] [--context C]]
       fidelity-check.py --coverage --original ORIG
Exit 0 = nothing lost (or coverage mode ran). Exit 1 = something lost (stdout lists
what). Exit 2 = usage error.

Categories map to SKILL.md "Never-compress whitelist". If you change the whitelist
in SKILL.md, update PATTERNS / NEGATIONS / QUANTIFIER_BOUNDS / HEDGES /
CUSTOM_TAG_PATTERNS below to match.

--log appends a JSONL record regardless of pass/fail. A failed (exit 1) run is still
worth logging: when compressed text is about to be persisted and the gate still fails
after retries, the fallback is to persist the ORIGINAL text — but keep logging the
failure anyway, since the pattern of what keeps failing (which level, which context)
is a signal for lowering that combination's compression cap later.

--coverage mode (a pre-check, not the gate itself): estimates what fraction of a file
is already whitelist material *before* you spend effort compressing it. Prints a
one-line JSON {"coverage_pct": <float>, "recommendation": "skip"|"lite"|"tier-cap"} and
always exits 0. See SKILL.md's Coverage 前置判斷 note for the field-measured rationale
(whitelist-dense dispatch prompts saved single-digit percentages after gate retries).
"""
import argparse
import json
import re
import sys
from pathlib import Path

# Add project-specific structured-tag regexes here (e.g. ticket IDs, custom
# evidence markers, review-status flags). Each entry is matched independently
# and treated the same as the built-in "tag" category.
CUSTOM_TAG_PATTERNS: list[str] = [
    # r"\bJIRA-\d+\b",
    # r"\[status:(?:open|closed|blocked)\]",
]

# Extraction regex per category (key = SKILL.md whitelist item).
PATTERNS = {
    "code_block": re.compile(r"```.*?```", re.S),
    "inline_code": re.compile(r"`[^`\n]+`"),
    "url": re.compile(r"https?://[^\s)\]>]+"),
    "path": re.compile(r"(?<![\w/])(?:/|~/|\./)[\w.\-/]+|\b[\w.\-]+/[\w.\-/]+\.\w{1,8}\b|\b[\w\-][\w.\-]*\.(?:log|ya?ml|json|jsonl|md|py|sh|ts|tsx|js|txt|conf|cfg|toml|csv|sql|swift|service)\b"),  # 2026-07-12: bare filenames with known extensions (card7)
    "date": re.compile(r"\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}(?::\d{2})?Z?)?"),  # 2026-07-12: composite token — digit-set compare let dates reorder (card2)
    "number": re.compile(r"\d+(?:[.,]\d+)*%?"),
    # Generic structured tags: [ALLCAPS], [key:value], evidence-tier markers.
    "tag": re.compile(r"\[[A-Z][A-Z0-9_:.\-]{1,30}\]|evidence-tier\s*[:=]\s*\w+"),
    "env_var": re.compile(r"\b[A-Z][A-Z0-9_]{2,}(?=\s*=[^=])"),  # 2026-07-12: assignment-form only — bare \b alternative matched every ALL-CAPS acronym (card1)
}
# Negation words: occurrence count must not drop (a mechanical approximation
# for "the whole negated clause must survive"). Add your own language's
# negation words here before first use.
NEGATIONS = [
    "not", "never", "unless", "except", "no ", "cannot", "don't", "won't",
    "不能", "不要", "禁止", "沒有", "除非", "不可", "不得", "並非",
]
# 2026-07-12 (card3): quantifier bounds flip meaning silently when dropped
# ("at most 3" -> "3"): same discipline as negations — count must not decrease.
QUANTIFIER_BOUNDS = ["at most", "at least", "up to", "no more than", "fewer than", "最多", "至少", "不超過", "上限", "下限"]
# 2026-07-17 (arXiv 2606.29251 "decontextualization"): evidence can survive
# compression while the caveat/qualifier needed to interpret it is stripped —
# a named second fidelity axis. Hedge/qualifier occurrence count must not
# decrease, same discipline as negations. The word list is deliberately
# conservative (multi-word phrases preferred, to lower false-FAIL rate). The word
# "may" is counted case-sensitively so modal "may" is protected without treating the
# month name "May" as a hedge. Add your own language's hedge words here before first use.
HEDGES = [
    "only", "may", "provisional", "unverified", "tentative", "estimated", "caveat",
    "assume", "assumes", "assuming", "subject to",
    "僅", "暫定", "假設", "可能", "未驗證", "未實測", "單一來源", "待驗證",
]
CASE_SENSITIVE_HEDGES = {"may"}


def _neg_regex(w: str) -> str:
    # Latin words get word boundaries so "not" doesn't match inside "noting"/"nothing"
    # (real false positive caught by install E2E 2026-07-11). CJK has no word
    # boundaries -> plain substring; prefer multi-character negation words there.
    if re.fullmatch(r"[A-Za-z']+", w):
        return r"\b" + re.escape(w) + r"\b"
    return re.escape(w)


def extract(text: str) -> dict:
    out = {}
    stripped = text
    # Pull code blocks out first so their contents aren't double-counted
    # under other categories.
    out["code_block"] = sorted(PATTERNS["code_block"].findall(stripped))
    stripped = PATTERNS["code_block"].sub(" ", stripped)
    for key in ("inline_code", "url", "path", "tag", "env_var", "date", "number"):
        out[key] = sorted(PATTERNS[key].findall(stripped))
    if CUSTOM_TAG_PATTERNS:
        custom_hits = []
        for pat in CUSTOM_TAG_PATTERNS:
            custom_hits.extend(re.findall(pat, stripped))
        out["tag"] = sorted(set(out["tag"]) | set(custom_hits))
    out["quantifier_counts"] = {
        w: len(re.findall(_neg_regex(w), stripped, re.I)) for w in QUANTIFIER_BOUNDS
    }
    out["negation_counts"] = {
        w: len(re.findall(_neg_regex(w), stripped, re.I)) for w in NEGATIONS
    }
    out["hedge_counts"] = {
        w: len(re.findall(_neg_regex(w), stripped, 0 if w in CASE_SENSITIVE_HEDGES else re.I))
        for w in HEDGES
    }
    return out


def _whitelist_spans(text: str) -> list[tuple[int, int]]:
    # Same categories as extract(), but keeping match offsets (into `text`,
    # not the code-block-stripped copy) so overlapping whitelist hits can be
    # deduped by span rather than double-counted across categories.
    spans = [m.span() for m in PATTERNS["code_block"].finditer(text)]
    # Blank out code blocks with same-length spaces so every other pattern's
    # offsets still line up with the original `text`.
    stripped = PATTERNS["code_block"].sub(lambda m: " " * len(m.group(0)), text)
    for key in ("inline_code", "url", "path", "tag", "env_var", "date", "number"):
        spans.extend(m.span() for m in PATTERNS[key].finditer(stripped))
    for pat in CUSTOM_TAG_PATTERNS:
        spans.extend(m.span() for m in re.finditer(pat, stripped))
    for w in NEGATIONS:
        spans.extend(m.span() for m in re.finditer(_neg_regex(w), stripped, re.I))
    for w in QUANTIFIER_BOUNDS:
        spans.extend(m.span() for m in re.finditer(_neg_regex(w), stripped, re.I))
    for w in HEDGES:
        spans.extend(m.span() for m in re.finditer(
            _neg_regex(w), stripped, 0 if w in CASE_SENSITIVE_HEDGES else re.I))
    return spans


def _merge_spans(spans: list[tuple[int, int]]) -> list[tuple[int, int]]:
    merged: list[tuple[int, int]] = []
    for s, e in sorted(spans):
        if merged and s <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], e))
        else:
            merged.append((s, e))
    return merged


def coverage_pct(text: str) -> float:
    total = len(text.encode("utf-8"))
    if not total:
        return 0.0
    merged = _merge_spans(_whitelist_spans(text))
    covered = sum(len(text[s:e].encode("utf-8")) for s, e in merged)
    return round(covered / total * 100, 1)


def diff(orig: dict, comp: dict) -> dict:
    missing = {}
    for key, vals in orig.items():
        if key in ("negation_counts", "quantifier_counts", "hedge_counts"):
            drops = {
                w: (n, comp[key].get(w, 0))
                for w, n in vals.items()
                if comp[key].get(w, 0) < n
            }
            if drops:
                missing[key] = drops
            continue
        if key in ("number", "path", "date"):
            # multiset (card2): occurrence count must not decrease — set-membership
            # let duplicate values ("retry 5s, timeout 5s") vanish silently
            from collections import Counter
            co, cc = Counter(vals), Counter(comp.get(key, []))
            lost = [v for v in co if cc[v] < co[v]]
        else:
            lost = [v for v in vals if v not in set(comp.get(key, []))]
        if lost:
            missing[key] = lost
    return missing



def _est_tokens(text: str) -> int:
    # Cheap tokenizer-free estimate (F2, 2026-07-12): CJK chars ~1 token each;
    # remaining text ~1 token per 4 bytes. Bytes alone overstate CJK 3x
    # (3 bytes/char vs ~1 token/char), which made saving_pct incomparable
    # across languages — this column makes the log's savings token-denominated.
    cjk = sum(1 for ch in text if '\u4e00' <= ch <= '\u9fff' or '\u3040' <= ch <= '\u30ff')
    non_cjk_bytes = len(text.encode('utf-8')) - cjk * 3
    return cjk + max(0, non_cjk_bytes) // 4

def _grounded_pct(orig_text: str, comp_text: str) -> float:
    """Groundedness proxy (2026-07-17, borrowing the groundedness-drop measurement
    idea from arXiv 2503.19114): fraction of the compressed text's word-level tokens
    that appear in the original. Under the "deletion, not rewriting" rule a pure
    deletion pass should score ~100; a drop means rewriting/generation crept in
    (ungrounded-content risk). Deterministic, no LLM."""
    tok = re.compile(r"[\u4e00-\u9fff]|[A-Za-z0-9_]+")
    orig_set = set(tok.findall(orig_text))
    comp_tokens = tok.findall(comp_text)
    if not comp_tokens:
        return 100.0
    return round(sum(1 for t in comp_tokens if t in orig_set) / len(comp_tokens) * 100, 1)


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--original", type=Path, required=True)
    p.add_argument("--compressed", type=Path, required=False)
    p.add_argument("--coverage", action="store_true", help="pre-check mode: estimate whitelist-density of --original before compressing; prints {coverage_pct, recommendation} and always exits 0")
    p.add_argument("--json", action="store_true")
    p.add_argument("--log", action="store_true", help="append a JSONL record to --log-file (pass or fail)")
    p.add_argument("--log-file", type=Path, default=Path("./compress-log.jsonl"), help="log path (default: ./compress-log.jsonl, relative to cwd)")
    p.add_argument("--level", default=None, help="compression level used (lite|full|ultra) — recorded when --log is set")
    p.add_argument("--context", default=None, help="what kind of output this was (e.g. scratchpad|subagent-prompt|report|memory) — recorded when --log is set")
    a = p.parse_args()
    if a.coverage:
        if not a.original.is_file():
            print(f"File not found: {a.original}", file=sys.stderr)
            return 2
        try:
            text = a.original.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError) as e:
            print(f"USAGE ERROR — cannot read input as UTF-8 text: {e}", file=sys.stderr)
            return 2
        pct = coverage_pct(text)
        rec = "skip" if pct >= 40 else "lite" if pct >= 20 else "tier-cap"
        print(json.dumps({"coverage_pct": pct, "recommendation": rec}, ensure_ascii=False))
        return 0
    if a.compressed is None:
        print("USAGE ERROR — --compressed is required unless --coverage is set", file=sys.stderr)
        return 2
    for f in (a.original, a.compressed):
        if not f.is_file():
            print(f"File not found: {f}", file=sys.stderr)
            return 2
    try:
        orig_text = a.original.read_text(encoding="utf-8")
        comp_text = a.compressed.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError) as e:
        # card5 (2026-07-12): binary/unreadable input used to crash with exit 1 —
        # indistinguishable from a real fidelity FAIL and never logged. Usage error = exit 2.
        print(f"USAGE ERROR — cannot read input as UTF-8 text: {e}", file=sys.stderr)
        return 2
    missing = diff(extract(orig_text), extract(comp_text))
    if a.log:
        import datetime
        ob, cb = len(orig_text.encode("utf-8")), len(comp_text.encode("utf-8"))
        entry = {
            "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
            "level": a.level, "context": a.context,
            "orig_bytes": ob, "comp_bytes": cb,
            "saving_pct": round((1 - cb / ob) * 100, 1) if ob else 0.0,
            "tokens_est_orig": _est_tokens(orig_text), "tokens_est_comp": _est_tokens(comp_text),
            "grounded_pct": _grounded_pct(orig_text, comp_text),
            "pass": not missing, "missing_keys": sorted(missing.keys()),
        }
        try:
            a.log_file.parent.mkdir(parents=True, exist_ok=True)
            with a.log_file.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except OSError as e:
            print(f"USAGE ERROR — cannot write log file: {e}", file=sys.stderr)
            return 2
    if a.json:
        print(json.dumps({"pass": not missing, "missing": missing}, ensure_ascii=False, indent=2))
    elif missing:
        print("FIDELITY FAIL — the following whitelisted elements were lost:")
        for key, vals in missing.items():
            print(f"  [{key}] {vals}")
    else:
        print("FIDELITY PASS — all whitelisted elements preserved")
    return 1 if missing else 0


if __name__ == "__main__":
    sys.exit(main())
