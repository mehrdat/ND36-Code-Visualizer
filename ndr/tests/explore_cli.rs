//! `ndr x` from the outside, as a user runs it.
//!
//! 1. Record the bundled sample with `ndr run`, explore it with `ndr x -c`.
//! 2. If the Python `nd` is importable, record the same sample with nd and
//!    check that `nd x` and `ndr x` print the same bytes for a spread of
//!    commands -- the parity the port promises. Skipped, loudly, otherwise.

use std::path::{Path, PathBuf};
use std::process::Command;

fn repo() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR")).join("..")
}

fn scratch(tag: &str) -> PathBuf {
    let d = std::env::temp_dir().join(format!("ndr-xcli-{}-{tag}", std::process::id()));
    let _ = std::fs::remove_dir_all(&d);
    std::fs::create_dir_all(&d).unwrap();
    d
}

fn ndr(args: &[&str], cwd: &Path) -> String {
    let out = Command::new(env!("CARGO_BIN_EXE_ndr")).args(args).current_dir(cwd)
        .env("COLUMNS", "100").output().expect("ndr runs");
    assert!(out.status.success(), "ndr {args:?} failed: {}", String::from_utf8_lossy(&out.stderr));
    String::from_utf8_lossy(&out.stdout).into_owned()
}

fn strip_colour(s: &str) -> String {
    let mut out = String::new();
    let mut it = s.chars();
    while let Some(c) = it.next() {
        if c == '\x1b' {
            for c2 in it.by_ref() {
                if c2 == 'm' {
                    break;
                }
            }
        } else {
            out.push(c);
        }
    }
    out
}

fn python() -> String {
    option_env!("PYO3_PYTHON").unwrap_or("python3").to_string()
}

#[test]
fn explores_a_trace_ndr_recorded() {
    let dir = scratch("own");
    std::fs::copy(repo().join("nd36/samples/sort_demo.py"), dir.join("sort_demo.py")).unwrap();
    ndr(&["run", "sort_demo.py"], &dir);
    let out = strip_colour(&ndr(&["x", "sort_demo.nd", "-c", "afl", "-c", "axv xs", "--command", "s fib;bt"], &dir));
    // Step counts belong to the recorder, so only the shape is pinned here;
    // the parity test below pins the bytes against nd.
    assert!(out.starts_with("function        calls  steps   ms  ←  →  vars\n"), "{out}");
    assert!(out.lines().any(|l| l.starts_with("fib                43 ")), "fib is called 43 times: {out}");
    assert!(out.contains("xs  written ") && out.contains(" in-place, 0 deletes)\n  alive from step "), "{out}");
    assert!(out.contains("read by  (from the source)\n  function        places\n  insertion_sort       7\n"), "{out}");
    assert!(out.contains("in fib\nframe ") && out.contains("fib  sort_demo.py:19"), "{out}");
    assert!(dir.join("sort_demo.nd/analysis.ndr.json").exists(), "the analysis is cached under its own name");
    assert!(!dir.join("sort_demo.nd/analysis.json").exists(), "nd's cache is left alone");

    // With no TRACE, the one trace in the folder is used, as nd does.
    let out = strip_colour(&ndr(&["x", "-c", "i"], &dir));
    assert!(out.starts_with("using sort_demo.nd\nsort_demo.nd\n  script"), "{out}");

    // The prompt, driven through a pipe, as Python's input() reads it.
    let child = Command::new(env!("CARGO_BIN_EXE_ndr")).args(["x", "sort_demo.nd"]).current_dir(&dir)
        .env("COLUMNS", "100").env("HOME", &dir)
        .stdin(std::process::Stdio::piped()).stdout(std::process::Stdio::piped()).spawn().unwrap();
    use std::io::Write;
    child.stdin.as_ref().unwrap().write_all(b"s 300\nc i>5\nq\n").unwrap();
    let out = strip_colour(&String::from_utf8_lossy(&child.wait_with_output().unwrap().stdout));
    assert!(out.contains(" steps, 4 functions, 12 variables\n"), "{out}");
    assert!(out.contains("nd[main sort_demo.py:29]> "), "{out}");
    assert!(out.contains("last time it was true is behind you, step "), "{out}");
    let hist = std::fs::read_to_string(dir.join(".nd36_history")).unwrap_or_default();
    assert_eq!(hist, "", "piped lines are not history, as with readline");
}

#[test]
fn prints_what_nd_x_prints() {
    let ok = Command::new(python()).args(["-c", "import nd36"]).env("PYTHONPATH", repo())
        .status().map(|s| s.success()).unwrap_or(false);
    if !ok {
        eprintln!("SKIPPED: nd36 not importable, so there is no nd x to compare with");
        return;
    }
    let dir = scratch("parity");
    for f in ["sort_demo.py", "bug_demo.py"] {
        std::fs::copy(repo().join("nd36/samples").join(f), dir.join(f)).unwrap();
    }
    let nd = |args: &[&str]| -> String {
        let out = Command::new(python())
            .args(["-c", "import sys; from nd36.cli import main; sys.argv=['nd']+sys.argv[1:]; main()"])
            .args(args).current_dir(&dir).env("PYTHONPATH", repo()).env("COLUMNS", "100")
            .output().expect("nd runs");
        String::from_utf8_lossy(&out.stdout).into_owned()
    };
    nd(&["run", "sort_demo.py"]);
    nd(&["run", "bug_demo.py"]);

    // Every command, with typical arguments and the error cases. `af` on a
    // function with tied variables is left out: nd orders that tie by a
    // Python set, which changes from one process to the next.
    let sort_cmds = [
        "afl", "af fib", "axf fib", "axv xs", "axv seen", "axv nope", "fv", "vl", "ch xs", "ch seen 3",
        "ch nope", "pl xs", "pl total", "pl seen", "tt", "tree", "cmp 100 300", "dis", "i", "?", "??", "? afl",
        "pd", "pv", "pV seen", "bt", "ll", "s 300", "s end", "s 50%", "s fn:fib", "s xs", "s @26", "s :26",
        "s nope", "n x", "f", "F", "u", "c raise", "c i>5", "c xs", "s 300;pv;bt", "/fib", "/zzz", "xs", "zz",
        "1200", "pd 5", "ll 4", "s 296; bt; pv; pd", "s 30;c j<0;c;c",
    ];
    let bug_cmds = ["afl", "axv total", "ch total", "pl total", "c total>50", "s end;pv", "s 60;pV sums", "tree", "fv"];
    let mut diffs = Vec::new();
    for (trace, cmds) in [("sort_demo.nd", &sort_cmds[..]), ("bug_demo.nd", &bug_cmds[..])] {
        for c in cmds {
            let a = nd(&["x", trace, "-c", c]);
            let b = ndr(&["x", trace, "-c", c], &dir);
            if a != b {
                diffs.push(format!("[{trace}: {c}]\n--- nd\n{a}\n--- ndr\n{b}"));
            }
        }
    }
    assert!(diffs.is_empty(), "{} commands differ:\n{}", diffs.len(), diffs.join("\n"));
}
