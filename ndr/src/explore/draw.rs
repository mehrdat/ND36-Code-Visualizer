//! Pictures made of text -- `nd36/draw.py`, line for line.
//!
//! One place knows about terminal width, colour and rounding, so every
//! command draws the same way `nd x` does.

use super::pyfmt::{self, floordiv, round_int, Num};
use std::collections::HashSet;

pub const BLOCKS: [char; 9] = [' ', '▁', '▂', '▃', '▄', '▅', '▆', '▇', '█'];
pub const DIM: &str = "\x1b[2m";
pub const BOLD: &str = "\x1b[1m";
pub const CYAN: &str = "\x1b[36m";
pub const YELLOW: &str = "\x1b[33m";
pub const GREEN: &str = "\x1b[32m";
pub const RED: &str = "\x1b[31m";
pub const OFF: &str = "\x1b[0m";

/// Terminal width, clamped to 60..160. `shutil.get_terminal_size((100, 24))`:
/// $COLUMNS first, then the terminal on stdout, then 100.
pub fn width() -> usize {
    let mut cols: i64 = std::env::var("COLUMNS").ok().and_then(|s| s.trim().parse().ok()).unwrap_or(0);
    if cols <= 0 {
        cols = term_cols().unwrap_or(100);
        if cols <= 0 {
            cols = 100;
        }
    }
    cols.clamp(60, 160) as usize
}

fn term_cols() -> Option<i64> {
    // SAFETY: TIOCGWINSZ fills a winsize struct we own; a failure is reported
    // through the return value, never by touching memory we did not pass.
    unsafe {
        let mut ws: libc::winsize = std::mem::zeroed();
        if libc::ioctl(1, libc::TIOCGWINSZ, &mut ws) == 0 {
            Some(ws.ws_col as i64)
        } else {
            None
        }
    }
}

/// Length of a string as the eye sees it, ignoring colour codes.
pub fn plain(text: &str) -> usize {
    let mut out = 0;
    let mut it = text.chars();
    while let Some(c) = it.next() {
        if c == '\x1b' {
            for c2 in it.by_ref() {
                if c2 == 'm' {
                    break;
                }
            }
        } else {
            out += 1;
        }
    }
    out
}

pub fn pad(text: &str, n: usize) -> String {
    let p = plain(text);
    if n > p { format!("{text}{}", " ".repeat(n - p)) } else { text.to_string() }
}

/// One row of blocks: the series' shape in the space of a word.
pub fn sparkline(values: &[Option<Num>]) -> String {
    let vals: Vec<f64> = values.iter().flatten().map(|v| v.f()).collect();
    if vals.is_empty() {
        return String::new();
    }
    let buckets = resample(values, values.len());
    let lo = vals.iter().cloned().fold(f64::INFINITY, f64::min);
    let hi = vals.iter().cloned().fold(f64::NEG_INFINITY, f64::max);
    let span = if hi - lo != 0.0 { hi - lo } else { 1.0 };
    buckets.iter().map(|v| match v {
        None => ' ',
        Some(v) => BLOCKS[round_int((v - lo) / span * 8.0).clamp(0, 8) as usize],
    }).collect()
}

/// Average `values` down to exactly `cells` buckets.
pub fn resample(values: &[Option<Num>], cells: usize) -> Vec<Option<f64>> {
    let n = values.len();
    if n <= cells {
        return values.iter().map(|v| v.map(|x| x.f())).collect();
    }
    (0..cells).map(|i| {
        let lo = i * n / cells;
        let hi = (lo + 1).max((i + 1) * n / cells);
        let chunk: Vec<Num> = values[lo..hi].iter().flatten().copied().collect();
        if chunk.is_empty() {
            None
        } else {
            Some(match pyfmt::sum(chunk.iter().copied()) {
                Num::I(s) => s as f64 / chunk.len() as f64,
                Num::F(s) => s / chunk.len() as f64,
            })
        }
    }).collect()
}

/// A chart with a y axis, so the shape has a scale.
pub fn plot(values: &[Option<Num>], label: &str, xlabel: &str) -> Vec<String> {
    let height = 9usize;
    let vals: Vec<f64> = values.iter().flatten().map(|v| v.f()).collect();
    if vals.is_empty() {
        return vec![format!("{DIM}(nothing numeric to plot){OFF}")];
    }
    let cells = width() - 14;
    let series = resample(values, cells);
    let lo = vals.iter().cloned().fold(f64::INFINITY, f64::min);
    let mut hi = vals.iter().cloned().fold(f64::NEG_INFINITY, f64::max);
    if hi == lo {
        hi = lo + 1.0;
    }
    let mut grid = vec![vec![' '; series.len()]; height];
    for (x, v) in series.iter().enumerate() {
        let Some(v) = v else { continue };
        let eighths = round_int((v - lo) / (hi - lo) * height as f64 * 8.0);
        let e = eighths.max(0);
        let (full, rest) = (e / 8, e % 8);
        for y in 0..(full.min(height as i64) as usize) {
            grid[height - 1 - y][x] = '█';
        }
        if full < height as i64 && rest != 0 {
            grid[height - 1 - full as usize][x] = BLOCKS[rest as usize];
        }
    }
    let tick = |y: usize| {
        let frac = (height - 1 - y) as f64 / (height - 1).max(1) as f64;
        num(lo + frac * (hi - lo))
    };
    let mut rows = Vec::new();
    if !label.is_empty() {
        rows.push(format!("{BOLD}{label}{OFF}"));
    }
    for (y, row) in grid.iter().enumerate() {
        rows.push(format!("{DIM}{:>10}{OFF} {DIM}│{OFF} {CYAN}{}{OFF}", tick(y), row.iter().collect::<String>()));
    }
    rows.push(format!("{DIM}{:>10} └{}{OFF}", "", "─".repeat(series.len())));
    if !xlabel.is_empty() {
        rows.push(format!("{DIM}{:>11} {xlabel}{OFF}", ""));
    }
    rows
}

/// A number short enough for an axis.
pub fn num(v: f64) -> String {
    let a = v.abs();
    if v.is_finite() && v == v.trunc() && a < 1e6 {
        return format!("{}", v as i64);
    }
    if a >= 1e9 {
        return format!("{}G", pyfmt::fixed(v / 1e9, 1));
    }
    if a >= 1e6 {
        return format!("{}M", pyfmt::fixed(v / 1e6, 1));
    }
    if a >= 1e3 {
        return format!("{}k", pyfmt::fixed(v / 1e3, 1));
    }
    if a < 0.01 {
        return pyfmt::sci(v, 1);
    }
    pyfmt::fixed(v, 2)
}

pub fn numn(v: Num) -> String {
    match v {
        Num::I(i) if i.unsigned_abs() < 1_000_000 => i.to_string(),
        other => num(other.f()),
    }
}

pub fn bar(frac: f64, cells: usize) -> String {
    let frac = if frac.is_nan() { 0.0 } else { frac.clamp(0.0, 1.0) };
    let n = round_int(frac * cells as f64).max(0) as usize;
    format!("{}{}", "▇".repeat(n), " ".repeat(cells.saturating_sub(n)))
}

/// Aligned columns. `aligns` is '<' for left, anything else for right.
pub fn table(headers: &[&str], rows: Vec<Vec<String>>, aligns: Option<&[char]>, limit: Option<usize>) -> Vec<String> {
    let mut rows = rows;
    let mut cut = 0;
    if let Some(l) = limit {
        if l > 0 && rows.len() > l {
            cut = rows.len() - l;
            rows.truncate(l);
        }
    }
    let cols = headers.len();
    let mut widest: Vec<usize> = headers.iter().map(|h| plain(h)).collect();
    for r in &rows {
        for i in 0..cols.min(r.len()) {
            widest[i] = widest[i].max(plain(&r[i]));
        }
    }
    let left = vec!['<'; cols];
    let aligns = aligns.unwrap_or(&left);
    let cap = width();
    let lay = |parts: &[String]| -> String {
        let mut out: Vec<String> = Vec::new();
        let mut used = 0;
        for (i, text) in parts.iter().enumerate() {
            let w = widest.get(i).copied().unwrap_or(0);
            if used + w + 2 > cap && i > 0 {
                break;
            }
            if aligns.get(i).copied().unwrap_or('<') == '<' {
                out.push(pad(text, w));
            } else {
                out.push(format!("{}{text}", " ".repeat(w.saturating_sub(plain(text)))));
            }
            used += w + 2;
        }
        out.join("  ").trim_end().to_string()
    };
    let head: Vec<String> = headers.iter().map(|h| h.to_string()).collect();
    let mut lines = vec![format!("{DIM}{}{OFF}", lay(&head))];
    for r in rows {
        let mut parts = r;
        // A row longer than the header is laid out in full, as zip-less
        // Python does; a shorter one is padded with empty cells.
        while parts.len() < cols {
            parts.push(String::new());
        }
        lines.push(lay(&parts));
    }
    if cut > 0 {
        lines.push(format!("{DIM}… {cut} more{OFF}"));
    }
    lines
}

/// An ASCII tree. `children(node)` -> names, `label(node)` -> text.
pub fn tree(root: &str, children: &dyn Fn(&str) -> Vec<String>, label: &dyn Fn(&str) -> String) -> Vec<String> {
    tree_at(root, children, label, "", &HashSet::new(), 0, 6)
}

fn tree_at(root: &str, children: &dyn Fn(&str) -> Vec<String>, label: &dyn Fn(&str) -> String,
           prefix: &str, seen: &HashSet<String>, depth: usize, maxdepth: usize) -> Vec<String> {
    let mut out = Vec::new();
    if prefix.is_empty() {
        out.push(label(root));
    }
    if depth >= maxdepth || seen.contains(root) {
        return out;
    }
    let mut seen = seen.clone();
    seen.insert(root.to_string());
    let kids = children(root);
    for (i, kid) in kids.iter().enumerate() {
        let last = i == kids.len() - 1;
        out.push(format!("{prefix}{}{}", if last { "└── " } else { "├── " }, label(kid)));
        let sub = format!("{prefix}{}", if last { "    " } else { "│   " });
        out.extend(tree_at(kid, children, label, &sub, &seen, depth + 1, maxdepth));
    }
    out
}

/// Where you are in the run: `──────●────────────  1,204 / 9,881`.
pub fn cursor_bar(step: u64, total: u64) -> String {
    let cells = width() as i64 - 34;
    let at = if total <= 1 { 0 } else { ((step as f64 - 1.0) / (total as f64 - 1.0) * (cells - 1) as f64) as i64 };
    let line = format!("{}{YELLOW}●{OFF}{}", "─".repeat(at.max(0) as usize), "─".repeat((cells - at - 1).max(0) as usize));
    format!("{DIM}{line}{OFF}  {BOLD}{} / {}{OFF}", pyfmt::nu(step), pyfmt::nu(total))
}

/// One row per name: a density sparkline over the same time axis.
pub fn heatrows(names: &[String], series: &[Vec<u64>]) -> Vec<String> {
    let name_width = 22;
    let cells = width() - name_width - 4;
    let peak = series.iter().map(|s| s.iter().copied().max().unwrap_or(0)).max().unwrap_or(0);
    let peak = if peak == 0 { 1 } else { peak } as f64;
    names.iter().zip(series).map(|(name, s)| {
        let vals: Vec<Option<Num>> = s.iter().map(|v| Some(Num::I(*v as i64))).collect();
        let row: String = resample(&vals, cells).iter().map(|v| match v {
            Some(v) if *v != 0.0 => BLOCKS[round_int((v / peak).sqrt() * 8.0).clamp(1, 8) as usize],
            _ => ' ',
        }).collect();
        format!("{} {CYAN}{row}{OFF}", pyfmt::ljust(pyfmt::take(name, name_width), name_width))
    }).collect()
}

/// `os.path.basename(path or "?")`.
pub fn rel(path: Option<&str>) -> String {
    match path {
        Some(p) if !p.is_empty() => pyfmt::basename(p).to_string(),
        _ => "?".into(),
    }
}

/// `x // 2` for the span arguments, which may be negative.
pub fn half(x: i64) -> i64 {
    floordiv(x, 2)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn plain_ignores_colour() {
        assert_eq!(plain(&format!("{CYAN}abc{OFF}é")), 4);
        assert_eq!(pad(&format!("{DIM}x{OFF}"), 3), format!("{DIM}x{OFF}  "));
    }

    #[test]
    fn axis_numbers() {
        assert_eq!(num(5.0), "5");
        assert_eq!(num(4208.0), "4208");
        assert_eq!(num(4208.5), "4.2k");
        assert_eq!(num(1_467_000.0), "1.5M");
        assert_eq!(num(0.005), "5.0e-03");
        assert_eq!(num(2.5), "2.50");
        assert_eq!(num(-3.0), "-3");
    }

    #[test]
    fn sparkline_and_bar() {
        let v: Vec<Option<Num>> = [1, 2, 3, 4, 5, 6, 7, 8, 9].iter().map(|x| Some(Num::I(*x))).collect();
        assert_eq!(sparkline(&v), " ▁▂▃▄▅▆▇█");
        assert_eq!(bar(0.5, 4), "▇▇  ");
        assert_eq!(bar(0.625, 4), "▇▇  "); // 2.5 rounds to even
        assert_eq!(bar(0.875, 4), "▇▇▇▇"); // 3.5 too
    }

    #[test]
    fn table_aligns_and_cuts() {
        std::env::set_var("COLUMNS", "100");
        let rows = vec![vec!["a".into(), "1".into()], vec!["bbb".into(), "22".into()], vec!["c".into(), "3".into()]];
        let t = table(&["name", "n"], rows, Some(&['<', '>']), Some(2));
        assert_eq!(t[0], format!("{DIM}name   n{OFF}"));
        assert_eq!(t[1], "a      1");
        assert_eq!(t[2], "bbb   22");
        assert_eq!(t[3], format!("{DIM}… 1 more{OFF}"));
    }

    #[test]
    fn tree_draws_and_stops_at_cycles() {
        let kids = |n: &str| -> Vec<String> {
            match n { "a" => vec!["b".into(), "c".into()], "b" => vec!["a".into()], _ => vec![] }
        };
        let label = |n: &str| n.to_string();
        assert_eq!(tree("a", &kids, &label), vec!["a", "├── b", "│   └── a", "└── c"]);
    }
}
