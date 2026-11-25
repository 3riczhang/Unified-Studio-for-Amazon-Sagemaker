"""Check that disposable E2E IAM roles are cleaned up on failure."""
from contextlib import ExitStack
from unittest.mock import Mock

import pytest
from botocore.exceptions import ClientError

from e2e import project_fixture


@pytest.fixture
def clients():
    iam, dz = Mock(), Mock()
    dz.meta.endpoint_url = "https://datazone.us-east-1.amazonaws.com"
    iam.create_role.return_value = {"Role": {
        "Arn": "arn:aws:iam::123456789012:role/service-role/AmazonSageMakerUserIAMExecutionRole-e2e-log-test",
    }}
    iam.get_paginator.return_value.paginate.return_value = []
    dz.get_paginator.return_value.paginate.return_value = []
    iam.get_role.side_effect = ClientError({"Error": {"Code": "NoSuchEntity"}}, "GetRole")
    return iam, dz


def test_project_creation_failure_removes_the_new_role(clients):
    iam, dz = clients
    dz.create_project.side_effect = RuntimeError("provisioning failed")

    with pytest.raises(RuntimeError, match="provisioning failed"), ExitStack() as cleanup:
        project_fixture.create_project(
            cleanup, iam, dz, name="e2e-log-test", partition="aws",
            account="123456789012", domain="dzd-test", profile="profile", members=[],
        )

    iam.delete_role.assert_called_once_with(RoleName="AmazonSageMakerUserIAMExecutionRole-e2e-log-test")
    iam.get_role.assert_called_once_with(RoleName="AmazonSageMakerUserIAMExecutionRole-e2e-log-test")


def test_fixture_deletes_project_before_its_role(clients, monkeypatch):
    iam, dz = clients
    dz.create_project.return_value = {"id": "project"}
    dz.get_project.side_effect = [
        {"projectStatus": "ACTIVE"}, {"projectStatus": "ACTIVE"}, {"projectStatus": "ACTIVE"},
        {"projectStatus": "DELETING"},
        ClientError({"Error": {"Code": "ResourceNotFoundException"}}, "GetProject"),
    ]
    dz.get_paginator.return_value.paginate.side_effect = [
        [{"items": [{"id": "env", "status": "VALIDATION_FAILED"}]}], [], [],
    ]
    monkeypatch.setattr(project_fixture.time, "sleep", lambda _: None)
    calls = []
    dz.delete_environment.side_effect = lambda **_: calls.append("environment")
    dz.delete_project.side_effect = lambda **_: calls.append("project")
    iam.delete_role.side_effect = lambda **_: calls.append("role")
    iam.get_paginator.return_value.paginate.side_effect = [
        [{"PolicyNames": ["SMUSGlueJobLogRead-test"]}], [{"AttachedPolicies": []}],
    ]
    iam.delete_role_policy.side_effect = lambda **_: calls.append("inline-policy")

    with ExitStack() as cleanup:
        assert project_fixture.create_project(
            cleanup, iam, dz, name="e2e-log-test", partition="aws",
            account="123456789012", domain="dzd-test", profile="profile", members=[],
        )["id"] == "project"

    assert calls == ["environment", "project", "inline-policy", "role"]
    dz.delete_project.assert_called_once()


def test_fixture_preserves_role_if_project_is_still_attached(clients):
    iam, dz = clients
    dz.create_project.return_value = {"id": "project"}
    dz.get_project.return_value = {"projectStatus": "DELETE_FAILED"}

    with pytest.raises(AssertionError, match="Retained role"), ExitStack() as cleanup:
        project_fixture.create_project(
            cleanup, iam, dz, name="e2e-log-test", partition="aws",
            account="123456789012", domain="dzd-test", profile="profile", members=[],
        )

    iam.delete_role.assert_not_called()
