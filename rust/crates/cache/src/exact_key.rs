//! Exact response-cache key derivation, byte-identical to `shared/cache/keys.py`.
//!
//! Python hashes `orjson.dumps(payload, OPT_SORT_KEYS)`; this module reproduces orjson's
//! canonical encoding (sorted object keys, compact separators, raw UTF-8, orjson float
//! formatting) so a key derived here addresses the same Valkey entry the Python proxy writes.

use serde_json::{Map, Value};
use sha2::{Digest, Sha256};

/// Canonical inputs to the exact-cache key (`ExactKeyParts` in Python).
///
/// `top_p` and `max_tokens` are raw JSON values (not coerced) because Python hashes whatever
/// the client sent: `1` and `1.0` are different keys there and must be here.
#[derive(Debug, Clone, PartialEq)]
pub struct ExactKeyParts<'a> {
    /// Owning organization.
    pub org_id: i64,
    /// `"{model}::{response_format}"` — the format is part of the key so wire formats never collide.
    pub model_class: &'a str,
    /// Post-input-filter conversation messages.
    pub messages: &'a [Value],
    /// Request `tools` schema, when present.
    pub tools: Option<&'a Value>,
    /// Sampling temperature (Python coerces `float(temperature or 0.0)`).
    pub temperature: f64,
    /// Raw `top_p`, when present.
    pub top_p: Option<&'a Value>,
    /// Raw `max_tokens`, when present.
    pub max_tokens: Option<&'a Value>,
}

/// SHA-256 hex digest over the canonical request shape (`derive_exact_key` in Python).
pub fn derive_exact_key(parts: &ExactKeyParts<'_>) -> String {
    let digest = Sha256::digest(canonical_payload(parts));
    digest.iter().map(|b| format!("{b:02x}")).collect()
}

/// The exact bytes Python feeds to SHA-256 for these parts.
pub fn canonical_payload(parts: &ExactKeyParts<'_>) -> Vec<u8> {
    let mut m = Map::new();
    m.insert("org_id".into(), Value::from(parts.org_id));
    m.insert("model_class".into(), Value::from(parts.model_class));
    m.insert("messages".into(), Value::Array(parts.messages.to_vec()));
    m.insert("tools".into(), parts.tools.cloned().unwrap_or(Value::Null));
    m.insert("temperature".into(), float_value(parts.temperature));
    m.insert("top_p".into(), parts.top_p.cloned().unwrap_or(Value::Null));
    m.insert(
        "max_tokens".into(),
        parts.max_tokens.cloned().unwrap_or(Value::Null),
    );
    canonical_json(&Value::Object(m))
}

/// A float as a JSON number; non-finite values become `null` (orjson does the same).
fn float_value(f: f64) -> Value {
    serde_json::Number::from_f64(f).map_or(Value::Null, Value::Number)
}

/// Serializes like `orjson.dumps(value, option=OPT_SORT_KEYS)`.
pub fn canonical_json(value: &Value) -> Vec<u8> {
    let mut out = Vec::with_capacity(256);
    write_value(value, &mut out);
    out
}

fn write_value(value: &Value, out: &mut Vec<u8>) {
    match value {
        Value::Null => out.extend_from_slice(b"null"),
        Value::Bool(true) => out.extend_from_slice(b"true"),
        Value::Bool(false) => out.extend_from_slice(b"false"),
        Value::Number(n) => match n.as_f64().filter(|_| n.is_f64()) {
            Some(f) => write_float(f, out),
            None => out.extend_from_slice(n.to_string().as_bytes()),
        },
        Value::String(s) => write_string(s, out),
        Value::Array(items) => {
            out.push(b'[');
            for (i, item) in items.iter().enumerate() {
                if i > 0 {
                    out.push(b',');
                }
                write_value(item, out);
            }
            out.push(b']');
        }
        Value::Object(map) => {
            let mut entries: Vec<(&String, &Value)> = map.iter().collect();
            entries.sort_by(|a, b| a.0.as_bytes().cmp(b.0.as_bytes()));
            out.push(b'{');
            for (i, (k, v)) in entries.into_iter().enumerate() {
                if i > 0 {
                    out.push(b',');
                }
                write_string(k, out);
                out.push(b':');
                write_value(v, out);
            }
            out.push(b'}');
        }
    }
}

/// orjson string escaping: `"`, `\`, and control characters only; everything else raw UTF-8.
fn write_string(s: &str, out: &mut Vec<u8>) {
    out.push(b'"');
    for ch in s.chars() {
        match ch {
            '"' => out.extend_from_slice(b"\\\""),
            '\\' => out.extend_from_slice(b"\\\\"),
            '\n' => out.extend_from_slice(b"\\n"),
            '\r' => out.extend_from_slice(b"\\r"),
            '\t' => out.extend_from_slice(b"\\t"),
            '\u{8}' => out.extend_from_slice(b"\\b"),
            '\u{c}' => out.extend_from_slice(b"\\f"),
            c if (c as u32) < 0x20 => {
                out.extend_from_slice(format!("\\u{:04x}", c as u32).as_bytes())
            }
            c => {
                let mut buf = [0u8; 4];
                out.extend_from_slice(c.encode_utf8(&mut buf).as_bytes());
            }
        }
    }
    out.push(b'"');
}

/// orjson float formatting: shortest round-trip digits, fixed notation for decimal exponents in
/// `-5..16`, otherwise `d.ddde+X` / `d.ddde-X` (no zero padding). Always has a `.` or exponent.
fn write_float(f: f64, out: &mut Vec<u8>) {
    if !f.is_finite() {
        out.extend_from_slice(b"null");
        return;
    }
    // `{:e}` yields the shortest round-trip digits as `d[.ddd]e[-]X`.
    let sci = format!("{f:e}");
    let (mant, exp) = sci.split_once('e').unwrap_or((sci.as_str(), "0"));
    let exp: i32 = exp.parse().unwrap_or(0);
    let (neg, mant) = match mant.strip_prefix('-') {
        Some(m) => (true, m),
        None => (false, mant),
    };
    let digits: String = mant.chars().filter(|c| *c != '.').collect();
    if neg {
        out.push(b'-');
    }
    if (-5..16).contains(&exp) {
        if exp < 0 {
            out.extend_from_slice(b"0.");
            out.extend(std::iter::repeat_n(b'0', (-exp - 1) as usize));
            out.extend_from_slice(digits.as_bytes());
        } else {
            let int_len = exp as usize + 1;
            if digits.len() <= int_len {
                out.extend_from_slice(digits.as_bytes());
                out.extend(std::iter::repeat_n(b'0', int_len - digits.len()));
                out.extend_from_slice(b".0");
            } else {
                out.extend_from_slice(&digits.as_bytes()[..int_len]);
                out.push(b'.');
                out.extend_from_slice(&digits.as_bytes()[int_len..]);
            }
        }
    } else {
        out.extend_from_slice(&digits.as_bytes()[..1]);
        if digits.len() > 1 {
            out.push(b'.');
            out.extend_from_slice(&digits.as_bytes()[1..]);
        }
        out.push(b'e');
        if exp > 0 {
            out.push(b'+');
        }
        out.extend_from_slice(exp.to_string().as_bytes());
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn fmt(f: f64) -> String {
        let mut o = Vec::new();
        write_float(f, &mut o);
        String::from_utf8(o).unwrap()
    }

    /// Expected strings are real `orjson.dumps` output (orjson 3.13.0).
    #[test]
    fn float_format_matches_orjson() {
        let cases: &[(f64, &str)] = &[
            (0.0, "0.0"),
            (-0.0, "-0.0"),
            (1.0, "1.0"),
            (0.9, "0.9"),
            (0.1, "0.1"),
            (1e-5, "0.00001"),
            (-1e-5, "-0.00001"),
            (1.5e-5, "0.000015"),
            (1.234e-5, "0.00001234"),
            (1e-6, "1e-6"),
            (1.2e-7, "1.2e-7"),
            (1.5e-10, "1.5e-10"),
            (1e15, "1000000000000000.0"),
            (1.5e15, "1500000000000000.0"),
            (1234567890000000.0, "1234567890000000.0"),
            (1e16, "1e+16"),
            (1.5e16, "1.5e+16"),
            (1.2345e17, "1.2345e+17"),
            (1e22, "1e+22"),
            (123456789.123, "123456789.123"),
            (100000.0, "100000.0"),
            (1.2345678901234567e20, "1.2345678901234567e+20"),
            (5e-324, "5e-324"),
            (1.7976931348623157e308, "1.7976931348623157e+308"),
            (f64::NAN, "null"),
        ];
        for (f, want) in cases {
            assert_eq!(fmt(*f), *want, "float {f:e}");
        }
    }

    #[test]
    fn strings_escape_like_orjson() {
        let v = json!({"b": "é/\u{7f}\u{1}\n\"\\\t\r\u{8}\u{c}日", "a": [1, null, true]});
        assert_eq!(
            String::from_utf8(canonical_json(&v)).unwrap(),
            "{\"a\":[1,null,true],\"b\":\"é/\u{7f}\\u0001\\n\\\"\\\\\\t\\r\\b\\f日\"}"
        );
    }

    #[test]
    fn keys_sort_by_bytes_and_arrays_keep_order() {
        let v = json!({"z": 1, "a": {"y": [3, 1, 2], "b": 0}, "é": 2});
        assert_eq!(
            String::from_utf8(canonical_json(&v)).unwrap(),
            r#"{"a":{"b":0,"y":[3,1,2]},"z":1,"é":2}"#
        );
    }

    #[test]
    fn ints_stay_ints_and_floats_stay_floats() {
        let v: Value = serde_json::from_str(r#"[1, 1.0, -7, 2.50, 1e2]"#).unwrap();
        assert_eq!(
            String::from_utf8(canonical_json(&v)).unwrap(),
            "[1,1.0,-7,2.5,100.0]"
        );
    }

    #[test]
    fn non_finite_temperature_serialises_null() {
        let p = ExactKeyParts {
            org_id: 1,
            model_class: "m::openai",
            messages: &[],
            tools: None,
            temperature: f64::NAN,
            top_p: None,
            max_tokens: None,
        };
        assert!(String::from_utf8(canonical_payload(&p))
            .unwrap()
            .contains("\"temperature\":null"));
    }
}
