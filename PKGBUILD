# Maintainer: FPP <fpphub@proton.me>
pkgname=linscreen
pkgver=1.0.0
pkgrel=1
pkgdesc="Area screenshot tool for Linux (Wayland and X11) with tray, hotkey and automatic compositor bindings"
arch=('any')
url="https://github.com/FPP-Labs/linscreen"
license=('MIT')
depends=(
    'python'
    'python-pyqt6'
)
optdepends=(
    'spectacle: screen capture backend on KDE'
    'grim: area capture backend on Hyprland/Sway/Niri/wlroots Wayland'
    'slurp: area selection helper for grim on Hyprland/Sway/Niri/wlroots Wayland'
    'gnome-screenshot: interactive area capture on GNOME'
    'wl-clipboard: reliable clipboard copy on Wayland compositors such as Niri/Hyprland'
    'xdg-desktop-portal: portal screenshot fallback on Wayland'
    'maim: interactive area capture on X11'
    'scrot: X11 screenshot fallback'
)
source=(
    "linscreen.py"
    "linscreen.desktop"
    "linscreen.png"
    "LICENSE"
)
sha256sums=('SKIP' 'SKIP' 'SKIP' 'SKIP')

package() {
    # Main executable
    install -Dm755 "$srcdir/linscreen.py" "$pkgdir/usr/bin/linscreen"

    # Desktop entry
    install -Dm644 "$srcdir/linscreen.desktop" \
        "$pkgdir/usr/share/applications/linscreen.desktop"

    # Application icon
    install -Dm644 "$srcdir/linscreen.png" \
        "$pkgdir/usr/share/pixmaps/linscreen.png"
    install -Dm644 "$srcdir/linscreen.png" \
        "$pkgdir/usr/share/icons/hicolor/256x256/apps/linscreen.png"

    # License
    install -Dm644 "$srcdir/LICENSE" \
        "$pkgdir/usr/share/licenses/$pkgname/LICENSE"
}
