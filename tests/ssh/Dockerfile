FROM ubuntu:24.04

# A minimal, throwaway SSH target for Tier-2 integration tests. It runs sshd,
# has an unprivileged `ansible` user with passwordless sudo (for become tests),
# and python3 (required by Ansible modules). The authorized public key is
# injected at runtime via the PUBLIC_KEY env var (see entrypoint.sh) so no key
# material is ever baked into the image.
ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        openssh-server \
        sudo \
        python3 \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && useradd -m -s /bin/bash ansible \
    && echo 'ansible ALL=(ALL) NOPASSWD:ALL' > /etc/sudoers.d/ansible \
    && chmod 0440 /etc/sudoers.d/ansible \
    && mkdir -p /home/ansible/.ssh /run/sshd \
    && chown -R ansible:ansible /home/ansible/.ssh \
    && chmod 700 /home/ansible/.ssh \
    && ssh-keygen -A

COPY entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

EXPOSE 22
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
