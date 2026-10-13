//! `c i>5`: the condition, parsed and tested the way `nd x` does it.
//!
//! nd matches `NAME OP VALUE` with a regex, then `eval()`s VALUE with no
//! builtins (literals and arithmetic) and falls back to the text with its
//! quotes stripped. Comparisons follow Python: 1 == 1.0 == True, strings
//! order by code point, and a comparison Python would refuse (`'a' < 3`)
//! is simply false.

use super::pyfmt::{self, Num};
use super::store::Py;

#[derive(Clone, Debug, PartialEq)]
pub enum Lit {
    V(Py),
    /// A list, tuple, dict, bytes or complex: equal to nothing a variable
    /// holds here, and not orderable against it.
    Opaque,
}

#[derive(Clone, Copy, Debug, PartialEq)]
pub enum Op {
    Eq,
    Ne,
    Gt,
    Lt,
    Ge,
    Le,
}

/// The regex `^\s*([A-Za-z_][\w.]*)\s*(==|!=|>=|<=|=|>|<)\s*(.+)\s*$`.
pub fn split(text: &str) -> Option<(String, Op, String)> {
    let s = text.trim_start_matches(pyfmt::is_space);
    let mut chars = s.char_indices().peekable();
    let (_, c0) = *chars.peek()?;
    if !(c0.is_ascii_alphabetic() || c0 == '_') {
        return None;
    }
    let mut end = 0;
    for (i, c) in s.char_indices() {
        if c.is_alphanumeric() || c == '_' || c == '.' {
            end = i + c.len_utf8();
        } else {
            break;
        }
    }
    let name = &s[..end];
    let rest = s[end..].trim_start_matches(pyfmt::is_space);
    for (tok, op) in [("==", Op::Eq), ("!=", Op::Ne), (">=", Op::Ge), ("<=", Op::Le), ("=", Op::Eq), (">", Op::Gt), ("<", Op::Lt)] {
        if let Some(after) = rest.strip_prefix(tok) {
            // `\s*(.+)`: at least one character must follow the operator.
            if after.is_empty() || after.contains('\n') {
                continue;
            }
            let rhs = after.trim_start_matches(pyfmt::is_space);
            let rhs = if rhs.is_empty() { &after[after.len() - after.chars().last().map(|c| c.len_utf8()).unwrap_or(0)..] } else { rhs };
            return Some((name.to_string(), op, rhs.to_string()));
        }
    }
    None
}

/// `eval(rhs, {"__builtins__": {}}, {})`, or None where Python would raise.
pub fn eval(src: &str) -> Option<Lit> {
    let toks = lex(src)?;
    let mut p = Parser { t: toks, i: 0 };
    let v = p.expr()?;
    if p.i != p.t.len() {
        return None;
    }
    Some(v)
}

/// The target `c` compares with: the literal, else the text minus quotes.
pub fn target(rhs: &str) -> Lit {
    eval(rhs).unwrap_or_else(|| Lit::V(Py::Str(rhs.trim_matches(['\'', '"']).to_string())))
}

pub fn test(op: Op, got: &Py, target: &Lit) -> bool {
    let t = match target {
        Lit::Opaque => return op == Op::Ne,
        Lit::V(t) => t,
    };
    let ord = match (got.num(), t.num()) {
        (Some(Num::I(a)), Some(Num::I(b))) => a.partial_cmp(&b),
        (Some(a), Some(b)) => a.f().partial_cmp(&b.f()),
        _ => match (got, t) {
            (Py::Str(a), Py::Str(b)) => Some(a.cmp(b)),
            (Py::None, Py::None) => {
                return match op {
                    Op::Eq => true,
                    Op::Ne => false,
                    _ => false,
                }
            }
            _ => return op == Op::Ne,
        },
    };
    use std::cmp::Ordering::*;
    match (op, ord) {
        (Op::Eq, o) => o == Some(Equal),
        (Op::Ne, o) => o != Some(Equal),
        (Op::Gt, o) => o == Some(Greater),
        (Op::Lt, o) => o == Some(Less),
        (Op::Ge, o) => matches!(o, Some(Greater) | Some(Equal)),
        (Op::Le, o) => matches!(o, Some(Less) | Some(Equal)),
    }
}

// ---------------------------------------------------------------- the eval

#[derive(Clone, Debug, PartialEq)]
enum T {
    Num(Lit),
    Str(String),
    Bytes,
    Name(String),
    P(&'static str),
}

fn lex(src: &str) -> Option<Vec<T>> {
    let c: Vec<char> = src.chars().collect();
    let mut i = 0;
    let mut out = Vec::new();
    while i < c.len() {
        let ch = c[i];
        if ch == ' ' || ch == '\t' || ch == '\x0c' {
            i += 1;
            continue;
        }
        if ch == '\n' || ch == '#' {
            // eval() of one line: a comment ends it, a newline is an error.
            if ch == '#' {
                break;
            }
            return None;
        }
        if ch.is_ascii_digit() || (ch == '.' && c.get(i + 1).is_some_and(|d| d.is_ascii_digit())) {
            let start = i;
            while i < c.len() && (c[i].is_ascii_alphanumeric() || c[i] == '_' || c[i] == '.'
                || ((c[i] == '+' || c[i] == '-') && matches!(c[i - 1], 'e' | 'E') && !c[start..i].iter().any(|x| matches!(x, 'x' | 'X' | 'b' | 'B' | 'o' | 'O')))) {
                i += 1;
            }
            out.push(T::Num(number(&c[start..i].iter().collect::<String>())?));
            continue;
        }
        if ch.is_alphabetic() || ch == '_' {
            let start = i;
            while i < c.len() && (c[i].is_alphanumeric() || c[i] == '_') {
                i += 1;
            }
            let word: String = c[start..i].iter().collect();
            if i < c.len() && (c[i] == '\'' || c[i] == '"') && word.len() <= 2
                && word.chars().all(|x| matches!(x.to_ascii_lowercase(), 'r' | 'b' | 'u' | 'f')) {
                let low = word.to_ascii_lowercase();
                let (s, n) = string(&c[i..], low.contains('r'))?;
                i += n;
                if low.contains('f') && s.contains(['{', '}']) {
                    return None; // an f-string with fields needs names: NameError
                }
                out.push(if low.contains('b') { T::Bytes } else { T::Str(s) });
                continue;
            }
            out.push(T::Name(word));
            continue;
        }
        if ch == '\'' || ch == '"' {
            let (s, n) = string(&c[i..], false)?;
            i += n;
            out.push(T::Str(s));
            continue;
        }
        let two: String = c[i..(i + 2).min(c.len())].iter().collect();
        if let Some(p) = ["**", "//"].iter().find(|p| two == **p) {
            out.push(T::P(p));
            i += 2;
            continue;
        }
        let p = match ch {
            '+' => "+", '-' => "-", '*' => "*", '/' => "/", '%' => "%", '~' => "~",
            '(' => "(", ')' => ")", '[' => "[", ']' => "]", '{' => "{", '}' => "}", ',' => ",", ':' => ":",
            _ => return None,
        };
        out.push(T::P(p));
        i += 1;
    }
    Some(out)
}

fn number(s: &str) -> Option<Lit> {
    let low = s.to_ascii_lowercase();
    if low.ends_with('j') {
        return Some(Lit::Opaque);
    }
    let digits_ok = |d: &str| !d.is_empty() && !d.starts_with('_') && !d.ends_with('_') && !d.contains("__");
    for (pre, radix) in [("0x", 16), ("0o", 8), ("0b", 2)] {
        if let Some(rest) = low.strip_prefix(pre) {
            let rest = rest.strip_prefix('_').unwrap_or(rest);
            if !digits_ok(rest) {
                return None;
            }
            let clean: String = rest.chars().filter(|c| *c != '_').collect();
            return match i64::from_str_radix(&clean, radix) {
                Ok(v) => Some(Lit::V(Py::Int(v))),
                Err(_) => u128::from_str_radix(&clean, radix).ok().map(|v| Lit::V(Py::Float(v as f64))),
            };
        }
    }
    if low.contains(['.', 'e']) {
        let mant = low.split('e').next().unwrap_or("");
        if mant.split('.').any(|p| !p.is_empty() && !digits_ok(p)) {
            return None;
        }
        let clean: String = low.chars().filter(|c| *c != '_').collect();
        return clean.parse::<f64>().ok().map(|f| Lit::V(Py::Float(f)));
    }
    if !digits_ok(&low) || !low.chars().all(|c| c.is_ascii_digit() || c == '_') {
        return None;
    }
    let clean: String = low.chars().filter(|c| *c != '_').collect();
    if clean.len() > 1 && clean.starts_with('0') && clean.chars().any(|c| c != '0') {
        return None; // "leading zeros in decimal integer literals are not permitted"
    }
    Some(match clean.parse::<i64>() {
        Ok(v) => Lit::V(Py::Int(v)),
        Err(_) => Lit::V(Py::Float(clean.parse::<f64>().ok()?)),
    })
}

/// One quoted string (single or triple), returning its value and length.
fn string(c: &[char], raw: bool) -> Option<(String, usize)> {
    let q = c[0];
    let triple = c.len() >= 3 && c[1] == q && c[2] == q;
    let mut i = if triple { 3 } else { 1 };
    let mut out = String::new();
    loop {
        let ch = *c.get(i)?;
        if triple {
            if ch == q && c.get(i + 1) == Some(&q) && c.get(i + 2) == Some(&q) {
                return Some((out, i + 3));
            }
        } else if ch == q {
            return Some((out, i + 1));
        } else if ch == '\n' {
            return None;
        }
        if ch == '\\' {
            let e = *c.get(i + 1)?;
            i += 2;
            if raw {
                out.push('\\');
                out.push(e);
                continue;
            }
            match e {
                'n' => out.push('\n'),
                't' => out.push('\t'),
                'r' => out.push('\r'),
                '0' => out.push('\0'),
                'a' => out.push('\x07'),
                'b' => out.push('\x08'),
                'f' => out.push('\x0c'),
                'v' => out.push('\x0b'),
                '\\' | '\'' | '"' => out.push(e),
                '\n' => {}
                'x' => {
                    let h: String = c.get(i..i + 2)?.iter().collect();
                    out.push(char::from_u32(u32::from_str_radix(&h, 16).ok()?)?);
                    i += 2;
                }
                'u' | 'U' => {
                    let n = if e == 'u' { 4 } else { 8 };
                    let h: String = c.get(i..i + n)?.iter().collect();
                    out.push(char::from_u32(u32::from_str_radix(&h, 16).ok()?)?);
                    i += n;
                }
                other => {
                    out.push('\\');
                    out.push(other);
                }
            }
            continue;
        }
        out.push(ch);
        i += 1;
    }
}

struct Parser {
    t: Vec<T>,
    i: usize,
}

impl Parser {
    fn peek(&self) -> Option<&T> {
        self.t.get(self.i)
    }
    fn eat(&mut self, p: &str) -> bool {
        if self.peek() == Some(&T::P(match p {
            "+" => "+", "-" => "-", "*" => "*", "/" => "/", "//" => "//", "%" => "%", "**" => "**",
            "~" => "~", "(" => "(", ")" => ")", "[" => "[", "]" => "]", "{" => "{", "}" => "}",
            "," => ",", ":" => ":", _ => return false,
        })) {
            self.i += 1;
            true
        } else {
            false
        }
    }
    /// A tuple at the top level (`1, 2`) is still a value.
    fn expr(&mut self) -> Option<Lit> {
        let first = self.not_expr()?;
        if self.eat(",") {
            while self.peek().is_some() {
                self.not_expr()?;
                if !self.eat(",") {
                    break;
                }
            }
            return Some(Lit::Opaque);
        }
        Some(first)
    }
    fn not_expr(&mut self) -> Option<Lit> {
        if self.peek() == Some(&T::Name("not".into())) {
            self.i += 1;
            let v = self.not_expr()?;
            return Some(Lit::V(Py::Bool(!truthy(&v))));
        }
        self.arith()
    }
    fn arith(&mut self) -> Option<Lit> {
        let mut v = self.term()?;
        loop {
            if self.eat("+") {
                let r = self.term()?;
                v = binop("+", v, r)?;
            } else if self.eat("-") {
                let r = self.term()?;
                v = binop("-", v, r)?;
            } else {
                return Some(v);
            }
        }
    }
    fn term(&mut self) -> Option<Lit> {
        let mut v = self.unary()?;
        loop {
            let op = if self.eat("*") { "*" } else if self.eat("//") { "//" } else if self.eat("/") { "/" } else if self.eat("%") { "%" } else { return Some(v) };
            let r = self.unary()?;
            v = binop(op, v, r)?;
        }
    }
    fn unary(&mut self) -> Option<Lit> {
        if self.eat("-") {
            let v = self.unary()?;
            return binop("-", Lit::V(Py::Int(0)), v).filter(|_| true).and_then(|r| match r {
                Lit::V(Py::Str(_)) => None,
                x => Some(x),
            });
        }
        if self.eat("+") {
            let v = self.unary()?;
            return match v {
                Lit::V(Py::Bool(b)) => Some(Lit::V(Py::Int(b as i64))),
                Lit::V(Py::Int(_)) | Lit::V(Py::Float(_)) | Lit::Opaque => Some(v),
                _ => None,
            };
        }
        if self.eat("~") {
            let v = self.unary()?;
            return match v {
                Lit::V(Py::Int(i)) => Some(Lit::V(Py::Int(!i))),
                Lit::V(Py::Bool(b)) => Some(Lit::V(Py::Int(!(b as i64)))),
                _ => None,
            };
        }
        self.power()
    }
    fn power(&mut self) -> Option<Lit> {
        let base = self.atom()?;
        if self.eat("**") {
            let e = self.unary()?;
            return binop("**", base, e);
        }
        Some(base)
    }
    fn atom(&mut self) -> Option<Lit> {
        let t = self.peek()?.clone();
        self.i += 1;
        match t {
            T::Num(v) => Some(v),
            T::Str(mut s) => {
                // Adjacent literals concatenate: 'a' 'b'.
                while let Some(T::Str(more)) = self.peek().cloned() {
                    s.push_str(&more);
                    self.i += 1;
                }
                Some(Lit::V(Py::Str(s)))
            }
            T::Bytes => Some(Lit::Opaque),
            T::Name(n) => match n.as_str() {
                "True" => Some(Lit::V(Py::Bool(true))),
                "False" => Some(Lit::V(Py::Bool(false))),
                "None" => Some(Lit::V(Py::None)),
                _ => None, // NameError: there are no names in this eval
            },
            T::P("(") => {
                if self.eat(")") {
                    return Some(Lit::Opaque);
                }
                let v = self.expr()?;
                if !self.eat(")") {
                    return None;
                }
                Some(v)
            }
            T::P(open @ ("[" | "{")) => {
                let close = if open == "[" { "]" } else { "}" };
                while !self.eat(close) {
                    self.not_expr()?;
                    if open == "{" && self.eat(":") {
                        self.not_expr()?;
                    }
                    if !self.eat(",") {
                        if !self.eat(close) {
                            return None;
                        }
                        break;
                    }
                }
                Some(Lit::Opaque)
            }
            _ => None,
        }
    }
}

fn truthy(v: &Lit) -> bool {
    match v {
        Lit::Opaque => true,
        Lit::V(Py::None) => false,
        Lit::V(Py::Bool(b)) => *b,
        Lit::V(Py::Int(i)) => *i != 0,
        Lit::V(Py::Float(f)) => *f != 0.0,
        Lit::V(Py::Str(s)) => !s.is_empty(),
    }
}

fn binop(op: &str, a: Lit, b: Lit) -> Option<Lit> {
    let (Lit::V(a), Lit::V(b)) = (a, b) else { return Some(Lit::Opaque) };
    if let (Some(x), Some(y)) = (a.num(), b.num()) {
        return arith(op, x, y).map(|n| Lit::V(match n {
            Num::I(i) => Py::Int(i),
            Num::F(f) => Py::Float(f),
        }));
    }
    match (op, &a, &b) {
        ("+", Py::Str(x), Py::Str(y)) => Some(Lit::V(Py::Str(format!("{x}{y}")))),
        ("*", Py::Str(s), n) | ("*", n, Py::Str(s)) => match n {
            Py::Int(k) => Some(Lit::V(Py::Str(s.repeat((*k).max(0) as usize)))),
            Py::Bool(k) => Some(Lit::V(Py::Str(if *k { s.clone() } else { String::new() }))),
            _ => None,
        },
        _ => None, // TypeError
    }
}

fn arith(op: &str, x: Num, y: Num) -> Option<Num> {
    if let (Num::I(a), Num::I(b)) = (x, y) {
        return match op {
            "+" => Some(a.checked_add(b).map(Num::I).unwrap_or(Num::F(a as f64 + b as f64))),
            "-" => Some(a.checked_sub(b).map(Num::I).unwrap_or(Num::F(a as f64 - b as f64))),
            "*" => Some(a.checked_mul(b).map(Num::I).unwrap_or(Num::F(a as f64 * b as f64))),
            "/" => if b == 0 { None } else { Some(Num::F(a as f64 / b as f64)) },
            "//" => if b == 0 { None } else { Some(Num::I(pyfmt::floordiv(a, b))) },
            "%" => if b == 0 { None } else { Some(Num::I(a - b * pyfmt::floordiv(a, b))) },
            "**" => {
                if b < 0 {
                    if a == 0 { None } else { Some(Num::F((a as f64).powf(b as f64))) }
                } else {
                    Some(u32::try_from(b).ok().and_then(|e| a.checked_pow(e)).map(Num::I)
                        .unwrap_or(Num::F((a as f64).powf(b as f64))))
                }
            }
            _ => None,
        };
    }
    let (a, b) = (x.f(), y.f());
    match op {
        "+" => Some(Num::F(a + b)),
        "-" => Some(Num::F(a - b)),
        "*" => Some(Num::F(a * b)),
        "/" => if b == 0.0 { None } else { Some(Num::F(a / b)) },
        "//" => if b == 0.0 { None } else { Some(Num::F((a / b).floor())) },
        "%" => if b == 0.0 { None } else { Some(Num::F(a - b * (a / b).floor())) },
        "**" => if a == 0.0 && b < 0.0 { None } else { Some(Num::F(a.powf(b))) },
        _ => None,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn v(s: &str) -> String {
        match eval(s) {
            Some(Lit::V(p)) => p.repr(),
            Some(Lit::Opaque) => "<opaque>".into(),
            None => "<error>".into(),
        }
    }

    #[test]
    fn splits_like_the_regex() {
        let (n, op, r) = split("i>=5").unwrap();
        assert_eq!((n.as_str(), op, r.as_str()), ("i", Op::Ge, "5"));
        let (n, op, r) = split("self.x == 'a b'").unwrap();
        assert_eq!((n.as_str(), op, r.as_str()), ("self.x", Op::Eq, "'a b'"));
        // `a==` cannot be `==` with nothing after it, so it is `=` then "=".
        let (_, op, r) = split("a==").unwrap();
        assert_eq!((op, r.as_str()), (Op::Eq, "="));
        assert!(split("grid").is_none());
        assert!(split("5 > i").is_none());
    }

    #[test]
    fn evaluates_literals_like_python() {
        assert_eq!(v("5"), "5");
        assert_eq!(v("-2.5"), "-2.5");
        assert_eq!(v("2*3+1"), "7");
        assert_eq!(v("7/2"), "3.5");
        assert_eq!(v("-7//2"), "-4");
        assert_eq!(v("'a' 'b'"), "'ab'");
        assert_eq!(v("True"), "True");
        assert_eq!(v("0x1F"), "31");
        assert_eq!(v("1e3"), "1000.0");
        assert_eq!(v("[1, 2]"), "<opaque>");
        assert_eq!(v("abc"), "<error>");
        assert_eq!(v("1/0"), "<error>");
        assert_eq!(v("007"), "<error>");
        assert_eq!(v("2**10"), "1024");
    }

    #[test]
    fn compares_like_python() {
        let t = target("5");
        assert!(test(Op::Gt, &Py::Int(6), &t));
        assert!(test(Op::Eq, &Py::Float(5.0), &t));
        assert!(test(Op::Eq, &Py::Bool(true), &target("1")));
        assert!(!test(Op::Lt, &Py::Str("a".into()), &t)); // TypeError -> False
        assert!(test(Op::Ne, &Py::Str("a".into()), &t));
        assert!(test(Op::Eq, &Py::Str("abc".into()), &target("abc"))); // NameError -> text
        assert!(test(Op::Eq, &Py::None, &target("None")));
        assert!(!test(Op::Eq, &Py::Int(1), &target("[1]")));
        assert!(test(Op::Ne, &Py::Int(1), &target("[1]")));
    }
}
