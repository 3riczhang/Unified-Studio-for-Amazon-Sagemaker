"""Verify migrated logs using the SMUS project's actual credentials."""
from contextlib import ExitStack
import os
import uuid

import pytest
from botocore.exceptions import ClientError

from e2e.project_fixture import (
    create_project, execution_policy_arn, ignore_missing, project_member, project_session, wait_for,
)
from migration.glue import datazone_resolve, migrate, naming
from migration.glue.session import datazone_client


pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(not os.environ.get("GS_SMUS_E2E_LOG_PROJECT_PROFILE_ID")
                       or not os.environ.get("GS_SMUS_E2E_LOG_KMS_KEY_ARN"),
                       reason="configure a disposable log-test project profile and fixture KMS key"),
]
SCRIPT = """\
from awsglue.context import GlueContext
from pyspark.context import SparkContext

glue = GlueContext(SparkContext.getOrCreate())
print("E2E_STDOUT_MARKER", flush=True)
glue.get_logger().warn("E2E_GLUE_LOGGER_MARKER")
"""


@pytest.fixture(scope="session")
def log_project(admin, runner, cfg):
    domain = cfg["domain_id"]
    domain_region = cfg["domain_region"] or cfg["region"]
    dz = datazone_client(admin, domain_region)
    iam, s3 = admin.client("iam"), admin.client("s3")
    identity = admin.client("sts").get_caller_identity()
    account = identity["Account"]
    partition = identity["Arn"].split(":")[1]
    key_prefix = f"arn:{partition}:kms:{cfg['region']}:{account}:key/"
    assert os.environ["GS_SMUS_E2E_LOG_KMS_KEY_ARN"].startswith(key_prefix), (
        "Use a customer-managed test key ARN in the Glue account and Region")
    assert dz.get_domain(identifier=domain)["arn"].split(":")[4] == account, (
        "Use a log-test domain in the Glue fixture account")
    profile_id = os.environ["GS_SMUS_E2E_LOG_PROJECT_PROFILE_ID"]
    profile = dz.get_project_profile(domainIdentifier=domain, identifier=profile_id)
    configs = [entry for entry in profile["environmentConfigurations"] if entry["deploymentMode"] == "ON_CREATE"]
    assert len(configs) == 1 and configs[0]["name"] == "ToolingLite", (
        "Use a profile that deploys only ToolingLite on creation")
    assert (configs[0]["awsAccount"].get("awsAccountId"), configs[0]["awsRegion"].get("regionName")) == (
        account, cfg["region"],
    ), "The ToolingLite profile must deploy to the Glue account and Region"
    runner_identity = runner.client("sts").get_caller_identity()
    assert runner_identity["Account"] == account, "The log migration runner must be in the fixture account"
    members = [] if runner_identity["UserId"].split(":")[0] == identity["UserId"].split(":")[0] else [
        project_member(dz, iam, domain, runner_identity["Arn"]),
    ]
    name = f"e2e-log-{uuid.uuid4().hex[:12]}"

    def delete_project_bucket(project_id):
        # ToolingLite's default bucket is project-specific and can survive project deletion.
        bucket = f"amazon-sagemaker-{account}-{cfg['region']}-{project_id}"
        ignore_missing(_delete_retained_bucket, "NoSuchBucket", s3=s3, bucket=bucket, account=account)

    with ExitStack() as cleanup:
        print(f"Creating disposable log project and role {name}", flush=True)
        project = create_project(cleanup, iam, dz, name=name, partition=partition,
                                 account=account, domain=domain, profile=profile_id, members=members,
                                 after_delete=delete_project_bucket)
        print(f"Disposable log project {project['id']} and role {project['roleArn']}", flush=True)

        def ready():
            current = dz.get_project(domainIdentifier=domain, identifier=project["id"])
            deployment = current.get("environmentDeploymentDetails", {})
            status = deployment.get("overallDeploymentStatus")
            assert status not in ("FAILED_VALIDATION", "FAILED_DEPLOYMENT"), deployment
            return current["projectStatus"] == "ACTIVE" and status == "SUCCESSFUL"

        wait_for(ready, f"project {project['id']} deployment", timeout=600)
        connection = datazone_resolve.resolve_project_iam_connection(dz, domain, project["id"])
        assert connection, "Disposable project has no default IAM connection"
        bucket = datazone_resolve.resolve_project_bucket(dz, domain, project["id"])
        assert bucket == f"amazon-sagemaker-{account}-{cfg['region']}-{project['id']}", (
            "Use ToolingLite's default disposable project bucket")
        # ToolingLite needs this policy to provision. Its broad log reads mask the migration grant.
        role_name = project["roleArn"].rsplit("/", 1)[-1]
        iam.detach_role_policy(RoleName=role_name, PolicyArn=execution_policy_arn(partition))
        cleanup.callback(iam.attach_role_policy, RoleName=role_name, PolicyArn=execution_policy_arn(partition))
        yield project, connection, bucket


def _markers(logs, key_arn, candidates):
    """Find the real stream as setup, independently of the tool's policy plan."""
    def find():
        for group, stream_prefix, marker in candidates:
            groups = logs.describe_log_groups(logGroupNamePrefix=group)["logGroups"]
            exact = next((item for item in groups if item["logGroupName"] == group), None)
            if not exact:
                continue
            for page in logs.get_paginator("describe_log_streams").paginate(
                logGroupName=group, logStreamNamePrefix=stream_prefix,
            ):
                for stream in page["logStreams"]:
                    request = {"logGroupName": group, "logStreamName": stream["logStreamName"],
                               "startFromHead": True}
                    if _has_marker(logs, request, marker):
                        assert exact.get("kmsKeyId") == key_arn, f"Unexpected KMS key on {group}"
                        return request, marker
        return None

    return wait_for(find, f"marker {candidates[0][2]} in a KMS-encrypted stream")


def _has_marker(logs, request, marker):
    token_args = {}
    while True:
        page = logs.get_log_events(**request, **token_args)
        if any(marker in event["message"] for event in page["events"]):
            return True
        token = page["nextForwardToken"]
        if token == token_args.get("nextToken"):
            return False
        token_args = {"nextToken": token}


def _delete_retained_bucket(s3, bucket, account):
    for page in s3.get_paginator("list_object_versions").paginate(
        Bucket=bucket, ExpectedBucketOwner=account, Prefix="shared/",
    ):
        for entry in page.get("Versions", []) + page.get("DeleteMarkers", []):
            if entry["Key"] in ("shared/", "shared/.libs.json"):
                s3.delete_object(Bucket=bucket, ExpectedBucketOwner=account,
                                 Key=entry["Key"], VersionId=entry["VersionId"])
    s3.delete_bucket(Bucket=bucket, ExpectedBucketOwner=account)


def _delete_migration_objects(s3, bucket, domain, project, job):
    prefixes = (
        naming.canonical_script_key(domain, project, job),
        f"{naming.project_root_prefix(domain, project)}/{naming.SNAPSHOTS_PREFIX}/{job}/",
    )
    for prefix in prefixes:
        for page in s3.get_paginator("list_object_versions").paginate(Bucket=bucket, Prefix=prefix):
            for entry in page.get("Versions", []) + page.get("DeleteMarkers", []):
                s3.delete_object(Bucket=bucket, Key=entry["Key"], VersionId=entry["VersionId"])


@pytest.mark.parametrize("version", ["5.0", "4.0"], ids=["custom-encrypted", "legacy-encrypted"])
def test_project_role_reads_migrated_job_logs(admin, runner, cfg, log_project, version):
    project, connection, bucket = log_project
    domain, project_id = cfg["domain_id"], project["id"]
    name = f"e2e-log-{uuid.uuid4().hex[:12]}"
    role_arn = os.environ.get("GS_SMUS_E2E_LOG_JOB_ROLE_ARN") or cfg["job_role_arn"]
    key_arn = os.environ["GS_SMUS_E2E_LOG_KMS_KEY_ARN"]
    prefix = f"/aws-glue/{name}"
    if version == "5.0":
        args = {"--custom-logGroup-prefix": prefix}
    else:
        args = {"--enable-continuous-cloudwatch-log": "true"}
        prefix = "/aws-glue/jobs"
    base = f"{prefix}/{name}-role/{role_arn.rsplit('/', 1)[-1]}"
    output, error = f"{base}/output", f"{base}/error"
    legacy = (f"{prefix}/error-{name}", f"{prefix}/logs-v2-{name}")
    glue, logs, s3 = (admin.client(service) for service in ("glue", "logs", "s3"))
    dz = datazone_client(admin, cfg["domain_region"] or cfg["region"])
    source_key = f"e2e/{name}/job.py"
    run_id = None

    def run_complete():
        run = glue.get_job_run(JobName=name, RunId=run_id)["JobRun"]
        return run if run["JobRunState"] in {
            "SUCCEEDED", "FAILED", "ERROR", "TIMEOUT", "STOPPED", "EXPIRED",
        } else None

    def stop_run():
        if not run_complete():
            glue.batch_stop_job_run(JobName=name, JobRunIds=[run_id])
            wait_for(run_complete, f"Glue run {run_id} to stop")

    with ExitStack() as cleanup:
        obj = s3.put_object(Bucket=cfg["bucket"], Key=source_key, Body=SCRIPT.encode())
        version_args = {"VersionId": obj["VersionId"]} if "VersionId" in obj else {}
        cleanup.callback(s3.delete_object, Bucket=cfg["bucket"], Key=source_key, **version_args)
        glue.create_security_configuration(Name=name, EncryptionConfiguration={
            "CloudWatchEncryption": {"CloudWatchEncryptionMode": "SSE-KMS", "KmsKeyArn": key_arn},
        })
        cleanup.callback(glue.delete_security_configuration, Name=name)
        for group in ((output, error) if version == "5.0" else (output, error, *legacy)):
            cleanup.callback(ignore_missing, logs.delete_log_group, "ResourceNotFoundException",
                             logGroupName=group)
        glue.create_job(Name=name, Role=role_arn, GlueVersion=version, WorkerType="G.1X", NumberOfWorkers=2,
                        Timeout=15, SecurityConfiguration=name, DefaultArguments=args,
                        Command={"Name": "glueetl", "PythonVersion": "3",
                                 "ScriptLocation": f"s3://{cfg['bucket']}/{source_key}"})
        cleanup.callback(glue.delete_job, JobName=name)
        run_id = glue.start_job_run(JobName=name)["JobRunId"]
        print(f"Glue log fixture {name}, run {run_id}", flush=True)
        cleanup.callback(stop_run)
        run = wait_for(run_complete, f"Glue job {name}", timeout=1200)
        assert run["JobRunState"] == "SUCCEEDED", run.get("ErrorMessage")
        if version == "5.0":
            checks = [
                _markers(logs, key_arn, [(output, run_id, "E2E_STDOUT_MARKER")]),
                _markers(logs, key_arn, [(error, run_id, "E2E_GLUE_LOGGER_MARKER")]),
            ]
        else:
            checks = [_markers(logs, key_arn, [
                (group, run_id, "E2E_GLUE_LOGGER_MARKER") for group in legacy
            ])]
        reader = project_session(dz, connection, project["roleArn"], cfg["region"]).client("logs")
        for request, _ in checks:
            with pytest.raises(ClientError) as denied:
                reader.get_log_events(**request)
            assert denied.value.response["Error"]["Code"] in ("AccessDenied", "AccessDeniedException")

        cleanup.callback(_delete_migration_objects, s3, bucket, domain, project_id, name)
        result = migrate.migrate_job(runner, job_name=name, domain_id=domain, project_id=project_id,
                                     region=cfg["region"], domain_region=cfg["domain_region"], apply=True)
        grant = result["logRead"]
        assert result["applied"] and grant["needsUpdate"] and grant["projectRoleArn"] == project["roleArn"]
        resources = next(stmt["Resource"] for stmt in grant["policyDocument"]["Statement"]
                         if stmt["Action"] == "logs:GetLogEvents")
        _, partition, _, _, account, _ = project["roleArn"].split(":", 5)
        reader = project_session(dz, connection, project["roleArn"], cfg["region"]).client("logs")

        def readable(request, marker):
            try:
                return _has_marker(reader, request, marker)
            except ClientError as exc:
                if exc.response["Error"]["Code"] not in ("AccessDenied", "AccessDeniedException"):
                    raise
                return False

        for request, marker in checks:
            group_arn = f"arn:{partition}:logs:{cfg['region']}:{account}:log-group:{request['logGroupName']}"
            assert f"{group_arn}:log-stream:*" in resources
            kms_grant = next(stmt for stmt in grant["policyDocument"]["Statement"]
                             if stmt["Action"] == "kms:Decrypt")
            assert kms_grant["Resource"] == key_arn
            assert group_arn in kms_grant["Condition"]["ArnEquals"]["kms:EncryptionContext:aws:logs:arn"]
            wait_for(lambda: readable(request, marker), f"project role to read {request}")
