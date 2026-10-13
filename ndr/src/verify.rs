//! `ndr verify`: record a program with both recorders and compare.
//!
//! The Python recorder is the reference. A recorder that is 40x faster and
//! subtly wrong is worse than none, because you would believe it -- so this
//! is the test that decides whether `ndr` can be trusted on a given program.

use crate::reader::Trace;
use crate::schema::{self, RK_OBJ};
use crate::ui::*;
use std::collections::{BTreeMap, HashMap};
use std::path::Path;
use std::process::{Command, Stdio};

pub struct Summary {
    pub kinds: BTreeMap<&'static str, u64>,
    pub lines: Vec<(String, i64)>,
    pub calls: BTreeMap<String, u64>,
    pub scalars: BTreeMap<String, Vec<String>>,
    pub mutates: BTreeMap<String, u64>,
}

pub fn summarise(t: &Trace) -> Summary {
    let pf = t.payload_funcs();
    let mut s = Summary {
        kinds: BTreeMap::new(), lines: Vec::new(), calls: BTreeMap::new(),
        scalars: BTreeMap::new(), mutates: BTreeMap::new(),
    };
    let mut place_cache: HashMap<u32, (String, i64)> = HashMap::new();
    t.each(|step, r| {
        *s.kinds.entry(schema::kind_name(r.kind)).or_insert(0) += 1;
        match r.kind {
            schema::LINE => {
                let p = place_cache.entry(r.loc).or_insert_with(|| t.place(r.loc)).clone();
                s.lines.push(p);
            }
            schema::CALL => {
                let f = pf.get(&r.payload).cloned().unwrap_or_else(|| "?".into());
                *s.calls.entry(f).or_insert(0) += 1;
            }
            schema::BIND if r.v.rk != RK_OBJ => {
                let name = t.names.get(&r.name).cloned().unwrap_or_default();
                let val = t.show(&r.v, step);
                let seq = s.scalars.entry(name).or_default();
                // The same value twice in a row carries no information, and
                // only ever comes from nd's phantom DELETE + re-BIND (below).
                if seq.last() != Some(&val) {
                    seq.push(val);
                }
            }
            schema::MUTATE => {
                let name = t.names.get(&r.name).cloned().unwrap_or_default();
                *s.mutates.entry(name).or_insert(0) += 1;
            }
            _ => {}
        }
    });
    s
}

/// Record with the Python recorder, on the SAME Python `ndr` embeds.
///
/// Line events are not identical across Python versions (3.12 and 3.13 lay
/// out loops differently), so comparing ndr-on-3.13 with nd-on-3.12 would
/// report differences that are nobody's bug. The interpreter ndr was built
/// against comes first; `nd` on PATH is the fallback.
fn record_python(script: &Path, argv: &[String], out: &Path) -> Result<(), String> {
    let repo = Path::new(env!("CARGO_MANIFEST_DIR")).join("..");
    let mut tries: Vec<Command> = Vec::new();
    if let Some(exe) = option_env!("NDR_PYTHON_EXE") {
        let mut a = Command::new(exe);
        a.args(["-m", "nd36.cli", "run"]).arg(script).arg("-o").arg(out).arg("--").args(argv)
            .env("PYTHONPATH", &repo);
        tries.push(a);
    }
    let mut b = Command::new("nd");
    b.arg("run").arg(script).arg("-o").arg(out).arg("--").args(argv);
    tries.push(b);
    let mut last = String::new();
    for mut c in tries {
        let _ = std::fs::remove_dir_all(out);
        match c.stdout(Stdio::null()).stderr(Stdio::piped()).output() {
            Ok(o) if o.status.success() && out.join("trace.db").exists() => return Ok(()),
            Ok(o) => last = String::from_utf8_lossy(&o.stderr).lines().last().unwrap_or("").to_string(),
            Err(e) => last = e.to_string(),
        }
    }
    Err(format!("could not run the Python recorder (nd): {last}\ninstall it: cd ND36-Code-Visualizer && pip install --user -e ."))
}

fn minor_version(t: &Trace) -> String {
    t.meta("python").and_then(|v| v.as_str()).and_then(|s| s.split_whitespace().next())
        .map(|v| v.split('.').take(2).collect::<Vec<_>>().join(".")).unwrap_or_default()
}

pub fn verify(script: &Path, argv: &[String]) -> Result<bool, String> {
    let tmp = std::env::temp_dir().join(format!("ndr-verify-{}", std::process::id()));
    let _ = std::fs::remove_dir_all(&tmp);
    let (ra, rb) = (tmp.join("rust.nd"), tmp.join("python.nd"));

    println!("{DIM}recording with ndr (Rust)…{OFF}");
    let res = crate::run::record(script, argv, &ra, 2000, true)?;
    crate::run::restore_stdout();
    println!("{DIM}recording with nd (Python, the reference)…{OFF}");
    record_python(script, argv, &rb)?;

    let (a, b) = (Trace::open(&ra)?, Trace::open(&rb)?);
    let (va, vb) = (minor_version(&a), minor_version(&b));
    if va != vb {
        println!("{YELLOW}warning:{OFF} ndr ran on Python {va}, nd on Python {vb}. Line events differ \
                  between Python versions, so some differences below may be nobody's bug.");
    }
    let (sa, sb) = (summarise(&a), summarise(&b));

    println!("\n{BOLD}event counts{OFF}");
    println!("  {DIM}{:<8} {:>12} {:>12}{OFF}", "event", "ndr", "nd");
    for k in ["LINE", "CALL", "RETURN", "BIND", "MUTATE", "DELETE", "RAISE", "UNWIND", "YIELD"] {
        let (x, y) = (sa.kinds.get(k).copied().unwrap_or(0), sb.kinds.get(k).copied().unwrap_or(0));
        if x == 0 && y == 0 { continue; }
        let mark = if x == y { format!("{GREEN}✓{OFF}") } else if k == "MUTATE" || k == "BIND" || k == "DELETE" { format!("{DIM}·{OFF}") } else { format!("{RED}✗{OFF}") };
        println!("  {:<8} {:>12} {:>12}  {mark}", k, fmt_n(x), fmt_n(y));
    }

    // 1. Control flow: every line, in order. This must be identical.
    let first_diff = sa.lines.iter().zip(&sb.lines).position(|(x, y)| x != y);
    let flow_ok = first_diff.is_none() && sa.lines.len() == sb.lines.len();
    println!("\n{BOLD}checks{OFF}");
    if flow_ok {
        println!("  {GREEN}✓{OFF} control flow     all {} lines, in the same order", fmt_n(sa.lines.len() as u64));
    } else {
        let i = first_diff.unwrap_or(sa.lines.len().min(sb.lines.len()));
        let show = |v: &Vec<(String, i64)>| v.get(i).map(|(f, l)| format!("{f}:{l}")).unwrap_or_else(|| "(ended)".into());
        println!("  {RED}✗{OFF} control flow     differs at line event #{}: ndr {} vs nd {}",
                 fmt_n(i as u64 + 1), show(&sa.lines), show(&sb.lines));
    }

    // 2. Calls per function.
    let calls_ok = sa.calls == sb.calls;
    if calls_ok {
        println!("  {GREEN}✓{OFF} calls            {} functions, every call count equal", sa.calls.len());
    } else {
        println!("  {RED}✗{OFF} calls            per-function call counts differ");
        for (f, n) in &sa.calls {
            let m = sb.calls.get(f).copied().unwrap_or(0);
            if *n != m { println!("      {f}: ndr {n}, nd {m}"); }
        }
    }

    // 3. Every scalar value each variable took, in order.
    let mut same = 0;
    let mut bad: Vec<String> = Vec::new();
    for (name, vals) in &sb.scalars {
        match sa.scalars.get(name) {
            Some(v) if v == vals => same += 1,
            Some(v) => {
                let i = v.iter().zip(vals).position(|(x, y)| x != y).unwrap_or(v.len().min(vals.len()));
                bad.push(format!("{name}: change #{} is {} in ndr, {} in nd", i + 1,
                                 v.get(i).cloned().unwrap_or("(none)".into()), vals.get(i).cloned().unwrap_or("(none)".into())));
            }
            None => bad.push(format!("{name}: never assigned in ndr")),
        }
    }
    let values_ok = bad.is_empty();
    if values_ok {
        println!("  {GREEN}✓{OFF} values           {same} variables, every scalar value identical, in order");
    } else {
        println!("  {RED}✗{OFF} values           {same} of {} variables identical", sb.scalars.len());
        for b in bad.iter().take(6) { println!("      {b}"); }
    }

    // 4. In-place changes: informational. ndr is expected to see MORE.
    // Not a pass/fail check: the two recorders disagree here BY DESIGN.
    //   nd stops re-checking a container after 150 looks, so it misses late
    //   changes -- and when that budget runs out its fingerprint flips, so it
    //   also reports changes on lines that change nothing (`print(xs)`).
    //   ndr hashes contents every line with no cut-off, so it does neither.
    let (ma, mb): (u64, u64) = (sa.mutates.values().sum(), sb.mutates.values().sum());
    println!("  {DIM}·{OFF} in-place changes ndr {}, nd {}  {DIM}— expected to differ, see `ndr verify --help`{OFF}",
             fmt_n(ma), fmt_n(mb));

    // nd (Python 3.12+) reports module globals as DELETED while an inlined
    // list comprehension runs at module level -- frame.f_locals briefly shows
    // the comprehension's scope instead -- then re-binds them. Nothing was
    // deleted. ndr reads the real namespace and does not do this.
    let (da, db) = (sa.kinds.get("DELETE").copied().unwrap_or(0), sb.kinds.get("DELETE").copied().unwrap_or(0));
    if db > da {
        println!("  {DIM}·{OFF} deletions       nd reported {} that ndr did not  {DIM}— nd's comprehension quirk, not real deletions{OFF}",
                 fmt_n(db - da));
    }

    let ok = flow_ok && calls_ok && values_ok;
    let (ta, tb) = (res.seconds, b.meta("wall_seconds").and_then(|v| v.as_f64()).unwrap_or(0.0));
    println!("\n{BOLD}speed{OFF}   ndr {:.3} s · nd {:.3} s · {BOLD}{:.1}× faster{OFF}", ta, tb, if ta > 0.0 { tb / ta } else { 0.0 });
    println!("\n{}", if ok {
        format!("{GREEN}{BOLD}ndr agrees with the reference recorder on this program.{OFF}")
    } else {
        format!("{RED}{BOLD}ndr and nd disagree on this program.{OFF} the traces are kept in {}", tmp.display())
    });
    if ok {
        let _ = std::fs::remove_dir_all(&tmp);
    }
    Ok(ok)
}
