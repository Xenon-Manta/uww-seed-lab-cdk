# SEED Labs EC2 — AWS CDK Deployment

Deploys a fully configured [SEED Security Labs](https://seedsecuritylabs.org/) VM on AWS EC2 using the Python CDK. After deployment you can reach the lab desktop over SSM. Rules for VNC and SSH for the Security Group in file seed_lab_stack.py are commented out by default for security posture. IF accessing via SSM is inadequate, uncomment and then redeploy as needed. I *strongly* encourage doing this AFTER you set a stronger password for the VM.

---

## Architecture

The architecture diagram is maintained as a [draw.io](https://www.drawio.com/) file:

📐 **[`docs/architecture.drawio`](docs/architecture.drawio)**

Open it with:
- **VS Code** — [Draw.io Integration](https://marketplace.visualstudio.com/items?itemName=hediet.vscode-drawio) extension (just double-click the file)
- **Desktop app** — [diagrams.net desktop](https://github.com/jgraph/drawio-desktop/releases)
- **Browser** — drag and drop the file onto [app.diagrams.net](https://app.diagrams.net)

The diagram covers:

| Component | What's shown |
|---|---|
| Your Device | SSH (:22) and VNC (:5901) connections into the VPC |
| VPC / Public Subnet | 10.10.0.0/16, single public subnet, Internet Gateway |
| EC2 Instance | t3.small, Ubuntu 20.04, Security Group, IAM Role, Key Pair |
| SSM Patch Manager | Weekly maintenance window (Sundays 02:00 UTC) via SSM Agent |
| EventBridge + Lambda | Daily auto-stop cron at 06:00 UTC (01:00 EST) |
| Amazon GuardDuty | Monitors VPC Flow Logs, CloudTrail, DNS, and EBS (Malware Protection) |

---

## What Gets Created

| Resource | Details |
|---|---|
| VPC | Dedicated `/16` VPC, single public subnet, Internet Gateway |
| Security Group | Inbound SSH (22) and VNC (5901–5910) from `0.0.0.0/0` by default |
| EC2 Instance | `t3.small` (2 vCPU / 2 GiB RAM), Ubuntu 20.04 LTS (x86_64) |
| Root Volume | 16 GiB gp3, encrypted, deleted on termination |
| Key Pair | RSA `.pem` key, private key stored in SSM Parameter Store |
| SSM Patch Manager | Weekly maintenance window (Sundays 02:00 UTC) — applies OS patches and reboots if needed |
| Auto-Stop | EventBridge rule fires daily at **06:00 UTC (01:00 EST)** — Lambda stops all instances tagged `Project=SEEDLabs` |
| GuardDuty | Detector enabled in the deployment region with Malware Protection (EBS scanning) turned on |

On first boot, user-data automatically:
1. Downloads and unpacks `src-cloud.zip` from the SEED CDN
2. Runs `install.sh` non-interactively (pre-seeds Wireshark → No, display manager → LightDM)
3. Creates the `seed` account and sets a VNC password
4. Starts TigerVNC on display `:1` (port `5901`) via a systemd service
5. Ensures the AWS SSM Agent (snap) is installed, enabled, and running

Bootstrap log: `/var/log/seed-bootstrap.log`

---

## Automated Operations

### Weekly OS Patching (SSM Patch Manager)

| Setting | Value |
|---|---|
| Schedule | Every **Sunday at 02:00 UTC** |
| Patch baseline | `AWS-DefaultPatchBaseline` (via `AWS-RunPatchBaseline`) |
| Operation | `Install` — downloads and applies all missing patches |
| Reboot | `RebootIfNeeded` — instance reboots automatically if a patch requires it |
| Target | All instances tagged `Project=SEEDLabs` in this account/region |
| Window duration | 2 hours, with a 1-hour cutoff |

Patch compliance results are visible in the **AWS Systems Manager → Patch Manager** console. The instance role includes both `AmazonSSMManagedInstanceCore` and `AmazonSSMPatchAssociation` managed policies so the SSM Agent can receive and report on patch tasks.

> The patching window (02:00 UTC Sunday) runs several hours **before** the daily auto-stop (06:00 UTC), so patches are fully applied before the instance shuts down.

### Daily Auto-Stop (EventBridge + Lambda)

| Setting | Value |
|---|---|
| Schedule | Every day at **06:00 UTC** |
| Local time | 01:00 EST (UTC-5) / 02:00 EDT (UTC-4) |
| Action | Stops all EC2 instances tagged `Project=SEEDLabs` in `running` or `pending` state |
| Implementation | EventBridge scheduled rule → Python 3.12 Lambda (`seed-lab-auto-stop`) |

This prevents the instance from running overnight and accruing unnecessary compute charges. A stopped instance incurs only EBS storage cost (~$1.28/month for 16 GiB gp3).

To **start the instance** before a lab session:

```bash
aws ec2 start-instances --instance-ids <InstanceId>
```

Or use the AWS Console → EC2 → Instances → Start.

### Amazon GuardDuty

GuardDuty is enabled automatically in the deployment region. It continuously monitors:

| Data source | What it detects |
|---|---|
| VPC Flow Logs | Port scans, unusual outbound traffic, C2 communication |
| AWS CloudTrail | Unusual API calls, credential abuse, privilege escalation |
| DNS query logs | Requests to known malicious domains |
| Malware Protection (EBS) | Malware on the instance's EBS volume (triggered on threat detection) |

Findings are published every **15 minutes** and are visible in the **Amazon GuardDuty** console. High-severity findings can be forwarded to SNS or Security Hub — set that up in the console if alerting is needed.

> GuardDuty adds approximately **$1–3/month** at typical lab-scale traffic volumes (based on ~2 hours of active use per week). See the [GuardDuty pricing page](https://aws.amazon.com/guardduty/pricing/) for details.

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

Based on **2 hours of active use per week** in US East (N. Virginia), with the instance stopped after each session. All prices are approximate on-demand rates.

### Weekly cost breakdown

| Item | Rate | Weekly usage | Weekly cost |
|---|---|---|---|
| t3.small compute (running) | $0.0208/hr | 2 hrs | ~$0.04 |
| t3.small compute (stopped) | $0.00/hr | 166 hrs | $0.00 |
| 16 GiB gp3 EBS storage | $0.08/GB-month | 16 GiB × 7/30 mo | ~$0.30 |
| Data transfer (est.) | variable | light lab use | ~$0.05 |
| GuardDuty (est.) | ~$1–3/month | 7/30 month | ~$0.25 |
| Lambda auto-stop (7 invocations) | negligible | 7 × <1ms | ~$0.00 |
| SSM Patch Manager | free for EC2 | — | $0.00 |
| **Weekly total** | | | **~$0.64** |
| **Monthly total** | | | **~$2.75** |

> GuardDuty cost scales with CloudTrail events and VPC Flow Log volume. At low lab-scale usage the $1–3/month figure is conservative. See the [GuardDuty pricing calculator](https://aws.amazon.com/guardduty/pricing/) to model your specific usage.

**Key savings from auto-stop:** Without the 01:00 EST auto-stop, a forgotten running instance would cost ~$15/month in compute alone. With auto-stop the worst-case daily exposure is the cost of one running session before the cutoff fires.

---

## Teardown

To delete all resources (instance, VPC, key pair, security group, maintenance window, Lambda, EventBridge rule, GuardDuty detector):

```powershell
cdk destroy
```

> The root EBS volume is set to `delete_on_termination=True`, so it will be removed automatically.
>
> GuardDuty findings and detector history are **permanently deleted** when the detector is removed. Export findings to S3 first if you need to retain them.

---

## Project Layout

```
uww-seed-lab-cdk/
├── app.py                      # CDK app entry point
├── cdk.json                    # CDK configuration
├── requirements.txt            # Python dependencies
├── docs/
│   └── architecture.drawio    # draw.io architecture diagram
└── seed_lab/
    ├── __init__.py
    ├── seed_lab_stack.py       # CDK stack (VPC, SG, EC2, Patch Manager,
    │                           #   Auto-Stop Lambda, GuardDuty, outputs)
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

**Instance stopped unexpectedly at night**
This is expected — the auto-stop Lambda fires every day at 06:00 UTC (01:00 EST). Start it again with:
```bash
aws ec2 start-instances --instance-ids <InstanceId>
```

**Patching status / compliance**
Open the AWS Console → Systems Manager → Patch Manager → Compliance. Filter by instance ID or tag `Project=SEEDLabs` to see which patches were applied and when.

**SSM Agent not reachable**
The bootstrap script installs and starts the snap-based SSM Agent. If Patch Manager reports the instance as not managed, SSH in and check:
```bash
snap services amazon-ssm-agent
sudo snap restart amazon-ssm-agent
```

**GuardDuty findings**
Open the AWS Console → Amazon GuardDuty → Findings. Findings are published every 15 minutes. For a new deployment, initial population takes a few minutes.

**AMI not found in my region**
Add your region's Ubuntu 20.04 AMI ID to the `UBUNTU_2004_AMI` dict in `seed_lab/seed_lab_stack.py`, or let CDK do a dynamic lookup (requires network access during `cdk synth`).

**SSH key permissions error on Windows**
PowerShell's `icacls` can fix this:
```powershell
icacls seed-lab-key.pem /inheritance:r /grant:r "$($env:USERNAME):(R)"
```
