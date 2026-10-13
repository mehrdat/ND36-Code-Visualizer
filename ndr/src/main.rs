//! ndr -- ND36's recorder, in Rust.
//!
//!     ndr run    FILE.py [args...]   record a program        -> FILE.nd
//!     ndr bench  FILE.py             what recording costs
//!     ndr verify FILE.py             same trace as the Python recorder?
//!     ndr info   TRACE               what is in a trace
//!     ndr watch  TRACE [NAME]        one variable's whole life
//!     ndr x      TRACE [-c CMD]...   explore a run (the `nd x` prompt)
//!
//! The traces are the same format `nd` writes, so everything else --
//! `nd x`, `nd flow`, `nd heat`, `nd report` -- reads them unchanged.

mod encode;
mod explore;
mod py;
mod reader;
mod recorder;
mod run;
mod schema;
mod verify;
mod writer;

mod ui {
    pub const DIM: &str = "\x1b[2m";
    pub const BOLD: &str = "\x1b[1m";
    pub const YELLOW: &str = "\x1b[33m";
    pub const GREEN: &str = "\x1b[32m";
    pub const RED: &str = "\x1b[31m";
    pub const OFF: &str = "\x1b[0m";

    pub fn fmt_n(n: u64) -> String {
        let s = n.to_string();
        let mut out = String::new();
        for (i, c) in s.chars().enumerate() {
            if i > 0 && (s.len() - i) % 3 == 0 {
                out.push(',');
            }
            out.push(c);
        }
        out
    }
}

use reader::Trace;
use std::path::{Path, PathBuf};
use std::process::ExitCode;
use ui::*;

const HELP: &str = "\
ndr — ND36's recorder, rewritten in Rust

  ndr run    FILE.py [args…]     record a program          → FILE.nd
  ndr bench  FILE.py             what recording costs, per step
  ndr verify FILE.py             record with ndr AND nd, compare them
  ndr info   TRACE               what is in a trace
  ndr watch  TRACE [NAME]        one variable's whole life
  ndr x      TRACE [-c CMD]…     explore a run: functions, variables, connections

The traces are the same format nd writes, so nd reads them too:
  ndr run anneal.py
  ndr x anneal.nd               (or: nd x anneal.nd)

options
  run     -o, --out PATH         where to write the trace   (default FILE.nd)
          --keyframe-every N     steps between snapshots    (default 2000)
          anything else after FILE goes to your program; `--` forces it
  bench   --compare              also time nd (Python) on the same program
  watch   --limit N              how many changes to show   (default 50)
  x       -c, --command CMD      run a command and exit, instead of the prompt.
                                 repeatable: -c afl -c 'axv total'
                                 inside the prompt, ? lists the commands

what verify compares
  must match  every line run, in order · calls per function · every value
              each variable took (ints, floats, strings, bools, None)
  may differ  in-place changes (MUTATE): nd stops re-checking a container
              after 150 looks, and once that budget runs out it also reports
              changes on lines that change nothing. ndr hashes what each
              line names (and anything holding what changed), with no
              cut-off, so it sees more and reports them on the line that
              actually did it.
              deletions: nd reports module globals as deleted while a list
              comprehension runs at module level (a Python 3.12+ quirk in
              how it reads frame locals). ndr does not.
";

fn die(msg: &str) -> ExitCode {
    eprintln!("{RED}{msg}{OFF}");
    let here = reader::traces_here();
    if !here.is_empty() && (msg.contains("trace") || msg.contains("exist")) {
        eprintln!("\n{BOLD}traces in this folder:{OFF}");
        for t in here.iter().take(10) {
            eprintln!("  {t}");
        }
    }
    ExitCode::from(1)
}

fn default_out(script: &Path) -> PathBuf {
    let stem = script.file_stem().map(|s| s.to_string_lossy().to_string()).unwrap_or_else(|| "trace".into());
    PathBuf::from(format!("{stem}.nd"))
}

fn need_program(arg: Option<&String>, cmd: &str) -> Result<PathBuf, String> {
    let Some(a) = arg else {
        return Err(format!("{cmd} needs a program.  e.g.  ndr {cmd} yourscript.py"));
    };
    let p = PathBuf::from(a);
    if p.is_dir() {
        return Err(format!("{cmd} takes a program, not a trace folder.\n  ndr {cmd} {}",
                           Trace::open(&p).ok().and_then(|t| t.meta("script").and_then(|s| s.as_str().map(|s| {
                               Path::new(s).file_name().map(|f| f.to_string_lossy().to_string()).unwrap_or_default()
                           }))).unwrap_or_else(|| "yourscript.py".into())));
    }
    if !p.exists() {
        return Err(format!("{} does not exist.", p.display()));
    }
    Ok(p)
}

/// Split `run` arguments into ours and the program's, keeping order --
/// the same rule as `nd run`.
fn split_run(args: &[String]) -> (Vec<String>, Vec<String>) {
    let (mut ours, mut theirs) = (Vec::new(), Vec::new());
    let mut script_seen = false;
    let mut i = 0;
    while i < args.len() {
        let a = &args[i];
        if a == "--" {
            theirs.extend_from_slice(&args[i + 1..]);
            break;
        }
        let head = a.split('=').next().unwrap_or("");
        let takes_value = matches!(head, "-o" | "--out" | "--keyframe-every");
        if a.starts_with('-') && (takes_value || a == "-h" || a == "--help") {
            ours.push(a.clone());
            if takes_value && !a.contains('=') && i + 1 < args.len() {
                ours.push(args[i + 1].clone());
                i += 1;
            }
        } else if a.starts_with('-') || script_seen {
            theirs.push(a.clone());
        } else {
            script_seen = true;
            ours.push(a.clone());
        }
        i += 1;
    }
    (ours, theirs)
}

fn opt(ours: &[String], names: &[&str]) -> Option<String> {
    for (i, a) in ours.iter().enumerate() {
        for n in names {
            if a == n {
                return ours.get(i + 1).cloned();
            }
            if let Some(v) = a.strip_prefix(&format!("{n}=")) {
                return Some(v.to_string());
            }
        }
    }
    None
}

fn positional(ours: &[String]) -> Vec<String> {
    let mut out = Vec::new();
    let mut skip = false;
    for a in ours {
        if skip { skip = false; continue; }
        if matches!(a.as_str(), "-o" | "--out" | "--keyframe-every" | "--limit") { skip = true; continue; }
        if a.starts_with('-') { continue; }
        out.push(a.clone());
    }
    out
}

fn cmd_run(args: &[String]) -> ExitCode {
    let (ours, theirs) = split_run(args);
    let pos = positional(&ours);
    let script = match need_program(pos.first(), "run") {
        Ok(p) => p,
        Err(e) => return die(&e),
    };
    let out = opt(&ours, &["-o", "--out"]).map(PathBuf::from).unwrap_or_else(|| default_out(&script));
    let every = opt(&ours, &["--keyframe-every"]).and_then(|s| s.parse().ok()).unwrap_or(2000);
    if !theirs.is_empty() {
        println!("{DIM}passing to {}:{OFF} {}", script.file_name().unwrap_or_default().to_string_lossy(), theirs.join(" "));
    }
    match run::record(&script, &theirs, &out, every, false) {
        Ok(r) => {
            let size = Trace::open(&out).map(|t| t.size_bytes()).unwrap_or(0);
            println!("{GREEN}{}{OFF}  {} steps  {:.2}s  {:.1} MB  {DIM}({:.2} µs/step, ndr){OFF}",
                     out.display(), fmt_n(r.steps), r.seconds, size as f64 / 1e6,
                     if r.steps > 0 { r.seconds / r.steps as f64 * 1e6 } else { 0.0 });
            if let Some(e) = r.error {
                println!("{YELLOW}script raised:{OFF} {e}");
            }
            println!("{DIM}explore it:{OFF}  ndr x {}", out.display());
            ExitCode::SUCCESS
        }
        Err(e) => die(&e),
    }
}

fn cmd_bench(args: &[String]) -> ExitCode {
    let compare = args.iter().any(|a| a == "--compare");
    let pos = positional(args);
    let script = match need_program(pos.first(), "bench") {
        Ok(p) => p,
        Err(e) => return die(&e),
    };
    let base = match run::baseline(&script, &[], 3) {
        Ok(b) => b,
        Err(e) => return die(&e),
    };
    let tmp = std::env::temp_dir().join(format!("ndr-bench-{}.nd", std::process::id()));
    let r = match run::record(&script, &[], &tmp, 2000, true) {
        Ok(r) => r,
        Err(e) => return die(&e),
    };
    run::restore_stdout();
    let size = Trace::open(&tmp).map(|t| t.size_bytes()).unwrap_or(0);
    let steps = r.steps.max(1);
    let per = r.seconds / steps as f64 * 1e6;
    println!("{:<22} ndr (Rust), Python {}", "recorder", run::python_version());
    println!("{:<22} {:.4} s", "untraced", base);
    println!("{:<22} {:.4} s", "traced", r.seconds);
    println!("{:<22} {:.1}×", "slowdown", r.seconds / base.max(1e-6));
    println!("{BOLD}{:<22} {:.3} µs{OFF}", "cost per step", per);
    println!("{:<22} {}", "steps", fmt_n(r.steps));
    println!("{:<22} {:.0} ns", "callback dispatches", r.seconds / r.callbacks.max(1) as f64 * 1e9);
    println!("{:<22} {:.1} MB  ({:.0} bytes/step)", "trace size", size as f64 / 1e6, size as f64 / steps as f64);
    let _ = std::fs::remove_dir_all(&tmp);

    if compare {
        println!("\n{DIM}now the Python recorder on the same program…{OFF}");
        let pt = std::env::temp_dir().join(format!("ndr-bench-py-{}.nd", std::process::id()));
        let ok = std::process::Command::new("nd").arg("run").arg(&script).arg("-o").arg(&pt)
            .stdout(std::process::Stdio::null()).stderr(std::process::Stdio::null()).status()
            .map(|s| s.success()).unwrap_or(false);
        if ok {
            if let Ok(t) = Trace::open(&pt) {
                let secs = t.meta("wall_seconds").and_then(|v| v.as_f64()).unwrap_or(0.0);
                let n = t.n_steps.max(1);
                let pper = secs / n as f64 * 1e6;
                println!("{:<22} {:.3} µs  ({} steps, {:.2} s)", "nd (Python)", pper, fmt_n(n), secs);
                println!("{BOLD}{GREEN}{:<22} {:.1}× faster per step{OFF}", "ndr vs nd", pper / per.max(1e-9));
            }
        } else {
            println!("{YELLOW}could not run `nd` — install the Python version to compare.{OFF}");
        }
        let _ = std::fs::remove_dir_all(&pt);
    }
    ExitCode::SUCCESS
}

fn open_or_die(arg: Option<&String>) -> Result<Trace, ExitCode> {
    let path = match arg {
        Some(p) => PathBuf::from(p),
        None => {
            let here = reader::traces_here();
            if here.len() == 1 {
                println!("{DIM}using {}{OFF}", here[0]);
                PathBuf::from(&here[0])
            } else if here.is_empty() {
                return Err(die("no trace given, and none in this folder. make one:\n  ndr run yourscript.py"));
            } else {
                return Err(die("no trace given, and there is more than one here. name the one you mean."));
            }
        }
    };
    Trace::open(&path).map_err(|e| die(&e))
}

fn cmd_info(args: &[String]) -> ExitCode {
    let pos = positional(args);
    let t = match open_or_die(pos.first()) {
        Ok(t) => t,
        Err(c) => return c,
    };
    println!("{BOLD}steps{OFF}        {}", fmt_n(t.n_steps));
    for key in ["script", "python", "platform", "driver", "wall_seconds", "error", "seed"] {
        if let Some(v) = t.meta(key) {
            let text = match v {
                serde_json::Value::String(s) => s.clone(),
                serde_json::Value::Null => "None".into(), // as `nd info` prints it
                other => other.to_string(),
            };
            println!("{:<13}{}", key, text);
        }
    }
    let mut counts: std::collections::BTreeMap<u8, u64> = Default::default();
    t.each(|_, r| *counts.entry(r.kind).or_insert(0) += 1);
    let peak = counts.values().copied().max().unwrap_or(1).max(1);
    let mut rows: Vec<_> = counts.into_iter().collect();
    rows.sort_by(|a, b| b.1.cmp(&a.1));
    println!("\n{BOLD}events{OFF}");
    for (k, n) in rows {
        println!("  {:<8} {:>10}  {}", schema::kind_name(k), fmt_n(n), "▇".repeat((30 * n / peak) as usize));
    }
    ExitCode::SUCCESS
}

fn cmd_watch(args: &[String]) -> ExitCode {
    let limit: usize = opt(args, &["--limit"]).and_then(|s| s.parse().ok()).unwrap_or(50);
    let pos = positional(args);
    // `ndr watch trace anneal.nd`: the docs' placeholder typed literally.
    let (trace_arg, name) = match (pos.first(), pos.get(1)) {
        (Some(a), Some(b)) if !Path::new(a).exists() && Path::new(b).is_dir() => {
            println!("{DIM}reading {b}  (the arguments were the other way round){OFF}");
            (Some(b.clone()), None)
        }
        (a, b) => (a.cloned(), b.cloned()),
    };
    let t = match open_or_die(trace_arg.as_ref()) {
        Ok(t) => t,
        Err(c) => return c,
    };
    let base = t.dir.file_name().map(|s| s.to_string_lossy().to_string()).unwrap_or_default();
    let counts = t.variable_counts();

    let Some(name) = name else {
        println!("{BOLD}{} variables were assigned in this trace{OFF}", counts.len());
        println!("{DIM}{:<22} times assigned{OFF}", "name");
        for (n, c) in counts.iter().take(40) {
            println!("  {:<22} {}", n, fmt_n(*c));
        }
        if counts.len() > 40 {
            println!("  {DIM}… and {} more{OFF}", counts.len() - 40);
        }
        if let Some((first, _)) = counts.first() {
            println!("\n{BOLD}follow one:{OFF}  ndr watch {base} {first}");
        }
        return ExitCode::SUCCESS;
    };

    let steps = t.steps_of(&name, Some(limit));
    if steps.is_empty() {
        println!("{YELLOW}no variable called {name:?} was assigned in this trace.{OFF}");
        let low = name.to_lowercase();
        let near: Vec<&String> = counts.iter().map(|(n, _)| n)
            .filter(|n| n.to_lowercase().contains(&low) || (n.len() >= 3 && low.contains(&n.to_lowercase())))
            .take(8).collect();
        if !near.is_empty() {
            println!("\n{BOLD}did you mean:{OFF}");
            for n in near {
                println!("  ndr watch {base} {n}");
            }
        } else {
            println!("\n{BOLD}these exist:{OFF} {}", counts.iter().take(12).map(|(n, _)| n.as_str()).collect::<Vec<_>>().join(", "));
        }
        return ExitCode::from(1);
    }
    println!("{BOLD}{:<10} {:<18} {:<7} {}{OFF}", "step", "where", "how", name);
    let mut prev: Option<schema::Vref> = None;
    for s in &steps {
        let Some(r) = t.rec(*s) else { continue };
        let (f, l) = t.place(r.loc);
        let how = schema::kind_name(r.kind);
        let colour = if r.kind == schema::MUTATE { YELLOW } else { "" };
        let mut val = t.show(&r.v, *s);
        if val.chars().count() > 60 {
            val = val.chars().take(60).collect::<String>() + "…";
        }
        let was = match prev {
            Some(p) if p.rk != schema::RK_OBJ && encode::is_scalar(&p) => format!("  {DIM}(was {}){OFF}", t.show(&p, *s)),
            _ => String::new(),
        };
        println!("{:<10} {:<18} {colour}{:<7}{OFF} {val}{was}", fmt_n(*s), format!("{f}:{l}"), how);
        prev = Some(r.v);
    }
    let total = counts.iter().find(|(n, _)| *n == name).map(|(_, c)| *c).unwrap_or(0);
    if total as usize > steps.len() {
        println!("{DIM}showing {} of {} changes. --limit {} for more.{OFF}", steps.len(), fmt_n(total), limit * 4);
    }
    ExitCode::SUCCESS
}

/// `ndr x [TRACE] [-c CMD]...`: the explorer, as `nd x`.
fn cmd_x(args: &[String]) -> ExitCode {
    let mut commands = Vec::new();
    let mut pos = Vec::new();
    let mut i = 0;
    while i < args.len() {
        let a = &args[i];
        if a == "-c" || a == "--command" {
            // As argparse: a value that starts with '-' is taken for an
            // option unless it reads as a negative number (`-c -5`).
            let negative = |v: &str| {
                let b = &v[1..];
                !b.is_empty() && (b.chars().all(|c| c.is_ascii_digit())
                    || b.split_once('.').is_some_and(|(x, y)| x.chars().all(|c| c.is_ascii_digit())
                        && !y.is_empty() && y.chars().all(|c| c.is_ascii_digit())))
            };
            match args.get(i + 1) {
                Some(c) if !c.starts_with('-') || negative(c) => commands.push(c.clone()),
                _ => return die(&format!("{a} needs a command after it, e.g.  -c afl")),
            }
            i += 2;
            continue;
        }
        if let Some(c) = a.strip_prefix("--command=") {
            commands.push(c.to_string());
        } else if let Some(c) = a.strip_prefix("-c").filter(|c| !c.is_empty() && !a.starts_with("--")) {
            commands.push(c.to_string());
        } else {
            pos.push(a.clone());
        }
        i += 1;
    }
    let t = match open_or_die(pos.first()) {
        Ok(t) => t,
        Err(c) => return c,
    };
    let path = pos.first().cloned().unwrap_or_else(|| t.dir.to_string_lossy().to_string());
    let store = explore::store::Store::open(t);
    if commands.is_empty() {
        explore::repl(store, &path);
    } else {
        explore::once(store, &path, &commands);
    }
    ExitCode::SUCCESS
}

fn cmd_verify(args: &[String]) -> ExitCode {
    let (ours, theirs) = split_run(args);
    let pos = positional(&ours);
    let script = match need_program(pos.first(), "verify") {
        Ok(p) => p,
        Err(e) => return die(&e),
    };
    match verify::verify(&script, &theirs) {
        Ok(true) => ExitCode::SUCCESS,
        Ok(false) => ExitCode::from(1),
        Err(e) => die(&e),
    }
}

fn main() -> ExitCode {
    run::prepare_python();
    let args: Vec<String> = std::env::args().skip(1).collect();
    let Some(cmd) = args.first() else {
        print!("{HELP}");
        return ExitCode::SUCCESS;
    };
    let rest = &args[1..];
    if rest.iter().any(|a| a == "-h" || a == "--help") && cmd != "run" {
        print!("{HELP}");
        return ExitCode::SUCCESS;
    }
    match cmd.as_str() {
        "run" => cmd_run(rest),
        "bench" => cmd_bench(rest),
        "verify" => cmd_verify(rest),
        "info" => cmd_info(rest),
        "watch" => cmd_watch(rest),
        "x" | "explore" => cmd_x(rest),
        "-h" | "--help" | "help" => { print!("{HELP}"); ExitCode::SUCCESS }
        "-V" | "--version" => { println!("ndr {}", env!("CARGO_PKG_VERSION")); ExitCode::SUCCESS }
        other => {
            let hint = if other.ends_with(".py") { format!("\ndid you mean:  ndr run {other}") } else { String::new() };
            eprintln!("{RED}no command {other:?}.{OFF}{hint}\n");
            eprint!("{HELP}");
            ExitCode::from(2)
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn v(xs: &[&str]) -> Vec<String> {
        xs.iter().map(|s| s.to_string()).collect()
    }

    #[test]
    fn unknown_switches_go_to_the_program() {
        let (ours, theirs) = split_run(&v(&["server.py", "--port", "8080"]));
        assert_eq!(ours, v(&["server.py"]));
        assert_eq!(theirs, v(&["--port", "8080"]));
        let (ours, theirs) = split_run(&v(&["-o", "x.nd", "s.py", "-v"]));
        assert_eq!(ours, v(&["-o", "x.nd", "s.py"]));
        assert_eq!(theirs, v(&["-v"]));
        let (_, theirs) = split_run(&v(&["s.py", "--", "-o", "y"]));
        assert_eq!(theirs, v(&["-o", "y"]));
    }

    #[test]
    fn thousands_separators() {
        assert_eq!(fmt_n(0), "0");
        assert_eq!(fmt_n(999), "999");
        assert_eq!(fmt_n(1000), "1,000");
        assert_eq!(fmt_n(1467162), "1,467,162");
    }

    // -- recording, end to end, through the embedded interpreter -------------
    //
    // One interpreter per process and one global recorder, so these must not
    // run in parallel: every test that records takes this lock first.
    static PY: std::sync::Mutex<()> = std::sync::Mutex::new(());

    fn record_src(name: &str, src: &str) -> Trace {
        let _g = PY.lock().unwrap_or_else(|e| e.into_inner());
        run::prepare_python();
        let dir = std::env::temp_dir().join(format!("ndr-test-{}-{name}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).unwrap();
        let script = dir.join(format!("{name}.py"));
        std::fs::write(&script, src).unwrap();
        let out = dir.join(format!("{name}.nd"));
        run::record(&script, &[], &out, 2000, true).expect("recording failed");
        run::restore_stdout();
        Trace::open(&out).expect("trace did not open")
    }

    #[test]
    fn fast_repr_matches_python_repr() {
        let _g = PY.lock().unwrap_or_else(|e| e.into_inner());
        run::prepare_python();
        pyo3::Python::with_gil(|_| unsafe {
            let src = c"[None, True, False, 0, -7, 2**62, 1.0, -0.0, 0.1, 2.5e-3, 123456.789, 1e15, 1e16, 1e-5, \
                        float('nan'), 'ab c', \"it's\", 'a\\\\b', 'caf\\u00e9', (), (1,), (1, (2, 3)), [1, [2]], \
                        [[[1]]], (1, 2, 3, 4, 5, 6, 7, 8, 9), {'k': 1}, 'x' * 70, list(range(40)), [str(i) for i in range(30)], \
                        object(), type('P', (), {})(), type('Q', (), {'__repr__': lambda s: 'q!'})()]";
            let code = pyo3::ffi::Py_CompileString(src.as_ptr(), c"<t>".as_ptr(), pyo3::ffi::Py_eval_input);
            assert!(!code.is_null());
            let g = pyo3::ffi::PyDict_New();
            pyo3::ffi::PyDict_SetItemString(g, c"__builtins__".as_ptr(), pyo3::ffi::PyEval_GetBuiltins());
            let list = pyo3::ffi::PyEval_EvalCode(code, g, g);
            assert!(!list.is_null());
            for i in 0..pyo3::ffi::PyList_Size(list) {
                let item = pyo3::ffi::PyList_GetItem(list, i);
                assert_eq!(encode::repr_of(item), py::repr(item), "item {i}");
            }
        });
    }

    #[test]
    fn an_alias_that_the_line_does_not_name_still_mutates_on_that_line() {
        // `b` is never named on line 3, but it IS `a`: both must MUTATE there.
        let t = record_src("alias", "def f():\n    a = [1]; b = a\n    a.append(2)\n    return b\nf()\n");
        assert!(kinds_for(&t, "b").contains(&"MUTATE"), "b: {:?}", kinds_for(&t, "b"));
        assert!(kinds_for(&t, "a").contains(&"MUTATE"));
    }

    #[test]
    fn an_unnamed_container_changed_by_a_callee_is_still_caught() {
        // `log` is changed inside g() via a closure-free global path; f's
        // own `seen` is changed through an alias held by an object. Back-off
        // must still report it, at worst a few lines late.
        let src = "class Box:\n    pass\n\ndef f():\n    seen = []\n    box = Box()\n    box.items = seen\n    for i in range(40):\n        box.items.append(i)\n        x = i\n    return len(seen)\nf()\n";
        let t = record_src("backoff", src);
        let m = kinds_for(&t, "seen").iter().filter(|k| **k == "MUTATE").count();
        assert!(m >= 5, "seen mutated only {m} times");
    }

    fn kinds_for(t: &Trace, name: &str) -> Vec<&'static str> {
        t.steps_of(name, None).iter().filter_map(|s| t.rec(*s)).map(|r| schema::kind_name(r.kind)).collect()
    }

    #[test]
    fn records_a_simple_program_correctly() {
        let t = record_src("simple", "x = 1\nfor i in range(3):\n    x += i\ny = [x]\ny.append(5)\n");
        assert!(t.n_steps > 10, "only {} steps", t.n_steps);
        let xs: Vec<String> = t.steps_of("x", None).iter()
            .filter_map(|s| t.rec(*s).map(|r| t.show(&r.v, *s))).collect();
        // x = 1; x += 0 (no change, so no event); x += 1 -> 2; x += 2 -> 4
        assert_eq!(xs, vec!["1", "2", "4"], "x took the wrong values");
        assert_eq!(kinds_for(&t, "y"), vec!["BIND", "MUTATE"], "y.append(5) must be a MUTATE");
        assert_eq!(t.meta("error"), Some(&serde_json::Value::Null));
    }

    #[test]
    fn a_loop_on_one_line_counts_every_iteration() {
        // sys.monitoring does not fire LINE for a backward jump to the same
        // line. Without the JUMP handling this records 1 iteration, not 4.
        let t = record_src("oneline", "n = 0\nwhile n < 4: n += 1\n");
        let mut hits = 0;
        t.each(|_, r| if r.kind == schema::LINE && t.place(r.loc).1 == 2 { hits += 1 });
        assert!(hits >= 4, "line 2 recorded {hits} times, expected one per iteration");
        let ns: Vec<String> = t.steps_of("n", None).iter()
            .filter_map(|s| t.rec(*s).map(|r| t.show(&r.v, *s))).collect();
        assert_eq!(ns, vec!["0", "1", "2", "3", "4"]);
    }

    #[test]
    fn both_aliases_see_an_in_place_change() {
        // The aliasing bug from anneal.py, in three lines. The Python recorder
        // reports whichever name it checks first; ndr reports both.
        let t = record_src("alias", "def f():\n    a = []\n    b = a\n    a.append(1)\n    return b\nf()\n");
        assert_eq!(kinds_for(&t, "a"), vec!["BIND", "MUTATE"]);
        assert_eq!(kinds_for(&t, "b"), vec!["BIND", "MUTATE"], "the alias missed the change");
    }

    #[test]
    fn a_crashing_program_still_leaves_a_trace() {
        let t = record_src("crash", "def g(d):\n    return d['missing']\nx = 5\ng({})\n");
        let err = t.meta("error").and_then(|v| v.as_str()).unwrap_or("");
        assert!(err.starts_with("KeyError"), "error was {err:?}");
        let mut raised = 0;
        let mut unwound = 0;
        t.each(|_, r| {
            if r.kind == schema::RAISE { raised += 1; }
            if r.kind == schema::UNWIND { unwound += 1; }
        });
        assert!(raised >= 1 && unwound >= 1, "RAISE {raised}, UNWIND {unwound}");
        assert_eq!(t.steps_of("x", None).len(), 1, "what ran before the crash is kept");
    }

    #[test]
    fn mutation_through_a_function_call_is_caught() {
        // `swap(tour, i, j)`: the change happens in another frame. The line
        // that made the call must be where the caller's MUTATE lands.
        let t = record_src("swap", "def swap(t, i, j):\n    t[i], t[j] = t[j], t[i]\nxs = [1, 2, 3]\nswap(xs, 0, 2)\nprint(xs)\n");
        let steps = t.steps_of("xs", None);
        let ev: Vec<(&str, i64)> = steps.iter().filter_map(|s| t.rec(*s))
            .map(|r| (schema::kind_name(r.kind), t.place(r.loc).1)).collect();
        assert_eq!(ev, vec![("BIND", 3), ("MUTATE", 4)], "events for xs: {ev:?}");
    }

    #[test]
    fn agrees_with_the_python_recorder_on_every_fixture() {
        // The reference check. Skipped, loudly, if nd36 is not importable.
        let repo = std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("..");
        let ok = std::process::Command::new(option_env!("PYO3_PYTHON").unwrap_or("python3"))
            .args(["-c", "import nd36"]).env("PYTHONPATH", &repo).status()
            .map(|s| s.success()).unwrap_or(false);
        if !ok {
            eprintln!("SKIPPED: nd36 not importable, so there is no reference to compare with");
            return;
        }
        std::env::set_var("PYTHONPATH", &repo);
        let _g = PY.lock().unwrap_or_else(|e| e.into_inner());
        run::prepare_python();
        let progs = repo.join("tests").join("programs");
        let mut failed = Vec::new();
        for name in ["sort.py", "mixed.py", "mutating.py", "realistic.py"] {
            let p = progs.join(name);
            if !p.exists() { continue; }
            match verify::verify(&p, &[]) {
                Ok(true) => {}
                Ok(false) => failed.push(name.to_string()),
                Err(e) => failed.push(format!("{name}: {e}")),
            }
        }
        let sample = repo.join("nd36").join("samples").join("sort_demo.py");
        if !matches!(verify::verify(&sample, &[]), Ok(true)) {
            failed.push("sort_demo.py".into());
        }
        assert!(failed.is_empty(), "ndr disagrees with nd on: {failed:?}");
    }
}
