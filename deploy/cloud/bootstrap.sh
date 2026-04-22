#!/usr/bin/env bash
# =============================================================================
# Indian Scalper — one-shot cloud VM bootstrap
# =============================================================================
# Runs on a fresh Ubuntu 22.04 / 24.04 VM (Hetzner / DigitalOcean / any). Idempotent.
#
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/<user>/indian-scalper/main/deploy/cloud/bootstrap.sh \
#     | sudo TS_AUTHKEY=tskey-auth-xxxx REPO_URL=https://github.com/<user>/indian-scalper.git bash
#
# Or (after cloud-init has placed this file):
#   sudo TS_AUTHKEY=tskey-auth-xxxx bash /opt/indian-scalper/deploy/cloud/bootstrap.sh
#
# Env vars honoured:
#   TS_AUTHKEY      — Tailscale reusable auth key (required for remote access)
#   REPO_URL        — git URL to clone (default: github origin if already present)
#   GIT_REF         — branch/tag/commit to check out (default: main)
#   SCALPER_USER    — system user (default: scalper)
#   INSTALL_DIR     — where the repo lands (default: /opt/indian-scalper)
# =============================================================================

set -euo pipefail

SCALPER_USER="${SCALPER_USER:-scalper}"
INSTALL_DIR="${INSTALL_DIR:-/opt/indian-scalper}"
GIT_REF="${GIT_REF:-main}"
REPO_URL="${REPO_URL:-}"

log() { printf "\n\033[1;33m▶\033[0m %s\n" "$*"; }
die() { printf "\n\033[1;31m✗\033[0m %s\n" "$*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "Run as root (sudo)."

# -----------------------------------------------------------------------------
# 1. Base packages
# -----------------------------------------------------------------------------
log "Installing base packages…"
apt-get update -y
DEBIAN_FRONTEND=noninteractive apt-get install -y \
    ca-certificates curl gnupg git ufw jq python3 python3-venv tzdata

timedatectl set-timezone Asia/Kolkata || true

# -----------------------------------------------------------------------------
# 2. Create scalper user (non-root, passwordless sudo for docker only)
# -----------------------------------------------------------------------------
if ! id -u "$SCALPER_USER" &>/dev/null; then
    log "Creating user $SCALPER_USER…"
    useradd -m -s /bin/bash "$SCALPER_USER"
fi
# Propagate the root user's SSH key so ssh scalper@<ip> works from the same laptop.
if [ -f /root/.ssh/authorized_keys ] && [ ! -s "/home/$SCALPER_USER/.ssh/authorized_keys" ]; then
    mkdir -p "/home/$SCALPER_USER/.ssh"
    cp /root/.ssh/authorized_keys "/home/$SCALPER_USER/.ssh/authorized_keys"
    chown -R "$SCALPER_USER:$SCALPER_USER" "/home/$SCALPER_USER/.ssh"
    chmod 700 "/home/$SCALPER_USER/.ssh"
    chmod 600 "/home/$SCALPER_USER/.ssh/authorized_keys"
fi

# Allow docker without password only — no arbitrary root.
cat >/etc/sudoers.d/90-scalper <<EOF
$SCALPER_USER ALL=(ALL) NOPASSWD: /usr/bin/docker, /usr/bin/systemctl, /usr/bin/journalctl
EOF
chmod 440 /etc/sudoers.d/90-scalper

# -----------------------------------------------------------------------------
# 3. Docker
# -----------------------------------------------------------------------------
if ! command -v docker &>/dev/null; then
    log "Installing Docker…"
    install -m 0755 -d /etc/apt/keyrings
    curl -fsSL https://download.docker.com/linux/ubuntu/gpg \
        | gpg --dearmor -o /etc/apt/keyrings/docker.gpg
    chmod a+r /etc/apt/keyrings/docker.gpg
    . /etc/os-release
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] \
          https://download.docker.com/linux/ubuntu $VERSION_CODENAME stable" \
        | tee /etc/apt/sources.list.d/docker.list >/dev/null
    apt-get update -y
    DEBIAN_FRONTEND=noninteractive apt-get install -y \
        docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
    usermod -aG docker "$SCALPER_USER"
    systemctl enable --now docker
fi

# -----------------------------------------------------------------------------
# 4. Tailscale
# -----------------------------------------------------------------------------
if ! command -v tailscale &>/dev/null; then
    log "Installing Tailscale…"
    curl -fsSL https://tailscale.com/install.sh | sh
fi

if [ -n "${TS_AUTHKEY:-}" ]; then
    log "Joining tailnet…"
    tailscale up --authkey="$TS_AUTHKEY" --ssh --hostname="scalper" --accept-routes || \
        log "tailscale up returned non-zero — may already be up"
else
    log "TS_AUTHKEY not set — skip tailnet join. Run later:"
    log "  sudo tailscale up --authkey=tskey-auth-xxxx --ssh --hostname=scalper"
fi

# -----------------------------------------------------------------------------
# 5. Firewall — deny everything except SSH + Tailscale
# -----------------------------------------------------------------------------
log "Configuring UFW…"
ufw --force reset
ufw default deny incoming
ufw default allow outgoing
ufw allow 22/tcp comment 'ssh'
ufw allow in on tailscale0 comment 'tailnet'
ufw --force enable

# -----------------------------------------------------------------------------
# 6. Clone / update the repo
# -----------------------------------------------------------------------------
if [ ! -d "$INSTALL_DIR/.git" ]; then
    [ -n "$REPO_URL" ] || die "REPO_URL not set and $INSTALL_DIR isn't a git clone."
    log "Cloning $REPO_URL → $INSTALL_DIR"
    rm -rf "$INSTALL_DIR"
    sudo -u "$SCALPER_USER" git clone "$REPO_URL" "$INSTALL_DIR"
fi

log "Checking out $GIT_REF"
cd "$INSTALL_DIR"
sudo -u "$SCALPER_USER" git fetch --all
sudo -u "$SCALPER_USER" git checkout "$GIT_REF"
sudo -u "$SCALPER_USER" git pull --ff-only || true

# -----------------------------------------------------------------------------
# 7. .env seed — never overwrite if present
# -----------------------------------------------------------------------------
if [ ! -f "$INSTALL_DIR/.env" ]; then
    log "Seeding $INSTALL_DIR/.env (edit before first start)"
    cat >"$INSTALL_DIR/.env" <<EOF
# Fill these in, then:  sudo -u $SCALPER_USER docker compose -f docker-compose.tailnet.yml up -d
UPSTOX_API_KEY=
UPSTOX_API_SECRET=
UPSTOX_ACCESS_TOKEN=
LIVE_TRADING_ACKNOWLEDGED=no
SCALPER_TAILSCALE_ONLY=yes
EOF
    chown "$SCALPER_USER:$SCALPER_USER" "$INSTALL_DIR/.env"
    chmod 600 "$INSTALL_DIR/.env"
fi

mkdir -p "$INSTALL_DIR/data" "$INSTALL_DIR/logs"
chown -R "$SCALPER_USER:$SCALPER_USER" "$INSTALL_DIR/data" "$INSTALL_DIR/logs"

# -----------------------------------------------------------------------------
# 8. Summary
# -----------------------------------------------------------------------------
log "Bootstrap complete."
echo
echo "  Next steps:"
echo "    1. ssh $SCALPER_USER@<your-vm>    (add your public key to ~${SCALPER_USER}/.ssh/authorized_keys)"
echo "    2. nano $INSTALL_DIR/.env        (fill Upstox API key + secret)"
echo "    3. cd $INSTALL_DIR && sudo -u $SCALPER_USER docker compose -f docker-compose.tailnet.yml up -d"
echo "    4. Open http://scalper:8080/auth/upstox on your phone (tailnet) to get the access token."
echo
if command -v tailscale &>/dev/null && tailscale status &>/dev/null; then
    TS_IP=$(tailscale ip -4 2>/dev/null | head -1 || true)
    [ -n "$TS_IP" ] && echo "  Tailnet IP: $TS_IP" && \
        echo "  Dashboard : http://$TS_IP:8080/"
fi
