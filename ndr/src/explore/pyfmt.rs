//! The bits of Python's behaviour the explorer's output depends on.
//!
//! `nd x` prints with `%r`, `%.1f`, `f"{x:,}"`, `str.splitlines()` and
//! friends. Rust's nearest equivalents differ in small ways (`1e-7` vs
//! `1e-07`, quoting, what counts as a line break), and every one of those
//! would show up as a diff between the two explorers. So they live here,
//! each written to match CPython exactly.

/// A number as Python holds it after `json.loads` or a record read: the
/// int/float split matters for `sum()` and for `repr`.
#[derive(Clone, Copy, Debug, PartialEq)]
pub enum Num {
    I(i64),
    F(f64),
}

impl Num {
    pub fn f(self) -> f64 {
        match self {
            Num::I(i) => i as f64,
            Num::F(f) => f,
        }
    }
}

/// `f"{x:,}"`.
pub fn n(x: i64) -> String {
    let s = x.unsigned_abs().to_string();
    let mut out = String::with_capacity(s.len() + s.len() / 3 + 1);
    if x < 0 {
        out.push('-');
    }
    for (i, c) in s.chars().enumerate() {
        if i > 0 && (s.len() - i) % 3 == 0 {
            out.push(',');
        }
        out.push(c);
    }
    out
}

pub fn nu(x: u64) -> String {
    n(x.min(i64::MAX as u64) as i64)
}

/// `"%.Nf" % x`. Rust and CPython both round the exact binary value half to
/// even, so only the non-finite spellings need translating.
pub fn fixed(x: f64, prec: usize) -> String {
    if x.is_nan() {
        return "nan".into();
    }
    if x.is_infinite() {
        return if x > 0.0 { "inf".into() } else { "-inf".into() };
    }
    format!("{:.*}", prec, x)
}

/// `"%.Ne" % x`: Python writes at least two exponent digits and a sign.
pub fn sci(x: f64, prec: usize) -> String {
    if !x.is_finite() {
        return fixed(x, prec);
    }
    let s = format!("{:.*e}", prec, x);
    let (mant, exp) = s.split_once('e').unwrap_or((&s, "0"));
    let e: i32 = exp.parse().unwrap_or(0);
    format!("{mant}e{}{:02}", if e < 0 { '-' } else { '+' }, e.abs())
}

/// `repr(float)`: the shortest round-trip digits, laid out the way CPython's
/// `float_repr_style = 'short'` does (`1e-05`, `1e+16`, `0.0001`, `3.0`).
pub fn repr_float(x: f64) -> String {
    if x.is_nan() {
        return "nan".into();
    }
    if x.is_infinite() {
        return if x > 0.0 { "inf".into() } else { "-inf".into() };
    }
    if x == 0.0 {
        return if x.is_sign_negative() { "-0.0".into() } else { "0.0".into() };
    }
    let s = format!("{:e}", x.abs());
    let (mant, exp) = s.split_once('e').unwrap();
    let exp: i32 = exp.parse().unwrap();
    let digits: String = mant.chars().filter(|c| *c != '.').collect();
    let decpt = exp + 1; // digits[0] sits just left of the point when decpt == 1
    let mut out = String::new();
    if x < 0.0 {
        out.push('-');
    }
    if decpt <= -4 || decpt > 16 {
        out.push_str(&digits[..1]);
        if digits.len() > 1 {
            out.push('.');
            out.push_str(&digits[1..]);
        }
        out.push_str(&format!("e{}{:02}", if exp < 0 { '-' } else { '+' }, exp.abs()));
    } else if decpt <= 0 {
        out.push_str("0.");
        for _ in 0..(-decpt) {
            out.push('0');
        }
        out.push_str(&digits);
    } else {
        let d = decpt as usize;
        if digits.len() <= d {
            out.push_str(&digits);
            for _ in digits.len()..d {
                out.push('0');
            }
            out.push_str(".0");
        } else {
            out.push_str(&digits[..d]);
            out.push('.');
            out.push_str(&digits[d..]);
        }
    }
    out
}

/// CPython's `Py_UNICODE_ISPRINTABLE`, close enough: control, format,
/// separator (other than space), private-use and noncharacter code points
/// are escaped by `repr`.
fn printable(c: char) -> bool {
    let u = c as u32;
    if c.is_control() {
        return false;
    }
    !matches!(u,
        0xa0 | 0xad | 0x600..=0x605 | 0x61c | 0x6dd | 0x70f | 0x890..=0x891 | 0x8e2
        | 0x1680 | 0x180e | 0x2000..=0x200f | 0x2028..=0x202f | 0x205f..=0x2064
        | 0x2066..=0x206f | 0x3000 | 0xd800..=0xdfff | 0xe000..=0xf8ff | 0xfdd0..=0xfdef
        | 0xfeff | 0xfff9..=0xfffb | 0x110bd | 0x110cd | 0x13430..=0x1343f
        | 0x1bca0..=0x1bca3 | 0x1d173..=0x1d17a | 0xe0001 | 0xe0020..=0xe007f
        | 0xf0000..=0x10ffff)
        && (u & 0xfffe) != 0xfffe
}

/// `repr(str)`.
pub fn repr_str(s: &str) -> String {
    let quote = if s.contains('\'') && !s.contains('"') { '"' } else { '\'' };
    let mut out = String::with_capacity(s.len() + 2);
    out.push(quote);
    for c in s.chars() {
        let u = c as u32;
        if c == quote || c == '\\' {
            out.push('\\');
            out.push(c);
        } else if c == '\t' {
            out.push_str("\\t");
        } else if c == '\n' {
            out.push_str("\\n");
        } else if c == '\r' {
            out.push_str("\\r");
        } else if u < 0x20 || u == 0x7f {
            out.push_str(&format!("\\x{u:02x}"));
        } else if u < 0x7f || printable(c) {
            out.push(c);
        } else if u <= 0xff {
            out.push_str(&format!("\\x{u:02x}"));
        } else if u <= 0xffff {
            out.push_str(&format!("\\u{u:04x}"));
        } else {
            out.push_str(&format!("\\U{u:08x}"));
        }
    }
    out.push(quote);
    out
}

/// `str.isspace()` for one character: Rust's White_Space plus the four
/// information separators Python also counts.
pub fn is_space(c: char) -> bool {
    c.is_whitespace() || ('\x1c'..='\x1f').contains(&c)
}

/// `s.strip()`.
pub fn strip(s: &str) -> &str {
    s.trim_matches(is_space)
}

/// `s.split()`.
pub fn split(s: &str) -> Vec<&str> {
    s.split(is_space).filter(|p| !p.is_empty()).collect()
}

/// `s.splitlines()` -- which also breaks on \v, \f, \x1c-\x1e, \x85,
/// U+2028 and U+2029, not just \n.
pub fn splitlines(s: &str) -> Vec<&str> {
    let mut out = Vec::new();
    let mut start = 0;
    let b: Vec<(usize, char)> = s.char_indices().collect();
    let mut i = 0;
    while i < b.len() {
        let (pos, c) = b[i];
        let brk = matches!(c, '\n' | '\r' | '\x0b' | '\x0c' | '\x1c' | '\x1d' | '\x1e' | '\u{85}' | '\u{2028}' | '\u{2029}');
        if brk {
            out.push(&s[start..pos]);
            let mut next = pos + c.len_utf8();
            if c == '\r' && i + 1 < b.len() && b[i + 1].1 == '\n' {
                next += 1;
                i += 1;
            }
            start = next;
        }
        i += 1;
    }
    if start < s.len() {
        out.push(&s[start..]);
    }
    out
}

/// `str.isdigit()`, for the ASCII digits the explorer's syntax uses.
pub fn isdigit(s: &str) -> bool {
    !s.is_empty() && s.chars().all(|c| c.is_ascii_digit())
}

/// `len(s)`: code points, not bytes.
pub fn clen(s: &str) -> usize {
    s.chars().count()
}

/// `s[:k]`.
pub fn take(s: &str, k: usize) -> &str {
    match s.char_indices().nth(k) {
        Some((i, _)) => &s[..i],
        None => s,
    }
}

/// `"%-Ns" % s`.
pub fn ljust(s: &str, w: usize) -> String {
    let l = clen(s);
    if l >= w { s.to_string() } else { format!("{s}{}", " ".repeat(w - l)) }
}

/// `os.path.basename`.
pub fn basename(p: &str) -> &str {
    p.rsplit('/').next().unwrap_or(p)
}

/// `int(s)` for base 10, saturating where Python would just grow. The
/// error text is Python's, because `nd x` prints the exception message.
pub fn int(s: &str) -> Result<i64, String> {
    let err = || format!("invalid literal for int() with base 10: {}", repr_str(s));
    let t = strip(s);
    let (neg, body) = match t.as_bytes().first() {
        Some(b'-') => (true, &t[1..]),
        Some(b'+') => (false, &t[1..]),
        _ => (false, t),
    };
    if body.is_empty() || body.starts_with('_') || body.ends_with('_') || body.contains("__") {
        return Err(err());
    }
    let mut v: i128 = 0;
    for c in body.chars() {
        if c == '_' {
            continue;
        }
        let d = c.to_digit(10).ok_or_else(err)? as i128;
        v = (v * 10 + d).min(i64::MAX as i128 + 1);
    }
    let v = if neg { -v } else { v };
    Ok(v.clamp(i64::MIN as i128, i64::MAX as i128) as i64)
}

/// `float(s)`: Python accepts surrounding whitespace and underscores.
pub fn float(s: &str) -> Option<f64> {
    let t: String = strip(s).chars().filter(|c| *c != '_').collect();
    let low = t.to_ascii_lowercase();
    match low.trim_start_matches(['+', '-']) {
        "inf" | "infinity" | "nan" => {}
        _ if low.contains("inf") || low.contains("nan") => return None,
        _ => {}
    }
    t.parse::<f64>().ok()
}

/// `sum(xs)` exactly as CPython 3.12+ does it: an exact integer sum until
/// the first float, then Neumaier-compensated float addition, with ints
/// added after that WITHOUT compensation. Averages in the charts go
/// through this, so the bars land in the same cells.
pub fn sum(xs: impl IntoIterator<Item = Num>) -> Num {
    let mut it = xs.into_iter();
    let mut acc: i128 = 0;
    let mut first_float = None;
    for x in it.by_ref() {
        match x {
            Num::I(i) => acc += i as i128,
            Num::F(f) => {
                first_float = Some(f);
                break;
            }
        }
    }
    let Some(f0) = first_float else {
        return Num::I(acc.clamp(i64::MIN as i128, i64::MAX as i128) as i64);
    };
    let mut f = acc as f64 + f0;
    let mut c = 0.0f64;
    for x in it {
        match x {
            Num::F(x) => {
                let t = f + x;
                if f.abs() >= x.abs() {
                    c += (f - t) + x;
                } else {
                    c += (x - t) + f;
                }
                f = t;
            }
            Num::I(i) => f += i as f64,
        }
    }
    if c != 0.0 && c.is_finite() {
        f += c;
    }
    Num::F(f)
}

/// `int(round(x))`: banker's rounding.
pub fn round_int(x: f64) -> i64 {
    x.round_ties_even() as i64
}

/// Python's `a // b` and `a % b` for ints: floor, not truncation.
pub fn floordiv(a: i64, b: i64) -> i64 {
    a.div_euclid(b) - if b < 0 && a.rem_euclid(b) != 0 { 1 } else { 0 }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn float_repr_matches_python() {
        for (v, want) in [(1e-7, "1e-07"), (1e16, "1e+16"), (1e15, "1000000000000000.0"),
                          (0.0001, "0.0001"), (-0.0, "-0.0"), (2.5, "2.5"), (3.0, "3.0"),
                          (1.2345678901234568e17, "1.2345678901234568e+17"), (0.1, "0.1"),
                          (12345678.9, "12345678.9"), (-1.5e-10, "-1.5e-10"), (1e-4, "0.0001"),
                          (1e-5, "1e-05"), (f64::INFINITY, "inf"), (123.456, "123.456")] {
            assert_eq!(repr_float(v), want, "{v}");
        }
    }

    #[test]
    fn str_repr_matches_python() {
        assert_eq!(repr_str("abc"), "'abc'");
        assert_eq!(repr_str("it's"), "\"it's\"");
        assert_eq!(repr_str("both ' and \""), "'both \\' and \"'");
        assert_eq!(repr_str("tab\there\n"), "'tab\\there\\n'");
        assert_eq!(repr_str("é\u{7f}\u{a0}"), "'é\\x7f\\xa0'");
        assert_eq!(repr_str("\u{200b}"), "'\\u200b'");
    }

    #[test]
    fn number_formats() {
        assert_eq!(n(-1234567), "-1,234,567");
        assert_eq!(n(999), "999");
        assert_eq!(sci(0.000123, 1), "1.2e-04");
        assert_eq!(sci(1.5e300, 1), "1.5e+300");
        assert_eq!(fixed(0.25, 1), "0.2");
        assert_eq!(fixed(0.35, 1), "0.3");
        assert_eq!(fixed(1.005, 2), "1.00");
        assert_eq!(round_int(2.5), 2);
        assert_eq!(round_int(3.5), 4);
        assert_eq!(floordiv(-5, 2), -3);
        assert_eq!(floordiv(5, 2), 2);
    }

    #[test]
    fn int_parses_like_python() {
        assert_eq!(int(" 12 "), Ok(12));
        assert_eq!(int("+5"), Ok(5));
        assert_eq!(int("1_000"), Ok(1000));
        assert_eq!(int("abc"), Err("invalid literal for int() with base 10: 'abc'".into()));
        assert!(int("1__0").is_err());
        assert!(int("5.0").is_err());
    }

    #[test]
    fn sum_is_cpythons() {
        // ints after a float are not compensated; floats are.
        assert_eq!(sum([Num::F(0.1), Num::I(10i64.pow(16)), Num::I(-(10i64.pow(16)))]), Num::F(0.0));
        assert_eq!(sum([Num::F(1e16), Num::F(1.0), Num::F(-1e16)]), Num::F(1.0));
        assert_eq!(sum([Num::F(0.1); 10]), Num::F(1.0));
        assert_eq!(sum([Num::I(2), Num::I(3)]), Num::I(5));
    }

    #[test]
    fn splitlines_breaks_where_python_does() {
        assert_eq!(splitlines("a\nb\r\nc\x0cd\re"), vec!["a", "b", "c", "d", "e"]);
        assert_eq!(splitlines("a\n"), vec!["a"]);
        assert_eq!(splitlines(""), Vec::<&str>::new());
        assert_eq!(split("  a \t b\x1c"), vec!["a", "b"]);
    }
}
