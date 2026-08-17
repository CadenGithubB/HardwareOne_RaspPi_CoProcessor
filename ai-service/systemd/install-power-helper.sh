#!/bin/sh
# Install the isolated host-power privilege boundary for one service account.
set -eu

if [ "$#" -ne 1 ]; then
    echo "usage: sudo ./systemd/install-power-helper.sh <service-user>" >&2
    exit 2
fi

service_user=$1
case "$service_user" in
    *[!A-Za-z0-9_.-]*|'')
        echo "invalid service user" >&2
        exit 2
        ;;
esac

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
sudoers_tmp=$(mktemp /etc/sudoers.d/.hw1-power-helper.XXXXXX)
trap 'rm -f "$sudoers_tmp"' EXIT HUP INT TERM
install -o root -g root -m 0440 \
    "$script_dir/hw1-power-helper.sudoers" "$sudoers_tmp"
# Validate the root-owned snapshot before it can become a live included file.
# The leading-dot temporary name is ignored by sudo's #includedir scan.
visudo -cf "$sudoers_tmp"

install -d -o root -g root -m 0755 /usr/local/libexec
install -o root -g root -m 0755 \
    "$script_dir/hw1-power-helper" /usr/local/libexec/hw1-power-helper
mv -f "$sudoers_tmp" /etc/sudoers.d/hw1-power-helper
chmod 0440 /etc/sudoers.d/hw1-power-helper
trap - EXIT HUP INT TERM

if ! getent group hw1-power >/dev/null; then
    groupadd --system hw1-power
fi
usermod -a -G hw1-power "$service_user"

echo "installed; log out/in (or reboot) before enabling power.enabled"
