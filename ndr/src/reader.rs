//! The read side, in pure Rust: no Python involved.
//!
//! `ndr info`, `ndr watch` and `ndr x` use this, so a trace can be read
//! without the Python reader in the loop. `ndr x` builds on it in
//! `explore/store.rs`.

use crate::schema::{self, Record, Vref, RECORD_SIZE};
use rusqlite::{Connection, OpenFlags};
use std::collections::HashMap;
use std::fs::File;
use std::io::Read;
use std::os::unix::fs::FileExt;
use std::path::{Path, PathBuf};

pub struct Trace {
    pub dir: PathBuf,
    pub db: Connection,
    log: File,
    pub n_steps: u64,
    pub names: HashMap<u32, String>,
    pub name_ids: HashMap<String, u32>,
    pub locs: HashMap<u32, (u32, i64)>,
    pub files: HashMap<u32, String>,
    pub funcs: HashMap<u32, String>,
    pub meta: Vec<(String, serde_json::Value)>,
}

impl Trace {
    /// Open a trace folder, or say in plain words why not.
    pub fn open(path: &Path) -> Result<Trace, String> {
        let p = PathBuf::from(path.to_string_lossy().trim_end_matches('/'));
        if !p.exists() {
            let stem = p.file_stem().map(|s| s.to_string_lossy().to_string()).unwrap_or_default();
            let mut msg = format!("{} does not exist.", p.display());
            if p.extension().map(|e| e == "py").unwrap_or(false) {
                msg += &format!("\nthat is a program, not a trace. record it first:\n  ndr run {}      → makes {stem}.nd", p.display());
            }
            return Err(msg);
        }
        if !p.is_dir() {
            return Err(format!("{} is a file; a trace is a folder.\nyou want the {}.nd folder next to it.",
                               p.display(), p.file_stem().map(|s| s.to_string_lossy()).unwrap_or_default()));
        }
        if !p.join("trace.db").exists() {
            return Err(format!("{} is not a trace folder (no trace.db inside).", p.display()));
        }
        let db = Connection::open_with_flags(p.join("trace.db"), OpenFlags::SQLITE_OPEN_READ_ONLY)
            .map_err(|e| format!("cannot read {}: {e}", p.display()))?;
        let log = File::open(p.join("events.bin")).map_err(|e| format!("events.bin: {e}"))?;
        let n_steps = log.metadata().map(|m| m.len()).unwrap_or(0) / RECORD_SIZE as u64;

        let q = |sql: &str| -> rusqlite::Result<Vec<(u32, String)>> {
            let mut st = db.prepare(sql)?;
            let rows = st.query_map([], |r| Ok((r.get::<_, u32>(0)?, r.get::<_, String>(1)?)))?;
            rows.collect()
        };
        let names: HashMap<u32, String> = q("SELECT id, text FROM names").unwrap_or_default().into_iter().collect();
        let name_ids = names.iter().map(|(k, v)| (v.clone(), *k)).collect();
        let files = q("SELECT id, path FROM files").unwrap_or_default().into_iter().collect();
        let funcs = q("SELECT id, COALESCE(qualname, name) FROM funcs").unwrap_or_default().into_iter().collect();
        let mut locs = HashMap::new();
        {
            let mut st = db.prepare("SELECT id, file, line FROM locs").map_err(|e| e.to_string())?;
            let rows = st.query_map([], |r| Ok((r.get::<_, u32>(0)?, (r.get::<_, u32>(1)?, r.get::<_, i64>(2)?))))
                .map_err(|e| e.to_string())?;
            for r in rows.flatten() {
                locs.insert(r.0, r.1);
            }
        }
        let mut meta = Vec::new();
        {
            let mut st = db.prepare("SELECT key, value FROM meta").map_err(|e| e.to_string())?;
            let rows = st.query_map([], |r| Ok((r.get::<_, String>(0)?, r.get::<_, String>(1)?)))
                .map_err(|e| e.to_string())?;
            for (k, v) in rows.flatten() {
                meta.push((k, serde_json::from_str(&v).unwrap_or(serde_json::Value::String(v))));
            }
        }
        Ok(Trace { dir: p, db, log, n_steps, names, name_ids, locs, files, funcs, meta })
    }

    pub fn meta(&self, key: &str) -> Option<&serde_json::Value> {
        self.meta.iter().find(|(k, _)| k == key).map(|(_, v)| v)
    }

    /// One event, by step number (1-based). O(1): a positioned read.
    pub fn rec(&self, step: u64) -> Option<Record> {
        if step == 0 || step > self.n_steps {
            return None;
        }
        let mut b = [0u8; RECORD_SIZE];
        self.log.read_exact_at(&mut b, (step - 1) * RECORD_SIZE as u64).ok()?;
        Some(schema::unpack(&b))
    }

    /// Every event in order, streamed in 4 MB chunks.
    pub fn each(&self, mut f: impl FnMut(u64, &Record)) {
        let mut file = match File::open(self.dir.join("events.bin")) {
            Ok(f) => f,
            Err(_) => return,
        };
        let mut buf = vec![0u8; RECORD_SIZE * 100_000];
        let mut step = 0u64;
        let mut carry = 0usize;
        loop {
            let n = match file.read(&mut buf[carry..]) {
                Ok(0) | Err(_) => break,
                Ok(n) => n + carry,
            };
            let whole = n / RECORD_SIZE * RECORD_SIZE;
            for chunk in buf[..whole].chunks_exact(RECORD_SIZE) {
                step += 1;
                f(step, &schema::unpack(chunk));
            }
            buf.copy_within(whole..n, 0);
            carry = n - whole;
        }
    }

    pub fn place(&self, loc: u32) -> (String, i64) {
        match self.locs.get(&loc) {
            Some((file, line)) => {
                let path = self.files.get(file).cloned().unwrap_or_default();
                let base = Path::new(&path).file_name().map(|s| s.to_string_lossy().to_string()).unwrap_or(path);
                (base, *line)
            }
            None => ("?".into(), 0),
        }
    }

    pub fn string(&self, id: i64) -> String {
        self.db.query_row("SELECT text FROM strings WHERE id=?1", [id], |r| r.get::<_, String>(0))
            .unwrap_or_default()
    }

    /// A value as one line of text, the way it looked at `step`.
    pub fn show(&self, v: &Vref, step: u64) -> String {
        match v.rk {
            schema::RK_ABSENT => "–".into(),
            schema::RK_INT => v.ri.to_string(),
            schema::RK_FLOAT => {
                let mut s = String::new();
                schema::push_float(&mut s, v.rf);
                s
            }
            schema::RK_BOOL => if v.ri != 0 { "True".into() } else { "False".into() },
            schema::RK_NULL => "None".into(),
            schema::RK_STR => format!("{:?}", self.string(v.ri)),
            schema::RK_OBJ => self.db
                .query_row(
                    "SELECT repr FROM objects WHERE obj_id=?1 AND step<=?2 ORDER BY step DESC LIMIT 1",
                    [v.ri, step as i64],
                    |r| r.get::<_, String>(0),
                )
                .unwrap_or_else(|_| format!("<obj {}>", v.ri)),
            _ => "?".into(),
        }
    }

    pub fn variable_counts(&self) -> Vec<(String, u64)> {
        let mut out = Vec::new();
        if let Ok(mut st) = self.db.prepare("SELECT name, COUNT(*) FROM binds GROUP BY name") {
            if let Ok(rows) = st.query_map([], |r| Ok((r.get::<_, u32>(0)?, r.get::<_, u64>(1)?))) {
                for (id, n) in rows.flatten() {
                    if let Some(name) = self.names.get(&id) {
                        out.push((name.clone(), n));
                    }
                }
            }
        }
        out.sort_by(|a, b| b.1.cmp(&a.1).then(a.0.cmp(&b.0)));
        out
    }

    pub fn steps_of(&self, name: &str, limit: Option<usize>) -> Vec<u64> {
        let Some(&id) = self.name_ids.get(name) else { return vec![] };
        let sql = match limit {
            Some(n) => format!("SELECT step FROM binds WHERE name=?1 ORDER BY step LIMIT {n}"),
            None => "SELECT step FROM binds WHERE name=?1 ORDER BY step".into(),
        };
        let mut st = match self.db.prepare(&sql) {
            Ok(s) => s,
            Err(_) => return vec![],
        };
        st.query_map([id], |r| r.get::<_, i64>(0))
            .map(|rows| rows.flatten().map(|s| s as u64).collect())
            .unwrap_or_default()
    }

    /// CALL payload id -> function name, for per-function call counts.
    pub fn payload_funcs(&self) -> HashMap<u32, String> {
        let mut out = HashMap::new();
        if let Ok(mut st) = self.db.prepare("SELECT id, json FROM payloads") {
            if let Ok(rows) = st.query_map([], |r| Ok((r.get::<_, u32>(0)?, r.get::<_, String>(1)?))) {
                for (id, json) in rows.flatten() {
                    if let Some(i) = json.find("\"func\":") {
                        let rest = json[i + 7..].trim_start();
                        let digits: String = rest.chars().take_while(|c| c.is_ascii_digit()).collect();
                        if let Ok(fid) = digits.parse::<u32>() {
                            if let Some(name) = self.funcs.get(&fid) {
                                out.insert(id, name.clone());
                            }
                        }
                    }
                }
            }
        }
        out
    }

    pub fn size_bytes(&self) -> u64 {
        std::fs::read_dir(&self.dir)
            .map(|it| it.flatten().filter_map(|e| e.metadata().ok()).map(|m| m.len()).sum())
            .unwrap_or(0)
    }
}

/// Every trace folder in the current directory, newest first.
pub fn traces_here() -> Vec<String> {
    let mut found: Vec<(std::time::SystemTime, String)> = std::fs::read_dir(".")
        .map(|it| {
            it.flatten()
                .filter(|e| e.path().join("trace.db").exists())
                .filter(|e| e.file_name().to_string_lossy().ends_with(".nd"))
                .map(|e| {
                    let t = e.path().join("trace.db").metadata().and_then(|m| m.modified()).unwrap_or(std::time::UNIX_EPOCH);
                    (t, e.file_name().to_string_lossy().to_string())
                })
                .collect()
        })
        .unwrap_or_default();
    found.sort_by(|a, b| b.0.cmp(&a.0));
    found.into_iter().map(|(_, n)| n).collect()
}
