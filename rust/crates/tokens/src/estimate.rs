//! Request-size heuristic ported from `shared/quota/credits.py::estimate_io_tokens`.
//!
//! ~4 characters per input token, output twice the input. Python measures non-text values by
//! `len(str(value))`, i.e. the Python `repr`, so a small `repr`-length emulator lives here to
//! keep the estimate byte-identical for tool calls and structured blocks.

use serde_json::Value;

/// Message keys whose payload is measured separately (content) or ignored (role/name).
const SKIP_FIELDS: [&str; 3] = ["content", "role", "name"];

/// Characters Python `str.isprintable()` rejects that appear in practice: C1 controls plus
/// the common Zs/Zl/Zp/Cf/Co code points. Unassigned (Cn) code points are not modelled.
fn py_nonprintable(c: char) -> bool {
    let u = c as u32;
    c.is_control()
        || matches!(
            u,
            0xA0 | 0xAD
                | 0x600..=0x605
                | 0x61C
                | 0x6DD
                | 0x70F
                | 0x1680
                | 0x180E
                | 0x2000..=0x200F
                | 0x2028..=0x202F
                | 0x205F..=0x2064
                | 0x2066..=0x206F
                | 0x3000
                | 0xFEFF
                | 0xFFF9..=0xFFFB
                | 0xE000..=0xF8FF
                | 0xF0000..=0x10FFFF
        )
}

/// Length in characters of Python's `repr(str)`, including quotes and escapes.
fn py_str_repr_len(s: &str) -> usize {
    let quote = if s.contains('\'') && !s.contains('"') {
        '"'
    } else {
        '\''
    };
    2 + s
        .chars()
        .map(|c| match c {
            '\\' | '\n' | '\r' | '\t' => 2,
            c if c == quote => 2,
            c if (c as u32) < 0x20 || c as u32 == 0x7f => 4,
            c if (c as u32) < 0x80 => 1,
            c if py_nonprintable(c) => match c as u32 {
                0..=0xff => 4,
                0x100..=0xffff => 6,
                _ => 10,
            },
            _ => 1,
        })
        .sum::<usize>()
}

/// Length of Python's `repr(float)` (exponent form is `e+16` / `e-05`, two digit minimum).
fn py_float_repr_len(f: f64) -> usize {
    let s = format!("{f:?}");
    match s.split_once('e') {
        Some((mantissa, exp)) => {
            let digits = exp.strip_prefix('-').unwrap_or(exp);
            // mantissa + 'e' + explicit sign + at least two exponent digits
            mantissa.len() + 2 + digits.len().max(2)
        }
        None => s.len(),
    }
}

/// Length of Python's `repr(value)` for a JSON-decoded value (None/True/False, dict/list).
fn py_repr_len(v: &Value) -> usize {
    match v {
        Value::Null => 4,
        Value::Bool(true) => 4,
        Value::Bool(false) => 5,
        Value::Number(n) => match (n.as_i64(), n.as_u64()) {
            (Some(i), _) => i.to_string().len(),
            (None, Some(u)) => u.to_string().len(),
            _ => py_float_repr_len(n.as_f64().unwrap_or(0.0)),
        },
        Value::String(s) => py_str_repr_len(s),
        Value::Array(a) => {
            2 + a.iter().map(py_repr_len).sum::<usize>() + 2 * a.len().saturating_sub(1)
        }
        Value::Object(m) => {
            2 + m
                .iter()
                .map(|(k, v)| py_str_repr_len(k) + 2 + py_repr_len(v))
                .sum::<usize>()
                + 2 * m.len().saturating_sub(1)
        }
    }
}

/// `len(str(value))` for a non-None top-level value: strings count raw characters,
/// everything else counts its Python `repr`.
fn size(v: &Value) -> usize {
    match v {
        Value::String(s) => s.chars().count(),
        other => py_repr_len(other),
    }
}

/// Character length of message `content`: text exactly, non-text blocks by serialized size.
fn text_len(content: Option<&Value>) -> usize {
    match content {
        None | Some(Value::Null) => 0,
        Some(Value::String(s)) => s.chars().count(),
        Some(Value::Array(blocks)) => blocks
            .iter()
            .map(|b| match b {
                Value::String(s) => s.chars().count(),
                Value::Object(m)
                    if m.get("type").and_then(Value::as_str) == Some("text")
                        && m.get("text").is_some_and(Value::is_string) =>
                {
                    m.get("text")
                        .and_then(Value::as_str)
                        .map_or(0, |t| t.chars().count())
                }
                Value::Null => 0,
                other => size(other),
            })
            .sum(),
        Some(other) => size(other),
    }
}

/// Characters in one message: its content plus every payload-bearing field (tool_calls, ...).
fn message_len(message: &Value) -> usize {
    match message {
        Value::Object(m) => {
            text_len(m.get("content"))
                + m.iter()
                    .filter(|(k, v)| !SKIP_FIELDS.contains(&k.as_str()) && !v.is_null())
                    .map(|(_, v)| size(v))
                    .sum::<usize>()
        }
        Value::Null => 0,
        other => size(other),
    }
}

/// Returns `(estimated_input_tokens, estimated_output_tokens)` for untrusted `messages`:
/// input is `sum(chars // 4)` floored at 1 so an empty prompt never reserves zero, output is
/// twice the input. Total over any JSON shape; never panics.
pub fn estimate_io_tokens(messages: &[Value]) -> (u64, u64) {
    let sum: usize = messages.iter().map(|m| message_len(m) / 4).sum();
    let input = if sum == 0 { 1 } else { sum as u64 };
    (input, input * 2)
}
