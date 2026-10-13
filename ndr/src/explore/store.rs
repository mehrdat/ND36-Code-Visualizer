//! The read side `nd x` needs, in Rust: `nd36/store.py`'s `TraceReader`.
//!
//! `reader::Trace` opens the folder (with its plain-words errors); this adds
//! what the explorer asks for: the event log memory-mapped so a scan over a
//! million steps is a loop over a slice, the side tables in their SQLite
//! order (dict order is what Python's tables print in), and the program
//! state at any step, rebuilt from the nearest keyframe.

use super::json::{self, Json};
use super::pyfmt::Num;
use crate::reader::Trace;
use crate::schema::{self, Record, RECORD_SIZE};
use rusqlite::OptionalExtension;
use std::collections::HashMap;
use std::io::Read;

/// A small insertion-ordered map: the shape of a Python dict.
#[derive(Clone, Debug)]
pub struct Om<K: std::hash::Hash + Eq + Clone, V> {
    pub items: Vec<(K, V)>,
    idx: HashMap<K, usize>,
}

impl<K: std::hash::Hash + Eq + Clone, V> Default for Om<K, V> {
    fn default() -> Self {
        Om { items: Vec::new(), idx: HashMap::new() }
    }
}

impl<K: std::hash::Hash + Eq + Clone, V> Om<K, V> {
    pub fn new() -> Self {
        Self::default()
    }
    pub fn get(&self, k: &K) -> Option<&V> {
        self.idx.get(k).map(|&i| &self.items[i].1)
    }
    pub fn contains(&self, k: &K) -> bool {
        self.idx.contains_key(k)
    }
    /// `d[k] = v`: an existing key keeps its place.
    pub fn insert(&mut self, k: K, v: V) {
        match self.idx.get(&k) {
            Some(&i) => self.items[i].1 = v,
            None => {
                self.idx.insert(k.clone(), self.items.len());
                self.items.push((k, v));
            }
        }
    }
    pub fn entry_or(&mut self, k: K, make: impl FnOnce() -> V) -> &mut V {
        let i = match self.idx.get(&k) {
            Some(&i) => i,
            None => {
                self.idx.insert(k.clone(), self.items.len());
                self.items.push((k, make()));
                self.items.len() - 1
            }
        };
        &mut self.items[i].1
    }
    /// `d.pop(k, None)`.
    pub fn remove(&mut self, k: &K) -> Option<V> {
        let i = self.idx.remove(k)?;
        let (_, v) = self.items.remove(i);
        for (j, (key, _)) in self.items.iter().enumerate().skip(i) {
            self.idx.insert(key.clone(), j);
        }
        Some(v)
    }
    pub fn len(&self) -> usize {
        self.items.len()
    }
    pub fn is_empty(&self) -> bool {
        self.items.is_empty()
    }
    pub fn keys(&self) -> impl Iterator<Item = &K> {
        self.items.iter().map(|(k, _)| k)
    }
    pub fn iter(&self) -> impl Iterator<Item = (&K, &V)> {
        self.items.iter().map(|(k, v)| (k, v))
    }
    pub fn values(&self) -> impl Iterator<Item = &V> {
        self.items.iter().map(|(_, v)| v)
    }
}

/// An immediate value, as `vref()` hands it back inside `["i", v]`.
#[derive(Clone, Debug, PartialEq)]
pub enum Py {
    None,
    Bool(bool),
    Int(i64),
    Float(f64),
    Str(String),
}

impl Py {
    /// `repr(v)`.
    pub fn repr(&self) -> String {
        match self {
            Py::None => "None".into(),
            Py::Bool(b) => if *b { "True".into() } else { "False".into() },
            Py::Int(i) => i.to_string(),
            Py::Float(f) => super::pyfmt::repr_float(*f),
            Py::Str(s) => super::pyfmt::repr_str(s),
        }
    }
    pub fn type_name(&self) -> &'static str {
        match self {
            Py::None => "NoneType",
            Py::Bool(_) => "bool",
            Py::Int(_) => "int",
            Py::Float(_) => "float",
            Py::Str(_) => "str",
        }
    }
    /// Python `==` between two immediates (1 == 1.0 == True).
    pub fn py_eq(&self, other: &Py) -> bool {
        match (self.num(), other.num()) {
            (Some(a), Some(b)) => match (a, b) {
                (Num::I(x), Num::I(y)) => x == y,
                (a, b) => a.f() == b.f(),
            },
            _ => match (self, other) {
                (Py::None, Py::None) => true,
                (Py::Str(a), Py::Str(b)) => a == b,
                _ => false,
            },
        }
    }
    pub fn num(&self) -> Option<Num> {
        match self {
            Py::Bool(b) => Some(Num::I(*b as i64)),
            Py::Int(i) => Some(Num::I(*i)),
            Py::Float(f) => Some(Num::F(*f)),
            _ => None,
        }
    }
}

/// A display ref: `["i", value]` or `["o", obj_id]`. Python's `None` ref
/// (absent) is `Option::None` wherever one of these is held.
#[derive(Clone, Debug)]
pub enum Ref {
    I(Py),
    O(i64),
}

impl Ref {
    /// `ref_a == ref_b` as Python compares the two lists.
    pub fn same(a: &Option<Ref>, b: &Option<Ref>) -> bool {
        match (a, b) {
            (None, None) => true,
            (Some(Ref::I(x)), Some(Ref::I(y))) => x.py_eq(y),
            (Some(Ref::O(x)), Some(Ref::O(y))) => x == y,
            _ => false,
        }
    }
}

/// What `resolve()` returns for an object: one version of it.
#[derive(Clone, Debug)]
pub struct Obj {
    pub ty: String,
    pub summary: Json,
    pub children: Option<Json>,
    pub repr: String,
}

pub enum Resolved {
    /// A plain Python value (including the "<obj N>" string for a miss).
    Val(Py),
    Obj(Obj),
}

#[derive(Clone, Debug)]
pub struct Frame {
    pub frame: i64,
    /// The `func` field: a function id, or None after a Python keyframe.
    pub func: Option<i64>,
    pub scope: String,
    pub loc: i64,
    pub locals: Om<String, Option<Ref>>,
}

pub struct State {
    pub frames: Vec<Frame>,
    pub globals: Om<String, Option<Ref>>,
}

/// One event, decoded for display: `TraceReader.view()`.
pub struct View {
    pub kind: String,
    pub file: Option<String>,
    pub line: Option<i64>,
    pub name: Option<String>,
    pub r: Option<Ref>,
    pub extra: Json,
}

#[derive(Clone, Debug)]
pub struct FuncRow {
    pub name: Option<String>,
    pub qualname: Option<String>,
    pub file: Option<i64>,
    pub first_line: Option<i64>,
}

impl FuncRow {
    /// `f.get("qualname") or f.get("name")`.
    pub fn label(&self) -> Option<&str> {
        self.qualname.as_deref().filter(|s| !s.is_empty()).or(self.name.as_deref().filter(|s| !s.is_empty()))
    }
}

/// The event log, mapped read-only.
struct Log {
    ptr: *const u8,
    len: usize,
}

impl Log {
    fn open(path: &std::path::Path) -> Log {
        let empty = Log { ptr: std::ptr::null(), len: 0 };
        let Ok(f) = std::fs::File::open(path) else { return empty };
        let len = f.metadata().map(|m| m.len() as usize).unwrap_or(0);
        if len == 0 {
            return empty;
        }
        use std::os::unix::io::AsRawFd;
        // SAFETY: a private read-only mapping of a file we just opened; the
        // pointer is checked against MAP_FAILED and unmapped in Drop.
        let p = unsafe { libc::mmap(std::ptr::null_mut(), len, libc::PROT_READ, libc::MAP_PRIVATE, f.as_raw_fd(), 0) };
        if p == libc::MAP_FAILED {
            return empty;
        }
        Log { ptr: p as *const u8, len }
    }
    fn bytes(&self) -> &[u8] {
        if self.ptr.is_null() {
            &[]
        } else {
            // SAFETY: ptr/len describe the live mapping made in `open`.
            unsafe { std::slice::from_raw_parts(self.ptr, self.len) }
        }
    }
}

impl Drop for Log {
    fn drop(&mut self) {
        if !self.ptr.is_null() {
            // SAFETY: exactly the region mmap returned.
            unsafe { libc::munmap(self.ptr as *mut libc::c_void, self.len) };
        }
    }
}

pub struct Store {
    pub t: Trace,
    log: Log,
    pub n_steps: u64,
    /// files: id -> path, in table order.
    pub files: Om<i64, String>,
    /// locs: id -> (file id, line), in table order.
    pub locs: Om<i64, (i64, Option<i64>)>,
    pub funcs: Om<i64, FuncRow>,
    pub names: HashMap<u32, String>,
    pub strings: HashMap<i64, String>,
    pub meta: Vec<(String, Json)>,
}

impl Store {
    pub fn open(t: Trace) -> Store {
        let log = Log::open(&t.dir.join("events.bin"));
        let n_steps = (log.len / RECORD_SIZE) as u64;
        let db = &t.db;
        let mut files = Om::new();
        if let Ok(mut st) = db.prepare("SELECT id, path FROM files") {
            if let Ok(rows) = st.query_map([], |r| Ok((r.get::<_, i64>(0)?, r.get::<_, Option<String>>(1)?))) {
                for (id, p) in rows.flatten() {
                    files.insert(id, p.unwrap_or_default());
                }
            }
        }
        let mut locs = Om::new();
        if let Ok(mut st) = db.prepare("SELECT id, file, line FROM locs") {
            if let Ok(rows) = st.query_map([], |r| Ok((r.get::<_, i64>(0)?, r.get::<_, Option<i64>>(1)?, r.get::<_, Option<i64>>(2)?))) {
                for (id, f, l) in rows.flatten() {
                    locs.insert(id, (f.unwrap_or(-1), l));
                }
            }
        }
        let mut funcs = Om::new();
        if let Ok(mut st) = db.prepare("SELECT id, name, qualname, file, first_line FROM funcs") {
            if let Ok(rows) = st.query_map([], |r| Ok((r.get::<_, i64>(0)?, FuncRow {
                name: r.get(1)?, qualname: r.get(2)?, file: r.get(3)?, first_line: r.get(4)?,
            }))) {
                for (id, f) in rows.flatten() {
                    funcs.insert(id, f);
                }
            }
        }
        let mut strings = HashMap::new();
        if let Ok(mut st) = db.prepare("SELECT id, text FROM strings") {
            if let Ok(rows) = st.query_map([], |r| Ok((r.get::<_, i64>(0)?, r.get::<_, Option<String>>(1)?))) {
                for (id, s) in rows.flatten() {
                    strings.insert(id, s.unwrap_or_default());
                }
            }
        }
        let mut meta = Vec::new();
        if let Ok(mut st) = db.prepare("SELECT key, value FROM meta") {
            if let Ok(rows) = st.query_map([], |r| Ok((r.get::<_, String>(0)?, r.get::<_, Option<String>>(1)?))) {
                for (k, v) in rows.flatten() {
                    let v = v.unwrap_or_default();
                    meta.push((k, json::parse(&v).unwrap_or(Json::Str(v))));
                }
            }
        }
        let names = t.names.clone();
        Store { t, log, n_steps, files, locs, funcs, names, strings, meta }
    }

    pub fn dir(&self) -> &std::path::Path {
        &self.t.dir
    }

    pub fn meta(&self, key: &str) -> Option<&Json> {
        self.meta.iter().rev().find(|(k, _)| k == key).map(|(_, v)| v)
    }

    /// Every record, as one slice: `bytes()[(step-1)*42..]`.
    pub fn log(&self) -> &[u8] {
        self.log.bytes()
    }

    /// The packed record for one step. O(1).
    pub fn raw(&self, step: u64) -> Record {
        let b = self.log.bytes();
        if step == 0 || step > self.n_steps {
            return schema::unpack(&[0u8; RECORD_SIZE]);
        }
        let off = (step as usize - 1) * RECORD_SIZE;
        schema::unpack(&b[off..off + RECORD_SIZE])
    }

    pub fn vref(&self, rk: u8, ri: i64, rf: f64) -> Option<Ref> {
        Some(match rk {
            schema::RK_ABSENT => return None,
            schema::RK_INT => Ref::I(Py::Int(ri)),
            schema::RK_FLOAT => Ref::I(Py::Float(rf)),
            schema::RK_STR => Ref::I(Py::Str(self.strings.get(&ri).cloned().unwrap_or_default())),
            schema::RK_BOOL => Ref::I(Py::Bool(ri != 0)),
            schema::RK_OBJ => Ref::O(ri),
            _ => Ref::I(Py::None),
        })
    }

    pub fn rec_ref(&self, r: &Record) -> Option<Ref> {
        self.vref(r.v.rk, r.v.ri, r.v.rf)
    }

    /// `_norm`: a `[rk, ri, rf]` triple from JSON back into a display ref.
    pub fn norm(&self, v: &Json) -> Option<Ref> {
        match v {
            Json::Arr(t) if t.len() == 3 => {
                let rk = t[0].as_i64().unwrap_or(0);
                let ri = t[1].as_i64().unwrap_or(0);
                let rf = t[2].as_f64().unwrap_or(0.0);
                // vref() compares rk with ==, so an rk Python would not
                // recognise lands on the final fallback: an immediate None.
                if !(0..=255).contains(&rk) {
                    return Some(Ref::I(Py::None));
                }
                self.vref(rk as u8, ri, rf)
            }
            _ => None,
        }
    }

    pub fn payload(&self, pid: u32) -> Json {
        let row: Option<Option<String>> = self.t.db
            .prepare_cached("SELECT json FROM payloads WHERE id=?1")
            .and_then(|mut st| st.query_row([pid], |r| r.get(0)).optional())
            .unwrap_or(None);
        match row {
            Some(Some(text)) => json::parse(&text).unwrap_or(Json::Obj(vec![])),
            _ => Json::Obj(vec![]),
        }
    }

    pub fn view(&self, step: u64) -> View {
        let r = self.raw(step);
        let (file, line) = self.where_(r.loc as i64);
        View {
            kind: match schema::kind_name(r.kind) { "?" => "None".into(), k => k.into() },
            file,
            line,
            name: self.names.get(&r.name).cloned(),
            r: self.rec_ref(&r),
            extra: if r.payload != 0 { self.payload(r.payload) } else { Json::Obj(vec![]) },
        }
    }

    pub fn where_(&self, loc: i64) -> (Option<String>, Option<i64>) {
        match self.locs.get(&loc) {
            Some((f, l)) => (self.files.get(f).cloned(), *l),
            None => (None, None),
        }
    }

    pub fn source(&self, path: &str) -> String {
        self.t.db
            .prepare_cached("SELECT source FROM files WHERE path=?1")
            .and_then(|mut st| st.query_row([path], |r| r.get::<_, Option<String>>(0)).optional())
            .ok().flatten().flatten().unwrap_or_default()
    }

    /// A ref -> something printable, at a given step.
    pub fn resolve(&self, r: &Ref, step: i64) -> Resolved {
        let id = match r {
            Ref::I(v) => return Resolved::Val(v.clone()),
            Ref::O(id) => *id,
        };
        type Row = (Option<String>, Option<String>, Option<String>, Option<String>);
        let row: Option<Row> = self.t.db
            .prepare_cached("SELECT type, summary, children, repr FROM objects WHERE obj_id=?1 AND step<=?2 ORDER BY step DESC LIMIT 1")
            .and_then(|mut st| st.query_row(rusqlite::params![id, step], |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?, r.get(3)?))).optional())
            .unwrap_or(None);
        match row {
            None => Resolved::Val(Py::Str(format!("<obj {id}>"))),
            Some((ty, summary, children, repr)) => Resolved::Obj(Obj {
                ty: ty.unwrap_or_else(|| "None".into()),
                summary: summary.and_then(|s| json::parse(&s)).unwrap_or(Json::Obj(vec![])),
                children: children.filter(|c| !c.is_empty()).and_then(|c| json::parse(&c)),
                repr: repr.unwrap_or_else(|| "None".into()),
            }),
        }
    }

    /// `[[key, triple], ...]` -> `[(str(key), ref)]`.
    pub fn decode_children(&self, children: &Json) -> Vec<(String, Option<Ref>)> {
        children.arr().iter().filter_map(|kv| match kv {
            Json::Arr(p) if p.len() == 2 => Some((p[0].py_str(), self.norm(&p[1]))),
            _ => None,
        }).collect()
    }

    fn frame_from(&self, f: &Json) -> Frame {
        let mut locals = Om::new();
        for (k, v) in f.get("locals").map(|l| l.items()).unwrap_or(&[]) {
            locals.insert(k.clone(), self.norm(v));
        }
        Frame {
            frame: f.get("frame").and_then(|v| v.as_i64()).unwrap_or(0),
            func: f.get("func").and_then(|v| v.as_i64()),
            scope: f.get("scope").and_then(|v| v.as_str()).unwrap_or("local").to_string(),
            loc: f.get("loc").and_then(|v| v.as_i64()).unwrap_or(0),
            locals,
        }
    }

    /// Program state as it was when `step` was about to execute.
    pub fn state_at(&self, step: u64) -> State {
        let step = step.clamp(1, self.n_steps.max(1));
        let row: Option<(i64, Vec<u8>)> = self.t.db
            .prepare_cached("SELECT step, blob FROM keyframes WHERE step<=?1 ORDER BY step DESC LIMIT 1")
            .and_then(|mut st| st.query_row([step as i64], |r| Ok((r.get(0)?, r.get(1)?))).optional())
            .unwrap_or(None);
        // Frames live in an arena so a frame id can map to the latest one
        // with that id, as Python's `by_id` dict does.
        let mut arena: Vec<Frame> = Vec::new();
        let mut order: Vec<usize> = Vec::new();
        let mut by_id: HashMap<i64, usize> = HashMap::new();
        let mut glob: Om<String, Option<Ref>> = Om::new();
        let mut base = 0u64;
        if let Some((s, blob)) = row {
            base = s.max(0) as u64;
            let mut text = String::new();
            let _ = flate2::read::ZlibDecoder::new(&blob[..]).read_to_string(&mut text);
            if let Some(state) = json::parse(&text) {
                for f in state.get("frames").map(|v| v.arr()).unwrap_or(&[]) {
                    let fr = self.frame_from(f);
                    by_id.insert(fr.frame, arena.len());
                    order.push(arena.len());
                    arena.push(fr);
                }
                for (k, v) in state.get("globals").map(|g| g.items()).unwrap_or(&[]) {
                    glob.insert(k.clone(), self.norm(v));
                }
            }
        }
        let b = self.log.bytes();
        for s in (base + 1)..=step.min(self.n_steps) {
            let off = (s as usize - 1) * RECORD_SIZE;
            let r = schema::unpack(&b[off..off + RECORD_SIZE]);
            let fr = r.frame as i64;
            match r.kind {
                schema::LINE => {
                    if let Some(&i) = by_id.get(&fr) {
                        arena[i].loc = r.loc as i64;
                    }
                }
                schema::BIND => {
                    let Some(name) = self.names.get(&r.name) else { continue };
                    let v = self.rec_ref(&r);
                    match by_id.get(&fr) {
                        Some(&i) => arena[i].locals.insert(name.clone(), v),
                        None => glob.insert(name.clone(), v),
                    }
                }
                schema::CALL => {
                    let extra = if r.payload != 0 { self.payload(r.payload) } else { Json::Obj(vec![]) };
                    let mut locals = Om::new();
                    if let Some(args) = extra.get("args") {
                        for (k, v) in args.items() {
                            locals.insert(k.clone(), self.norm(v));
                        }
                    }
                    let f = Frame {
                        frame: fr,
                        func: extra.get("func").and_then(|v| v.as_i64()),
                        scope: extra.get("scope").and_then(|v| v.as_str()).unwrap_or("local").to_string(),
                        loc: r.loc as i64,
                        locals,
                    };
                    by_id.insert(fr, arena.len());
                    order.push(arena.len());
                    arena.push(f);
                }
                schema::RETURN | schema::UNWIND => {
                    if let Some(i) = by_id.remove(&fr) {
                        if let Some(p) = order.iter().position(|&j| j == i) {
                            order.remove(p);
                        }
                    }
                }
                schema::DELETE => {
                    let Some(name) = self.names.get(&r.name) else { continue };
                    match by_id.get(&fr) {
                        Some(&i) => { arena[i].locals.remove(name); }
                        None => { glob.remove(name); }
                    }
                }
                _ => {}
            }
        }
        // A module-level frame's locals ARE the globals.
        let mut frames = Vec::new();
        for i in order {
            let f = std::mem::replace(&mut arena[i], Frame { frame: 0, func: None, scope: String::new(), loc: 0, locals: Om::new() });
            if f.scope == "global" {
                for (k, v) in f.locals.items {
                    glob.insert(k, v);
                }
            } else {
                frames.push(f);
            }
        }
        State { frames, globals: glob }
    }

    /// Every step that wrote `name`. One indexed read.
    pub fn steps_of(&self, name: &str) -> Vec<u64> {
        self.t.steps_of(name, None)
    }

    /// The first `limit` writes of a name (no limit for 0, as `if limit:`).
    pub fn history_steps(&self, name: &str, limit: i64) -> Vec<u64> {
        let Some(&id) = self.t.name_ids.get(name) else { return vec![] };
        let mut sql = "SELECT step FROM binds WHERE name=?1 ORDER BY step".to_string();
        if limit != 0 {
            sql += &format!(" LIMIT {limit}");
        }
        let Ok(mut st) = self.t.db.prepare(&sql) else { return vec![] };
        st.query_map([id], |r| r.get::<_, i64>(0))
            .map(|rows| rows.flatten().map(|s| s as u64).collect())
            .unwrap_or_default()
    }

    /// `[(step, number)]` for plotting -- straight out of the log.
    pub fn numeric_series(&self, name: &str, cap: usize) -> (Vec<(u64, Option<Num>)>, Option<i64>) {
        let steps = self.steps_of(name);
        if steps.is_empty() {
            return (vec![], None);
        }
        let stride = (steps.len() / cap.max(1)).max(1);
        let mut obj: Option<i64> = None;
        let out = steps.iter().step_by(stride).map(|&s| {
            let r = self.raw(s);
            let v = match r.v.rk {
                schema::RK_INT => Some(Num::I(r.v.ri)),
                schema::RK_FLOAT => Some(Num::F(r.v.rf)),
                schema::RK_BOOL => Some(Num::I((r.v.ri != 0) as i64)),
                schema::RK_OBJ => {
                    if obj.unwrap_or(0) == 0 {
                        obj = Some(r.v.ri);
                    }
                    None
                }
                _ => None,
            };
            (s, v)
        }).collect();
        (out, obj)
    }

    /// `[(step, summary, nbytes)]` -- every version of one object.
    pub fn object_series(&self, obj: i64) -> Vec<(u64, Json, Option<Json>)> {
        let Ok(mut st) = self.t.db.prepare("SELECT step, summary, nbytes FROM objects WHERE obj_id=?1 ORDER BY step") else {
            return vec![];
        };
        let rows = st.query_map([obj], |r| {
            let nb: rusqlite::types::Value = r.get(2)?;
            Ok((r.get::<_, i64>(0)?, r.get::<_, Option<String>>(1)?, nb))
        });
        let Ok(rows) = rows else { return vec![] };
        rows.flatten().map(|(s, sm, nb)| {
            let summary = sm.filter(|t| !t.is_empty()).and_then(|t| json::parse(&t)).unwrap_or(Json::Obj(vec![]));
            let nb = match nb {
                rusqlite::types::Value::Integer(i) => Some(Json::Int(i)),
                rusqlite::types::Value::Real(f) => Some(Json::Float(f)),
                _ => None,
            };
            (s as u64, summary, nb)
        }).collect()
    }

    /// `{(file, line): (hits, total_ns)}` -- the execution heatmap.
    pub fn line_stats(&self) -> Om<(Option<String>, Option<i64>), (i64, i64)> {
        let mut out = Om::new();
        if let Ok(mut st) = self.t.db.prepare("SELECT loc, hits, t_ns FROM linehits") {
            if let Ok(rows) = st.query_map([], |r| Ok((r.get::<_, i64>(0)?, r.get::<_, Option<i64>>(1)?, r.get::<_, Option<i64>>(2)?))) {
                for (loc, h, t) in rows.flatten() {
                    out.insert(self.where_(loc), (h.unwrap_or(0), t.unwrap_or(0)));
                }
            }
        }
        out
    }

    /// `f.get("qualname") or f.get("name")` for a frame's `func` field.
    pub fn func_label(&self, func: Option<i64>) -> Option<String> {
        func.and_then(|id| self.funcs.get(&id)).and_then(|f| f.label().map(|s| s.to_string()))
    }
}
