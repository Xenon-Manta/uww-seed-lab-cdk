# SEED Labs EC2 — AWS CDK Deployment

Deploys a fully configured [SEED Security Labs](https://seedsecuritylabs.org/) VM on AWS EC2 using the Python CDK. After deployment you can reach the lab desktop over VNC or work via SSH.

## What Gets Created

| Resource | Details |
|---|---|
| VPC | Dedicated `/16` VPC, single public subnet, Internet Gateway |
| Security Group | Inbound SSH (22) and VNC (5901–5910) from `0.0.0.0/0` by default |
| EC2 Instance | `t3.small` (2 vCPU / 2 GiB RAM), Ubuntu 20.04 LTS (x86_64) |
| Root Volume | 16 GiB gp3, encrypted, deleted on termination |
| Key Pair | RSA `.pem` key, private key stored in SSM Parameter Store |

On first boot, user-data automatically:
1. Downloads and unpacks `src-cloud.zip` from the SEED CDN
2. Runs `install.sh` non-interactively (pre-seeds Wireshark → No, display manager → LightDM)
3. Creates the `seed` account and sets a VNC password
4. Starts TigerVNC on display `:1` (port `5901`) via a systemd service

Bootstrap log: `/var/log/seed-bootstrap.log`

---

## Prerequisites

| Tool | Min Version | Install |
|---|---|---|
| Python | 3.11+ | [python.org](https://python.org) |
| Node.js | 18+ | [nodejs.org](https://nodejs.org) |
| AWS CDK CLI | 2.x | `npm install -g aws-cdk` |
| AWS CLI | 2.x | [docs.aws.amazon.com/cli](https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html) |

You need an AWS account with credentials configured (`aws configure` or environment variables).

---

## Quick Start

### 1. Clone / open the project

```powershell
cd sec-systems   # this workspace folder
```

### 2. Create and activate a virtual environment

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
```

### 3. Install Python dependencies

```powershell
pip install -r requirements.txt
```

### 4. Bootstrap CDK (first time only, per account/region)

```powershell
cdk bootstrap
```

### 5. Deploy

```powershell
cdk deploy
```

CDK will show you the resources to be created and ask for confirmation. Type `y` to proceed.

Deployment takes ~2 minutes. The SEED software installation on the instance then runs in the background and takes an additional **5–10 minutes**. Watch progress with:

```bash
# SSH in first (see below), then:
tail -f /var/log/seed-bootstrap.log
```

---

## Connecting to the VM

### Retrieve the SSH private key

The key is stored in SSM Parameter Store. Use the `KeyPairSsmPath` output printed after `cdk deploy`:

```powershell
# Replace <key-pair-id> with the value from the KeyPairSsmPath output
aws ssm get-parameter `
    --name "/ec2/keypair/<key-pair-id>" `
    --with-decryption `
    --query Parameter.Value `
    --output text | Out-File -Encoding ascii seed-lab-key.pem
```

On Linux/macOS:
```bash
chmod 400 seed-lab-key.pem
```

### SSH

```bash
# Use the SshCommand output printed by cdk deploy, or:
ssh -i seed-lab-key.pem ubuntu@<PublicIp>
```

Switch to the `seed` account once connected:
```bash
sudo su seed
```

### VNC (graphical desktop)

1. Install a VNC viewer — [TigerVNC](https://tigervnc.org/) or [RealVNC](https://www.realvnc.com/en/connect/download/viewer/) are both compatible.
2. Connect to `<PublicIp>:5901` (use the `VncAddress` output).
3. Enter the default VNC password: **`SEEDlabs2024!`**
4. **Change the password immediately** after first login:
   ```bash
   sudo su seed
   vncpasswd
   ```

> VNC traffic is unencrypted. For sensitive work, tunnel it over SSH:
> ```bash
> ssh -i seed-lab-key.pem -L 5901:localhost:5901 ubuntu@<PublicIp>
> ```
> Then connect your VNC viewer to `localhost:5901`.

---

## Customisation

Override defaults at deploy time with `--context` flags:

```powershell
cdk deploy `
    --context allowed_cidr=203.0.113.0/24 `   # restrict SSH/VNC to your IP range
    --context instance_type=t3.medium `        # upgrade if performance is slow
    --context volume_size=24                   # extra disk space in GiB
```

| Context Key | Default | Description |
|---|---|---|
| `allowed_cidr` | `0.0.0.0/0` | CIDR allowed to reach SSH and VNC |
| `instance_type` | `t3.small` | EC2 instance type |
| `volume_size` | `16` | Root volume size in GiB |

---

## Cost Estimate

All prices are approximate US East (N. Virginia) on-demand rates:

| Item | $/hr | $/month (730 hr) |
|---|---|---|
| t3.small compute | ~$0.023 | ~$17 |
| 16 GiB gp3 storage | — | ~$1.28 |
| Data transfer | variable | ~$1–5 |

**Stop the instance when not in use** — a stopped instance incurs only storage cost (~$1.28/mo). Resume it from the AWS Console or CLI:

```bash
aws ec2 start-instances  --instance-ids <InstanceId>
aws ec2 stop-instances   --instance-ids <InstanceId>
```

---

## Teardown

To delete all resources (instance, VPC, key pair, security group):

```powershell
cdk destroy
```

> The root EBS volume is set to `delete_on_termination=True`, so it will be removed automatically.

---

## Project Layout

```
sec-systems/
├── app.py                      # CDK app entry point
├── cdk.json                    # CDK configuration
├── requirements.txt            # Python dependencies
└── seed_lab/
    ├── __init__.py
    ├── seed_lab_stack.py       # CDK stack (VPC, SG, EC2, outputs)
    └── user_data.sh            # EC2 bootstrap / cloud-init script
```

---

## Troubleshooting

**Bootstrap takes longer than 15 minutes**
Check the log: `tail -f /var/log/seed-bootstrap.log`. The `install.sh` script downloads several hundred MB; slow regions can take up to 20 minutes.

**VNC connection refused**
The VNC service may still be starting. Wait a minute after SSH access works, then check:
```bash
sudo systemctl status vncserver@1.service
sudo journalctl -u vncserver@1.service --no-pager
```

**AMI not found in my region**
Add your region's Ubuntu 20.04 AMI ID to the `UBUNTU_2004_AMI` dict in `seed_lab/seed_lab_stack.py`, or let CDK do a dynamic lookup (requires network access during `cdk synth`).

**SSH key permissions error on Windows**
PowerShell's `icacls` can fix this:
```powershell
icacls seed-lab-key.pem /inheritance:r /grant:r "$($env:USERNAME):(R)"
```
