#!/bin/sh
# Install the finite root privilege boundary used by the OC campaign runner.
set -eu

if [ "$#" -ne 1 ]; then
    echo "usage: sudo ./systemd/install-oc-helper.sh <campaign-user>" >&2
    exit 2
fi

campaign_user=$1
case "$campaign_user" in
    *[!A-Za-z0-9_.-]*|'')
        echo "invalid campaign user" >&2
        exit 2
        ;;
esac
getent passwd "$campaign_user" >/dev/null || {
    echo "unknown campaign user: $campaign_user" >&2
    exit 2
}

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)

if ! getent group hw1-oc >/dev/null; then
    groupadd --system hw1-oc
fi

install -d -o root -g root -m 0755 /usr/local/libexec
install -o root -g root -m 0755 \
    "$script_dir/hw1-oc-helper" /usr/local/libexec/hw1-oc-helper

sudoers_tmp=$(mktemp /etc/sudoers.d/.hw1-oc-helper.XXXXXX)
trap 'rm -f "$sudoers_tmp"' EXIT HUP INT TERM
install -o root -g root -m 0440 \
    "$script_dir/hw1-oc-helper.sudoers" "$sudoers_tmp"
visudo -cf "$sudoers_tmp"
mv -f "$sudoers_tmp" /etc/sudoers.d/hw1-oc-helper
chmod 0440 /etc/sudoers.d/hw1-oc-helper
trap - EXIT HUP INT TERM

usermod -a -G hw1-oc "$campaign_user"
/usr/local/libexec/hw1-oc-helper status
echo "installed; reboot once so the campaign user's new group is active"
