#!/usr/bin/env python3
"""Generate the Rust `security` crate ContentFilter parity corpus from the REAL Python filter.

Inputs are hand-picked texts; every expected value (action, violations, redacted text) is
produced by `shared.security.content_filter.ContentFilter` -- never hand-written. Custom rules
and per-org toggles are injected by overriding the two DB loaders on the instance, so all
matching/redaction/decision logic is the unmodified Python code.
  usage: gen-content-filter-corpus.py [--check]   (--check exits 1 on drift)
Needs the repo venv (`make venv`); run from anywhere.
"""

import asyncio
import json
import logging
import shutil

# Runs rustfmt with a fixed argv; no untrusted input (dev-only generator).
import subprocess  # nosec B404
import sys
from pathlib import Path

root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root))

from shared.security.content_filter import ContentFilter, FilterRule  # noqa: E402

OUT = root / "rust/crates/security/tests/fixtures/content_filter_corpus.json"
BUILTINS_RS = root / "rust/crates/security/src/builtins.rs"
logging.disable(logging.CRITICAL)

# One or more positive texts per builtin; extras probe overlaps, case, unicode, truncation.
BUILTIN_TEXTS = {
    "credit_card": ["card 4111 1111 1111 1111 ok", "4111-1111-1111-1111 and 5500000000000004"],
    "credit_card_amex": ["amex 3782 822463 10005 end", "3714-496353-98431"],
    "routing_number": ["routing 021000021 please", "012345678 and 399999999"],
    "iban": ["iban GB82 WEST 1234 5698 7654 32 done", "DE89370400440532013000"],
    "ssn": ["ssn 123-45-6789 here", "123-45-6789 and 987-65-4321 twice 123-45-6789"],
    "ssn_unformatted": ["ssn 123456789 raw", "078051120 and 000123456 and 666123456"],
    "passport_us": ["passport A12345678 x", "a12345678 lower"],
    "passport_generic": ["Passport: X1234567 valid", "travel document #AB123456"],
    "drivers_license_us": ["driver's license: D1234567890 ok", "DL # AB1234567 and dl: XY99887766"],
    "medicare_id_us": ["mbi 1EG4TE5MK73 ok", "2AB3CD4EF56"],
    "national_id_uk": ["ni QQ123456C ok", "AB123456D"],
    "email": ["mail john.doe+x@example.co.uk now", "A@B.COM and c@d.org"],
    "phone_us": ["call (555) 123-4567 now", "+1 555-123-4567 or 555.123.4567"],
    "phone_international": ["intl +44 20 7946 0958 ok", "+49-30-1234567"],
    "date_of_birth": ["DOB: 01/02/1990 noted", "date of birth 1990-02-01"],
    "ip_address_private": ["host 10.0.0.5 up", "192.168.1.1 and 172.16.5.4"],
    "ip_address_public": ["server 8.8.8.8 up", "1.2.3.4 and 203.0.113.9"],
    "api_key_openai": ["key sk-abcdefghijklmnopqrstuvwxyz0123 end"],
    "api_key_anthropic": ["key sk-ant-api03-abcdefghijklmnopqrstuvwxyz end"],
    "api_key_github": ["ghp_" + "a" * 36, "tok github_pat_" + "B" * 59],
    "api_key_aws": ["aws AKIAIOSFODNN7EXAMPLE end"],
    "api_key_generic": ["api_key = '" + "x" * 24 + "'", "token: " + "x" * 150],
    "password_in_text": ["password: hunter22 ok", "pwd=secretpass, next passwd = 'abcdef1'"],
}
EXTRA_TEXTS = [
    "",
    "nothing sensitive in this sentence at all",
    "UPPER EMAIL USER@EXAMPLE.COM and lower user@example.com",
    "unicode café résumé 日本語 a@b.com",
    "multi\nline\nssn 123-45-6789\nmail x@y.io",
    "overlap 4111111111111111 digits",
    "version 1.2.3.4.5 and 12345 short",
    "the [REDACTED:EMAIL] marker plus a@b.com",
    "x" * 300 + " a@b.com",
]

CUSTOM_CASES = [
    {
        "name": "string_redact",
        "rules": [("proj-x", "custom_string", "both", "Project Falcon", "redact")],
        "texts": ["about project falcon launch", "PROJECT FALCON and Project Falcon"],
    },
    {
        "name": "string_block",
        "rules": [("no-secret", "custom_string", "input", "top secret", "block")],
        "texts": ["this is TOP SECRET stuff a@b.com"],
    },
    {
        "name": "regex_redact",
        "rules": [("emp-id", "custom_regex", "both", r"EMP-\d{4}", "redact")],
        "texts": ["emp-1234 and EMP-9999 here"],
    },
    {
        "name": "regex_block_overrides_builtin",
        "rules": [("deny-ssn", "custom_regex", "both", r"\d{3}-\d{2}-\d{4}", "block")],
        "texts": ["ssn 123-45-6789"],
    },
    {
        "name": "regex_log_only",
        "rules": [("watch", "custom_string", "both", "watchword", "log")],
        "texts": ["a watchword appears"],
    },
    {
        "name": "target_output_only",
        "rules": [("out-only", "custom_string", "output", "leakme", "redact")],
        "texts": ["please leakme"],
    },
    {
        "name": "invalid_regex_skipped",
        "rules": [("bad", "custom_regex", "both", "(unclosed", "redact")],
        "texts": ["anything (unclosed here"],
    },
    {
        "name": "lookahead_regex",
        "rules": [("la", "custom_regex", "both", r"foo(?=bar)", "redact")],
        "texts": ["foobar foobaz"],
    },
]
TOGGLE_CASES = [
    {"name": "disable_email", "disabled": ["email"], "text": "a@b.com ssn 123-45-6789"},
    {
        "name": "disable_many",
        "disabled": ["email", "ssn", "phone_us"],
        "text": "a@b.com 123-45-6789 555-123-4567",
    },
    {"name": "disable_unknown", "disabled": ["nope"], "text": "a@b.com"},
]


def _filter(rules, disabled):
    """Build a ContentFilter whose two DB loaders return the injected rules/toggles."""
    cf = ContentFilter(None)
    cf.ner_filter = None

    async def load_rules(_org):
        return [
            FilterRule(i, n, "", rt, tg, pat, act, "[REDACTED]", True, None)
            for i, (n, rt, tg, pat, act) in enumerate(rules, 1)
        ]

    async def load_disabled(_org):
        return set(disabled)

    cf._load_custom_rules = load_rules
    cf._load_disabled_builtins = load_disabled
    cf._log_filter_event = lambda **_k: None
    return cf


def _run(cf, phase, text):
    fn = cf.filter_input if phase == "input" else cf.filter_output
    r = asyncio.run(fn(text, 1, 1, "127.0.0.1"))
    return {
        "action": r.action,
        "allowed": r.allowed,
        "filtered_text": r.filtered_text,
        "violations": [
            {
                "rule_name": v.rule_name,
                "rule_type": v.rule_type,
                "action": v.action,
                "confidence": v.confidence,
            }
            for v in r.violations
        ],
    }


def build():
    """Return the corpus dict; fails if any builtin pattern is not exercised."""
    cases = []
    base = _filter([], [])
    texts = [t for ts in BUILTIN_TEXTS.values() for t in ts] + EXTRA_TEXTS
    for t in texts:
        for phase in ("input", "output"):
            cases.append(
                {
                    "kind": "builtin",
                    "phase": phase,
                    "text": t,
                    "rules": [],
                    "disabled": [],
                    "expected": _run(base, phase, t),
                }
            )
    seen = {v["rule_name"] for c in cases for v in c["expected"]["violations"]}
    missing = set(ContentFilter.BUILTIN_PATTERNS) - seen
    if missing:
        sys.exit(f"corpus does not exercise builtins: {sorted(missing)}")
    for cc in CUSTOM_CASES:
        cf = _filter(cc["rules"], [])
        for t in cc["texts"]:
            for phase in ("input", "output"):
                cases.append(
                    {
                        "kind": "custom:" + cc["name"],
                        "phase": phase,
                        "text": t,
                        "rules": [list(r) for r in cc["rules"]],
                        "disabled": [],
                        "expected": _run(cf, phase, t),
                    }
                )
    for tc in TOGGLE_CASES:
        cf = _filter([], tc["disabled"])
        cases.append(
            {
                "kind": "toggle:" + tc["name"],
                "phase": "input",
                "text": tc["text"],
                "rules": [],
                "disabled": tc["disabled"],
                "expected": _run(cf, "input", tc["text"]),
            }
        )
    builtins = [
        {"name": n, "pattern": c["pattern"], "confidence": c["confidence"]}
        for n, c in ContentFilter.BUILTIN_PATTERNS.items()
    ]
    return {"builtins": builtins, "cases": cases}


def builtins_rs(corpus):
    """Render the verbatim BUILTIN_PATTERNS table as Rust source."""
    out = [
        "//! Exported BUILTIN_PATTERNS table (verbatim Python source patterns, dict order).",
        "//!",
        "//! Generated from `shared/security/content_filter.py` by",
        "//! `scripts/gen-content-filter-corpus.py`; the parity test fails on drift.",
        "",
        "/// Every builtin PII/PCI pattern as `(name, Python regex source, confidence)`.",
        "pub const BUILTIN_PATTERNS: &[(&str, &str, f64)] = &[",
    ]
    for b in corpus["builtins"]:
        if '"####' in b["pattern"]:
            sys.exit(f"pattern {b['name']} cannot be rendered as a Rust raw string")
        out.append(f'    ("{b["name"]}", r####"{b["pattern"]}"####, {b["confidence"]!r}),')
    out.append("];")
    src = "\n".join(out) + "\n"
    # Match `cargo fmt` so the committed file is stable and --check is meaningful.
    # Fixed argv (rustfmt), generated source on stdin; no untrusted input.
    return subprocess.run(  # noqa: S603  # nosec B603
        [shutil.which("rustfmt") or "rustfmt", "--edition", "2021", "--emit", "stdout"],
        input=src,
        capture_output=True,
        text=True,
        check=True,
    ).stdout


def main():
    """Write (or with --check, verify) the corpus file."""
    corpus = build()
    text = json.dumps(corpus, indent=1, ensure_ascii=False) + "\n"
    rs = builtins_rs(corpus)
    if "--check" in sys.argv:
        if not OUT.exists() or OUT.read_text() != text or BUILTINS_RS.read_text() != rs:
            sys.exit(f"drift: {OUT} / {BUILTINS_RS}")
        return
    OUT.write_text(text)
    BUILTINS_RS.write_text(rs)
    print(f"wrote {OUT}")


main()
