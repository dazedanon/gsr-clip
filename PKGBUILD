# Maintainer: dazed <dazed@users.noreply.github.com>
pkgname=gsr-clip
pkgver=0.1.0
pkgrel=1
pkgdesc="Clip + session recorder on gpu-screen-recorder: replay buffer, Steam-gated auto sessions, highlight viewer + GUI"
arch=('any')
url="https://github.com/dazedanon/gsr-clip"
license=('GPL-3.0-only')
depends=(
  'python'
  'python-evdev'
  'gpu-screen-recorder'
  'ffmpeg'
  'kdotool'           # AUR; required for active-window detection on KDE Wayland
)
optdepends=(
  'pyside6: desktop GUI and system tray (gsr-clip-gui)'
  'libnotify: desktop notification popups (notify-send)'
  'libpulse: sound cues via paplay'
  'pipewire: sound cues via pw-play'
)
makedepends=('git' 'python-build' 'python-installer' 'python-wheel' 'python-setuptools')
source=("$pkgname::git+file://$startdir")
sha256sums=('SKIP')

build() {
  cd "$srcdir/$pkgname"
  python -m build --wheel --no-isolation
}

package() {
  cd "$srcdir/$pkgname"
  python -m installer --destdir="$pkgdir" dist/*.whl

  # System-wide user service (ExecStart uses the installed /usr/bin entry point)
  install -Dm644 packaging/gsr-clip.user.service \
    "$pkgdir/usr/lib/systemd/user/gsr-clip.service"

  # Desktop launcher + icon
  install -Dm644 packaging/gsr-clip.desktop \
    "$pkgdir/usr/share/applications/gsr-clip.desktop"
  install -Dm644 gsr_clip/assets/icon.svg \
    "$pkgdir/usr/share/icons/hicolor/scalable/apps/gsr-clip.svg"

  # Docs + example config
  install -Dm644 packaging/config.example.toml \
    "$pkgdir/usr/share/doc/$pkgname/config.example.toml"
  install -Dm644 README.md "$pkgdir/usr/share/doc/$pkgname/README.md"
}
