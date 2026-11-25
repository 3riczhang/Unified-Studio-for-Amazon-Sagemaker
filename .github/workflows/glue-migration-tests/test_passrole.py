"""Exercise project PassRole grants through the job migration entry point."""
import copy
import json

import pytest
from botocore.exceptions import ClientError

from test_preserved_connections import JOB, Session, run
from migration.glue import cli

LOG_KEY_ARN = "arn:aws:kms:eu-north-1:111122223333:key/12345678-1234-1234-1234-123456789012"


def test_dry_run_previews_exact_role_and_service_without_writes():
    session = Session()
    plan = run(session)["passRole"]
    assert plan["projectRoleArn"] == "arn:aws:iam::111122223333:role/project/exec"
    assert plan["policyDocument"] == {
        "Version": "2012-10-17",
        "Statement": [{
            "Effect": "Allow", "Action": "iam:PassRole",
            "Resource": "arn:aws:iam::111122223333:role/legacy",
            "Condition": {"StringEquals": {"iam:PassedToService": "glue.amazonaws.com"}},
        }],
    }
    assert plan["needsUpdate"]
    assert session.writes == []


@pytest.mark.parametrize("version,command,arguments,overrides,security,groups", [
    ("5.0", "glueetl", {}, {}, None, ["/aws-glue/jobs/error", "/aws-glue/jobs/output"]),
    ("6.0", "glueetl", {"--custom-logGroup-prefix": "old"},
     {"--custom-logGroup-prefix": "/custom/glue"}, None,
     ["/custom/glue/error", "/custom/glue/output"]),
    ("auto", "glueetl", {"--custom-logGroup-prefix": "/custom/glue"}, {}, None,
     ["/aws-glue/jobs/error", "/aws-glue/jobs/output", "/custom/glue/error", "/custom/glue/output"]),
    ("5.0", "glueetl", {"--custom-logGroup-prefix": "/custom/glue"}, {}, "secured",
     ["/custom/glue/secured-role/legacy/error", "/custom/glue/secured-role/legacy/output"]),
    ("5.0", "glueetl", {"--custom-logGroup-prefix": "/custom/glue"}, {}, "unencrypted",
     ["/custom/glue/error", "/custom/glue/output"]),
    ("4.0", "glueetl", {"--enable-continuous-cloudwatch-log": "",
                      "--continuous-log-logGroup": "/legacy/continuous"}, {}, "secured",
     ["/aws-glue/jobs/secured-role/legacy/error", "/aws-glue/jobs/secured-role/legacy/output",
      "/legacy/continuous-secured"]),
    ("3.0", "glueetl", {"--enable-continuous-cloudwatch-log": "true"}, {}, None,
     ["/aws-glue/jobs/error", "/aws-glue/jobs/logs-v2", "/aws-glue/jobs/output"]),
    ("3.0", "glueetl", {"--enable-continuous-cloudwatch-log": "false"}, {}, None,
     ["/aws-glue/jobs/error", "/aws-glue/jobs/logs-v2", "/aws-glue/jobs/output"]),
    ("2.0", "glueetl", {"--enable-job-insights": ""},
     {"--enable-job-insights": "false"}, None,
     ["/aws-glue/jobs/error", "/aws-glue/jobs/output"]),
    ("2.0", "glueetl", {"--enable-job-insights": "",
                      "--continuous-log-logGroup": "/legacy/insights"},
     {}, None, ["/aws-glue/jobs/error", "/aws-glue/jobs/output", "/legacy/insights"]),
    ("3.0", "glueetl", {"--enable-continuous-cloudwatch-log": "true"}, {}, "secured",
     ["/aws-glue/jobs/error-secured", "/aws-glue/jobs/secured-role/legacy/error",
      "/aws-glue/jobs/secured-role/legacy/output", "/aws-glue/jobs/logs-v2-secured"]),
    (None, "pythonshell", {}, {}, None,
     ["/aws-glue/python-jobs/error", "/aws-glue/python-jobs/output"]),
])
def test_dry_run_previews_read_only_access_to_exact_glue_log_groups(
        version, command, arguments, overrides, security, groups):
    job = {**JOB, "Command": {**JOB["Command"], "Name": command}, "GlueVersion": version,
           "DefaultArguments": arguments, "NonOverridableArguments": overrides,
           "SecurityConfiguration": security}
    if version == "auto":
        job["AutoUpgradeMetadata"] = {"SourceVersion": "4.0", "TargetVersion": "6.0"}
    session = Session(job=job)

    def get_security_configuration(Name):
        assert Name == security
        return {"SecurityConfiguration": {"EncryptionConfiguration": {
            "CloudWatchEncryption": {
                "CloudWatchEncryptionMode": "SSE-KMS" if Name == "secured" else "DISABLED",
                "KmsKeyArn": LOG_KEY_ARN,
            },
        }}}

    session.get_security_configuration = get_security_configuration
    result = run(session, domain_region="us-east-1")
    group_arns = [f"arn:aws:logs:eu-north-1:111122223333:log-group:{group}"
                  for group in sorted(groups)]
    statements = [{
        "Effect": "Allow", "Action": "logs:GetLogEvents",
        "Resource": [f"{arn}:log-stream:*" for arn in group_arns],
    }]
    if security == "secured":
        statements.append({
            "Effect": "Allow", "Action": "kms:Decrypt", "Resource": LOG_KEY_ARN,
            "Condition": {
                "StringEquals": {"kms:ViaService": "logs.eu-north-1.amazonaws.com"},
                "ArnEquals": {"kms:EncryptionContext:aws:logs:arn": group_arns},
            },
        })
    assert result["logRead"]["policyDocument"] == {
        "Version": "2012-10-17",
        "Statement": statements,
    }
    assert session.writes == []


@pytest.mark.parametrize("key_arn", [
    None,
    "arn:aws:kms:eu-north-1:111122223333:key/*",
    "arn:aws:kms:eu-north-1:111122223333:alias/aws/glue",
])
def test_encrypted_log_grant_requires_an_exact_kms_key_arn(key_arn):
    session = Session(job={**JOB, "GlueVersion": "5.0", "SecurityConfiguration": "secure"})
    session.get_security_configuration = lambda **kw: {
        "SecurityConfiguration": {"EncryptionConfiguration": {"CloudWatchEncryption": {
            "CloudWatchEncryptionMode": "SSE-KMS", "KmsKeyArn": key_arn,
        }}},
    }
    session.describe_key = lambda **kw: pytest.fail("DescribeKey must not run for this identifier")
    with pytest.raises(ValueError, match="KMS key ARN"):
        run(session)
    with pytest.raises(ValueError, match="KMS key ARN"):
        run(session, apply=True)
    assert session.writes == []


def test_encrypted_log_grant_resolves_key_alias():
    alias_arn = "arn:aws:kms:eu-north-1:111122223333:alias/team/glue-logs"
    session = Session(job={**JOB, "GlueVersion": "5.0", "SecurityConfiguration": "secure"})
    session.get_security_configuration = lambda **kw: {
        "SecurityConfiguration": {"EncryptionConfiguration": {"CloudWatchEncryption": {
            "CloudWatchEncryptionMode": "SSE-KMS", "KmsKeyArn": alias_arn,
        }}},
    }

    def describe_key(KeyId):
        assert KeyId == alias_arn
        return {"KeyMetadata": {"Arn": LOG_KEY_ARN}}

    session.describe_key = describe_key
    result = run(session, apply=True)
    grant = result["logRead"]
    assert session.policies["exec", grant["policyName"]]["Statement"][1]["Resource"] == LOG_KEY_ARN
    session.writes.clear()
    assert not run(session, apply=True)["logRead"]["needsUpdate"]
    assert session.writes == []


@pytest.mark.parametrize("prefix", ["*", "/aws-glue/jobs?", "x:log-stream:y", "${aws:username}"])
def test_log_group_arguments_cannot_widen_iam_access(prefix):
    session = Session(job={**JOB, "GlueVersion": "5.0",
                           "DefaultArguments": {"--custom-logGroup-prefix": prefix}})
    with pytest.raises(ValueError, match="log group"):
        run(session, apply=True)
    assert session.writes == []


def test_already_migrated_job_can_repair_only_log_read_access():
    session = Session()
    plan = run(session)
    pass_role = plan["passRole"]
    session.policies["exec", pass_role["policyName"]] = pass_role["policyDocument"]
    session.tags["AmazonDataZoneProject"] = "p1"
    repaired = run(session, apply=True)
    assert repaired["skipped"] and repaired["applied"]
    assert not repaired["passRole"]["needsUpdate"]
    log_read = repaired["logRead"]
    assert session.writes == [("policy", {"RoleName": "exec", "PolicyName": log_read["policyName"]})]
    assert session.policies["exec", log_read["policyName"]] == log_read["policyDocument"]
    session.writes.clear()
    assert not run(session, apply=True)["applied"]
    assert session.writes == []


def test_apply_preserves_other_policies_and_retries_without_policy_writes():
    session = Session()
    unrelated = {"Version": "2012-10-17", "Statement": []}
    session.policies["exec", "existing"] = unrelated
    first = run(session, apply=True)
    assert session.policies["exec", "existing"] == unrelated
    assert session.policies["exec", first["passRole"]["policyName"]] == first["passRole"]["policyDocument"]
    session.writes.clear()
    retry = run(session, apply=True)
    assert not retry["passRole"]["needsUpdate"]
    assert not retry["applied"]
    assert session.writes == []


def test_migrating_another_role_keeps_first_grant():
    session = Session()
    run(session, apply=True)
    first = copy.deepcopy(session.policies)
    session.job["Role"] = "arn:aws:iam::111122223333:role/another"
    run(session, apply=True)
    assert len(session.policies) == 3  # two PassRole grants and one shared log grant
    assert first.items() <= session.policies.items()


def test_bare_job_role_is_resolved_with_its_iam_path():
    session = Session(job={**JOB, "Role": "legacy"})

    def get_role(RoleName):
        assert RoleName == "legacy"
        return {"Role": {"Arn": "arn:aws:iam::111122223333:role/service-role/legacy"}}

    session.get_role = get_role
    plan = run(session)["passRole"]
    assert plan["policyDocument"]["Statement"][0]["Resource"].endswith("/service-role/legacy")


@pytest.mark.parametrize("role", [
    "arn:aws:iam::999999999999:role/other-account",
    "arn:aws:iam::111122223333:role/*",
])
def test_invalid_job_role_cannot_grant_broader_access(role):
    session = Session(job={**JOB, "Role": role})
    with pytest.raises(ValueError):
        run(session, apply=True)
    assert session.writes == []


def test_missing_project_role_fails_before_any_write():
    session = Session()
    session.list_connections = lambda **kw: {"items": []}
    with pytest.raises(LookupError):
        run(session, apply=True)
    assert session.writes == []


def test_conflicting_policy_is_not_overwritten():
    session = Session()
    policy_name = run(session)["passRole"]["policyName"]
    conflicting = {"Version": "2012-10-17", "Statement": []}
    session.policies["exec", policy_name] = conflicting
    with pytest.raises(ValueError, match="policy"):
        run(session, apply=True)
    assert session.policies["exec", policy_name] == conflicting
    assert session.writes == []


@pytest.mark.parametrize("operation", ["get_role_policy", "put_role_policy"])
def test_iam_denial_prevents_job_changes(operation):
    session = Session()

    def denied(**kw):
        raise ClientError({"Error": {"Code": "AccessDenied"}}, operation)

    setattr(session, operation, denied)
    with pytest.raises(ClientError, match="AccessDenied"):
        run(session, apply=True)
    assert all(op == "put" for op, _ in session.writes)  # snapshot may already exist


def test_cli_previews_repairs_and_then_skips_existing_job(monkeypatch, capsys):
    session = Session(tags={"AmazonDataZoneProject": "p1"})
    monkeypatch.setattr(cli.session_mod, "build_session", lambda **kw: session)
    args = ["--job-name", "job", "--domain-id", "dzd-1", "--project-id", "p1"]
    for flags, heading, changed in [
        ([], "DRY-RUN", False), (["--apply"], "APPLIED", True),
        (["--apply"], "SKIPPED", False),
    ]:
        assert cli.main(args + flags) == 0
        banner, body = capsys.readouterr().out.split("\n", 1)
        assert heading in banner
        assert json.loads(body)["applied"] == changed
    assert [op for op, _ in session.writes] == ["policy", "policy"]
