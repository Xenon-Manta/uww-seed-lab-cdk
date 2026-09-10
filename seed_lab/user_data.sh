#!/bin/bash
# =============================================================================
# SEED Labs EC2 User-Data Bootstrap Script
# Runs as root on first boot via cloud-init.
#
# What this does:
#   1. Creates the 'seed' user account (no password — sudo su seed to access)
#   2. Downloads and installs SEED Labs software from seed.nyc3.cdn.digitaloceanspaces.com
#   3. Answers Wireshark and LightDM prompts non-interactively
#   4. Starts TigerVNC server on display :1 (port 5901) for the seed user
#   5. Writes a systemd service so VNC auto-starts on reboot
# =============================================================================

set -euo pipefail
exec > /var/log/seed-bootstrap.log 2>&1

echo "=== SEED Labs bootstrap started at $(date) ==="

# ---------------------------------------------------------------------------
# 1. System update
# ---------------------------------------------------------------------------
export DEBIAN_FRONTEND=noninteractive
apt-get update -y
apt-get upgrade -y -o Dpkg::Options::="--force-confold"

# ---------------------------------------------------------------------------
# 2. Install unzip (needed to unpack src-cloud.zip)
# ---------------------------------------------------------------------------
apt-get install -y unzip

# ---------------------------------------------------------------------------
# 3. Pre-seed debconf answers so install.sh runs fully unattended
#    - Wireshark: non-superusers should NOT capture packets → select "No"
#    - Display manager: use LightDM
# ---------------------------------------------------------------------------
apt-get install -y debconf-utils

echo "wireshark-common wireshark-common/install-setuid boolean false" \
    | debconf-set-selections

echo "lightdm shared/default-x-display-manager select lightdm" \
    | debconf-set-selections

# ---------------------------------------------------------------------------
# 4. Download and unpack src-cloud.zip
# ---------------------------------------------------------------------------
cd /tmp
curl -fsSL -o src-cloud.zip \
    https://seed.nyc3.cdn.digitaloceanspaces.com/src-cloud.zip

unzip -q src-cloud.zip

# ---------------------------------------------------------------------------
# 5. Run the SEED install script
#    DEBIAN_FRONTEND=noninteractive ensures no interactive prompts block us.
# ---------------------------------------------------------------------------
cd /tmp/src-cloud
chmod +x install.sh
DEBIAN_FRONTEND=noninteractive ./install.sh

echo "=== SEED install.sh finished at $(date) ==="

# ---------------------------------------------------------------------------
# 6. Set a VNC password for the seed user
#    We write it directly to the TigerVNC passwd file so there is no
#    interactive prompt.  Change this password after first login!
# ---------------------------------------------------------------------------
VNC_PASSWORD="SEEDlabs2024!"   # <-- change after deployment

mkdir -p /home/seed/.vnc
# vncpasswd -f reads plaintext from stdin and writes the hashed file
echo "${VNC_PASSWORD}" | sudo -u seed vncpasswd -f > /home/seed/.vnc/passwd
chmod 600 /home/seed/.vnc/passwd
chown seed:seed /home/seed/.vnc/passwd

# ---------------------------------------------------------------------------
# 7. Write an xstartup file so TigerVNC launches the XFCE4 desktop
# ---------------------------------------------------------------------------
cat > /home/seed/.vnc/xstartup <<'XSTARTUP'
#!/bin/bash
unset SESSION_MANAGER
unset DBUS_SESSION_BUS_ADDRESS
exec /bin/sh /etc/xdg/xfce4/xinitrc
XSTARTUP
chmod +x /home/seed/.vnc/xstartup
chown seed:seed /home/seed/.vnc/xstartup

# ---------------------------------------------------------------------------
# 8. Create a systemd service so VNC starts automatically on boot
# ---------------------------------------------------------------------------
cat > /etc/systemd/system/vncserver@.service <<'SYSTEMD'
[Unit]
Description=TigerVNC server for display :%i
After=network.target

[Service]
Type=forking
User=seed
Group=seed
WorkingDirectory=/home/seed

# Clean up any stale lock files from a previous run
ExecStartPre=/bin/sh -c '/usr/bin/vncserver -kill :%i > /dev/null 2>&1 || true'
ExecStart=/usr/bin/vncserver :%i -localhost no -geometry 1280x800 -depth 24
ExecStop=/usr/bin/vncserver -kill :%i

Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
SYSTEMD

systemctl daemon-reload
systemctl enable vncserver@1.service
systemctl start  vncserver@1.service

# ---------------------------------------------------------------------------
# 9. Ensure the AWS SSM Agent is installed, enabled, and running.
#    Ubuntu 20.04 ships snap-based SSM Agent; make sure it is active so that
#    SSM Patch Manager can connect during the weekly maintenance window.
# ---------------------------------------------------------------------------
echo "=== Ensuring SSM Agent is running ==="

# The snap package is the recommended install path on Ubuntu 20.04.
# If it is already installed (as on most AWS-published AMIs) this is a no-op.
if ! snap list amazon-ssm-agent &>/dev/null; then
    snap install amazon-ssm-agent --classic
fi

# Start and enable via snap service management.
snap start amazon-ssm-agent
systemctl enable snap.amazon-ssm-agent.amazon-ssm-agent.service || true

echo "SSM Agent status: $(snap services amazon-ssm-agent | tail -1)"

echo "=== SEED Labs bootstrap completed successfully at $(date) ==="
