"""Tests for writing the screenshot bind into compositor configs.

Run with:  python -m unittest discover tests
Each test gets a throwaway HOME and stub `hyprctl` / `swaymsg`, so no real
compositor or config is touched.
"""
import importlib.util
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = Path(__file__).resolve().parent.parent

HYPRCTL = """#!/bin/sh
if [ "$1" = reload ]; then [ -f "$HOME/break" ] && touch "$HOME/broken" || rm -f "$HOME/broken"; fi
if [ "$1" = configerrors ]; then echo "old unrelated error"; [ -f "$HOME/broken" ] && echo "bindings.lua:12: bad bind"; fi
exit 0
"""
SWAYMSG = '#!/bin/sh\necho \'[ { "success": true } ]\'\n'


def load_app():
    spec = importlib.util.spec_from_file_location("linscreen_app", ROOT / "linscreen.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class BindingsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ls = load_app()
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication(sys.argv)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name) / "home"
        bin_dir = Path(self.tmp.name) / "bin"
        bin_dir.mkdir()
        for name, body in (("hyprctl", HYPRCTL), ("swaymsg", SWAYMSG)):
            (bin_dir / name).write_text(body)
            (bin_dir / name).chmod(0o755)
        self._env = {k: os.environ.get(k) for k in ("HOME", "PATH")}
        os.environ["HOME"] = str(self.home)
        os.environ["PATH"] = f"{bin_dir}:/usr/bin:/bin"
        for sub in ("hypr", "sway", "niri"):
            (self.home / ".config" / sub).mkdir(parents=True)

    def tearDown(self):
        for k, v in self._env.items():
            os.environ[k] = v
        self.tmp.cleanup()

    def cfg(self, rel, text):
        path = self.home / ".config" / rel
        path.write_text(text)
        return path

    def same_keys(self, a, b):
        from PyQt6.QtGui import QKeySequence
        return QKeySequence(a) == QKeySequence(b)

    # Hyprland (Omarchy Lua) -------------------------------------------------

    def test_lua_old_marker_block_is_adopted_and_migrated(self):
        path = self.cfg("hypr/bindings.lua", (
            'o.bind("SUPER + RETURN", "Terminal", "uwsm-app -- xdg-terminal-exec")\n'
            "-- LinScreen managed shortcuts: begin\n"
            "-- Updated automatically from the LinScreen settings window.\n"
            'hl.unbind("SUPER + CTRL + ALT + SPACE")\n'
            'o.bind("SUPER + CTRL + ALT + SPACE", "LinScreen screenshot", "linscreen --screenshot")\n'
            'hl.unbind("SUPER + CTRL + SHIFT + Z")\n'
            'o.bind("SUPER + CTRL + SHIFT + Z", "LinScreen screenshot with editor", "linscreen --edit-screenshot")\n'
            "-- LinScreen managed shortcuts: end\n"
        ))
        b = self.ls._HyprLuaBackend(path)
        self.assertTrue(self.same_keys(b.find(), "Ctrl+Alt+Meta+Space"))
        ok, _ = b.apply("Print")
        text = path.read_text()
        self.assertTrue(ok)
        self.assertIn('o.bind("SUPER + RETURN", "Terminal"', text)
        self.assertNotIn("managed shortcuts", text)
        self.assertNotIn("--edit-screenshot", text)
        self.assertNotIn("SUPER + CTRL + ALT + SPACE", text)
        self.assertIn('o.bind("PRINT", "LinScreen screenshot", "linscreen --screenshot")', text)

    def test_lua_foreign_lines_are_not_ours(self):
        b = self.ls._HyprLuaBackend(self.home / ".config/hypr/bindings.lua")
        self.assertIsNone(b.parse('o.bind("SUPER + B", "Browser", "firefox") -- was "linscreen --screenshot"'))
        self.assertIsNone(b.parse('o.bind("SUPER + B", "B", "firefox"); o.bind("PRINT", nil, "linscreen --screenshot")'))
        self.assertEqual(b.parse('o.bind("PRINT", nil, "linscreen --screenshot")'), ([], "Print"))

    def test_hyprland_rolls_back_on_new_config_error(self):
        path = self.cfg("hypr/bindings.lua", 'o.bind("SUPER + RETURN", "Terminal", "foot")\n')
        (self.home / "break").touch()
        ok, message = self.ls._HyprLuaBackend(path).apply("Meta+Shift+X")
        self.assertFalse(ok, message)
        self.assertEqual(path.read_text(), 'o.bind("SUPER + RETURN", "Terminal", "foot")\n')

    def test_key_names_and_bare_keys(self):
        b = self.ls._HyprLuaBackend(self.cfg("hypr/bindings.lua", ""))
        self.assertIn("SUPER + ESCAPE", b.snippet("Meta+Esc"))
        self.assertIn("SUPER + PERIOD", b.snippet("Meta+."))
        self.assertIn("XF86AudioRaiseVolume", b.snippet("Volume Up"))
        for bare in ("S", "Esc", "Backspace"):
            self.assertFalse(b.apply(bare)[0], bare)
        self.assertTrue(b.apply("F9")[0])

    # Hyprland (hyprland.conf) ----------------------------------------------

    def test_hyprconf_modifier_variables(self):
        b = self.ls._HyprConfBackend(self.home / ".config/hypr/hyprland.conf")
        self.cfg("hypr/hyprland.conf", "$mainMod = SUPER\nbind = $mainMod SHIFT, S, exec, linscreen --screenshot\n")
        self.assertTrue(self.same_keys(b.find(), "Meta+Shift+S"))
        self.cfg("hypr/hyprland.conf", "bind = SUPERSHIFT, P, exec, linscreen --screenshot\n")
        self.assertTrue(self.same_keys(b.find(), "Meta+Shift+P"))
        self.cfg("hypr/hyprland.conf", "bind = $unknown SHIFT, S, exec, linscreen --screenshot\n")
        self.assertIsNone(b.find())

    # Sway --------------------------------------------------------------------

    def test_orphan_marker_never_deletes_user_lines(self):
        path = self.cfg("sway/config", (
            "set $mod Mod4\n"
            "# LinScreen: begin (managed by LinScreen, edit the shortcut in its settings)\n"
            "bindsym $mod+Return exec foot\n"
            "bindsym $mod+d exec wofi\n"
        ))
        b = self.ls._SwayBackend(path)
        self.assertFalse(b.apply("Print")[0])
        self.assertFalse(b.apply("Meta+S")[0])
        self.assertIn("wofi", path.read_text())

    def test_symlink_and_mode_are_preserved(self):
        real = Path(self.tmp.name) / "dotfiles-sway.conf"
        real.write_text("set $mod Mod4\nbindsym $mod+Return exec foot\n")
        real.chmod(0o600)
        link = self.home / ".config/sway/config"
        link.symlink_to(real)
        ok, message = self.ls._SwayBackend(link).apply("Meta+Shift+S")
        self.assertTrue(ok, message)
        self.assertTrue(link.is_symlink())
        self.assertIn("linscreen --screenshot", real.read_text())
        self.assertEqual(stat.S_IMODE(real.stat().st_mode), 0o600)

    def test_handwritten_sway_bind_is_adopted(self):
        path = self.cfg("sway/config", "set $mod Mod4\nbindsym $mod+Shift+s exec linscreen --screenshot\n")
        b = self.ls._SwayBackend(path)
        self.assertTrue(self.same_keys(b.find(), "Meta+Shift+S"))
        b.apply("Print")
        text = path.read_text()
        self.assertEqual(text.count("linscreen --screenshot"), 1)
        self.assertIn("bindsym --no-warn Print exec linscreen --screenshot", text)

    def test_crlf_does_not_accumulate_blocks(self):
        path = self.cfg("sway/config", "set $mod Mod4\r\nbindsym $mod+Return exec foot\r\n")
        b = self.ls._SwayBackend(path)
        b.apply("Print")
        b.apply("Meta+S")
        text = path.read_text()
        self.assertEqual(text.count("LinScreen: begin"), 1)
        self.assertEqual(text.count("linscreen --screenshot"), 1)

    # Niri --------------------------------------------------------------------

    def test_niri_insert_conflict_and_one_line_section(self):
        path = self.cfg("niri/config.kdl", (
            "  binds {\n"
            '    Mod+T { spawn "alacritty"; }\n'
            "    Ctrl+Print { screenshot; }\n"
            "  }\n"
        ))
        b = self.ls._NiriBackend(path)
        self.assertFalse(b.apply("Ctrl+Print")[0])
        self.assertTrue(b.apply("Meta+Shift+S")[0])
        text = path.read_text()
        self.assertEqual(text.count("binds"), 1)
        self.assertLess(text.index("LinScreen: begin"), text.rindex("}"))
        self.assertTrue(self.same_keys(b.find(), "Meta+Shift+S"))
        self.cfg("niri/config.kdl", 'binds { Mod+T { spawn "alacritty"; } }\n')
        self.assertFalse(b.apply("Meta+Shift+S")[0])


if __name__ == "__main__":
    unittest.main()
