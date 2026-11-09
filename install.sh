#!/bin/sh
# One-line installer for Games: the retro library for all terminal games, plus every game.
#
#   macOS / Linux:      curl -fsSL https://raw.githubusercontent.com/RaphaelA4U/games/main/install.sh | sh
#   iSH (iOS) / Alpine: wget -qO- https://raw.githubusercontent.com/RaphaelA4U/games/main/install.sh | sh
#
# Installs into ~/.games, creates a virtualenv, and links the `games`
# command into ~/.local/bin. Safe to re-run; it then updates in place.
#
# Environment overrides:
#   GAMES_HOME      install directory (default: ~/.games)
#   GAMES_REPO_URL  git remote to install from
#   GAMES_YES=1     non-interactive: auto-confirm system package installs
#   GAMES_ONLY=1    install only the library, not the games
#
# POSIX sh: this script must keep working under busybox ash (iSH) and dash.
set -eu
(set -o pipefail) 2>/dev/null && set -o pipefail || true

REPO_URL="${GAMES_REPO_URL:-https://github.com/RaphaelA4U/games.git}"
APP_DIR="${GAMES_HOME:-$HOME/.games}"
VENV="$APP_DIR/.venv"
BIN_DIR="$HOME/.local/bin"
MIN_PY_MAJOR=3 MIN_PY_MINOR=9

say() { printf '\033[38;5;99m[games]\033[0m %s\n' "$1"; }
err() { printf '\033[31m[games] Error:\033[0m %s\n' "$1" >&2; }

# --- platform / package manager detection --------------------------------
OS="$(uname -s 2>/dev/null || echo unknown)"

# Git Bash / MSYS / Cygwin: hand over to the native PowerShell installer so
# the same one-liner works on Windows too (venv layout differs there).
case "$OS" in
    MINGW*|MSYS*|CYGWIN*)
        say "Windows detected; handing over to the PowerShell installer..."
        exec powershell.exe -NoProfile -ExecutionPolicy Bypass -Command \
            "irm https://raw.githubusercontent.com/RaphaelA4U/games/main/install.ps1 | iex"
        ;;
esac
IS_ALPINE=0; [ -f /etc/alpine-release ] && IS_ALPINE=1
IS_ISH=0; case "$(uname -r 2>/dev/null)" in *ish*) IS_ISH=1 ;; esac

PKG=""
if command -v apk >/dev/null 2>&1; then PKG=apk
elif command -v apt-get >/dev/null 2>&1; then PKG=apt
elif command -v dnf >/dev/null 2>&1; then PKG=dnf
elif command -v pacman >/dev/null 2>&1; then PKG=pacman
elif command -v brew >/dev/null 2>&1; then PKG=brew
fi

confirm() { # $1 = question; true if GAMES_YES=1 or user says yes on the tty
    [ "${GAMES_YES:-0}" = "1" ] && return 0
    # /dev/tty can exist yet be unopenable (no controlling terminal, e.g. CI).
    if { printf '[games] %s [y/N] ' "$1" > /dev/tty; } 2>/dev/null; then
        { IFS= read -r ans < /dev/tty; } 2>/dev/null || return 1
        case "$ans" in y|Y|yes|YES) return 0 ;; esac
    fi
    return 1
}

run_root() { # run as root; use sudo when we are not root (brew never wants root)
    if [ "$PKG" = brew ] || [ "$(id -u)" = 0 ]; then "$@"
    elif command -v sudo >/dev/null 2>&1; then sudo "$@"
    else err "need root (or sudo) to run: $*"; return 1
    fi
}

pkg_install() { # $@ = package names for the detected manager
    say "Installing with $PKG: $*"
    case "$PKG" in
        apk)    run_root apk add --no-cache "$@" ;;
        apt)    run_root apt-get update -qq && run_root apt-get install -y -qq "$@" ;;
        dnf)    run_root dnf install -y -q "$@" ;;
        pacman) run_root pacman -S --noconfirm --needed "$@" ;;
        brew)   brew install "$@" ;;
        *)      return 1 ;;
    esac
}

need_cmd() { # $1 = command, $2 = package name(s), $3 = manual hint
    command -v "$1" >/dev/null 2>&1 && return 0
    if [ -n "$PKG" ] && confirm "'$1' is missing. Install it now with $PKG?"; then
        # shellcheck disable=SC2086
        pkg_install $2 && command -v "$1" >/dev/null 2>&1 && return 0
    fi
    err "'$1' is required. Install it first ($3), then re-run this installer."
    exit 2
}

py_ok() { "$1" -c "import sys; sys.exit(0 if sys.version_info >= ($MIN_PY_MAJOR, $MIN_PY_MINOR) else 1)" 2>/dev/null; }

find_python() {
    for c in python3 python3.13 python3.12 python3.11 python3.10 python3.9 python; do
        if command -v "$c" >/dev/null 2>&1 && py_ok "$c"; then
            command -v "$c"; return 0
        fi
    done
    return 1
}

# --- prerequisites (checked BEFORE anything is cloned or written) --------
case "$PKG" in
    apk)  need_cmd git git "apk add git" ;;
    apt)  need_cmd git git "sudo apt-get install git" ;;
    *)    need_cmd git git "https://git-scm.com" ;;
esac

PYTHON="$(find_python || true)"
if [ -z "$PYTHON" ]; then
    FOUND="$(command -v python3 >/dev/null 2>&1 && python3 -V 2>&1 || echo none)"
    say "Python >= $MIN_PY_MAJOR.$MIN_PY_MINOR not found (found: $FOUND)."
    case "$PKG" in
        apk)  PYPKGS="python3 py3-pip" ;;
        apt)  PYPKGS="python3 python3-venv python3-pip" ;;
        dnf)  PYPKGS="python3" ;;
        pacman) PYPKGS="python" ;;
        brew) PYPKGS="python3" ;;
        *)    PYPKGS="" ;;
    esac
    if [ -n "$PYPKGS" ] && confirm "Install Python now with $PKG ($PYPKGS)?"; then
        # shellcheck disable=SC2086
        pkg_install $PYPKGS
        PYTHON="$(find_python || true)"
    fi
fi
if [ -z "$PYTHON" ]; then
    err "Python $MIN_PY_MAJOR.$MIN_PY_MINOR+ is required and could not be installed automatically."
    err "Install it with your package manager (apk add python3 / apt install python3 python3-venv / brew install python3) and re-run."
    exit 2
fi
say "Using $PYTHON ($("$PYTHON" -V 2>&1))"

# --- clone / update ------------------------------------------------------
if [ -d "$APP_DIR/.git" ]; then
    say "Existing install found in $APP_DIR; updating..."
    if ! git -C "$APP_DIR" pull --ff-only; then
        # The checkout is app-owned; a blocked fast-forward gets repaired.
        say "Update blocked; repairing the checkout (reset to upstream)..."
        git -C "$APP_DIR" fetch origin
        git -C "$APP_DIR" reset --hard '@{u}' 2>/dev/null \
            || git -C "$APP_DIR" reset --hard origin/main
    fi
elif [ -e "$APP_DIR" ] && [ ! -d "$APP_DIR/.git" ] && [ -n "$(ls -A "$APP_DIR" 2>/dev/null)" ]; then
    err "$APP_DIR exists but is not a games checkout; move it aside and re-run."
    exit 1
else
    say "Cloning into $APP_DIR ..."
    git clone --depth 1 "$REPO_URL" "$APP_DIR"
fi

# --- virtualenv (recreated when broken or on an outdated Python) ---------
if [ -e "$VENV" ] && ! py_ok "$VENV/bin/python"; then
    say "Existing virtualenv is broken or on an old Python; recreating..."
    rm -rf "$VENV"
fi
if [ ! -e "$VENV" ]; then
    say "Creating virtualenv..."
    if [ "$IS_ALPINE" = 1 ]; then
        # --without-pip skips ensurepip, which is unusably slow on iSH.
        "$PYTHON" -m venv --without-pip "$VENV" \
            || { rm -rf "$VENV"; err "could not create a virtualenv."; exit 1; }
    elif ! "$PYTHON" -m venv "$VENV"; then
        rm -rf "$VENV"
        if [ "$PKG" = apt ] && confirm "venv failed (python3-venv missing?). Install python3-venv and retry?"; then
            pkg_install python3-venv python3-pip
            "$PYTHON" -m venv "$VENV" || { rm -rf "$VENV"; err "could not create a virtualenv."; exit 1; }
        else
            err "could not create a virtualenv (on Debian/Ubuntu: sudo apt install python3-venv)."
            exit 1
        fi
    fi
fi

# --- install the app (metadata in pyproject.toml pulls in dependencies) --
say "Installing dependencies (textual)..."
if [ -x "$VENV/bin/pip" ]; then
    "$VENV/bin/pip" install -q --disable-pip-version-check -e "$APP_DIR"
else
    # pip-less venv (Alpine/iSH): drive the venv with an outside pip >= 22.3
    # via `pip --python`. Alpine 3.14 ships pip 20; upgrade it (user site) first.
    if ! "$PYTHON" -c 'import pip; from pip._vendor.packaging.version import Version; import sys; sys.exit(0 if Version(pip.__version__) >= Version("22.3") else 1)' 2>/dev/null; then
        say "Upgrading pip (needed for editable installs)..."
        "$PYTHON" -m pip install --user -q --disable-pip-version-check --no-warn-script-location --no-cache-dir --no-compile -U pip
    fi
    if [ "$IS_ISH" = 1 ]; then
        say "iSH heads-up: this step unpacks thousands of files and takes a while"
        say "on a phone. It is NOT stuck. Keep iSH in the foreground and the"
        say "screen awake; iOS pauses it in the background."
    fi
    # Not -q on purpose: on iSH this is the slow step and progress reassures.
    # --no-cache-dir and --no-compile cut the file writes and skip bytecode
    # compilation, the two things iSH's emulated filesystem/CPU choke on.
    # --only-binary=:all: forbids doomed source builds (no compiler on iSH);
    # every dependency ships a pure-Python wheel.
    "$PYTHON" -m pip --python "$VENV/bin/python" install --disable-pip-version-check --no-cache-dir --no-compile --only-binary=:all: -e "$APP_DIR"
fi

# --- command on PATH -----------------------------------------------------
mkdir -p "$BIN_DIR"
chmod +x "$APP_DIR/bin/games"
ln -sf "$APP_DIR/bin/games" "$BIN_DIR/games"

case ":$PATH:" in
    *":$BIN_DIR:"*) ;;
    *)
        RC="$HOME/.profile"
        case "$(basename "${SHELL:-}")" in
            zsh) RC="$HOME/.zshrc" ;;
            bash) RC="$HOME/.bashrc" ;;
        esac
        if ! grep -qs 'Added by games installer' "$RC"; then
            printf '\n# Added by games installer\nexport PATH="$HOME/.local/bin:$PATH"\n' >> "$RC"
            say "Added $BIN_DIR to PATH in $RC"
        fi
        say "Open a new terminal (or run: . $RC) so the command is found."
        ;;
esac

# --- verify --------------------------------------------------------------
"$VENV/bin/python" -c 'import textual' \
    || { err "dependency verification failed; re-run the installer."; exit 1; }
"$VENV/bin/python" -m games_app --version

# --- every game, each through its own installer --------------------------
# GAMES_ONLY=1 installs just the library (games can be added from the shelf).
if [ "${GAMES_ONLY:-0}" != 1 ]; then
    say "Installing every game (each with its own installer)..."
    "$VENV/bin/python" -m games_app --install-all || say "Some games did not install; retry them from the library."
fi
say "Done! Open the library with: games"
