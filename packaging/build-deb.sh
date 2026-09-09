#!/bin/sh
set -eu
PROJECT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
VERSION=${1:-0.4.0}
OUTPUT_DIR="$PROJECT_DIR/dist"
WORK_DIR=$(mktemp -d)
STAGE="$WORK_DIR/root"
DEB_PARTS="$WORK_DIR/deb-parts"
trap 'rm -rf -- "$WORK_DIR"' EXIT HUP INT TERM
mkdir -p "$STAGE" "$DEB_PARTS"
chmod 0755 "$STAGE"

install -d "$STAGE/DEBIAN" "$STAGE/usr/bin"
install -d "$STAGE/usr/lib/creator-catcher/creator_catcher/static"
install -d "$STAGE/usr/lib/creator-catcher/vendor"
install -d "$STAGE/usr/lib/systemd/system" "$STAGE/usr/lib/sysusers.d"
install -d "$STAGE/usr/lib/tmpfiles.d" "$STAGE/usr/share/doc/creator-catcher"
install -d "$STAGE/etc/default"
install -m 0644 "$PROJECT_DIR/src/creator_catcher/app.py" "$STAGE/usr/lib/creator-catcher/creator_catcher/app.py"
install -m 0644 "$PROJECT_DIR/src/creator_catcher/static/index.html" "$STAGE/usr/lib/creator-catcher/creator_catcher/static/index.html"
install -m 0644 "$PROJECT_DIR/src/creator_catcher/static/app.css" "$STAGE/usr/lib/creator-catcher/creator_catcher/static/app.css"
install -m 0644 "$PROJECT_DIR/src/creator_catcher/static/app.js" "$STAGE/usr/lib/creator-catcher/creator_catcher/static/app.js"
install -m 0755 "$PROJECT_DIR/vendor/yt-dlp" "$STAGE/usr/lib/creator-catcher/vendor/yt-dlp"
install -m 0644 "$PROJECT_DIR/packaging/systemd/creator-catcher-web.service" "$STAGE/usr/lib/systemd/system/creator-catcher-web.service"
install -m 0644 "$PROJECT_DIR/packaging/systemd/creator-catcher-scan.service" "$STAGE/usr/lib/systemd/system/creator-catcher-scan.service"
install -m 0644 "$PROJECT_DIR/packaging/systemd/creator-catcher-scan.timer" "$STAGE/usr/lib/systemd/system/creator-catcher-scan.timer"
install -m 0644 "$PROJECT_DIR/packaging/debian/creator-catcher.default" "$STAGE/etc/default/creator-catcher"
install -m 0644 "$PROJECT_DIR/packaging/debian/creator-catcher.sysusers" "$STAGE/usr/lib/sysusers.d/creator-catcher.conf"
install -m 0644 "$PROJECT_DIR/packaging/debian/creator-catcher.tmpfiles" "$STAGE/usr/lib/tmpfiles.d/creator-catcher.conf"
install -m 0644 "$PROJECT_DIR/packaging/debian/copyright" "$STAGE/usr/share/doc/creator-catcher/copyright"
install -m 0644 "$PROJECT_DIR/README.md" "$STAGE/usr/share/doc/creator-catcher/README"

cat >"$STAGE/usr/bin/creator-catcher" <<'EOF'
#!/bin/sh
exec /usr/bin/python3 /usr/lib/creator-catcher/creator_catcher/app.py "$@"
EOF
chmod 0755 "$STAGE/usr/bin/creator-catcher"

cat >"$STAGE/DEBIAN/control" <<EOF
Package: creator-catcher
Version: $VERSION
Section: video
Priority: optional
Architecture: all
Maintainer: tmtgitrep <tmtgitrep@users.noreply.github.com>
Depends: python3 (>= 3.11), ffmpeg, ca-certificates, cifs-utils, systemd
Description: Monitor and download authorized creator videos
 Creator Catcher provides a private web interface, scheduled channel checks,
 download progress, duplicate protection, and post-download network transfers.
EOF
printf '/etc/default/creator-catcher\n' >"$STAGE/DEBIAN/conffiles"
install -m 0755 "$PROJECT_DIR/packaging/debian/postinst" "$STAGE/DEBIAN/postinst"
install -m 0755 "$PROJECT_DIR/packaging/debian/prerm" "$STAGE/DEBIAN/prerm"
install -m 0755 "$PROJECT_DIR/packaging/debian/postrm" "$STAGE/DEBIAN/postrm"
mkdir -p "$OUTPUT_DIR"
PACKAGE_FILE="$OUTPUT_DIR/creator-catcher_${VERSION}_all.deb"
if command -v dpkg-deb >/dev/null 2>&1; then
    dpkg-deb --root-owner-group --build "$STAGE" "$PACKAGE_FILE"
else
    TEMP_PACKAGE="$WORK_DIR/creator-catcher.deb"
    printf '2.0\n' >"$DEB_PARTS/debian-binary"
    tar --sort=name --owner=0 --group=0 --numeric-owner \
        -C "$STAGE/DEBIAN" -cJf "$DEB_PARTS/control.tar.xz" .
    tar --sort=name --owner=0 --group=0 --numeric-owner \
        --exclude='./DEBIAN' -C "$STAGE" -cJf "$DEB_PARTS/data.tar.xz" .
    ar cr "$TEMP_PACKAGE" \
        "$DEB_PARTS/debian-binary" "$DEB_PARTS/control.tar.xz" "$DEB_PARTS/data.tar.xz"
    mv "$TEMP_PACKAGE" "$PACKAGE_FILE"
fi
cd "$OUTPUT_DIR"
sha256sum "creator-catcher_${VERSION}_all.deb" >"creator-catcher_${VERSION}_all.deb.sha256"
