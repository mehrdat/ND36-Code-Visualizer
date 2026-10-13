//! Run a Python script inside the embedded interpreter, recorded or not.
//!
//! `ndr` links CPython in, so there is no second process and no `python`
//! on PATH to get wrong: the interpreter is the one `ndr` was built against.

use crate::py::{self, Owned};
use crate::recorder;
use crate::writer::{json_str, Writer};
use pyo3::ffi;
use std::path::{Path, PathBuf};
use std::time::Instant;

pub struct RunResult {
    pub steps: u64,
    pub seconds: f64,
    pub error: Option<String>,
    pub callbacks: u64,
}

/// `os.path.abspath` -- lexical, never resolving symlinks, because the path
/// has to equal the `co_filename` CPython gives the compiled code.
pub fn abspath(p: &Path) -> PathBuf {
    let joined = if p.is_absolute() {
        p.to_path_buf()
    } else {
        std::env::current_dir().unwrap_or_default().join(p)
    };
    let mut out = PathBuf::new();
    for c in joined.components() {
        match c {
            std::path::Component::ParentDir => { out.pop(); }
            std::path::Component::CurDir => {}
            other => out.push(other.as_os_str()),
        }
    }
    out
}

/// Point the embedded interpreter at its own installation before it starts.
/// Must run before the first GIL acquisition.
pub fn prepare_python() {
    if std::env::var_os("PYTHONHOME").is_none() {
        if let Some(home) = option_env!("NDR_PYTHON_HOME") {
            if Path::new(home).join("lib").exists() || Path::new(home).join("Lib").exists() {
                std::env::set_var("PYTHONHOME", home);
            }
        }
    }
}

/// Everything about running user code that is the same traced or not.
struct Prepared {
    code: Owned,
    ns: Owned,
}

unsafe fn prepare(script: &Path, argv: &[String], quiet: bool) -> Result<Prepared, String> {
    let abs = abspath(script);
    let abs_s = abs.to_string_lossy().to_string();
    let source = std::fs::read_to_string(&abs).map_err(|e| format!("cannot read {}: {e}", abs.display()))?;

    // sys.argv, and the script's own folder on sys.path, as `python x.py` does.
    let sys = Owned::new(ffi::PyImport_ImportModule(c"sys".as_ptr())).ok_or("no sys module")?;
    let list = Owned::new(ffi::PyList_New(0)).ok_or("list")?;
    for a in std::iter::once(abs_s.clone()).chain(argv.iter().cloned()) {
        let s = py::new_str(&a).ok_or("str")?;
        ffi::PyList_Append(list.as_ptr(), s.as_ptr());
    }
    ffi::PyObject_SetAttrString(sys.as_ptr(), c"argv".as_ptr(), list.as_ptr());
    if let Some(path) = py::getattr(sys.as_ptr(), c"path") {
        let dir = abs.parent().map(|d| d.to_string_lossy().to_string()).unwrap_or_default();
        let d = py::new_str(&dir).ok_or("str")?;
        if ffi::PySequence_Contains(path.as_ptr(), d.as_ptr()) != 1 {
            ffi::PyList_Insert(path.as_ptr(), 0, d.as_ptr());
        }
    }
    ffi::PyErr_Clear();

    if quiet {
        // Benchmark runs: keep the program's own output off the screen.
        if let Some(io) = Owned::new(ffi::PyImport_ImportModule(c"io".as_ptr())) {
            if let Some(buf) = py::call0(io.as_ptr(), c"StringIO") {
                ffi::PyObject_SetAttrString(sys.as_ptr(), c"stdout".as_ptr(), buf.as_ptr());
            }
        }
    } else if let Some(orig) = py::getattr(sys.as_ptr(), c"__stdout__") {
        ffi::PyObject_SetAttrString(sys.as_ptr(), c"stdout".as_ptr(), orig.as_ptr());
    }

    // random.seed(0): the same determinism the Python recorder sets up, so a
    // program that uses `random` without seeding records the same run twice.
    if let Some(random) = Owned::new(ffi::PyImport_ImportModule(c"random".as_ptr())) {
        if let Some(seed) = py::getattr(random.as_ptr(), c"seed") {
            let zero = Owned::new(ffi::PyLong_FromLong(0)).ok_or("int")?;
            Owned::new(ffi::PyObject_CallOneArg(seed.as_ptr(), zero.as_ptr()));
        }
    }

    let src = py::new_str(&source).ok_or("source text")?;
    let fname = py::new_str(&abs_s).ok_or("str")?;
    let builtins = Owned::new(ffi::PyImport_ImportModule(c"builtins".as_ptr())).ok_or("builtins")?;
    let compile = py::getattr(builtins.as_ptr(), c"compile").ok_or("compile")?;
    let mode = py::new_str("exec").ok_or("str")?;
    let code = ffi::PyObject_CallFunctionObjArgs(compile.as_ptr(), src.as_ptr(), fname.as_ptr(), mode.as_ptr(), std::ptr::null_mut::<ffi::PyObject>());
    if code.is_null() {
        return Err(format!("{} does not compile: {}", script.display(), recorder::fetch_error()));
    }
    let code = Owned(code);

    // A fresh namespace, so the script behaves like `python script.py`.
    let ns = Owned::new(ffi::PyDict_New()).ok_or("dict")?;
    let main = py::new_str("__main__").ok_or("str")?;
    ffi::PyDict_SetItemString(ns.as_ptr(), c"__name__".as_ptr(), main.as_ptr());
    ffi::PyDict_SetItemString(ns.as_ptr(), c"__file__".as_ptr(), fname.as_ptr());
    ffi::PyDict_SetItemString(ns.as_ptr(), c"__builtins__".as_ptr(), builtins.as_ptr());
    Ok(Prepared { code, ns })
}

/// exec() the prepared code. `Some(message)` if the script raised.
unsafe fn exec(p: &Prepared) -> Option<String> {
    let out = ffi::PyEval_EvalCode(p.code.as_ptr(), p.ns.as_ptr(), p.ns.as_ptr());
    if !out.is_null() {
        ffi::Py_DecRef(out);
        return None;
    }
    // SystemExit(0) / sys.exit() is a normal ending, not an error.
    let exc = ffi::PyErr_GetRaisedException();
    if exc.is_null() {
        return Some("unknown error".into());
    }
    let exc = Owned(exc);
    let tname = py::type_name(ffi::Py_TYPE(exc.as_ptr()));
    if tname == "SystemExit" {
        let code = py::getattr(exc.as_ptr(), c"code");
        let clean = match &code {
            None => true,
            Some(c) => c.as_ptr() == ffi::Py_None() || (ffi::PyLong_Check(c.as_ptr()) != 0 && ffi::PyLong_AsLong(c.as_ptr()) == 0),
        };
        ffi::PyErr_Clear();
        if clean {
            return None;
        }
    }
    let msg = py::str_of(exc.as_ptr()).unwrap_or_default();
    // Show the traceback, as `python script.py` would.
    ffi::PyErr_SetRaisedException(exc.as_ptr());
    ffi::Py_IncRef(exc.as_ptr());
    ffi::PyErr_Print();
    Some(if msg.is_empty() { tname } else { format!("{tname}: {msg}") })
}

unsafe fn flush_std() {
    if let Some(sys) = Owned::new(ffi::PyImport_ImportModule(c"sys".as_ptr())) {
        for s in [c"stdout", c"stderr"] {
            if let Some(stream) = py::getattr(sys.as_ptr(), s) {
                py::call0(stream.as_ptr(), c"flush");
            }
        }
    }
    ffi::PyErr_Clear();
}

/// Record `script` into the trace folder `out`.
pub fn record(script: &Path, argv: &[String], out: &Path, keyframe_every: u64, quiet: bool) -> Result<RunResult, String> {
    let abs = abspath(script);
    let w = Writer::create(out, keyframe_every).map_err(|e| format!("cannot write {}: {e}", out.display()))?;
    pyo3::Python::with_gil(|_py| unsafe {
        let p = prepare(script, argv, quiet)?;
        let started = std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).map(|d| d.as_secs_f64()).unwrap_or(0.0);
        recorder::start(w, vec![abs.to_string_lossy().to_string()])?;
        let t0 = Instant::now();
        let error = exec(&p);
        let seconds = t0.elapsed().as_secs_f64();
        let mut r = recorder::stop().ok_or("recorder vanished")?;
        flush_std();
        r.keyframe();
        let callbacks = r.callbacks;

        let mut w = r_into_writer(r);
        let meta = python_meta();
        w.set_meta("schema_version", "1".into());
        w.set_meta("script", json_str(&abs.to_string_lossy()));
        for (k, v) in meta {
            w.set_meta(k, v);
        }
        w.set_meta("argv", serde_json::to_string(argv).unwrap_or_else(|_| "[]".into()));
        w.set_meta("cwd", json_str(&std::env::current_dir().unwrap_or_default().to_string_lossy()));
        w.set_meta("seed", "0".into());
        w.set_meta("hashseed", std::env::var("PYTHONHASHSEED").map(|s| json_str(&s)).unwrap_or_else(|_| "null".into()));
        w.set_meta("started_at", format!("{started}"));
        w.set_meta("memory", "false".into());
        w.set_meta("memory_seconds", "null".into());
        w.set_meta("driver", json_str("ndr (Rust)"));
        w.set_meta("recorder", json_str(concat!("ndr ", env!("CARGO_PKG_VERSION"))));
        w.set_meta("wall_seconds", format!("{:.4}", seconds));
        w.set_meta("n_steps", format!("{}", w.step));
        w.set_meta("error", error.as_deref().map(json_str).unwrap_or_else(|| "null".into()));
        w.set_meta("peak_bytes", "null".into());
        let steps = w.close().map_err(|e| format!("writing trace.db: {e}"))?;
        Ok(RunResult { steps, seconds, error, callbacks })
    })
}

fn r_into_writer(r: Box<recorder::Recorder>) -> Writer {
    let r = *r;
    r.w
}

/// sys.version, platform.platform(), platform.machine() -- JSON-encoded.
unsafe fn python_meta() -> Vec<(&'static str, String)> {
    let mut out = Vec::new();
    if let Some(sys) = Owned::new(ffi::PyImport_ImportModule(c"sys".as_ptr())) {
        if let Some(v) = py::getattr(sys.as_ptr(), c"version").and_then(|v| py::to_string(v.as_ptr())) {
            out.push(("python", json_str(&v)));
        }
    }
    if let Some(platform) = Owned::new(ffi::PyImport_ImportModule(c"platform".as_ptr())) {
        if let Some(v) = py::call0(platform.as_ptr(), c"platform").and_then(|v| py::to_string(v.as_ptr())) {
            out.push(("platform", json_str(&v)));
        }
        if let Some(v) = py::call0(platform.as_ptr(), c"machine").and_then(|v| py::to_string(v.as_ptr())) {
            out.push(("machine", json_str(&v)));
        }
    }
    ffi::PyErr_Clear();
    out
}

/// Run untraced, best of `repeats`, for the overhead baseline.
pub fn baseline(script: &Path, argv: &[String], repeats: usize) -> Result<f64, String> {
    pyo3::Python::with_gil(|_py| unsafe {
        let mut best = f64::INFINITY;
        for _ in 0..repeats {
            let p = prepare(script, argv, true)?;
            let t0 = Instant::now();
            let _ = exec(&p);
            best = best.min(t0.elapsed().as_secs_f64());
            ffi::PyErr_Clear();
        }
        restore_stdout();
        Ok(best)
    })
}

pub fn restore_stdout() {
    pyo3::Python::with_gil(|_py| unsafe {
        if let Some(sys) = Owned::new(ffi::PyImport_ImportModule(c"sys".as_ptr())) {
            if let Some(orig) = py::getattr(sys.as_ptr(), c"__stdout__") {
                ffi::PyObject_SetAttrString(sys.as_ptr(), c"stdout".as_ptr(), orig.as_ptr());
            }
        }
        ffi::PyErr_Clear();
    })
}

pub fn python_version() -> String {
    pyo3::Python::with_gil(|_py| unsafe {
        Owned::new(ffi::PyImport_ImportModule(c"sys".as_ptr()))
            .and_then(|s| py::getattr(s.as_ptr(), c"version"))
            .and_then(|v| py::to_string(v.as_ptr()))
            .map(|v| v.split_whitespace().next().unwrap_or("").to_string())
            .unwrap_or_default()
    })
}
