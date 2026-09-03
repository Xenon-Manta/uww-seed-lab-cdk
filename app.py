#!/usr/bin/env python3
"""
Entry point for the SEED Labs EC2 CDK application.
Deploy with: cdk deploy
"""
import os
import aws_cdk as cdk
from seed_lab.seed_lab_stack import SeedLabStack

app = cdk.App()

SeedLabStack(
    app,
    "SeedLabStack",
    # Uncomment and set these if you want to deploy to a specific account/region.
    # Otherwise CDK will use your default AWS CLI credentials.
    # env=cdk.Environment(account="123456789012", region="us-east-1"),
    env=cdk.Environment(
        account=os.environ.get("CDK_DEFAULT_ACCOUNT"),
        region=os.environ.get("CDK_DEFAULT_REGION"),
    ),
)

app.synth()
