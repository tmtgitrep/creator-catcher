#!/bin/sh
set -eu
trap 'status=$?; echo "TEST_EXIT=$status"; if [ "$status" -ne 0 ]; then tail -n 80 /tmp/creator-catcher-apt.log 2>/dev/null || true; fi' EXIT
export DEBIAN_FRONTEND=noninteractive
apt-get -qq update >/tmp/creator-catcher-apt.log 2>&1
if ! apt-get -qq -o Dpkg::Use-Pty=0 install -y /project/dist/creator-catcher_0.3.1_all.deb >>/tmp/creator-catcher-apt.log 2>&1; then
    cat /tmp/creator-catcher-apt.log
    exit 1
fi
test "$(dpkg-query -W -f='${Status}' creator-catcher)" = "install ok installed"
test "$(creator-catcher --version)" = "0.3.1"
su -s /bin/sh -c 'creator-catcher validate' creator-catcher
test -d /var/lib/creator-catcher/downloads
test "$(stat -c %U /var/lib/creator-catcher)" = "creator-catcher"
echo "Clean package installation test passed."
