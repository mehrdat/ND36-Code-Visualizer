"""
The desktop shell: a real window, and a real app, with nothing to install.

The plan said Tauri. Tauri means a Rust toolchain, a build step, and on
macOS a codesigning dance -- a lot of apparatus for what is, in the end, a
window with no address bar. Every Chromium-family browser already ships that
window: `--app=URL` opens a frameless one with its own Dock icon and no
tabs, and it is indistinguishable from a small native app.

So `nd app` opens that, and `nd install-app` writes a macOS .app bundle (or
a Linux .desktop entry) that launches it. Zero dependencies, works the
minute you have the repo, and if no Chromium-family browser is found it
falls back to an ordinary tab rather than failing.
"""

import os
import plistlib
import shutil
import subprocess
import sys

#: Where Chromium-family browsers live, most preferred first.
MAC_BROWSERS = [
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
    "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    "/Applications/Vivaldi.app/Contents/MacOS/Vivaldi",
]
LINUX_BROWSERS = ["google-chrome", "google-chrome-stable", "chromium",
                  "chromium-browser", "microsoft-edge", "brave-browser",
                  "vivaldi-stable"]
WIN_BROWSERS = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
]


def find_browser():
    """The first Chromium-family browser on this machine, or None."""
    if sys.platform == "darwin":
        for path in MAC_BROWSERS:
            if os.path.exists(path):
                return path
    elif sys.platform.startswith("win"):
        for path in WIN_BROWSERS:
            if os.path.exists(path):
                return path
    else:
        for name in LINUX_BROWSERS:
            found = shutil.which(name)
            if found:
                return found
    for name in ("chromium", "google-chrome", "chrome"):
        found = shutil.which(name)
        if found:
            return found
    return None


def app_command(browser, url, profile_dir, width=1280, height=880):
    """The argv for a frameless app window.

    A separate profile directory is what keeps this from opening as a tab in
    the browser you already have running, and keeps its window size and
    theme choice to itself.
    """
    return [browser, "--app=%s" % url,
            "--user-data-dir=%s" % profile_dir,
            "--window-size=%d,%d" % (width, height),
            "--no-first-run", "--no-default-browser-check"]


def open_window(url, profile_dir=None, width=1280, height=880):
    """Open `url` as an app window. Returns (how, process-or-None).

    The returned process is NOT a reliable handle on the window. On macOS,
    launching Chrome while Chrome is already running hands the window to the
    running instance and the process we spawned exits within a second. The
    caller must not treat that exit as "the user closed the window".
    """
    browser = find_browser()
    if browser is None:
        import webbrowser
        webbrowser.open(url)
        return "tab", None
    profile_dir = profile_dir or os.path.join(
        os.path.expanduser("~"), ".cache", "nd36", "window")
    os.makedirs(profile_dir, exist_ok=True)
    proc = subprocess.Popen(
        app_command(browser, url, profile_dir, width, height),
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return os.path.basename(browser), proc


# --------------------------------------------------------------------------
# installing a launcher
# --------------------------------------------------------------------------

def repo_root():
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def install_mac_app(target_dir=None, python=None, repo=None, name="ND36"):
    """Write a real .app bundle, then check it is actually there.

    A macOS app is a directory holding an Info.plist, an executable and an
    icon. That really is all. The verification at the end exists because the
    first version printed a path and never checked anything had been written
    to it -- exactly the sort of lie that costs somebody twenty minutes
    looking in the wrong folder.
    """
    from . import icon as icon_mod

    target_dir = os.path.expanduser(target_dir or "~/Applications")
    python = python or sys.executable
    repo = repo or repo_root()
    bundle = os.path.join(target_dir, name + ".app")
    contents = os.path.join(bundle, "Contents")
    macos = os.path.join(contents, "MacOS")
    resources = os.path.join(contents, "Resources")
    os.makedirs(macos, exist_ok=True)
    os.makedirs(resources, exist_ok=True)

    script = os.path.join(macos, "run")
    with open(script, "w") as fh:
        fh.write("#!/bin/sh\n"
                 "# ND36. Written by `nd install-app`; edit freely.\n"
                 'cd "%s" || exit 1\n'
                 'exec "%s" -m nd36.cli app --pick\n' % (repo, python))
    os.chmod(script, 0o755)

    icon_mod.write_icns(os.path.join(resources, "nd36.icns"))

    with open(os.path.join(contents, "Info.plist"), "wb") as fh:
        plistlib.dump({
            "CFBundleName": name,
            "CFBundleDisplayName": name,
            "CFBundleIdentifier": "dev.nd36.app",
            "CFBundleVersion": "1.0",
            "CFBundleShortVersionString": "1.0",
            "CFBundleExecutable": "run",
            "CFBundleIconFile": "nd36",
            "CFBundlePackageType": "APPL",
            "LSMinimumSystemVersion": "11.0",
            "NSHighResolutionCapable": True,
        }, fh)

    verify_bundle(bundle)
    _register_with_launch_services(bundle)
    return bundle


def verify_bundle(bundle):
    """Raise if what we just wrote is not a working app."""
    needed = [
        os.path.join(bundle, "Contents", "Info.plist"),
        os.path.join(bundle, "Contents", "MacOS", "run"),
        os.path.join(bundle, "Contents", "Resources", "nd36.icns"),
    ]
    missing = [p for p in needed if not os.path.exists(p)]
    if missing:
        raise RuntimeError("the bundle is incomplete, missing: %s"
                           % ", ".join(missing))
    if not os.access(needed[1], os.X_OK):
        raise RuntimeError("%s is not executable" % needed[1])
    with open(needed[0], "rb") as fh:
        plist = plistlib.load(fh)
    if plist.get("CFBundleExecutable") != "run":
        raise RuntimeError("Info.plist does not point at the executable")
    return True


def _register_with_launch_services(bundle):
    """Nudge Finder and Spotlight so the app appears straight away."""
    lsregister = ("/System/Library/Frameworks/CoreServices.framework/"
                  "Frameworks/LaunchServices.framework/Support/lsregister")
    if os.path.exists(lsregister):
        try:
            subprocess.run([lsregister, "-f", bundle], timeout=15,
                           stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL)
        except Exception:
            pass                 # cosmetic; the app works either way


def reveal(path):
    """Show it in Finder, so there is no doubt it exists."""
    try:
        if sys.platform == "darwin":
            subprocess.run(["open", "-R", path], timeout=15)
            return True
        if shutil.which("xdg-open"):
            subprocess.run(["xdg-open", os.path.dirname(path)], timeout=15)
            return True
    except Exception:
        pass
    return False


DESKTOP = """[Desktop Entry]
Type=Application
Name={name}
Comment=Replay what your code did
Exec={python} -m nd36.cli app --pick
Path={repo}
Icon=nd36
Terminal=false
Categories=Development;Debugger;
"""


def install_linux_launcher(target_dir=None, python=None, repo=None,
                           name="ND36"):
    from . import icon as icon_mod

    target_dir = os.path.expanduser(
        target_dir or "~/.local/share/applications")
    python = python or sys.executable
    repo = repo or repo_root()
    os.makedirs(target_dir, exist_ok=True)

    icon_dir = os.path.expanduser("~/.local/share/icons/hicolor/256x256/apps")
    os.makedirs(icon_dir, exist_ok=True)
    icon_mod.write_png(os.path.join(icon_dir, "nd36.png"), 256)

    path = os.path.join(target_dir, "nd36.desktop")
    with open(path, "w") as fh:
        fh.write(DESKTOP.format(name=name, python=python, repo=repo))
    os.chmod(path, 0o755)
    if shutil.which("update-desktop-database"):
        try:
            subprocess.run(["update-desktop-database", target_dir],
                           timeout=15, stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL)
        except Exception:
            pass
    return path


def install(target_dir=None, python=None, repo=None):
    if sys.platform == "darwin":
        return install_mac_app(target_dir, python, repo)
    if sys.platform.startswith("win"):
        raise RuntimeError("Windows launchers are not generated yet; run "
                           "`nd app` from a terminal, or make a shortcut to "
                           "`python -m nd36.cli app --pick`")
    return install_linux_launcher(target_dir, python, repo)


# --------------------------------------------------------------------------
# the VS Code extension
# --------------------------------------------------------------------------

def vscode_extensions_dir():
    if sys.platform.startswith("win"):
        return os.path.join(os.environ.get("USERPROFILE", ""),
                            ".vscode", "extensions")
    return os.path.expanduser("~/.vscode/extensions")


def install_vscode(target_dir=None, source=None, version="0.1.0"):
    """Copy the extension into VS Code's extensions folder.

    `code --install-extension` wants a packaged .vsix, which wants Node and
    @vscode/vsce. Copying the folder into ~/.vscode/extensions is what that
    packaging ends up doing anyway, and it needs nothing installed. The
    folder name must be `publisher.name-version` or VS Code ignores it.
    """
    source = source or os.path.join(repo_root(), "vscode-extension")
    if not os.path.isdir(source):
        raise RuntimeError("no extension source at %s" % source)
    target_dir = os.path.expanduser(target_dir or vscode_extensions_dir())
    os.makedirs(target_dir, exist_ok=True)
    dest = os.path.join(target_dir, "nd36.nd-%s" % version)
    if os.path.exists(dest):
        shutil.rmtree(dest)
    shutil.copytree(source, dest,
                    ignore=shutil.ignore_patterns("node_modules", "*.vsix",
                                                  ".git"))
    for needed in ("package.json", "extension.js"):
        if not os.path.exists(os.path.join(dest, needed)):
            raise RuntimeError("the copy is missing %s" % needed)
    return dest
