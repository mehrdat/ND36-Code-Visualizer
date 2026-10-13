//! Turning Python values into trace records.
//!
//! Two operations, and the split between them is where most of the speedup
//! over the Python recorder comes from:
//!
//!   `cheap` + `deep_hash`   run on every line that could mutate, for the
//!                           variables it names (others on a back-off --
//!                           see `recorder::check`).
//!                           No allocation, no repr, no JSON. A 12-item list
//!                           hashes in tens of nanoseconds.
//!   `encode`                runs only when that hash says something changed.
//!                           This is the expensive part -- repr, children,
//!                           a JSON row -- and it is now off the common path.
//!
//! The Python recorder had to ration its re-walks of containers (a budget of
//! 150 per object), so after a while it silently stopped noticing in-place
//! changes. Hashing is cheap enough here that there is no such cut-off.

use crate::py::{self, Owned};
use crate::schema::Vref;
use crate::writer::{json_str, ObjRow, Writer};
use pyo3::ffi;
use rustc_hash::FxHashMap as HashMap;
use std::fmt::Write;
use std::hash::{Hash, Hasher};

pub const MAX_STR: usize = 200; // characters of a string kept
pub const MAX_CHILDREN: usize = 50; // items of a container kept
pub const MAX_DEPTH: u32 = 3; // nesting walked
pub const MAX_WALK: i32 = 192; // child encodes per top-level encode
pub const HASH_BUDGET: i32 = 512; // elements hashed per variable per line
const REPR_LIMIT: isize = 32; // longer than this: preview, never full repr()

const MEMO_SLOTS: usize = 256; // a power of two

#[derive(Clone, Copy, Default)]
struct MemoSlot {
    key: usize,
    depth: u32,
    gen: u32,
    hash: u64,
    cost: i32, // budget the walk used: a hit must charge the same
    seen: Bloom, // mutable objects found below it
}

/// A 256-bit Bloom filter of object addresses: "which mutable objects did
/// this hash walk through?" One bit per object, so a false "yes" costs a
/// needless re-hash and a "no" is always right.
#[derive(Clone, Copy, Default, PartialEq, Debug)]
pub struct Bloom(pub [u64; 4]);

impl Bloom {
    #[inline]
    pub fn add(&mut self, addr: usize) {
        let b = ((addr >> 4).wrapping_mul(0x9E37_79B9_7F4A_7C15) >> 56) as usize;
        self.0[b >> 6] |= 1 << (b & 63);
    }
    #[inline]
    pub fn has(&self, addr: usize) -> bool {
        let b = ((addr >> 4).wrapping_mul(0x9E37_79B9_7F4A_7C15) >> 56) as usize;
        self.0[b >> 6] & (1 << (b & 63)) != 0
    }
    #[inline]
    pub fn merge(&mut self, o: &Bloom) {
        for i in 0..4 { self.0[i] |= o.0[i]; }
    }
}

/// A fast stand-in for "is this the same value as last line?".
#[derive(Clone, Copy, PartialEq, Debug)]
pub enum Cheap {
    None,
    Bool(bool),
    Int(i64),
    Big(isize), // an int too large for i64: compare by hash
    Float(u64),
    Str(isize, isize), // (hash, len) -- str hashes are cached in the object
    Obj(usize, isize), // (address, len or -1)
}

#[derive(Clone, Copy, PartialEq)]
enum Shape {
    List,
    Tuple,
    Dict,
    Set,
    ByteArray,
    Heavy,   // numpy / pandas / torch: never repr(), never walk
    Generic, // has a __dict__: walk its fields
    Opaque,  // functions, modules, classes, iterators: identity only
}

struct TypeInfo {
    name: String,
    shape: Shape,
}

pub struct Encoder {
    by_ptr: HashMap<usize, (u32, usize)>, // address -> (obj_id, type address)
    next_id: u32,
    written: HashMap<u32, u64>, // obj_id -> deep hash of the last row we wrote
    types: HashMap<usize, TypeInfo>,
    walk: i32,
    /// (address, depth) -> content hash, valid for ONE line's diff. An object
    /// reachable many ways -- `solutions` holding the same `board` 92 times,
    /// or a list that is also a local -- is walked once per line, not once
    /// per reference. Cleared whenever user code may have run (see encode).
    memo: Vec<MemoSlot>,
    memo_gen: u32,
    /// Did the last top-level `encode` write any new object version? The
    /// recorder uses it to tell an in-place change from nothing at all.
    pub last_changed: bool,
    /// After `deep_hash`: the mutable objects its walk went through. If one
    /// of them changes, the value's hash can change; if none does, it can't.
    pub last_seen: Bloom,
}

const HEAVY_MODULES: &[&str] = &["numpy", "pandas", "torch", "tensorflow", "jax", "polars", "scipy", "cupy"];

impl Encoder {
    pub fn new() -> Encoder {
        Encoder {
            by_ptr: HashMap::default(),
            next_id: 1,
            written: HashMap::default(),
            types: HashMap::default(),
            walk: 0,
            memo: vec![MemoSlot::default(); MEMO_SLOTS],
            memo_gen: 1,
            last_changed: false,
            last_seen: Bloom::default(),
        }
    }

    unsafe fn type_info(&mut self, tp: *mut ffi::PyTypeObject) -> &TypeInfo {
        let key = tp as usize;
        if !self.types.contains_key(&key) {
            let name = py::type_name(tp);
            let shape = if tp == std::ptr::addr_of_mut!(ffi::PyList_Type) {
                Shape::List
            } else if tp == std::ptr::addr_of_mut!(ffi::PyTuple_Type) {
                Shape::Tuple
            } else if tp == std::ptr::addr_of_mut!(ffi::PyDict_Type) {
                Shape::Dict
            } else if tp == std::ptr::addr_of_mut!(ffi::PySet_Type)
                || tp == std::ptr::addr_of_mut!(ffi::PyFrozenSet_Type)
            {
                Shape::Set
            } else if tp == std::ptr::addr_of_mut!(ffi::PyByteArray_Type) {
                Shape::ByteArray
            } else if ffi::PyType_IsSubtype(tp, std::ptr::addr_of_mut!(ffi::PyDict_Type)) != 0 {
                Shape::Dict // defaultdict, Counter, OrderedDict
            } else if ffi::PyType_IsSubtype(tp, std::ptr::addr_of_mut!(ffi::PyList_Type)) != 0 {
                Shape::List
            } else {
                let module = py::type_module(tp);
                let root = module.split('.').next().unwrap_or("");
                if HEAVY_MODULES.contains(&root) {
                    Shape::Heavy
                } else if module == "builtins" {
                    // functions, modules, classes, range, generators...
                    Shape::Opaque
                } else if (*tp).tp_dictoffset != 0 || ((*tp).tp_flags & ffi::Py_TPFLAGS_MANAGED_DICT) != 0 {
                    Shape::Generic
                } else {
                    Shape::Opaque
                }
            };
            self.types.insert(key, TypeInfo { name, shape });
        }
        &self.types[&key]
    }

    // -- the every-line path -------------------------------------------------

    /// Constant-time identity/value key. No allocation.
    #[inline]
    pub unsafe fn cheap(&self, v: *mut ffi::PyObject) -> Cheap {
        if v == ffi::Py_None() {
            return Cheap::None;
        }
        if ffi::PyBool_Check(v) != 0 {
            return Cheap::Bool(v == ffi::Py_True());
        }
        if ffi::PyLong_CheckExact(v) != 0 {
            let mut overflow: std::ffi::c_int = 0;
            let n = ffi::PyLong_AsLongLongAndOverflow(v, &mut overflow);
            if overflow == 0 {
                return Cheap::Int(n);
            }
            return Cheap::Big(ffi::PyObject_Hash(v));
        }
        if ffi::PyFloat_CheckExact(v) != 0 {
            return Cheap::Float(ffi::PyFloat_AsDouble(v).to_bits());
        }
        if ffi::PyUnicode_CheckExact(v) != 0 {
            return Cheap::Str(ffi::PyObject_Hash(v), ffi::PyUnicode_GetLength(v));
        }
        let n = if ffi::PyList_CheckExact(v) != 0 {
            ffi::PyList_Size(v)
        } else if ffi::PyDict_CheckExact(v) != 0 {
            ffi::PyDict_Size(v)
        } else if ffi::PyTuple_CheckExact(v) != 0 {
            ffi::PyTuple_Size(v)
        } else {
            -1
        };
        Cheap::Obj(v as usize, n)
    }

    /// Is this a value whose contents could change while the name keeps
    /// pointing at it? Only those are worth a `deep_hash` every line.
    #[inline]
    pub unsafe fn mutable(&mut self, v: *mut ffi::PyObject, cheap: Cheap) -> bool {
        match cheap {
            Cheap::Obj(..) => {
                if ffi::PyTuple_CheckExact(v) != 0 {
                    return true; // immutable itself, but may hold lists
                }
                let tp = ffi::Py_TYPE(v);
                !matches!(self.type_info(tp).shape, Shape::Opaque)
            }
            _ => false,
        }
    }

    /// A content hash, for noticing in-place changes. Walks at most
    /// `HASH_BUDGET` elements in total, three levels deep.
    /// A new line: nothing hashed before this point can be trusted.
    #[inline]
    pub fn new_line(&mut self) {
        // Invalidate every slot at once by moving to a new generation --
        // no clearing, so a line with nothing to share costs nothing.
        self.memo_gen = self.memo_gen.wrapping_add(1);
        if self.memo_gen == 0 {
            self.memo.iter_mut().for_each(|m| m.gen = 0);
            self.memo_gen = 1;
        }
    }

    #[inline]
    fn memo_slot(v: usize, depth: u32) -> usize {
        ((v >> 4) ^ (depth as usize).wrapping_mul(0x9E37)) & (MEMO_SLOTS - 1)
    }

    pub unsafe fn deep_hash(&mut self, v: *mut ffi::PyObject) -> u64 {
        let mut h = rustc_hash::FxHasher::default();
        let mut budget = HASH_BUDGET;
        let mut seen = Bloom::default();
        self.hash_into(v, 0, &mut budget, &mut h, &mut seen);
        self.last_seen = seen;
        h.finish()
    }

    unsafe fn hash_into(&mut self, v: *mut ffi::PyObject, depth: u32, budget: &mut i32, h: &mut impl Hasher, mask: &mut Bloom) {
        *budget -= 1;
        let c = self.cheap(v);
        c_hash(&c, h);
        if !matches!(c, Cheap::Obj(..)) {
            return;
        }
        if depth > 0 && ffi::PyTuple_CheckExact(v) == 0 {
            mask.add(v as usize);
        }
        if depth >= MAX_DEPTH || *budget <= 0 {
            return;
        }
        // The memo must never change the answer, only its cost: a hit is
        // charged the budget the real walk used, and only walks that ran to
        // completion are remembered. Otherwise a list's hash would depend on
        // which OTHER variable happened to be hashed first on that line (one
        // sharing its items), and an untouched container would "change".
        let i = Self::memo_slot(v as usize, depth);
        let m = self.memo[i];
        if m.gen == self.memo_gen && m.key == v as usize && m.depth == depth && m.cost < *budget {
            *budget -= m.cost;
            mask.merge(&m.seen);
            m.hash.hash(h); // already walked this object on this line
            return;
        }
        let before = *budget;
        let mut sub_h = rustc_hash::FxHasher::default();
        let mut sub_mask = Bloom::default();
        self.walk_children(v, depth, budget, &mut sub_h, &mut sub_mask);
        let sub = sub_h.finish();
        if *budget > 0 {
            self.memo[i] = MemoSlot { key: v as usize, depth, gen: self.memo_gen, hash: sub,
                                      cost: before - *budget, seen: sub_mask };
        }
        mask.merge(&sub_mask);
        sub.hash(h);
    }

    unsafe fn walk_children(&mut self, v: *mut ffi::PyObject, depth: u32, budget: &mut i32, h: &mut impl Hasher, mask: &mut Bloom) {
        let tp = ffi::Py_TYPE(v);
        let shape = self.type_info(tp).shape;
        match shape {
            Shape::List => {
                let n = ffi::PyList_Size(v);
                for i in 0..n {
                    if *budget <= 0 { break; }
                    let item = ffi::PyList_GetItem(v, i); // borrowed
                    if !item.is_null() { self.hash_into(item, depth + 1, budget, h, mask); }
                }
            }
            Shape::Tuple => {
                let n = ffi::PyTuple_Size(v);
                for i in 0..n {
                    if *budget <= 0 { break; }
                    let item = ffi::PyTuple_GetItem(v, i);
                    if !item.is_null() { self.hash_into(item, depth + 1, budget, h, mask); }
                }
            }
            Shape::Dict => self.hash_dict(v, depth, budget, h, mask),
            Shape::Set => {
                (ffi::PySet_Size(v)).hash(h);
                if let Some(it) = Owned::new(ffi::PyObject_GetIter(v)) {
                    loop {
                        if *budget <= 0 { break; }
                        let item = ffi::PyIter_Next(it.as_ptr());
                        if item.is_null() { ffi::PyErr_Clear(); break; }
                        let item = Owned(item);
                        self.hash_into(item.as_ptr(), depth + 1, budget, h, mask);
                    }
                }
            }
            Shape::ByteArray => {
                let n = ffi::PyByteArray_Size(v);
                let p = ffi::PyByteArray_AsString(v) as *const u8;
                if !p.is_null() {
                    let take = (n as usize).min(*budget as usize * 8);
                    std::slice::from_raw_parts(p, take).hash(h);
                    *budget -= (take / 8) as i32;
                }
            }
            Shape::Generic => {
                if let Some(d) = py::getattr(v, c"__dict__") {
                    if ffi::PyDict_CheckExact(d.as_ptr()) != 0 {
                        self.hash_dict(d.as_ptr(), depth, budget, h, mask);
                    }
                }
            }
            Shape::Heavy => {
                // Shape only: hashing a tensor's contents is O(rows).
                if let Some(s) = py::getattr(v, c"shape") {
                    ffi::PyObject_Hash(s.as_ptr()).hash(h);
                    ffi::PyErr_Clear();
                }
            }
            Shape::Opaque => {}
        }
    }

    unsafe fn hash_dict(&mut self, d: *mut ffi::PyObject, depth: u32, budget: &mut i32, h: &mut impl Hasher, mask: &mut Bloom) {
        let mut pos: ffi::Py_ssize_t = 0;
        let mut k: *mut ffi::PyObject = std::ptr::null_mut();
        let mut val: *mut ffi::PyObject = std::ptr::null_mut();
        ffi::PyDict_Size(d).hash(h);
        while *budget > 0 && ffi::PyDict_Next(d, &mut pos, &mut k, &mut val) != 0 {
            (k as usize).hash(h); // keys are almost always interned or shared
            self.hash_into(val, depth + 1, budget, h, mask);
        }
    }

    // -- the only-on-change path ---------------------------------------------

    /// Pack a value. Objects get an obj_id and, if their contents moved since
    /// the last row we wrote for them, a new version row.
    pub unsafe fn encode(&mut self, w: &mut Writer, v: *mut ffi::PyObject, step: u64) -> Vref {
        self.last_changed = false;
        self.walk = MAX_WALK;
        let out = self.encode_at(w, v, step, 0);
        if self.last_changed {
            self.new_line(); // a __repr__ ran; it could have changed anything
        }
        out
    }

    unsafe fn encode_at(&mut self, w: &mut Writer, v: *mut ffi::PyObject, step: u64, depth: u32) -> Vref {
        if v == ffi::Py_None() {
            return Vref::NULL;
        }
        if ffi::PyBool_Check(v) != 0 {
            return Vref::boolean(v == ffi::Py_True());
        }
        if ffi::PyLong_CheckExact(v) != 0 {
            let mut overflow: std::ffi::c_int = 0;
            let n = ffi::PyLong_AsLongLongAndOverflow(v, &mut overflow);
            if overflow == 0 {
                return Vref::int(n);
            }
            let text = py::str_of(v).unwrap_or_default();
            return Vref::string(w.string_id(&py::clip(&text, MAX_STR)));
        }
        if ffi::PyFloat_CheckExact(v) != 0 {
            return Vref::float(ffi::PyFloat_AsDouble(v));
        }
        if ffi::PyUnicode_CheckExact(v) != 0 {
            let text = py::to_string(v).unwrap_or_default();
            return Vref::string(w.string_id(&py::clip(&text, MAX_STR)));
        }
        if ffi::PyBytes_CheckExact(v) != 0 {
            let n = ffi::PyBytes_Size(v) as usize;
            let p = ffi::PyBytes_AsString(v) as *const u8;
            let bytes = std::slice::from_raw_parts(p, n.min(MAX_STR));
            return Vref::string(w.string_id(&String::from_utf8_lossy(bytes)));
        }

        // An object. Identity is the address, checked against the type so a
        // recycled address is not mistaken for the old object.
        let tp = ffi::Py_TYPE(v);
        let key = v as usize;
        let obj_id = match self.by_ptr.get(&key) {
            Some(&(id, t)) if t == tp as usize => id,
            _ => {
                let id = self.next_id;
                self.next_id += 1;
                self.by_ptr.insert(key, (id, tp as usize));
                id
            }
        };

        let h = self.deep_hash(v);
        if self.written.get(&obj_id) == Some(&h) {
            return Vref::obj(obj_id);
        }
        self.written.insert(obj_id, h);
        self.last_changed = true;

        let (tname, shape) = {
            let ti = self.type_info(tp);
            (ti.name.clone(), ti.shape)
        };
        let n = if shape == Shape::Opaque || shape == Shape::Heavy { None } else { py::len(v) };

        let mut summary = String::with_capacity(64);
        let _ = write!(summary, "{{\"type\":{},\"kind\":\"{}\"", json_str(&tname),
                       if shape == Shape::Heavy { "array" } else { "generic" });
        if let Some(n) = n {
            let _ = write!(summary, ",\"len\":{n}");
        }
        let mut label = String::new();
        if shape == Shape::Heavy {
            let shp = py::getattr(v, c"shape").and_then(|s| py::str_of(s.as_ptr()));
            let dt = py::getattr(v, c"dtype").and_then(|s| py::str_of(s.as_ptr()));
            if let Some(s) = &shp {
                let _ = write!(summary, ",\"shape\":{}", json_str(s));
            }
            if let Some(d) = &dt {
                let _ = write!(summary, ",\"dtype\":{}", json_str(d));
            }
            label = format!("<{}{}{}>", tname,
                            shp.map(|s| format!(" shape={s}")).unwrap_or_default(),
                            dt.map(|d| format!(" dtype={d}")).unwrap_or_default());
        }
        summary.push('}');

        let children = if depth < MAX_DEPTH && matches!(shape, Shape::List | Shape::Tuple | Shape::Dict | Shape::Set | Shape::Generic) {
            self.children(w, v, shape, step, depth)
        } else {
            None
        };
        if label.is_empty() {
            label = self.short_repr(v, shape, n);
        }

        w.objects.push(ObjRow { obj_id, step, type_name: tname, summary, children, repr: label });
        Vref::obj(obj_id)
    }

    unsafe fn children(&mut self, w: &mut Writer, v: *mut ffi::PyObject, shape: Shape, step: u64, depth: u32) -> Option<String> {
        let mut out = String::from("[");
        let mut first = true;
        let mut push = |enc: &mut Encoder, out: &mut String, key: String, item: *mut ffi::PyObject| -> bool {
            if enc.walk <= 0 {
                return false;
            }
            enc.walk -= 1;
            let child = enc.encode_at(w, item, step, depth + 1);
            if !first { out.push(','); }
            first = false;
            out.push('[');
            out.push_str(&key);
            out.push(',');
            child.json(out);
            out.push(']');
            true
        };
        match shape {
            Shape::List | Shape::Tuple => {
                let n = py::len(v).unwrap_or(0) as usize;
                for i in 0..n.min(MAX_CHILDREN) {
                    let item = if shape == Shape::List { ffi::PyList_GetItem(v, i as isize) } else { ffi::PyTuple_GetItem(v, i as isize) };
                    if item.is_null() { ffi::PyErr_Clear(); break; }
                    let item = Owned::from_borrowed(item); // user __repr__ could mutate the list
                    if !push(self, &mut out, i.to_string(), item.as_ptr()) { break; }
                }
            }
            Shape::Set => {
                if let Some(it) = Owned::new(ffi::PyObject_GetIter(v)) {
                    for i in 0..MAX_CHILDREN {
                        let item = ffi::PyIter_Next(it.as_ptr());
                        if item.is_null() { ffi::PyErr_Clear(); break; }
                        let item = Owned(item);
                        if !push(self, &mut out, i.to_string(), item.as_ptr()) { break; }
                    }
                }
            }
            Shape::Dict | Shape::Generic => {
                let d = if shape == Shape::Dict {
                    Some(Owned::from_borrowed(v))
                } else {
                    py::getattr(v, c"__dict__")
                };
                let d = d?;
                if ffi::PyDict_CheckExact(d.as_ptr()) == 0 {
                    return None;
                }
                // Snapshot the first MAX_CHILDREN pairs (not the whole dict:
                // copying 10,000 items to keep 50 was a fifth of the run).
                // Encoding a child can run user code, and a dict that changes
                // size mid-iteration is UB, so nothing runs until we're done.
                let pairs = first_items(d.as_ptr(), MAX_CHILDREN);
                for (k, val) in &pairs {
                    let k = k.as_ptr();
                    let key = if ffi::PyUnicode_CheckExact(k) != 0 {
                        json_str(&py::to_string(k).unwrap_or_default())
                    } else if ffi::PyLong_CheckExact(k) != 0 {
                        py::str_of(k).unwrap_or_else(|| "0".into())
                    } else {
                        json_str(&py::clip(&repr_of(k).unwrap_or_else(|| "?".into()), 40))
                    };
                    if !push(self, &mut out, key, val.as_ptr()) { break; }
                }
            }
            _ => return None,
        }
        out.push(']');
        Some(out)
    }

    /// A short, safe, cheap label. Never builds a 200 kB repr to keep 200
    /// characters of it -- that was 11 of 12 seconds in the Python version
    /// before it learned the same trick.
    unsafe fn short_repr(&mut self, v: *mut ffi::PyObject, shape: Shape, n: Option<isize>) -> String {
        if let Some(n) = n {
            if n > REPR_LIMIT && matches!(shape, Shape::List | Shape::Tuple | Shape::Set | Shape::Dict) {
                let mut parts = Vec::new();
                if shape == Shape::Dict {
                    for (k, val) in first_items(v, 4) {
                        let k = repr_of(k.as_ptr()).unwrap_or_default();
                        let val = repr_of(val.as_ptr()).unwrap_or_default();
                        parts.push(format!("{}: {}", py::clip(&k, 20), py::clip(&val, 20)));
                    }
                    return format!("{{{}, …}}  {} items", parts.join(", "), n);
                }
                if let Some(it) = Owned::new(ffi::PyObject_GetIter(v)) {
                    for _ in 0..6 {
                        let item = ffi::PyIter_Next(it.as_ptr());
                        if item.is_null() { ffi::PyErr_Clear(); break; }
                        let item = Owned(item);
                        parts.push(py::clip(&repr_of(item.as_ptr()).unwrap_or_default(), 20));
                    }
                }
                let (o, c) = if shape == Shape::List { ("[", "]") } else { ("(", ")") };
                return format!("{o}{}, …{c}  {n} items", parts.join(", "));
            }
        }
        match repr_of(v) {
            Some(text) => py::clip(&text, MAX_STR),
            None => format!("<{}>", py::type_name(ffi::Py_TYPE(v))),
        }
    }
}

/// The first `n` (key, value) pairs of a dict, each held by a new reference.
unsafe fn first_items(d: *mut ffi::PyObject, n: usize) -> Vec<(Owned, Owned)> {
    let mut out = Vec::with_capacity(n.min(64));
    let mut pos: ffi::Py_ssize_t = 0;
    let mut k: *mut ffi::PyObject = std::ptr::null_mut();
    let mut v: *mut ffi::PyObject = std::ptr::null_mut();
    while out.len() < n && ffi::PyDict_Next(d, &mut pos, &mut k, &mut v) != 0 {
        out.push((Owned::from_borrowed(k), Owned::from_borrowed(v)));
    }
    out
}

/// `repr(v)`, built in Rust when the answer is certain to match Python's
/// (None, bools, ints, plain floats, simple strings, and tuples or lists of
/// those), otherwise by calling `repr()`. Most children of a big container
/// are exactly these -- `(3, 7)` keys, `(f, g, (r, c))` heap entries -- and
/// one `PyObject_Repr` each was a tenth of the run.
pub unsafe fn repr_of(v: *mut ffi::PyObject) -> Option<String> {
    let mut out = String::new();
    if fast_repr(v, &mut out, 0) {
        return Some(out);
    }
    py::repr(v)
}

unsafe fn fast_repr(v: *mut ffi::PyObject, out: &mut String, depth: u32) -> bool {
    if v == ffi::Py_None() {
        out.push_str("None");
        return true;
    }
    if ffi::PyBool_Check(v) != 0 {
        out.push_str(if v == ffi::Py_True() { "True" } else { "False" });
        return true;
    }
    if ffi::PyLong_CheckExact(v) != 0 {
        let mut overflow: std::ffi::c_int = 0;
        let n = ffi::PyLong_AsLongLongAndOverflow(v, &mut overflow);
        if overflow != 0 { ffi::PyErr_Clear(); return false; }
        let _ = write!(out, "{n}");
        return true;
    }
    if ffi::PyFloat_CheckExact(v) != 0 {
        let x = ffi::PyFloat_AsDouble(v);
        let a = x.abs();
        // Python switches to exponent form outside [1e-4, 1e16): leave those to it.
        if !x.is_finite() || (a != 0.0 && !(1e-4..1e16).contains(&a)) {
            return false;
        }
        let start = out.len();
        let _ = write!(out, "{x}");
        if !out[start..].contains('.') {
            out.push_str(".0");
        }
        return true;
    }
    if ffi::PyUnicode_CheckExact(v) != 0 {
        if ffi::PyUnicode_GetLength(v) > 64 {
            return false;
        }
        let Some(s) = py::to_string(v) else { ffi::PyErr_Clear(); return false };
        if !s.bytes().all(|b| (0x20..0x7f).contains(&b) && b != b'\\' && b != b'\'') {
            return false;
        }
        out.push('\'');
        out.push_str(&s);
        out.push('\'');
        return true;
    }
    if depth >= 2 {
        return false;
    }
    let tp = ffi::Py_TYPE(v);
    let plain = (*tp).tp_repr.map(|f| f as usize) == ffi::PyBaseObject_Type.tp_repr.map(|f| f as usize);
    if depth == 0 && plain {
        // A class without its own __repr__: object.__repr__ is fixed text.
        let module = py::type_module(tp);
        let qual = py::getattr(tp as *mut ffi::PyObject, c"__qualname__")
            .and_then(|q| py::to_string(q.as_ptr()))
            .unwrap_or_else(|| py::type_name(tp));
        ffi::PyErr_Clear();
        if module == "builtins" || module.is_empty() {
            let _ = write!(out, "<{qual} object at {:#x}>", v as usize);
        } else {
            let _ = write!(out, "<{module}.{qual} object at {:#x}>", v as usize);
        }
        return true;
    }
    let (is_tuple, is_list) = (ffi::PyTuple_CheckExact(v) != 0, ffi::PyList_CheckExact(v) != 0);
    if !is_tuple && !is_list {
        return false;
    }
    let n = if is_tuple { ffi::PyTuple_Size(v) } else { ffi::PyList_Size(v) };
    if n > 32 || out.len() > 400 {
        return false;
    }
    let start = out.len();
    out.push(if is_tuple { '(' } else { '[' });
    for i in 0..n {
        if i > 0 { out.push_str(", "); }
        let item = if is_tuple { ffi::PyTuple_GetItem(v, i) } else { ffi::PyList_GetItem(v, i) };
        if item.is_null() || !fast_repr(item, out, depth + 1) {
            ffi::PyErr_Clear();
            out.truncate(start);
            return false;
        }
    }
    if is_tuple && n == 1 { out.push(','); }
    out.push(if is_tuple { ')' } else { ']' });
    true
}

#[inline]
fn c_hash(c: &Cheap, h: &mut impl Hasher) {
    match *c {
        Cheap::None => 0u8.hash(h),
        Cheap::Bool(b) => (1u8, b).hash(h),
        Cheap::Int(n) => (2u8, n).hash(h),
        Cheap::Big(x) => (3u8, x).hash(h),
        Cheap::Float(b) => (4u8, b).hash(h),
        Cheap::Str(x, n) => (5u8, x, n).hash(h),
        Cheap::Obj(p, n) => (6u8, p, n).hash(h),
    }
}

/// Is a packed value a scalar (as opposed to an object reference)?
pub fn is_scalar(v: &Vref) -> bool {
    v.rk != crate::schema::RK_OBJ && v.rk != crate::schema::RK_ABSENT
}
