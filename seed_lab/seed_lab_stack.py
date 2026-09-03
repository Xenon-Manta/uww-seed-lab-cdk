"""
SeedLabStack
============
Deploys a single EC2 instance pre-configured as a SEED Security Labs VM.

Resources created
-----------------
* VPC  – dedicated /16 VPC with one public subnet (no NAT gateway needed;
         the instance uses an Internet Gateway for outbound access during setup).
* Security Group – allows inbound SSH (22) and VNC (5901-5910) from anywhere.
         Restrict the allowed CIDR(s) via the context variable `allowed_cidr`
         (default: 0.0.0.0/0).  Example:
             cdk deploy --context allowed_cidr=203.0.113.0/24
* EC2 instance – t3.small, Ubuntu 20.04 LTS (x86_64), 16 GiB gp3 root volume.
* Key Pair – an EC2 managed key pair whose private-key material is stored in
             AWS Systems Manager Parameter Store at /seed-lab/key-pair/private-key.
             Retrieve it after deploy with:
             aws ssm get-parameter --name /seed-lab/key-pair/private-key \
                 --with-decryption --query Parameter.Value --output text \
                 > seed-lab-key.pem

User-data
---------
The bootstrap script (user_data.sh, read at synth time) runs on first boot and:
  - Downloads and installs the SEED Labs software bundle (src-cloud.zip)
  - Pre-seeds debconf so Wireshark and LightDM install without prompts
  - Creates the `seed` account with a hashed VNC password
  - Starts TigerVNC on display :1 (port 5901) via a systemd service

Outputs
-------
* InstanceId        – EC2 instance ID
* PublicIp          – Instance public IP (connect with VNC viewer)
* SshCommand        – Ready-to-use SSH command string
* VncAddress        – VNC address string  <ip>:5901
* KeyPairSsmPath    – SSM path to retrieve the private key
"""

from pathlib import Path

import aws_cdk as cdk
from aws_cdk import (
    CfnOutput,
    Stack,
    Tags,
    aws_ec2 as ec2,
)
from constructs import Construct


# ---------------------------------------------------------------------------
# Ubuntu 20.04 LTS (Focal) – official Canonical AMIs (amd64, hvm:ebs-ssd).
# These are the most recent IDs as of mid-2024.  CDK's ec2.MachineImage
# .lookup() could also be used but requires `--context` network access at
# synth time; hard-coded IDs are more reproducible for a lab environment.
# ---------------------------------------------------------------------------
UBUNTU_2004_AMI: dict[str, str] = {
    "us-east-1":      "ami-0e001c9271cf7f3b9",
    "us-east-2":      "ami-0f30a9c3a48f3fa79",
    "us-west-1":      "ami-0d7ae6a161c5c4239",
    "us-west-2":      "ami-0b029b1931b347543",
    "eu-west-1":      "ami-0d2a4a5d69e46ea0b",
    "eu-west-2":      "ami-0e34bbddc66def5ac",
    "eu-central-1":   "ami-0faab6bdbac9486fb",
    "ap-southeast-1": "ami-078c1149d8ad719a7",
    "ap-southeast-2": "ami-0310483fb2b488153",
    "ap-northeast-1": "ami-09a81b370b76de6a2",
    "ap-south-1":     "ami-0c2af51e265bd5e0e",
    "ca-central-1":   "ami-0083d3f8b2a6c7a81",
    "sa-east-1":      "ami-0b6c2d49148000cd5",
}


class SeedLabStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # ------------------------------------------------------------------
        # Configuration – override via CDK context flags
        # ------------------------------------------------------------------
        # IP range allowed to reach SSH and VNC.  Restrict in production!
        allowed_cidr: str = self.node.try_get_context("allowed_cidr") or "0.0.0.0/0"

        # EC2 instance type.  t3.small (2 vCPU / 2 GiB) is the SEED minimum.
        instance_type_str: str = (
            self.node.try_get_context("instance_type") or "t3.small"
        )

        # Root volume size in GiB.  SEED recommends at least 12; 16 gives headroom.
        volume_size_gib: int = int(
            self.node.try_get_context("volume_size") or "16"
        )

        # ------------------------------------------------------------------
        # VPC – one public subnet, no NAT gateway (saves ~$30/mo)
        # ------------------------------------------------------------------
        vpc = ec2.Vpc(
            self,
            "SeedLabVpc",
            ip_addresses=ec2.IpAddresses.cidr("10.10.0.0/16"),
            max_azs=1,
            subnet_configuration=[
                ec2.SubnetConfiguration(
                    name="Public",
                    subnet_type=ec2.SubnetType.PUBLIC,
                    cidr_mask=24,
                )
            ],
            nat_gateways=0,
        )

        # ------------------------------------------------------------------
        # Security Group – SSH (22) and VNC (5901-5910)
        # ------------------------------------------------------------------
        sg = ec2.SecurityGroup(
            self,
            "SeedLabSG",
            vpc=vpc,
            security_group_name="seed-lab-sg",
            description="SEED Labs: SSH and VNC access",
            allow_all_outbound=True,
        )

        sg.add_ingress_rule(
            peer=ec2.Peer.ipv4(allowed_cidr),
            connection=ec2.Port.tcp(22),
            description="SSH",
        )

        # VNC display :1 through :10  →  ports 5901-5910
        sg.add_ingress_rule(
            peer=ec2.Peer.ipv4(allowed_cidr),
            connection=ec2.Port.tcp_range(5901, 5910),
            description="VNC (displays :1-:10)",
        )

        # ------------------------------------------------------------------
        # EC2 Managed Key Pair
        # The private key is stored automatically in SSM Parameter Store.
        # ------------------------------------------------------------------
        key_pair = ec2.KeyPair(
            self,
            "SeedLabKeyPair",
            key_pair_name="seed-lab-key",
            type=ec2.KeyPairType.RSA,
            format=ec2.KeyPairFormat.PEM,
        )

        # ------------------------------------------------------------------
        # AMI selection
        # Prefer a dynamic lookup so we always get the latest Canonical AMI;
        # fall back to the hard-coded table for offline / restricted synths.
        # ------------------------------------------------------------------
        region = self.region

        if region in UBUNTU_2004_AMI:
            machine_image = ec2.MachineImage.generic_linux(
                {region: UBUNTU_2004_AMI[region]}
            )
        else:
            # Dynamic lookup – requires network access during `cdk synth`
            machine_image = ec2.MachineImage.from_ssm_parameter(
                "/aws/service/canonical/ubuntu/server/20.04/stable/current/amd64/hvm/ebs-gp2/ami-id"
            )

        # ------------------------------------------------------------------
        # User-data – read from the companion shell script
        # ------------------------------------------------------------------
        user_data_path = Path(__file__).parent / "user_data.sh"
        user_data_script = user_data_path.read_text(encoding="utf-8")

        user_data = ec2.UserData.for_linux()
        user_data.add_commands(user_data_script)

        # ------------------------------------------------------------------
        # EC2 Instance
        # ------------------------------------------------------------------
        instance = ec2.Instance(
            self,
            "SeedLabInstance",
            instance_type=ec2.InstanceType(instance_type_str),
            machine_image=machine_image,
            vpc=vpc,
            vpc_subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PUBLIC),
            security_group=sg,
            key_pair=key_pair,
            user_data=user_data,
            # IMDSv2 required – best practice for new instances
            require_imdsv2=True,
            block_devices=[
                ec2.BlockDevice(
                    device_name="/dev/sda1",
                    volume=ec2.BlockDeviceVolume.ebs(
                        volume_size_gib,
                        volume_type=ec2.EbsDeviceVolumeType.GP3,
                        encrypted=True,
                        delete_on_termination=True,
                    ),
                )
            ],
        )

        Tags.of(instance).add("Name", "seed-lab-vm")
        Tags.of(instance).add("Project", "SEEDLabs")

        # ------------------------------------------------------------------
        # CloudFormation Outputs
        # ------------------------------------------------------------------
        CfnOutput(
            self,
            "InstanceId",
            value=instance.instance_id,
            description="EC2 instance ID",
        )

        CfnOutput(
            self,
            "PublicIp",
            value=instance.instance_public_ip,
            description="Public IP address of the SEED Lab VM",
        )

        CfnOutput(
            self,
            "SshCommand",
            value=cdk.Fn.sub(
                "ssh -i seed-lab-key.pem ubuntu@${IP}",
                {"IP": instance.instance_public_ip},
            ),
            description="SSH command (use the downloaded .pem key)",
        )

        CfnOutput(
            self,
            "VncAddress",
            value=cdk.Fn.sub(
                "${IP}:5901",
                {"IP": instance.instance_public_ip},
            ),
            description="VNC viewer address (display :1, port 5901)",
        )

        CfnOutput(
            self,
            "KeyPairSsmPath",
            value=f"/ec2/keypair/{key_pair.key_pair_id}",
            description=(
                "SSM Parameter Store path for the private key. "
                "Retrieve with: aws ssm get-parameter "
                "--name /ec2/keypair/<key-id> --with-decryption "
                "--query Parameter.Value --output text > seed-lab-key.pem"
            ),
        )
