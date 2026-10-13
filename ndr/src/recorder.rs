//! The recorder: PEP 669 (`sys.monitoring`) callbacks written as raw C
//! functions in Rust.
//!
//! Why raw `METH_FASTCALL` functions and not `#[pyfunction]`: these run on
//! every line of the traced program. A raw C callable costs CPython ~22 ns to
//! dispatch; anything layered on top is paid a million times over.
//!
//! The logic mirrors `nd36/recorder.py` event for event, so the two produce
//! traces `ndr verify` can compare. Where it deliberately differs it says so.

use crate::encode::{Bloom, Cheap, Encoder};
use crate::py::{self, Owned};
use crate::schema::{self, Vref};
use crate::writer::{json_str, Writer};
use pyo3::ffi;
use rustc_hash::FxHashMap as HashMap;
use std::cell::RefCell;
use std::ffi::c_int;
use std::fmt::Write as _;
use std::time::Instant;

const CO_OPTIMIZED: c_int = 0x0001;

/// What we learn about a code object, once.
struct CodeInfo {
    in_scope: bool,
    file: u32,
    func: u32,
    first_line: i32,
    first_loc: u32,
    optimized: bool, // function frame: fast locals
    is_module: bool,
    /// Positional + keyword-only parameter names, for the CALL payload.
    args: Vec<(Owned, u32)>,
    /// Every local name (co_varnames + co_cellvars): what diff() checks.
    vars: Vec<(Owned, u32)>,
    /// offset/2 -> line, for spotting same-line backward jumps.
    offset_line: Vec<i32>,
    /// What a stretch of a line can do, read off the bytecode once and keyed
    /// by the instruction the line was ENTERED at -- because a line is not
    /// always run whole. `for x in items:` runs `items` + GET_ITER once, then
    /// only FOR_ITER + STORE x on every later pass, so `items` is named on
    /// the first pass and not on the rest.
    ///   may:  can this stretch change the CONTENTS of something? A stretch
    ///         that only reads (`if grid[r][i] == v`) cannot, so nothing on
    ///         it is re-hashed -- the biggest single saving.
    ///   refs: the variables it names. On a stretch that may mutate, only
    ///         these are re-hashed every time; the rest are re-checked on a
    ///         back-off schedule (see `check`).
    entry_of: Vec<u32>, // code unit -> index into `entries`
    entries: Vec<Entry>,
    /// Per variable of this code, shared by every call of it: the back-off
    /// for when it is NOT named. A generator that lives for four steps
    /// still benefits from what the last thousand instances learned.
    backoff: RefCell<HashMap<u32, (u8, u8)>>,
    _code: Owned, // keeps the address from being reused for another code object
}

struct Entry {
    may: bool,
    refs: Vec<u32>,
}

/// Used when we cannot tell where a line was entered: assume the worst.
static ALL: Entry = Entry { may: true, refs: Vec::new() };

struct Slot {
    cheap: Cheap,
    hash: u64,
    vref: Vref,
    seen: u32, // generation stamp, for spotting deleted module globals
    inside: Bloom, // mutable objects inside it, as of its last hash
}

/// How long an unreferenced container may go un-hashed, in lines that could
/// have mutated it. A change through an alias is reported at most this late.
const MAX_WAIT: u8 = 32;

enum Seen {
    Same,
    Skipped,
    Changed(Option<usize>), // Some(address) if it was a MUTATE
}

struct FrameRec {
    fid: u32,
    code: usize,
    func: u32,
    is_module: bool,
    prev_loc: u32,
    prev_line: i32,
    prev_start: u32, // code unit the previous line was entered at
    prev_t: u64,
    slots: HashMap<u32, Slot>, // name id -> what it held last line
    gen: u32,
}

pub struct Recorder {
    pub w: Writer,
    enc: Encoder,
    scope: Vec<String>,
    codes: HashMap<usize, Box<CodeInfo>>,
    frames: HashMap<usize, FrameRec>,
    stack: Vec<usize>,
    loc_cache: HashMap<(usize, i32), u32>,
    key_ids: HashMap<usize, (u32, Owned)>, // dict key str -> name id (key kept alive)
    next_fid: u32,
    t0: Instant,
    disable: Owned,
    pub callbacks: u64,
    /// opcode -> can it mutate? Built from `dis.opmap`, so it is right for
    /// whichever CPython this is (opcode numbers change between versions).
    mut_ops: [bool; 256],
    binary_op: u8,
    extended_arg: u8,
    /// opcode -> how its oparg names a variable: 0 no, 1 fast/cell index,
    /// 2 two packed fast indices, 3 co_names index, 4 co_names index << 1.
    var_ops: [u8; 256],
    refs_known: bool,
    op_cache: u8,
    op_for_iter: u8,
    op_get_iter: u8,
    op_load_const: u8,
}

static mut REC: *mut Recorder = std::ptr::null_mut();

#[inline(always)]
unsafe fn rec() -> Option<&'static mut Recorder> {
    let p = REC;
    if p.is_null() { None } else { Some(&mut *p) }
}

impl Recorder {
    #[inline(always)]
    fn now(&self) -> u64 {
        self.t0.elapsed().as_nanos() as u64
    }

    /// Learn about a code object the first time we see it.
    unsafe fn code_info(&mut self, code: *mut ffi::PyObject) -> &CodeInfo {
        let key = code as usize;
        if !self.codes.contains_key(&key) {
            let info = self.learn(code);
            self.codes.insert(key, Box::new(info));
        }
        &self.codes[&key]
    }

    unsafe fn learn(&mut self, code: *mut ffi::PyObject) -> CodeInfo {
        let filename = py::getattr(code, c"co_filename")
            .and_then(|f| py::to_string(f.as_ptr()))
            .unwrap_or_default();
        let in_scope = self.scope.iter().any(|s| *s == filename);
        let owned = Owned::from_borrowed(code);
        if !in_scope {
            return CodeInfo {
                in_scope, file: 0, func: 0, first_line: 0, first_loc: 0,
                optimized: false, is_module: false, args: vec![], vars: vec![],
                offset_line: vec![], entry_of: vec![], entries: vec![],
                backoff: RefCell::new(HashMap::default()), _code: owned,
            };
        }
        let name = py::getattr(code, c"co_name").and_then(|n| py::to_string(n.as_ptr())).unwrap_or_default();
        let qual = py::getattr(code, c"co_qualname").and_then(|n| py::to_string(n.as_ptr())).unwrap_or_else(|| name.clone());
        let first_line = py::getattr(code, c"co_firstlineno").map(|n| ffi::PyLong_AsLong(n.as_ptr()) as i32).unwrap_or(0);
        let flags = py::getattr(code, c"co_flags").map(|n| ffi::PyLong_AsLong(n.as_ptr()) as c_int).unwrap_or(0);
        let argc = py::getattr(code, c"co_argcount").map(|n| ffi::PyLong_AsLong(n.as_ptr())).unwrap_or(0)
            + py::getattr(code, c"co_kwonlyargcount").map(|n| ffi::PyLong_AsLong(n.as_ptr())).unwrap_or(0);
        ffi::PyErr_Clear();

        let file = self.w.file_id(&filename);
        let func = self.w.func_id(&name, &qual, file, first_line);
        let first_loc = self.w.loc_id(file, first_line);
        let optimized = flags & CO_OPTIMIZED != 0;

        let mut vars = Vec::new();
        let mut args = Vec::new();
        for attr in [c"co_varnames", c"co_cellvars"] {
            if let Some(t) = py::getattr(code, attr) {
                let n = ffi::PyTuple_Size(t.as_ptr());
                for i in 0..n {
                    let s = ffi::PyTuple_GetItem(t.as_ptr(), i);
                    let text = py::to_string(s).unwrap_or_default();
                    // Inlined comprehension temporaries ("_", ".0") are not
                    // variables a person wrote.
                    if text.starts_with('.') {
                        continue;
                    }
                    let nid = self.w.name_id(&text);
                    if attr == c"co_varnames" && (i as i64) < argc as i64 {
                        args.push((Owned::from_borrowed(s), nid));
                    }
                    vars.push((Owned::from_borrowed(s), nid));
                }
            }
        }

        let mut offset_line = Vec::new();
        if let Some(lines) = py::call0(code, c"co_lines") {
            if let Some(it) = Owned::new(ffi::PyObject_GetIter(lines.as_ptr())) {
                loop {
                    let t = ffi::PyIter_Next(it.as_ptr());
                    if t.is_null() { ffi::PyErr_Clear(); break; }
                    let t = Owned(t);
                    let start = ffi::PyLong_AsLong(ffi::PyTuple_GetItem(t.as_ptr(), 0));
                    let end = ffi::PyLong_AsLong(ffi::PyTuple_GetItem(t.as_ptr(), 1));
                    let l = ffi::PyTuple_GetItem(t.as_ptr(), 2);
                    let line = if l == ffi::Py_None() { -1 } else { ffi::PyLong_AsLong(l) as i32 };
                    let (a, b) = ((start / 2) as usize, (end / 2) as usize);
                    if offset_line.len() < b { offset_line.resize(b, -1); }
                    for slot in &mut offset_line[a..b] { *slot = line; }
                }
            }
        }
        ffi::PyErr_Clear();

        // Pass 1: per code unit, can it mutate, and which names does it use.
        let mut unit_mut: Vec<bool> = Vec::new();
        let mut unit_refs: Vec<[u32; 2]> = Vec::new();
        let names = py::getattr(code, c"co_names");
        let varname = py::getattr(code, c"_varname_from_oparg");
        let mut fast_ids: HashMap<u32, u32> = HashMap::default();
        ffi::PyErr_Clear();
        if let Some(bytes) = py::getattr(code, c"co_code") {
            let n = ffi::PyBytes_Size(bytes.as_ptr()) as usize;
            let p = ffi::PyBytes_AsString(bytes.as_ptr()) as *const u8;
            if !p.is_null() {
                let b = std::slice::from_raw_parts(p, n);
                let mut ext: u32 = 0;
                let (mut last, mut before) = (255u8, 255u8); // previous two real opcodes
                for pair in b.chunks_exact(2) {
                    let (op, arg) = (pair[0], pair[1] as u32 | (ext << 8));
                    ext = if op == self.extended_arg { arg } else { 0 };
                    // BINARY_OP's in-place forms (`xs += [1]`) mutate; the
                    // plain ones (`a + b`) build something new.
                    let mut hit = self.mut_ops[op as usize] || (op == self.binary_op && arg >= 13);
                    // `for d in ((0, 1), (1, 0)):` iterates a constant: nothing
                    // can run but the tuple iterator.
                    if op == self.op_for_iter && last == self.op_get_iter && before == self.op_load_const {
                        hit = false;
                    }
                    let mut ids = [0u32; 2];
                    let mut fast = |i: u32| -> u32 {
                        if let Some(&id) = fast_ids.get(&i) { return id; }
                        let mut id = 0;
                        if let Some(f) = &varname {
                            if let Some(n) = Owned::new(ffi::PyLong_FromLong(i as _)) {
                                if let Some(s) = Owned::new(ffi::PyObject_CallOneArg(f.as_ptr(), n.as_ptr())) {
                                    id = py::to_string(s.as_ptr()).map(|t| self.w.name_id(&t)).unwrap_or(0);
                                }
                            }
                            ffi::PyErr_Clear();
                        }
                        fast_ids.insert(i, id);
                        id
                    };
                    match self.var_ops[op as usize] {
                        0 => {}
                        1 => ids[0] = fast(arg),
                        2 => { ids[0] = fast(arg >> 4); ids[1] = fast(arg & 15); }
                        how => {
                            let i = if how == 4 { arg >> 1 } else { arg };
                            if let Some(t) = &names {
                                let s = ffi::PyTuple_GetItem(t.as_ptr(), i as isize);
                                if s.is_null() { ffi::PyErr_Clear(); }
                                else if let Some(text) = py::to_string(s) { ids[0] = self.w.name_id(&text); }
                            }
                        }
                    }
                    unit_mut.push(hit);
                    unit_refs.push(ids);
                    if op != self.op_cache && op != self.extended_arg {
                        before = last;
                        last = op;
                    }
                }
            }
        }

        // Pass 2, backwards: what can run from each unit to the end of its
        // stretch of line. Consecutive units mostly share one entry.
        let nunits = unit_mut.len();
        let mut entry_of = vec![0u32; nunits];
        let mut entries = vec![Entry { may: false, refs: Vec::new() }];
        let (mut cur_line, mut cur_may, mut cur_refs, mut cur_idx) = (i32::MIN, false, Vec::<u32>::new(), 0u32);
        for u in (0..nunits).rev() {
            let line = offset_line.get(u).copied().unwrap_or(-1);
            if line != cur_line && line >= 0 {
                cur_line = line;
                cur_may = false;
                cur_refs.clear();
                cur_idx = 0;
            }
            let mut changed = false;
            if unit_mut[u] && !cur_may {
                cur_may = true;
                changed = true;
            }
            for id in unit_refs[u] {
                if id != 0 && !cur_refs.contains(&id) {
                    cur_refs.push(id);
                    changed = true;
                }
            }
            if changed {
                entries.push(Entry { may: cur_may, refs: cur_refs.clone() });
                cur_idx = entries.len() as u32 - 1;
            }
            entry_of[u] = cur_idx;
        }
        ffi::PyErr_Clear();

        CodeInfo {
            in_scope, file, func, first_line, first_loc, optimized,
            is_module: !optimized && name == "<module>",
            args, vars, offset_line, entry_of, entries,
            backoff: RefCell::new(HashMap::default()), _code: owned,
        }
    }

    // -- events ----------------------------------------------------------------

    unsafe fn push(&mut self, frame: *mut ffi::PyFrameObject, code: *mut ffi::PyObject) {
        let ci: *const CodeInfo = self.code_info(code);
        let ci = &*ci;
        self.enc.new_line();
        let fid = self.next_fid;
        self.next_fid += 1;
        let now = self.now();
        let mut rec = FrameRec {
            fid, code: code as usize, func: ci.func, is_module: ci.is_module,
            prev_loc: ci.first_loc, prev_line: ci.first_line, prev_start: 0, prev_t: now,
            slots: HashMap::default(), gen: 0,
        };

        // Parameters go in the CALL payload, not as BIND events -- same as
        // the Python recorder, so `af` can show each call's arguments.
        let mut payload = String::with_capacity(64);
        let _ = write!(payload, "{{\"func\":{},\"args\":{{", ci.func);
        if !ci.args.is_empty() {
            if let Some(locals) = Owned::new(py::PyFrame_GetLocals(frame)) {
                let step = self.w.step + 1;
                let mut first = true;
                for (name, nid) in &ci.args {
                    if let Some(v) = py::lookup(locals.as_ptr(), name.as_ptr()) {
                        let cheap = self.enc.cheap(v.as_ptr());
                        let (hash, inside) = if self.enc.mutable(v.as_ptr(), cheap) {
                            (self.enc.deep_hash(v.as_ptr()), self.enc.last_seen)
                        } else {
                            (0, Bloom::default())
                        };
                        let vref = self.enc.encode(&mut self.w, v.as_ptr(), step);
                        rec.slots.insert(*nid, Slot { cheap, hash, vref, seen: 0, inside });
                        if !first { payload.push(','); }
                        first = false;
                        payload.push_str(&json_str(self.w.name_text(*nid)));
                        payload.push(':');
                        vref.json(&mut payload);
                    }
                }
            }
        }
        let _ = write!(payload, "}},\"scope\":\"{}\"}}", if ci.is_module { "global" } else { "local" });
        let pid = self.w.payload_id(payload);
        self.w.event(now, schema::CALL, fid, ci.first_loc, 0, Vref::ABSENT, pid);
        self.frames.insert(frame as usize, rec);
        self.stack.push(frame as usize);
    }

    /// Emit BIND / MUTATE / DELETE for whatever changed since the last line.
    unsafe fn diff(&mut self, frame: *mut ffi::PyFrameObject) {
        let Some(f) = self.frames.get_mut(&(frame as usize)) else { return };
        let ci: &CodeInfo = &self.codes[&f.code];
        let Some(locals) = Owned::new(py::PyFrame_GetLocals(frame)) else { return };
        self.enc.new_line();
        let step = self.w.step + 1;
        let (t, loc, fid) = (f.prev_t, f.prev_loc, f.fid);
        let e: &Entry = match ci.entry_of.get(f.prev_start as usize) {
            Some(&i) => &ci.entries[i as usize],
            None => &ALL,
        };
        let may = e.may;
        let all = !self.refs_known || std::ptr::eq(e, &ALL);
        let refs: &[u32] = &e.refs;
        let named = |nid: u32| all || refs.contains(&nid);
        let bo = &ci.backoff;

        // Containers this line does not name are skipped on a back-off
        // schedule. If a named one changed IN PLACE, a skipped one may have
        // changed with it: the very same object under another name
        // (`b = a; a.append(1)`), or one that holds it (`solutions` holding
        // `board`, and `board[row] = col`). Those are re-checked now, not 32
        // lines later -- but only containers whose last hash actually walked
        // through the object that changed (see `Bloom`).
        let mut skipped: Vec<(u32, *mut ffi::PyObject)> = Vec::new();
        let mut moved: Vec<usize> = Vec::new();
        let mut note = |r: Seen, nid: u32, v: *mut ffi::PyObject, skipped: &mut Vec<(u32, *mut ffi::PyObject)>| match r {
            Seen::Skipped => skipped.push((nid, v)),
            Seen::Changed(Some(a)) => moved.push(a),
            _ => {}
        };

        if ci.optimized {
            let mut held: Vec<Owned> = Vec::new(); // keeps skipped values alive
            for (name, nid) in &ci.vars {
                match py::lookup(locals.as_ptr(), name.as_ptr()) {
                    Some(v) => {
                        let r = check(&mut self.w, &mut self.enc, f, *nid, v.as_ptr(), step, t, loc, fid, may, named(*nid), bo);
                        if matches!(r, Seen::Skipped) { held.push(Owned::from_borrowed(v.as_ptr())); }
                        note(r, *nid, v.as_ptr(), &mut skipped);
                    }
                    None => {
                        if f.slots.remove(nid).is_some() {
                            self.w.event(t, schema::DELETE, fid, loc, *nid, Vref::ABSENT, 0);
                        }
                    }
                }
            }
            if !moved.is_empty() {
                for (nid, v) in &skipped {
                    if affected(f, *nid, *v, &moved) {
                        check(&mut self.w, &mut self.enc, f, *nid, *v, step, t, loc, fid, may, true, bo);
                    }
                }
            }
            drop(held);
        } else if ffi::PyDict_Check(locals.as_ptr()) != 0 {
            // Module and class bodies: the namespace is a real dict.
            f.gen = f.gen.wrapping_add(1);
            let gen = f.gen;
            let d = locals.as_ptr();
            // Snapshot items: encoding can run user code (a __repr__), and a
            // dict that changes size while PyDict_Next walks it is UB.
            let Some(items) = Owned::new(ffi::PyDict_Items(d)) else { return };
            let n = ffi::PyList_Size(items.as_ptr());
            for i in 0..n {
                let pair = ffi::PyList_GetItem(items.as_ptr(), i);
                let k = ffi::PyTuple_GetItem(pair, 0);
                let v = ffi::PyTuple_GetItem(pair, 1);
                let nid = match self.key_ids.get(&(k as usize)) {
                    Some((id, _)) => *id,
                    None => {
                        let text = py::to_string(k).unwrap_or_default();
                        let id = if text.starts_with("__") && text.ends_with("__") { 0 } else { self.w.name_id(&text) };
                        self.key_ids.insert(k as usize, (id, Owned::from_borrowed(k)));
                        id
                    }
                };
                if nid == 0 {
                    continue;
                }
                let r = check(&mut self.w, &mut self.enc, f, nid, v, step, t, loc, fid, may, named(nid), bo);
                note(r, nid, v, &mut skipped);
                if let Some(s) = f.slots.get_mut(&nid) {
                    s.seen = gen;
                }
            }
            if !moved.is_empty() {
                for (nid, v) in &skipped { // `items` still holds every value
                    if affected(f, *nid, *v, &moved) {
                        check(&mut self.w, &mut self.enc, f, *nid, *v, step, t, loc, fid, may, true, bo);
                    }
                }
            }
            let gone: Vec<u32> = f.slots.iter().filter(|(_, s)| s.seen != gen).map(|(k, _)| *k).collect();
            for nid in gone {
                f.slots.remove(&nid);
                self.w.event(t, schema::DELETE, fid, loc, nid, Vref::ABSENT, 0);
            }
        }
    }

    unsafe fn line(&mut self, frame: *mut ffi::PyFrameObject, code: *mut ffi::PyObject, lineno: i32, start: u32) {
        if !self.frames.contains_key(&(frame as usize)) {
            self.push(frame, code);
        }
        self.diff(frame);
        let key = (code as usize, lineno);
        let loc = match self.loc_cache.get(&key) {
            Some(&l) => l,
            None => {
                let file = self.codes[&(code as usize)].file;
                let l = self.w.loc_id(file, lineno);
                self.loc_cache.insert(key, l);
                l
            }
        };
        let now = self.now();
        let f = self.frames.get_mut(&(frame as usize)).unwrap();
        let (prev_loc, prev_t, fid) = (f.prev_loc, f.prev_t, f.fid);
        f.prev_loc = loc;
        f.prev_line = lineno;
        f.prev_start = start;
        f.prev_t = now;
        self.w.note_line(loc, prev_loc, now.saturating_sub(prev_t));
        self.w.event(now, schema::LINE, fid, loc, 0, Vref::ABSENT, 0);
        if self.w.should_keyframe() {
            self.keyframe();
        }
    }

    unsafe fn pop(&mut self, frame: *mut ffi::PyFrameObject, retval: *mut ffi::PyObject, unwinding: bool) {
        if !self.frames.contains_key(&(frame as usize)) {
            return;
        }
        self.diff(frame);
        let f = self.frames.remove(&(frame as usize)).unwrap();
        if let Some(i) = self.stack.iter().rposition(|&p| p == frame as usize) {
            self.stack.remove(i);
        }
        let now = self.now();
        if unwinding {
            self.w.event(now, schema::UNWIND, f.fid, f.prev_loc, 0, Vref::ABSENT, 0);
        } else {
            let step = self.w.step + 1;
            let v = self.enc.encode(&mut self.w, retval, step);
            self.w.event(now, schema::RETURN, f.fid, f.prev_loc, 0, v, 0);
        }
    }

    unsafe fn yielded(&mut self, frame: *mut ffi::PyFrameObject, value: *mut ffi::PyObject) {
        let Some(f) = self.frames.get(&(frame as usize)) else { return };
        let (fid, loc) = (f.fid, f.prev_loc);
        self.diff(frame);
        let step = self.w.step + 1;
        let v = self.enc.encode(&mut self.w, value, step);
        let now = self.now();
        self.w.event(now, schema::YIELD, fid, loc, 0, v, 0);
    }

    unsafe fn raised(&mut self, frame: *mut ffi::PyFrameObject, code: *mut ffi::PyObject, exc: *mut ffi::PyObject) {
        let (fid, loc) = match self.frames.get(&(frame as usize)) {
            Some(f) => (f.fid, f.prev_loc),
            None => (0, self.code_info(code).first_loc),
        };
        let tname = py::type_name(ffi::Py_TYPE(exc));
        let nid = self.w.name_id(&tname);
        let msg = py::clip(&py::str_of(exc).unwrap_or_default(), 200);
        let pid = self.w.payload_id(format!("{{\"msg\":{}}}", json_str(&msg)));
        let now = self.now();
        self.w.event(now, schema::RAISE, fid, loc, nid, Vref::ABSENT, pid);
    }

    /// A full snapshot of every live frame, so seeking never replays from
    /// step 1. Unlike the Python recorder this records which FUNCTION each
    /// frame belongs to, so `nd x` names it correctly straight after a seek.
    pub fn keyframe(&mut self) {
        let mut s = String::with_capacity(256);
        s.push_str("{\"frames\":[");
        for (i, p) in self.stack.iter().enumerate() {
            let Some(f) = self.frames.get(p) else { continue };
            if i > 0 { s.push(','); }
            let _ = write!(s, "{{\"frame\":{},\"func\":{},\"scope\":\"{}\",\"loc\":{},\"locals\":{{",
                           f.fid, f.func, if f.is_module { "global" } else { "local" }, f.prev_loc);
            let mut first = true;
            for (nid, slot) in &f.slots {
                if !first { s.push(','); }
                first = false;
                s.push_str(&json_str(self.w.name_text(*nid)));
                s.push(':');
                slot.vref.json(&mut s);
            }
            s.push_str("}}");
        }
        s.push_str("],\"globals\":{}}");
        let step = self.w.step;
        self.w.keyframe(step, &s);
    }
}

/// Could a skipped container have changed along with what did?
#[inline]
fn affected(f: &FrameRec, nid: u32, v: *mut ffi::PyObject, moved: &[usize]) -> bool {
    let inside = f.slots.get(&nid).map(|s| s.inside).unwrap_or_default();
    moved.iter().any(|&a| a == v as usize || inside.has(a))
}

/// One variable, one line: has it changed, and how?
///
/// `named`: the stretch of line just run mentions this variable. A container
/// it does not mention can only have changed through an alias or a callee
/// holding a reference, so it is re-hashed on a doubling schedule (1, 2, 4 ..
/// 32 such lines) instead of every time, and the schedule resets the moment
/// it is caught changing. The schedule lives on the code object (`bo`), so
/// short-lived frames -- a generator per call -- share what was learned.
#[inline]
#[allow(clippy::too_many_arguments)]
unsafe fn check(w: &mut Writer, enc: &mut Encoder, f: &mut FrameRec, nid: u32,
                v: *mut ffi::PyObject, step: u64, t: u64, loc: u32, fid: u32,
                may: bool, named: bool, bo: &RefCell<HashMap<u32, (u8, u8)>>) -> Seen {
    let cheap = enc.cheap(v);
    let mutable = enc.mutable(v, cheap);
    let gen = f.gen;
    let prev = f.slots.get(&nid).map(|p| (p.cheap, p.hash, p.vref));
    let mut hash = 0;
    if let Some((pc, ph, _)) = prev {
        if pc == cheap {
            f.slots.get_mut(&nid).unwrap().seen = gen;
            if !mutable || !may {
                return Seen::Same; // same scalar, or same object we cannot see inside
            }
            if !named {
                let mut b = bo.borrow_mut();
                let (skip, _) = b.entry(nid).or_insert((0, 0));
                if *skip > 0 {
                    *skip -= 1;
                    return Seen::Skipped;
                }
            }
            hash = enc.deep_hash(v);
            if hash == ph {
                f.slots.get_mut(&nid).unwrap().inside = enc.last_seen;
                if !named {
                    let mut b = bo.borrow_mut();
                    let (skip, wait) = b.entry(nid).or_insert((0, 0));
                    *wait = (*wait).max(1).saturating_mul(2).min(MAX_WAIT);
                    *skip = *wait - 1;
                }
                return Seen::Same; // the common case: nothing moved
            }
            if !named {
                bo.borrow_mut().insert(nid, (0, 0)); // caught one: watch closely again
            }
        }
    }
    if hash == 0 && mutable {
        hash = enc.deep_hash(v);
    }
    let inside = if mutable { enc.last_seen } else { Bloom::default() };
    // Deliberate difference from the Python recorder: when two names point at
    // one list (`best = tour`) and it changes, BOTH get a MUTATE. Python only
    // reported whichever name it happened to check first -- which is exactly
    // the aliasing bug a person is trying to find.
    let vref = enc.encode(w, v, step);
    let was = prev.map(|p| p.2);
    let same_object = matches!(was, Some(w0) if w0.rk == schema::RK_OBJ && vref.rk == schema::RK_OBJ && w0.ri == vref.ri);
    let kind = if same_object { schema::MUTATE } else { schema::BIND };
    w.event(t, kind, fid, loc, nid, vref, 0);
    w.note_bind(nid);
    f.slots.insert(nid, Slot { cheap, hash, vref, seen: gen, inside });
    Seen::Changed(if same_object { Some(v as usize) } else { None })
}

// -- the C callbacks ------------------------------------------------------------
//
// Signature: METH_FASTCALL, i.e. (self, args, nargs). Each must:
//   * return a NEW reference (None, or DISABLE to switch that location off)
//   * never return NULL -- that would inject an exception into the program
//   * never leave an exception set

type Args = *mut *mut ffi::PyObject;

#[inline(always)]
unsafe fn disable() -> *mut ffi::PyObject {
    match rec() {
        Some(r) => { ffi::Py_IncRef(r.disable.as_ptr()); r.disable.as_ptr() }
        None => py::none(),
    }
}

/// Is this code ours? Out-of-scope code is switched off at that location
/// for good, which is why the standard library costs nothing after the
/// first hit.
#[inline(always)]
unsafe fn ours(r: &mut Recorder, code: *mut ffi::PyObject) -> bool {
    r.callbacks += 1;
    r.code_info(code).in_scope
}

unsafe extern "C" fn cb_start(_s: *mut ffi::PyObject, a: Args, _n: ffi::Py_ssize_t) -> *mut ffi::PyObject {
    let Some(r) = rec() else { return py::none() };
    let code = *a;
    if !ours(r, code) { return disable(); }
    let frame = ffi::PyEval_GetFrame();
    if !frame.is_null() { r.push(frame, code); }
    ffi::PyErr_Clear();
    py::none()
}

unsafe extern "C" fn cb_resume(_s: *mut ffi::PyObject, a: Args, _n: ffi::Py_ssize_t) -> *mut ffi::PyObject {
    let Some(r) = rec() else { return py::none() };
    let code = *a;
    if !ours(r, code) { return disable(); }
    let frame = ffi::PyEval_GetFrame();
    if !frame.is_null() && !r.frames.contains_key(&(frame as usize)) {
        r.push(frame, code);
    }
    ffi::PyErr_Clear();
    py::none()
}

unsafe extern "C" fn cb_line(_s: *mut ffi::PyObject, a: Args, _n: ffi::Py_ssize_t) -> *mut ffi::PyObject {
    let Some(r) = rec() else { return py::none() };
    let code = *a;
    if !ours(r, code) { return disable(); }
    let lineno = ffi::PyLong_AsLong(*a.add(1)) as i32;
    let frame = ffi::PyEval_GetFrame();
    if !frame.is_null() {
        // Where the line was entered: the instruction about to run.
        let start = (py::PyFrame_GetLasti(frame).max(0) / 2) as u32;
        r.line(frame, code, lineno, start);
    }
    ffi::PyErr_Clear();
    py::none()
}

/// LINE does not fire when a loop jumps back to the SAME line
/// (`while n < 3: n += 1`), so those iterations would vanish. A jump's
/// direction and target are fixed per instruction, so every jump that can
/// never be one of those is switched off on its first hit.
unsafe extern "C" fn cb_jump(_s: *mut ffi::PyObject, a: Args, _n: ffi::Py_ssize_t) -> *mut ffi::PyObject {
    let Some(r) = rec() else { return py::none() };
    let code = *a;
    if !ours(r, code) { return disable(); }
    let from = ffi::PyLong_AsLong(*a.add(1));
    let dest = ffi::PyLong_AsLong(*a.add(2));
    if dest >= from {
        return disable();
    }
    let (lf, ld) = {
        let ci = r.code_info(code);
        let at = |off: i64| ci.offset_line.get((off / 2) as usize).copied().unwrap_or(-1);
        (at(from as i64), at(dest as i64))
    };
    if lf < 0 || lf != ld {
        return disable();
    }
    let frame = ffi::PyEval_GetFrame();
    if !frame.is_null() { r.line(frame, code, ld, (dest / 2) as u32); }
    ffi::PyErr_Clear();
    py::none()
}

unsafe extern "C" fn cb_return(_s: *mut ffi::PyObject, a: Args, _n: ffi::Py_ssize_t) -> *mut ffi::PyObject {
    let Some(r) = rec() else { return py::none() };
    let code = *a;
    if !ours(r, code) { return disable(); }
    let frame = ffi::PyEval_GetFrame();
    if !frame.is_null() { r.pop(frame, *a.add(2), false); }
    ffi::PyErr_Clear();
    py::none()
}

unsafe extern "C" fn cb_yield(_s: *mut ffi::PyObject, a: Args, _n: ffi::Py_ssize_t) -> *mut ffi::PyObject {
    let Some(r) = rec() else { return py::none() };
    let code = *a;
    if !ours(r, code) { return disable(); }
    let frame = ffi::PyEval_GetFrame();
    if !frame.is_null() { r.yielded(frame, *a.add(2)); }
    ffi::PyErr_Clear();
    py::none()
}

// PY_UNWIND and RAISE are not "local" events: returning DISABLE from them is
// an error that tears the callback down. They always return None.
unsafe extern "C" fn cb_unwind(_s: *mut ffi::PyObject, a: Args, _n: ffi::Py_ssize_t) -> *mut ffi::PyObject {
    let Some(r) = rec() else { return py::none() };
    let code = *a;
    if ours(r, code) {
        let frame = ffi::PyEval_GetFrame();
        if !frame.is_null() { r.pop(frame, std::ptr::null_mut(), true); }
    }
    ffi::PyErr_Clear();
    py::none()
}

unsafe extern "C" fn cb_raise(_s: *mut ffi::PyObject, a: Args, _n: ffi::Py_ssize_t) -> *mut ffi::PyObject {
    let Some(r) = rec() else { return py::none() };
    let code = *a;
    if ours(r, code) {
        let frame = ffi::PyEval_GetFrame();
        if !frame.is_null() { r.raised(frame, code, *a.add(2)); }
    }
    ffi::PyErr_Clear();
    py::none()
}

// -- start / stop ----------------------------------------------------------------

const TOOL: i64 = 0; // sys.monitoring.DEBUGGER_ID

const EVENTS: [(&std::ffi::CStr, &std::ffi::CStr, ffi::PyCFunctionFast); 8] = [
    (c"PY_START", c"ndr_start", cb_start),
    (c"PY_RESUME", c"ndr_resume", cb_resume),
    (c"PY_RETURN", c"ndr_return", cb_return),
    (c"PY_YIELD", c"ndr_yield", cb_yield),
    (c"PY_UNWIND", c"ndr_unwind", cb_unwind),
    (c"LINE", c"ndr_line", cb_line),
    (c"JUMP", c"ndr_jump", cb_jump),
    (c"RAISE", c"ndr_raise", cb_raise),
];

/// Install the recorder. Everything Python-facing happens here, once.
pub unsafe fn start(w: Writer, scope: Vec<String>) -> Result<(), String> {
    let mon = Owned::new(ffi::PyImport_ImportModule(c"sys".as_ptr()))
        .and_then(|sys| py::getattr(sys.as_ptr(), c"monitoring"))
        .ok_or("this Python has no sys.monitoring (needs 3.12+)")?;
    let events = py::getattr(mon.as_ptr(), c"events").ok_or("sys.monitoring.events missing")?;
    let disable = py::getattr(mon.as_ptr(), c"DISABLE").ok_or("sys.monitoring.DISABLE missing")?;

    // Which opcodes can change the contents of an existing object. Looked up
    // by NAME in `dis.opmap`, because the numbers differ between 3.12, 3.13
    // and 3.14. Calls are on the list because a callee can mutate anything
    // it was handed (`swap(tour, i, j)`); iteration because a generator's
    // `next()` runs arbitrary code.
    let mut mut_ops = [false; 256];
    let (mut binary_op, mut extended_arg) = (255u8, 255u8);
    let mut var_ops = [0u8; 256];
    let mut refs_known = false; // if dis is missing: treat every name as referenced
    let (mut op_cache, mut op_for_iter, mut op_get_iter, mut op_load_const) = (0u8, 254u8, 254u8, 254u8);
    if let Some(opmap) = Owned::new(ffi::PyImport_ImportModule(c"dis".as_ptr())).and_then(|d| py::getattr(d.as_ptr(), c"opmap")) {
        let num = |name: &str| -> Option<u8> {
            let k = py::new_str(name)?;
            let v = ffi::PyDict_GetItemWithError(opmap.as_ptr(), k.as_ptr());
            if v.is_null() { ffi::PyErr_Clear(); return None; }
            Some(ffi::PyLong_AsLong(v) as u8)
        };
        for name in ["STORE_SUBSCR", "STORE_SLICE", "DELETE_SUBSCR", "STORE_ATTR", "DELETE_ATTR",
                     "CALL", "CALL_KW", "CALL_FUNCTION_EX", "BEFORE_WITH", "BEFORE_ASYNC_WITH",
                     "FOR_ITER", "SEND", "YIELD_VALUE", "IMPORT_NAME", "IMPORT_FROM",
                     "STORE_GLOBAL", "DELETE_GLOBAL", "STORE_DEREF", "DELETE_DEREF"] {
            if let Some(n) = num(name) { mut_ops[n as usize] = true; }
        }
        for (name, how) in [("LOAD_FAST", 1u8), ("LOAD_FAST_CHECK", 1), ("LOAD_FAST_AND_CLEAR", 1),
                            ("LOAD_FAST_BORROW", 1), ("STORE_FAST", 1), ("STORE_FAST_MAYBE_NULL", 1),
                            ("DELETE_FAST", 1), ("LOAD_DEREF", 1), ("STORE_DEREF", 1),
                            ("DELETE_DEREF", 1), ("LOAD_CLOSURE", 1), ("LOAD_FROM_DICT_OR_DEREF", 1),
                            ("LOAD_FAST_LOAD_FAST", 2), ("STORE_FAST_LOAD_FAST", 2),
                            ("STORE_FAST_STORE_FAST", 2), ("LOAD_FAST_BORROW_LOAD_FAST_BORROW", 2),
                            ("LOAD_NAME", 3), ("STORE_NAME", 3), ("DELETE_NAME", 3),
                            ("STORE_GLOBAL", 3), ("DELETE_GLOBAL", 3), ("LOAD_FROM_DICT_OR_GLOBALS", 3),
                            ("LOAD_GLOBAL", 4)] {
            if let Some(n) = num(name) { var_ops[n as usize] = how; }
        }
        refs_known = var_ops.iter().any(|&h| h == 1);
        binary_op = num("BINARY_OP").unwrap_or(255);
        op_cache = num("CACHE").unwrap_or(0);
        op_for_iter = num("FOR_ITER").unwrap_or(254);
        op_get_iter = num("GET_ITER").unwrap_or(254);
        op_load_const = num("LOAD_CONST").unwrap_or(254);
        extended_arg = num("EXTENDED_ARG").unwrap_or(255);
    } else {
        mut_ops = [true; 256]; // cannot tell: assume every line can mutate
    }
    ffi::PyErr_Clear();

    let r = Box::new(Recorder {
        w, enc: Encoder::new(), scope, codes: HashMap::default(), frames: HashMap::default(),
        stack: Vec::new(), loc_cache: HashMap::default(), key_ids: HashMap::default(),
        next_fid: 1, t0: Instant::now(), disable, callbacks: 0,
        mut_ops, binary_op, extended_arg, var_ops, refs_known,
        op_cache, op_for_iter, op_get_iter, op_load_const,
    });
    REC = Box::into_raw(r);

    let tool = Owned::new(ffi::PyLong_FromLongLong(TOOL)).ok_or("int")?;
    let call = |name: &std::ffi::CStr, args: &[*mut ffi::PyObject]| -> Result<Owned, String> {
        let f = py::getattr(mon.as_ptr(), name).ok_or_else(|| format!("sys.monitoring.{name:?} missing"))?;
        let tup = Owned::new(ffi::PyTuple_New(args.len() as ffi::Py_ssize_t)).ok_or("tuple")?;
        for (i, a) in args.iter().enumerate() {
            ffi::Py_IncRef(*a);
            ffi::PyTuple_SetItem(tup.as_ptr(), i as ffi::Py_ssize_t, *a);
        }
        let out = ffi::PyObject_Call(f.as_ptr(), tup.as_ptr(), std::ptr::null_mut());
        if out.is_null() {
            let msg = fetch_error();
            return Err(format!("sys.monitoring.{}: {msg}", name.to_string_lossy()));
        }
        Ok(Owned(out))
    };

    // Another tool (a debugger, coverage.py) may already hold the slot.
    let label = py::new_str("ndr").ok_or("str")?;
    call(c"use_tool_id", &[tool.as_ptr(), label.as_ptr()])
        .map_err(|e| format!("{e}\n(is a debugger or coverage tool already attached?)"))?;

    let mut mask: i64 = 0;
    for (ev, fname, f) in EVENTS {
        let bit = py::getattr(events.as_ptr(), ev).ok_or("event constant")?;
        mask |= ffi::PyLong_AsLongLong(bit.as_ptr());
        let def = Box::leak(Box::new(ffi::PyMethodDef {
            ml_name: fname.as_ptr(),
            ml_meth: ffi::PyMethodDefPointer { PyCFunctionFast: f },
            ml_flags: ffi::METH_FASTCALL,
            ml_doc: std::ptr::null(),
        }));
        let func = Owned::new(ffi::PyCFunction_NewEx(def, std::ptr::null_mut(), std::ptr::null_mut()))
            .ok_or("could not create callback")?;
        call(c"register_callback", &[tool.as_ptr(), bit.as_ptr(), func.as_ptr()])?;
    }
    let m = Owned::new(ffi::PyLong_FromLongLong(mask)).ok_or("int")?;
    call(c"set_events", &[tool.as_ptr(), m.as_ptr()])?;
    Ok(())
}

/// Uninstall and hand the recorder back so its trace can be written.
pub unsafe fn stop() -> Option<Box<Recorder>> {
    if let Some(mon) = Owned::new(ffi::PyImport_ImportModule(c"sys".as_ptr())).and_then(|s| py::getattr(s.as_ptr(), c"monitoring")) {
        let tool = ffi::PyLong_FromLongLong(TOOL);
        let zero = ffi::PyLong_FromLongLong(0);
        if let Some(f) = py::getattr(mon.as_ptr(), c"set_events") {
            Owned::new(ffi::PyObject_CallFunctionObjArgs(f.as_ptr(), tool, zero, std::ptr::null_mut::<ffi::PyObject>()));
        }
        if let Some(events) = py::getattr(mon.as_ptr(), c"events") {
            if let Some(f) = py::getattr(mon.as_ptr(), c"register_callback") {
                for (ev, _, _) in EVENTS {
                    if let Some(bit) = py::getattr(events.as_ptr(), ev) {
                        Owned::new(ffi::PyObject_CallFunctionObjArgs(f.as_ptr(), tool, bit.as_ptr(), ffi::Py_None(), std::ptr::null_mut::<ffi::PyObject>()));
                    }
                }
            }
        }
        if let Some(f) = py::getattr(mon.as_ptr(), c"free_tool_id") {
            Owned::new(ffi::PyObject_CallFunctionObjArgs(f.as_ptr(), tool, std::ptr::null_mut::<ffi::PyObject>()));
        }
        ffi::Py_DecRef(tool);
        ffi::Py_DecRef(zero);
        ffi::PyErr_Clear();
    }
    let p = REC;
    REC = std::ptr::null_mut();
    if p.is_null() { None } else { Some(Box::from_raw(p)) }
}

pub unsafe fn fetch_error() -> String {
    let exc = ffi::PyErr_GetRaisedException();
    if exc.is_null() {
        return "unknown error".into();
    }
    let exc = Owned(exc);
    let t = py::type_name(ffi::Py_TYPE(exc.as_ptr()));
    let m = py::str_of(exc.as_ptr()).unwrap_or_default();
    if m.is_empty() { t } else { format!("{t}: {m}") }
}
