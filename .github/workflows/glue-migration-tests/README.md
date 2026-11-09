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
cannot run the live job with this workflow. Unit tests use GitHub-hosted runners;
E2E uses the `glue-migration-e2e` CodeBuild project in your test account.

Configure these GitHub repository variables before enabling live runs:

| Variable | Value |
|---|---|
| `GS_SMUS_E2E_ACCOUNT_ID` | AWS account containing the existing fixture |
| `GS_SMUS_E2E_SETUP_ROLE_ARN` | Setup/cleanup role assumed by the CodeBuild service role |
| `GS_SMUS_E2E_RUNNER_ROLE_ARN` | Migration caller role assumed by the setup role |
| `GS_SMUS_E2E_REGION` | Glue and fixture Region |
| `GS_SMUS_E2E_DOMAIN_REGION` | DataZone domain Region |
| `GS_SMUS_E2E_DOMAIN_ID` | Existing test domain ID |
| `GS_SMUS_E2E_PROJECT_ID` | First test project ID |
| `GS_SMUS_E2E_PROJECT2_ID` | Second test project ID; required to run all eight cases |

Use a dedicated test account and set these variables to your fixture's Region,
roles, domain and projects. Each repository needs its own variable configuration
and authorized AWS identities.

The workflow checks repository variables before assuming a role, then checks all
fixture settings before pytest. Missing configuration fails the job instead of
reporting success with skipped tests.

### CodeBuild runner setup

Create an AWS CodeConnections GitHub App connection in the test account and
complete its browser authorization for this repository only. Once its status is
`AVAILABLE`, deploy [codebuild-runner.yml](codebuild-runner.yml):

```bash
aws cloudformation deploy \
  --stack-name glue-migration-e2e-runner \
  --template-file .github/workflows/glue-migration-tests/codebuild-runner.yml \
  --capabilities CAPABILITY_IAM \
  --parameter-overrides \
    RepositoryUrl=https://github.com/OWNER/REPOSITORY \
    GitHubActorId=YOUR_NUMERIC_GITHUB_USER_ID \
    ConnectionArn=YOUR_CONNECTION_ARN \
    SetupRoleArn=YOUR_SETUP_ROLE_ARN
```

Use the fixture Region for deployment. The stack creates the CodeBuild project,
its service role, a log group with 14-day retention, and the GitHub workflow-job
webhook. No external IAM OIDC provider is needed.

The webhook permits only the configured GitHub actor and the exact workflow name
`Glue migration tests`. Keep that actor restricted to a trusted maintainer; the
workflow name and runner labels are not security boundaries. The `main` condition
is enforced by this workflow, not by the webhook: do not run untrusted workflow
revisions or approve untrusted code for this privileged runner. Do not broaden
the actor filter to allow arbitrary pull-request authors.

### Role trust and permissions

Add the stack's `ServiceRoleArn` output as a trusted AWS principal on the existing
setup role, allowing `sts:AssumeRole`. The setup role must be allowed to assume
the migration runner role, whose trust policy permits the setup role. Both roles
must have project-owner memberships in **both** fixture projects.

The setup profile uses `credential_source = EcsContainer` to obtain CodeBuild's
service-role credentials. The migration profile uses the setup profile as its
source. Both request one-hour sessions and refresh automatically during the
90-minute job. Setup, assertions and cleanup use `migration-e2e-setup`; migration
commands use `migration-e2e-runner`. The workflow checks both AWS account IDs
before accessing the fixture.

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

Deploying the runner template applies its IAM policy, but does not modify the
existing test-role trust policies or GitHub branch protection. Apply those
separately through an authorized administrator. Review existing OIDC consumers
before removing any shared legacy provider or trust statements.

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

Runs are serialized per account and Region within this repository and do not
cancel active runs. GitHub concurrency does not coordinate with other
repositories or local pytest: avoid overlapping those runs against this fixture.
Cleanup uses pytest finalizers; force cancellation can leave temporary resources.
JUnit reports are uploaded even on test failure. CodeBuild compute, Glue execution,
RDS and VPC endpoints incur AWS charges.
