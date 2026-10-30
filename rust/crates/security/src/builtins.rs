//! Exported BUILTIN_PATTERNS table (verbatim Python source patterns, dict order).
//!
//! Generated from `shared/security/content_filter.py` by
//! `scripts/gen-content-filter-corpus.py`; the parity test fails on drift.

/// Every builtin PII/PCI pattern as `(name, Python regex source, confidence)`.
pub const BUILTIN_PATTERNS: &[(&str, &str, f64)] = &[
    ("credit_card", r####"\b(?:\d{4}[-\s]?){3}\d{4}\b"####, 0.95),
    ("credit_card_amex", r####"\b3[47]\d{2}[\s\-]?\d{6}[\s\-]?\d{5}\b"####, 0.95),
    ("routing_number", r####"\b[0-3]\d{8}\b"####, 0.65),
    ("iban", r####"\b[A-Z]{2}[0-9]{2}(?:\s?[A-Z0-9]{4}){2,7}\b"####, 0.7),
    ("ssn", r####"\b\d{3}-\d{2}-\d{4}\b"####, 0.95),
    ("ssn_unformatted", r####"\b(?!000|666|9\d{2})\d{3}(?!00)\d{2}(?!0000)\d{4}\b"####, 0.6),
    ("passport_us", r####"\b[A-Z][0-9]{8}\b"####, 0.7),
    ("passport_generic", r####"\b(?:passport|travel\s+doc(?:ument)?)\s*[#:\s]*([A-Z0-9]{6,9})\b"####, 0.8),
    ("drivers_license_us", r####"\b(?:d(?:river)?(?:\'?s)?\s*lic(?:ense)?|d\.?l\.?)\s*[#:\s]*([A-Z0-9]{6,12})\b"####, 0.75),
    ("medicare_id_us", r####"\b[1-9][A-Z][A-Z0-9]\d[A-Z][A-Z0-9]\d[A-Z]{2}\d{2}\b"####, 0.75),
    ("national_id_uk", r####"\b[A-CEGHJ-PR-TW-Z]{2}\d{6}[A-D]\b"####, 0.9),
    ("email", r####"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b"####, 0.85),
    ("phone_us", r####"\b(?:\+?1[-.\s]?)?\(?(?:[\d]{3})\)?[-.\s]?(?:[\d]{3})[-.\s]?(?:[\d]{4})\b"####, 0.8),
    ("phone_international", r####"\+(?:[0-9][\s\-]?){6,14}[0-9]"####, 0.8),
    ("date_of_birth", r####"\b(?:dob|date\s+of\s+birth|birth\s*(?:date|day))\s*[:\-]?\s*(?:\d{1,2}[/\-.]\d{1,2}[/\-.]\d{2,4}|\d{4}[/\-.]\d{1,2}[/\-.]\d{1,2})\b"####, 0.85),
    ("ip_address_private", r####"\b(?:10\.\d{1,3}\.\d{1,3}\.\d{1,3}|172\.(?:1[6-9]|2[0-9]|3[01])\.\d{1,3}\.\d{1,3}|192\.168\.\d{1,3}\.\d{1,3})\b"####, 0.9),
    ("ip_address_public", r####"\b(?!(?:10|127)\.\d{1,3}\.\d{1,3}\.\d{1,3}\b)(?!172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3}\b)(?!192\.168\.\d{1,3}\.\d{1,3}\b)(?:(?:25[0-5]|2[0-4]\d|[01]?\d\d?)\.){3}(?:25[0-5]|2[0-4]\d|[01]?\d\d?)\b"####, 0.75),
    ("api_key_openai", r####"\bsk-[a-zA-Z0-9]{20,}\b"####, 0.98),
    ("api_key_anthropic", r####"\bsk-ant-[a-zA-Z0-9\-]{20,}\b"####, 0.98),
    ("api_key_github", r####"\b(?:ghp_[a-zA-Z0-9]{36}|github_pat_[a-zA-Z0-9]{59})\b"####, 0.98),
    ("api_key_aws", r####"\bAKIA[0-9A-Z]{16}\b"####, 0.95),
    ("api_key_generic", r####"(?:api[_-]?key|apikey|token)\s*[:=]\s*['\"]?[A-Za-z0-9\-_]{20,}['\"]?"####, 0.75),
    ("password_in_text", r####"(?:password|passwd|pwd)\s*[:=]\s*['\"]?[^\s'\",]{6,}['\"]?"####, 0.7),
];
