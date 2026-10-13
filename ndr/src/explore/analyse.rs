//! One pass over the event log that answers every structural question:
//! `nd36/analyse.py`, in Rust.
//!
//!     functions   calls, self/total steps and time, callers, callees
//!     variables   how often written, by which function, on which line
//!     activity    a per-function density over time, for the timeline
//!     invocations call/return pairs, so one call's args and result line up
//!
//! Cached as `analysis.ndr.json` in the trace folder (nd keeps its own
//! `analysis.json`; the two never touch each other's file).
//!
//! Reads are static, as in nd: they come from the program's AST, walked in
//! the same breadth-first order as Python's `ast.walk`, because that order
//! decides how ties print.

use super::json::{self, Json};
use super::pyfmt::basename;
use super::store::{Om, Store};
use crate::schema::{self, RECORD_SIZE};
use rustpython_parser::ast::{self, Expr, ExprContext, Pattern, Stmt};
use std::collections::{HashMap, VecDeque};

pub const CACHE: &str = "analysis.ndr.json";
const CACHE_VERSION: i64 = 2;
const BUCKETS: usize = 160;
const MAX_INVOCATIONS: usize = 4000;

/// The names `nd` leaves out of the reads: what `dir(__builtins__)` gives
/// inside an imported module (the builtins DICT, so dict's own attributes),
/// plus its explicit list.
const BUILTINS: &[&str] = &[
    "__class__", "__class_getitem__", "__contains__", "__delattr__", "__delitem__", "__dir__", "__doc__",
    "__eq__", "__format__", "__ge__", "__getattribute__", "__getitem__", "__getstate__", "__gt__",
    "__hash__", "__init__", "__init_subclass__", "__ior__", "__iter__", "__le__", "__len__", "__lt__",
    "__ne__", "__new__", "__or__", "__reduce__", "__reduce_ex__", "__repr__", "__reversed__", "__ror__",
    "__setattr__", "__setitem__", "__sizeof__", "__str__", "__subclasshook__", "abs", "all", "any",
    "bool", "clear", "cls", "copy", "dict", "enumerate", "filter", "float", "format", "fromkeys", "get",
    "int", "isinstance", "items", "keys", "len", "list", "map", "max", "min", "open", "pop", "popitem",
    "print", "range", "repr", "reversed", "round", "self", "set", "setdefault", "sorted", "str", "sum",
    "super", "tuple", "type", "update", "values", "zip",
];

#[derive(Clone, Debug, Default)]
pub struct Func {
    pub name: String,
    pub file: Option<String>,
    pub line: i64,
    pub calls: u64,
    pub returns: u64,
    pub raises: u64,
    pub self_steps: u64,
    pub total_steps: u64,
    pub self_ms: f64,
    pub lines: u64,
    pub first_step: u64,
    pub last_step: u64,
    pub callers: Om<String, u64>,
    pub callees: Om<String, u64>,
    pub writes: Om<String, u64>,
}

#[derive(Clone, Debug, Default)]
pub struct Var {
    pub writes: u64,
    pub binds: u64,
    pub mutates: u64,
    pub deletes: u64,
    pub first: u64,
    pub last: u64,
    pub by_func: Om<String, u64>,
    pub by_line: Om<String, u64>,
}

pub type Reads = Om<String, Om<String, u64>>;

pub struct Analysis {
    pub functions: Om<String, Func>,
    pub variables: Om<String, Var>,
    pub activity: Om<String, Vec<u64>>,
    pub invocations: Om<String, Vec<(u64, u64)>>,
    pub n_steps: u64,
    pub seconds: f64,
    reads: Option<Reads>,
}

impl Analysis {
    /// Static reads, computed on first use (as nd's lazy property).
    pub fn reads(&mut self, r: &Store) -> &Reads {
        if self.reads.is_none() {
            self.reads = Some(static_reads(r));
        }
        self.reads.as_ref().unwrap()
    }

    /// Look a function up forgivingly: exact, then case, then substring.
    pub fn func(&self, name: &str) -> Option<String> {
        if self.functions.contains(&name.to_string()) {
            return Some(name.to_string());
        }
        let low = name.to_lowercase();
        if let Some(k) = self.functions.keys().find(|k| k.to_lowercase() == low) {
            return Some(k.clone());
        }
        self.functions.keys().find(|k| k.to_lowercase().contains(&low)).cloned()
    }

    /// `Counter({fn: c[var] for fn, c in reads.items() if c.get(var)})`.
    pub fn readers_of(&mut self, r: &Store, var: &str) -> Vec<(String, u64)> {
        let key = var.to_string();
        self.reads(r).iter().filter_map(|(f, c)| c.get(&key).filter(|n| **n > 0).map(|n| (f.clone(), *n))).collect()
    }
}

/// `Counter.most_common(k)`: by count, ties in insertion order.
pub fn most_common(items: &[(String, u64)], k: usize) -> Vec<(String, u64)> {
    let mut v = items.to_vec();
    v.sort_by(|a, b| b.1.cmp(&a.1));
    v.truncate(k);
    v
}

/// `sorted(d.items(), key=lambda kv: -kv[1])`.
pub fn by_count(m: &Om<String, u64>) -> Vec<(String, u64)> {
    let mut v: Vec<(String, u64)> = m.iter().map(|(k, v)| (k.clone(), *v)).collect();
    v.sort_by(|a, b| b.1.cmp(&a.1));
    v
}

// --------------------------------------------------------------------------
// the pass
// --------------------------------------------------------------------------

/// `"func":\s*(\d+)`, the first match: the CALL payload's function id.
fn payload_func(text: &str) -> Option<i64> {
    let mut from = 0;
    while let Some(i) = text[from..].find("\"func\":") {
        let rest = text[from + i + 7..].trim_start_matches([' ', '\t', '\n', '\r', '\x0b', '\x0c']);
        let digits: String = rest.chars().take_while(|c| c.is_ascii_digit()).collect();
        if !digits.is_empty() {
            return digits.parse().ok();
        }
        from += i + 7;
    }
    None
}

pub fn analyse(r: &Store) -> Analysis {
    let t0 = std::time::Instant::now();
    let n = r.n_steps;

    let mut pid_func: HashMap<i64, i64> = HashMap::new();
    if let Ok(mut st) = r.t.db.prepare("SELECT id, json FROM payloads") {
        if let Ok(rows) = st.query_map([], |row| Ok((row.get::<_, i64>(0)?, row.get::<_, Option<String>>(1)?))) {
            for (id, text) in rows.flatten() {
                if let Some(f) = text.as_deref().and_then(payload_func) {
                    pid_func.insert(id, f);
                }
            }
        }
    }
    let fname: HashMap<i64, String> = r.funcs.iter()
        .map(|(id, f)| (*id, f.label().map(|s| s.to_string()).unwrap_or_else(|| "None".into())))
        .collect();

    // Function names are interned to small ints for the hot loop.
    let mut ids: HashMap<String, usize> = HashMap::new();
    let mut names: Vec<String> = Vec::new();
    fn intern(s: &str, ids: &mut HashMap<String, usize>, names: &mut Vec<String>) -> usize {
        if let Some(&i) = ids.get(s) {
            return i;
        }
        ids.insert(s.to_string(), names.len());
        names.push(s.to_string());
        names.len() - 1
    }
    let module = intern("<module>", &mut ids, &mut names);
    let unknown = intern("?", &mut ids, &mut names);
    // CALL payload id -> interned function, resolved once per payload.
    let mut pid_fn: HashMap<u32, usize> = HashMap::new();

    #[derive(Default, Clone)]
    struct Acc {
        calls: u64,
        returns: u64,
        raises: u64,
        self_steps: u64,
        total_steps: u64,
        self_ns: u64,
        first_step: u64,
        last_step: u64,
        callers: Om<usize, u64>,
        callees: Om<usize, u64>,
        writes: Om<String, u64>,
        lines: std::collections::HashSet<u32>,
        activity: Option<Vec<u64>>,
        invocations: Vec<(u64, u64)>,
    }
    let mut acc: Vec<Acc> = Vec::new();
    // Insertion orders of the Python Counters/defaultdicts, by function.
    let mut calls_order: Vec<usize> = Vec::new();
    let mut self_order: Vec<usize> = Vec::new();
    let mut activity_order: Vec<usize> = Vec::new();
    let mut inv_order: Vec<usize> = Vec::new();

    struct VarAcc {
        v: Var,
        by_loc: Om<u32, u64>,
    }
    let mut vars: Om<String, VarAcc> = Om::new();

    let mut frame_fn: HashMap<u32, usize> = HashMap::new();
    let mut entered: HashMap<u32, (u64, u64)> = HashMap::new();
    let mut stack: Vec<u32> = Vec::new();
    let mut last_line: Option<(usize, u64)> = None;
    let nb = n.max(1);
    let log = r.log();

    macro_rules! a {
        ($i:expr) => {{
            let i = $i;
            if acc.len() <= i {
                acc.resize(i + 1, Acc::default());
            }
            &mut acc[i]
        }};
    }

    for i in 0..n as usize {
        let step = i as u64 + 1;
        let rec = schema::unpack(&log[i * RECORD_SIZE..(i + 1) * RECORD_SIZE]);
        let here = match stack.last() {
            Some(top) => frame_fn.get(top).copied().unwrap_or(module),
            None => module,
        };
        match rec.kind {
            schema::LINE => {
                if let Some((f, t)) = last_line {
                    if rec.t > t {
                        a!(f).self_ns += rec.t - t;
                    }
                }
                last_line = Some((here, rec.t));
                let ah = a!(here);
                if ah.self_steps == 0 && !self_order.contains(&here) {
                    self_order.push(here);
                }
                ah.self_steps += 1;
                ah.lines.insert(rec.loc);
                if ah.activity.is_none() {
                    ah.activity = Some(vec![0; BUCKETS]);
                    activity_order.push(here);
                }
                let b = (step as usize * BUCKETS / nb as usize) - (step == n) as usize;
                ah.activity.as_mut().unwrap()[b] += 1;
            }
            schema::BIND | schema::MUTATE | schema::DELETE => {
                let Some(name) = r.names.get(&rec.name) else { continue };
                let va = vars.entry_or(name.clone(), || VarAcc { v: Var::default(), by_loc: Om::new() });
                va.v.writes += 1;
                match rec.kind {
                    schema::BIND => va.v.binds += 1,
                    schema::MUTATE => va.v.mutates += 1,
                    _ => va.v.deletes += 1,
                }
                *va.v.by_func.entry_or(names[here].clone(), || 0) += 1;
                *va.by_loc.entry_or(rec.loc, || 0) += 1;
                if va.v.first == 0 {
                    va.v.first = step;
                }
                va.v.last = step;
                *a!(here).writes.entry_or(name.clone(), || 0) += 1;
            }
            schema::CALL => {
                let f = match pid_fn.get(&rec.payload) {
                    Some(&f) => f,
                    None => {
                        let f = match pid_func.get(&(rec.payload as i64)).and_then(|f| fname.get(f)) {
                            Some(s) => intern(s, &mut ids, &mut names),
                            None => unknown,
                        };
                        pid_fn.insert(rec.payload, f);
                        f
                    }
                };
                if a!(f).calls == 0 && !calls_order.contains(&f) {
                    calls_order.push(f);
                }
                a!(f).calls += 1;
                if let Some(top) = stack.last() {
                    let parent = frame_fn.get(top).copied().unwrap_or(module);
                    *a!(f).callers.entry_or(parent, || 0) += 1;
                    *a!(parent).callees.entry_or(f, || 0) += 1;
                }
                frame_fn.insert(rec.frame, f);
                entered.insert(rec.frame, (step, rec.t));
                stack.push(rec.frame);
                let af = a!(f);
                if af.first_step == 0 {
                    af.first_step = step;
                }
            }
            schema::RETURN | schema::UNWIND => {
                let f = frame_fn.remove(&rec.frame);
                if let Some(p) = stack.iter().position(|x| *x == rec.frame) {
                    stack.remove(p);
                }
                let (s0, _) = entered.remove(&rec.frame).unwrap_or((step, rec.t));
                if let Some(f) = f {
                    let af = a!(f);
                    af.total_steps += step - s0;
                    af.last_step = step;
                    if rec.kind == schema::UNWIND { af.raises += 1 } else { af.returns += 1 }
                    if af.invocations.len() < MAX_INVOCATIONS {
                        if af.invocations.is_empty() && !inv_order.contains(&f) {
                            inv_order.push(f);
                        }
                        af.invocations.push((s0, step));
                    }
                }
            }
            schema::YIELD => {
                if let Some(p) = stack.iter().position(|x| *x == rec.frame) {
                    stack.remove(p);
                }
            }
            schema::RESUME => {
                if !stack.contains(&rec.frame) {
                    stack.push(rec.frame);
                }
            }
            _ => {}
        }
    }
    acc.resize(names.len(), Acc::default());

    // `set(list(calls) + list(self_steps) + ["<module>"])` -- Python's set
    // order is a hash order, different in every process. First seen it is;
    // `follow_nd_order` lines it up with nd's cache when there is one.
    let mut order: Vec<usize> = Vec::new();
    for f in calls_order.iter().chain(self_order.iter()).chain(std::iter::once(&module)) {
        if !order.contains(f) {
            order.push(*f);
        }
    }
    let script = r.meta("script").map(|v| v.py_str());
    let mut functions = Om::new();
    for f in order {
        let fnm = &names[f];
        let meta = r.funcs.values().find(|row| row.label().map(|s| s.to_string()).unwrap_or_else(|| "None".into()) == *fnm);
        let a = &acc[f];
        let name_map = |m: &Om<usize, u64>| {
            let mut o = Om::new();
            for (k, v) in m.iter() {
                o.insert(names[*k].clone(), *v);
            }
            o
        };
        functions.insert(fnm.clone(), Func {
            name: fnm.clone(),
            file: match meta {
                Some(m) => m.file.and_then(|id| r.files.get(&id).cloned()),
                None => Some(script.clone().unwrap_or_default()),
            },
            line: meta.and_then(|m| m.first_line).unwrap_or(0),
            calls: a.calls,
            returns: a.returns,
            raises: a.raises,
            self_steps: a.self_steps,
            total_steps: a.total_steps,
            self_ms: a.self_ns as f64 / 1e6,
            lines: a.lines.len() as u64,
            first_step: a.first_step,
            last_step: a.last_step,
            callers: name_map(&a.callers),
            callees: name_map(&a.callees),
            writes: a.writes.clone(),
        });
    }

    let mut variables = Om::new();
    for (name, va) in vars.items {
        let mut v = va.v;
        for (loc, c) in va.by_loc.iter() {
            let (path, line) = r.where_(*loc as i64);
            let key = format!("{}:{}", basename(path.as_deref().unwrap_or("?")),
                              line.map(|l| l.to_string()).unwrap_or_else(|| "None".into()));
            *v.by_line.entry_or(key, || 0) += c;
        }
        variables.insert(name, v);
    }
    let mut activity = Om::new();
    for f in activity_order {
        activity.insert(names[f].clone(), acc[f].activity.clone().unwrap_or_default());
    }
    let mut invocations = Om::new();
    for f in inv_order {
        invocations.insert(names[f].clone(), acc[f].invocations.clone());
    }
    Analysis { functions, variables, activity, invocations, n_steps: n, seconds: t0.elapsed().as_secs_f64(), reads: None }
}

// --------------------------------------------------------------------------
// cache
// --------------------------------------------------------------------------

fn om_json(m: &Om<String, u64>) -> Json {
    Json::Obj(m.iter().map(|(k, v)| (k.clone(), Json::Int(*v as i64))).collect())
}

fn json_om(j: Option<&Json>) -> Om<String, u64> {
    let mut o = Om::new();
    for (k, v) in j.map(|j| j.items()).unwrap_or(&[]) {
        o.insert(k.clone(), v.as_i64().unwrap_or(0).max(0) as u64);
    }
    o
}

fn to_json(a: &Analysis) -> Json {
    let int = |v: u64| Json::Int(v as i64);
    let functions = a.functions.iter().map(|(k, f)| (k.clone(), Json::Obj(vec![
        ("name".into(), Json::Str(f.name.clone())),
        ("file".into(), f.file.clone().map(Json::Str).unwrap_or(Json::Null)),
        ("line".into(), Json::Int(f.line)),
        ("calls".into(), int(f.calls)),
        ("returns".into(), int(f.returns)),
        ("raises".into(), int(f.raises)),
        ("self_steps".into(), int(f.self_steps)),
        ("total_steps".into(), int(f.total_steps)),
        ("self_ms".into(), Json::Float(f.self_ms)),
        ("lines".into(), int(f.lines)),
        ("first_step".into(), int(f.first_step)),
        ("last_step".into(), int(f.last_step)),
        ("callers".into(), om_json(&f.callers)),
        ("callees".into(), om_json(&f.callees)),
        ("writes".into(), om_json(&f.writes)),
    ]))).collect();
    let variables = a.variables.iter().map(|(k, v)| (k.clone(), Json::Obj(vec![
        ("writes".into(), int(v.writes)),
        ("binds".into(), int(v.binds)),
        ("mutates".into(), int(v.mutates)),
        ("deletes".into(), int(v.deletes)),
        ("first".into(), int(v.first)),
        ("last".into(), int(v.last)),
        ("by_func".into(), om_json(&v.by_func)),
        ("by_line".into(), om_json(&v.by_line)),
    ]))).collect();
    let activity = a.activity.iter().map(|(k, v)| (k.clone(), Json::Arr(v.iter().map(|x| int(*x)).collect()))).collect();
    let invocations = a.invocations.iter()
        .map(|(k, v)| (k.clone(), Json::Arr(v.iter().map(|(x, y)| Json::Arr(vec![int(*x), int(*y)])).collect())))
        .collect();
    Json::Obj(vec![
        ("version".into(), Json::Int(CACHE_VERSION)),
        ("n_steps".into(), int(a.n_steps)),
        ("seconds".into(), Json::Float(a.seconds)),
        ("functions".into(), Json::Obj(functions)),
        ("variables".into(), Json::Obj(variables)),
        ("activity".into(), Json::Obj(activity)),
        ("invocations".into(), Json::Obj(invocations)),
    ])
}

fn from_json(d: &Json) -> Analysis {
    let u = |j: &Json, k: &str| j.get(k).and_then(|v| v.as_i64()).unwrap_or(0).max(0) as u64;
    let mut functions = Om::new();
    for (k, f) in d.get("functions").map(|v| v.items()).unwrap_or(&[]) {
        functions.insert(k.clone(), Func {
            name: f.get("name").map(|v| v.py_str()).unwrap_or_default(),
            file: f.get("file").and_then(|v| v.as_str()).map(|s| s.to_string()),
            line: f.get("line").and_then(|v| v.as_i64()).unwrap_or(0),
            calls: u(f, "calls"),
            returns: u(f, "returns"),
            raises: u(f, "raises"),
            self_steps: u(f, "self_steps"),
            total_steps: u(f, "total_steps"),
            self_ms: f.get("self_ms").and_then(|v| v.as_f64()).unwrap_or(0.0),
            lines: u(f, "lines"),
            first_step: u(f, "first_step"),
            last_step: u(f, "last_step"),
            callers: json_om(f.get("callers")),
            callees: json_om(f.get("callees")),
            writes: json_om(f.get("writes")),
        });
    }
    let mut variables = Om::new();
    for (k, v) in d.get("variables").map(|v| v.items()).unwrap_or(&[]) {
        variables.insert(k.clone(), Var {
            writes: u(v, "writes"),
            binds: u(v, "binds"),
            mutates: u(v, "mutates"),
            deletes: u(v, "deletes"),
            first: u(v, "first"),
            last: u(v, "last"),
            by_func: json_om(v.get("by_func")),
            by_line: json_om(v.get("by_line")),
        });
    }
    let mut activity = Om::new();
    for (k, v) in d.get("activity").map(|v| v.items()).unwrap_or(&[]) {
        activity.insert(k.clone(), v.arr().iter().map(|x| x.as_i64().unwrap_or(0).max(0) as u64).collect());
    }
    let mut invocations = Om::new();
    for (k, v) in d.get("invocations").map(|v| v.items()).unwrap_or(&[]) {
        invocations.insert(k.clone(), v.arr().iter().map(|p| {
            let p = p.arr();
            let g = |i: usize| p.get(i).and_then(|x| x.as_i64()).unwrap_or(0).max(0) as u64;
            (g(0), g(1))
        }).collect());
    }
    Analysis {
        functions, variables, activity, invocations,
        n_steps: u(d, "n_steps"),
        seconds: d.get("seconds").and_then(|v| v.as_f64()).unwrap_or(0.0),
        reads: None,
    }
}

/// Function ties print in dict order, and nd's dict order comes from a
/// Python set (a hash order that changes per process) frozen into its
/// `analysis.json`. When nd has analysed this trace, use its order, so the
/// two explorers print the same table. Read-only: nd's file is never written.
fn follow_nd_order(a: &mut Analysis, r: &Store) {
    let Ok(text) = std::fs::read_to_string(r.dir().join("analysis.json")) else { return };
    let Some(d) = json::parse(&text) else { return };
    if d.get("version").and_then(|v| v.as_i64()) != Some(2) || d.get("n_steps").and_then(|v| v.as_i64()) != Some(a.n_steps as i64) {
        return;
    }
    let theirs: Vec<&String> = d.get("functions").map(|f| f.items().iter().map(|(k, _)| k).collect()).unwrap_or_default();
    let mut out = Om::new();
    let mut mine = std::mem::take(&mut a.functions);
    for k in theirs {
        if let Some(f) = mine.remove(k) {
            out.insert(k.clone(), f);
        }
    }
    for (k, f) in mine.items {
        out.insert(k, f);
    }
    a.functions = out;
}

/// The analysis for this trace, computed once and kept on disk.
pub fn load(r: &Store, use_cache: bool) -> Analysis {
    let path = r.dir().join(CACHE);
    let mut got = None;
    if use_cache {
        if let Ok(text) = std::fs::read_to_string(&path) {
            if let Some(d) = json::parse(&text) {
                if d.get("version").and_then(|v| v.as_i64()) == Some(CACHE_VERSION)
                    && d.get("n_steps").and_then(|v| v.as_i64()) == Some(r.n_steps as i64) {
                    got = Some(from_json(&d));
                }
            }
        }
    }
    let mut a = match got {
        Some(a) => a,
        None => {
            let a = analyse(r);
            if use_cache {
                let mut s = String::new();
                to_json(&a).dump(&mut s);
                let _ = std::fs::write(&path, s); // a read-only folder just means no cache
            }
            a
        }
    };
    follow_nd_order(&mut a, r);
    a
}

// --------------------------------------------------------------------------
// static side: which function does a line belong to, and what does it read
// --------------------------------------------------------------------------

/// A node of the AST as CPython shapes it, for a breadth-first walk in
/// `ast.iter_child_nodes` order. Leaves that can hold no name (operators,
/// contexts, constants, aliases) are left out: dropping a leaf does not
/// change the order in which the other nodes are visited.
enum N<'a> {
    S(&'a Stmt),
    E(&'a Expr),
    Args(&'a ast::Arguments),
    Arg(&'a ast::Arg),
    Kw(&'a ast::Keyword),
    Comp(&'a ast::Comprehension),
    Handler(&'a ast::ExceptHandler),
    Item(&'a ast::WithItem),
    Case(&'a ast::MatchCase),
    Pat(&'a Pattern),
    Tp(&'a ast::TypeParam),
}

fn push_stmts<'a>(q: &mut VecDeque<N<'a>>, b: &'a [Stmt]) {
    q.extend(b.iter().map(N::S));
}
fn push_exprs<'a>(q: &mut VecDeque<N<'a>>, b: &'a [Expr]) {
    q.extend(b.iter().map(N::E));
}
fn push_opt<'a>(q: &mut VecDeque<N<'a>>, e: &'a Option<Box<Expr>>) {
    if let Some(e) = e {
        q.push_back(N::E(e));
    }
}

fn children<'a>(n: &N<'a>, q: &mut VecDeque<N<'a>>) {
    match n {
        N::S(s) => match s {
            Stmt::FunctionDef(f) => {
                q.push_back(N::Args(&f.args));
                push_stmts(q, &f.body);
                push_exprs(q, &f.decorator_list);
                push_opt(q, &f.returns);
                q.extend(f.type_params.iter().map(N::Tp));
            }
            Stmt::AsyncFunctionDef(f) => {
                q.push_back(N::Args(&f.args));
                push_stmts(q, &f.body);
                push_exprs(q, &f.decorator_list);
                push_opt(q, &f.returns);
                q.extend(f.type_params.iter().map(N::Tp));
            }
            Stmt::ClassDef(c) => {
                push_exprs(q, &c.bases);
                q.extend(c.keywords.iter().map(N::Kw));
                push_stmts(q, &c.body);
                push_exprs(q, &c.decorator_list);
                q.extend(c.type_params.iter().map(N::Tp));
            }
            Stmt::Return(x) => push_opt(q, &x.value),
            Stmt::Delete(x) => push_exprs(q, &x.targets),
            Stmt::Assign(x) => {
                push_exprs(q, &x.targets);
                q.push_back(N::E(&x.value));
            }
            Stmt::TypeAlias(x) => {
                q.push_back(N::E(&x.name));
                q.extend(x.type_params.iter().map(N::Tp));
                q.push_back(N::E(&x.value));
            }
            Stmt::AugAssign(x) => {
                q.push_back(N::E(&x.target));
                q.push_back(N::E(&x.value));
            }
            Stmt::AnnAssign(x) => {
                q.push_back(N::E(&x.target));
                q.push_back(N::E(&x.annotation));
                push_opt(q, &x.value);
            }
            Stmt::For(x) => {
                q.push_back(N::E(&x.target));
                q.push_back(N::E(&x.iter));
                push_stmts(q, &x.body);
                push_stmts(q, &x.orelse);
            }
            Stmt::AsyncFor(x) => {
                q.push_back(N::E(&x.target));
                q.push_back(N::E(&x.iter));
                push_stmts(q, &x.body);
                push_stmts(q, &x.orelse);
            }
            Stmt::While(x) => {
                q.push_back(N::E(&x.test));
                push_stmts(q, &x.body);
                push_stmts(q, &x.orelse);
            }
            Stmt::If(x) => {
                q.push_back(N::E(&x.test));
                push_stmts(q, &x.body);
                push_stmts(q, &x.orelse);
            }
            Stmt::With(x) => {
                q.extend(x.items.iter().map(N::Item));
                push_stmts(q, &x.body);
            }
            Stmt::AsyncWith(x) => {
                q.extend(x.items.iter().map(N::Item));
                push_stmts(q, &x.body);
            }
            Stmt::Match(x) => {
                q.push_back(N::E(&x.subject));
                q.extend(x.cases.iter().map(N::Case));
            }
            Stmt::Raise(x) => {
                push_opt(q, &x.exc);
                push_opt(q, &x.cause);
            }
            Stmt::Try(x) => {
                push_stmts(q, &x.body);
                q.extend(x.handlers.iter().map(N::Handler));
                push_stmts(q, &x.orelse);
                push_stmts(q, &x.finalbody);
            }
            Stmt::TryStar(x) => {
                push_stmts(q, &x.body);
                q.extend(x.handlers.iter().map(N::Handler));
                push_stmts(q, &x.orelse);
                push_stmts(q, &x.finalbody);
            }
            Stmt::Assert(x) => {
                q.push_back(N::E(&x.test));
                push_opt(q, &x.msg);
            }
            Stmt::Expr(x) => q.push_back(N::E(&x.value)),
            _ => {}
        },
        N::E(e) => match e {
            Expr::BoolOp(x) => push_exprs(q, &x.values),
            Expr::NamedExpr(x) => {
                q.push_back(N::E(&x.target));
                q.push_back(N::E(&x.value));
            }
            Expr::BinOp(x) => {
                q.push_back(N::E(&x.left));
                q.push_back(N::E(&x.right));
            }
            Expr::UnaryOp(x) => q.push_back(N::E(&x.operand)),
            Expr::Lambda(x) => {
                q.push_back(N::Args(&x.args));
                q.push_back(N::E(&x.body));
            }
            Expr::IfExp(x) => {
                q.push_back(N::E(&x.test));
                q.push_back(N::E(&x.body));
                q.push_back(N::E(&x.orelse));
            }
            Expr::Dict(x) => {
                q.extend(x.keys.iter().flatten().map(N::E));
                push_exprs(q, &x.values);
            }
            Expr::Set(x) => push_exprs(q, &x.elts),
            Expr::ListComp(x) => {
                q.push_back(N::E(&x.elt));
                q.extend(x.generators.iter().map(N::Comp));
            }
            Expr::SetComp(x) => {
                q.push_back(N::E(&x.elt));
                q.extend(x.generators.iter().map(N::Comp));
            }
            Expr::GeneratorExp(x) => {
                q.push_back(N::E(&x.elt));
                q.extend(x.generators.iter().map(N::Comp));
            }
            Expr::DictComp(x) => {
                q.push_back(N::E(&x.key));
                q.push_back(N::E(&x.value));
                q.extend(x.generators.iter().map(N::Comp));
            }
            Expr::Await(x) => q.push_back(N::E(&x.value)),
            Expr::Yield(x) => push_opt(q, &x.value),
            Expr::YieldFrom(x) => q.push_back(N::E(&x.value)),
            Expr::Compare(x) => {
                q.push_back(N::E(&x.left));
                push_exprs(q, &x.comparators);
            }
            Expr::Call(x) => {
                q.push_back(N::E(&x.func));
                push_exprs(q, &x.args);
                q.extend(x.keywords.iter().map(N::Kw));
            }
            Expr::FormattedValue(x) => {
                q.push_back(N::E(&x.value));
                push_opt(q, &x.format_spec);
            }
            Expr::JoinedStr(x) => push_exprs(q, &x.values),
            Expr::Attribute(x) => q.push_back(N::E(&x.value)),
            Expr::Subscript(x) => {
                q.push_back(N::E(&x.value));
                q.push_back(N::E(&x.slice));
            }
            Expr::Starred(x) => q.push_back(N::E(&x.value)),
            Expr::List(x) => push_exprs(q, &x.elts),
            Expr::Tuple(x) => push_exprs(q, &x.elts),
            Expr::Slice(x) => {
                push_opt(q, &x.lower);
                push_opt(q, &x.upper);
                push_opt(q, &x.step);
            }
            Expr::Constant(_) | Expr::Name(_) => {}
        },
        N::Args(a) => {
            // CPython: posonlyargs, args, vararg, kwonlyargs, kw_defaults,
            // kwarg, defaults -- the defaults are separate lists there.
            q.extend(a.posonlyargs.iter().map(|x| N::Arg(&x.def)));
            q.extend(a.args.iter().map(|x| N::Arg(&x.def)));
            if let Some(v) = &a.vararg {
                q.push_back(N::Arg(v));
            }
            q.extend(a.kwonlyargs.iter().map(|x| N::Arg(&x.def)));
            q.extend(a.kwonlyargs.iter().filter_map(|x| x.default.as_deref()).map(N::E));
            if let Some(v) = &a.kwarg {
                q.push_back(N::Arg(v));
            }
            q.extend(a.posonlyargs.iter().chain(a.args.iter()).filter_map(|x| x.default.as_deref()).map(N::E));
        }
        N::Arg(a) => push_opt(q, &a.annotation),
        N::Kw(k) => q.push_back(N::E(&k.value)),
        N::Comp(c) => {
            q.push_back(N::E(&c.target));
            q.push_back(N::E(&c.iter));
            push_exprs(q, &c.ifs);
        }
        N::Handler(ast::ExceptHandler::ExceptHandler(h)) => {
            push_opt(q, &h.type_);
            push_stmts(q, &h.body);
        }
        N::Item(w) => {
            q.push_back(N::E(&w.context_expr));
            push_opt(q, &w.optional_vars);
        }
        N::Case(c) => {
            q.push_back(N::Pat(&c.pattern));
            push_opt(q, &c.guard);
            push_stmts(q, &c.body);
        }
        N::Pat(p) => match p {
            Pattern::MatchValue(x) => q.push_back(N::E(&x.value)),
            Pattern::MatchSequence(x) => q.extend(x.patterns.iter().map(N::Pat)),
            Pattern::MatchMapping(x) => {
                push_exprs(q, &x.keys);
                q.extend(x.patterns.iter().map(N::Pat));
            }
            Pattern::MatchClass(x) => {
                q.push_back(N::E(&x.cls));
                q.extend(x.patterns.iter().map(N::Pat));
                q.extend(x.kwd_patterns.iter().map(N::Pat));
            }
            Pattern::MatchAs(x) => {
                if let Some(p) = &x.pattern {
                    q.push_back(N::Pat(p));
                }
            }
            Pattern::MatchOr(x) => q.extend(x.patterns.iter().map(N::Pat)),
            Pattern::MatchSingleton(_) | Pattern::MatchStar(_) => {}
        },
        N::Tp(t) => {
            if let ast::TypeParam::TypeVar(v) = t {
                push_opt(q, &v.bound);
            }
        }
    }
}

/// Line numbers as CPython's tokenizer counts them: \n, \r\n and \r.
struct Lines(Vec<usize>);

impl Lines {
    fn new(src: &str) -> Lines {
        let b = src.as_bytes();
        let mut starts = vec![0];
        let mut i = 0;
        while i < b.len() {
            if b[i] == b'\n' || (b[i] == b'\r' && b.get(i + 1) != Some(&b'\n')) {
                starts.push(i + 1);
            }
            i += 1;
        }
        Lines(starts)
    }
    fn at(&self, off: usize) -> i64 {
        (self.0.partition_point(|&s| s <= off)) as i64
    }
}

type Scopes = Vec<(String, i64, i64)>;

/// `_scopes` and `_reads` in one walk: function ranges (innermost first)
/// and `{line: Counter(names loaded on it)}`, both in `ast.walk` order.
fn walk(source: &str, path: &str) -> Option<(Scopes, Om<i64, Om<String, u64>>)> {
    use rustpython_parser::Parse;
    let suite = ast::Suite::parse(source, path).ok()?;
    let lines = Lines::new(source);
    let start = |r: rustpython_parser::text_size::TextRange| lines.at(u32::from(r.start()) as usize);
    let end = |r: rustpython_parser::text_size::TextRange| lines.at((u32::from(r.end()) as usize).saturating_sub(1));
    let mut scopes = Vec::new();
    let mut reads: Om<i64, Om<String, u64>> = Om::new();
    let mut q: VecDeque<N> = suite.iter().map(N::S).collect();
    while let Some(n) = q.pop_front() {
        children(&n, &mut q);
        match n {
            N::S(Stmt::FunctionDef(f)) => scopes.push((f.name.to_string(), start(f.range), end(f.range))),
            N::S(Stmt::AsyncFunctionDef(f)) => scopes.push((f.name.to_string(), start(f.range), end(f.range))),
            N::E(Expr::Name(x)) if x.ctx == ExprContext::Load => {
                *reads.entry_or(start(x.range), Om::new).entry_or(x.id.to_string(), || 0) += 1;
            }
            N::E(Expr::Attribute(x)) if x.ctx == ExprContext::Load => {
                if let Expr::Name(v) = &*x.value {
                    *reads.entry_or(start(x.range), Om::new).entry_or(v.id.to_string(), || 0) += 1;
                }
            }
            _ => {}
        }
    }
    scopes.sort_by_key(|s| s.2 - s.1); // smallest range first = innermost
    Some((scopes, reads))
}

/// `{function name: Counter(variable -> times read in its body)}`.
pub fn static_reads(r: &Store) -> Reads {
    let mut out: Reads = Om::new();
    for (_, path) in r.files.iter() {
        let source = r.source(path);
        if source.is_empty() {
            continue;
        }
        let Some((scopes, reads)) = walk(&source, path) else { continue };
        for (line, names) in reads.iter() {
            let owner = scopes.iter().find(|(_, lo, hi)| lo <= line && line <= hi)
                .map(|s| s.0.clone()).unwrap_or_else(|| "<module>".into());
            for (var, n) in names.iter() {
                if !BUILTINS.contains(&var.as_str()) {
                    *out.entry_or(owner.clone(), Om::new).entry_or(var.clone(), || 0) += n;
                }
            }
        }
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    fn reads_of(src: &str) -> Vec<(i64, Vec<(String, u64)>)> {
        let (_, r) = walk(src, "<t>").unwrap();
        r.iter().map(|(l, c)| (*l, c.iter().map(|(k, v)| (k.clone(), *v)).collect())).collect()
    }

    #[test]
    fn loads_not_stores_and_attributes_count_their_root_twice() {
        // Breadth first: line 3's `y` sits one level above line 2's names.
        let got = reads_of("x = 1\ny = x + z\nobj.attr = y\nprint(obj.attr)\nx += 1\n");
        let want: Vec<(i64, Vec<(String, u64)>)> = vec![
            (3, vec![("y".into(), 1), ("obj".into(), 1)]),
            (2, vec![("x".into(), 1), ("z".into(), 1)]),
            (4, vec![("print".into(), 1), ("obj".into(), 2)]),
        ];
        assert_eq!(got, want);
    }

    #[test]
    fn breadth_first_order_decides_line_order() {
        // Line 3's name is shallower than line 2's, so ast.walk meets it first.
        let got = reads_of("if a:\n    f(g(h))\nb\n");
        let lines: Vec<i64> = got.iter().map(|(l, _)| *l).collect();
        assert_eq!(lines, vec![1, 3, 2]);
    }

    #[test]
    fn scopes_are_innermost_first_and_start_at_def() {
        let (scopes, _) = walk("@deco\ndef outer():\n    def inner():\n        return 1\n    return inner\n", "<t>").unwrap();
        assert_eq!(scopes, vec![("inner".to_string(), 3, 4), ("outer".to_string(), 2, 5)]);
    }

    #[test]
    fn payload_regex() {
        assert_eq!(payload_func(r#"{"func":12,"args":{}}"#), Some(12));
        assert_eq!(payload_func(r#"{"func": null,"args":{"func":[1,3,0.0]}}"#), None);
        assert_eq!(payload_func(r#"{"x":1, "func":  7}"#), Some(7));
    }
}
