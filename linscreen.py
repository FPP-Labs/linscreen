#!/usr/bin/env python3
import sys, os, json, datetime, subprocess, tempfile, time, re, select, shutil, signal, threading, fcntl
from pathlib import Path
from urllib.parse import unquote, urlparse

from PyQt6.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QHBoxLayout, QPushButton,
    QLabel, QSystemTrayIcon, QMenu, QCheckBox, QFrame, QMessageBox,
    QKeySequenceEdit, QScrollArea, QSizePolicy, QBoxLayout,
)
from PyQt6.QtCore import (
    Qt, QRect, QRectF, QPoint, QPointF, QTimer, QObject, QThread, QProcess, pyqtSignal, QUrl,
    QPropertyAnimation, QEasingCurve, pyqtProperty,
)
from PyQt6.QtNetwork import QLocalServer, QLocalSocket
from PyQt6.QtGui import (
    QPixmap, QImage, QPainter, QColor, QIcon, QCursor,
    QGuiApplication, QFont, QPen, QBrush, QKeySequence, QDesktopServices,
    QPalette, QFontDatabase, QFontMetrics,
)

_EXEC_PATH = Path(__file__).resolve()
_EXEC_DIR  = _EXEC_PATH.parent

if any(p in ("usr", "bin", "opt") for p in _EXEC_DIR.parts):
    SCREENSHOTS_DIR = Path.home() / "Pictures" / "LinScreen"
else:
    SCREENSHOTS_DIR = _EXEC_DIR / "screenshots"

CONFIG_DIR  = Path.home() / ".config" / "linscreen"
CONFIG_FILE = CONFIG_DIR / "config.json"
LOCK_FILE   = CONFIG_DIR / "instance.lock"
SOCKET_NAME = "com.fpplabs.linscreen.ipc"
APP_VERSION  = "1.0.0"
APP_URL      = "https://github.com/FPP-Labs/linscreen"

DEFAULT_SHORTCUT = "Print"
_ACTIVE_PROCESS_LOCK = threading.Lock()
_ACTIVE_PROCESS = None
_INSTANCE_LOCK = None


def current_desktop():
    return ":".join(
        os.environ.get(name, "")
        for name in ("XDG_CURRENT_DESKTOP", "XDG_SESSION_DESKTOP", "DESKTOP_SESSION")
    ).upper()


def is_kde(): return "KDE" in current_desktop()
def is_gnome(): return "GNOME" in current_desktop()
def is_hyprland(): return "HYPRLAND" in current_desktop() or bool(os.environ.get("HYPRLAND_INSTANCE_SIGNATURE"))
def is_niri(): return "NIRI" in current_desktop() or bool(os.environ.get("NIRI_SOCKET"))
def is_sway(): return "SWAY" in current_desktop() or bool(os.environ.get("SWAYSOCK"))
def is_i3(): return "I3" in current_desktop().split(":") or bool(os.environ.get("I3SOCK"))
def is_wayland(): return bool(os.environ.get("WAYLAND_DISPLAY"))


def _which(command):
    found = shutil.which(command)
    if found:
        return found
    fallback = Path("/usr/bin") / command
    if fallback.exists() and os.access(fallback, os.X_OK):
        return str(fallback)
    return None


def _slurp_command():
    slurp = _which("slurp")
    if not slurp:
        return None
    return [
        slurp,
        "-b", "#00000099",
        "-s", "#00000000",
        "-c", "#1e90ffff",
        "-w", "2",
    ]


def _acquire_instance_lock():
    global _INSTANCE_LOCK
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    lock = open(LOCK_FILE, "a+")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock.close()
        return False
    lock.seek(0)
    lock.truncate()
    lock.write(str(os.getpid()))
    lock.flush()
    _INSTANCE_LOCK = lock
    return True


def _locked_instance_pid():
    try:
        text = LOCK_FILE.read_text().strip()
        return int(text) if text else None
    except Exception:
        return None


def _find_linscreen_pids():
    pids = []
    proc_root = Path("/proc")
    for entry in proc_root.iterdir():
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        if pid == os.getpid():
            continue
        try:
            raw = (entry / "cmdline").read_bytes()
        except Exception:
            continue
        args = [a.decode(errors="ignore") for a in raw.split(b"\0") if a]
        if any(Path(arg).name in ("linscreen", "linscreen.py") for arg in args):
            pids.append(pid)
    return pids


def _terminate_pids(pids, timeout=3):
    if not pids:
        return False
    sent = False
    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
            sent = True
        except ProcessLookupError:
            sent = True
        except Exception:
            pass

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        alive = []
        for pid in pids:
            try:
                os.kill(pid, 0)
                alive.append(pid)
            except ProcessLookupError:
                pass
            except Exception:
                pass
        if not alive:
            return True
        time.sleep(0.1)

    for pid in pids:
        try:
            os.kill(pid, signal.SIGKILL)
            sent = True
        except ProcessLookupError:
            sent = True
        except Exception:
            pass
    return sent


def _stop_locked_instance(timeout=3):
    pid = _locked_instance_pid()
    if not pid or pid == os.getpid():
        return _terminate_pids(_find_linscreen_pids(), timeout=timeout)
    return _terminate_pids([pid], timeout=timeout)


def _to_pixmap(image):
    if isinstance(image, QPixmap):
        return image
    return QPixmap.fromImage(image)


def _set_active_process(proc):
    global _ACTIVE_PROCESS
    with _ACTIVE_PROCESS_LOCK:
        _ACTIVE_PROCESS = proc


def _clear_active_process(proc):
    global _ACTIVE_PROCESS
    with _ACTIVE_PROCESS_LOCK:
        if _ACTIVE_PROCESS is proc:
            _ACTIVE_PROCESS = None


def _terminate_process(proc):
    if not proc or proc.poll() is not None:
        return
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    except Exception:
        try: proc.terminate()
        except Exception: return
    try:
        proc.wait(timeout=0.7)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except Exception:
            try: proc.kill()
            except Exception: pass
        try: proc.wait(timeout=1)
        except Exception: pass


def cancel_active_capture():
    with _ACTIVE_PROCESS_LOCK:
        proc = _ACTIVE_PROCESS
    if not proc or proc.poll() is not None:
        return False
    _terminate_process(proc)
    return True


def _run_command(cmd, *, timeout, text=False, input_data=None):
    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE if input_data is not None else None,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=text,
        start_new_session=True,
    )
    _set_active_process(proc)
    try:
        stdout, stderr = proc.communicate(input=input_data, timeout=timeout)
        return subprocess.CompletedProcess(cmd, proc.returncode, stdout, stderr)
    except subprocess.TimeoutExpired:
        _terminate_process(proc)
        raise
    finally:
        _clear_active_process(proc)


def _selection_timeout():
    return 15 if is_niri() else 60


class Config:
    _DEFAULTS = {
        "autostart": False,
        "shortcut": DEFAULT_SHORTCUT,
    }
    def __init__(self):
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        self._d = dict(self._DEFAULTS)
        if CONFIG_FILE.exists():
            try:
                self._d.update(json.load(open(CONFIG_FILE)))
            except Exception:
                pass
    def save(self):
        json.dump(self._d, open(CONFIG_FILE, "w"), indent=2)
    def __getitem__(self, k):
        return self._d.get(k, self._DEFAULTS.get(k))
    def __setitem__(self, k, v):
        self._d[k] = v
        self.save()


# ---- Compositor key bindings ---------------------------------------------
#
# On Wayland an app cannot grab global keys itself, so for compositors that are
# configured through a text file LinScreen writes the bind there. Its lines live
# in a marked block; a bind the user wrote by hand is adopted as well, so the
# shortcut is found again after a reinstall or with a fresh LinScreen config.
# Anything LinScreen cannot parse with certainty is left untouched.

SCREENSHOT_CMD = "linscreen --screenshot"
_BLOCK_BEGIN = "LinScreen: begin (managed by LinScreen, edit the shortcut in its settings)"
_BLOCK_END = "LinScreen: end"
# Older marker style in Omarchy's bindings.lua; adopted and rewritten as the current block.
_LEGACY_BEGIN = "LinScreen managed shortcuts: begin"
_LEGACY_END = "LinScreen managed shortcuts: end"
_QT_MODS = ("Ctrl", "Alt", "Meta", "Shift")

_QT_TO_KEYSYM = {
    "Esc": "Escape", "Tab": "Tab", "Backspace": "BackSpace", "Return": "Return", "Enter": "KP_Enter",
    "Ins": "Insert", "Del": "Delete", "Pause": "Pause", "Print": "Print", "Home": "Home", "End": "End",
    "Left": "Left", "Up": "Up", "Right": "Right", "Down": "Down", "PgUp": "Prior", "PgDown": "Next",
    "ScrollLock": "Scroll_Lock", "Menu": "Menu", "Space": "space",
    ".": "period", ",": "comma", "/": "slash", "-": "minus", "=": "equal", ";": "semicolon",
    "'": "apostrophe", "+": "plus", "[": "bracketleft", "]": "bracketright", "\\": "backslash", "`": "grave",
    "Media Play": "XF86AudioPlay", "Media Pause": "XF86AudioPause", "Media Stop": "XF86AudioStop",
    "Media Next": "XF86AudioNext", "Media Previous": "XF86AudioPrev",
    "Volume Up": "XF86AudioRaiseVolume", "Volume Down": "XF86AudioLowerVolume", "Volume Mute": "XF86AudioMute",
}
_KEYSYM_TO_QT = {v.lower(): k for k, v in _QT_TO_KEYSYM.items()}
_KEYSYM_TO_QT.update({"escape": "Esc", "esc": "Esc", "page_up": "PgUp", "page_down": "PgDown",
                      "prior": "PgUp", "next": "PgDown", "enter": "Return", "print": "Print"})
_FKEY = re.compile(r"F([1-9]|1[0-9]|2[0-4])", re.IGNORECASE)


def _keysym(key):
    """Qt key name -> xkb keysym, or None if it cannot be expressed in a config."""
    if key in _QT_TO_KEYSYM:
        return _QT_TO_KEYSYM[key]
    if re.fullmatch(r"[A-Z0-9]", key):
        return key.lower()
    if _FKEY.fullmatch(key):
        return key.upper()
    return None


def _qt_key(keysym):
    """xkb keysym as written in a config -> Qt key name, or None if unknown."""
    low = keysym.strip().lower()
    if low in _KEYSYM_TO_QT:
        return _KEYSYM_TO_QT[low]
    if re.fullmatch(r"[a-z0-9]", low):
        return low.upper()
    if _FKEY.fullmatch(low):
        return low.upper()
    return None


def _split_qt(shortcut):
    """'Meta+Shift+S' -> (['Meta', 'Shift'], 'S'); empty key for an empty shortcut."""
    portable = QKeySequence(shortcut).toString(QKeySequence.SequenceFormat.PortableText)
    if not portable:
        return [], ""
    if portable.endswith("++"):
        parts, key = portable[:-2].split("+"), "+"
    else:
        parts = portable.split("+")
        key = parts.pop()
    return [p for p in parts if p in _QT_MODS], key


def _join_qt(mods, key):
    portable = "+".join(sorted(set(mods), key=_QT_MODS.index) + [key])
    return QKeySequence(portable).toString(QKeySequence.SequenceFormat.PortableText) or portable


def _map_mods(tokens, table):
    """Modifier tokens -> Qt names; None if any token is not a known modifier."""
    mods = []
    for token in tokens:
        mod = table.get(token.strip().upper())
        if mod is None:
            return None
        mods.append(mod)
    return mods


class _BindBackend:
    """One compositor config format: how to render, find and apply LinScreen's bind."""

    name = ""
    comment = "#"
    validate_cmd = None   # e.g. ["niri", "validate", "-c"]; the config path is appended
    reload_cmd = None

    def __init__(self, path):
        self.path = path

    @property
    def display_path(self):
        home = str(Path.home())
        p = str(self.path)
        return "~" + p[len(home):] if p.startswith(home + os.sep) else p

    # Format-specific parts ------------------------------------------------

    def render(self, mods, keysym):
        """Config lines binding Qt modifiers + keysym to the screenshot command."""
        raise NotImplementedError

    def parse(self, line):
        """(qt_mods, qt_key) if the line is exactly a LinScreen screenshot bind, else None."""
        raise NotImplementedError

    def unbind_line(self, mods, keysym):
        """The companion 'unbind' line LinScreen writes before its bind, if any."""
        return None

    def conflict(self, text, mods, keysym):
        """A user's bind on the same keys that cannot be overridden, or None."""
        return None

    def insert_block(self, text, block):
        """Text with the block added, or None if there is no safe place for it."""
        text = text.rstrip()
        return f"{text}\n\n{block}\n" if text else f"{block}\n"

    def before_write(self):
        return None

    # Shared logic ----------------------------------------------------------

    def snippet(self, shortcut):
        mods, key = _split_qt(shortcut)
        keysym = _keysym(key) if key else None
        return "\n".join(self.render(mods, keysym)) if keysym else ""

    def _read(self):
        try:
            return self.path.read_text(encoding="utf-8", errors="surrogateescape")
        except FileNotFoundError:
            return ""

    def find(self):
        """Qt shortcut currently bound to the screenshot command, or None."""
        try:
            text = self._read()
        except Exception:
            return None
        for line in text.splitlines():
            parsed = self.parse(line)
            if parsed:
                return _join_qt(*parsed)
        return None

    def _strip_own(self, text):
        """Remove LinScreen's blocks and adopted bind lines. Raises ValueError on broken markers."""
        markers = {f"{self.comment} {_BLOCK_BEGIN}": f"{self.comment} {_BLOCK_END}",
                   f"{self.comment} {_LEGACY_BEGIN}": f"{self.comment} {_LEGACY_END}"}
        ends = set(markers.values())
        out, closing = [], None
        for line in text.split("\n"):
            marker = line.strip()
            if closing is None:
                if marker in markers:
                    closing = markers[marker]
                elif marker in ends:
                    raise ValueError(f"line '{marker}' has no matching block start")
                else:
                    out.append(line)
            elif marker == closing:
                closing = None
            elif marker in markers or marker in ends:
                raise ValueError(f"nested or unclosed LinScreen block near '{marker}'")
        if closing is not None:
            raise ValueError("LinScreen block is not closed (end marker missing)")

        kept = []
        for line in out:
            parsed = self.parse(line)
            if not parsed:
                kept.append(line)
                continue
            mods, key = parsed
            unbind = self.unbind_line(mods, _keysym(key))
            if unbind and kept and _norm(kept[-1]) == _norm(unbind):
                kept.pop()
        return "\n".join(kept)

    def _render_block(self, mods, keysym, indent=""):
        lines = [f"{self.comment} {_BLOCK_BEGIN}", *self.render(mods, keysym), f"{self.comment} {_BLOCK_END}"]
        return "\n".join(indent + l for l in lines)

    def _validate(self, target, text):
        if not self.validate_cmd or not shutil.which(self.validate_cmd[0]):
            return None
        # Validate a sibling copy so relative includes still resolve.
        tmp = target.with_name(f".{target.name}.linscreen-check")
        try:
            tmp.write_text(text, encoding="utf-8", errors="surrogateescape")
            r = subprocess.run(self.validate_cmd + [str(tmp)], capture_output=True, text=True, timeout=10)
            if r.returncode != 0:
                lines = (r.stderr or r.stdout).strip().splitlines()
                return lines[-1] if lines else "config validation failed"
        except Exception as exc:
            return str(exc)
        finally:
            try: tmp.unlink()
            except OSError: pass
        return None

    def _reload(self, state):
        """Reload the compositor; return an error string or None."""
        if not self.reload_cmd or not shutil.which(self.reload_cmd[0]):
            return None
        try:
            r = subprocess.run(self.reload_cmd, capture_output=True, text=True, timeout=8)
        except Exception as exc:
            return str(exc)
        out = (r.stdout or "") + (r.stderr or "")
        if r.returncode != 0 or '"success": false' in out:
            return out.strip() or "config reload failed"
        return None

    @staticmethod
    def _write(target, text):
        # Write next to the real file (symlinks are followed) and keep its permissions.
        tmp = target.with_name(f".{target.name}.linscreen-{os.getpid()}.tmp")
        tmp.write_text(text, encoding="utf-8", errors="surrogateescape")
        if target.exists():
            shutil.copymode(target, tmp)
        os.replace(tmp, target)

    def apply(self, shortcut):
        """Write the bind for `shortcut`. Returns (ok, message)."""
        mods, key = _split_qt(shortcut)
        if not key:
            return False, "The shortcut is empty."
        keysym = _keysym(key)
        if not keysym:
            return False, f"The key '{key}' cannot be written to the config. Choose another one."
        if not mods and not (key == "Print" or _FKEY.fullmatch(key) or keysym.startswith("XF86")):
            return False, ("Without a modifier, only Print, F1–F24 or a media key can be used; "
                           "otherwise the key would stop working in other apps.")
        target = self.path.resolve()
        try:
            original = self._read()
        except Exception as exc:
            return False, f"Could not read {self.display_path}: {exc}"
        clash = self.conflict(original, mods, keysym)
        if clash:
            return False, f"The shortcut is already taken in {self.display_path}: {clash}"
        try:
            stripped = self._strip_own(original)
        except ValueError as exc:
            return False, f"Config not changed: {exc}. Fix {self.display_path} by hand."
        updated = self.insert_block(stripped, self._render_block(mods, keysym))
        if updated is None:
            return False, f"Could not find a safe place for the bind in {self.display_path}."
        if updated == original:
            return True, f"Bind is already in {self.display_path}"
        problem = self._validate(target, updated)
        if problem:
            return False, f"Config not changed: {problem}"
        state = self.before_write()
        try:
            self._backup(target)
            self._write(target, updated)
        except OSError as exc:
            return False, f"Could not write {self.display_path}: {exc}"
        error = self._reload(state)
        if error:
            try:
                self._write(target, original)
                self._reload(None)
            except OSError:
                pass
            return False, f"Change rolled back, the compositor rejected the bind: {error}"
        return True, f"Bind written to {self.display_path}"

    @staticmethod
    def _backup(target):
        stamp = datetime.date.today().strftime("%Y%m%d")
        backup = target.with_name(f"{target.name}.bak-linscreen-{stamp}")
        if target.exists() and not backup.exists():
            shutil.copy2(target, backup)


def _norm(line):
    return re.sub(r"\s+", "", line).upper()


class _HyprBackend(_BindBackend):
    reload_cmd = ["hyprctl", "reload"]
    _MODS = {"Meta": "SUPER", "Ctrl": "CTRL", "Alt": "ALT", "Shift": "SHIFT"}
    _MODS_BACK = {"SUPER": "Meta", "WIN": "Meta", "LOGO": "Meta", "MOD4": "Meta",
                  "CTRL": "Ctrl", "CONTROL": "Ctrl", "ALT": "Alt", "MOD1": "Alt", "SHIFT": "Shift"}

    @staticmethod
    def _hypr_key(keysym):
        return keysym if keysym.startswith("XF86") else keysym.upper()

    @staticmethod
    def _config_errors():
        if not shutil.which("hyprctl"):
            return ""
        try:
            r = subprocess.run(["hyprctl", "configerrors"], capture_output=True, text=True, timeout=8)
            return (r.stdout or "").strip()
        except Exception:
            return ""

    def before_write(self):
        # Hyprland may auto-reload as soon as the file changes, so snapshot errors first.
        return self._config_errors()

    def _reload(self, state):
        error = super()._reload(state)
        if error or state is None:
            return error
        before = set(state.splitlines())
        new = [l for l in self._config_errors().splitlines() if l.strip() and l not in before]
        return "; ".join(new) or None


class _HyprLuaBackend(_HyprBackend):
    """Omarchy's Lua-based Hyprland config (bindings.lua)."""

    name = "Hyprland (Lua)"
    comment = "--"
    _BIND_RE = re.compile(
        r'^\s*o\.bind\(\s*"([^"]+)"\s*,\s*(?:"[^"]*"|nil)\s*,\s*"' + re.escape(SCREENSHOT_CMD)
        + r'"\s*(?:,\s*\{[^{}]*\})?\s*\)\s*(?:--.*)?$'
    )

    def _chord(self, mods, keysym):
        return " + ".join([self._MODS[m] for m in mods] + [self._hypr_key(keysym)])

    def render(self, mods, keysym):
        chord = self._chord(mods, keysym)
        return [f'hl.unbind("{chord}")', f'o.bind("{chord}", "LinScreen screenshot", "{SCREENSHOT_CMD}")']

    def unbind_line(self, mods, keysym):
        return f'hl.unbind("{self._chord(mods, keysym)}")'

    def parse(self, line):
        m = self._BIND_RE.match(line.rstrip("\r"))
        if not m:
            return None
        parts = [p.strip() for p in m.group(1).split("+")]
        if not parts or not all(parts):
            return None
        mods, key = _map_mods(parts[:-1], self._MODS_BACK), _qt_key(parts[-1])
        return (mods, key) if mods is not None and key else None


class _HyprConfBackend(_HyprBackend):
    name = "Hyprland"
    _BIND_RE = re.compile(
        r"^\s*bind[a-z]*\s*=\s*([^,]*),\s*([^,]+?)\s*,\s*exec\s*,\s*" + re.escape(SCREENSHOT_CMD) + r"\s*(?:#.*)?$"
    )

    def render(self, mods, keysym):
        hm = " ".join(self._MODS[m] for m in mods)
        key = self._hypr_key(keysym)
        return [f"unbind = {hm}, {key}", f"bind = {hm}, {key}, exec, {SCREENSHOT_CMD}"]

    def unbind_line(self, mods, keysym):
        return f"unbind = {' '.join(self._MODS[m] for m in mods)}, {self._hypr_key(keysym)}"

    def _variables(self):
        try:
            text = self._read()
        except Exception:
            return {}
        return {f"${k}": v for k, v in re.findall(r"^\s*\$(\w+)\s*=\s*(.*?)\s*$", text, re.MULTILINE)}

    def _parse_mods(self, text):
        """'SUPER SHIFT', 'SUPER_SHIFT', 'SUPERSHIFT', '$mainMod SHIFT' -> Qt mods; None if unsure."""
        variables = self._variables()
        text = re.sub(r"\$\w+", lambda m: variables.get(m.group(0), "\0"), text)
        rest = re.sub(r"[\s_+]", "", text.upper())
        mods = []
        while rest:
            for name in sorted(self._MODS_BACK, key=len, reverse=True):
                if rest.startswith(name):
                    mods.append(self._MODS_BACK[name])
                    rest = rest[len(name):]
                    break
            else:
                return None
        return mods

    def parse(self, line):
        m = self._BIND_RE.match(line.rstrip("\r"))
        if not m:
            return None
        mods, key = self._parse_mods(m.group(1)), _qt_key(m.group(2))
        return (mods, key) if mods is not None and key else None


class _SwayBackend(_BindBackend):
    name = "Sway"
    reload_cmd = ["swaymsg", "reload"]
    _MODS = {"Meta": "Mod4", "Ctrl": "Control", "Alt": "Mod1", "Shift": "Shift"}
    _MODS_BACK = {"MOD4": "Meta", "SUPER": "Meta", "CONTROL": "Ctrl", "CTRL": "Ctrl",
                  "MOD1": "Alt", "ALT": "Alt", "SHIFT": "Shift"}
    _BIND_RE = re.compile(
        r"^\s*bindsym((?:\s+--[\w-]+)*)\s+(\S+)\s+exec(?:\s+--no-startup-id)?\s+"
        + re.escape(SCREENSHOT_CMD) + r"\s*$"
    )

    def _line(self, combo):
        return f"bindsym --no-warn {combo} exec {SCREENSHOT_CMD}"

    def render(self, mods, keysym):
        return [self._line("+".join([self._MODS[m] for m in mods] + [keysym]))]

    def _variables(self):
        try:
            text = self._read()
        except Exception:
            return {}
        return dict(re.findall(r"^\s*set\s+(\$\w+)\s+(\S+)", text, re.MULTILINE))

    def parse(self, line):
        m = self._BIND_RE.match(line.rstrip("\r"))
        if not m:
            return None
        variables = self._variables()
        parts = [variables.get(p, p) for p in m.group(2).split("+")]
        mods, key = _map_mods(parts[:-1], self._MODS_BACK), _qt_key(parts[-1])
        return (mods, key) if mods is not None and key else None


class _I3Backend(_SwayBackend):
    name = "i3"
    validate_cmd = ["i3", "-C", "-c"]
    reload_cmd = ["i3-msg", "reload"]

    def _line(self, combo):
        return f"bindsym {combo} exec --no-startup-id {SCREENSHOT_CMD}"


class _NiriBackend(_BindBackend):
    name = "Niri"
    comment = "//"
    validate_cmd = ["niri", "validate", "-c"]
    _MODS = {"Meta": "Super", "Ctrl": "Ctrl", "Alt": "Alt", "Shift": "Shift"}
    _MODS_BACK = {"SUPER": "Meta", "MOD": "Meta", "WIN": "Meta", "CTRL": "Ctrl",
                  "CONTROL": "Ctrl", "ALT": "Alt", "SHIFT": "Shift"}
    _BIND_RE = re.compile(
        r'^\s*([\w+]+)[^{\n]*\{\s*(?:spawn\s+"linscreen"\s+"--screenshot"|spawn-sh\s+"'
        + re.escape(SCREENSHOT_CMD) + r'")\s*;\s*\}\s*(?://.*)?$'
    )
    _ANY_BIND_RE = re.compile(r"^\s*([\w+]+)[^{\n]*\{")
    _ALIASES = {"mod": "super", "win": "super", "control": "ctrl"}

    @staticmethod
    def _niri_key(keysym):
        return keysym.upper() if len(keysym) == 1 else keysym

    def render(self, mods, keysym):
        combo = "+".join([self._MODS[m] for m in mods] + [self._niri_key(keysym)])
        return [f'{combo} {{ spawn "linscreen" "--screenshot"; }}']

    def parse(self, line):
        m = self._BIND_RE.match(line.rstrip("\r"))
        if not m:
            return None
        parts = m.group(1).split("+")
        mods, key = _map_mods(parts[:-1], self._MODS_BACK), _qt_key(parts[-1])
        return (mods, key) if mods is not None and key else None

    @staticmethod
    def _binds_section(text):
        """(start, end) offsets of the `binds { ... }` body, or None."""
        m = re.search(r"^[ \t]*binds[ \t]*\{", text, re.MULTILINE)
        if not m:
            return None
        depth = 0
        for i in range(m.end() - 1, len(text)):
            if text[i] == "{":
                depth += 1
            elif text[i] == "}":
                depth -= 1
                if depth == 0:
                    return m.end(), i
        return None

    def conflict(self, text, mods, keysym):
        norm = lambda p: self._ALIASES.get(p.lower(), p.lower())
        wanted = {norm(p) for p in [self._MODS[m] for m in mods] + [keysym]}
        section = self._binds_section(text)
        for line in (text[section[0]:section[1]] if section else "").splitlines():
            m = self._ANY_BIND_RE.match(line)
            if m and not self.parse(line) and {norm(p) for p in m.group(1).split("+")} == wanted:
                return line.strip()
        return None

    def insert_block(self, text, block):
        # Niri binds must live inside the top-level `binds { }` section.
        if not re.search(r"^[ \t]*binds\b", text, re.MULTILINE):
            return super().insert_block(text, f"binds {{\n{block}\n}}")
        section = self._binds_section(text)
        if not section:
            return None
        newline = text.find("\n", section[0])
        if newline == -1 or newline >= section[1]:
            return None   # one-line `binds { ... }`: don't guess
        return text[:newline + 1] + block + "\n" + text[newline + 1:]

    def _render_block(self, mods, keysym, indent="    "):
        return super()._render_block(mods, keysym, indent)


def bind_backend():
    """The config-file backend for the running compositor, or None (KDE, GNOME, X11 DEs)."""
    hypr = Path.home() / ".config" / "hypr"
    if is_hyprland():
        if (hypr / "bindings.lua").exists():
            return _HyprLuaBackend(hypr / "bindings.lua")
        if (hypr / "bindings.conf").exists():
            return _HyprConfBackend(hypr / "bindings.conf")
        if (hypr / "hyprland.conf").exists():
            return _HyprConfBackend(hypr / "hyprland.conf")
        return None
    candidates = []
    if is_niri():
        candidates.append(_NiriBackend(Path.home() / ".config" / "niri" / "config.kdl"))
    if is_sway():
        candidates.append(_SwayBackend(Path.home() / ".config" / "sway" / "config"))
    if is_i3():
        candidates.append(_I3Backend(Path.home() / ".config" / "i3" / "config"))
    # Never create a config from scratch: for Sway/i3 that would replace the system default.
    return next((b for b in candidates if b.path.exists()), None)


def _grab_with_tool(cmd, tmp, name, timeout=10, cancel_codes=()):
    try:
        # If tmp is None, we expect output on stdout
        capture_out = tmp is None
        r = _run_command(cmd, timeout=timeout, text=False)
        err = (r.stderr or b"").decode(errors="ignore").strip()
        if r.returncode in cancel_codes and (not err or "cancel" in err.lower() or "отмен" in err.lower()):
            return None, f"{name}: capture cancelled by the user" + (f" ({err})" if err else ""), True
        if r.returncode == 0:
            px = QImage()
            if capture_out:
                if px.loadFromData(r.stdout):
                    return px, None, False
            else:
                px = QImage(tmp)
                if not px.isNull() and px.width() > 0:
                    return px, None, False
            return None, f"{name}: command succeeded but no screenshot was created", False
        return None, f"{name}: command exited with code {r.returncode}" + (f" ({err})" if err else ""), False
    except FileNotFoundError:
        return None, f"{name}: command not found", False
    except subprocess.TimeoutExpired:
        return None, f"{name}: timed out", False


def _grab_with_grim_slurp(tmp, timeout=None):
    if timeout is None:
        timeout = _selection_timeout()
    slurp_cmd = _slurp_command()
    if not slurp_cmd:
        return None, "slurp: command not found", False
    grim = _which("grim")
    if not grim:
        return None, "grim: command not found", False
    try:
        selection = _run_command(slurp_cmd, timeout=timeout, text=True)
    except FileNotFoundError:
        return None, "slurp: command not found", False
    except subprocess.TimeoutExpired:
        return None, "slurp: area selection timed out", False

    geometry = selection.stdout.strip()
    if selection.returncode != 0 or not geometry:
        err = (selection.stderr or "").strip()
        cancelled = not err or "cancel" in err.lower() or "отмен" in err.lower()
        if cancelled:
            return None, "slurp: area selection cancelled" + (f" ({err})" if err else ""), True
        return None, "slurp: area selection failed" + (f" ({err})" if err else ""), False

    return _grab_with_tool([grim, "-g", geometry, tmp], tmp, "grim", timeout=15)


def grab_area_native():
    if not is_wayland() and not os.environ.get("DISPLAY"):
        return None, "no active graphical session", False

    errors = []
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
        tmp = f.name
    try:
        if is_wayland():
            if is_kde():
                return None, None, False

            if is_hyprland() or is_niri():
                px, err, cancelled = _grab_with_grim_slurp(tmp)
                if px: return px, None, False
                if cancelled: return None, err, True
                errors.append(err)

                px, err, cancelled = _grab_with_portal(timeout=15, interactive=True)
                if px: return px, None, False
                if cancelled: return None, err, True
                errors.append(err)
                return None, "\n".join(e for e in errors if e), False

            if is_gnome():
                px, err, cancelled = _grab_with_tool(
                    ["gnome-screenshot", "-a", "-f", tmp],
                    tmp,
                    "gnome-screenshot",
                    timeout=60,
                    cancel_codes=(1,),
                )
                if px: return px, None, False
                if cancelled: return None, err, True
                errors.append(err)

            for backend in ("grim/slurp", "gnome-screenshot", "xdg-desktop-portal"):
                if backend == "grim/slurp" and not (is_hyprland() or is_niri()):
                    px, err, cancelled = _grab_with_grim_slurp(tmp)
                elif backend == "gnome-screenshot" and not is_gnome():
                    px, err, cancelled = _grab_with_tool(
                        ["gnome-screenshot", "-a", "-f", tmp],
                        tmp,
                        "gnome-screenshot",
                        timeout=60,
                        cancel_codes=(1,),
                    )
                elif backend == "xdg-desktop-portal":
                    px, err, cancelled = _grab_with_portal(timeout=60, interactive=True)
                else:
                    continue
                if px: return px, None, False
                if cancelled: return None, err, True
                errors.append(err)
        else:
            for cmd, name in (
                (["maim", "-s", tmp], "maim"),
                (["scrot", "-s", "--overwrite", tmp], "scrot"),
            ):
                px, err, cancelled = _grab_with_tool(cmd, tmp, name, timeout=60, cancel_codes=(1,))
                if px: return px, None, False
                if cancelled: return None, err, True
                errors.append(err)
    finally:
        try: os.unlink(tmp)
        except OSError: pass

    return None, "\n".join(e for e in errors if e), False


def _pixmap_from_uri(uri):
    path = unquote(urlparse(uri).path)
    if not path:
        return None
    px = QImage(path)
    if px.isNull() or px.width() <= 0:
        return None
    return px


def _grab_with_portal(timeout=30, interactive=False):
    token = f"linscreen_{os.getpid()}_{int(time.time() * 1000)}"
    options = "{'modal': <false>, 'interactive': <%s>, 'handle_token': <'%s'>}" % (
        "true" if interactive else "false",
        token,
    )
    try:
        call = _run_command(
            [
                "gdbus", "call", "--session",
                "--dest", "org.freedesktop.portal.Desktop",
                "--object-path", "/org/freedesktop/portal/desktop",
                "--method", "org.freedesktop.portal.Screenshot.Screenshot",
                "", options,
            ],
            text=True,
            timeout=10,
        )
    except FileNotFoundError:
        return None, "xdg-desktop-portal: gdbus not found", False
    except subprocess.TimeoutExpired:
        return None, "xdg-desktop-portal: could not send the screenshot request", False

    if call.returncode != 0:
        err = (call.stderr or call.stdout or "").strip()
        return None, "xdg-desktop-portal: call failed" + (f" ({err})" if err else ""), False

    match = re.search(r"['\"](?P<handle>/org/[^'\"]+)['\"]", call.stdout)
    if not match:
        return None, "xdg-desktop-portal: could not get the request handle", False

    proc = subprocess.Popen(
        [
            "gdbus", "monitor", "--session",
            "--dest", "org.freedesktop.portal.Desktop",
            "--object-path", match.group("handle"),
        ],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        start_new_session=True,
    )
    _set_active_process(proc)
    output = ""
    deadline = time.monotonic() + timeout
    try:
        while time.monotonic() < deadline:
            ready, _, _ = select.select([proc.stdout, proc.stderr], [], [], 1)
            for stream in ready:
                line = stream.readline()
                if not line:
                    continue
                output += line
                response = re.search(r"Response\s*\(uint32\s+(\d+),", output)
                uri = re.search(r"'uri': <'([^']+)'>", output)
                if response:
                    code = int(response.group(1))
                    if code == 0 and uri:
                        px = _pixmap_from_uri(uri.group(1))
                        if px:
                            return px, None, False
                        return None, "xdg-desktop-portal: screenshot taken but the file could not be opened", False
                    if code == 1:
                        return None, "xdg-desktop-portal: capture cancelled by the user", True
                    if code == 2:
                        return None, "xdg-desktop-portal: screen capture failed", False
            if proc.poll() is not None:
                break
    finally:
        _clear_active_process(proc)
        _terminate_process(proc)

    return None, "xdg-desktop-portal: no capture result received", False


def grab_all_screens():
    combined   = QRect()
    errors     = []
    for s in QGuiApplication.screens():
        combined = combined.united(s.geometry())
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
        tmp = f.name
    try:
        if is_wayland():
            if is_kde():
                # On KDE, spectacle -b is often faster and more "silent" than the portal.
                px, err, _ = _grab_with_tool(
                    ["spectacle", "-b", "-n", "-f", "-o", tmp], tmp, "spectacle", timeout=15)
                if px: return px, QRect(0,0,px.width(),px.height()), None
                errors.append(err)
                
                # Fallback to portal if spectacle fails or is missing
                px, err, cancelled = _grab_with_portal(timeout=10)
                if px: return px, QRect(0,0,px.width(),px.height()), None
                if cancelled: return None, combined, None
                errors.append(err)
            elif is_gnome():
                px, err, _ = _grab_with_tool(["gnome-screenshot","-f",tmp], tmp, "gnome-screenshot", timeout=20)
                if px: return px, combined, None
                errors.append(err)
                px, err, cancelled = _grab_with_portal(timeout=30)
                if px: return px, QRect(0,0,px.width(),px.height()), None
                if cancelled: return None, combined, None
                errors.append(err)
            else:
                # Fast Wayland capture with grim and PPM
                px, err, _ = _grab_with_tool(["grim", "-t", "ppm", "-"], None, "grim", timeout=10)
                if px: return px, QRect(0,0,px.width(),px.height()), None
                errors.append(err)
                px, err, _ = _grab_with_portal()
                if px: return px, QRect(0,0,px.width(),px.height()), None
                errors.append(err)
                px, err, _ = _grab_with_tool(["gnome-screenshot","-f",tmp], tmp, "gnome-screenshot", timeout=20)
                if px: return px, combined, None
                errors.append(err)
            return None, combined, "\n".join(e for e in errors if e)
        # Fast X11 capture with maim and PPM
        for cmd_info in [
            (["maim", "-f", "ppm"], None, "maim"),
            (["scrot", "--overwrite", tmp], tmp, "scrot"),
        ]:
            px, err, _ = _grab_with_tool(cmd_info[0], cmd_info[1], cmd_info[2], timeout=20)
            if px: return px, combined, None
            errors.append(err)
    finally:
        try: os.unlink(tmp)
        except OSError: pass
    # X11-only fallback via Qt
    result = QPixmap(combined.size()); result.fill(QColor(0,0,0))
    p = QPainter(result)
    for s in QGuiApplication.screens():
        p.drawPixmap(s.geometry().translated(-combined.topLeft()).topLeft(), s.grabWindow(0))
    p.end()
    return result, combined, None


def _mix(a, b, t):
    return QColor.fromRgbF(
        a.redF() + (b.redF() - a.redF()) * t,
        a.greenF() + (b.greenF() - a.greenF()) * t,
        a.blueF() + (b.blueF() - a.blueF()) * t,
    )


def ui_tokens():
    """Color tokens derived from the system palette, so the UI follows light/dark themes."""
    pal = QApplication.palette()
    bg = pal.color(QPalette.ColorRole.Window)
    fg = pal.color(QPalette.ColorRole.WindowText)
    dark = bg.lightness() < 128
    white = QColor("#ffffff")
    accent = QColor(pal.color(QPalette.ColorRole.Highlight))
    # Some themes ship a grey highlight; fall back to a brand blue in that case.
    if not accent.isValid() or accent.hsvSaturation() < 70:
        accent = QColor("#3b82f6")
    return {
        "dark": dark,
        "bg": bg,
        "fg": fg,
        "card": _mix(bg, fg, 0.05) if dark else _mix(bg, white, 0.75),
        "border": _mix(bg, fg, 0.14 if dark else 0.12),
        "muted": _mix(fg, bg, 0.40),
        "code": _mix(bg, fg, 0.09) if dark else _mix(bg, fg, 0.045),
        "control": _mix(bg, fg, 0.10) if dark else white,
        "control_hover": _mix(bg, fg, 0.16) if dark else _mix(white, fg, 0.05),
        "accent": accent,
        "accent_hover": accent.lighter(112) if dark else accent.darker(110),
        "accent_fg": white if accent.lightnessF() < 0.66 else QColor("#111111"),
        "ok": QColor("#4ade80" if dark else "#15803d"),
        "error": QColor("#f87171" if dark else "#b91c1c"),
        "info": accent.lighter(135) if dark else accent.darker(115),
    }


def _settings_stylesheet(t, base_pt, mono_family):
    c = {k: v.name() for k, v in t.items() if isinstance(v, QColor)}
    pt = lambda k: f"{base_pt * k:.1f}pt"
    return f"""
    QWidget#settingsRoot {{ background: {c['bg']}; }}
    QScrollArea#scroll, QWidget#content {{ background: transparent; border: none; }}
    QLabel {{ color: {c['fg']}; background: transparent; }}
    QLabel#title {{ font-size: {pt(1.6)}; font-weight: 700; }}
    QLabel#subtitle, QLabel#muted, QLabel#footer {{ color: {c['muted']}; font-size: {pt(0.92)}; }}
    QLabel#footer a {{ color: {c['muted']}; }}
    QLabel#badge {{
        color: {c['muted']}; border: 1px solid {c['border']}; border-radius: 10px;
        padding: 2px 9px; font-size: {pt(0.85)};
    }}
    QLabel#sectionTitle {{ font-size: {pt(1.05)}; font-weight: 600; }}
    QLabel#kbd {{
        background: {c['control']}; border: 1px solid {c['border']}; border-bottom-width: 2px;
        border-radius: 6px; padding: 2px 8px; font-weight: 600;
    }}
    QLabel#status {{ font-size: {pt(0.92)}; color: {c['muted']}; }}
    QLabel#status[tone="ok"] {{ color: {c['ok']}; }}
    QLabel#status[tone="error"] {{ color: {c['error']}; }}
    QLabel#status[tone="info"] {{ color: {c['info']}; }}
    QFrame#card {{ background: {c['card']}; border: 1px solid {c['border']}; border-radius: 12px; }}
    QFrame#code {{ background: {c['code']}; border: 1px solid {c['border']}; border-radius: 8px; }}
    QLabel#codeText {{ font-family: "{mono_family}"; font-size: {pt(0.95)}; }}
    QPushButton {{
        background: {c['control']}; color: {c['fg']}; border: 1px solid {c['border']};
        border-radius: 8px; padding: 6px 14px;
    }}
    QPushButton:hover {{ background: {c['control_hover']}; }}
    QPushButton:pressed {{ background: {c['border']}; }}
    QPushButton:focus {{ border-color: {c['accent']}; }}
    QPushButton#primary {{
        background: {c['accent']}; color: {c['accent_fg']}; border: none; border-radius: 10px;
        padding: 11px 20px; font-size: {pt(1.08)}; font-weight: 600;
    }}
    QPushButton#primary:hover {{ background: {c['accent_hover']}; }}
    QPushButton#ghost {{
        background: transparent; border: none; color: {c['muted']}; padding: 4px 8px;
    }}
    QPushButton#ghost:hover {{ color: {c['fg']}; background: {c['control_hover']}; }}
    QKeySequenceEdit QLineEdit {{
        background: {c['control']}; color: {c['fg']}; border: 1px solid {c['border']};
        border-radius: 8px; padding: 6px 10px; selection-background-color: {c['accent']};
    }}
    QKeySequenceEdit QLineEdit:focus {{ border: 1px solid {c['accent']}; }}
    QScrollBar:vertical {{ background: transparent; width: 8px; margin: 2px; }}
    QScrollBar::handle:vertical {{ background: {c['border']}; border-radius: 3px; min-height: 28px; }}
    QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
    QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{ background: none; }}
    """


def environment_label():
    if is_hyprland():
        de = "Hyprland"
    elif is_niri():
        de = "Niri"
    elif is_sway():
        de = "Sway"
    elif is_kde():
        de = "KDE Plasma"
    elif is_gnome():
        de = "GNOME"
    else:
        de = current_desktop().split(":")[0].title() or "Linux"
    return f"{de} · {'Wayland' if is_wayland() else 'X11'}"


def _shortcut_parts(portable):
    parts = [p for p in (portable or "Print").split("+") if p]
    return parts[:-1], (parts[-1] if parts else "Print")


class ToggleSwitch(QCheckBox):
    """Animated on/off switch that replaces the plain checkbox."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._pos = 0.0
        self.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self.setFixedSize(44, 24)
        self._anim = QPropertyAnimation(self, b"knob", self)
        self._anim.setDuration(150)
        self._anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        self.toggled.connect(self._animate)

    def _get_knob(self):
        return self._pos

    def _set_knob(self, value):
        self._pos = value
        self.update()

    knob = pyqtProperty(float, _get_knob, _set_knob)

    def _animate(self, on):
        self._anim.stop()
        if not self.isVisible():
            self._set_knob(1.0 if on else 0.0)
            return
        self._anim.setStartValue(self._pos)
        self._anim.setEndValue(1.0 if on else 0.0)
        self._anim.start()

    def hitButton(self, pos):
        return self.rect().contains(pos)

    def paintEvent(self, _):
        t = ui_tokens()
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(self.rect()).adjusted(1, 1, -1, -1)
        p.setPen(QPen(t["accent"], 1.5) if self.hasFocus() else Qt.PenStyle.NoPen)
        p.setBrush(_mix(t["border"], t["accent"], self._pos))
        p.drawRoundedRect(r, r.height() / 2, r.height() / 2)
        d = r.height() - 6
        x = r.left() + 3 + (r.width() - 6 - d) * self._pos
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor("#ffffff"))
        p.drawEllipse(QRectF(x, r.top() + 3, d, d))


class ElidedLabel(QLabel):
    """Single-line label that elides long paths in the middle instead of widening the window."""

    def __init__(self, text, parent=None):
        super().__init__(parent)
        self._full = text
        self.setToolTip(text)
        self.setMinimumWidth(60)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self.setText(self.fontMetrics().elidedText(self._full, Qt.TextElideMode.ElideMiddle, self.width()))


def _copy_icon(color):
    px = QPixmap(32, 32)
    px.fill(Qt.GlobalColor.transparent)
    p = QPainter(px)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setPen(QPen(color, 2.4))
    p.setBrush(Qt.BrushStyle.NoBrush)
    p.drawRoundedRect(QRectF(5, 9, 15, 18), 3, 3)
    p.drawRoundedRect(QRectF(12, 4, 15, 18), 3, 3)
    p.end()
    return QIcon(px)


class CodeBlock(QFrame):
    """Monospace snippet with an inline copy button."""

    def __init__(self, text="", parent=None):
        super().__init__(parent)
        self._text = text
        self.setObjectName("code")
        lay = QHBoxLayout(self)
        lay.setContentsMargins(12, 6, 6, 6)
        lay.setSpacing(8)
        self._label = QLabel(self._display(text))
        self._label.setObjectName("codeText")
        self._label.setWordWrap(True)
        self._label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self._label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        lay.addWidget(self._label, 1)
        self._btn = QPushButton()
        self._btn.setObjectName("ghost")
        self._btn.setToolTip("Copy")
        self._btn.setFixedSize(30, 30)
        self._btn.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self._btn.clicked.connect(self._copy)
        lay.addWidget(self._btn, 0, Qt.AlignmentFlag.AlignVCenter)

    @staticmethod
    def _display(text):
        # A word joiner after each hyphen stops "--screenshot" from being split across lines.
        return text.replace("-", "-\u2060")

    def text(self):
        return self._text

    def setText(self, text):
        self._text = text
        self._label.setText(self._display(text))

    def refresh_icon(self):
        self._btn.setIcon(_copy_icon(ui_tokens()["muted"]))

    def _copy(self):
        QGuiApplication.clipboard().setText(self.text())
        self._btn.setIcon(QIcon())
        self._btn.setText("✓")
        QTimer.singleShot(1400, lambda: (self._btn.setText(""), self.refresh_icon()))


class ScreenshotOverlay(QWidget):
    captured  = pyqtSignal(QPixmap)
    cancelled = pyqtSignal()
    def __init__(self, screenshot, global_geom, screen):
        super().__init__()
        self._screenshot = screenshot
        self._global_geom = global_geom
        self._screen = screen
        self._screen_geom = screen.geometry()
        self._offset = self._screen_geom.topLeft() - self._global_geom.topLeft()
        self._accent = ui_tokens()["accent"]

        self._start = self._end = QPoint()
        self._dragging = False

        self.setWindowFlags(Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint | Qt.WindowType.Tool)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.setCursor(QCursor(Qt.CursorShape.CrossCursor))
        self.setMouseTracking(True)

        # Ensure the widget is placed on the correct screen immediately
        self.setGeometry(self._screen_geom)
        self.create()  # Force creation of window handle
        if self.windowHandle():
            self.windowHandle().setScreen(self._screen)

    def _sel(self): return QRect(self._start, self._end).normalized()

    def _pill(self, p, text, anchor, align_center=False):
        f = QFont(self.font())
        f.setPointSizeF(max(9.0, f.pointSizeF()))
        f.setWeight(QFont.Weight.DemiBold)
        p.setFont(f)
        fm = QFontMetrics(f)
        w, h = fm.horizontalAdvance(text) + 20, fm.height() + 10
        x = anchor.x() - w / 2 if align_center else anchor.x()
        x = max(8, min(x, self.width() - w - 8))
        y = max(8, min(anchor.y(), self.height() - h - 8))
        r = QRectF(x, y, w, h)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(15, 17, 22, 215))
        p.drawRoundedRect(r, h / 2, h / 2)
        p.setPen(QColor(255, 255, 255))
        p.drawText(r, Qt.AlignmentFlag.AlignCenter, text)
        return r

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        # Draw the part of the global screenshot that corresponds to this screen
        p.drawPixmap(0, 0, self._screenshot, self._offset.x(), self._offset.y(), self._screen_geom.width(), self._screen_geom.height())
        p.fillRect(self.rect(), QColor(8, 10, 14, 120))

        sel = self._sel()
        if self._dragging and self._start != self._end:
            # Draw the un-dimmed portion from the global screenshot
            p.drawPixmap(sel, self._screenshot, sel.translated(self._offset))
            p.setPen(QPen(self._accent, 2))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawRect(QRectF(sel).adjusted(-1, -1, 1, 1))
            p.setPen(QPen(QColor(255, 255, 255), 1.5))
            p.setBrush(self._accent)
            for corner in (sel.topLeft(), sel.topRight(), sel.bottomLeft(), sel.bottomRight()):
                p.drawEllipse(QPointF(corner), 4.5, 4.5)
            label = f"{sel.width()} × {sel.height()}"
            below = QPoint(sel.left(), sel.bottom() + 10)
            if below.y() + 40 > self.height():
                below = QPoint(sel.left(), sel.top() - 40)
            self._pill(p, label, below)
        elif self.underMouse() or self.rect().contains(self.mapFromGlobal(QCursor.pos())):
            pos = self.mapFromGlobal(QCursor.pos())
            p.setPen(QPen(QColor(255, 255, 255, 70), 1, Qt.PenStyle.DashLine))
            p.drawLine(0, pos.y(), self.width(), pos.y())
            p.drawLine(pos.x(), 0, pos.x(), self.height())

        if not self._dragging:
            self._pill(p, "Select an area  ·  Esc to cancel", QPoint(self.width() // 2, 28), align_center=True)

    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            self._start = self._end = e.pos()
            self._dragging = True
            self.update()

    def mouseMoveEvent(self, e):
        if self._dragging:
            self._end = e.pos()
        self.update()

    def mouseReleaseEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton and self._dragging:
            self._dragging = False
            self._end = e.pos()
            sel = self._sel()
            if sel.width() > 4 and sel.height() > 4:
                # Capture the area from the global screenshot
                global_sel = sel.translated(self._offset)
                self.captured.emit(self._screenshot.copy(global_sel))
            else:
                self.cancelled.emit()
            self.close()

    def keyPressEvent(self, e):
        if e.key() == Qt.Key.Key_Escape:
            self.cancelled.emit()
            self.close()


class SettingsWindow(QWidget):
    NARROW_WIDTH = 520

    def __init__(self, app):
        super().__init__()
        self._app = app
        self._config = app.config
        self._flow_rows = []
        self._binds = bind_backend()
        self.setObjectName("settingsRoot")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setWindowTitle("LinScreen — Settings")
        self.setWindowIcon(LinScreenApp._make_icon())
        self.setMinimumSize(380, 360)
        self.setWindowFlags(Qt.WindowType.Window | Qt.WindowType.WindowCloseButtonHint)
        self._build()
        self._apply_theme()
        hints = QGuiApplication.styleHints()
        if hasattr(hints, "colorSchemeChanged"):
            hints.colorSchemeChanged.connect(lambda *_: QTimer.singleShot(0, self._apply_theme))

    # ---- layout helpers -------------------------------------------------

    def _card(self, title=None, subtitle=None):
        card = QFrame()
        card.setObjectName("card")
        lay = QVBoxLayout(card)
        lay.setContentsMargins(18, 16, 18, 16)
        lay.setSpacing(10)
        if title:
            lbl = QLabel(title)
            lbl.setObjectName("sectionTitle")
            lay.addWidget(lbl)
        if subtitle:
            sub = QLabel(subtitle)
            sub.setObjectName("muted")
            sub.setWordWrap(True)
            lay.addWidget(sub)
        return card, lay

    def _flow_row(self, spacing=10):
        """Horizontal row that stacks vertically when the window gets narrow."""
        row = QBoxLayout(QBoxLayout.Direction.LeftToRight)
        row.setSpacing(spacing)
        self._flow_rows.append(row)
        return row

    def _build(self):
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea()
        scroll.setObjectName("scroll")
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        content = QWidget()
        content.setObjectName("content")
        scroll.setWidget(content)
        outer.addWidget(scroll)

        root = QVBoxLayout(content)
        root.setSpacing(14)
        root.setContentsMargins(22, 20, 22, 18)

        # Header
        header = self._flow_row(12)
        brand = QHBoxLayout()
        brand.setSpacing(12)
        icon = QLabel()
        icon.setPixmap(LinScreenApp._make_icon().pixmap(44, 44))
        brand.addWidget(icon, 0, Qt.AlignmentFlag.AlignTop)
        titles = QVBoxLayout()
        titles.setSpacing(0)
        title = QLabel("LinScreen")
        title.setObjectName("title")
        titles.addWidget(title)
        subtitle = QLabel("Area screenshots in one click")
        subtitle.setObjectName("subtitle")
        titles.addWidget(subtitle)
        brand.addLayout(titles, 1)
        header.addLayout(brand, 1)
        self._badges = QBoxLayout(QBoxLayout.Direction.TopToBottom)
        self._badges.setSpacing(6)
        for text in (f"v{APP_VERSION}", environment_label()):
            b = QLabel(text)
            b.setObjectName("badge")
            self._badges.addWidget(b, 0, Qt.AlignmentFlag.AlignRight)
        self._badges.addStretch()
        header.addLayout(self._badges)
        root.addLayout(header)

        # Hero: capture action
        hero, hero_lay = self._card()
        hero_row = self._flow_row(14)
        btn = QPushButton("Take screenshot")
        btn.setObjectName("primary")
        btn.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        btn.setMinimumWidth(200)
        btn.clicked.connect(self._app.take_screenshot)
        hero_row.addWidget(btn, 1)
        hint_row = QHBoxLayout()
        hint_row.setSpacing(6)
        or_lbl = QLabel("or press")
        or_lbl.setObjectName("muted")
        hint_row.addWidget(or_lbl)
        self._kbd_lbl = QLabel()
        self._kbd_lbl.setObjectName("kbd")
        hint_row.addWidget(self._kbd_lbl)
        hint_row.addStretch()
        hero_row.addLayout(hint_row)
        hero_lay.addLayout(hero_row)
        root.addWidget(hero)

        # Hotkey
        hk_card, hk = self._card(
            "Hotkey",
            "Click the field and press the shortcut you want.",
        )
        hk_row = QHBoxLayout()
        hk_row.setSpacing(8)
        self._shortcut_edit = QKeySequenceEdit(self)
        if hasattr(self._shortcut_edit, "setMaximumSequenceLength"):
            self._shortcut_edit.setMaximumSequenceLength(1)
        current_shortcut = self._config["shortcut"] or DEFAULT_SHORTCUT
        if self._binds:
            # The compositor config is the source of truth: it survives reinstalls.
            found = self._binds.find()
            if found:
                current_shortcut = found
                self._config["shortcut"] = found
        self._shortcut_edit.setKeySequence(QKeySequence(current_shortcut))
        self._shortcut_edit.setMinimumHeight(36)
        self._shortcut_edit.keySequenceChanged.connect(lambda _seq: self._refresh_shortcut_views())
        self._shortcut_edit.editingFinished.connect(self._commit_shortcut)
        hk_row.addWidget(self._shortcut_edit, 1)
        self._reset_btn = QPushButton("Reset")
        self._reset_btn.setMinimumHeight(36)
        self._reset_btn.clicked.connect(self._reset_shortcut)
        hk_row.addWidget(self._reset_btn)
        hk.addLayout(hk_row)
        self._status_lbl = QLabel()
        self._status_lbl.setObjectName("status")
        self._status_lbl.setWordWrap(True)
        hk.addWidget(self._status_lbl)
        root.addWidget(hk_card)

        # System integration
        integ_card, integ = self._card("System binding")
        self._hint_lbl = QLabel()
        self._hint_lbl.setObjectName("muted")
        self._hint_lbl.setWordWrap(True)
        integ.addWidget(self._hint_lbl)
        self._snippet = CodeBlock()
        integ.addWidget(self._snippet)
        self._bind_btn = QPushButton("Write to config")
        self._bind_btn.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self._bind_btn.clicked.connect(self._commit_shortcut)
        self._bind_btn.setVisible(False)
        integ.addWidget(self._bind_btn, 0, Qt.AlignmentFlag.AlignLeft)
        cmd_lbl = QLabel("Command for scripts")
        cmd_lbl.setObjectName("muted")
        integ.addWidget(cmd_lbl)
        self._cmd_block = CodeBlock(self._get_command_text())
        integ.addWidget(self._cmd_block)
        if self._shortcut_settings_command():
            kbtn = QPushButton(self._shortcut_button_text())
            kbtn.clicked.connect(self._open_shortcut_settings)
            integ.addWidget(kbtn, 0, Qt.AlignmentFlag.AlignLeft)
        root.addWidget(integ_card)

        # General
        gen_card, gen = self._card("General")
        auto_row = QHBoxLayout()
        auto_text = QVBoxLayout()
        auto_text.setSpacing(2)
        auto_text.addWidget(QLabel("Start on login"))
        auto_sub = QLabel("LinScreen will wait in the tray")
        auto_sub.setObjectName("muted")
        auto_text.addWidget(auto_sub)
        auto_row.addLayout(auto_text, 1)
        self._auto_cb = ToggleSwitch()
        self._auto_cb.setChecked(bool(self._config["autostart"]))
        self._auto_cb.toggled.connect(self._toggle_auto)
        auto_row.addWidget(self._auto_cb, 0, Qt.AlignmentFlag.AlignVCenter)
        gen.addLayout(auto_row)

        sep = QFrame()
        sep.setObjectName("code")
        sep.setFixedHeight(1)
        gen.addWidget(sep)

        folder_row = self._flow_row(10)
        folder_text = QVBoxLayout()
        folder_text.setSpacing(2)
        folder_text.addWidget(QLabel("Screenshots folder"))
        home = str(Path.home())
        shown = str(SCREENSHOTS_DIR)
        if shown.startswith(home + os.sep):
            shown = "~" + shown[len(home):]
        path_lbl = ElidedLabel(shown)
        path_lbl.setObjectName("muted")
        folder_text.addWidget(path_lbl)
        folder_row.addLayout(folder_text, 1)
        open_folder_btn = QPushButton("Open folder")
        open_folder_btn.clicked.connect(self._open_screenshots_folder)
        folder_row.addWidget(open_folder_btn, 0, Qt.AlignmentFlag.AlignVCenter)
        gen.addLayout(folder_row)
        root.addWidget(gen_card)

        root.addStretch()
        footer = QLabel(f'LinScreen {APP_VERSION} · <a href="{APP_URL}">GitHub</a> · MIT')
        footer.setObjectName("footer")
        footer.setOpenExternalLinks(True)
        footer.setAlignment(Qt.AlignmentFlag.AlignCenter)
        root.addWidget(footer)

        self._refresh_shortcut_views()
        self._update_bind_status()

        self._content = content
        self._fitted = False
        self.resize(560, 640)

    def showEvent(self, e):
        super().showEvent(e)
        if not self._fitted:
            self._fitted = True
            QTimer.singleShot(0, self._fit_height)

    def _fit_height(self):
        """Grow the window to show all content without scrolling, within the screen."""
        lay = self._content.layout()
        h = lay.heightForWidth(self._content.width()) if lay.hasHeightForWidth() else lay.sizeHint().height()
        screen = self.screen() or QGuiApplication.primaryScreen()
        max_h = int(screen.availableGeometry().height() * 0.9) if screen else 820
        self.resize(self.width(), min(h + 2, max_h))

    def _apply_theme(self):
        base = QApplication.font().pointSizeF()
        if base <= 0:
            base = 10.0
        mono = QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont).family()
        self.setStyleSheet(_settings_stylesheet(ui_tokens(), base, mono))
        self._auto_cb.update()
        for block in self.findChildren(CodeBlock):
            block.refresh_icon()

    def resizeEvent(self, e):
        super().resizeEvent(e)
        direction = (
            QBoxLayout.Direction.TopToBottom
            if self.width() < self.NARROW_WIDTH else QBoxLayout.Direction.LeftToRight
        )
        for row in self._flow_rows:
            if row.direction() != direction:
                row.setDirection(direction)
        narrow = direction == QBoxLayout.Direction.TopToBottom
        self._badges.setDirection(QBoxLayout.Direction.LeftToRight if narrow else QBoxLayout.Direction.TopToBottom)
        align = Qt.AlignmentFlag.AlignLeft if narrow else Qt.AlignmentFlag.AlignRight
        for i in range(self._badges.count()):
            if self._badges.itemAt(i).widget():
                self._badges.setAlignment(self._badges.itemAt(i).widget(), align)

    # ---- behaviour ------------------------------------------------------

    def _set_status(self, text, tone):
        self._status_lbl.setText(text)
        self._status_lbl.setProperty("tone", tone)
        self._status_lbl.style().unpolish(self._status_lbl)
        self._status_lbl.style().polish(self._status_lbl)

    def _refresh_shortcut_views(self):
        seq = self._shortcut_edit.keySequence()
        native = seq.toString(QKeySequence.SequenceFormat.NativeText) or "Print"
        self._kbd_lbl.setText(native)
        text, snippet = self._shortcut_hint()
        self._hint_lbl.setText(text)
        self._snippet.setVisible(bool(snippet))
        self._snippet.setText(snippet or "")

    def _get_command_text(self):
        return "linscreen --screenshot"

    def _open_screenshots_folder(self):
        SCREENSHOTS_DIR.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(SCREENSHOTS_DIR)))

    def _commit_shortcut(self):
        sequence = self._shortcut_edit.keySequence()
        seq_str = sequence.toString(QKeySequence.SequenceFormat.PortableText)
        if not seq_str:
            return
        self._config["shortcut"] = seq_str
        self._refresh_shortcut_views()
        if self._binds:
            ok, message = self._binds.apply(seq_str)
            self._set_status(f"✓ {message}" if ok else message, "ok" if ok else "error")
            self._bind_btn.setVisible(not ok)
        else:
            self._set_status("✓ Saved. Update the system binding as shown below.", "info")

    def _update_bind_status(self):
        if not self._binds:
            self._set_status("The shortcut is filled into the hint below. Bind it in your system settings.", "")
            return
        found = self._binds.find()
        current = QKeySequence(self._config["shortcut"] or DEFAULT_SHORTCUT)
        bound = bool(found) and QKeySequence(found) == current
        if bound:
            self._set_status(f"✓ Bound in {self._binds.display_path}", "ok")
        else:
            self._set_status(
                f"The bind is not in {self._binds.display_path} yet. "
                "Press \"Write to config\" or choose a shortcut.",
                "info",
            )
        self._bind_btn.setVisible(not bound)

    def _reset_shortcut(self):
        self._shortcut_edit.setKeySequence(QKeySequence(DEFAULT_SHORTCUT))
        self._commit_shortcut()

    def _shortcut_hint(self):
        """Return (instructions, config snippet or None) for the current desktop."""
        key_seq = self._shortcut_edit.keySequence()
        port_key = key_seq.toString(QKeySequence.SequenceFormat.PortableText) or "Print"
        nat_key = key_seq.toString(QKeySequence.SequenceFormat.NativeText) or port_key
        mods, key = _shortcut_parts(port_key)
        cmd = "linscreen --screenshot"
        if self._binds:
            return (
                f"LinScreen writes the bind into {self._binds.display_path} "
                "and updates it when the shortcut changes. The config will contain:",
                self._binds.snippet(port_key),
            )
        if is_hyprland():
            hypr_mods = " ".join({"Meta": "SUPER"}.get(m, m.upper()) for m in mods)
            return (
                "Add this line to ~/.config/hypr/hyprland.conf (or bindings.conf on Omarchy):",
                f"bind = {hypr_mods}, {key}, exec, {cmd}",
            )
        if is_niri():
            niri_mods = [{"Meta": "Super"}.get(m, m) for m in mods]
            return (
                "Add this to the binds block of ~/.config/niri/config.kdl:",
                f'{"+".join(niri_mods + [key])} {{ spawn "linscreen" "--screenshot"; }}',
            )
        if is_sway():
            sway_mods = [{"Meta": "Mod4", "Alt": "Mod1", "Ctrl": "Control"}.get(m, m) for m in mods]
            return (
                "Add this line to ~/.config/sway/config (for i3: ~/.config/i3/config):",
                f"bindsym {'+'.join(sway_mods + [key])} exec {cmd}",
            )
        if is_kde():
            return (
                f"System Settings → Shortcuts → Add New → Command. "
                f"Assign {nat_key} to the command below.",
                None,
            )
        if is_gnome():
            return (
                f"Settings → Keyboard → Keyboard Shortcuts → Custom Shortcuts (+). "
                f"Name: LinScreen, shortcut: {nat_key}, command below.",
                None,
            )
        return (
            f"Assign {nat_key} to the command below in your system's keyboard shortcut settings.",
            None,
        )

    def _shortcut_settings_command(self):
        if is_kde() and shutil.which("systemsettings"):
            return ["systemsettings", "kcm_keys"]
        if is_gnome() and shutil.which("gnome-control-center"):
            return ["gnome-control-center", "keyboard"]
        return None

    def _shortcut_button_text(self):
        if is_kde():
            return "Open KDE shortcut settings"
        if is_gnome():
            return "Open GNOME shortcut settings"
        return "Open system shortcut settings"

    def _open_shortcut_settings(self):
        cmd = self._shortcut_settings_command()
        if not cmd:
            QMessageBox.warning(self, "LinScreen", "Could not find the system settings tool.")
            return
        subprocess.Popen(cmd, start_new_session=True)

    def _toggle_auto(self, en):
        self._config["autostart"] = en
        self._app.set_autostart(en)

    def closeEvent(self, e):
        e.ignore()
        self.hide()


class CaptureWorker(QObject):
    finished = pyqtSignal(object, object, bool)

    def __init__(self, capture_func):
        super().__init__()
        self._capture_func = capture_func

    def run(self):
        try:
            shot, error, cancelled = self._capture_func()
        except Exception as exc:
            shot, error, cancelled = None, f"internal capture error: {exc}", False
        self.finished.emit(shot, error, cancelled)


class LinScreenApp(QApplication):
    def __init__(self, argv, *, show_settings_on_start=False, take_screenshot_on_start=False):
        super().__init__(argv)
        self.setQuitOnLastWindowClosed(False)
        self.setApplicationName("LinScreen")
        self.config   = Config()
        self._overlays = []
        self._capture_thread = None
        self._capture_worker = None
        self._capture_proc = None
        self._capture_timeout = None
        self._capture_tmp = None
        SCREENSHOTS_DIR.mkdir(parents=True, exist_ok=True)
        self._setup_ipc()
        self._setup_tray()
        self._settings = SettingsWindow(self)
        if show_settings_on_start or not QSystemTrayIcon.isSystemTrayAvailable():
            self._show_settings()
        if take_screenshot_on_start:
            QTimer.singleShot(0, self.take_screenshot)

    def _setup_ipc(self):
        QLocalServer.removeServer(SOCKET_NAME)
        self._server = QLocalServer(self)
        self._server.newConnection.connect(self._on_ipc)
        self._server.listen(SOCKET_NAME)

    def _on_ipc(self):
        conn = self._server.nextPendingConnection()
        if not conn: return
        conn.waitForReadyRead(100)
        msg = bytes(conn.readAll()).decode(errors="ignore").strip()
        conn.disconnectFromServer()
        if msg == "screenshot":
            self.take_screenshot()
        elif msg == "show-settings":
            self._show_settings()
        elif msg == "quit":
            self.quit_app()

    def _setup_tray(self):
        self._tray = QSystemTrayIcon(self._make_icon(), self)
        m = QMenu()
        m.addAction("Settings", self._show_settings)
        m.addAction("Take screenshot", self.take_screenshot)
        m.addSeparator(); m.addAction("Quit", self.quit_app)
        self._tray.setContextMenu(m)
        self._tray.activated.connect(lambda r: r == QSystemTrayIcon.ActivationReason.Trigger and self._show_settings())
        self._tray.setToolTip("LinScreen"); self._tray.show()

    @staticmethod
    def _make_icon():
        if QIcon.hasThemeIcon("linscreen"):
            return QIcon.fromTheme("linscreen")
        for p in (
            Path("/usr/share/pixmaps/linscreen.png"),
            Path("/usr/share/icons/hicolor/256x256/apps/linscreen.png"),
            _EXEC_DIR / "linscreen.png",
            _EXEC_DIR / "assets" / "linscreen.png",
        ):
            if p.exists():
                return QIcon(str(p))
        px = QPixmap(32, 32)
        px.fill(Qt.GlobalColor.transparent)
        p = QPainter(px)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(QColor(30, 120, 220)))
        p.drawRoundedRect(2, 8, 28, 20, 3, 3)
        p.setBrush(QBrush(QColor(180, 210, 255)))
        p.drawEllipse(10, 12, 12, 12)
        p.setBrush(QBrush(QColor(255, 255, 255)))
        p.drawEllipse(13, 15, 6, 6)
        p.setBrush(QBrush(QColor(30, 120, 220)))
        p.drawRoundedRect(11, 4, 10, 6, 2, 2)
        p.end()
        return QIcon(px)

    def _notify(self, title, message, icon_type=QSystemTrayIcon.MessageIcon.Information, timeout_ms=3000):
        notify_send = _which("notify-send")
        if is_wayland() and notify_send:
            urgency = "critical" if icon_type == QSystemTrayIcon.MessageIcon.Critical else "normal"
            try:
                subprocess.Popen([
                    notify_send,
                    "-a", "LinScreen",
                    "-i", "linscreen",
                    "-u", urgency,
                    "-t", str(timeout_ms),
                    title,
                    message,
                ], start_new_session=True)
                return
            except Exception:
                pass
        try:
            self._tray.showMessage(title, message, icon_type, timeout_ms)
        except Exception:
            pass

    def _show_settings(self):
        self._settings.show(); self._settings.raise_(); self._settings.activateWindow()

    def take_screenshot(self):
        if self._capture_thread is not None or self._capture_proc is not None:
            self._cancel_capture()
            return
        self._settings.hide()
        QTimer.singleShot(50, self._start_native_capture)

    def _cancel_capture(self):
        stopped = False
        if self._capture_proc is not None:
            stopped = True
            proc = self._capture_proc
            proc.kill()
            proc.deleteLater()
            self._capture_proc = None
            self._finish_async_capture()
        elif cancel_active_capture():
            stopped = True
        self._notify(
            "LinScreen",
            "Cancelling the current capture." if stopped else "The capture is already finishing.",
            QSystemTrayIcon.MessageIcon.Information,
            2000,
        )

    def _start_slurp_capture(self):
        self._notify(
            "LinScreen",
            "Select an area of the screen.",
            QSystemTrayIcon.MessageIcon.Information,
            2000,
        )
        tmp_file = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
        self._capture_tmp = tmp_file.name
        tmp_file.close()
        self._capture_proc = QProcess(self)
        slurp_cmd = _slurp_command() or ["slurp"]
        self._capture_proc.setProgram(slurp_cmd[0])
        self._capture_proc.setArguments(slurp_cmd[1:])
        self._capture_proc.finished.connect(self._on_slurp_done)
        self._capture_proc.errorOccurred.connect(lambda _: self._show_async_capture_error("slurp: command not found"))
        self._start_capture_timeout(_selection_timeout(), "slurp: area selection timed out")
        self._capture_proc.start()

    def _start_capture_timeout(self, seconds, message):
        if self._capture_timeout:
            self._capture_timeout.stop()
            self._capture_timeout.deleteLater()
        self._capture_timeout = QTimer(self)
        self._capture_timeout.setSingleShot(True)
        self._capture_timeout.timeout.connect(lambda: self._show_async_capture_error(message))
        self._capture_timeout.start(seconds * 1000)

    def _on_slurp_done(self, exit_code, _exit_status):
        if self._capture_proc is None:
            return
        proc = self._capture_proc
        geometry = bytes(proc.readAllStandardOutput()).decode(errors="ignore").strip()
        err = bytes(proc.readAllStandardError()).decode(errors="ignore").strip()
        proc.deleteLater()
        self._capture_proc = None
        if exit_code != 0 or not geometry:
            self._finish_async_capture()
            if err:
                self._show_capture_error(f"slurp: area selection failed ({err})", None)
            else:
                self._notify(
                    "LinScreen",
                    "Area selection cancelled.",
                    QSystemTrayIcon.MessageIcon.Information,
                    2000,
                )
            return
        self._start_grim_capture(geometry)

    def _start_grim_capture(self, geometry):
        self._capture_proc = QProcess(self)
        self._capture_proc.setProgram(_which("grim") or "grim")
        self._capture_proc.setArguments(["-g", geometry, self._capture_tmp])
        self._capture_proc.finished.connect(self._on_grim_done)
        self._capture_proc.errorOccurred.connect(lambda _: self._show_async_capture_error("grim: command not found"))
        self._start_capture_timeout(15, "grim: timed out")
        self._capture_proc.start()

    def _on_grim_done(self, exit_code, _exit_status):
        if self._capture_proc is None:
            return
        proc = self._capture_proc
        err = bytes(proc.readAllStandardError()).decode(errors="ignore").strip()
        proc.deleteLater()
        self._capture_proc = None
        path = self._capture_tmp
        self._finish_async_capture(unlink_tmp=False)
        if exit_code == 0:
            image = QImage(path)
            try: os.unlink(path)
            except OSError: pass
            if not image.isNull() and image.width() > 0:
                self._save(image)
                return
            self._show_capture_error("grim: command succeeded but no screenshot was created", None)
            return
        try: os.unlink(path)
        except OSError: pass
        self._show_capture_error(f"grim: command exited with code {exit_code}" + (f" ({err})" if err else ""), None)

    def _show_async_capture_error(self, message):
        if self._capture_proc is not None:
            self._capture_proc.kill()
            self._capture_proc.deleteLater()
            self._capture_proc = None
        self._finish_async_capture()
        self._show_capture_error(message, None)

    def _finish_async_capture(self, unlink_tmp=True):
        if self._capture_timeout:
            self._capture_timeout.stop()
            self._capture_timeout.deleteLater()
            self._capture_timeout = None
        if unlink_tmp and self._capture_tmp:
            try: os.unlink(self._capture_tmp)
            except OSError: pass
        self._capture_tmp = None

    def _start_native_capture(self):
        self._capture_thread = QThread(self)
        self._capture_worker = CaptureWorker(grab_area_native)
        self._capture_worker.moveToThread(self._capture_thread)
        self._capture_thread.started.connect(self._capture_worker.run)
        self._capture_worker.finished.connect(self._on_native_capture_done)
        self._capture_worker.finished.connect(self._capture_thread.quit)
        self._capture_worker.finished.connect(self._capture_worker.deleteLater)
        self._capture_thread.finished.connect(self._capture_thread.deleteLater)
        self._capture_thread.finished.connect(self._clear_capture_worker)
        self._capture_thread.start()

    def _clear_capture_worker(self):
        self._capture_thread = None
        self._capture_worker = None

    def _on_native_capture_done(self, native_shot, native_error, native_cancelled):
        if native_cancelled:
            return
        if native_shot:
            self._save(native_shot)
            return

        if is_wayland() and (is_hyprland() or is_niri()):
            self._show_capture_error(native_error, None)
            return

        shot, geom, error = grab_all_screens()
        if shot is None:
            self._show_capture_error(native_error, error)
            return

        shot = _to_pixmap(shot)
        self._overlays = []
        for screen in QGuiApplication.screens():
            overlay = ScreenshotOverlay(shot, geom, screen)
            overlay.captured.connect(self._save)
            overlay.cancelled.connect(self._close_overlays)
            # Use only showFullScreen for snappier appearance
            overlay.showFullScreen()
            self._overlays.append(overlay)

    def _show_capture_error(self, native_error, error):
        self._settings.show()
        if is_wayland() and is_kde():
            message = (
                "Screen capture failed.\n"
                "KDE Wayland: make sure xdg-desktop-portal-kde is running,\n"
                "or install spectacle."
            )
        elif is_wayland() and (is_hyprland() or is_niri()):
            message = (
                "Screen capture failed.\n"
                "Hyprland/Niri: install grim and slurp, or set up xdg-desktop-portal."
            )
        elif is_wayland() and is_gnome():
            message = (
                "Screen capture failed.\n"
                "GNOME Wayland: install gnome-screenshot or set up xdg-desktop-portal-gnome."
            )
        elif is_wayland():
            message = (
                "Screen capture failed.\n"
                "Wayland: install grim/slurp or gnome-screenshot, or set up xdg-desktop-portal."
            )
        else:
            message = (
                "Screen capture failed.\n"
                "X11: install maim or scrot."
            )
        details = "\n".join(e for e in (native_error, error) if e)
        if details:
            message = f"{message}\n\nDetails:\n{details}"
        self._notify("LinScreen", "Screen capture failed.", QSystemTrayIcon.MessageIcon.Critical, 5000)
        QMessageBox.critical(self._settings, "LinScreen", message)

    def _close_overlays(self):
        for o in self._overlays:
            try: o.close()
            except: pass
        self._overlays = []

    def _save(self, px):
        px = _to_pixmap(px)
        self._close_overlays()
        SCREENSHOTS_DIR.mkdir(parents=True, exist_ok=True)
        ts   = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        path = SCREENSHOTS_DIR / f"screenshot_{ts}.png"
        px.save(str(path))
        copied = self._copy_to_clipboard(px, path)
        if copied:
            message = f"Copied to clipboard. Saved: {path.name}"
            icon = QSystemTrayIcon.MessageIcon.Information
        else:
            message = f"Saved: {path.name}. Clipboard is unavailable."
            if is_wayland():
                message += " Install wl-clipboard."
            icon = QSystemTrayIcon.MessageIcon.Warning
        self._notify("LinScreen", message, icon, 3000)

    def _copy_to_clipboard(self, px, path):
        wl_copy = _which("wl-copy")
        if is_wayland() and wl_copy:
            try:
                r = subprocess.run(
                    [wl_copy, "--type", "image/png"],
                    input=path.read_bytes(),
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=5,
                )
                if r.returncode == 0:
                    return True
            except Exception:
                pass

        QGuiApplication.clipboard().setPixmap(px)
        clipboard = QGuiApplication.clipboard()
        mime = clipboard.mimeData()
        qt_has_image = bool(mime and mime.hasImage()) or not clipboard.pixmap().isNull()

        # On wlroots compositors without wl-copy this Qt state can be local only:
        # the tray app has no focused window, so other apps may not receive it.
        if is_wayland() and (is_niri() or is_hyprland()) and not wl_copy:
            return False
        return qt_has_image

    def _restore(self, restore):
        if restore: QTimer.singleShot(100, self._show_settings)

    def set_autostart(self, enabled):
        d = Path.home() / ".config" / "autostart"; d.mkdir(parents=True, exist_ok=True)
        f = d / "linscreen.desktop"
        if enabled:
            f.write_text("[Desktop Entry]\nType=Application\nName=LinScreen\nExec=/usr/bin/linscreen\nHidden=false\nNoDisplay=false\nX-GNOME-Autostart-enabled=true\n")
        elif f.exists(): f.unlink()

    def quit_app(self):
        if self._capture_proc is not None:
            self._capture_proc.kill()
            self._capture_proc.deleteLater()
            self._capture_proc = None
            self._finish_async_capture()
        if self._capture_thread is not None:
            self._capture_thread.quit()
            self._capture_thread.wait(1000)
        self.quit()


def _send_ipc_message(message):
    sock = QLocalSocket()
    sock.connectToServer(SOCKET_NAME)
    if not sock.waitForConnected(1000):
        return False
    sock.write(message.encode())
    sock.waitForBytesWritten(1000)
    sock.disconnectFromServer()
    return True


def main():
    screenshot_mode = "--screenshot" in sys.argv
    quit_mode = "--quit" in sys.argv
    replace_mode = "--replace" in sys.argv
    if replace_mode:
        _stop_locked_instance()
        try: QLocalServer.removeServer(SOCKET_NAME)
        except Exception: pass
    ipc_message = "screenshot" if screenshot_mode else "show-settings"
    if quit_mode:
        ipc_message = "quit"
    if not _acquire_instance_lock():
        if _send_ipc_message(ipc_message):
            sys.exit(0)
        pid = _locked_instance_pid()
        detail = f" PID: {pid}." if pid else ""
        print(f"LinScreen is already running but does not answer over IPC.{detail} Run: linscreen --replace", file=sys.stderr)
        sys.exit(1)
    if quit_mode:
        sys.exit(0)
    app = LinScreenApp(
        sys.argv,
        show_settings_on_start=False,
        take_screenshot_on_start=screenshot_mode,
    )
    sys.exit(app.exec())

if __name__ == "__main__":
    main()
