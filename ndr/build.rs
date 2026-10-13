//! Three jobs, all about the Python that `ndr` embeds.
//!
//! 1. Refuse, in plain words, to build against a Python older than 3.12.
//!    `ndr` is built on `sys.monitoring` (PEP 669), which does not exist
//!    before 3.12 -- and without this check the failure is five unreadable
//!    "cannot find value in module ffi" errors.
//! 2. Give the binary an rpath to libpython, so the loader finds it on macOS
//!    with Anaconda and on Linux with a Python outside /usr.
//! 3. Record the interpreter's own prefix, so the embedded Python finds its
//!    standard library no matter where the `ndr` binary ends up.
use std::process::Command;

const MIN: (u8, u8) = (3, 12);

fn main() {
    pyo3_build_config::use_pyo3_cfgs();
    println!("cargo:rerun-if-env-changed=PYO3_PYTHON");
    println!("cargo:rerun-if-changed=build.rs");

    // The interpreter pyo3 actually chose -- not a guess at it.
    let cfg = pyo3_build_config::get();
    let (major, minor) = (cfg.version.major, cfg.version.minor);
    let exe = cfg.executable.clone().unwrap_or_else(|| "python3".into());
    if (major, minor) < MIN {
        eprintln!(
            "\n\
             ndr needs Python {}.{} or newer. This build found Python {major}.{minor}\n\
             at: {exe}\n\
             \n\
             ndr records programs with sys.monitoring (PEP 669), which Python\n\
             added in 3.12. There is no way to build it against {major}.{minor}.\n\
             \n\
             Install a newer Python, then point the build at it:\n\
             \n\
             \x20 Ubuntu / Debian   sudo add-apt-repository ppa:deadsnakes/ppa\n\
             \x20                   sudo apt install python3.12 python3.12-dev\n\
             \x20 Arch              sudo pacman -S python          (already 3.12+)\n\
             \x20 macOS             brew install python@3.13\n\
             \x20 anywhere          curl -LsSf https://astral.sh/uv/install.sh | sh\n\
             \x20                   uv python install 3.13\n\
             \n\
             \x20 PYO3_PYTHON=$(which python3.12) cargo build --release\n\
             \n\
             (`uv python find 3.13` prints the path if you used uv.)\n",
            MIN.0, MIN.1
        );
        std::process::exit(1);
    }

    let probe = "import sys, sysconfig\n\
                 print(sysconfig.get_config_var('LIBDIR') or '')\n\
                 print(sys.base_prefix)";
    if let Ok(out) = Command::new(&exe).args(["-c", probe]).output() {
        let text = String::from_utf8_lossy(&out.stdout).to_string();
        let mut lines = text.lines();
        if let Some(libdir) = lines.next() {
            if !libdir.is_empty() {
                println!("cargo:rustc-link-arg=-Wl,-rpath,{libdir}");
            }
        }
        if let Some(prefix) = lines.next() {
            println!("cargo:rustc-env=NDR_PYTHON_HOME={prefix}");
        }
    }
    println!("cargo:rustc-env=NDR_PYTHON_VERSION={major}.{minor}");
    println!("cargo:rustc-env=NDR_PYTHON_EXE={exe}");
}
