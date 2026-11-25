"""Plan read access to the log groups configured for a Glue job."""
from __future__ import annotations

import json
import re

from .passrole import plan_inline_policy

_LOG_GROUP_NAME = re.compile(r"[A-Za-z0-9_./#-]{1,512}")


def plan(session, iam, glue, job: dict, job_arn: str, pass_role: dict) -> dict:
    _, partition, _, region, account, _ = job_arn.split(":", 5)
    project_role = pass_role["projectRoleArn"]
    if not project_role.startswith(f"arn:{partition}:iam::{account}:role/"):
        raise ValueError("Project role must be in the Glue job's account")

    arguments = {
        **(job.get("DefaultArguments") or {}),
        **(job.get("NonOverridableArguments") or {}),
    }
    shell = job["Command"]["Name"] == "pythonshell"
    version = job.get("GlueVersion") or "0"
    if version == "auto":
        upgrade = job.get("AutoUpgradeMetadata") or {}
        target = upgrade.get("TargetVersion") or "6.0"
        versions = (upgrade.get("SourceVersion") or target, target)
    else:
        versions = (version,)
    major_versions = set() if shell else {int(v.split(".", 1)[0]) for v in versions}
    base = "/aws-glue/python-jobs" if shell else "/aws-glue/jobs"
    modern = any(v >= 5 for v in major_versions)
    legacy = any(v < 5 for v in major_versions)
    bases = {base} if shell or legacy else set()
    if modern:
        bases.add(arguments.get("--custom-logGroup-prefix") or base)

    security = job.get("SecurityConfiguration")
    key_arn = None
    if security:
        config = glue.get_security_configuration(Name=security)["SecurityConfiguration"]
        encryption = config["EncryptionConfiguration"].get("CloudWatchEncryption") or {}
        if encryption.get("CloudWatchEncryptionMode") == "SSE-KMS":
            key_arn = encryption.get("KmsKeyArn") or ""
            key_prefix = rf"arn:{re.escape(partition)}:kms:{re.escape(region)}:[0-9]{{12}}:"
            if re.match(key_prefix + r"alias/aws/", key_arn):
                # DescribeKey can provision a key for an unbound AWS-managed alias.
                raise ValueError("CloudWatch log encryption requires a customer-managed KMS key ARN")
            if re.fullmatch(key_prefix + r"alias/[A-Za-z0-9/_-]+", key_arn):
                kms = session.client("kms", region_name=region)
                key_arn = kms.describe_key(KeyId=key_arn)["KeyMetadata"]["Arn"]
            if not re.fullmatch(key_prefix + r"key/[A-Za-z0-9-]+", key_arn):
                raise ValueError("CloudWatch log encryption requires an exact KMS key ARN in the Glue Region")
        else:
            security = None

    if security:
        role_arn = pass_role["policyDocument"]["Statement"][0]["Resource"]
        bases = {f"{base}/{security}-role/{role_arn.rsplit('/', 1)[-1]}" for base in bases}

    groups = {f"{base}/{kind}" for base in bases for kind in ("error", "output")}
    continuous_enabled = arguments.get("--enable-continuous-cloudwatch-log", "false").lower() in ("", "true")
    insights_enabled = bool(major_versions & {2, 3}) and (
        arguments.get("--enable-job-insights", "true").lower() in ("", "true")
    )
    if legacy and (continuous_enabled or insights_enabled):
        custom = arguments.get("--continuous-log-logGroup")
        continuous = custom or "/aws-glue/jobs/logs-v2"
        groups.add(f"{continuous}-{security}" if security else continuous)
        if security and not custom:
            # Glue's legacy logging guide also names this encrypted default group.
            groups.add(f"/aws-glue/jobs/error-{security}")

    for group in groups:
        if not _LOG_GROUP_NAME.fullmatch(group):
            raise ValueError(f"Invalid CloudWatch log group: {group!r}")

    group_arns = [f"arn:{partition}:logs:{region}:{account}:log-group:{group}" for group in sorted(groups)]
    resources = [f"{arn}:log-stream:*" for arn in group_arns]
    statements = [{"Effect": "Allow", "Action": "logs:GetLogEvents", "Resource": resources}]
    if key_arn:
        statements.append({
            "Effect": "Allow", "Action": "kms:Decrypt", "Resource": key_arn,
            "Condition": {
                "StringEquals": {"kms:ViaService": f"logs.{region}.amazonaws.com"},
                "ArnEquals": {"kms:EncryptionContext:aws:logs:arn": group_arns},
            },
        })
    scope = json.dumps([resources, key_arn] if key_arn else resources)
    return plan_inline_policy(iam, project_role, "SMUSGlueJobLogRead", scope, statements)
