# Glue migration tests

These tests exercise `migration/glue` using local module commands, without
installing it as a package. Run from the repository root with Python 3.12:

```bash
python -m pip install -r migration/glue/requirements.txt pytest hypothesis
python -m pytest -c .github/workflows/glue-migration-tests/pytest.ini \
  .github/workflows/glue-migration-tests -m "not e2e"
```

Unit tests use mocks and require no AWS credentials. The dedicated
[Glue migration workflow](../glue-migration-tests.yml) runs them on every pull
request so the check can be required without path-filtered PRs remaining pending.
In your repository's branch protection or ruleset, require
**Glue migration unit tests** before merging. Adding workflow YAML does not
configure that repository setting.

## Live E2E configuration

Live tests run after unit tests on `main` pushes affecting Glue files, or manual
dispatch on `main`. Pull requests receive no AWS credentials. Other branches
cannot run the live job. To approve another branch, update both the workflow's
trigger/condition and the setup role's trust policy.

Configure these GitHub repository variables before enabling live runs:

| Variable | Value |
|---|---|
| `GS_SMUS_E2E_ACCOUNT_ID` | AWS account containing the existing fixture |
| `GS_SMUS_E2E_SETUP_ROLE_ARN` | Setup/cleanup role assumed through GitHub OIDC |
| `GS_SMUS_E2E_RUNNER_ROLE_ARN` | Migration caller role assumed by the setup role |
| `GS_SMUS_E2E_REGION` | Glue and fixture Region |
| `GS_SMUS_E2E_DOMAIN_REGION` | DataZone domain Region |
| `GS_SMUS_E2E_DOMAIN_ID` | Existing test domain ID |
| `GS_SMUS_E2E_PROJECT_ID` | First test project ID |
| `GS_SMUS_E2E_PROJECT2_ID` | Second test project ID for the existing connection suite |
| `GS_SMUS_E2E_LOG_PROJECT_PROFILE_ID` | Dedicated ToolingLite project profile for the project-role log tests |
| `GS_SMUS_E2E_LOG_KMS_KEY_ARN` | Dedicated customer-managed CloudWatch Logs test key ARN |
| `GS_SMUS_E2E_LOG_JOB_ROLE_ARN` | Optional encryption-enabled Glue job role. Defaults to the fixture job role |

Use a dedicated test account and set these variables to your fixture's Region,
roles, domain and projects. Each repository needs its own variable configuration
and authorized AWS identities.

The workflow checks repository variables before assuming a role, then checks all
fixture settings before pytest. Missing configuration fails the job instead of
reporting success with skipped tests.

### Role trust and permissions

Configure a GitHub OIDC provider in the test account. Restrict the setup role's
trust policy to your repository's `main` branch, replacing the placeholders:

```text
repo:<OWNER>/<REPOSITORY>:ref:refs/heads/main
```

Keep the OIDC audience restricted to `sts.amazonaws.com` and use the account's
`token.actions.githubusercontent.com` provider. Do not allow pull-request subjects
or wildcard branches. If your repository customizes its OIDC subject, use its
configured subject format while retaining the repository and branch restrictions.

The setup role needs a two-hour maximum session duration and permission to assume
the runner role. The runner trust permits the setup role. Both roles must have
project-owner memberships in **both** fixture projects. Migration commands
use a refreshable runner profile with `credential_source = Environment`; setup,
assertions and cleanup use the setup identity.

Grant the setup role scoped fixture permissions: it reads the fixture
RDS master secret and network configuration, creates and cleans up temporary
`e2e-*` Glue jobs/connections/secrets and S3 objects, starts Glue runs, reads their
logs, and inspects/deletes test DataZone connections. Grant the runner migration
permissions and conditional secret access as described in
[the tool README](../../../migration/glue/README.md#iam-permissions).
The live secret-sharing tests also require Studio project-role permissions.
Keep these test permissions scoped to the dedicated fixture account.
Job migration also writes the fixture project's PassRole policy using the
runner's `iam:GetRolePolicy`/`iam:PutRolePolicy` permissions. That shared grant is
retained across test runs, as it is during production rollback.

Repository files do not apply IAM policies or change branch protection. Apply
trust changes through an authorized account administrator.

### Existing fixture and local execution

The workflow reads the long-lived private `e2e-gs-smus-mysql` fixture. It does not
provision or delete it. With setup credentials configured locally:

```bash
export GS_SMUS_E2E_REGION=eu-north-1
export GS_SMUS_E2E_DOMAIN_REGION=eu-north-1
export GS_SMUS_E2E_DOMAIN_ID=YOUR_DOMAIN_ID
export GS_SMUS_E2E_PROJECT_ID=YOUR_FIRST_PROJECT_ID
export GS_SMUS_E2E_PROJECT2_ID=YOUR_SECOND_PROJECT_ID
export GS_SMUS_E2E_RUNNER_PROFILE=YOUR_RUNNER_PROFILE
fixture_env="$(python .github/workflows/glue-migration-tests/e2e/provision_mysql.py env)"
eval "$fixture_env"
python -m pytest -c .github/workflows/glue-migration-tests/pytest.ini \
  .github/workflows/glue-migration-tests/e2e -m e2e -v \
  --junitxml=reports/glue-e2e.xml
```

For local runs, `GS_SMUS_E2E_PROFILE` optionally selects the setup profile, and
`GS_SMUS_E2E_ROLE_ARN` optionally selects an assumed migration role when no runner
profile is supplied. Without either runner setting, tests use the setup identity.
Local tests skip when fixture configuration is missing; CI adds a mandatory
configuration check.

Only a fixture administrator should run `provision_mysql.py up` or `down`. `up`
creates RDS, networking, a script bucket and a job role; `down` deletes them and
the fixture bucket's contents. If the RDS instance is recreated, update the setup
role's master-secret ARN permission before testing.

The eight live cases cover automatic secret sharing, maximum-length indexed tags,
job execution with preserved original connections, cloning into two projects,
two ownership-skip cases, prior-tag restoration, and CLI cutover/execution/recovery.
The last case preserves the existing script rewrite test without requiring agent
documentation or an installed command.

### Project-role log regressions

Set both log test variables to run the two additional cases against the same test
domain. Use an enabled IAM-based project profile that deploys only its
`ToolingLite` environment on creation, in the Glue account and Region, with a
default project bucket. Each pytest run creates one ToolingLite project and one
`service-role/AmazonSageMakerUserIAMExecutionRole-e2e-log-*` IAM role. A job first
writes encrypted log markers, then the test uses temporary credentials from
`default.iam` to read the same stream before and after migration. The cases cover
Glue 5 custom output and error groups and Glue 4 continuous logs.
To run just these tests after loading the fixture settings:

```bash
python -m pytest -c .github/workflows/glue-migration-tests/pytest.ini \
  .github/workflows/glue-migration-tests/e2e/test_job_log_access_e2e.py -m e2e -v
```

The setup identity needs permission to create and delete projects from that
profile, list and delete environments, create membership assignments, resolve
the migration runner's DataZone profile, and obtain default IAM connection credentials. Grant it IAM
create, pass, inspect, and delete access only for those project roles,
including attaching and detaching `SageMakerStudioUserIAMDefaultExecutionPolicy`
and managing inline policies. Allow the runner to read the Glue security
configuration, get and put inline policies on those roles, and migrate to the
disposable project buckets. Allow setup to remove test object versions and
ToolingLite's `shared/` and `shared/.libs.json` objects and delete the project buckets. The
ToolingLite provisioning role also needs access to deploy that profile.

The setup identity needs to manage `e2e-log-*` Glue security configurations and
CloudWatch log groups, discover and read the resulting streams, and decrypt test
logs through CloudWatch Logs. The Glue job role needs `logs:AssociateKmsKey` on the
test groups and access to describe and use the configured key. Allow the regional
CloudWatch Logs service to use the key for the test groups and let account IAM
policies authorize the project role. After provisioning, the test detaches the
standard execution policy from the disposable role so its broad log reads
cannot mask the migration grant. Teardown reattaches it for environment deletion.

Teardown waits for project deletion before deleting the role and its inline
policies. It reports the project ID and role ARN so interrupted runs can be
cleaned up without changing the retained fixture roles.

Runs are serialized per account and Region within this repository and do not
cancel active runs. GitHub concurrency does not coordinate with other
repositories or local pytest: avoid overlapping those runs against this fixture.
Cleanup uses pytest finalizers; force cancellation can leave temporary resources.
JUnit reports are uploaded even on test failure. Glue execution, RDS and VPC
endpoints incur AWS charges.
