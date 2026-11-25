"""Provision and tear down an IAM-based ToolingLite E2E project."""
import json
import time

import boto3
from botocore.exceptions import ClientError


def execution_policy_arn(partition):
    return f"arn:{partition}:iam::aws:policy/SageMakerStudioUserIAMDefaultExecutionPolicy"


def wait_for(check, description, timeout=300):
    deadline = time.monotonic() + timeout
    while True:
        if result := check():
            return result
        if time.monotonic() >= deadline:
            raise AssertionError(f"Timed out waiting for {description}")
        time.sleep(4)


def ignore_missing(call, code, **kwargs):
    try:
        return call(**kwargs)
    except ClientError as exc:
        if exc.response["Error"]["Code"] != code:
            raise


def create_project(cleanup, iam, dz, *, name, partition, account, domain, profile, members,
                   after_delete=None):
    role_name = f"AmazonSageMakerUserIAMExecutionRole-{name}"
    services = [
        "datazone.amazonaws.com", "glue.amazonaws.com",
        "lakeformation.amazonaws.com", "sagemaker.amazonaws.com", "athena.amazonaws.com",
        "emr-serverless.amazonaws.com", "redshift.amazonaws.com", "bedrock.amazonaws.com",
        "airflow-serverless.amazonaws.com", "scheduler.amazonaws.com",
    ]
    if dz.meta.endpoint_url.startswith("https://iceland-gamma."):
        services += [
            "datazone.aws.internal", "athena.aws.internal",
            "glue-gamma.amazonaws.com", "glue.gamma.amazonaws.com",
            "devo.llaps.looseleaf.im.aws.internal", "loadtest.llaps.looseleaf.im.aws.internal",
            "devo.workflows.looseleaf.im.aws.internal", "loadtest.workflows.looseleaf.im.aws.internal",
        ]
    trust = {
        "Version": "2012-10-17",
        "Statement": [{
            "Effect": "Allow",
            "Principal": {"Service": services},
            "Action": ["sts:AssumeRole", "sts:TagSession", "sts:SetContext", "sts:SetSourceIdentity"],
            "Condition": {"StringEquals": {"aws:SourceAccount": account}},
        }],
    }
    policy = execution_policy_arn(partition)
    role = iam.create_role(RoleName=role_name, Path="/service-role/", AssumeRolePolicyDocument=json.dumps(trust),
                           Tags=[{"Key": "purpose", "Value": "gs-smus-migration-e2e"},
                                 {"Key": "AmazonDataZonePassedRolePath",
                                  "Value": f"service-role/{role_name}"}])["Role"]
    project_id = None

    def delete_role():
        if project_id:
            raise AssertionError(f"Retained role {role_name} because project {project_id} still exists")
        for page in iam.get_paginator("list_role_policies").paginate(RoleName=role_name):
            for inline in page["PolicyNames"]:
                iam.delete_role_policy(RoleName=role_name, PolicyName=inline)
        for page in iam.get_paginator("list_attached_role_policies").paginate(RoleName=role_name):
            for attached in page["AttachedPolicies"]:
                iam.detach_role_policy(RoleName=role_name, PolicyArn=attached["PolicyArn"])
        ignore_missing(iam.delete_role, "NoSuchEntity", RoleName=role_name)
        wait_for(lambda: ignore_missing(iam.get_role, "NoSuchEntity", RoleName=role_name) is None,
                 f"role {role_name} deletion")

    cleanup.callback(delete_role)
    iam.attach_role_policy(RoleName=role_name, PolicyArn=policy)
    iam.put_role_policy(RoleName=role_name, PolicyName="PassProjectRole", PolicyDocument=json.dumps({
        "Version": "2012-10-17",
        "Statement": [{
            "Effect": "Allow", "Action": "iam:PassRole", "Resource": role["Arn"],
            "Condition": {"StringEquals": {"iam:PassedToService": [
                "glue.amazonaws.com", "lakeformation.amazonaws.com", "sagemaker.amazonaws.com",
                "athena.amazonaws.com", "emr-serverless.amazonaws.com", "redshift-serverless.amazonaws.com",
            ]}},
        }],
    }))
    project_id = dz.create_project(
        domainIdentifier=domain, name=name, projectProfileId=profile, projectExecutionRole=role["Arn"],
        **({"membershipAssignments": [{"member": member, "designation": "PROJECT_OWNER"} for member in members]}
           if members else {}),
    )["id"]
    ids = {"domainIdentifier": domain, "identifier": project_id}

    def delete_project():
        nonlocal project_id
        deletion_requested = False
        environment_requests = set()

        def deleted():
            nonlocal deletion_requested
            project = ignore_missing(dz.get_project, "ResourceNotFoundException", **ids)
            if project is None:
                return True
            if project.get("projectStatus") == "DELETE_FAILED":
                raise AssertionError(f"Project {project_id} deletion failed: {project.get('failureReasons')}")
            if not deletion_requested and project.get("projectStatus") != "DELETING":
                deployment = project.get("environmentDeploymentDetails", {}).get("overallDeploymentStatus")
                if deployment in ("PENDING_DEPLOYMENT", "IN_PROGRESS"):
                    return False
                environments = [env for page in dz.get_paginator("list_environments").paginate(
                    domainIdentifier=domain, projectIdentifier=project_id,
                ) for env in page["items"]]
                for environment in environments:
                    env_id = environment["id"]
                    if environment["status"] == "DELETE_FAILED":
                        raise AssertionError(f"Environment {env_id} deletion failed")
                    if env_id not in environment_requests and environment["status"] not in ("CREATING", "DELETING"):
                        ignore_missing(dz.delete_environment, "ResourceNotFoundException",
                                       domainIdentifier=domain, identifier=env_id)
                        environment_requests.add(env_id)
                if environments:
                    return False
                ignore_missing(dz.delete_project, "ResourceNotFoundException", **ids)
                deletion_requested = True
            return False

        wait_for(deleted, f"project {project_id} deletion", timeout=600)
        project_id = None
        if after_delete:
            after_delete(ids["identifier"])

    cleanup.callback(delete_project)
    return {"id": project_id, "roleArn": role["Arn"]}


def project_session(dz, connection, role_arn, region):
    assert connection["environmentUserRole"] == role_arn
    credentials = dz.get_connection(domainIdentifier=connection["domainId"],
                                    identifier=connection["connectionId"], withSecret=True)["connectionCredentials"]
    session = boto3.Session(aws_access_key_id=credentials["accessKeyId"],
                            aws_secret_access_key=credentials["secretAccessKey"],
                            aws_session_token=credentials["sessionToken"], region_name=region)
    _, partition, _, _, account, _ = role_arn.split(":", 5)
    expected = f"arn:{partition}:sts::{account}:assumed-role/{role_arn.rsplit('/', 1)[-1]}/"
    caller = session.client("sts").get_caller_identity()["Arn"]
    assert caller.startswith(expected), f"Unexpected project session identity: {caller}"
    return session


def project_member(dz, iam, domain, caller_arn):
    if ":assumed-role/" in caller_arn:
        role_name = caller_arn.split("/")[-2]
        role_arn = iam.get_role(RoleName=role_name)["Role"]["Arn"]
        group = dz.get_group_profile(domainIdentifier=domain, groupIdentifier=role_arn)
        return {"groupIdentifier": group["id"]}
    profile = dz.get_user_profile(domainIdentifier=domain, userIdentifier=caller_arn)
    return {"userIdentifier": profile["id"]}
