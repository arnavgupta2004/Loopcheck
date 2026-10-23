# D4.2: least-privilege deployment identity for LoopCheck

**Status: in use.** The team created this deployment identity in its own AWS account and used
it to deploy stack `loopcheck-demo` in `ap-south-1` (see
[d4_demo_and_deployment.md](d4_demo_and_deployment.md#deployment-status)). The first deployment
attempts hit two AccessDenied errors, both documented below:
- `cloudformation:DescribeStacks`: the committed policy already allowed it, so the policy
  attached in IAM was corrected. The most likely cause, a mis-filled account ID, was
  inferred and never confirmed;
- `apigateway:TagResource` on HTTP API stage creation: a genuinely missing permission, now
  added to the committed policy.

Account IDs, IAM user details and credentials are deliberately not recorded in this
repository. If a future change needs a new permission, see
[If a permission is missing](#if-a-permission-is-missing).

The goal is to deploy stack `loopcheck-demo` in `ap-south-1` from your personal account
**without using the root user for routine work**. Root is used once, in the console, to
create the identity below. Root never gets access keys, and you never need to share a
credential with anyone, including Claude.

| File | What it is |
|---|---|
| [`deploy/iam/loopcheck-deployer-policy.json`](../deploy/iam/loopcheck-deployer-policy.json) | `LoopCheckDeployerPolicy`: routine deploy, update, logs, delete. Attach permanently |
| [`deploy/iam/loopcheck-sam-bootstrap-policy.json`](../deploy/iam/loopcheck-sam-bootstrap-policy.json) | `LoopCheckSamBootstrapPolicy`: only creates SAM's artifact bucket stack the first time. Attach only if that stack doesn't exist yet, and detach after the first deploy |

Both files contain the placeholder `ACCOUNT_ID`. Nothing secret is in them; your account ID
is filled in locally when you create the policies.

The architecture is unchanged:
- HTTP API → Lambda (python3.13, x86_64, zip `CodeUri`);
- `ConcurrencyMode=unreserved`;
- throttling at 2 req/s, burst 5;
- the 7-day log group `/aws/lambda/loopcheck-demo-inspect`;
- metadata-only logging;
- the logging-only execution role.

## Which identity: IAM user with `aws login`, or IAM Identity Center

**Recommendation for this personal, single account: a dedicated IAM user
`loopcheck-deployer` that signs in with a console password plus MFA. The CLI gets
temporary credentials through `aws login`.**

| | IAM user + `aws login` (recommended here) | IAM Identity Center |
|---|---|---|
| Setup | 2 managed policies, 1 user, MFA. About 10 console minutes | Enable an **organization** instance of Identity Center (this creates AWS Organizations with your account as management account). Then create a directory user, verify its email, create a permission set holding the policy, and assign it to the account |
| CLI credentials | Temporary, refreshed automatically by the CLI (needs AWS CLI ≥ 2.32.0) | Temporary role credentials via `aws configure sso` / `aws sso login` |
| Long-lived secrets | A console password (protected by MFA). **No access keys** | None in IAM; the Identity Center user has a password and MFA |
| When it is better | One person, one account, one project | Several accounts or people, or if you already use Organizations |

An *account* instance of Identity Center (the variant that does not create an organization)
cannot grant access to AWS accounts. For a standalone account that leaves the
organization route, which is more change than this project needs. Both options avoid
long-lived access keys. **Never create access keys for root, and never create access keys
for `loopcheck-deployer` either.** `aws login` makes them unnecessary.

## Required permissions and why

| Need | Actions (all in `LoopCheckDeployerPolicy` unless marked) | Scope |
|---|---|---|
| Preflight reads | `lambda:GetAccountSettings`; `cloudformation:DescribeStacks`/`DescribeStackResource`; `lambda:GetFunctionConcurrency` | `*` only for `GetAccountSettings` (no resource-level support; pinned to ap-south-1). The rest are scoped to the stack and function |
| SAM transform | `cloudformation:CreateChangeSet` | `arn:aws:cloudformation:ap-south-1:aws:transform/Serverless-2016-10-31` |
| Deploy/update/delete the stack | `CreateChangeSet`, `DescribeChangeSet`, `ExecuteChangeSet`, `DeleteChangeSet`, `ListChangeSets`, `DescribeStacks`, `DescribeStackEvents`, `DescribeStackResource(s)`, `ListStackResources`, `GetTemplate`, `GetTemplateSummary`, `DeleteStack`, `TagResource`/`UntagResource` (stack tags) | `stack/loopcheck-demo/*` only |
| `sam validate` | `cloudformation:ValidateTemplate` | `*` (no resource-level support), ap-south-1 only. `--lint` itself runs locally |
| Upload SAM artifacts | `s3:PutObject`, `GetObject`, `GetObjectVersion`, `DeleteObject` | `aws-sam-cli-managed-default-samclisourcebucket-*/loopcheck-demo/*` (this project's prefix) |
| Find the artifact bucket | `s3:ListBucket`, `GetBucketLocation`; `cloudformation:DescribeStacks`/`DescribeStackResource(s)` on `aws-sam-cli-managed-default` | The SAM bucket and its stack |
| First-time SAM bucket (bootstrap) | `LoopCheckSamBootstrapPolicy`: change-set actions on `stack/aws-sam-cli-managed-default/*`; `s3:CreateBucket`, bucket versioning, policy, tagging, encryption, public-access-block and ownership settings | Bucket name prefix only; the random suffix doesn't exist yet. **Temporary** |
| Lambda function | `Create`/`Delete`/`GetFunction`, `GetFunctionConfiguration`, `UpdateFunctionCode`/`Configuration`, tags, `GetPolicy`, `RemovePermission`, `Get`/`DeleteFunctionConcurrency`, and the read calls CloudFormation makes (`GetFunctionCodeSigningConfig`, `GetRuntimeManagementConfig`, `GetFunctionRecursionConfig`) | `function:loopcheck-demo-*` (CloudFormation names it `loopcheck-demo-InspectFunction-<random>`) |
| API Gateway invoke permission | `lambda:AddPermission` **only when the principal is `apigateway.amazonaws.com`** | Same function prefix |
| Execution role | `iam:CreateRole`, `DeleteRole`, `GetRole`, `TagRole`, `UntagRole`, `ListRoleTags`, `ListAttachedRolePolicies`, `ListRolePolicies`, `ListInstanceProfilesForRole` | `role/loopcheck-demo-*` |
| Role gets logging only | `iam:AttachRolePolicy`, `DetachRolePolicy` **only for** `arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole` | Same role prefix |
| Pass the role | `iam:PassRole` **only to `lambda.amazonaws.com`** | Same role prefix |
| HTTP API (routes, integration, `$default` stage, throttling, CORS, tags) | `apigateway:GET`/`POST`/`PUT`/`PATCH`/`DELETE` | `/apis`, `/apis/*`, `/tags/*` in ap-south-1. **Broader**: see below |
| Tag the `$default` stage on create | `apigateway:TagResource` | `arn:aws:apigateway:ap-south-1::/apis/*/stages` only: the Stages collection, which is where tag-on-create is authorized. No `UntagResource`. See [AccessDenied on apigateway:TagResource](#accessdenied-on-apigatewaytagresource-stage) |
| Log group (7-day retention) and reading logs | `logs:CreateLogGroup`, `DeleteLogGroup`, `Put`/`DeleteRetentionPolicy`, tag actions, `GetDataProtectionPolicy`, `FilterLogEvents`, `GetLogEvents`, `DescribeLogStreams` | `log-group:/aws/lambda/loopcheck-demo-inspect` only |
| Find the log group | `logs:DescribeLogGroups` | `*`, ap-south-1 only. It has **no resource-level support**: a scoped ARN such as `log-group:*` never matches. Read-only metadata |
| Console/CLI sign-in | AWS managed policy `SignInLocalDevelopmentAccess` (`signin:AuthorizeOAuth2Access`, `signin:CreateOAuth2Token`) | Lets `aws login` turn the console session into CLI credentials |

**Deliberately absent:**
- **No inline role policies or policy editing** (`iam:PutRolePolicy`, `CreatePolicy`,
  `CreatePolicyVersion`). This blocks giving the role more rights.
- **No `iam:UpdateAssumeRolePolicy`** and no `iam:*User*` or access-key actions.
- **No `sts:AssumeRole`.**
- **No function URLs, no `lambda:InvokeFunction`.**
- **No `lambda:PutFunctionConcurrency`.** Unreserved mode never sets it.
- **No S3 ACL changes and no account-level settings.**

The deployer can therefore:
- create only a role named `loopcheck-demo-*`;
- give it only `AWSLambdaBasicExecutionRole`;
- hand it only to Lambda;
- let only API Gateway invoke the function.

Even a misused deployer can only run code with CloudWatch-Logs-only rights.

### Permissions that cannot be narrowly scoped, and narrower alternatives

1. **API Gateway: every HTTP API in ap-south-1.** The API ID is random and doesn't exist
   before the first deploy, so `/apis/*` can't be narrowed in advance.
   - *Narrower, after the first deploy:* replace `/apis/*` with
     `arn:aws:apigateway:ap-south-1::/apis/<ApiId>` and `.../apis/<ApiId>/*`, and remove
     `/apis` (which is only needed to create an API).
     - Updates and `sam delete` keep working.
     - A change that replaces the API would then need `/apis` again.
     - `<ApiId>` is the first label of the `ApiUrl` output.
   - *Alternative, untested:* add
     `"Condition": {"ForAnyValue:StringEquals": {"aws:CalledVia": ["cloudformation.amazonaws.com"]}}`
     to `HttpApiInRegion`. This allows API Gateway changes only when CloudFormation makes
     them on your behalf. Remove the condition again if the deploy fails with an
     `AccessDenied` mentioning API Gateway.
2. **Bootstrap S3 + `aws-sam-cli-managed-default` stack.** Before the bucket exists, only
   its name prefix is known.
   - *Narrower:* run `bash deploy/preflight.sh` first. If it prints
     `SAM managed artifact bucket exists`, **don't attach** the bootstrap policy at all.
   - Otherwise attach it for the first deploy only, then detach it (step 6 below).
   - *Alternative without any bootstrap rights:* create a private bucket yourself once in
     the console, as root or an administrator. Then deploy with `--s3-bucket <name>`
     instead of `--resolve-s3`, after changing the S3 ARNs in the deployer policy to that
     bucket.
3. **Read-only calls with `Resource: "*"`.** `lambda:GetAccountSettings`,
   `cloudformation:ValidateTemplate` and `logs:DescribeLogGroups` have no resource-level
   permissions (their "Resource types" column in the Service Authorization Reference is
   empty), so IAM matches them only with `"*"`. They are read-only and limited to ap-south-1.
   `cloudformation:DescribeStacks` is **not** in this group. It supports the `stack`
   resource type, so it stays scoped to `stack/loopcheck-demo/*` (see
   [AccessDenied on cloudformation:DescribeStacks](#accessdenied-on-cloudformationdescribestacks)).
4. **`s3:ListBucket` on the shared SAM bucket** also lists other SAM projects' artifact
   *names* (not contents). *Narrower:* add
   `"Condition": {"StringLike": {"s3:prefix": ["loopcheck-demo/*"]}}`. If `sam deploy`
   then fails its "already uploaded?" check, remove it again.
5. **Execution-role creation.** IAM can't pin the random role suffix; the
   `loopcheck-demo-*` prefix plus the attach and pass conditions above contain it.
   - *Stronger, needs a template change (not made):* a permissions boundary. Add
     `PermissionsBoundary` to `InspectFunction` and require it with the
     `iam:PermissionsBoundary` condition on `iam:CreateRole`.

**`AdministratorAccess` is not needed and should not be attached.** If something is
missing, add the single action named in the error (next section), not a broad policy.

**Reserved mode:** if you ever switch to `ConcurrencyMode=reserved`, add
`lambda:PutFunctionConcurrency` to the `LoopCheckFunction` statement. The policy targets
**`ConcurrencyMode=unreserved`**, the mode you chose. Reserved concurrency 0 must still never
be used.

### If a permission is missing

For `AccessDenied` on `cloudformation:DescribeStacks` right after the artifact upload, see
[AccessDenied on cloudformation:DescribeStacks](#accessdenied-on-cloudformationdescribestacks)
first: it is almost always a mis-filled account ID, not a missing action.

`sam deploy` shows the change set and CloudFormation then reports each failure as a
stack event like:
`... is not authorized to perform: logs:SomeAction on resource: arn:aws:logs:ap-south-1:…`.
CloudFormation rolls the stack back. Then:
1. Note the exact action and resource from the event.
2. As root (console), edit `LoopCheckDeployerPolicy`. Add **that action** to the statement
   for that resource, with the same scoped ARN. Never use `*` actions or `AdministratorAccess`.
3. If the stack is in `ROLLBACK_COMPLETE`, delete only the stack record and wait. Then deploy
   again. Use `aws cloudformation delete-stack`, not `sam delete`, so that nothing in S3 is
   touched (see [Recover from ROLLBACK_COMPLETE](#recover-from-rollback_complete)).

### AccessDenied on cloudformation:DescribeStacks

**Symptom (observed):**
- `sam deploy --profile loopcheck-deployer --region ap-south-1 --s3-bucket <existing bucket>`
  uploads the 80 MiB artifact.
- It then stops with:
  `User: arn:aws:iam::<account>:user/loopcheck-deployer is not authorized to perform:
  cloudformation:DescribeStacks on resource: arn:aws:cloudformation:ap-south-1:<account>:stack/loopcheck-demo/*
  because no identity-based policy allows the cloudformation:DescribeStacks action`.

**What it means:**
- **"No identity-based policy allows"** is an *implicit* deny: no Allow statement in the
  policies attached to the user matched the action and resource.
  - It is not an explicit Deny.
  - It is not a permissions boundary or an SCP; those produce differently worded messages.
- **The reported resource is the one IAM evaluated.** For a stack that doesn't exist yet,
  CloudFormation authorizes the request as `stack/loopcheck-demo/*`.
- **SAM's first CloudFormation call after the upload** is `has_stack()`, which is
  `DescribeStacks(StackName="loopcheck-demo")` (SAM CLI 1.170.0,
  `samcli/lib/deploy/deployer.py`).
  - It treats only `Stack with id … does not exist` as "new stack", and stops on any other
    error.
  - A by-name `DescribeStacks` therefore never needs `Resource: "*"`. That would only be
    needed to list *all* stacks, which SAM doesn't do here.

**The repository policy allows exactly that ARN:**
- `LoopCheckStack` allows `cloudformation:DescribeStacks` on
  `arn:aws:cloudformation:ap-south-1:ACCOUNT_ID:stack/loopcheck-demo/*`.
- Once rendered with the right account ID, that is character-for-character the reported
  resource (`tests/test_iam_policy.py` checks it).

**So the policy that is attached in IAM differs from the repository's rendered policy.**
The upload succeeding is consistent with this: S3 ARNs contain no account ID, so a
wrong account field breaks only the account-scoped statements (CloudFormation, Lambda,
IAM, Logs), and `DescribeStacks` is the first of those SAM calls. Likely causes, most likely
first:
1. **Account ID not filled in:** the repository JSON was pasted with the literal `ACCOUNT_ID`.
2. **Empty account field:** `sed "s/ACCOUNT_ID/$ACCOUNT_ID/g"` ran in a shell where
   `ACCOUNT_ID` was unset (the earlier instructions allowed this, and their `grep -c` check
   could not catch it).
3. **Wrong digits:** a typo, or a **dropped leading zero** (11 digits).
4. **The edit isn't effective:**
   - the edited policy was never saved;
   - a different version is the **default** (Policy versions tab);
   - the policy is attached to a different user;
   - or only the bootstrap policy is attached.
5. **Stack name or region differs:** `--stack-name` other than `loopcheck-demo`, or a
   region other than ap-south-1 in either the command or the policy.

**Diagnose offline:**
1. As root, open **IAM → Policies → `LoopCheckDeployerPolicy` → Permissions**. Make sure
   the default version is shown.
2. Choose **JSON** and copy the whole document into a local file outside the repository,
   e.g. `~/loopcheck-attached.json`.
3. Run:
   ```bash
   python3 deploy/iam/policy_tool.py check ~/loopcheck-attached.json --account-id <12-digit id> [--bucket <your --s3-bucket>]
   ```
   It names each statement whose account field is a placeholder, empty, short or
   different. It also reports whether `DescribeStacks` on `stack/loopcheck-demo/*`, and the
   other key `sam deploy` requests, are allowed, and lists differences from the repository.
   Expect `CHECK: PASS`.

**Fix it in IAM.** Editing the repository has **no effect on AWS**. The corrected
document must be saved as the policy's default version before you retry.
1. Render locally:
   ```bash
   python3 deploy/iam/policy_tool.py render --account-id <12-digit id> [--bucket <your --s3-bucket>]
   ```
   Then check the result:
   ```bash
   python3 deploy/iam/policy_tool.py check deploy/.build/iam/deployer.json --account-id <id> [--bucket …]
   ```
   Expect `CHECK: PASS`.
2. As root in the console, open **IAM → Policies** and search for `LoopCheckDeployerPolicy`
   (Type: Customer managed). Open it.
3. Choose **Edit**, then the **JSON** tab. Select all, delete, and paste the full contents of
   `deploy/.build/iam/deployer.json`.
4. Review the policy validator panel below the editor and resolve every **error**.
   Security warnings or suggestions about `"*"` resources are expected only for the
   `ReadOnlyCallsWithoutResourceLevelSupport` statement.
5. Choose **Next**, review the summary, then **Save changes**. IAM stores this as a new
   version and makes it the **default**.
   - A managed policy keeps at most 5 versions. If the console asks, delete an old,
     non-default version first.
   - The policy stays attached to `loopcheck-deployer`; there is no need to detach and
     re-attach it.
6. Confirm that **Policy versions** shows the new version as default, and that **Entities
   attached** lists `loopcheck-deployer`.
7. Copy the JSON again and rerun `policy_tool.py check` on it. Expect `CHECK: PASS`.
8. As the deployer, run the read-only preflight:
   `LOOPCHECK_AWS_PROFILE=loopcheck-deployer bash deploy/preflight.sh`.
   Step 5 must print `stack loopcheck-demo does not exist yet (describe-stacks allowed …)`.
   The preflight now **fails** on AccessDenied; earlier versions wrongly reported it as
   "does not exist yet".
9. Retry the same `sam deploy` command.
   - Nothing needs cleaning up from the failed run. It stopped before `CreateChangeSet`, so
     no stack was created.
   - The uploaded artifact stays in the bucket, and SAM skips re-uploading an identical
     file. **Don't delete anything from S3.**
   - IAM changes usually apply within seconds; if the first retry is still denied, wait a
     minute.

**Also fixed in the repository policy at the same time:** `logs:DescribeLogGroups` had been
scoped to `log-group:*`. It has no resource-level support, so that could never match, and
it would have been the next denial: CloudFormation's log-group handler and runbook step 10
call it. It now sits in the `ReadOnlyCallsWithoutResourceLevelSupport` statement (`*`,
ap-south-1 only). **Paste the updated policy even if the account ID turns out to be right.**

**How this was checked:**
- The fetch of `docs.aws.amazon.com` was blocked by this development environment's network
  policy.
- The resource-level facts above therefore come from the Service Authorization Reference
  data bundled in `policy_sentry` 0.15.2 (PyPI, April 2026):
  - `DescribeStacks` supports resource type `stack`, ARN `stack/${StackName}/${Id}`;
  - `DescribeLogGroups`, `GetAccountSettings` and `ValidateTemplate` have none.
- SAM's calls come from the SAM CLI 1.170.0 source.
- Re-check against the live reference if you can open it:
  https://docs.aws.amazon.com/service-authorization/latest/reference/list_awscloudformation.html

### AccessDenied on apigateway:TagResource (Stage)

**Observed CloudFormation event** (`AWS::ApiGatewayV2::Stage`, `CREATE_FAILED`):
`user arn:aws:iam::<account>:user/loopcheck-deployer is not authorized to perform
apigateway:TagResource on resource arn:aws:apigateway:ap-south-1::/apis/<ApiId>/stages because no
identity-based policy allows the action`.

The HTTP API (`AWS::ApiGatewayV2::Api`) had already been created. The stack rolled back to
`ROLLBACK_COMPLETE`, which deleted everything it had created.

**Why the old policy failed.** API Gateway has two separate naming schemes:
- The **API operations** are `TagResource` (`POST /v2/tags/{resource-arn}`) and
  `UntagResource` (`DELETE /v2/tags/{resource-arn}`).
- Their **IAM authorization** historically used HTTP-verb actions: `apigateway:POST` and
  `apigateway:DELETE` on `/tags/*`. The old policy granted those.

The event shows that creating a stage **with tags** is now also authorized as the named IAM
action `apigateway:TagResource`, on the stage collection ARN. No HTTP-verb action satisfies
it. A Stage gets tags even though `template.yaml` sets none:
- CloudFormation adds its system tags (`aws:cloudformation:stack-name` and others);
- `--tags project=loopcheck …` propagates to it.

**Evidence used** (the Service Authorization Reference page on `docs.aws.amazon.com` was
blocked from the development environment):
1. **The CloudFormation event above.** It is authoritative for the action name and the
   resource ARN that IAM evaluated.
2. **AWS's published CloudFormation resource schema for `AWS::ApiGatewayV2::Stage`**
   (`https://schema.cloudformation.ap-south-1.amazonaws.com/CloudformationSchema.zip`,
   `aws-apigatewayv2-stage.json`, downloaded 2026-10-23):
   - **create:** `apigateway:POST`, `apigateway:TagResource`;
   - **update:** `apigateway:PATCH`, `POST`, `DELETE`, `TagResource`, `UntagResource`;
   - **delete:** `apigateway:DELETE`;
   - **read and list:** `apigateway:GET`.

   The same bundle shows `AWS::ApiGatewayV2::Api`, `Integration`, `Route` and `Deployment`
   use only the HTTP-verb actions. That is why the API was created and the stage was not.
3. **Older action lists lag behind.** AWS's Policy Generator data (`policies.js`) and
   `policy_sentry` 0.15.2 don't list `apigateway:TagResource` yet. If you can open the
   Service Authorization Reference, check "Amazon API Gateway Management V2"
   (`list_amazonapigatewaymanagementv2.html`).

**The fix (statement `HttpApiStageTagOnCreate`):**
```json
{
  "Sid": "HttpApiStageTagOnCreate",
  "Effect": "Allow",
  "Action": "apigateway:TagResource",
  "Resource": "arn:aws:apigateway:ap-south-1::/apis/*/stages"
}
```
- **One action, no wildcard actions.** The resource is the documented `Stages` resource
  type (`/apis/${ApiId}/stages`) in ap-south-1. `ApiId` is random and doesn't exist before
  creation, so it can't be narrowed further in advance.
- **It does not allow tagging:** the API itself, routes, an existing stage, REST APIs, or
  any other region.
- **No `apigateway:UntagResource`.** Neither create, rollback (the delete handler), nor
  `sam delete` needs it. Redeploying with the same `--tags` doesn't change the stage's tags.
- **What a stack update could still need:** an update that changes or removes a stage tag
  (a different `--tags`) runs the stage update handler. That may need `TagResource` and
  `UntagResource` on the existing stage, which by the documented `Stage` resource type is
  `arn:aws:apigateway:ap-south-1::/apis/*/stages/*`. Add those two then, scoped to that
  ARN, only if CloudFormation reports the denial.

Throttling (2 req/s, burst 5), CORS, `ConcurrencyMode=unreserved`, the log group and
logging are unchanged: `template.yaml` was not modified.

**Other handler permissions checked at the same time.** The same schema bundle was compared
with the policy for every resource type in this stack (`Lambda::Function`,
`Lambda::Permission`, `IAM::Role`, `Logs::LogGroup`, the `ApiGatewayV2` types). The rest of
the difference is optional-feature permissions that this template doesn't use:
- VPC/ENI (`ec2:Describe*`), KMS, EFS, layers, code signing, SnapStart/scaling;
- inline role policies (`iam:PutRolePolicy`, deliberately denied);
- log delivery, index or resource policies.

Your rollback shows the function, role and log-group create and delete paths already ran.
If a later attempt reports another action, add only that action as described in
[If a permission is missing](#if-a-permission-is-missing).

**Apply the fix in IAM.** Editing the repository has **no effect on AWS** until the new
version is saved as the policy's default.
1. Render and check locally:
   ```bash
   python3 deploy/iam/policy_tool.py render --account-id <12-digit id> [--bucket <your --s3-bucket>]
   python3 deploy/iam/policy_tool.py check deploy/.build/iam/deployer.json --account-id <12-digit id> [--bucket <…>]
   ```
   Expect `OK apigateway:TagResource on arn:aws:apigateway:ap-south-1::/apis/ta8wy1elv5/stages:
   allowed by HttpApiStageTagOnCreate` and `CHECK: PASS`.
2. Console, as root: open **IAM → Policies** and search for `LoopCheckDeployerPolicy`. Open
   it, choose **Edit**, then the **JSON** tab. Select all and replace it with the full
   contents of `deploy/.build/iam/deployer.json`.
3. Resolve any **errors** shown by the policy validator. A warning that `TagResource` isn't
   recognised can happen while AWS's validator data lags behind the service. It doesn't stop
   the save, and the event above proves IAM evaluates this action.
4. Choose **Next** → **Save changes**. This creates a new version and makes it the default.
   If it says there are already 5 versions, delete the oldest non-default version under
   **Policy versions** first.
5. Confirm:
   - **Policy versions** shows the new version as **(Default)**;
   - **Entities attached** still lists `loopcheck-deployer` (editing never detaches it).
6. Copy the saved JSON from **Permissions → JSON** into a file outside the repository, e.g.
   `~/loopcheck-attached.json`, and run:
   ```bash
   python3 deploy/iam/policy_tool.py check ~/loopcheck-attached.json --account-id <id> [--bucket <…>]
   ```
   Expect `CHECK: PASS`. This checks what IAM actually holds.
7. Preflight (read-only):
   ```bash
   LOOPCHECK_AWS_PROFILE=loopcheck-deployer bash deploy/preflight.sh
   ```
   It should end with `RESULT: PASS`. While the failed stack still exists, step 5 warns
   that it is `ROLLBACK_COMPLETE` and prints the two recovery commands below.

### Recover from ROLLBACK_COMPLETE

A stack whose **first** create failed ends in `ROLLBACK_COMPLETE`. Its resources are already
deleted, but the stack record remains, and CloudFormation can't update it. Delete only that
record, **after** the policy fix above is saved and the checker and preflight pass:

```bash
P=loopcheck-deployer R=ap-south-1 STACK=loopcheck-demo
aws cloudformation describe-stacks --stack-name $STACK --region $R --profile $P --query 'Stacks[0].StackStatus' --output text
#   must print ROLLBACK_COMPLETE. If it prints anything else, STOP and review before deleting.
aws cloudformation describe-stack-resources --stack-name $STACK --region $R --profile $P \
  --query 'StackResources[].[LogicalResourceId,ResourceStatus]' --output text
#   optional: every resource should show DELETE_COMPLETE (or be absent)
aws cloudformation delete-stack --stack-name $STACK --region $R --profile $P
aws cloudformation wait stack-delete-complete --stack-name $STACK --region $R --profile $P
#   returns when deletion has finished (non-zero exit = check the stack events)
aws cloudformation describe-stacks --stack-name $STACK --region $R --profile $P 2>&1 | tail -1
#   expect: "... Stack with id loopcheck-demo does not exist"
```

- **Use `delete-stack` here, not `sam delete`.** `delete-stack` touches only CloudFormation.
  It doesn't delete:
  - the shared SAM artifact bucket;
  - the uploaded artifact (`loopcheck-demo/…` objects);
  - the `aws-sam-cli-managed-default` stack.

  `sam delete` also offers to delete the stack's S3 artifacts, which you want to keep for the
  retry.
- **Permissions:** the deployer policy already allows `DeleteStack` and `DescribeStacks` on
  `stack/loopcheck-demo/*`, and the waiter uses `DescribeStacks`. Deleting a
  `ROLLBACK_COMPLETE` stack calls no resource-delete handlers, because nothing is left to
  delete.
- **Then retry** with the same `sam deploy` command, including `ConcurrencyMode=unreserved`
  and `ThrottleRatePerSecond=2 ThrottleBurst=5`. SAM sees the identical artifact already in
  the bucket and skips the upload.
- **This fix is not verified in AWS** until that retry reaches `CREATE_COMPLETE`.

## One-time setup in the AWS Console (as root, MFA on; no access keys)

These steps **create IAM resources** (IAM itself is free). Do them yourself; nothing here
was run on your behalf.

0. **Secure root first.** Sign in as root and open account menu → **Security credentials**.
   - Confirm **MFA** is assigned.
   - Confirm **Access keys** shows none. If one exists and you don't use it, deactivate,
     then delete it.
1. **Note your account ID** (account menu, top right): all 12 digits, **including leading
   zeros**. Fill it into the policies locally; this is a file-only step with no AWS call:
   ```bash
   python3 deploy/iam/policy_tool.py render --account-id 123456789012
   #   writes deploy/.build/iam/deployer.json and bootstrap.json, and prints the stack ARN to eyeball
   #   with an explicit existing artifact bucket (sam deploy --s3-bucket NAME), add: --bucket NAME
   #   (scopes S3 to that bucket's loopcheck-demo/ prefix; no bootstrap policy is written or needed)
   ```
   The renderer refuses anything that isn't exactly 12 digits. It also refuses to write a
   policy whose account field is empty, shortened or still `ACCOUNT_ID`. IAM accepts such a
   policy without complaint, but it matches nothing (see troubleshooting). **Don't** use
   `sed` with a shell variable: run in a shell where the variable is unset, it silently
   writes an empty account field. `deploy/.build/` is git-ignored.
2. **IAM → Policies → Create policy → JSON.** Paste `deploy/.build/iam/deployer.json`.
   - The editor validates it. Expect no errors. Fix any reported error before continuing.
   - **Next.** Name: `LoopCheckDeployerPolicy`. Description: `Deploy/update/delete stack
     loopcheck-demo in ap-south-1 only`.
   - **Create policy.**
3. **Bootstrap policy, only if `aws-sam-cli-managed-default` doesn't exist in ap-south-1.**
   - To check as root: open **CloudFormation**, select region **Asia Pacific (Mumbai)
     ap-south-1**, and open **Stacks**.
   - If that stack is missing: create a second policy from `deploy/.build/iam/bootstrap.json`
     named `LoopCheckSamBootstrapPolicy`.
4. **IAM → Users → Create user.**
   - User name: `loopcheck-deployer`.
   - Tick **Provide user access to the AWS Management Console** → **I want to create an
     IAM user** → custom password. Store it in your password manager.
   - Untick "must create a new password" (or leave it ticked; the console then adds
     `IAMUserChangePassword`). **Next**.
   - **Attach policies directly:**
     - `LoopCheckDeployerPolicy`;
     - the AWS managed `SignInLocalDevelopmentAccess` (needed for `aws login`);
     - `LoopCheckSamBootstrapPolicy`, only if you created it in step 3.
   - **Do not** attach `AdministratorAccess` or any `*FullAccess` policy. **Next →
     Create user**.
   - **Do not** create access keys. If the console offers them, skip it: never create
     access keys for this user.
5. **IAM → Users → `loopcheck-deployer` → Security credentials → Assign MFA device.**
   Use a passkey or an authenticator app.
6. **After the first successful deploy**, if you attached the bootstrap policy: **IAM →
   Users → `loopcheck-deployer` → Permissions → remove `LoopCheckSamBootstrapPolicy`**.
7. Note the IAM sign-in URL: `https://<ACCOUNT_ID>.signin.aws.amazon.com/console`.
   **Sign out of the root session.**

Optional CLI equivalent of steps 2–4, **run only as root or an administrator; it creates
resources**. Set the password and MFA in the console (steps 4–5), never on a command line.
```bash
aws iam create-policy --policy-name LoopCheckDeployerPolicy --policy-document file://deploy/.build/iam/deployer.json
aws iam create-user --user-name loopcheck-deployer
aws iam attach-user-policy --user-name loopcheck-deployer --policy-arn "arn:aws:iam::$ACCOUNT_ID:policy/LoopCheckDeployerPolicy"
aws iam attach-user-policy --user-name loopcheck-deployer --policy-arn arn:aws:iam::aws:policy/SignInLocalDevelopmentAccess
```

Optional, read-only lint of the policy before creating it (IAM Access Analyzer; creates
nothing):
`aws accessanalyzer validate-policy --policy-type IDENTITY_POLICY --policy-document file://deploy/.build/iam/deployer.json --region ap-south-1`.

**If you prefer IAM Identity Center:**
- Create a permission set named `LoopCheckDeployer`, with `deployer.json` as its inline
  policy. Inline permission-set policies can be up to 10,240 characters.
- Assign your Identity Center user to the account with that permission set.
- Configure the CLI with `aws configure sso --profile loopcheck-deployer`; choose the
  `LoopCheckDeployer` role and region ap-south-1.
- The preflight recognises the role `AWSReservedSSO_LoopCheckDeployer_*`.

## Configure the CLI profile (your machine)

Your existing CLI profile (`loopcheck`) may be signed in as **root**, so don't reuse it. The
new profile `loopcheck-deployer` is the runbook and preflight default.

```bash
aws --version                     # need aws-cli/2.32.0 or newer for `aws login`; upgrade with the official installer if older
unset AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_SESSION_TOKEN AWS_PROFILE AWS_DEFAULT_PROFILE
aws login --profile loopcheck-deployer
#   The browser opens AWS sign-in. If it offers an existing ROOT session, do NOT pick it:
#   choose to sign in with another identity (or use a private window). Sign in with the
#   IAM user: account ID, user name loopcheck-deployer, password, MFA.
aws configure set region ap-south-1 --profile loopcheck-deployer   # local config file only; no AWS call
```

The CLI stores only a reference to the session (`login_session = arn:…:user/loopcheck-deployer`)
in `~/.aws/config`. The short-lived tokens are cached under `~/.aws/login/cache/`. Nothing
goes into the repository. Sign in again with the same command when the session expires.

**SAM CLI compatibility.** SAM reads profiles through its bundled botocore. If `sam` reports
`Unable to locate credentials` or a `NoCredentialsError` for this profile while `aws` works,
the bundled botocore doesn't yet understand `login_session`. Add this local profile, which
asks the AWS CLI for the temporary credentials on demand (nothing secret is written):
```ini
# ~/.aws/config
[profile loopcheck-deployer-sam]
region = ap-south-1
credential_process = aws configure export-credentials --profile loopcheck-deployer --format process
```
Then use `--profile loopcheck-deployer-sam` for `sam` commands only, and
`LOOPCHECK_AWS_PROFILE=loopcheck-deployer-sam` for the preflight.

**Retire the root CLI session** once the deployer works:
`aws logout --profile loopcheck`. This removes the local cached session only and doesn't
change anything in AWS.

## Verify the identity (read-only)

```bash
P=loopcheck-deployer R=ap-south-1
aws sts get-caller-identity --profile $P --region $R --query '[Account,Arn]' --output text
#   expect: <your account ID>  arn:aws:iam::<your account ID>:user/loopcheck-deployer
#   STOP if the ARN ends in ":root" — you are signed in as root; run aws login again as the IAM user.
aws lambda get-account-settings --profile $P --region $R --query 'AccountLimit.[ConcurrentExecutions,UnreservedConcurrentExecutions]' --output text
#   expect two numbers (allowed)
aws cloudformation describe-stacks --stack-name loopcheck-demo --profile $P --region $R
#   before the first deploy, expect "Stack with id loopcheck-demo does not exist" (allowed; not AccessDenied)
aws cloudformation describe-stacks --stack-name aws-sam-cli-managed-default --profile $P --region $R --query 'Stacks[0].StackStatus'
#   "does not exist" -> bootstrap needed (setup step 3); a status -> bucket exists
aws iam list-users --profile $P
#   expect AccessDenied (the deployer cannot read or change IAM users) — this is the least-privilege check
aws lambda list-functions --profile $P --region $R
#   expect AccessDenied (no account-wide Lambda listing)
sam list stack-outputs --stack-name loopcheck-demo --profile $P --region $R
#   before the first deploy, a "does not exist" error is fine; a credentials error -> use the -sam profile above
LOOPCHECK_AWS_PROFILE=$P bash deploy/preflight.sh
#   step 4 must print "[ OK ] dedicated LoopCheck deployment identity"; root makes the preflight FAIL
```

All of these are read-only. Then continue with the runbook in
[d4_demo_and_deployment.md](d4_demo_and_deployment.md#runbook-deploy-from-your-own-machine).
The default profile there is now `loopcheck-deployer`. Use step **5b** (`ConcurrencyMode=unreserved`):

```bash
sam deploy --stack-name loopcheck-demo --region ap-south-1 --profile loopcheck-deployer \
  --resolve-s3 --s3-prefix loopcheck-demo --capabilities CAPABILITY_IAM --confirm-changeset \
  --parameter-overrides AllowedOrigin=http://localhost:8501 ConcurrencyMode=unreserved ThrottleRatePerSecond=2 ThrottleBurst=5 \
  --tags project=loopcheck purpose=opencv-competition-demo
```

## Offline checks in this repository

`tests/test_iam_policy.py` (no AWS calls) checks that:
- both policies are valid JSON with no wildcard actions;
- `*` resources appear only for the three read-only actions without resource-level support
  (`lambda:GetAccountSettings`, `cloudformation:ValidateTemplate`, `logs:DescribeLogGroups`),
  pinned to ap-south-1, and those actions never appear with a scoped ARN;
- every regional ARN is in ap-south-1, and both policies fit the 6,144-character managed
  policy limit;
- no escalation actions exist (`PutRolePolicy`, `CreateAccessKey`, `PassRole` without a
  service condition…) and `AdministratorAccess` is never referenced;
- role attach and detach are limited to `AWSLambdaBasicExecutionRole`, `PassRole` to Lambda,
  and `AddPermission` to API Gateway;
- `template.yaml` still contains only the three resource types the policy covers, with no
  `Policies`, `Role` or function URL, and the log-group, stack, function and S3-prefix
  names match.

`tests/test_deploy.py` checks that the preflight defaults to `loopcheck-deployer`, fails on
a root ARN before making any other AWS call, warns on any other identity, and reports
AccessDenied on `describe-stacks` as a FAIL (never as "stack does not exist yet").

`deploy/iam/policy_tool.py` (tested in `tests/test_iam_policy.py`) renders the policies and
checks a copy of the attached one. The tests check that:
- the rendered policy allows `cloudformation:DescribeStacks` on exactly the ARN from the
  AccessDenied message, and on the real stack ARN, but not on other stacks;
- account IDs that are not exactly 12 digits are rejected;
- the checker explains the denial for the placeholder, empty, dropped-leading-zero and
  other-account cases;
- the `--bucket` mode keeps S3 scoped to the `loopcheck-demo/` prefix.
- `apigateway:TagResource` is allowed on exactly the Stage failure ARN
  (`/apis/ta8wy1elv5/stages`) and nowhere else (the API itself, routes, existing stages,
  REST APIs, other regions), and `UntagResource` is not granted;
- the checker fails when that statement, its action, its resource pattern or its region is
  wrong.
