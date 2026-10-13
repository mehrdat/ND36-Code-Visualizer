//! The write side of a trace.
//!
//! The Python writer inserts into SQLite as it goes. This one does not touch
//! SQLite until the run is over: everything is interned into in-memory maps
//! and written in one transaction at `close()`. The hot path is a `Vec<u8>`
//! append and nothing else.

use crate::schema::{self, Vref};
use rustc_hash::FxHashMap as HashMap;
use std::fs::File;
use std::io::Write;
use std::path::{Path, PathBuf};

pub struct ObjRow {
    pub obj_id: u32,
    pub step: u64,
    pub type_name: String,
    pub summary: String,
    pub children: Option<String>,
    pub repr: String,
}

pub struct Writer {
    dir: PathBuf,
    log: File,
    buf: Vec<u8>,
    pub step: u64,
    pub keyframe_every: u64,
    next_keyframe: u64,

    files: HashMap<String, u32>,
    file_rows: Vec<(u32, String, String)>,
    locs: HashMap<(u32, i32), u32>,
    loc_rows: Vec<(u32, u32, i32)>,
    funcs: HashMap<(String, u32, i32), u32>,
    func_rows: Vec<(u32, String, String, u32, i32)>,
    names: HashMap<String, u32>,
    pub name_rows: Vec<String>, // index = id - 1
    strings: HashMap<String, u32>,
    string_rows: Vec<String>,
    payloads: Vec<String>,
    pub objects: Vec<ObjRow>,
    keyframes: Vec<(u64, Vec<u8>)>,
    binds: Vec<(u32, u64)>,
    linehits: Vec<(u64, u64)>, // indexed by loc id: ids are dense from 1
    meta: Vec<(String, String)>,
}

impl Writer {
    pub fn create(dir: &Path, keyframe_every: u64) -> std::io::Result<Writer> {
        std::fs::create_dir_all(dir)?;
        let db = dir.join("trace.db");
        if db.exists() {
            std::fs::remove_file(&db)?;
        }
        let _ = std::fs::remove_file(dir.join("analysis.json")); // stale cache
        let log = File::create(dir.join("events.bin"))?;
        Ok(Writer {
            dir: dir.to_path_buf(),
            log,
            buf: Vec::with_capacity(1 << 21),
            step: 0,
            keyframe_every,
            next_keyframe: keyframe_every,
            files: HashMap::default(),
            file_rows: Vec::new(),
            locs: HashMap::default(),
            loc_rows: Vec::new(),
            funcs: HashMap::default(),
            func_rows: Vec::new(),
            names: HashMap::default(),
            name_rows: Vec::new(),
            strings: HashMap::default(),
            string_rows: Vec::new(),
            payloads: Vec::new(),
            objects: Vec::new(),
            keyframes: Vec::new(),
            binds: Vec::new(),
            linehits: Vec::new(),
            meta: Vec::new(),
        })
    }

    // -- interning ---------------------------------------------------------

    pub fn file_id(&mut self, path: &str) -> u32 {
        if let Some(&id) = self.files.get(path) {
            return id;
        }
        let id = self.files.len() as u32 + 1;
        let source = std::fs::read_to_string(path).unwrap_or_default();
        self.files.insert(path.to_string(), id);
        self.file_rows.push((id, path.to_string(), source));
        id
    }

    pub fn loc_id(&mut self, file: u32, line: i32) -> u32 {
        if let Some(&id) = self.locs.get(&(file, line)) {
            return id;
        }
        let id = self.locs.len() as u32 + 1;
        self.locs.insert((file, line), id);
        self.loc_rows.push((id, file, line));
        id
    }

    pub fn func_id(&mut self, name: &str, qualname: &str, file: u32, first_line: i32) -> u32 {
        let key = (qualname.to_string(), file, first_line);
        if let Some(&id) = self.funcs.get(&key) {
            return id;
        }
        let id = self.funcs.len() as u32 + 1;
        self.funcs.insert(key, id);
        self.func_rows.push((id, name.to_string(), qualname.to_string(), file, first_line));
        id
    }

    pub fn name_id(&mut self, text: &str) -> u32 {
        if let Some(&id) = self.names.get(text) {
            return id;
        }
        let id = self.names.len() as u32 + 1;
        self.names.insert(text.to_string(), id);
        self.name_rows.push(text.to_string());
        id
    }

    pub fn name_text(&self, id: u32) -> &str {
        self.name_rows.get(id as usize - 1).map(|s| s.as_str()).unwrap_or("?")
    }

    pub fn string_id(&mut self, text: &str) -> u32 {
        if let Some(&id) = self.strings.get(text) {
            return id;
        }
        let id = self.strings.len() as u32 + 1;
        self.strings.insert(text.to_string(), id);
        self.string_rows.push(text.to_string());
        id
    }

    pub fn payload_id(&mut self, json: String) -> u32 {
        self.payloads.push(json);
        self.payloads.len() as u32
    }

    // -- the hot path --------------------------------------------------------

    #[inline(always)]
    pub fn event(&mut self, t: u64, kind: u8, frame: u32, loc: u32, name: u32, v: Vref, payload: u32) -> u64 {
        self.step += 1;
        schema::pack(&mut self.buf, t, kind, frame, loc, name, v, payload);
        if self.buf.len() >= 1 << 21 {
            let _ = self.log.write_all(&self.buf);
            self.buf.clear();
        }
        self.step
    }

    #[inline(always)]
    pub fn note_bind(&mut self, name: u32) {
        self.binds.push((name, self.step));
    }

    /// The hit belongs to the line being ENTERED; the elapsed time to the
    /// line being LEFT. Same rule as the Python writer.
    #[inline(always)]
    pub fn note_line(&mut self, loc: u32, prev_loc: u32, dt: u64) {
        let need = loc.max(prev_loc) as usize + 1;
        if self.linehits.len() < need {
            self.linehits.resize(need, (0, 0));
        }
        self.linehits[loc as usize].0 += 1;
        if prev_loc != 0 {
            self.linehits[prev_loc as usize].1 += dt;
        }
    }

    pub fn should_keyframe(&mut self) -> bool {
        if self.step >= self.next_keyframe {
            self.next_keyframe = self.step + self.keyframe_every;
            return true;
        }
        false
    }

    pub fn keyframe(&mut self, step: u64, json: &str) {
        use flate2::{write::ZlibEncoder, Compression};
        let mut z = ZlibEncoder::new(Vec::new(), Compression::fast());
        let _ = z.write_all(json.as_bytes());
        if let Ok(blob) = z.finish() {
            // INSERT OR REPLACE semantics: a later keyframe at the same step wins.
            if let Some(last) = self.keyframes.last_mut() {
                if last.0 == step {
                    last.1 = blob;
                    return;
                }
            }
            self.keyframes.push((step, blob));
        }
    }

    pub fn set_meta(&mut self, key: &str, json_value: String) {
        self.meta.retain(|(k, _)| k != key);
        self.meta.push((key.to_string(), json_value));
    }


    // -- the slow path, once ---------------------------------------------------

    pub fn close(mut self) -> rusqlite::Result<u64> {
        if !self.buf.is_empty() {
            let _ = self.log.write_all(&self.buf);
            self.buf.clear();
        }
        let _ = self.log.flush();

        let mut db = rusqlite::Connection::open(self.dir.join("trace.db"))?;
        db.execute_batch("PRAGMA journal_mode=OFF; PRAGMA synchronous=OFF;")?;
        db.execute_batch(schema::DDL)?;
        let tx = db.transaction()?;
        {
            let mut st = tx.prepare("INSERT INTO files VALUES (?1,?2,?3)")?;
            for (id, path, src) in &self.file_rows {
                st.execute(rusqlite::params![id, path, src])?;
            }
            let mut st = tx.prepare("INSERT INTO locs VALUES (?1,?2,?3,NULL,NULL)")?;
            for (id, file, line) in &self.loc_rows {
                st.execute(rusqlite::params![id, file, line])?;
            }
            let mut st = tx.prepare("INSERT INTO funcs VALUES (?1,?2,?3,?4,?5)")?;
            for (id, name, qual, file, first) in &self.func_rows {
                st.execute(rusqlite::params![id, name, qual, file, first])?;
            }
            let mut st = tx.prepare("INSERT INTO names VALUES (?1,?2)")?;
            for (i, text) in self.name_rows.iter().enumerate() {
                st.execute(rusqlite::params![i as u32 + 1, text])?;
            }
            let mut st = tx.prepare("INSERT INTO strings VALUES (?1,?2)")?;
            for (i, text) in self.string_rows.iter().enumerate() {
                st.execute(rusqlite::params![i as u32 + 1, text])?;
            }
            let mut st = tx.prepare("INSERT INTO payloads VALUES (?1,?2)")?;
            for (i, json) in self.payloads.iter().enumerate() {
                st.execute(rusqlite::params![i as u32 + 1, json])?;
            }
            let mut st = tx.prepare("INSERT OR REPLACE INTO objects VALUES (?1,?2,?3,?4,?5,?6,NULL)")?;
            for o in &self.objects {
                st.execute(rusqlite::params![o.obj_id, o.step as i64, o.type_name, o.summary, o.children, o.repr])?;
            }
            let mut st = tx.prepare("INSERT OR REPLACE INTO keyframes VALUES (?1,?2)")?;
            for (step, blob) in &self.keyframes {
                st.execute(rusqlite::params![*step as i64, blob])?;
            }
            let mut st = tx.prepare("INSERT INTO binds VALUES (?1,?2)")?;
            for (name, step) in &self.binds {
                st.execute(rusqlite::params![name, *step as i64])?;
            }
            let mut st = tx.prepare("INSERT INTO linehits VALUES (?1,?2,?3)")?;
            for (loc, (hits, ns)) in self.linehits.iter().enumerate() {
                if *hits > 0 || *ns > 0 {
                    st.execute(rusqlite::params![loc as u32, *hits as i64, *ns as i64])?;
                }
            }
            let mut st = tx.prepare("INSERT OR REPLACE INTO meta VALUES (?1,?2)")?;
            for (k, v) in &self.meta {
                st.execute(rusqlite::params![k, v])?;
            }
        }
        tx.commit()?;
        db.execute_batch(
            "CREATE INDEX ix_binds ON binds(name, step);
             CREATE INDEX ix_allocs ON allocs(snap);",
        )?;
        Ok(self.step)
    }
}

/// JSON-encode a string the way `json.dumps` would.
pub fn json_str(s: &str) -> String {
    serde_json::to_string(s).unwrap_or_else(|_| "\"\"".into())
}
