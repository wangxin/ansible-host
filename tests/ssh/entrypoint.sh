#!/bin/sh
# Install the test's public key (passed via PUBLIC_KEY) into the ansible user's
# authorized_keys, then run sshd in the foreground. No key material is baked
# into the image; it is supplied at container start time.
set -eu

if [ -n "${PUBLIC_KEY:-}" ]; then
    printf '%s\n' "$PUBLIC_KEY" > /home/ansible/.ssh/authorized_keys
    chown ansible:ansible /home/ansible/.ssh/authorized_keys
    chmod 600 /home/ansible/.ssh/authorized_keys
fi

exec /usr/sbin/sshd -D -e
