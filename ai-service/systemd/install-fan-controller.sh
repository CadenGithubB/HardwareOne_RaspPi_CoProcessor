#!/bin/sh
# Install the isolated root-owned CM5 fan controller for one socket client.
set -eu

if [ "$#" -ne 1 ]; then
    echo "usage: sudo ./systemd/install-fan-controller.sh <service-user>" >&2
    exit 2
fi

service_user=$1
case "$service_user" in
    *[!A-Za-z0-9_.-]*|'')
        echo "invalid service user" >&2
        exit 2
        ;;
esac
if ! getent passwd "$service_user" >/dev/null; then
    echo "service user does not exist: $service_user" >&2
    exit 2
fi
if [ "$(id -u "$service_user")" -eq 0 ]; then
    echo "service user must be an unprivileged non-root account" >&2
    exit 2
fi

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)

if ! getent group hw1-fan >/dev/null; then
    groupadd --system hw1-fan
fi
usermod -a -G hw1-fan "$service_user"

install -d -o root -g root -m 0755 /usr/local/libexec
install -o root -g root -m 0755 \
    "$script_dir/hw1-fan-controller" \
    /usr/local/libexec/hw1-fan-controller

install -d -o root -g root -m 0755 /etc/systemd/system
install -o root -g root -m 0644 \
    "$script_dir/hw1-fan-controller.service" \
    /etc/systemd/system/hw1-fan-controller.service

if [ ! -e /etc/hw1-fan-controller.json ]; then
    install -o root -g root -m 0644 \
        "$script_dir/hw1-fan-controller.example.json" \
        /etc/hw1-fan-controller.json
else
    echo "preserving existing /etc/hw1-fan-controller.json"
fi

/usr/local/libexec/hw1-fan-controller \
    --config /etc/hw1-fan-controller.json --check-config
systemctl daemon-reload
systemctl enable hw1-fan-controller.service
systemctl restart hw1-fan-controller.service

status_text=$(systemctl show \
    --property=StatusText --value hw1-fan-controller.service)
echo "installed and started the hw1-fan-controller process"
echo "first hardware status: ${status_text:-not yet reported}"
echo "verify health is not unavailable/io_error before enabling fan.enabled"
echo "reboot before the lingering user service for $service_user opens the control socket"
