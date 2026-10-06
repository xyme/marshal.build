#!/usr/bin/env bash
# Authorizes the current workstation IP on the dev RDS security group.
# Dev IPs drift (VPN/ISP) — run this whenever DB connections start timing out.
set -euo pipefail
export AWS_REGION=${AWS_REGION:-us-east-1}

MY_IP=$(curl -s https://checkip.amazonaws.com)
SG_ID=$(aws ec2 describe-security-groups --region "$AWS_REGION" \
  --filters "Name=group-name,Values=*DbSecurityGroup*" \
  --query "SecurityGroups[0].GroupId" --output text)

echo "Authorizing $MY_IP/32 on $SG_ID …"
if aws ec2 authorize-security-group-ingress --region "$AWS_REGION" \
  --group-id "$SG_ID" --protocol tcp --port 5432 --cidr "$MY_IP/32" 2>/dev/null; then
  echo "added"
else
  echo "already present"
fi
