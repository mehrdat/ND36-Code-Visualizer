//! The trace format. Mirrors `nd36/schema.py` exactly -- if the two ever
//! disagree, `nd x` will half-read a Rust trace and show nonsense, so the
//! `schema_matches_python` test reads the Python file and checks.
//!
//! One event = one fixed-width 42-byte record in `events.bin`:
//!
//! ```text
//!   t_ns  kind frame loc  name rk  ri  rf  payload
//!   u64   u8   u32   u32  u32  u8  i64 f64 u32        little-endian
//! ```
//!
//! Fixed width is the point: step N lives at byte (N-1)*42, so seeking needs
//! no index at all.

pub const RECORD_SIZE: usize = 42;
/// The struct format string from schema.py, kept here so a test can compare.
#[cfg(test)]
pub const PY_RECORD: &str = "<Q B I I I B q d I";

// Event kinds. `KIND_CODE = {k: i + 1 for i, k in enumerate(ALL_KINDS)}`.
pub const LINE: u8 = 1;
pub const CALL: u8 = 2;
pub const RETURN: u8 = 3;
pub const YIELD: u8 = 4;
pub const RESUME: u8 = 5;
pub const BIND: u8 = 6;
pub const MUTATE: u8 = 7;
pub const DELETE: u8 = 8;
pub const RAISE: u8 = 9;
pub const UNWIND: u8 = 10;
pub const IO: u8 = 11;

pub fn kind_name(k: u8) -> &'static str {
    match k {
        LINE => "LINE",
        CALL => "CALL",
        RETURN => "RETURN",
        YIELD => "YIELD",
        RESUME => "RESUME",
        BIND => "BIND",
        MUTATE => "MUTATE",
        DELETE => "DELETE",
        RAISE => "RAISE",
        UNWIND => "UNWIND",
        IO => "IO",
        _ => "?",
    }
}

// Value-ref kinds: how the (rk, ri, rf) triple is to be read.
pub const RK_ABSENT: u8 = 0;
pub const RK_INT: u8 = 1; // ri holds the int
pub const RK_FLOAT: u8 = 2; // rf holds the float
pub const RK_STR: u8 = 3; // ri is an id into `strings`
pub const RK_BOOL: u8 = 4; // ri is 0 or 1
pub const RK_OBJ: u8 = 5; // ri is an obj_id into `objects`
pub const RK_NULL: u8 = 6; // None

/// A packed value reference.
#[derive(Clone, Copy, Debug, PartialEq)]
pub struct Vref {
    pub rk: u8,
    pub ri: i64,
    pub rf: f64,
}

impl Vref {
    pub const ABSENT: Vref = Vref { rk: RK_ABSENT, ri: 0, rf: 0.0 };
    pub fn int(v: i64) -> Self { Vref { rk: RK_INT, ri: v, rf: 0.0 } }
    pub fn float(v: f64) -> Self { Vref { rk: RK_FLOAT, ri: 0, rf: v } }
    pub fn string(id: u32) -> Self { Vref { rk: RK_STR, ri: id as i64, rf: 0.0 } }
    pub fn boolean(v: bool) -> Self { Vref { rk: RK_BOOL, ri: v as i64, rf: 0.0 } }
    pub fn obj(id: u32) -> Self { Vref { rk: RK_OBJ, ri: id as i64, rf: 0.0 } }
    pub const NULL: Vref = Vref { rk: RK_NULL, ri: 0, rf: 0.0 };

    /// As the JSON triple `[rk, ri, rf]` that keyframes and CALL payloads use.
    pub fn json(&self, out: &mut String) {
        use std::fmt::Write;
        let _ = write!(out, "[{},{},", self.rk, self.ri);
        push_float(out, self.rf);
        out.push(']');
    }
}

/// A float the way Python's `json.dumps` writes one, so `json.loads` reads it
/// back: `0.0` not `0`, and `NaN` / `Infinity` rather than invalid JSON.
pub fn push_float(out: &mut String, v: f64) {
    use std::fmt::Write;
    if v.is_nan() {
        out.push_str("NaN");
    } else if v.is_infinite() {
        out.push_str(if v > 0.0 { "Infinity" } else { "-Infinity" });
    } else if v == v.trunc() && v.abs() < 1e16 {
        let _ = write!(out, "{:.1}", v);
    } else {
        let _ = write!(out, "{}", v);
    }
}

/// Append one record to a byte buffer.
#[inline(always)]
pub fn pack(buf: &mut Vec<u8>, t: u64, kind: u8, frame: u32, loc: u32, name: u32, v: Vref, payload: u32) {
    buf.extend_from_slice(&t.to_le_bytes());
    buf.push(kind);
    buf.extend_from_slice(&frame.to_le_bytes());
    buf.extend_from_slice(&loc.to_le_bytes());
    buf.extend_from_slice(&name.to_le_bytes());
    buf.push(v.rk);
    buf.extend_from_slice(&v.ri.to_le_bytes());
    buf.extend_from_slice(&v.rf.to_le_bytes());
    buf.extend_from_slice(&payload.to_le_bytes());
}

/// One decoded record.
#[derive(Clone, Copy, Debug)]
#[allow(dead_code)] // t and frame are part of the format; readers may want them
pub struct Record {
    pub t: u64,
    pub kind: u8,
    pub frame: u32,
    pub loc: u32,
    pub name: u32,
    pub v: Vref,
    pub payload: u32,
}

#[inline(always)]
pub fn unpack(b: &[u8]) -> Record {
    let u32at = |i: usize| u32::from_le_bytes([b[i], b[i + 1], b[i + 2], b[i + 3]]);
    Record {
        t: u64::from_le_bytes(b[0..8].try_into().unwrap()),
        kind: b[8],
        frame: u32at(9),
        loc: u32at(13),
        name: u32at(17),
        v: Vref {
            rk: b[21],
            ri: i64::from_le_bytes(b[22..30].try_into().unwrap()),
            rf: f64::from_le_bytes(b[30..38].try_into().unwrap()),
        },
        payload: u32at(38),
    }
}

/// The SQLite side tables -- copied verbatim from `nd36/store.py`.
pub const DDL: &str = "
CREATE TABLE meta    (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE files   (id INTEGER PRIMARY KEY, path TEXT UNIQUE, source TEXT);
CREATE TABLE locs    (id INTEGER PRIMARY KEY, file INTEGER, line INTEGER,
                      col0 INTEGER, col1 INTEGER);
CREATE TABLE funcs   (id INTEGER PRIMARY KEY, name TEXT, qualname TEXT,
                      file INTEGER, first_line INTEGER);
CREATE TABLE names   (id INTEGER PRIMARY KEY, text TEXT UNIQUE);
CREATE TABLE strings (id INTEGER PRIMARY KEY, text TEXT);
CREATE TABLE payloads(id INTEGER PRIMARY KEY, json TEXT);
CREATE TABLE objects (obj_id INTEGER, step INTEGER, type TEXT,
                      summary TEXT, children TEXT, repr TEXT, nbytes INTEGER,
                      PRIMARY KEY (obj_id, step));
CREATE TABLE keyframes (step INTEGER PRIMARY KEY, blob BLOB);
CREATE TABLE binds   (name INTEGER, step INTEGER);
CREATE TABLE linehits(loc INTEGER PRIMARY KEY, hits INTEGER, t_ns INTEGER);
CREATE TABLE samples  (step INTEGER PRIMARY KEY, t_ns INTEGER,
                       blocks INTEGER, cur INTEGER, peak INTEGER,
                       g0 INTEGER, g1 INTEGER, g2 INTEGER);
CREATE TABLE memcurve (at_ns INTEGER PRIMARY KEY, cur INTEGER, peak INTEGER,
                       blocks INTEGER);
CREATE TABLE snapshots(id INTEGER PRIMARY KEY, step INTEGER, why TEXT,
                       total INTEGER);
CREATE TABLE allocs   (snap INTEGER, path TEXT, line INTEGER,
                       size INTEGER, count INTEGER);
";

#[cfg(test)]
mod tests {
    use super::*;

    /// The one test that keeps the two implementations honest: read the
    /// Python schema file and check every constant we rely on.
    #[test]
    fn schema_matches_python() {
        let path = concat!(env!("CARGO_MANIFEST_DIR"), "/../nd36/schema.py");
        let py = std::fs::read_to_string(path).expect("nd36/schema.py next to ndr/");
        assert!(py.contains(&format!("RECORD = \"{}\"", PY_RECORD)), "record layout drifted");
        let kinds = ["LINE", "CALL", "RETURN", "YIELD", "RESUME", "BIND", "MUTATE",
                     "DELETE", "RAISE", "UNWIND", "IO"];
        let all = py.split("ALL_KINDS = (").nth(1).unwrap().split(')').next().unwrap();
        let order: Vec<&str> = all.split(',').map(|s| s.trim()).filter(|s| !s.is_empty()).collect();
        assert_eq!(order, kinds, "event kind order drifted");
        for (code, k) in kinds.iter().enumerate() {
            assert_eq!(kind_name(code as u8 + 1), *k);
        }
        for (name, val) in [("RK_ABSENT", 0), ("RK_INT", 1), ("RK_FLOAT", 2), ("RK_STR", 3),
                            ("RK_BOOL", 4), ("RK_OBJ", 5), ("RK_NULL", 6)] {
            assert!(py.contains(&format!("{} = {}", name, val)), "{name} drifted");
        }
        let store = std::fs::read_to_string(concat!(env!("CARGO_MANIFEST_DIR"), "/../nd36/store.py")).unwrap();
        for table in ["meta", "files", "locs", "funcs", "names", "strings", "payloads",
                      "objects", "keyframes", "binds", "linehits"] {
            assert!(store.contains(&format!("CREATE TABLE {table}")), "{table} missing in store.py");
            assert!(DDL.contains(&format!("CREATE TABLE {table}")), "{table} missing here");
        }
    }

    #[test]
    fn a_record_is_42_bytes_and_round_trips() {
        let mut buf = Vec::new();
        let v = Vref { rk: RK_FLOAT, ri: -7, rf: 2.5 };
        pack(&mut buf, 123, MUTATE, 9, 44, 3, v, 17);
        assert_eq!(buf.len(), RECORD_SIZE);
        let r = unpack(&buf);
        assert_eq!((r.t, r.kind, r.frame, r.loc, r.name, r.payload), (123, MUTATE, 9, 44, 3, 17));
        assert_eq!(r.v, v);
    }

    #[test]
    fn floats_are_written_the_way_python_json_reads_them() {
        let mut s = String::new();
        for (v, want) in [(0.0, "0.0"), (3.0, "3.0"), (2.5, "2.5"), (f64::NAN, "NaN"),
                          (f64::INFINITY, "Infinity"), (-f64::INFINITY, "-Infinity")] {
            s.clear();
            push_float(&mut s, v);
            assert_eq!(s, want);
        }
    }
}
