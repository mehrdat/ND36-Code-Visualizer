"""
Phase 6 acceptance tests: the shells.

There are two, and neither contains any product logic: a desktop window and
a VS Code panel, both showing the server from Phase 1. So what is checked
here is the wiring -- that the launcher is a real bundle, that the window
command is the one that opens a frameless window, and that the extension
manifest is something VS Code would actually load.

The plan said Tauri for the desktop shell. That needs a Rust toolchain, a
build step and codesigning for what is in the end a window with no address
bar, and every Chromium-family browser already ships that window behind
`--app=`. So this is a .app bundle that opens one. The trade is written
down in app.py rather than quietly made.

Run:  python3 tests/test_phase6.py
"""

import json
import os
import plistlib
import re
import shutil
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from nd36 import app                                      # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
EXT = os.path.join(REPO, "vscode-extension")


# ---------------------------------------------------------------- the tests

def test_the_window_command_is_a_window():
    """`--app=` is what makes it frameless; a separate profile is what stops
    it opening as a tab in the browser you already have running."""
    argv = app.app_command("/x/chrome", "http://127.0.0.1:7878/",
                           "/tmp/prof", 1200, 800)
    assert argv[0] == "/x/chrome"
    joined = " ".join(argv)
    assert "--app=http://127.0.0.1:7878/" in joined, joined
    assert "--user-data-dir=/tmp/prof" in joined, joined
    assert "--window-size=1200,800" in joined, joined
    assert "--no-first-run" in joined
    # Never a plain URL argument -- that is what opens a tab.
    assert not any(a.startswith("http") and not a.startswith("--")
                   for a in argv), argv
    print("  window       --app, own profile, fixed size, no tab")


def test_browser_lookup_is_honest():
    """It must return None rather than a path that is not there, because the
    caller falls back to an ordinary tab on None."""
    found = app.find_browser()
    assert found is None or os.path.exists(found) or shutil.which(found), found
    print("  browser      %s" % (found or "none here — would fall back to a tab"))


def test_mac_bundle_is_a_real_app():
    """A .app is a directory with an Info.plist and an executable. If either
    is wrong, macOS shows 'the application can't be opened'."""
    work = tempfile.mkdtemp(prefix="vizt6_")
    bundle = app.install_mac_app(target_dir=work, python="/usr/bin/python3",
                                 repo="/somewhere/nd36")
    assert bundle.endswith(".app")
    plist_path = os.path.join(bundle, "Contents", "Info.plist")
    exe = os.path.join(bundle, "Contents", "MacOS", "run")
    assert os.path.exists(plist_path), "no Info.plist"
    assert os.path.exists(exe), "no executable"
    assert os.access(exe, os.X_OK), "the executable is not executable"
    icns = os.path.join(bundle, "Contents", "Resources", "nd36.icns")
    assert os.path.exists(icns), "no icon — macOS shows a blank page"
    assert app.verify_bundle(bundle) is True

    with open(plist_path, "rb") as fh:
        plist = plistlib.load(fh)
    assert plist["CFBundleExecutable"] == "run", plist
    assert plist["CFBundlePackageType"] == "APPL", plist
    assert plist["CFBundleIdentifier"].count(".") >= 2, plist

    body = open(exe).read()
    assert body.startswith("#!"), "no shebang"
    assert "nd36.cli app" in body, body
    assert "/somewhere/nd36" in body, "the cwd is not baked in"
    print("  macOS app    bundle, plist and executable all well formed")
    shutil.rmtree(work)


def test_linux_launcher_is_a_desktop_entry():
    work = tempfile.mkdtemp(prefix="vizt6_")
    path = app.install_linux_launcher(target_dir=work, python="/usr/bin/python3",
                                      repo="/somewhere")
    body = open(path).read()
    assert body.startswith("[Desktop Entry]"), body
    for key in ("Type=Application", "Name=", "Exec=", "Terminal=false"):
        assert key in body, key
    assert "nd36.cli app" in body
    print("  linux        .desktop entry well formed")
    shutil.rmtree(work)


def test_cli_exposes_both_shells():
    out = subprocess.run([sys.executable, "-m", "nd36.cli", "--help"],
                         cwd=REPO, capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    for command in ("serve", "app", "install-app", "run", "report"):
        assert command in out.stdout, command
    for command in ("app", "install-app"):
        sub = subprocess.run(
            [sys.executable, "-m", "nd36.cli", command, "--help"],
            cwd=REPO, capture_output=True, text=True)
        assert sub.returncode == 0, sub.stderr
    print("  cli          serve · app · install-app all present and documented")


def test_vscode_manifest_would_load():
    path = os.path.join(EXT, "package.json")
    assert os.path.exists(path), "no extension manifest"
    manifest = json.load(open(path))

    for key in ("name", "version", "engines", "main", "contributes"):
        assert key in manifest, key
    assert manifest["engines"]["vscode"].startswith("^1."), manifest["engines"]
    main = os.path.join(EXT, manifest["main"].lstrip("./"))
    assert os.path.exists(main), "main points at %s which is missing" % main

    commands = {c["command"] for c in manifest["contributes"]["commands"]}
    assert "nd.visualizeFile" in commands, commands
    assert manifest["name"] == "nd", manifest["name"]
    assert manifest["displayName"] == "ND36", manifest["displayName"]

    # Every command surfaced in a menu must actually exist.
    for group in manifest["contributes"]["menus"].values():
        for item in group:
            assert item["command"] in commands, item
            re.compile(item["when"].split("=~")[-1].strip().strip("/"))

    settings = manifest["contributes"]["configuration"]["properties"]
    for key in ("nd.pythonPath", "nd.repoPath", "nd.port"):
        assert key in settings, key
    print("  vscode       manifest valid, %d commands, %d settings"
          % (len(commands), len(settings)))


def test_vscode_extension_is_sound():
    source = open(os.path.join(EXT, "extension.js")).read()

    # The commands it registers must match the ones it advertises.
    manifest = json.load(open(os.path.join(EXT, "package.json")))
    advertised = {c["command"] for c in manifest["contributes"]["commands"]}
    registered = set(re.findall(r"registerCommand\(\s*'([^']+)'", source))
    assert registered == advertised, (registered, advertised)

    # It must start the SAME server, not a second implementation.
    assert "nd36.cli" in source and "serve" in source, "does not call nd"
    assert "--no-browser" in source, "would pop a browser tab as well"
    # Remote and Codespaces windows need the port forwarded.
    assert "asExternalUri" in source, (
        "a bare 127.0.0.1 URL points at the wrong machine in a remote window")
    # And it must clean up after itself.
    assert "onDidDispose" in source and "kill()" in source, \
        "the server would outlive the panel"

    if shutil.which("node"):
        check = subprocess.run(["node", "--check",
                                os.path.join(EXT, "extension.js")],
                               capture_output=True, text=True)
        assert check.returncode == 0, check.stderr
        syntax = "syntax checked with node"
    else:
        syntax = "node not available, syntax unchecked"
    print("  vscode       %d commands wired, cleans up, %s"
          % (len(registered), syntax))


def test_shells_add_no_product_logic():
    """Both shells should be wrappers. If either grows a reader or a
    recorder of its own, the three front ends will drift apart."""
    source = open(os.path.join(REPO, "nd36", "app.py")).read()
    for forbidden in ("TraceReader", "sqlite3", "state_at"):
        assert forbidden not in source, (
            "app.py touches %s; the shell should only open a window"
            % forbidden)
    ext = open(os.path.join(EXT, "extension.js")).read()
    assert "sqlite" not in ext.lower() and "vizt" not in ext.lower() \
        or "openTrace" in ext, "the extension should not read traces itself"
    print("  shells       no reader, no recorder — both just open the UI")


def test_the_app_outlives_the_browser():
    """The first version stopped the server when the browser process exited.

    On macOS, launching Chrome while Chrome is already running hands the
    window to the running instance and the process we spawned exits inside a
    second -- so the server died a moment after the window appeared, and the
    window showed nothing at all. That is the bug this asserts against.
    """
    source = open(os.path.join(REPO, "nd36", "cli.py")).read()
    body = source[source.index("def cmd_app("):source.index("def _pick_a_trace(")]
    assert "proc.wait()" not in body, (
        "cmd_app waits on the browser process again; its exit says nothing "
        "about whether the window is still open")
    assert "serve_forever" in body, "the server has to keep running"
    print("  nd app       server no longer dies with the browser process")


def test_watch_without_a_name_helps():
    """`nd watch trace.nd` used to be an argparse error, which told you
    nothing about what you could actually watch."""
    out = subprocess.run([sys.executable, "-m", "nd36.cli", "watch",
                          "--help"], cwd=REPO, capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    assert "[name]" in out.stdout, (
        "name is still required: %s" % out.stdout.splitlines()[0])
    cli = open(os.path.join(REPO, "nd36", "cli.py")).read()
    assert "_list_variables" in cli, "no fallback listing"
    assert "variable_counts" in cli, (
        "the listing should count from the index, not by building a view "
        "for every assignment")
    print("  nd watch     name is optional, lists what you can watch")


def test_icon_files_are_valid():
    """A macOS app with a broken icon shows a blank page in Finder."""
    import struct
    from nd36 import icon

    png = icon.png_bytes(64)
    assert png[:8] == b"\x89PNG\r\n\x1a\n", "not a PNG"

    blob = icon.icns_bytes()
    assert blob[:4] == b"icns"
    total = struct.unpack(">I", blob[4:8])[0]
    assert total == len(blob), (total, len(blob))
    pos, chunks = 8, 0
    while pos < len(blob):
        length = struct.unpack(">I", blob[pos + 4:pos + 8])[0]
        assert blob[pos + 8:pos + 16] == b"\x89PNG\r\n\x1a\n", "bad chunk"
        pos += length
        chunks += 1
    assert chunks == len(icon.ICNS_TYPES), chunks
    print("  icon         %d-chunk .icns and a PNG, both well formed" % chunks)


def test_vscode_install_copies_everything():
    from nd36.app import install_vscode
    work = tempfile.mkdtemp(prefix="vizt6_")
    dest = install_vscode(target_dir=work)
    # VS Code only loads a folder named publisher.name-version.
    assert re.match(r"^nd36\.nd-\d+\.\d+\.\d+$", os.path.basename(dest)), \
        os.path.basename(dest)
    for needed in ("package.json", "extension.js", "icon.png"):
        assert os.path.exists(os.path.join(dest, needed)), needed
    # Installing twice must not fail.
    install_vscode(target_dir=work)
    print("  install-vscode  copies to %s and is repeatable"
          % os.path.basename(dest))
    shutil.rmtree(work)


def test_doctor_reports_without_crashing():
    out = subprocess.run([sys.executable, "-m", "nd36.cli", "doctor"],
                         cwd=REPO, capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    for section in ("core", "desktop app", "vs code", "C / C++"):
        assert section in out.stdout, section
    print("  nd doctor    reports every section")


def test_packaging_files_are_present():
    """Homebrew and the AUR both need these to exist before a release."""
    for name in ("PKGBUILD", "nd36.rb", "README.md"):
        path = os.path.join(REPO, "packaging", name)
        assert os.path.exists(path), "packaging/%s is missing" % name
    pkgbuild = open(os.path.join(REPO, "packaging", "PKGBUILD")).read()
    for field in ("pkgname=nd36", "pkgver=", "arch=", "license=", "package()"):
        assert field in pkgbuild, field
    formula = open(os.path.join(REPO, "packaging", "nd36.rb")).read()
    assert "class Nd36 < Formula" in formula
    assert "test do" in formula, "a formula without a test ships broken"
    assert os.path.exists(os.path.join(REPO, "LICENSE")), "no LICENSE"
    print("  packaging    PKGBUILD, formula and LICENSE all present")


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
