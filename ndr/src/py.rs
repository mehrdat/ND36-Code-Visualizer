//! Thin, careful wrappers over the CPython C API.
//!
//! Every function here is called from inside a `sys.monitoring` callback,
//! with the GIL held, millions of times. Two rules follow:
//!
//!   1. Never leave a Python exception set. A callback that returns with an
//!      exception pending turns into a `SystemError` in the user's program.
//!      Every fallible call clears on failure.
//!   2. Never leak or double-free a reference. `Owned` decrefs on drop;
//!      anything else here is a borrowed pointer and is not freed.

use pyo3::ffi;
#[allow(unused_imports)] // c_int and ptr are only used on 3.13+
use std::ffi::{c_char, c_int, CStr};
#[allow(unused_imports)]
use std::ptr;

extern "C" {
    // In CPython since 3.11, not exposed by pyo3-ffi. Returns a new reference:
    // a dict (module / class bodies, and every frame before 3.13) or a
    // FrameLocalsProxy (function frames, 3.13+; PEP 667).
    pub fn PyFrame_GetLocals(frame: *mut ffi::PyFrameObject) -> *mut ffi::PyObject;
    // Public since 3.11: the byte offset of the instruction being executed.
    pub fn PyFrame_GetLasti(frame: *mut ffi::PyFrameObject) -> std::ffi::c_int;
}

#[cfg(Py_3_13)]
extern "C" {
    // 3.13+: look a key up without raising KeyError when it is missing.
    // 1 = found (new ref in *result), 0 = missing, -1 = error.
    fn PyMapping_GetOptionalItem(
        obj: *mut ffi::PyObject,
        key: *mut ffi::PyObject,
        result: *mut *mut ffi::PyObject,
    ) -> c_int;
}

/// A strong reference, released on drop.
pub struct Owned(pub *mut ffi::PyObject);

impl Owned {
    /// Take ownership of a new reference. `None` if the pointer is NULL,
    /// in which case the pending exception is cleared.
    #[inline]
    pub fn new(p: *mut ffi::PyObject) -> Option<Owned> {
        if p.is_null() {
            unsafe { ffi::PyErr_Clear() };
            None
        } else {
            Some(Owned(p))
        }
    }
    /// Take a new strong reference to a borrowed pointer.
    #[inline]
    pub fn from_borrowed(p: *mut ffi::PyObject) -> Owned {
        unsafe { ffi::Py_IncRef(p) };
        Owned(p)
    }
    #[inline]
    pub fn as_ptr(&self) -> *mut ffi::PyObject {
        self.0
    }
}

impl Drop for Owned {
    fn drop(&mut self) {
        unsafe { ffi::Py_DecRef(self.0) };
    }
}

/// `str` object -> Rust String. `None` if it is not a str.
#[inline]
pub unsafe fn to_string(obj: *mut ffi::PyObject) -> Option<String> {
    if obj.is_null() || ffi::PyUnicode_Check(obj) == 0 {
        return None;
    }
    let mut size: ffi::Py_ssize_t = 0;
    let data = ffi::PyUnicode_AsUTF8AndSize(obj, &mut size);
    if data.is_null() {
        ffi::PyErr_Clear(); // lone surrogates cannot be UTF-8
        return Some(String::from("<unprintable str>"));
    }
    let bytes = std::slice::from_raw_parts(data as *const u8, size as usize);
    Some(String::from_utf8_lossy(bytes).into_owned())
}

/// `getattr(obj, name)` that returns `None` instead of raising.
#[inline]
pub unsafe fn getattr(obj: *mut ffi::PyObject, name: &CStr) -> Option<Owned> {
    #[cfg(Py_3_13)]
    {
        let mut out: *mut ffi::PyObject = ptr::null_mut();
        let rc = ffi::PyObject_GetOptionalAttrString(obj, name.as_ptr(), &mut out);
        if rc == 1 {
            return Some(Owned(out));
        }
        if rc < 0 {
            ffi::PyErr_Clear();
        }
        None
    }
    #[cfg(not(Py_3_13))]
    {
        Owned::new(ffi::PyObject_GetAttrString(obj, name.as_ptr()))
    }
}

/// Call a method with no arguments.
pub unsafe fn call0(obj: *mut ffi::PyObject, name: &CStr) -> Option<Owned> {
    let m = getattr(obj, name)?;
    Owned::new(ffi::PyObject_CallNoArgs(m.as_ptr()))
}

/// `repr(obj)` as text, or `None` if repr raised.
pub unsafe fn repr(obj: *mut ffi::PyObject) -> Option<String> {
    let r = Owned::new(ffi::PyObject_Repr(obj))?;
    to_string(r.as_ptr())
}

/// `str(obj)` as text, or `None` if it raised.
pub unsafe fn str_of(obj: *mut ffi::PyObject) -> Option<String> {
    let r = Owned::new(ffi::PyObject_Str(obj))?;
    to_string(r.as_ptr())
}

/// `len(obj)`, or `None` for unsized objects (without leaving a TypeError).
#[inline]
pub unsafe fn len(obj: *mut ffi::PyObject) -> Option<isize> {
    let n = ffi::PyObject_Size(obj);
    if n < 0 {
        ffi::PyErr_Clear();
        None
    } else {
        Some(n)
    }
}

/// `type(obj).__name__` -- the short name, the way Python prints it.
pub unsafe fn type_name(tp: *mut ffi::PyTypeObject) -> String {
    let full = CStr::from_ptr((*tp).tp_name as *const c_char).to_string_lossy();
    match full.rfind('.') {
        Some(i) => full[i + 1..].to_string(),
        None => full.into_owned(),
    }
}

/// `type(obj).__module__`, for telling numpy from a user class.
pub unsafe fn type_module(tp: *mut ffi::PyTypeObject) -> String {
    let full = CStr::from_ptr((*tp).tp_name as *const c_char).to_string_lossy();
    if let Some(i) = full.rfind('.') {
        return full[..i].to_string(); // static types carry it in tp_name
    }
    getattr(tp as *mut ffi::PyObject, c"__module__")
        .and_then(|m| to_string(m.as_ptr()))
        .unwrap_or_default()
}

/// Look one name up in a frame's locals mapping (dict or 3.13 proxy).
/// Returns a NEW reference, or `None` if the name is unbound.
#[inline]
pub unsafe fn lookup(locals: *mut ffi::PyObject, name: *mut ffi::PyObject) -> Option<Owned> {
    if ffi::PyDict_CheckExact(locals) != 0 {
        let v = ffi::PyDict_GetItemWithError(locals, name); // borrowed
        if v.is_null() {
            ffi::PyErr_Clear();
            return None;
        }
        return Some(Owned::from_borrowed(v));
    }
    #[cfg(Py_3_13)]
    {
        let mut out: *mut ffi::PyObject = ptr::null_mut();
        let rc = PyMapping_GetOptionalItem(locals, name, &mut out);
        if rc == 1 {
            return Some(Owned(out));
        }
        if rc < 0 {
            ffi::PyErr_Clear();
        }
        None
    }
    #[cfg(not(Py_3_13))]
    {
        Owned::new(ffi::PyObject_GetItem(locals, name))
    }
}

/// A new `str` from Rust text.
pub unsafe fn new_str(s: &str) -> Option<Owned> {
    Owned::new(ffi::PyUnicode_FromStringAndSize(
        s.as_ptr() as *const c_char,
        s.len() as ffi::Py_ssize_t,
    ))
}

#[inline]
pub unsafe fn none() -> *mut ffi::PyObject {
    let n = ffi::Py_None();
    ffi::Py_IncRef(n);
    n
}

/// Truncate text the way the Python encoder does: 200 characters, then "…".
pub fn clip(text: &str, max_chars: usize) -> String {
    let mut it = text.char_indices();
    match it.nth(max_chars) {
        None => text.to_string(),
        Some((i, _)) => format!("{}…", &text[..i]),
    }
}

