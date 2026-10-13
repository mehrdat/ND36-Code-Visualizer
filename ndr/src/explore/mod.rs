//! `ndr x` -- the explorer: `nd36/explore.py`, in Rust.
//!
//! A prompt that sits on top of a recorded run, in the spirit of radare2: a
//! cursor, very short commands, and a table out of every one of them. The
//! cursor is a *moment in time*, so `s` seeks to a step, and every other
//! command answers "at this moment" or "across the whole run".
//!
//! Every command, alias, message and layout matches `nd x`, colours
//! included, so the two can be diffed. Where nd's behaviour is odd (it is
//! documented in the report that came with this port), the oddity is kept.

pub mod analyse;
pub mod cond;
pub mod draw;
pub mod json;
pub mod pyfmt;
pub mod store;

use analyse::{by_count, most_common, Analysis};
use draw::{bar, cursor_bar, heatrows, plain, plot, rel, sparkline, table, BOLD, CYAN, DIM, GREEN, OFF, RED, YELLOW};
use json::Json;
use pyfmt::{int, n, nu, repr_str, strip, take, Num};
use std::collections::HashSet;
use std::io::Write;
use std::sync::atomic::{AtomicBool, Ordering};
use store::{Om, Py, Ref, Resolved, State, Store};

use crate::schema;

const SCAN_CAP: usize = 300_000;

/// Why a command stopped early. `Value` and `Index` are the two Python
/// exceptions `nd x` catches per command and prints with the command's help.
pub enum Fail {
    Value(String),
    Index(String),
    Interrupt,
}

type R = Result<(), Fail>;

static INTERRUPTED: AtomicBool = AtomicBool::new(false);

extern "C" fn on_sigint(_: libc::c_int) {
    INTERRUPTED.store(true, Ordering::Relaxed);
}

/// Ctrl-C stops the running command, not the session (KeyboardInterrupt).
fn catch_ctrl_c() {
    // SAFETY: the handler only stores to an atomic, which is signal-safe.
    unsafe {
        libc::signal(libc::SIGINT, on_sigint as *const () as libc::sighandler_t);
    }
}

fn check_interrupt(i: usize) -> R {
    if i & 0xffff == 0 && INTERRUPTED.swap(false, Ordering::Relaxed) {
        return Err(Fail::Interrupt);
    }
    Ok(())
}

#[derive(Clone, Copy, PartialEq, Eq, Debug)]
pub enum Cmd {
    S, N, P, F, FBack, U, C, Pd, Pv, PV, Bt, Ll, Afl, Af, Axf, Axv, Fv, Vl, Ch, Pl, Tt, Tree, Cmp, Dis, Info, Help, Quit,
}

/// `Explorer.TABLE`, in its dict order.
pub const TABLE: &[(&str, Cmd)] = &[
    ("s", Cmd::S), ("seek", Cmd::S), ("n", Cmd::N), ("next", Cmd::N), ("p", Cmd::P), ("prev", Cmd::P),
    ("f", Cmd::F), ("F", Cmd::FBack), ("u", Cmd::U), ("out", Cmd::U), ("c", Cmd::C), ("cont", Cmd::C),
    (".", Cmd::S), ("pd", Cmd::Pd), ("src", Cmd::Pd), ("pv", Cmd::Pv), ("vars", Cmd::Pv), ("pV", Cmd::PV),
    ("var", Cmd::PV), ("bt", Cmd::Bt), ("stack", Cmd::Bt), ("ll", Cmd::Ll), ("events", Cmd::Ll),
    ("afl", Cmd::Afl), ("funcs", Cmd::Afl), ("af", Cmd::Af), ("func", Cmd::Af), ("axf", Cmd::Axf),
    ("axv", Cmd::Axv), ("fv", Cmd::Fv), ("grid", Cmd::Fv), ("vl", Cmd::Vl), ("list", Cmd::Vl),
    ("ch", Cmd::Ch), ("changes", Cmd::Ch), ("pl", Cmd::Pl), ("plot", Cmd::Pl), ("tt", Cmd::Tt),
    ("timeline", Cmd::Tt), ("tree", Cmd::Tree), ("cmp", Cmd::Cmp), ("diff", Cmd::Cmp), ("dis", Cmd::Dis),
    ("hot", Cmd::Dis), ("i", Cmd::Info), ("info", Cmd::Info), ("help", Cmd::Help), ("q", Cmd::Quit),
    ("quit", Cmd::Quit), ("exit", Cmd::Quit),
];

fn lookup(head: &str) -> Option<Cmd> {
    TABLE.iter().find(|(k, _)| *k == head).map(|(_, c)| *c)
}

/// Each command's docstring, which `? CMD` and error messages print.
fn doc(c: Cmd) -> Option<&'static str> {
    Some(match c {
        Cmd::S => "s TARGET -- seek. The one command everything else hangs off.",
        Cmd::N => "n [K] -- forward K steps (1).",
        Cmd::P => "p [K] -- back K steps (1).",
        Cmd::F => "f -- the next event in THIS frame: step over any call.",
        Cmd::FBack => "F -- the previous event in this frame.",
        Cmd::U => "u -- step out: where this frame returns.",
        Cmd::C => "c COND -- run on until it is true.  c i>5   c grid   c raise",
        Cmd::Pd => "pd [N] -- the source here, with what each line does. (N=9)",
        Cmd::Pv => "pv -- the variables that exist right now.",
        Cmd::PV => "pV NAME -- one variable in full, here.",
        Cmd::Bt => "bt -- the call stack at the cursor.",
        Cmd::Ll => "ll [N] -- the raw events around the cursor. (N=16)",
        Cmd::Afl => "afl -- every function: how often, how long, what it touches.",
        Cmd::Af => "af [NAME] -- one function, wired up. The cursor's, by default.",
        Cmd::Axf => "axf [NAME] -- cross references: in and out of a function.",
        Cmd::Axv => "axv NAME -- cross references of a variable: writers and readers.",
        Cmd::Fv => "fv [NAME] -- the function x variable grid. W = writes, r = reads.",
        Cmd::Vl => "vl [TEXT] -- every variable: writes, where, when.",
        Cmd::Ch => "ch NAME -- the value table: every change, with the line.",
        Cmd::Pl => "pl NAME -- draw it over the whole run.",
        Cmd::Tt => "tt -- the timeline: which function was busy, when.",
        Cmd::Tree => "tree [NAME] -- the call tree, by cost.",
        Cmd::Cmp => "cmp A B -- what is different between two steps.",
        Cmd::Dis => "dis -- the hottest lines in this file.",
        Cmd::Info => "i -- what this run was.",
        Cmd::Help | Cmd::Quit => return None,
    })
}

const GROUPS: &[(&str, &[(&str, &str)])] = &[
    ("where am i", &[
        ("s N", "go to step N.  s +50  s -50  s 50%  s end"),
        ("s NAME", "go to the next change of a variable"),
        ("s fn:NAME", "go to the next call of a function"),
        ("s :26", "go to the next time line 26 ran"),
        ("n / p", "one step forward / back   (n 100 for a hundred)"),
        ("f / F", "next / previous event in THIS frame (step over)"),
        ("u", "step out: where this frame returns"),
        ("c COND", "run on until true.  c i>5   c grid   c raise"),
    ]),
    ("look at this moment", &[
        ("pd", "the source here, with hits and what each line writes"),
        ("pv", "the variables that exist right now"),
        ("pV NAME", "one variable in full, here"),
        ("bt", "the call stack"),
        ("ll", "the raw events around the cursor"),
    ]),
    ("look at the whole run", &[
        ("afl", "every function: calls, time, how wired up it is"),
        ("af NAME", "one function: callers, callees, its variables"),
        ("axf NAME", "who calls it, who it calls"),
        ("axv NAME", "who writes a variable, who reads it, where"),
        ("fv", "the function x variable grid -- the connections"),
        ("vl", "every variable, by how often it changes"),
        ("ch NAME", "the value table: every change, with the line"),
        ("pl NAME", "draw a variable over the whole run"),
        ("tt", "timeline: which function was busy when"),
        ("tree", "the call tree, by cost"),
        ("cmp A B", "what is different between two steps"),
        ("dis", "the hottest lines in this file"),
        ("/ TEXT", "find a name, a value or a line of source"),
        ("i", "what this run was"),
    ]),
];

/// `print(...)` into the session's buffer.
macro_rules! say {
    ($s:expr) => { $s.p(String::new()) };
    ($s:expr, $($arg:tt)*) => { $s.p(format!($($arg)*)) };
}

/// `"%r" % text` for messages.
fn r(s: &str) -> String {
    repr_str(s)
}

/// `%r` of an argument that may be None.
fn r_opt(s: Option<&str>) -> String {
    s.map(repr_str).unwrap_or_else(|| "None".into())
}

fn line_text(l: Option<i64>) -> String {
    l.map(|l| l.to_string()).unwrap_or_else(|| "None".into())
}

fn ms(x: f64) -> String {
    if x >= 0.05 { pyfmt::fixed(x, 1) } else { String::new() }
}

fn float_err(s: &str) -> Fail {
    Fail::Value(format!("could not convert string to float: {}", repr_str(s)))
}

/// One session. `run(line)` runs one command; everything else is state.
pub struct Explorer {
    pub r: Store,
    pub base: String,
    pub a: Analysis,
    pub cursor: u64,
    pub running: bool,
    last_cond: Option<String>,
    lines: Om<(Option<String>, Option<i64>), (i64, i64)>,
    /// What the commands printed, not yet written out.
    pub out: String,
    /// Write `out` to stdout as it fills (the CLI); off in tests.
    live: bool,
}

impl Explorer {
    pub fn new(r: Store, path: &str, quiet: bool, live: bool) -> Explorer {
        let base = pyfmt::basename(path.trim_end_matches('/')).to_string();
        if !quiet {
            print!("{DIM}reading the run…{OFF}");
            let _ = std::io::stdout().flush();
        }
        let a = analyse::load(&r, true);
        if !quiet {
            println!("\r{DIM}analysed {} steps, {} functions, {} variables{OFF}", nu(r.n_steps), a.functions.len(), a.variables.len());
        }
        let lines = r.line_stats();
        let mut ex = Explorer { r, base, a, cursor: 1, running: true, last_cond: None, lines, out: String::new(), live };
        ex.cursor = ex.first_real_line();
        ex
    }

    fn p(&mut self, s: String) {
        self.out.push_str(&s);
        self.out.push('\n');
        if self.live && self.out.len() > 1 << 16 {
            self.flush();
        }
    }

    pub fn flush(&mut self) {
        if self.live && !self.out.is_empty() {
            let mut o = std::io::stdout().lock();
            let _ = o.write_all(self.out.as_bytes());
            let _ = o.flush();
        }
        if self.live {
            self.out.clear();
        }
    }
    // ------------------------------------------------------------------ bits

    /// Where the cursor starts: the first line of real work, not the
    /// module's CALL (whose `pd` is the file's docstring).
    fn first_real_line(&self) -> u64 {
        let mut first_line = None;
        let mut fallback = None;
        let n = self.r.n_steps;
        for step in 1..=n.min(20000) {
            let rec = self.r.raw(step);
            if rec.kind == schema::LINE && first_line.is_none() {
                first_line = Some(step);
            } else if rec.kind == schema::CALL && step > 1 {
                for after in (step + 1)..=(step + 40).min(n) {
                    if self.r.raw(after).kind == schema::LINE {
                        return after;
                    }
                }
                return step;
            } else if rec.kind == schema::BIND && fallback.is_none() {
                let name = self.r.names.get(&rec.name);
                let kind = match self.r.rec_ref(&rec) {
                    Some(rf) => match self.r.resolve(&rf, step as i64) {
                        Resolved::Obj(o) => o.ty,
                        _ => String::new(),
                    },
                    None => String::new(),
                };
                if let Some(name) = name {
                    if !name.is_empty() && !self.a.functions.contains(name) && kind != "module" && kind != "function" {
                        fallback = Some(step);
                    }
                }
            }
        }
        fallback.or(first_line).unwrap_or(1)
    }

    fn here(&self) -> (String, i64) {
        let e = self.r.view(self.cursor);
        (e.file.unwrap_or_default(), e.line.unwrap_or(0))
    }

    /// Which function the cursor is inside, from the live stack.
    pub fn fn_at(&self) -> String {
        let st = self.r.state_at(self.cursor);
        match st.frames.last() {
            None => "<module>".into(),
            Some(f) => match f.func {
                Some(id) => self.r.funcs.get(&id).and_then(|row| row.label()).unwrap_or("?").to_string(),
                None => "<module>".into(),
            },
        }
    }

    pub fn prompt(&self) -> String {
        let (path, line) = self.here();
        format!("{BOLD}nd[{OFF}{} {}:{line}{BOLD}]>{OFF} ", self.fn_at(), rel(Some(&path)))
    }

    fn seek(&mut self, step: i64) -> R {
        self.cursor = step.min(self.r.n_steps as i64).max(1) as u64;
        self.status();
        Ok(())
    }

    pub fn status(&mut self) {
        let e = self.r.view(self.cursor);
        say!(self, "{}", cursor_bar(self.cursor, self.r.n_steps));
        let mut bits = vec![
            format!("{BOLD}{}:{}{OFF}", rel(e.file.as_deref()), line_text(e.line)),
            format!("{CYAN}{}{OFF}", e.kind),
        ];
        if let Some(name) = e.name.as_deref().filter(|n| !n.is_empty()) {
            bits.push(format!("{name} = {}", self.val(&e.r, 46, 0)));
        }
        let f = self.fn_at();
        say!(self, "  {}   {DIM}in {f}{OFF}", bits.join("   "));
    }

    /// A ref as one line, resolved at `step` (0 = the cursor): an object's
    /// contents change, so the wrong step shows the wrong version.
    fn val(&self, rf: &Option<Ref>, width: usize, step: i64) -> String {
        let Some(rf) = rf else { return format!("{DIM}–{OFF}") };
        let at = if step == 0 { self.cursor as i64 } else { step };
        let mut text = match self.r.resolve(rf, at) {
            Resolved::Obj(o) => {
                let extra: Vec<String> = o.summary.items().iter()
                    .filter(|(k, _)| matches!(k.as_str(), "len" | "shape" | "dtype" | "rows" | "cols"))
                    .map(|(k, v)| format!("{k}={}", v.py_str()))
                    .collect();
                if extra.is_empty() { o.repr } else { format!("{} {DIM}[{}]{OFF}", o.repr, extra.join(" ")) }
            }
            Resolved::Val(v) => v.repr(),
        };
        if plain(&text) > width {
            text = format!("{}…", take(&text, width));
        }
        text
    }

    // ---------------------------------------------------------------- navigate

    fn c_s(&mut self, arg: Option<&str>) -> R {
        let Some(arg) = arg else {
            self.status();
            return Ok(());
        };
        let t = strip(arg);
        let n = self.r.n_steps as i64;
        if t == "end" || t == "$" {
            return self.seek(n);
        }
        if t == "start" || t == "^" || t == "0" {
            return self.seek(1);
        }
        if let Some(head) = t.strip_suffix('%') {
            if pyfmt::isdigit(strip(head).trim_start_matches('-')) {
                let f = pyfmt::float(head).ok_or_else(|| float_err(head))?;
                return self.seek((f / 100.0 * n as f64) as i64);
            }
        }
        if t.starts_with(['+', '-']) && pyfmt::isdigit(strip(&t[1..])) {
            let k = int(&t.replace(' ', "")).map_err(Fail::Value)?;
            return self.seek((self.cursor as i64).saturating_add(k));
        }
        if pyfmt::isdigit(t.trim_start_matches('-')) {
            return self.seek(int(t).map_err(Fail::Value)?);
        }
        if let Some(rest) = t.strip_prefix("fn:").or_else(|| t.strip_prefix("@fn:")) {
            return self.seek_call(rest);
        }
        if let Some((file, line)) = line_target(t) {
            if !pyfmt::isdigit(t) {
                return self.seek_line(file.as_deref(), int(&line).map_err(Fail::Value)?);
            }
        }
        if self.a.functions.contains(&t.to_string()) {
            return self.seek_call(t);
        }
        self.seek_write(t, None)
    }

    fn near_vars(&self, name: &str, k: usize) -> Vec<String> {
        let low = name.to_lowercase();
        self.a.variables.keys().filter(|v| v.to_lowercase().contains(&low)).take(k).cloned().collect()
    }

    fn seek_write(&mut self, name: &str, test: Option<&dyn Fn(&Py) -> bool>) -> R {
        let steps = self.r.steps_of(name);
        if steps.is_empty() {
            say!(self, "{YELLOW}nothing called {} is written in this run.{OFF}", r(name));
            let near = self.near_vars(name, 6);
            if !near.is_empty() {
                say!(self, "  did you mean: {}", near.join(", "));
            }
            return Ok(());
        }
        let at = self.cursor;
        let ahead: Vec<u64> = steps.iter().copied().filter(|s| *s > at).collect();
        let mut hit = self.first_true(&ahead, test)?;
        if hit.is_none() && test.is_some() {
            let behind: Vec<u64> = steps.iter().copied().filter(|s| *s <= at).collect();
            hit = self.first_true(&behind, test)?;
            match hit {
                None => {
                    say!(self, "{YELLOW}that is never true in this run.{OFF}");
                    return Ok(());
                }
                Some(h) => say!(self, "{DIM}last time it was true is behind you, step {}.{OFF}", nu(h)),
            }
        }
        match hit {
            None => {
                let last = *steps.last().unwrap();
                say!(self, "{DIM}{name} does not change again. its last change was step {}.{OFF}", nu(last));
                self.seek(last as i64)
            }
            Some(h) => self.seek(h as i64),
        }
    }

    fn first_true(&mut self, steps: &[u64], test: Option<&dyn Fn(&Py) -> bool>) -> Result<Option<u64>, Fail> {
        let Some(test) = test else { return Ok(steps.first().copied()) };
        for (i, s) in steps.iter().enumerate() {
            if i >= SCAN_CAP {
                say!(self, "{DIM}looked at {} changes and stopped there.{OFF}", n(SCAN_CAP as i64));
                return Ok(None);
            }
            check_interrupt(i)?;
            if test(&self.value_at(*s)) {
                return Ok(Some(*s));
            }
        }
        Ok(None)
    }

    /// The value written at `step`, as cheaply as possible: an int, float,
    /// bool or short string sits inside the record; only an object needs a
    /// lookup, and then its repr is what is compared.
    fn value_at(&self, step: u64) -> Py {
        let rec = self.r.raw(step);
        match self.r.rec_ref(&rec) {
            None => Py::None,
            Some(Ref::I(v)) => v,
            Some(rf) => match self.r.resolve(&rf, step as i64) {
                Resolved::Obj(o) => Py::Str(o.repr),
                Resolved::Val(v) => v,
            },
        }
    }

    fn need_name(&mut self, cmd: &str, what: &str) -> R {
        let names: Vec<String> = if what == "function" {
            let mut items: Vec<&analyse::Func> = self.a.functions.values().filter(|f| f.name != "<module>").collect();
            items.sort_by(|a, b| b.self_steps.cmp(&a.self_steps));
            items.iter().take(6).map(|f| f.name.clone()).collect()
        } else {
            let mut items: Vec<(&String, &analyse::Var)> = self.a.variables.iter().collect();
            items.sort_by(|a, b| b.1.writes.cmp(&a.1.writes));
            items.iter().filter(|(k, _)| !self.a.functions.contains(k)).take(6).map(|(k, _)| (*k).clone()).collect()
        };
        say!(self, "{DIM}{cmd} needs a {what}. the ones worth a look here:{OFF}");
        for name in &names {
            say!(self, "  {CYAN}{cmd} {name}{OFF}");
        }
        if names.is_empty() {
            say!(self, "  {DIM}(this trace has none){OFF}");
        }
        Ok(())
    }

    fn seek_call(&mut self, name: &str) -> R {
        let Some(f) = self.a.func(name) else {
            say!(self, "{YELLOW}no function called {}.{OFF}", r(name));
            return Ok(());
        };
        let hit = self.a.invocations.get(&f).and_then(|inv| inv.iter().find(|(enter, _)| *enter > self.cursor).map(|p| p.0));
        if let Some(enter) = hit {
            return self.seek(enter as i64);
        }
        let first = self.a.functions.get(&f).map(|i| i.first_step).unwrap_or(0);
        if first != 0 {
            say!(self, "{DIM}last call already passed; wrapping to the first.{OFF}");
            return self.seek(first as i64);
        }
        say!(self, "{YELLOW}{f} was never called.{OFF}");
        Ok(())
    }

    fn seek_line(&mut self, filename: Option<&str>, line: i64) -> R {
        let (mut path, _) = self.here();
        if let Some(fname) = filename {
            if let Some(p) = self.r.files.values().find(|p| pyfmt::basename(p) == fname || p.as_str() == fname) {
                path = p.clone();
            }
        }
        let want = self.r.locs.iter()
            .find(|(_, (f, ln))| *ln == Some(line) && self.r.files.get(f) == Some(&path))
            .map(|(id, _)| *id);
        let Some(want) = want else {
            say!(self, "{YELLOW}line {line} of {} never ran.{OFF}", rel(Some(&path)));
            return Ok(());
        };
        for s in (self.cursor + 1)..=self.r.n_steps {
            check_interrupt(s as usize)?;
            let rec = self.r.raw(s);
            if rec.kind == schema::LINE && rec.loc as i64 == want {
                return self.seek(s as i64);
            }
        }
        say!(self, "{DIM}that line does not run again after here.{OFF}");
        Ok(())
    }

    fn c_n(&mut self, arg: Option<&str>) -> R {
        let k = match arg { Some(a) => int(a).map_err(Fail::Value)?, None => 1 };
        self.seek((self.cursor as i64).saturating_add(k))
    }

    fn c_p(&mut self, arg: Option<&str>) -> R {
        let k = match arg { Some(a) => int(a).map_err(Fail::Value)?, None => 1 };
        self.seek((self.cursor as i64).saturating_sub(k))
    }

    fn c_f(&mut self) -> R {
        let frame = self.r.raw(self.cursor).frame;
        for s in (self.cursor + 1)..=self.r.n_steps {
            check_interrupt(s as usize)?;
            if self.r.raw(s).frame == frame {
                return self.seek(s as i64);
            }
        }
        say!(self, "{DIM}this frame never runs again.{OFF}");
        Ok(())
    }

    fn c_fback(&mut self) -> R {
        let frame = self.r.raw(self.cursor).frame;
        for s in (1..self.cursor).rev() {
            check_interrupt(s as usize)?;
            if self.r.raw(s).frame == frame {
                return self.seek(s as i64);
            }
        }
        say!(self, "{DIM}this is the start of the frame.{OFF}");
        Ok(())
    }

    fn c_u(&mut self) -> R {
        let frame = self.r.raw(self.cursor).frame;
        for s in (self.cursor + 1)..=self.r.n_steps {
            check_interrupt(s as usize)?;
            let rec = self.r.raw(s);
            if rec.frame == frame && (rec.kind == schema::RETURN || rec.kind == schema::UNWIND) {
                return self.seek(s as i64);
            }
        }
        say!(self, "{DIM}this frame never returns (it is the main one).{OFF}");
        Ok(())
    }

    fn c_c(&mut self, arg: Option<&str>) -> R {
        let arg = match arg {
            Some(a) => a.to_string(),
            None => match &self.last_cond {
                Some(c) => c.clone(),
                None => {
                    say!(self, "c needs a condition. try:  c i>5   c grid   c raise");
                    return Ok(());
                }
            },
        };
        self.last_cond = Some(arg.clone());
        let arg = strip(&arg).to_string();
        if matches!(arg.as_str(), "raise" | "error" | "except") {
            for s in (self.cursor + 1)..=self.r.n_steps {
                check_interrupt(s as usize)?;
                let k = self.r.raw(s).kind;
                if k == schema::RAISE || k == schema::UNWIND {
                    return self.seek(s as i64);
                }
            }
            say!(self, "{DIM}nothing was raised after here.{OFF}");
            return Ok(());
        }
        let Some((name, op, rhs)) = cond::split(&arg) else {
            return self.c_s(Some(&arg));
        };
        let target = cond::target(strip(&rhs));
        let test = move |got: &Py| cond::test(op, got, &target);
        self.seek_write(&name, Some(&test))
    }

    // -------------------------------------------------------------------- look

    fn c_pd(&mut self, arg: Option<&str>) -> R {
        let span = match arg { Some(a) => int(a).map_err(Fail::Value)?, None => 9 };
        let (path, line) = self.here();
        let source = self.r.source(&path);
        let src = pyfmt::splitlines(&source);
        if src.is_empty() {
            say!(self, "{DIM}no source stored for {path}{OFF}");
            return Ok(());
        }
        let prefix = format!("{}:", rel(Some(&path)));
        let mut writes: std::collections::HashMap<i64, Vec<String>> = Default::default();
        for (var, info) in self.a.variables.iter() {
            for (key, _) in info.by_line.iter() {
                if key.starts_with(&prefix) {
                    let ln = key.rsplit(':').next().unwrap_or("");
                    let ln = int(ln).map_err(Fail::Value)?;
                    writes.entry(ln).or_default().push(var.clone());
                }
            }
        }
        let lo = (line - draw::half(span)).max(1);
        let hi = (src.len() as i64).min(lo + span - 1);
        say!(self, "{BOLD}{path}{OFF}");
        let w = draw::width();
        for i in lo..=hi {
            let hits = self.lines.get(&(Some(path.clone()), Some(i))).map(|h| h.0).unwrap_or(0);
            let mark = if i == line { format!("{YELLOW}▸{OFF}") } else { " ".into() };
            let note = match writes.get(&i) {
                Some(ws) => {
                    let mut u: Vec<&String> = ws.iter().collect::<HashSet<_>>().into_iter().collect();
                    u.sort();
                    let u: Vec<&str> = u.iter().take(4).map(|s| s.as_str()).collect();
                    format!("  {CYAN}→ {}{OFF}", u.join(","))
                }
                None => String::new(),
            };
            let text = take(src[(i - 1) as usize], w.saturating_sub(30));
            say!(self, "{mark} {DIM}{i:>5} {:>7}{OFF}  {text}{note}", if hits != 0 { n(hits) } else { "·".into() });
        }
        Ok(())
    }

    fn c_pv(&mut self) -> R {
        let st = self.r.state_at(self.cursor);
        let e = self.r.view(self.cursor);
        let changed = e.name.clone();
        let mut rows: Vec<Vec<String>> = Vec::new();
        if let Some(frame) = st.frames.last() {
            let fname = self.r.func_label(frame.func);
            for (k, v) in frame.locals.iter() {
                let key = if Some(k) == changed.as_ref() { format!("{YELLOW}{k}{OFF}") } else { k.clone() };
                rows.push(vec![key, self.val(v, 46, 0), fname.clone().unwrap_or_else(|| "?".into())]);
            }
        }
        let glob: Vec<Vec<String>> = st.globals.iter()
            .filter(|(k, _)| !k.starts_with("__") && !self.a.functions.contains(k))
            .take(12)
            .map(|(k, v)| vec![k.clone(), self.val(v, 46, 0), format!("{DIM}global{OFF}")])
            .collect();
        rows.extend(glob);
        if rows.is_empty() {
            say!(self, "{DIM}no variables in scope here.{OFF}");
            return Ok(());
        }
        for l in table(&["name", "value", "where"], rows, None, Some(40)) {
            say!(self, "{l}");
        }
        if let Some(c) = changed.filter(|c| !c.is_empty()) {
            say!(self, "{DIM}{c} is what step {} changed.{OFF}", nu(self.cursor));
        }
        Ok(())
    }

    fn lookup_var(st: &State, name: &str) -> Option<Ref> {
        let mut found: Option<Ref> = None;
        for f in st.frames.iter().rev() {
            if let Some(v) = f.locals.get(&name.to_string()) {
                found = v.clone();
                break;
            }
        }
        if found.is_none() {
            found = st.globals.get(&name.to_string()).cloned().flatten();
        }
        found
    }

    fn c_pv_one(&mut self, arg: Option<&str>) -> R {
        let Some(arg) = arg else { return self.need_name("pV", "variable") };
        let st = self.r.state_at(self.cursor);
        let Some(rf) = Self::lookup_var(&st, arg) else {
            say!(self, "{YELLOW}{arg} is not in scope at step {}.{OFF}", nu(self.cursor));
            if let Some(info) = self.a.variables.get(&arg.to_string()) {
                let (w, f) = (info.writes, info.first);
                say!(self, "  it is written {} times, first at step {}. `s {arg}` jumps there.", nu(w), nu(f));
            }
            return Ok(());
        };
        let o = match self.r.resolve(&rf, self.cursor as i64) {
            Resolved::Val(v) => {
                say!(self, "{BOLD}{arg}{OFF} = {}   {DIM}({}){OFF}", v.repr(), v.type_name());
                return Ok(());
            }
            Resolved::Obj(o) => o,
        };
        say!(self, "{BOLD}{arg}{OFF}  {CYAN}{}{OFF}", o.ty);
        say!(self, "  {}", take(&o.repr, draw::width().saturating_sub(4)));
        for (k, v) in o.summary.items() {
            say!(self, "  {DIM}{}{OFF} {}", pyfmt::ljust(k, 10), v.py_str());
        }
        let kids = o.children.as_ref().map(|c| self.r.decode_children(c)).unwrap_or_default();
        if !kids.is_empty() {
            let rows: Vec<Vec<String>> = kids.iter().take(30).map(|(k, v)| vec![k.clone(), self.val(v, 46, 0)]).collect();
            for l in table(&["key", "value"], rows, None, None) {
                say!(self, "  {l}");
            }
        }
        Ok(())
    }

    fn c_bt(&mut self) -> R {
        let st = self.r.state_at(self.cursor);
        if st.frames.is_empty() {
            say!(self, "{DIM}at module level -- no calls open.{OFF}");
            return Ok(());
        }
        let mut rows = Vec::new();
        for (depth, frame) in st.frames.iter().enumerate() {
            let fname = self.r.func_label(frame.func);
            let (path, line) = self.r.where_(frame.loc);
            let args: Vec<String> = frame.locals.iter().take(3).map(|(k, v)| format!("{k}={}", self.val(v, 14, 0))).collect();
            rows.push(vec![format!("{}{}", "  ".repeat(depth), fname.unwrap_or_else(|| "?".into())),
                           format!("{}:{}", rel(path.as_deref()), line_text(line)), args.join(", ")]);
        }
        rows.reverse();
        for l in table(&["frame", "at", "locals"], rows, None, None) {
            say!(self, "{l}");
        }
        Ok(())
    }

    fn c_ll(&mut self, arg: Option<&str>) -> R {
        let span = match arg { Some(a) => int(a).map_err(Fail::Value)?, None => 16 };
        let lo = (self.cursor as i64 - draw::half(span)).max(1);
        let hi = (self.r.n_steps as i64).min(lo + span - 1);
        let mut rows = Vec::new();
        for s in lo..=hi {
            let e = self.r.view(s as u64);
            let mark = if s as u64 == self.cursor { format!("{YELLOW}▸{OFF}") } else { " ".into() };
            let named = e.name.as_deref().is_some_and(|n| !n.is_empty());
            rows.push(vec![format!("{mark}{}", n(s)), format!("{}:{}", rel(e.file.as_deref()), line_text(e.line)),
                           e.kind.clone(), e.name.clone().unwrap_or_default(),
                           if named { self.val(&e.r, 30, s) } else { String::new() }]);
        }
        for l in table(&["step", "where", "event", "name", "value"], rows, None, None) {
            say!(self, "{l}");
        }
        Ok(())
    }

    // ----------------------------------------------------------------- analyse

    fn funcs_by_self(&self) -> Vec<&analyse::Func> {
        let mut fns: Vec<&analyse::Func> = self.a.functions.values().collect();
        fns.sort_by(|a, b| b.self_steps.cmp(&a.self_steps));
        fns
    }

    fn c_afl(&mut self) -> R {
        let fns = self.funcs_by_self();
        let peak = fns.iter().map(|f| f.self_steps).max().unwrap_or(1).max(1) as f64;
        let rows: Vec<Vec<String>> = fns.iter().map(|f| vec![
            f.name.clone(), nu(f.calls), nu(f.self_steps), ms(f.self_ms),
            f.callers.len().to_string(), f.callees.len().to_string(), f.writes.len().to_string(),
            format!("{CYAN}{}{OFF}", bar(f.self_steps as f64 / peak, 12)),
        ]).collect();
        let t = table(&["function", "calls", "steps", "ms", "←", "→", "vars", ""], rows,
                      Some(&['<', '>', '>', '>', '>', '>', '>', '<']), Some(30));
        for l in t {
            say!(self, "{l}");
        }
        say!(self, "{DIM}steps and ms are its own lines only, not the ones it called · ← callers → callees · vars = variables it writes{OFF}");
        say!(self, "{DIM}`af NAME` for one function · `tree` for the shape{OFF}");
        Ok(())
    }

    fn two_col(&mut self, rows: Vec<(String, u64)>, head: &[&str], limit: Option<usize>, plain_count: bool) {
        let rows: Vec<Vec<String>> = rows.into_iter()
            .map(|(k, v)| vec![k, if plain_count { v.to_string() } else { nu(v) }]).collect();
        for l in table(head, rows, Some(&['<', '>']), limit) {
            say!(self, "  {l}");
        }
    }

    fn c_af(&mut self, arg: Option<&str>) -> R {
        let mut name: Option<String> = match arg {
            Some(a) => self.a.func(a),
            None => Some(self.fn_at()),
        };
        if arg.is_none() && name.as_deref() == Some("<module>") {
            let busiest = self.funcs_by_self().into_iter().find(|f| f.name != "<module>").map(|f| f.name.clone());
            if let Some(b) = busiest {
                name = Some(b);
                say!(self, "{DIM}no function given, so showing the busiest one. `af NAME` for another, `afl` lists them.{OFF}");
            }
        }
        let Some(f) = name.as_ref().and_then(|n| self.a.functions.get(n)).cloned() else {
            say!(self, "{YELLOW}no function called {}. `afl` lists them.{OFF}", r_opt(arg));
            return Ok(());
        };
        let name = name.unwrap();
        say!(self, "{BOLD}{name}{OFF}  {DIM}{}:{}{OFF}", rel(f.file.as_deref()), f.line);
        say!(self, "  called {} times · {} steps inside · {} ms of its own", nu(f.calls), nu(f.self_steps), pyfmt::fixed(f.self_ms, 1));
        if f.raises != 0 {
            say!(self, "  {YELLOW}left by an exception {} times{OFF}", nu(f.raises));
        }
        if !f.callers.is_empty() {
            say!(self, "\n{BOLD}called by{OFF}");
            self.two_col(by_count(&f.callers), &["function", "times"], Some(10), false);
        }
        if !f.callees.is_empty() {
            say!(self, "\n{BOLD}calls{OFF}");
            self.two_col(by_count(&f.callees), &["function", "times"], Some(10), false);
        }

        let reads: Om<String, u64> = self.a.reads(&self.r).get(&name).cloned().unwrap_or_default();
        let writes = &f.writes;
        // `set(writes) | set(reads)`: a set, so nd's tie order is a hash
        // order. Writes then reads, in their own orders, here.
        let mut both: Vec<String> = writes.keys().cloned().collect();
        for k in reads.keys() {
            if !writes.contains(k) {
                both.push(k.clone());
            }
        }
        let score = |v: &String| writes.get(v).copied().unwrap_or(0) + reads.get(v).copied().unwrap_or(0);
        both.sort_by_key(|v| std::cmp::Reverse(score(v)));
        if !both.is_empty() {
            say!(self, "\n{BOLD}variables{OFF}  {DIM}(reads are from the source, writes from the run){OFF}");
            let rows: Vec<Vec<String>> = both.iter().map(|v| {
                let w = writes.get(v);
                let rd = reads.get(v);
                vec![v.clone(), w.map(|x| nu(*x)).unwrap_or_default(), rd.map(|x| x.to_string()).unwrap_or_default(),
                     match (w.is_some(), rd.is_some()) {
                         (true, true) => "write+read", (true, false) => "writes", _ => "reads only",
                     }.to_string()]
            }).collect();
            for l in table(&["name", "writes", "reads", ""], rows, Some(&['<', '>', '>', '<']), Some(20)) {
                say!(self, "  {l}");
            }
        }

        // Recorded when each call returns, so an inner call is stored before
        // the outer one it was made from. Sort, or the table reads backwards.
        let mut invs = self.a.invocations.get(&name).cloned().unwrap_or_default();
        invs.sort();
        if !invs.is_empty() {
            say!(self, "\n{BOLD}each call{OFF}");
            let mut rows = Vec::new();
            for (enter, leave) in invs.iter().take(8) {
                let e = self.r.view(*enter);
                let args: Vec<String> = e.extra.get("args").map(|a| a.items().to_vec()).unwrap_or_default().iter()
                    .map(|(k, v)| format!("{k}={}", self.val(&self.r.norm(v), 16, *enter as i64))).collect();
                let ret = self.r.view(*leave);
                let args = args.join(", ");
                rows.push(vec![nu(*enter), if args.is_empty() { "–".into() } else { args },
                               self.val(&ret.r, 24, *leave as i64), n(*leave as i64 - *enter as i64)]);
            }
            for l in table(&["at step", "arguments", "returned", "steps"], rows, Some(&['<', '<', '<', '>']), None) {
                say!(self, "  {l}");
            }
            if invs.len() > 8 {
                say!(self, "  {DIM}… {} more calls. `s fn:{name}` walks them.{OFF}", n(invs.len() as i64 - 8));
            }
        }
        Ok(())
    }

    fn c_axf(&mut self, arg: Option<&str>) -> R {
        let name = match arg {
            Some(a) => self.a.func(a),
            None => Some(self.fn_at()),
        };
        let Some(name) = name else {
            say!(self, "{YELLOW}no such function.{OFF}");
            return Ok(());
        };
        let (up, down) = match self.a.functions.get(&name) {
            Some(f) => (f.callers.iter().map(|(k, v)| (k.clone(), *v)).collect::<Vec<_>>(),
                        f.callees.iter().map(|(k, v)| (k.clone(), *v)).collect::<Vec<_>>()),
            None => (vec![], vec![]),
        };
        say!(self, "{BOLD}{name}{OFF}");
        for (who, c) in most_common(&up, 8) {
            say!(self, "  {DIM}◀── {}{OFF} {} calls", pyfmt::ljust(&who, 24), nu(c));
        }
        say!(self, "  {CYAN}{name}{OFF}");
        for (who, c) in most_common(&down, 8) {
            say!(self, "  {DIM}──▶ {}{OFF} {} calls", pyfmt::ljust(&who, 24), nu(c));
        }
        if up.is_empty() && down.is_empty() {
            say!(self, "  {DIM}nothing calls it and it calls nothing.{OFF}");
        }
        Ok(())
    }

    fn c_axv(&mut self, arg: Option<&str>) -> R {
        let Some(arg) = arg else { return self.need_name("axv", "variable") };
        let name = strip(arg).to_string();
        let Some(info) = self.a.variables.get(&name).cloned() else {
            let near = self.near_vars(&name, 8);
            say!(self, "{YELLOW}{name} is never written.{OFF}");
            if !near.is_empty() {
                say!(self, "  did you mean: {}", near.join(", "));
            }
            return Ok(());
        };
        say!(self, "{BOLD}{name}{OFF}  written {} times  {DIM}({} new values, {} in-place, {} deletes){OFF}",
             nu(info.writes), nu(info.binds), nu(info.mutates), nu(info.deletes));
        say!(self, "  alive from step {} to {}", nu(info.first), nu(info.last));
        say!(self, "\n{BOLD}written by{OFF}");
        self.two_col(by_count(&info.by_func), &["function", "times"], Some(10), false);
        say!(self, "\n{BOLD}on these lines{OFF}");
        self.two_col(by_count(&info.by_line), &["line", "times"], Some(10), false);
        let readers = self.a.readers_of(&self.r, &name);
        if !readers.is_empty() {
            say!(self, "\n{BOLD}read by{OFF}  {DIM}(from the source){OFF}");
            self.two_col(most_common(&readers, 10), &["function", "places"], None, true);
        }
        let (series, _) = self.r.numeric_series(&name, draw::width());
        let nums: Vec<Num> = series.iter().filter_map(|(_, v)| *v).collect();
        if nums.len() > 3 {
            let vals: Vec<Option<Num>> = series.iter().map(|(_, v)| *v).collect();
            say!(self, "\n  {CYAN}{}{OFF}  {DIM}{} → {}{OFF}", sparkline(&vals), draw::numn(nums[0]), draw::numn(*nums.last().unwrap()));
        }
        say!(self, "\n{DIM}`ch {name}` for the value table · `pl {name}` to plot it · `s {name}` to jump there{OFF}");
        Ok(())
    }

    fn c_fv(&mut self, arg: Option<&str>) -> R {
        let fns: Vec<analyse::Func> = match arg {
            Some(a) => {
                let low = a.to_lowercase();
                self.a.functions.values().filter(|f| f.name.to_lowercase().contains(&low)).take(12).cloned().collect()
            }
            None => self.funcs_by_self().into_iter().take(12).cloned().collect(),
        };
        if fns.is_empty() {
            say!(self, "{YELLOW}no functions match.{OFF}");
            return Ok(());
        }
        let reads = self.a.reads(&self.r).clone();
        let mut score: Om<String, u64> = Om::new();
        for f in &fns {
            for (v, c) in f.writes.iter() {
                *score.entry_or(v.clone(), || 0) += c;
            }
            if let Some(rd) = reads.get(&f.name) {
                for (v, c) in rd.iter() {
                    *score.entry_or(v.clone(), || 0) += c;
                }
            }
        }
        let names: Vec<String> = by_count(&score).into_iter().map(|(k, _)| k)
            .filter(|v| !self.a.functions.contains(v)).take(14).collect();
        if names.is_empty() {
            say!(self, "{DIM}nothing to cross.{OFF}");
            return Ok(());
        }
        let mut head: Vec<String> = vec!["function".into()];
        head.extend(names.iter().map(|v| take(v, 7).to_string()));
        let mut rows = Vec::new();
        let empty = Om::new();
        for f in &fns {
            let rd = reads.get(&f.name).unwrap_or(&empty);
            let cells: Vec<String> = names.iter().map(|v| {
                let (w, rr) = (f.writes.get(v).copied().unwrap_or(0), rd.get(v).copied().unwrap_or(0));
                if w != 0 && rr != 0 {
                    format!("{GREEN}W{OFF}r")
                } else if w != 0 {
                    format!("{GREEN}W{OFF}")
                } else if rr != 0 {
                    format!("{CYAN}r{OFF}")
                } else {
                    format!("{DIM}·{OFF}")
                }
            }).collect();
            if cells.iter().any(|c| !c.contains('·')) {
                let mut row = vec![take(&f.name, 18).to_string()];
                row.extend(cells);
                rows.push(row);
            }
        }
        let mut aligns = vec!['<'];
        aligns.extend(std::iter::repeat_n('^', names.len()));
        let head_refs: Vec<&str> = head.iter().map(|s| s.as_str()).collect();
        for l in table(&head_refs, rows, Some(&aligns), None) {
            say!(self, "{l}");
        }
        say!(self, "{DIM}W = writes it during the run · r = reads it in the source · · = no connection{OFF}");
        Ok(())
    }

    fn c_vl(&mut self, arg: Option<&str>) -> R {
        let mut items: Vec<(&String, &analyse::Var)> = self.a.variables.iter().collect();
        items.sort_by(|a, b| b.1.writes.cmp(&a.1.writes));
        if let Some(a) = arg {
            let low = a.to_lowercase();
            items.retain(|(k, _)| k.to_lowercase().contains(&low));
        }
        let rows: Vec<Vec<String>> = items.iter().map(|(name, v)| {
            // max(by_func, key=by_func.get): the first of the largest.
            let mut owner: Option<(&String, u64)> = None;
            for (k, c) in v.by_func.iter() {
                if owner.is_none_or(|(_, best)| *c > best) {
                    owner = Some((k, *c));
                }
            }
            vec![(*name).clone(), nu(v.writes), nu(v.binds), nu(v.mutates),
                 owner.map(|o| o.0.clone()).unwrap_or_else(|| "?".into()),
                 format!("{}–{}", nu(v.first), nu(v.last))]
        }).collect();
        for l in table(&["name", "writes", "new", "in-place", "mostly in", "steps"], rows,
                       Some(&['<', '>', '>', '>', '<', '>']), Some(35)) {
            say!(self, "{l}");
        }
        Ok(())
    }

    fn c_ch(&mut self, arg: Option<&str>) -> R {
        let Some(arg) = arg else { return self.need_name("ch", "variable") };
        let parts = pyfmt::split(arg);
        let name = parts[0].to_string();
        let limit = match parts.get(1) { Some(p) => int(p).map_err(Fail::Value)?, None => 25 };
        let steps = self.r.history_steps(&name, limit);
        if steps.is_empty() {
            return self.c_axv(Some(&name));
        }
        let mut out = Vec::new();
        let mut prev: Option<Ref> = None;
        for s in &steps {
            let e = self.r.view(*s);
            let was = match &prev {
                Some(Ref::I(v)) => format!("{DIM}was {}{OFF}", v.repr()),
                _ => String::new(),
            };
            out.push(vec![nu(*s), format!("{}:{}", rel(e.file.as_deref()), line_text(e.line)), e.kind.clone(),
                          self.val(&e.r, 34, *s as i64), was]);
            prev = e.r;
        }
        let shown = steps.len();
        for l in table(&["step", "where", "how", &name, ""], out, None, None) {
            say!(self, "{l}");
        }
        let total = self.a.variables.get(&name).map(|v| v.writes).unwrap_or(shown as u64);
        if total > shown as u64 {
            say!(self, "{DIM}showing {shown} of {} changes. `ch {name} {}` for more.{OFF}", nu(total), limit.saturating_mul(4));
        }
        Ok(())
    }

    fn c_pl(&mut self, arg: Option<&str>) -> R {
        let Some(arg) = arg else { return self.need_name("pl", "variable") };
        let name = pyfmt::split(arg)[0].to_string();
        let (series, obj) = self.r.numeric_series(&name, draw::width() - 14);
        if series.is_empty() {
            return self.c_axv(Some(&name));
        }
        let values: Vec<Option<Num>> = series.iter().map(|(_, v)| *v).collect();
        if values.iter().any(|v| v.is_some()) {
            let label = format!("{name} over the run");
            let xlabel = format!("step {} → {}", nu(series[0].0), nu(series[series.len() - 1].0));
            for l in plot(&values, &label, &xlabel) {
                say!(self, "{l}");
            }
            let nums: Vec<Num> = values.iter().flatten().copied().collect();
            // min()/max() keep the first of equals, as Python's do.
            let mut lo = nums[0];
            let mut hi = nums[0];
            for v in &nums[1..] {
                if v.f() < lo.f() {
                    lo = *v;
                }
                if v.f() > hi.f() {
                    hi = *v;
                }
            }
            say!(self, "{DIM}min {} · max {} · first {} · last {}{OFF}", draw::numn(lo), draw::numn(hi),
                 draw::numn(nums[0]), draw::numn(nums[nums.len() - 1]));
            return Ok(());
        }
        if let Some(obj) = obj.filter(|o| *o != 0) {
            let versions = self.r.object_series(obj);
            for key in ["len", "rows", "nbytes"] {
                let got: Vec<Option<Num>> = versions.iter().filter_map(|(_, sm, nb)| {
                    let v = if key == "nbytes" { nb.as_ref() } else { sm.get(key) };
                    match v {
                        Some(Json::Int(i)) => Some(Some(Num::I(*i))),
                        Some(Json::Bool(b)) => Some(Some(Num::I(*b as i64))),
                        Some(Json::Float(f)) => Some(Some(Num::F(*f))),
                        _ => None,
                    }
                }).collect();
                if got.len() > 3 {
                    for l in plot(&got, &format!("{name} · {key} of the object"), "") {
                        say!(self, "{l}");
                    }
                    return Ok(());
                }
            }
        }
        say!(self, "{DIM}{name} holds objects with nothing numeric to plot. try `ch {name}`.{OFF}");
        Ok(())
    }

    fn c_tt(&mut self) -> R {
        let mut order: Vec<(&String, u64)> = self.a.activity.iter().map(|(k, v)| (k, v.iter().sum())).collect();
        order.sort_by(|a, b| b.1.cmp(&a.1));
        order.truncate(14);
        if order.is_empty() {
            say!(self, "{DIM}nothing to draw.{OFF}");
            return Ok(());
        }
        let names: Vec<String> = order.iter().map(|(k, _)| (*k).clone()).collect();
        let series: Vec<Vec<u64>> = names.iter().map(|k| self.a.activity.get(k).cloned().unwrap_or_default()).collect();
        say!(self, "{DIM}who was running, left = start of the run, right = the end{OFF}");
        for l in heatrows(&names, &series) {
            say!(self, "{l}");
        }
        let cells = draw::width() as i64 - 26;
        let at = ((self.cursor as f64 - 1.0) / (self.r.n_steps as f64 - 1.0).max(1.0) * (cells - 1) as f64) as i64;
        say!(self, "{:<22} {}{YELLOW}▲{OFF}", "", " ".repeat(at.max(0) as usize));
        say!(self, "{:<22} {DIM}you are here: step {}{OFF}", "", nu(self.cursor));
        Ok(())
    }

    fn c_tree(&mut self, arg: Option<&str>) -> R {
        let mut root = match arg {
            Some(a) => self.a.func(a).unwrap_or_default(),
            None => "<module>".into(),
        };
        if !self.a.functions.contains(&root) {
            root = "<module>".into();
        }
        let fns = &self.a.functions;
        let kids = |name: &str| -> Vec<String> {
            let Some(f) = fns.get(&name.to_string()) else { return vec![] };
            by_count(&f.callees).into_iter().map(|(k, _)| k).filter(|k| fns.contains(k) && k != name).take(8).collect()
        };
        let label = |name: &str| -> String {
            let f = fns.get(&name.to_string());
            let (calls, steps, msv) = f.map(|f| (f.calls, f.self_steps, f.self_ms)).unwrap_or((0, 0, 0.0));
            let mark = match f.and_then(|f| f.callees.get(&name.to_string())) {
                Some(c) => format!("  {YELLOW}↻ calls itself {} times{OFF}", nu(*c)),
                None => String::new(),
            };
            format!("{BOLD}{name}{OFF} {DIM}{} calls · {} steps · {} ms{OFF}{mark}", nu(calls), nu(steps), pyfmt::fixed(msv, 1))
        };
        let lines = draw::tree(&root, &kids, &label);
        for l in lines {
            say!(self, "{l}");
        }
        Ok(())
    }

    fn c_cmp(&mut self, arg: Option<&str>) -> R {
        let parts = pyfmt::split(arg.unwrap_or(""));
        if parts.is_empty() {
            say!(self, "cmp needs two steps: cmp 100 2000 (one step compares it with the cursor)");
            return Ok(());
        }
        let a = int(parts[0]).map_err(Fail::Value)?;
        let b = match parts.get(1) { Some(p) => int(p).map_err(Fail::Value)?, None => self.cursor as i64 };
        let clamp = |s: i64| s.max(1).min(i64::MAX) as u64;
        let (sa, sb) = (self.r.state_at(clamp(a)), self.r.state_at(clamp(b)));
        let flat = |st: &State| {
            let mut out: Om<String, Option<Ref>> = Om::new();
            for (k, v) in st.globals.iter() {
                out.insert(k.clone(), v.clone());
            }
            for f in &st.frames {
                for (k, v) in f.locals.iter() {
                    out.insert(k.clone(), v.clone());
                }
            }
            out
        };
        let (fa, fb) = (flat(&sa), flat(&sb));
        let mut keys: Vec<&String> = fa.keys().chain(fb.keys()).collect::<HashSet<_>>().into_iter().collect();
        keys.sort();
        let mut rows = Vec::new();
        for k in keys {
            if k.starts_with("__") {
                continue;
            }
            let va = fa.get(k).cloned().flatten();
            let vb = fb.get(k).cloned().flatten();
            if Ref::same(&va, &vb) {
                continue;
            }
            rows.push(vec![k.clone(),
                           if va.is_some() { self.val(&va, 28, a) } else { format!("{DIM}(not yet){OFF}") },
                           if vb.is_some() { self.val(&vb, 28, b) } else { format!("{DIM}(gone){OFF}") }]);
        }
        say!(self, "{BOLD}step {} → step {}{OFF}", n(a), n(b));
        if rows.is_empty() {
            say!(self, "  {DIM}nothing visible changed.{OFF}");
            return Ok(());
        }
        let (ha, hb) = (format!("at {}", n(a)), format!("at {}", n(b)));
        for l in table(&["name", &ha, &hb], rows, None, Some(30)) {
            say!(self, "{l}");
        }
        Ok(())
    }

    fn c_search(&mut self, arg: &str) -> R {
        if arg.is_empty() {
            say!(self, "/ needs something to look for.");
            return Ok(());
        }
        let text = strip(arg).to_lowercase();
        let hits: Vec<String> = self.a.variables.keys().filter(|v| v.to_lowercase().contains(&text)).cloned().collect();
        let fns: Vec<String> = self.a.functions.keys().filter(|f| f.to_lowercase().contains(&text)).cloned().collect();
        if !fns.is_empty() {
            say!(self, "{BOLD}functions{OFF}  {}", fns.iter().take(12).cloned().collect::<Vec<_>>().join(", "));
        }
        if !hits.is_empty() {
            say!(self, "{BOLD}variables{OFF}  {}", hits.iter().take(14).cloned().collect::<Vec<_>>().join(", "));
        }
        let (path, _) = self.here();
        let source = self.r.source(&path);
        let mut rows = Vec::new();
        for (i, lt) in pyfmt::splitlines(&source).iter().enumerate() {
            let i = i as i64 + 1;
            if lt.to_lowercase().contains(&text) {
                let hit = self.lines.get(&(Some(path.clone()), Some(i))).map(|h| h.0).unwrap_or(0);
                rows.push(vec![i.to_string(), if hit != 0 { n(hit) } else { format!("{YELLOW}never ran{OFF}") },
                               take(strip(lt), 70).to_string()]);
            }
        }
        let any_rows = !rows.is_empty();
        if any_rows {
            say!(self, "{BOLD}in {}{OFF}", rel(Some(&path)));
            for l in table(&["line", "hits", "source"], rows, None, Some(15)) {
                say!(self, "  {l}");
            }
        }
        if hits.is_empty() && fns.is_empty() && !any_rows {
            say!(self, "{DIM}nothing matches {}.{OFF}", r(arg));
        }
        Ok(())
    }

    fn c_dis(&mut self) -> R {
        let (path, _) = self.here();
        let source = self.r.source(&path);
        let src = pyfmt::splitlines(&source);
        let mut stats: Vec<(Option<i64>, i64, i64)> = self.lines.iter()
            .filter(|((p, _), _)| p.as_deref() == Some(path.as_str()))
            .map(|((_, ln), (h, t))| (*ln, *h, *t)).collect();
        stats.sort_by(|a, b| b.1.cmp(&a.1));
        let peak = stats.first().map(|s| s.1).unwrap_or(1);
        if peak == 0 && !stats.is_empty() {
            return Err(Fail::Value("division by zero".into()));
        }
        let mut rows = Vec::new();
        for (ln, hits, t_ns) in stats.iter().take(20) {
            let text = match ln {
                Some(l) if *l <= src.len() as i64 => {
                    // src[ln - 1]: line 0 is Python's src[-1], the last line.
                    let idx = l - 1;
                    let idx = if idx < 0 { src.len() as i64 + idx } else { idx };
                    if idx < 0 {
                        return Err(Fail::Index("list index out of range".into()));
                    }
                    take(strip(src[idx as usize]), 54).to_string()
                }
                _ => String::new(),
            };
            rows.push(vec![line_text(*ln), n(*hits), pyfmt::fixed(*t_ns as f64 / 1e6, 1),
                           format!("{CYAN}{}{OFF}", bar(*hits as f64 / peak as f64, 10)), text]);
        }
        for l in table(&["line", "hits", "ms", "", "source"], rows, Some(&['<', '>', '>', '<', '<']), None) {
            say!(self, "{l}");
        }
        Ok(())
    }

    fn c_info(&mut self) -> R {
        say!(self, "{BOLD}{}{OFF}", self.base);
        for key in ["script", "python", "platform", "driver", "wall_seconds", "error"] {
            if let Some(v) = self.r.meta(key) {
                let v = v.py_str();
                say!(self, "  {DIM}{}{OFF} {v}", pyfmt::ljust(key, 13));
            }
        }
        say!(self, "  {DIM}{}{OFF} {}", pyfmt::ljust("steps", 13), nu(self.r.n_steps));
        say!(self, "  {DIM}{}{OFF} {} functions, {} variables", pyfmt::ljust("found", 13), self.a.functions.len(), self.a.variables.len());
        Ok(())
    }

    // -------------------------------------------------------------------- help

    fn c_help(&mut self, arg: Option<&str>) -> R {
        if let Some(a) = arg {
            match lookup(strip(a)) {
                Some(c) => say!(self, "{}", doc(c).unwrap_or("no help")),
                None => say!(self, "{YELLOW}no command {}.{OFF}", r(a)),
            }
            return Ok(());
        }
        say!(self, "{DIM}every command works with no arguments. the cursor is one step of the run.{OFF}");
        for (title, items) in GROUPS {
            say!(self, "\n{BOLD}{title}{OFF}");
            for (cmd, text) in items.iter() {
                say!(self, "  {CYAN}{}{OFF} {text}", pyfmt::ljust(cmd, 10));
            }
        }
        say!(self, "\n{CYAN}q{OFF} to leave · {CYAN}??{OFF} for a worked example");
        Ok(())
    }

    fn c_tour(&mut self) -> R {
        let hot = self.funcs_by_self().into_iter().find(|f| f.name != "<module>").map(|f| f.name.clone())
            .unwrap_or_else(|| "<module>".into());
        let mut var: Option<(&String, u64)> = None;
        for (k, v) in self.a.variables.iter() {
            if var.is_none_or(|(_, w)| v.writes > w) {
                var = Some((k, v.writes));
            }
        }
        let var = var.map(|v| v.0.clone()).unwrap_or_else(|| "x".into());
        say!(self, "{BOLD}a walk through this run, in six commands{OFF}");
        for (cmd, why) in [
            ("afl".to_string(), "which functions did the work".to_string()),
            (format!("af {hot}"), format!("what {hot} is wired to")),
            (format!("axv {var}"), format!("who writes {var}, and who reads it")),
            (format!("s {var}"), format!("jump to the first change of {var}")),
            ("pd".to_string(), "see the code there, with the cursor on it".to_string()),
            (format!("pl {var}"), format!("draw {var} over the whole run")),
        ] {
            say!(self, "  {CYAN}{}{OFF} {DIM}{why}{OFF}", pyfmt::ljust(&cmd, 18));
        }
        Ok(())
    }

    // --------------------------------------------------------------- dispatch

    /// Run one line. `;` separates commands, as in r2.
    pub fn run(&mut self, text: &str) {
        for piece in text.split(';') {
            let piece = strip(piece);
            if piece.is_empty() || piece.starts_with('#') {
                continue;
            }
            INTERRUPTED.store(false, Ordering::Relaxed);
            self.one(piece);
            self.flush();
        }
    }

    fn one(&mut self, piece: &str) {
        if let Some(rest) = piece.strip_prefix('/') {
            let got = self.c_search(rest);
            return self.report(got, "/");
        }
        if let Some(rest) = piece.strip_prefix('?') {
            let got = if rest.starts_with('?') {
                self.c_tour()
            } else {
                self.c_help(if rest.is_empty() { None } else { Some(rest) })
            };
            return self.report(got, "?");
        }
        let (head, rest) = piece.split_once(' ').unwrap_or((piece, ""));
        let Some(cmd) = lookup(head) else {
            // A bare number is a seek: `1200` means `s 1200`.
            if pyfmt::isdigit(head.trim_start_matches(['+', '-']).trim_end_matches('%')) {
                let got = self.c_s(Some(piece));
                return self.report(got, "s");
            }
            // A bare name is almost always the thing you want to ask about.
            let h = head.to_string();
            if self.a.variables.contains(&h) && !self.a.functions.contains(&h) {
                say!(self, "{DIM}(reading that as `axv {head}`){OFF}");
                let got = self.c_axv(Some(head));
                return self.report(got, "axv");
            }
            if self.a.functions.contains(&h) {
                say!(self, "{DIM}(reading that as `af {head}`){OFF}");
                let got = self.c_af(Some(head));
                return self.report(got, "af");
            }
            say!(self, "{YELLOW}no command {}. press ? for the list.{OFF}", r(head));
            let first: String = head.chars().take(1).collect();
            let mut near: Vec<&str> = TABLE.iter().map(|(k, _)| *k).filter(|k| k.starts_with(&first)).collect();
            if !near.is_empty() {
                near.sort();
                near.truncate(10);
                say!(self, "{DIM}commands starting with {}: {}{OFF}", r(&first), near.join(" "));
            }
            return;
        };
        let rest = strip(rest);
        let arg = if rest.is_empty() { None } else { Some(rest) };
        let got = match cmd {
            Cmd::S => self.c_s(arg),
            Cmd::N => self.c_n(arg),
            Cmd::P => self.c_p(arg),
            Cmd::F => self.c_f(),
            Cmd::FBack => self.c_fback(),
            Cmd::U => self.c_u(),
            Cmd::C => self.c_c(arg),
            Cmd::Pd => self.c_pd(arg),
            Cmd::Pv => self.c_pv(),
            Cmd::PV => self.c_pv_one(arg),
            Cmd::Bt => self.c_bt(),
            Cmd::Ll => self.c_ll(arg),
            Cmd::Afl => self.c_afl(),
            Cmd::Af => self.c_af(arg),
            Cmd::Axf => self.c_axf(arg),
            Cmd::Axv => self.c_axv(arg),
            Cmd::Fv => self.c_fv(arg),
            Cmd::Vl => self.c_vl(arg),
            Cmd::Ch => self.c_ch(arg),
            Cmd::Pl => self.c_pl(arg),
            Cmd::Tt => self.c_tt(),
            Cmd::Tree => self.c_tree(arg),
            Cmd::Cmp => self.c_cmp(arg),
            Cmd::Dis => self.c_dis(),
            Cmd::Info => self.c_info(),
            Cmd::Help => self.c_help(arg),
            Cmd::Quit => {
                self.running = false;
                Ok(())
            }
        };
        match got {
            Err(Fail::Value(m)) | Err(Fail::Index(m)) => {
                say!(self, "{YELLOW}{m}{OFF}  ({})", doc(cmd).unwrap_or(head));
            }
            Err(Fail::Interrupt) => say!(self),
            Ok(()) => {}
        }
    }

    /// Errors from the routes `nd x` does not wrap in its handler. There a
    /// ValueError would end the session with a traceback; here it is shown
    /// the way the REPL's last-resort handler shows any error.
    fn report(&mut self, got: R, _what: &str) {
        match got {
            Err(Fail::Value(m)) => say!(self, "{RED}ValueError: {m}{OFF}"),
            Err(Fail::Index(m)) => say!(self, "{RED}IndexError: {m}{OFF}"),
            Err(Fail::Interrupt) => say!(self),
            Ok(()) => {}
        }
    }

    /// Words for tab completion: commands, functions, variables.
    pub fn words(&self) -> Vec<String> {
        let mut w: Vec<String> = TABLE.iter().map(|(k, _)| k.to_string())
            .chain(self.a.functions.keys().cloned())
            .chain(self.a.variables.keys().cloned())
            .collect::<HashSet<_>>().into_iter().collect();
        w.sort();
        w
    }
}

/// `^@?(?:([\w.]+\.\w+):)?(\d+)$` -> (file, digits).
fn line_target(t: &str) -> Option<(Option<String>, String)> {
    let s = t.strip_prefix('@').unwrap_or(t);
    let word = |c: char| c.is_alphanumeric() || c == '_';
    let digits_only = |d: &str| !d.is_empty() && d.chars().all(|c| c.is_ascii_digit());
    match s.split_once(':') {
        None => digits_only(s).then(|| (None, s.to_string())),
        Some((file, num)) => {
            if !digits_only(num) || !file.chars().all(|c| word(c) || c == '.') {
                return None;
            }
            let dot = file.rfind('.')?;
            let tail = &file[dot + 1..];
            if dot == 0 || tail.is_empty() || !tail.chars().all(word) {
                return None;
            }
            Some((Some(file.to_string()), num.to_string()))
        }
    }
}

// --------------------------------------------------------------------------
// the prompt
// --------------------------------------------------------------------------

mod prompt {
    use rustyline::completion::{Completer, Pair};
    use rustyline::highlight::Highlighter;
    use rustyline::hint::Hinter;
    use rustyline::validate::Validator;
    use rustyline::{Context, Helper};

    /// Tab completion of commands, function and variable names.
    pub struct Words(pub Vec<String>);

    impl Completer for Words {
        type Candidate = Pair;
        fn complete(&self, line: &str, pos: usize, _: &Context<'_>) -> rustyline::Result<(usize, Vec<Pair>)> {
            let start = line[..pos].rfind([' ', '\t']).map(|i| i + 1).unwrap_or(0);
            let text = &line[start..pos];
            let got = self.0.iter().filter(|w| w.starts_with(text))
                .map(|w| Pair { display: w.clone(), replacement: format!("{w} ") }).collect();
            Ok((start, got))
        }
    }
    impl Hinter for Words {
        type Hint = String;
    }
    impl Highlighter for Words {}
    impl Validator for Words {}
    impl Helper for Words {}
}

/// `~/.nd36_history`: the file nd's readline uses, read and written in
/// readline's plain one-entry-per-line format so both tools share it.
fn history_path() -> Option<std::path::PathBuf> {
    std::env::var_os("HOME").map(|h| std::path::PathBuf::from(h).join(".nd36_history"))
}

fn read_history(path: &std::path::Path) -> Vec<String> {
    let Ok(text) = std::fs::read_to_string(path) else { return vec![] };
    let mut lines: Vec<&str> = text.lines().collect();
    let libedit = lines.first() == Some(&"_HiStOrY_V2_");
    if libedit {
        lines.remove(0);
    }
    lines.into_iter()
        .filter(|l| !(l.len() > 1 && l.starts_with('#') && l[1..].chars().all(|c| c.is_ascii_digit())))
        .map(|l| if libedit { unescape_libedit(l) } else { l.to_string() })
        .collect()
}

/// libedit (macOS) writes spaces and the like as `\040`.
fn unescape_libedit(l: &str) -> String {
    let b = l.as_bytes();
    let mut out = Vec::new();
    let mut i = 0;
    while i < b.len() {
        if b[i] == b'\\' && i + 3 < b.len() && b[i + 1..i + 4].iter().all(|c| (b'0'..=b'7').contains(c)) {
            out.push(((b[i + 1] - b'0') << 6) | ((b[i + 2] - b'0') << 3) | (b[i + 3] - b'0'));
            i += 4;
        } else {
            out.push(b[i]);
            i += 1;
        }
    }
    String::from_utf8_lossy(&out).into_owned()
}

/// One line from a plain stdin, as Python's `input()` reads it when the
/// terminal is not interactive: the prompt goes to stdout, EOF is None.
fn plain_input(prompt: &str) -> Option<String> {
    use std::io::BufRead;
    print!("{prompt}");
    let _ = std::io::stdout().flush();
    let mut line = String::new();
    match std::io::stdin().lock().read_line(&mut line) {
        Ok(0) | Err(_) => None,
        Ok(_) => {
            if line.ends_with('\n') {
                line.pop();
            }
            Some(line)
        }
    }
}

/// readline's `write_history_file` + `history_truncate_file`: the last
/// 2000 entries, one per line, in a file only its owner can read.
fn write_history(path: &std::path::Path, entries: &[String]) {
    use std::os::unix::fs::OpenOptionsExt;
    let keep = &entries[entries.len().saturating_sub(2000)..];
    let mut text = String::new();
    for e in keep {
        text.push_str(e);
        text.push('\n');
    }
    let f = std::fs::OpenOptions::new().write(true).create(true).truncate(true).mode(0o600).open(path);
    if let Ok(mut f) = f {
        let _ = f.write_all(text.as_bytes());
        // readline writes through a temp file and renames it, so the file
        // ends up 0600 even when it existed with looser permissions.
        use std::os::unix::fs::PermissionsExt;
        let _ = f.set_permissions(std::fs::Permissions::from_mode(0o600));
    }
}

/// The interactive session.
pub fn repl(r: Store, path: &str) {
    use rustyline::history::{History, MemHistory};
    use rustyline::{Config, Editor};
    use std::io::IsTerminal;

    let mut ex = Explorer::new(r, path, false, true);
    catch_ctrl_c();
    let hist = history_path();
    let mut entries: Vec<String> = hist.as_deref().map(read_history).unwrap_or_default();

    // Line editing only when both ends are a terminal, as Python's input()
    // decides; otherwise the prompt is printed and stdin read plainly.
    let mut ed: Option<Editor<prompt::Words, MemHistory>> = None;
    if std::io::stdin().is_terminal() && std::io::stdout().is_terminal() {
        let config = Config::builder()
            .max_history_size(2000).unwrap_or_else(|_| Config::builder())
            .completion_type(rustyline::CompletionType::List)
            .auto_add_history(true)
            .build();
        if let Ok(mut e) = Editor::with_history(config, MemHistory::new()) {
            e.set_helper(Some(prompt::Words(ex.words())));
            for line in &entries {
                let _ = e.history_mut().add(line);
            }
            ed = Some(e);
        }
    }

    say!(ex, "\n{BOLD}{}{OFF} · {} steps · {DIM}press ? for commands, q to leave{OFF}", ex.base, nu(ex.r.n_steps));
    ex.status();
    ex.flush();
    while ex.running {
        let prompt = ex.prompt();
        let line = match ed.as_mut() {
            Some(e) => e.readline(&prompt).ok(),
            None => plain_input(&prompt),
        };
        let Some(line) = line else {
            println!();
            break;
        };
        if strip(&line).is_empty() {
            continue;
        }
        ex.run(&line);
    }
    if let Some(h) = hist {
        if let Some(e) = &ed {
            entries = e.history().into_iter().cloned().collect();
        }
        write_history(&h, &entries);
    }
}

/// `ndr x TRACE -c 'afl'` -- run and exit, so it can be piped.
pub fn once(r: Store, path: &str, commands: &[String]) {
    let mut ex = Explorer::new(r, path, true, true);
    catch_ctrl_c();
    for c in commands {
        ex.run(c);
    }
    ex.flush();
}

#[cfg(test)]
mod tests;
