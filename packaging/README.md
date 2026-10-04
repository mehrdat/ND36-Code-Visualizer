# Installing ND36 on a system

Three routes, in order of how soon they work.

---

## 1. Right now, on your own machine

```bash
cd ~/Desktop/claude_code/nd36
pip install --user -e .        # -e = edits take effect immediately
nd doctor
```

That puts `nd` on your PATH. On macOS with Homebrew Python you may need
`pip3`; if `nd` is still not found, your user bin folder is not on PATH:

```bash
echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.zshrc && exec zsh
```

**Cleaner:** `pipx install -e .` keeps it in its own virtualenv and still
puts `nd` on PATH. `brew install pipx` or `pacman -S python-pipx`.

---

## 2. Homebrew (macOS and Linux)

A formula is in `nd36.rb`. Homebrew installs from a **URL with a checksum**,
so this needs a published release first.

```bash
# once: create the repo and tag a release
gh repo create mehrdat/nd36 --public --source=. --push
git tag v0.1.0 && git push --tags

# get the digest of the tarball GitHub generates
curl -sL https://github.com/mehrdat/nd36/archive/refs/tags/v0.1.0.tar.gz \
  | shasum -a 256
# paste it into nd36.rb, replacing REPLACE_WITH_THE_REAL_DIGEST
```

Then publish it as a **tap** — a repo named `homebrew-tap`:

```bash
gh repo create mehrdat/homebrew-tap --public
mkdir -p homebrew-tap/Formula && cp nd36.rb homebrew-tap/Formula/
cd homebrew-tap && git add . && git commit -m "nd36 0.1.0" && git push
```

Anyone can then:

```bash
brew tap mehrdat/tap
brew install nd36
```

`brew install` runs the formula's `test do` block, which records a trace and
reads a variable back out of it — so a broken release fails at install time
rather than in somebody's terminal.

---

## 3. Arch Linux / AUR

`PKGBUILD` is here. The AUR is a git repo per package.

```bash
# test the build locally first
cd packaging && makepkg -si

# then publish
#  - make an AUR account, add your SSH key
git clone ssh://aur@aur.archlinux.org/nd36.git aur-nd36
cp PKGBUILD aur-nd36/
cd aur-nd36
makepkg --printsrcinfo > .SRCINFO     # required; the AUR rejects without it
git add PKGBUILD .SRCINFO
git commit -m "nd36 0.1.0"
git push
```

Anyone on Arch can then:

```bash
yay -S nd36          # or paru -S nd36
```

Before pushing, replace `sha256sums=('SKIP')` with the real digest:

```bash
updpkgsums        # from pacman-contrib — rewrites it for you
```

The `check()` function runs four of the test suites during the build, so a
package that does not work will not be built.

**Order matters:** tag a GitHub release *first*. Both Homebrew and the AUR
download a tarball from a URL; neither can package a folder on your laptop.

---

## 4. PyPI (makes both of the above simpler)

```bash
python3 -m build
python3 -m twine upload dist/*
```

Then `pip install nd36` works everywhere, and the Homebrew formula can use
`url "https://files.pythonhosted.org/..."` instead of a GitHub tarball.

---

## What `nd install-app` does, and does not

It writes a launcher **for the copy of ND36 you already have** — it bakes in
the path to this folder and to the Python that ran it. It is not a
redistributable app: move or delete the folder and the launcher stops
working. That is the right trade for a tool you are still changing. A
signed, self-contained `.app` is a release-time job, after the code settles.
