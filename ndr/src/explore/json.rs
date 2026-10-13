//! JSON the way Python's `json` module sees it.
//!
//! Three things serde_json does differently, each of which `nd x` relies on:
//! object keys keep their order (a dict's order is what tables print in),
//! `NaN` / `Infinity` are accepted (the recorders write them), and a value
//! prints as Python's `str()` of what `json.loads` returned (`[3, 4]`,
//! `{'a': 1}`, `True`, `None`).

use super::pyfmt::{repr_float, repr_str};
use std::fmt::Write;

#[derive(Clone, Debug, PartialEq)]
pub enum Json {
    Null,
    Bool(bool),
    Int(i64),
    /// An int too big for i64, kept as its digits.
    Big(String),
    Float(f64),
    Str(String),
    Arr(Vec<Json>),
    Obj(Vec<(String, Json)>),
}

impl Json {
    pub fn get(&self, key: &str) -> Option<&Json> {
        match self {
            // A repeated key: Python keeps the last value.
            Json::Obj(kv) => kv.iter().rev().find(|(k, _)| k == key).map(|(_, v)| v),
            _ => None,
        }
    }
    pub fn as_i64(&self) -> Option<i64> {
        match self {
            Json::Int(i) => Some(*i),
            Json::Bool(b) => Some(*b as i64),
            _ => None,
        }
    }
    pub fn as_f64(&self) -> Option<f64> {
        match self {
            Json::Int(i) => Some(*i as f64),
            Json::Float(f) => Some(*f),
            Json::Bool(b) => Some(*b as i64 as f64),
            _ => None,
        }
    }
    pub fn as_str(&self) -> Option<&str> {
        match self {
            Json::Str(s) => Some(s),
            _ => None,
        }
    }
    pub fn items(&self) -> &[(String, Json)] {
        match self {
            Json::Obj(kv) => kv,
            _ => &[],
        }
    }
    pub fn arr(&self) -> &[Json] {
        match self {
            Json::Arr(v) => v,
            _ => &[],
        }
    }

    /// `str(value)`.
    pub fn py_str(&self) -> String {
        match self {
            Json::Str(s) => s.clone(),
            other => other.py_repr(),
        }
    }

    /// `repr(value)`.
    pub fn py_repr(&self) -> String {
        let mut s = String::new();
        self.repr_into(&mut s);
        s
    }

    fn repr_into(&self, out: &mut String) {
        match self {
            Json::Null => out.push_str("None"),
            Json::Bool(b) => out.push_str(if *b { "True" } else { "False" }),
            Json::Int(i) => {
                let _ = write!(out, "{i}");
            }
            Json::Big(d) => out.push_str(d),
            Json::Float(f) => out.push_str(&repr_float(*f)),
            Json::Str(s) => out.push_str(&repr_str(s)),
            Json::Arr(v) => {
                out.push('[');
                for (i, x) in v.iter().enumerate() {
                    if i > 0 {
                        out.push_str(", ");
                    }
                    x.repr_into(out);
                }
                out.push(']');
            }
            Json::Obj(kv) => {
                out.push('{');
                let mut first = true;
                for (i, (k, v)) in kv.iter().enumerate() {
                    // A repeated key keeps its first position, last value.
                    if kv[..i].iter().any(|(k2, _)| k2 == k) {
                        continue;
                    }
                    if !first {
                        out.push_str(", ");
                    }
                    first = false;
                    out.push_str(&repr_str(k));
                    out.push_str(": ");
                    self.get(k).unwrap_or(v).repr_into(out);
                }
                out.push('}');
            }
        }
    }

    /// Compact JSON, as `json.dump` writes it (with `NaN` for nan).
    pub fn dump(&self, out: &mut String) {
        match self {
            Json::Null => out.push_str("null"),
            Json::Bool(b) => out.push_str(if *b { "true" } else { "false" }),
            Json::Int(i) => {
                let _ = write!(out, "{i}");
            }
            Json::Big(d) => out.push_str(d),
            Json::Float(f) => {
                if f.is_nan() {
                    out.push_str("NaN")
                } else if f.is_infinite() {
                    out.push_str(if *f > 0.0 { "Infinity" } else { "-Infinity" })
                } else {
                    out.push_str(&repr_float(*f))
                }
            }
            Json::Str(s) => dump_str(s, out),
            Json::Arr(v) => {
                out.push('[');
                for (i, x) in v.iter().enumerate() {
                    if i > 0 {
                        out.push(',');
                    }
                    x.dump(out);
                }
                out.push(']');
            }
            Json::Obj(kv) => {
                out.push('{');
                for (i, (k, v)) in kv.iter().enumerate() {
                    if i > 0 {
                        out.push(',');
                    }
                    dump_str(k, out);
                    out.push(':');
                    v.dump(out);
                }
                out.push('}');
            }
        }
    }
}

pub fn dump_str(s: &str, out: &mut String) {
    out.push('"');
    for c in s.chars() {
        match c {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            c if (c as u32) < 0x20 => {
                let _ = write!(out, "\\u{:04x}", c as u32);
            }
            c => out.push(c),
        }
    }
    out.push('"');
}

/// `json.loads`. None on anything malformed.
pub fn parse(text: &str) -> Option<Json> {
    let mut p = P { b: text.as_bytes(), i: 0 };
    let v = p.value()?;
    p.ws();
    if p.i == p.b.len() { Some(v) } else { None }
}

struct P<'a> {
    b: &'a [u8],
    i: usize,
}

impl P<'_> {
    fn ws(&mut self) {
        while self.i < self.b.len() && matches!(self.b[self.i], b' ' | b'\t' | b'\n' | b'\r') {
            self.i += 1;
        }
    }
    fn lit(&mut self, s: &str) -> bool {
        if self.b[self.i..].starts_with(s.as_bytes()) {
            self.i += s.len();
            true
        } else {
            false
        }
    }
    fn value(&mut self) -> Option<Json> {
        self.ws();
        match *self.b.get(self.i)? {
            b'{' => {
                self.i += 1;
                let mut kv = Vec::new();
                self.ws();
                if self.b.get(self.i) == Some(&b'}') {
                    self.i += 1;
                    return Some(Json::Obj(kv));
                }
                loop {
                    self.ws();
                    let k = self.string()?;
                    self.ws();
                    if self.b.get(self.i) != Some(&b':') {
                        return None;
                    }
                    self.i += 1;
                    let v = self.value()?;
                    kv.push((k, v));
                    self.ws();
                    match self.b.get(self.i)? {
                        b',' => self.i += 1,
                        b'}' => {
                            self.i += 1;
                            return Some(Json::Obj(kv));
                        }
                        _ => return None,
                    }
                }
            }
            b'[' => {
                self.i += 1;
                let mut v = Vec::new();
                self.ws();
                if self.b.get(self.i) == Some(&b']') {
                    self.i += 1;
                    return Some(Json::Arr(v));
                }
                loop {
                    v.push(self.value()?);
                    self.ws();
                    match self.b.get(self.i)? {
                        b',' => self.i += 1,
                        b']' => {
                            self.i += 1;
                            return Some(Json::Arr(v));
                        }
                        _ => return None,
                    }
                }
            }
            b'"' => self.string().map(Json::Str),
            b't' if self.lit("true") => Some(Json::Bool(true)),
            b'f' if self.lit("false") => Some(Json::Bool(false)),
            b'n' if self.lit("null") => Some(Json::Null),
            b'N' if self.lit("NaN") => Some(Json::Float(f64::NAN)),
            b'I' if self.lit("Infinity") => Some(Json::Float(f64::INFINITY)),
            b'-' if self.lit("-Infinity") => Some(Json::Float(f64::NEG_INFINITY)),
            _ => self.number(),
        }
    }
    fn number(&mut self) -> Option<Json> {
        let start = self.i;
        if self.b.get(self.i) == Some(&b'-') {
            self.i += 1;
        }
        let mut float = false;
        while let Some(&c) = self.b.get(self.i) {
            match c {
                b'0'..=b'9' => {}
                b'.' | b'e' | b'E' | b'+' | b'-' => float = true,
                _ => break,
            }
            self.i += 1;
        }
        let s = std::str::from_utf8(&self.b[start..self.i]).ok()?;
        if s.is_empty() || s == "-" {
            return None;
        }
        if float {
            s.parse().ok().map(Json::Float)
        } else {
            match s.parse::<i64>() {
                Ok(i) => Some(Json::Int(i)),
                Err(_) => Some(Json::Big(s.trim_start_matches('0').to_string())),
            }
        }
    }
    fn string(&mut self) -> Option<String> {
        if self.b.get(self.i) != Some(&b'"') {
            return None;
        }
        self.i += 1;
        let mut out = String::new();
        loop {
            // Copy a run of plain bytes in one go: strings are most of a payload.
            let run = self.i;
            while self.i < self.b.len() && self.b[self.i] != b'"' && self.b[self.i] != b'\\' {
                self.i += 1;
            }
            out.push_str(std::str::from_utf8(&self.b[run..self.i]).ok()?);
            match *self.b.get(self.i)? {
                b'"' => {
                    self.i += 1;
                    return Some(out);
                }
                _ => {
                    self.i += 1;
                    let e = *self.b.get(self.i)?;
                    self.i += 1;
                    match e {
                        b'"' => out.push('"'),
                        b'\\' => out.push('\\'),
                        b'/' => out.push('/'),
                        b'b' => out.push('\x08'),
                        b'f' => out.push('\x0c'),
                        b'n' => out.push('\n'),
                        b'r' => out.push('\r'),
                        b't' => out.push('\t'),
                        b'u' => {
                            let hi = self.hex4()?;
                            if (0xd800..0xdc00).contains(&hi) && self.b[self.i..].starts_with(b"\\u") {
                                let save = self.i;
                                self.i += 2;
                                let lo = self.hex4()?;
                                if (0xdc00..0xe000).contains(&lo) {
                                    let c = 0x10000 + ((hi - 0xd800) << 10) + (lo - 0xdc00);
                                    out.push(char::from_u32(c).unwrap_or('\u{fffd}'));
                                    continue;
                                }
                                self.i = save;
                            }
                            out.push(char::from_u32(hi).unwrap_or('\u{fffd}'));
                        }
                        _ => return None,
                    }
                }
            }
        }
    }
    fn hex4(&mut self) -> Option<u32> {
        let s = std::str::from_utf8(self.b.get(self.i..self.i + 4)?).ok()?;
        self.i += 4;
        u32::from_str_radix(s, 16).ok()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn keeps_order_and_reads_nan() {
        let v = parse(r#"{"b": 1, "a": [1, 2.5, NaN, "x"], "c": {"t": true, "n": null}}"#).unwrap();
        let keys: Vec<&str> = v.items().iter().map(|(k, _)| k.as_str()).collect();
        assert_eq!(keys, ["b", "a", "c"]);
        assert_eq!(v.get("a").unwrap().py_str(), "[1, 2.5, nan, 'x']");
        assert_eq!(v.get("c").unwrap().py_str(), "{'t': True, 'n': None}");
        assert_eq!(parse("\"caf\\u00e9 \\ud83d\\ude00\"").unwrap().py_str(), "café 😀");
        assert_eq!(parse("123456789012345678901234567890").unwrap().py_str(), "123456789012345678901234567890");
    }

    #[test]
    fn dump_round_trips() {
        let v = parse(r#"{"k":[1,-2.5,"q\"\n"],"z":{}}"#).unwrap();
        let mut s = String::new();
        v.dump(&mut s);
        assert_eq!(parse(&s).unwrap(), v);
    }
}
