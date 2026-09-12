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
* IAM Role – instance profile with AmazonSSMManagedInstanceCore and
             AmazonSSMPatchAssociation, which allows AWS Systems Manager
             Session Manager and Patch Manager to manage the instance.
* Key Pair – an EC2 managed key pair whose private-key material is stored in
             AWS Systems Manager Parameter Store at /seed-lab/key-pair/private-key.
             Retrieve it after deploy with:
             aws ssm get-parameter --name /seed-lab/key-pair/private-key \
                 --with-decryption --query Parameter.Value --output text \
                 > seed-lab-key.pem
* SSM Patch Manager – a maintenance window runs every Sunday at 02:00 UTC
             and applies the AWS-DefaultPatchBaseline to all instances tagged
             Project=SEEDLabs.  Results are visible in the SSM Patch Manager
             console.
* Auto-stop – an EventBridge scheduled rule fires every day at 06:00 UTC
             (01:00 EST / 02:00 EDT).  A Lambda function (Python 3.12) calls
             ec2:StopInstances targeting the instance by tag (Project=SEEDLabs).
             This ensures the instance does not run overnight and accrue costs.
* GuardDuty – a GuardDuty detector is enabled in the deployment region to
             monitor for malicious activity, unusual API calls, and potential
             compromises of the EC2 instance and associated AWS account.

User-data
---------
The bootstrap script (user_data.sh, read at synth time) runs on first boot and:
  - Downloads and installs the SEED Labs software bundle (src-cloud.zip)
  - Pre-seeds debconf so Wireshark and LightDM install without prompts
  - Creates the `seed` account with a hashed VNC password
  - Starts TigerVNC on display :1 (port 5901) via a systemd service
  - Ensures the SSM Agent is enabled and running for Patch Manager

Outputs
-------
* InstanceId          – EC2 instance ID
* PublicIp            – Instance public IP (connect with VNC viewer)
* SshCommand          – Ready-to-use SSH command string
* VncAddress          – VNC address string  <ip>:5901
* KeyPairSsmPath      – SSM path to retrieve the private key
* SsmSessionCommand   – Command to open an SSM Session Manager shell
* GuardDutyDetectorId – GuardDuty detector ID for this region
"""

import json
from pathlib import Path

import aws_cdk as cdk
from aws_cdk import (
    CfnOutput,
    Duration,
    Stack,
    Tags,
    aws_ec2 as ec2,
    aws_events as events,
    aws_events_targets as targets,
    aws_guardduty as guardduty,
    aws_iam as iam,
    aws_lambda as lambda_,
    aws_ssm as ssm,
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
        # Security Group – SSH (22) and VNC (5901-5910) - Disabled By Default!!
        # ------------------------------------------------------------------
        sg = ec2.SecurityGroup(
            self,
            "SeedLabSG",
            vpc=vpc,
            description="SEED Labs: SSH and VNC access",
            allow_all_outbound=True,
        )

        #sg.add_ingress_rule(
        #    peer=ec2.Peer.ipv4(allowed_cidr),
        #    connection=ec2.Port.tcp(22),
        #    description="SSH",
        #)

        # VNC display :1 through :10  →  ports 5901-5910
        #sg.add_ingress_rule(
        #    peer=ec2.Peer.ipv4(allowed_cidr),
        #    connection=ec2.Port.tcp_range(5901, 5910),
        #    description="VNC (displays :1-:10)",
        #)

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
        # IAM Role – grants the instance permission to:
        #   * Register with SSM (Session Manager + Patch Manager)
        #   * Receive patch associations from SSM Patch Manager
        # ------------------------------------------------------------------
        role = iam.Role(
            self,
            "SeedLabInstanceRole",
            assumed_by=iam.ServicePrincipal("ec2.amazonaws.com"),
            managed_policies=[
                iam.ManagedPolicy.from_aws_managed_policy_name(
                    "AmazonSSMManagedInstanceCore"
                ),
                # Required for Patch Manager to apply patches and write
                # compliance data back to SSM.
                iam.ManagedPolicy.from_aws_managed_policy_name(
                    "AmazonSSMPatchAssociation"
                ),
            ],
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
            role=role,
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

        # ==================================================================
        # SSM PATCH MANAGER – weekly maintenance window
        # ==================================================================
        # Maintenance window: every Sunday at 02:00 UTC, 2-hour cutoff.
        # This is off-peak time globally and well before the 06:00 UTC
        # auto-stop fires, so patching completes before the instance stops.
        maintenance_window = ssm.CfnMaintenanceWindow(
            self,
            "SeedLabPatchWindow",
            name="seed-lab-weekly-patch",
            description="Weekly OS patching for SEED Lab VM (Sundays 02:00 UTC)",
            # cron: minute hour day-of-month month day-of-week year
            schedule="cron(0 2 ? * SUN *)",
            duration=2,           # window lasts up to 2 hours
            cutoff=1,             # stop scheduling new tasks 1 hour before end
            allow_unassociated_targets=False,
        )

        # Target: all instances tagged Project=SEEDLabs in this account/region.
        patch_target = ssm.CfnMaintenanceWindowTarget(
            self,
            "SeedLabPatchTarget",
            window_id=maintenance_window.ref,
            resource_type="INSTANCE",
            targets=[
                ssm.CfnMaintenanceWindowTarget.TargetsProperty(
                    key="tag:Project",
                    values=["SEEDLabs"],
                )
            ],
            name="seed-lab-instances",
            description="SEED Lab VM instances (tag Project=SEEDLabs)",
        )

        # IAM role that SSM Maintenance Windows assumes to run patch tasks.
        # AmazonSSMMaintenanceWindowRole was retired by AWS; the current
        # best-practice is an inline policy scoped to exactly what the
        # maintenance window task needs:
        #   - Send the RunPatchBaseline command to the instance
        #   - Read instance/command status (required by the task runner)
        #   - Write patch compliance results back to SSM
        #   - Write task run logs to CloudWatch Logs
        patch_task_role = iam.Role(
            self,
            "SeedLabPatchTaskRole",
            assumed_by=iam.ServicePrincipal("ssm.amazonaws.com"),
            inline_policies={
                "SeedLabPatchTaskPolicy": iam.PolicyDocument(
                    statements=[
                        iam.PolicyStatement(
                            sid="SendRunCommand",
                            actions=["ssm:SendCommand"],
                            resources=[
                                # Allow sending the patch document to any instance
                                f"arn:aws:ec2:{self.region}:{self.account}:instance/*",
                                # The AWS-RunPatchBaseline document itself
                                f"arn:aws:ssm:{self.region}::document/AWS-RunPatchBaseline",
                            ],
                        ),
                        iam.PolicyStatement(
                            sid="MonitorCommand",
                            actions=[
                                "ssm:ListCommands",
                                "ssm:ListCommandInvocations",
                                "ssm:GetCommandInvocation",
                                "ssm:DescribeInstanceInformation",
                            ],
                            resources=["*"],
                        ),
                        iam.PolicyStatement(
                            sid="WritePatchCompliance",
                            actions=[
                                "ssm:PutComplianceItems",
                                "ssm:GetDefaultPatchBaseline",
                                "ssm:DescribePatchBaseline",
                            ],
                            resources=["*"],
                        ),
                        iam.PolicyStatement(
                            sid="WriteTaskLogs",
                            actions=[
                                "logs:CreateLogGroup",
                                "logs:CreateLogStream",
                                "logs:PutLogEvents",
                                "logs:DescribeLogGroups",
                                "logs:DescribeLogStreams",
                            ],
                            resources=["*"],
                        ),
                    ]
                )
            },
        )

        # Task: run AWS-RunPatchBaseline with Install operation.
        ssm.CfnMaintenanceWindowTask(
            self,
            "SeedLabPatchTask",
            window_id=maintenance_window.ref,
            task_arn="AWS-RunPatchBaseline",
            task_type="RUN_COMMAND",
            priority=1,
            max_concurrency="1",
            max_errors="1",
            service_role_arn=patch_task_role.role_arn,
            targets=[
                ssm.CfnMaintenanceWindowTask.TargetProperty(
                    key="WindowTargetIds",
                    values=[patch_target.ref],
                )
            ],
            task_invocation_parameters=ssm.CfnMaintenanceWindowTask.TaskInvocationParametersProperty(
                maintenance_window_run_command_parameters=ssm.CfnMaintenanceWindowTask.MaintenanceWindowRunCommandParametersProperty(
                    parameters={
                        # Install mode: download and apply missing patches.
                        "Operation": ["Install"],
                        # Reboot if required by the patches.
                        "RebootOption": ["RebootIfNeeded"],
                    },
                    timeout_seconds=3600,
                )
            ),
        )

        # ==================================================================
        # AUTO-STOP LAMBDA – stops the VM every day at 06:00 UTC (01:00 EST)
        # ==================================================================
        # Inline Lambda: small enough to keep the stack self-contained.
        auto_stop_code = lambda_.Code.from_inline(
            "\n".join([
                "import boto3, os",
                "",
                "ec2_client = boto3.client('ec2')",
                "",
                "def handler(event, context):",
                "    tag_key   = os.environ.get('TAG_KEY',   'Project')",
                "    tag_value = os.environ.get('TAG_VALUE', 'SEEDLabs')",
                "    paginator = ec2_client.get_paginator('describe_instances')",
                "    instance_ids = []",
                "    pages = paginator.paginate(",
                "        Filters=[",
                "            {'Name': f'tag:{tag_key}', 'Values': [tag_value]},",
                "            {'Name': 'instance-state-name', 'Values': ['running', 'pending']},",
                "        ]",
                "    )",
                "    for page in pages:",
                "        for reservation in page['Reservations']:",
                "            for inst in reservation['Instances']:",
                "                instance_ids.append(inst['InstanceId'])",
                "    if instance_ids:",
                "        print(f'Stopping instances: {instance_ids}')",
                "        ec2_client.stop_instances(InstanceIds=instance_ids)",
                "    else:",
                "        print('No running instances found with the specified tag — nothing to stop.')",
                "    return {'stopped': instance_ids}",
            ])
        )

        auto_stop_fn = lambda_.Function(
            self,
            "SeedLabAutoStopFn",
            function_name="seed-lab-auto-stop",
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="index.handler",
            code=auto_stop_code,
            timeout=Duration.seconds(30),
            description=(
                "Stops all EC2 instances tagged Project=SEEDLabs. "
                "Triggered daily at 06:00 UTC (01:00 EST / 02:00 EDT)."
            ),
            environment={
                "TAG_KEY":   "Project",
                "TAG_VALUE": "SEEDLabs",
            },
        )

        # Grant the Lambda permission to describe and stop EC2 instances that
        # carry the Project=SEEDLabs tag.
        auto_stop_fn.add_to_role_policy(
            iam.PolicyStatement(
                sid="DescribeInstances",
                actions=["ec2:DescribeInstances"],
                resources=["*"],  # DescribeInstances does not support resource-level restriction
            )
        )
        auto_stop_fn.add_to_role_policy(
            iam.PolicyStatement(
                sid="StopTaggedInstances",
                actions=["ec2:StopInstances"],
                resources=["*"],
                conditions={
                    "StringEquals": {
                        "ec2:ResourceTag/Project": "SEEDLabs",
                    }
                },
            )
        )

        # EventBridge rule: fire every day at 06:00 UTC.
        # 06:00 UTC = 01:00 EST (UTC-5) = 02:00 EDT (UTC-4).
        auto_stop_rule = events.Rule(
            self,
            "SeedLabAutoStopRule",
            rule_name="seed-lab-auto-stop",
            description="Stop SEED Lab VM daily at 06:00 UTC (01:00 EST)",
            schedule=events.Schedule.cron(
                minute="0",
                hour="6",
                month="*",
                week_day="*",
                year="*",
            ),
        )
        auto_stop_rule.add_target(targets.LambdaFunction(auto_stop_fn))

        # ==================================================================
        # AMAZON GUARDDUTY – threat detection for the account/region
        # ==================================================================
        # Enabling GuardDuty at the detector level covers the entire AWS
        # account in this region: EC2 network flows (VPC Flow Logs),
        # CloudTrail management events, DNS query logs, and — with the
        # additional data sources enabled below — S3 data events and
        # EKS audit logs if those services are used.
        guardduty_detector = guardduty.CfnDetector(
            self,
            "SeedLabGuardDuty",
            enable=True,
            # FIFTEEN_MINUTES gives near-real-time alerting; use SIX_HOURS
            # to reduce costs if high-frequency alerting is not needed.
            finding_publishing_frequency="FIFTEEN_MINUTES",
            # Enable the Malware Protection data source so EBS volumes
            # on the instance are scanned if a threat is detected.
            data_sources=guardduty.CfnDetector.CFNDataSourceConfigurationsProperty(
                malware_protection=guardduty.CfnDetector.CFNMalwareProtectionConfigurationProperty(
                    scan_ec2_instance_with_findings=guardduty.CfnDetector.CFNScanEc2InstanceWithFindingsConfigurationProperty(
                        ebs_volumes=True,
                    )
                )
            ),
        )

        Tags.of(guardduty_detector).add("Project", "SEEDLabs")

        # ==================================================================
        # CloudFormation Outputs
        # ==================================================================
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

        CfnOutput(
            self,
            "SsmSessionCommand",
            value=cdk.Fn.sub(
                "aws ssm start-session --target ${InstanceId}",
                {"InstanceId": instance.instance_id},
            ),
            description=(
                "Start an SSM Session Manager shell session. "
                "Requires the AWS CLI and the Session Manager plugin installed locally."
            ),
        )

        CfnOutput(
            self,
            "GuardDutyDetectorId",
            value=guardduty_detector.ref,
            description=(
                "GuardDuty detector ID for this region. "
                "View findings at: https://console.aws.amazon.com/guardduty/"
            ),
        )
