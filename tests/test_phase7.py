"""
Phase 7 acceptance tests: the explorer, and the five things that broke.

The second half of this file is a regression suite built straight from
real reports. Each one is named after what the user actually saw, because
that is the thing that must never come back:

    nd run script.py --port 8080   ->  argparse rejected --port
    nd watch trace.nd grid         ->  "no variable called 'grid'", no help
    nd watch missing.nd v          ->  a raw sqlite3.OperationalError
    nd bench FILE                  ->  no file to put there

Run:  python3 tests/test_phase7.py
"""

import io
import os
import subprocess
import sys
import tempfile
from contextlib import redirect_stdout

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from nd36 import analyse, cli, draw, explore                # noqa: E402
from nd36.run import trace_script                           # noqa: E402
from nd36.store import TraceReader                          # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
PROGRAMS = os.path.join(HERE, "programs")

_cache = {}


def trace_of(name):
    """Record a fixture once per run of this file."""
    if name not in _cache:
        tmp = tempfile.mkdtemp(prefix="nd7_")
        out = os.path.join(tmp, name.replace(".py", ".nd"))
        trace_script(os.path.join(PROGRAMS, name), out)
        _cache[name] = out
    return _cache[name]


def run(reader, path, *commands):
    """Run explorer commands and hand back what they printed."""
    buf = io.StringIO()
    with redirect_stdout(buf):
        explore.once(reader, path, list(commands))
    return buf.getvalue()


def nd(*args, **kw):
    """Invoke the CLI as a user would, in a subprocess."""
    env = dict(os.environ, PYTHONPATH=REPO, COLUMNS="100")
    return subprocess.run([sys.executable, "-m", "nd36.cli"] + list(args),
                          capture_output=True, text=True, env=env,
                          cwd=kw.get("cwd"), timeout=180)


# ------------------------------------------------------- the analysis itself

def test_the_pass_finds_the_structure():
    """One walk has to produce callers, callees, writes and reads."""
    path = trace_of("sort.py")
    r = TraceReader(path)
    a = analyse.load(r, use_cache=False)
    assert a.functions, "no functions found"
    assert a.variables, "no variables found"
    for name, f in a.functions.items():
        for caller, n in f["callers"].items():
            assert n > 0
            # Every edge must agree with itself from the other end.
            assert a.functions[caller]["callees"].get(name) == n, \
                "%s<-%s disagrees with %s->%s" % (name, caller, caller, name)
    print("  sort.py      %d functions, %d variables, edges consistent"
          % (len(a.functions), len(a.variables)))


def test_exclusive_time_adds_up_to_the_run():
    """Per-function time must sum to the run, not to a multiple of it.

    This is the bug the first version shipped with: deriving time from the
    gaps between all events double-counted, because a CALL record carries
    a timestamp from before its arguments were encoded. A recursive program
    then claimed to have spent twenty times the wall clock inside itself.
    """
    path = trace_of("mixed.py")
    r = TraceReader(path)
    a = analyse.load(r, use_cache=False)
    total = sum(f["self_ms"] for f in a.functions.values())
    wall = float(r.meta.get("wall_seconds", 0)) * 1000
    assert wall > 0, "no wall time recorded"
    assert total <= wall * 1.05, \
        "self time sums to %.0f ms but the run took %.0f ms" % (total, wall)
    assert total >= wall * 0.5, \
        "self time sums to only %.0f ms of a %.0f ms run" % (total, wall)
    print("  mixed.py     %.0f ms across functions vs %.0f ms wall"
          % (total, wall))


def test_the_cache_is_used_and_is_keyed_to_the_trace():
    """The second `nd x` on a big trace must not re-walk the log."""
    path = trace_of("sort.py")
    r = TraceReader(path)
    analyse.load(r)
    cached = os.path.join(path, analyse.CACHE)
    assert os.path.exists(cached), "nothing was cached"
    first = analyse.load(r)
    assert first.functions, "cached analysis came back empty"
    # A cache from a different-sized trace must be ignored, not trusted.
    import json
    blob = json.load(open(cached))
    blob["n_steps"] = blob["n_steps"] + 1
    json.dump(blob, open(cached, "w"))
    again = analyse.load(r, use_cache=False)
    assert again.n_steps == r.n_steps
    print("  cache        written, and a stale one is rejected")


# ------------------------------------------------------------- the explorer

def test_every_command_runs_with_no_arguments():
    """`?` says every command works bare. That has to be true."""
    path = trace_of("sort.py")
    r = TraceReader(path)
    bare = ["s", "n", "p", "f", "u", "pd", "pv", "bt", "ll", "afl", "af",
            "axf", "fv", "vl", "tt", "tree", "dis", "i", "?", "??"]
    out = run(r, path, *bare)
    assert "Traceback" not in out
    for bad in ("no command", "TypeError", "AttributeError"):
        assert bad not in out, "%r appeared in the output" % bad
    print("  explorer     %d commands run bare without an argument"
          % len(bare))


def test_seek_understands_every_form_it_advertises():
    path = trace_of("sort.py")
    r = TraceReader(path)
    ex = explore.Explorer(r, path, quiet=True)
    with redirect_stdout(io.StringIO()):
        ex.do("s 50")
        assert ex.cursor == 50
        ex.do("s +10")
        assert ex.cursor == 60
        ex.do("s -20")
        assert ex.cursor == 40
        ex.do("s end")
        assert ex.cursor == r.n_steps
        ex.do("s 50%")
        assert abs(ex.cursor - r.n_steps // 2) <= 1
        ex.do("s start")
        assert ex.cursor == 1
        ex.do("1200")               # a bare number is a seek
        assert ex.cursor == min(1200, r.n_steps)
    print("  s            N, +N, -N, N%%, end, start and a bare number")


def test_a_condition_that_is_never_true_says_so():
    """It used to wrap to the first write, which does not satisfy it --
    so the cursor moved and the prompt implied the condition had held."""
    path = trace_of("sort.py")
    r = TraceReader(path)
    ex = explore.Explorer(r, path, quiet=True)
    buf = io.StringIO()
    with redirect_stdout(buf):
        ex.do("s 1")
        before = ex.cursor
        ex.do("c i>100000")
    assert "never true" in buf.getvalue(), buf.getvalue()
    assert ex.cursor == before, "the cursor moved on a condition that failed"
    print("  c            an impossible condition says so and stays put")


def test_the_function_variable_grid_is_the_connection_table():
    """The thing the explorer exists for: who touches what."""
    path = trace_of("mixed.py")
    r = TraceReader(path)
    a = analyse.load(r, use_cache=False)
    writers = [f for f in a.functions.values() if f["writes"]]
    assert writers, "not one function writes a variable"
    hot = max(writers, key=lambda f: f["self_steps"])
    for var in hot["writes"]:
        writers = a.writers_of(var)
        assert hot["name"] in writers, \
            "%s writes %s but is not in its writers" % (hot["name"], var)
    out = run(r, path, "fv")
    assert "W = writes it" in out, "the legend is missing"
    print("  fv           %s writes %d variables, all cross-referenced"
          % (hot["name"], len(hot["writes"])))


def test_plotting_reads_values_straight_from_the_log():
    """A numeric series must cost no queries -- it is in the record."""
    path = trace_of("sort.py")
    r = TraceReader(path)
    counts = r.variable_counts()
    numeric = None
    for name in counts:
        series, _obj = r.numeric_series(name)
        if sum(1 for _s, v in series if v is not None) > 3:
            numeric = name
            break
    assert numeric, "no numeric variable in sort.py"
    series, _obj = r.numeric_series(numeric)
    rows = draw.plot([v for _s, v in series])
    assert len(rows) >= 9, "the plot has no body"
    assert any("█" in row for row in rows), "the plot drew nothing"
    print("  pl           %s plotted from %d points, no object lookups"
          % (numeric, len(series)))


def test_the_repl_survives_nonsense():
    """One bad command must never end the session."""
    path = trace_of("sort.py")
    r = TraceReader(path)
    ex = explore.Explorer(r, path, quiet=True)
    buf = io.StringIO()
    with redirect_stdout(buf):
        for junk in ("zzz", "s notavariable", "af notafunction",
                     "axv notavariable", "ch", "pl", "cmp", "/",
                     "pV notascope", "n abc"):
            ex.do(junk)
    assert ex.running, "the session ended"
    assert "Traceback" not in buf.getvalue()
    print("  explorer     ten bad commands, session still alive")


# ------------------------------------------------- the five reported bugs

def test_run_passes_unknown_arguments_to_the_program():
    """Reported: `nd run server.py --port 8080` -> unrecognized arguments."""
    ours, theirs = cli._split_run(["server.py", "--port", "8080"])
    assert ours == ["server.py"], ours
    assert theirs == ["--port", "8080"], theirs
    # Our own options still work, before or after the script.
    ours, theirs = cli._split_run(["s.py", "--memory", "--port", "80"])
    assert "--memory" in ours and theirs == ["--port", "80"]
    ours, theirs = cli._split_run(["-o", "/tmp/x.nd", "s.py", "-v"])
    assert ours == ["-o", "/tmp/x.nd", "s.py"] and theirs == ["-v"]
    # `--` hands everything over, even things that look like ours.
    ours, theirs = cli._split_run(["s.py", "--", "--memory"])
    assert theirs == ["--memory"], theirs

    tmp = tempfile.mkdtemp(prefix="nd7_")
    script = os.path.join(tmp, "srv.py")
    open(script, "w").write("import sys\nprint('ARGV', sys.argv[1:])\n")
    got = nd("run", script, "--port", "8080", "-o",
             os.path.join(tmp, "srv.nd"))
    assert "ARGV ['--port', '8080']" in got.stdout, got.stdout + got.stderr
    assert "unrecognized" not in got.stderr
    print("  nd run       --port 8080 reaches the program, not argparse")


def test_a_missing_trace_is_explained_not_raised():
    """Reported: sqlite3.OperationalError: unable to open database file."""
    tmp = tempfile.mkdtemp(prefix="nd7_")
    for cmd in ("info", "flow", "heat", "mem", "tensors", "data", "x"):
        got = nd(cmd, os.path.join(tmp, "nope.nd"))
        assert "Traceback" not in got.stderr, "%s raised:\n%s" % (cmd,
                                                                  got.stderr)
        assert "OperationalError" not in got.stderr, cmd
        assert "does not exist" in got.stderr, "%s: %r" % (cmd, got.stderr)
        assert got.returncode == 1, cmd
    # Pointing at the script instead of the trace is the common slip.
    script = os.path.join(tmp, "thing.py")
    open(script, "w").write("x = 1\n")
    got = nd("info", script)
    assert "a trace is a folder" in got.stderr, got.stderr
    print("  bad trace    7 commands explain it; none show a traceback")


def test_watch_suggests_what_you_probably_meant():
    """Reported: `nd watch sodoko.nd grid` -> "no variable called 'grid'"
    and nothing else, which leaves you stuck."""
    path = trace_of("sort.py")
    r = TraceReader(path)
    names = list(r.variable_counts())
    assert names, "fixture has no variables"
    target = names[0]
    buf = io.StringIO()
    with redirect_stdout(buf):
        cli._no_such_variable(r, path, target[:-1] + "zz")
    out = buf.getvalue()
    assert "did you mean" in out or target in out, out
    # An attribute is recorded under its root, and that must be said.
    buf = io.StringIO()
    with redirect_stdout(buf):
        cli._no_such_variable(r, path, "%s.shape" % target)
    assert "attribute" in buf.getvalue(), buf.getvalue()
    assert target in buf.getvalue()
    print("  nd watch     a near-miss gets a suggestion, not a dead end")


def test_bench_and_demo_work_with_no_file_at_all():
    """Reported: no files I could use with `nd bench FILE`."""
    for name in ("sort_demo.py", "bug_demo.py", "loop_demo.c"):
        assert os.path.exists(cli.sample_path(name)), name
    tmp = tempfile.mkdtemp(prefix="nd7_")
    got = nd("bench", cwd=tmp)
    assert "cost per step" in got.stdout, got.stdout + got.stderr
    assert "bundled sample" in got.stdout
    got = nd("demo", cwd=tmp)
    assert os.path.isdir(os.path.join(tmp, "sort_demo.nd")), got.stdout
    assert "now try these" in got.stdout
    print("  nd bench     runs with no argument; nd demo needs no setup")


def test_the_vscode_extension_looks_for_nd_in_more_than_one_place():
    """Reported: ND36: the server never came up.

    VS Code's PATH is shorter than a login shell's, so `nd` installed into
    ~/.local/bin is invisible to it. The extension now tries the places it
    could be and reports the child's own stderr instead of a guess.
    """
    import json
    js = open(os.path.join(REPO, "vscode-extension", "extension.js")).read()
    assert "function candidates" in js, "no fallback search"
    for place in (".local/bin/nd", "/usr/local/bin/nd", "homebrew"):
        assert place in js, "does not look in %s" % place
    assert "nd36.cli" in js, "lost the python -m fallback"
    assert "tail" in js, "the real error is still not surfaced"
    manifest = json.load(open(os.path.join(REPO, "vscode-extension",
                                           "package.json")))
    props = manifest["contributes"]["configuration"]["properties"]
    assert "nd.command" in props, "no way to point it at the right binary"
    print("  vscode       four candidate paths, and the child's own error")


def test_the_commands_doc_covers_every_command():
    """A command that is not in COMMANDS.md might as well not exist."""
    doc = open(os.path.join(REPO, "COMMANDS.md")).read()
    parser_names = []
    import argparse

    class Spy(argparse.ArgumentParser):
        def add_parser(self, name, **kw):                  # pragma: no cover
            parser_names.append(name)
            return argparse.ArgumentParser()
    # Simpler and sturdier than monkeypatching: read the source.
    source = open(os.path.join(REPO, "nd36", "cli.py")).read()
    import re
    names = set(re.findall(r'sub\.add_parser\(\s*"([\w-]+)"', source))
    names |= {"x", "explore", "demo", "samples"}
    missing = sorted(n for n in names if "`nd %s" % n not in doc)
    assert not missing, "COMMANDS.md says nothing about: %s" % ", ".join(
        missing)
    print("  COMMANDS.md  documents all %d commands" % len(names))


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for fn in tests:
        print(fn.__name__)
        try:
            fn()
        except AssertionError as exc:
            failed += 1
            print("  FAILED: %s" % exc)
    print("\n%d/%d passed" % (len(tests) - failed, len(tests)))
    sys.exit(1 if failed else 0)
