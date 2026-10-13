//! The explorer over a hand-built trace.
//!
//! The fixture is written byte by byte (events.bin + trace.db), so these
//! tests pin the explorer's behaviour without depending on either recorder.
//! The program it stands for:
//!
//! ```text
//!  1  def f(n):
//!  2      total = 0
//!  3      for i in range(n):
//!  4          total += i
//!  5      return total
//!  6  xs = []
//!  7  r = f(3)
//!  8  xs.append(r)
//! ```

use super::*;
use crate::schema::{pack, Vref};
use std::path::PathBuf;

const SRC: &str = "def f(n):\n    total = 0\n    for i in range(n):\n        total += i\n    return total\nxs = []\nr = f(3)\nxs.append(r)\n";

const F: u32 = 1;
const XS: u32 = 2;
const TOTAL: u32 = 3;
const I: u32 = 4;
const RR: u32 = 5;

/// (kind, frame, line, name, value, payload)
fn events() -> Vec<(u8, u32, u32, u32, Vref, u32)> {
    use schema::*;
    let none = Vref::ABSENT;
    vec![
        (CALL, 1, 1, 0, none, 1),
        (LINE, 1, 1, 0, none, 0),
        (BIND, 1, 1, F, Vref::obj(10), 0),
        (LINE, 1, 6, 0, none, 0),
        (BIND, 1, 6, XS, Vref::obj(11), 0),
        (LINE, 1, 7, 0, none, 0),
        (CALL, 2, 1, 0, none, 2),
        (LINE, 2, 2, 0, none, 0),
        (BIND, 2, 2, TOTAL, Vref::int(0), 0),
        (LINE, 2, 3, 0, none, 0),
        (BIND, 2, 3, I, Vref::int(0), 0),
        (LINE, 2, 4, 0, none, 0), // 12: keyframe
        (LINE, 2, 3, 0, none, 0),
        (BIND, 2, 3, I, Vref::int(1), 0),
        (LINE, 2, 4, 0, none, 0),
        (BIND, 2, 4, TOTAL, Vref::int(1), 0),
        (LINE, 2, 3, 0, none, 0),
        (BIND, 2, 3, I, Vref::int(2), 0),
        (LINE, 2, 4, 0, none, 0),
        (BIND, 2, 4, TOTAL, Vref::int(3), 0), // 20
        (LINE, 2, 5, 0, none, 0),
        (RETURN, 2, 5, 0, Vref::int(3), 0),
        (BIND, 1, 7, RR, Vref::int(3), 0),
        (LINE, 1, 8, 0, none, 0),
        (MUTATE, 1, 8, XS, Vref::obj(11), 0), // 25
        (RETURN, 1, 8, 0, Vref::NULL, 0),
    ]
}

fn build(dir: &std::path::Path) {
    let _ = std::fs::remove_dir_all(dir);
    std::fs::create_dir_all(dir).unwrap();
    let mut buf = Vec::new();
    let mut hits = std::collections::BTreeMap::<u32, i64>::new();
    let mut binds = Vec::new();
    for (i, (k, fr, line, name, v, pid)) in events().into_iter().enumerate() {
        let step = i as i64 + 1;
        pack(&mut buf, 1_000_000 * step as u64, k, fr, line, name, v, pid);
        if k == schema::LINE {
            *hits.entry(line).or_default() += 1;
        }
        if matches!(k, schema::BIND | schema::MUTATE | schema::DELETE) {
            binds.push((name, step));
        }
    }
    std::fs::write(dir.join("events.bin"), &buf).unwrap();

    let db = rusqlite::Connection::open(dir.join("trace.db")).unwrap();
    db.execute_batch(schema::DDL).unwrap();
    let path = "/t/prog.py";
    db.execute("INSERT INTO files VALUES (1, ?1, ?2)", rusqlite::params![path, SRC]).unwrap();
    for line in 1..=8 {
        db.execute("INSERT INTO locs VALUES (?1, 1, ?1, NULL, NULL)", [line]).unwrap();
    }
    db.execute("INSERT INTO funcs VALUES (1, '<module>', '<module>', 1, 1)", []).unwrap();
    db.execute("INSERT INTO funcs VALUES (2, 'f', 'f', 1, 1)", []).unwrap();
    for (id, n) in [(F, "f"), (XS, "xs"), (TOTAL, "total"), (I, "i"), (RR, "r")] {
        db.execute("INSERT INTO names VALUES (?1, ?2)", rusqlite::params![id, n]).unwrap();
    }
    db.execute("INSERT INTO payloads VALUES (1, '{\"func\":1,\"args\":{},\"scope\":\"global\"}')", []).unwrap();
    db.execute("INSERT INTO payloads VALUES (2, '{\"func\":2,\"args\":{\"n\":[1,3,0.0]},\"scope\":\"local\"}')", []).unwrap();
    db.execute("INSERT INTO objects VALUES (10, 3, 'function', '{\"type\":\"function\",\"kind\":\"generic\"}', NULL, '<function f at 0x1>', NULL)", []).unwrap();
    db.execute("INSERT INTO objects VALUES (11, 5, 'list', '{\"type\":\"list\",\"kind\":\"generic\",\"len\":0}', '[]', '[]', 56)", []).unwrap();
    db.execute("INSERT INTO objects VALUES (11, 25, 'list', '{\"type\":\"list\",\"kind\":\"generic\",\"len\":1}', '[[0,[1,3,0.0]]]', '[3]', 64)", []).unwrap();
    for (name, step) in binds {
        db.execute("INSERT INTO binds VALUES (?1, ?2)", rusqlite::params![name, step]).unwrap();
    }
    for (loc, n) in hits {
        db.execute("INSERT INTO linehits VALUES (?1, ?2, ?3)", rusqlite::params![loc, n, n * 1_500_000]).unwrap();
    }
    for (k, v) in [("script", "\"/t/prog.py\""), ("python", "\"3.13.0\""), ("driver", "\"handmade\""),
                   ("wall_seconds", "0.0125"), ("error", "null")] {
        db.execute("INSERT INTO meta VALUES (?1, ?2)", [k, v]).unwrap();
    }
    // A keyframe the way nd writes one: the module frame's func is null.
    let kf = r#"{"frames":[{"frame":1,"func":null,"scope":"global","loc":7,"locals":{"f":[5,10,0.0],"xs":[5,11,0.0]}},{"frame":2,"func":2,"scope":"local","loc":4,"locals":{"n":[1,3,0.0],"total":[1,0,0.0],"i":[1,0,0.0]}}],"globals":{}}"#;
    let mut z = flate2::write::ZlibEncoder::new(Vec::new(), flate2::Compression::fast());
    z.write_all(kf.as_bytes()).unwrap();
    db.execute("INSERT INTO keyframes VALUES (12, ?1)", [z.finish().unwrap()]).unwrap();
}

fn fixture(tag: &str) -> PathBuf {
    let dir = std::env::temp_dir().join(format!("ndr-x-{}-{tag}.nd", std::process::id()));
    build(&dir);
    dir
}

fn session(tag: &str) -> Explorer {
    std::env::set_var("COLUMNS", "100");
    let dir = fixture(tag);
    let t = crate::reader::Trace::open(&dir).unwrap();
    Explorer::new(Store::open(t), dir.to_str().unwrap(), true, false)
}

/// Run commands and return what they printed, colour codes removed.
fn run(ex: &mut Explorer, cmds: &[&str]) -> String {
    for c in cmds {
        ex.run(c);
    }
    let out = std::mem::take(&mut ex.out);
    let mut plain = String::new();
    let mut it = out.chars();
    while let Some(c) = it.next() {
        if c == '\x1b' {
            for c2 in it.by_ref() {
                if c2 == 'm' {
                    break;
                }
            }
        } else {
            plain.push(c);
        }
    }
    plain
}

#[test]
fn opens_on_the_first_line_inside_a_function() {
    let ex = session("open");
    // Step 7 is f's CALL; the first LINE after it is step 8.
    assert_eq!(ex.cursor, 8);
    assert_eq!(ex.fn_at(), "f");
    assert_eq!(ex.prompt(), format!("{BOLD}nd[{OFF}f prog.py:2{BOLD}]>{OFF} "));
}

#[test]
fn afl_and_af_report_the_structure() {
    let mut ex = session("afl");
    let out = run(&mut ex, &["afl"]);
    let lines: Vec<&str> = out.lines().collect();
    assert_eq!(lines[0], "function  calls  steps    ms  ←  →  vars");
    assert_eq!(lines[1], "f             1      8  16.0  1  0     2  ▇▇▇▇▇▇▇▇▇▇▇▇");
    assert_eq!(lines[2], "<module>      1      4   6.0  0  1     3  ▇▇▇▇▇▇      "); // the bar pads inside its colour

    let out = run(&mut ex, &["af f"]);
    assert!(out.starts_with("f  prog.py:1\n  called 1 times · 8 steps inside"), "{out}");
    assert!(out.contains("called by\n  function  times\n  <module>      1"), "{out}");
    // total and i tie on 3+1; nd orders a tie by a Python set, ndr by
    // writes-then-reads, so only the rows themselves are pinned.
    assert!(out.contains("  name   writes  reads\n"), "{out}");
    assert!(out.contains("  total       3      1  write+read\n"), "{out}");
    assert!(out.contains("  i           3      1  write+read\n"), "{out}");
    assert!(out.contains("  n                  1  reads only\n"), "{out}");
    assert!(out.ends_with("each call\n  at step  arguments  returned  steps\n  7        n=3        3            15\n"), "{out}");
}

#[test]
fn axv_and_ch_follow_one_variable() {
    let mut ex = session("axv");
    let out = run(&mut ex, &["axv total"]);
    assert!(out.starts_with("total  written 3 times  (3 new values, 0 in-place, 0 deletes)\n  alive from step 9 to 20"), "{out}");
    assert!(out.contains("on these lines\n  line       times\n  prog.py:4      2\n  prog.py:2      1\n"), "{out}");
    assert!(out.contains("read by  (from the source)\n  function  places\n  f              1\n"), "{out}");

    let out = run(&mut ex, &["ch total"]);
    assert_eq!(out, "step  where      how   total\n9     prog.py:2  BIND  0\n16    prog.py:4  BIND  1      was 0\n20    prog.py:4  BIND  3      was 1\n");
    let out = run(&mut ex, &["ch total 2"]);
    assert!(out.ends_with("showing 2 of 3 changes. `ch total 8` for more.\n"), "{out}");
    let out = run(&mut ex, &["ch nope"]);
    assert_eq!(out, "nope is never written.\n");
}

#[test]
fn seeking_and_conditions() {
    let mut ex = session("seek");
    run(&mut ex, &["s total"]);
    assert_eq!(ex.cursor, 9);
    run(&mut ex, &["c total>2"]);
    assert_eq!(ex.cursor, 20);
    let out = run(&mut ex, &["c"]);
    assert!(out.starts_with("last time it was true is behind you, step 20."), "{out}");
    run(&mut ex, &["s end"]);
    assert_eq!(ex.cursor, 26);
    run(&mut ex, &["s 50%"]);
    assert_eq!(ex.cursor, 13);
    run(&mut ex, &["s -3"]); // relative: 13 - 3
    assert_eq!(ex.cursor, 10);
    run(&mut ex, &["12"]); // a bare number seeks
    assert_eq!(ex.cursor, 12);
    run(&mut ex, &["u"]);
    assert_eq!(ex.cursor, 22);
    run(&mut ex, &["s 8", "f"]);
    assert_eq!(ex.cursor, 9);
    run(&mut ex, &["s fn:f"]);
    assert_eq!(ex.cursor, 7, "past the only call, so it wraps to the first");
    run(&mut ex, &["s @4"]);
    assert_eq!(ex.cursor, 12);
    let out = run(&mut ex, &["s :4"]);
    assert!(out.starts_with("nothing called ':4' is written in this run."), "nd's own reading of `s :N`: {out}");
    let out = run(&mut ex, &["s 99999999999999999999999"]);
    assert!(out.contains("26 / 26"), "{out}");
}

#[test]
fn state_at_a_moment() {
    let mut ex = session("state");
    let out = run(&mut ex, &["s 20", "pv"]);
    // f is a global too, but a known function, so pv leaves it out.
    assert!(out.ends_with("name   value       where\nn      3           f\ntotal  3           f\ni      2           f\nxs     [] [len=0]  global\ntotal is what step 20 changed.\n"), "{out}");
    let out = run(&mut ex, &["bt"]);
    assert_eq!(out, "frame  at         locals\nf      prog.py:4  n=3, total=3, i=2\n");
    // At the last step the module frame has returned, so nothing is in scope.
    let out = run(&mut ex, &["s end", "pV xs"]);
    assert!(out.ends_with("xs is not in scope at step 26.\n  it is written 2 times, first at step 5. `s xs` jumps there.\n"), "{out}");
    let out = run(&mut ex, &["s 25", "pV xs"]);
    assert!(out.ends_with("xs  list\n  [3]\n  type       list\n  kind       generic\n  len        1\n  key  value\n  0    3\n"), "{out}");
    let out = run(&mut ex, &["pV r"]);
    assert_eq!(out, "r = 3   (int)\n");
    let out = run(&mut ex, &["s 3", "pV total"]);
    assert!(out.contains("total is not in scope at step 3.\n  it is written 3 times, first at step 9. `s total` jumps there."), "{out}");
    let out = run(&mut ex, &["cmp 12 20"]);
    assert_eq!(out, "step 12 → step 20\nname   at 12  at 20\ni      0      2\ntotal  0      3\n");
}

#[test]
fn source_and_tables() {
    let mut ex = session("src");
    let out = run(&mut ex, &["s 12", "pd 3"]);
    assert!(out.ends_with("/t/prog.py\n      3       3      for i in range(n):  → i\n▸     4       3          total += i  → total\n      5       1      return total\n"), "{out}");
    let out = run(&mut ex, &["tree"]);
    assert_eq!(out, "<module> 1 calls · 4 steps · 6.0 ms\n└── f 1 calls · 8 steps · 16.0 ms\n");
    let out = run(&mut ex, &["dis"]);
    assert!(out.starts_with("line  hits   ms              source\n3        3  4.5  ▇▇▇▇▇▇▇▇▇▇  for i in range(n):\n"), "{out}");
    let out = run(&mut ex, &["fv"]);
    assert_eq!(out, "function  total   i  xs   r  n\nf            Wr  Wr   ·   ·  r\n<module>      ·   ·  Wr  Wr  ·\nW = writes it during the run · r = reads it in the source · · = no connection\n");
    let out = run(&mut ex, &["i"]);
    assert!(out.contains("  wall_seconds  0.0125\n  error         None\n  steps         26\n  found         2 functions, 5 variables\n"), "{out}");
    let out = run(&mut ex, &["/total"]);
    assert_eq!(out, "variables  total\nin prog.py\n  line  hits  source\n  2     1     total = 0\n  4     3     total += i\n  5     1     return total\n");
}

#[test]
fn errors_and_help_read_like_nd() {
    let mut ex = session("err");
    assert_eq!(run(&mut ex, &["n x"]), "invalid literal for int() with base 10: 'x'  (n [K] -- forward K steps (1).)\n");
    assert_eq!(run(&mut ex, &["zz"]), "no command 'zz'. press ? for the list.\n");
    assert_eq!(run(&mut ex, &["qq"]), "no command 'qq'. press ? for the list.\ncommands starting with 'q': q quit\n");
    assert_eq!(run(&mut ex, &["? afl"]), "afl -- every function: how often, how long, what it touches.\n");
    assert_eq!(run(&mut ex, &["?help"]), "no help\n");
    assert!(run(&mut ex, &["?"]).ends_with("q to leave · ?? for a worked example\n"));
    assert!(run(&mut ex, &["??"]).contains("\n  af f               what f is wired to\n"));
    assert!(run(&mut ex, &["axv"]).starts_with("axv needs a variable. the ones worth a look here:\n  axv total\n  axv i\n"));
    assert!(run(&mut ex, &["total"]).starts_with("(reading that as `axv total`)\ntotal  written"));
    assert!(run(&mut ex, &["c"]).starts_with("c needs a condition."));
    assert_eq!(run(&mut ex, &["c i>99"]), "that is never true in this run.\n");
    assert_eq!(run(&mut ex, &["s nope"]), "nothing called 'nope' is written in this run.\n");
    assert_eq!(run(&mut ex, &["s @99"]), "line 99 of prog.py never ran.\n");
    run(&mut ex, &["q"]);
    assert!(!ex.running);
}

#[test]
fn the_analysis_is_cached_and_reads_back_the_same() {
    let dir = fixture("cache");
    let open = || Store::open(crate::reader::Trace::open(&dir).unwrap());
    let fresh = analyse::load(&open(), false);
    let _ = analyse::load(&open(), true); // writes analysis.ndr.json
    assert!(dir.join(analyse::CACHE).exists());
    assert!(!dir.join("analysis.json").exists(), "nd's cache file is never written");
    let cached = analyse::load(&open(), true);
    let keys = |a: &Analysis| a.functions.keys().cloned().collect::<Vec<_>>();
    assert_eq!(keys(&fresh), keys(&cached));
    for (k, f) in fresh.functions.iter() {
        let g = cached.functions.get(k).unwrap();
        assert_eq!((f.calls, f.self_steps, f.total_steps, f.callers.len()), (g.calls, g.self_steps, g.total_steps, g.callers.len()));
        assert!((f.self_ms - g.self_ms).abs() < 1e-9);
    }
    assert_eq!(fresh.variables.get(&"total".to_string()).unwrap().by_line.items,
               cached.variables.get(&"total".to_string()).unwrap().by_line.items);
    assert_eq!(fresh.invocations.items, cached.invocations.items);
}

#[test]
fn line_targets_follow_nds_regex() {
    assert_eq!(line_target("@26"), Some((None, "26".into())));
    assert_eq!(line_target("foo.py:12"), Some((Some("foo.py".into()), "12".into())));
    assert_eq!(line_target(":26"), None);
    assert_eq!(line_target("foo:12"), None);
    assert_eq!(line_target(".py:12"), None);
}
