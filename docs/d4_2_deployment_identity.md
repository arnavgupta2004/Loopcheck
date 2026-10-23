# D4.2: least-privilege deployment identity for LoopCheck

**Status: prepared, NOT created.** Nothing in this document has been run against AWS. No
IAM user, policy, role or credential exists yet, and these policies have **not** been tested
against a real deployment. Your first `sam deploy` with them is that test (see
[If a permission is missing](#if-a-permission-is-missing)).

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
| Log group (7-day retention) and reading logs | `logs:CreateLogGroup`, `DeleteLogGroup`, `Put`/`DeleteRetentionPolicy`, tag actions, `GetDataProtectionPolicy`, `FilterLogEvents`, `GetLogEvents`, `DescribeLogStreams` | `log-group:/aws/lambda/loopcheck-demo-inspect` only |
| Find the log group | `logs:DescribeLogGroups` | `log-group:*` in ap-south-1 (needs list-level scope; read-only metadata) |
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
3. **Read-only calls with `Resource: "*"`.** `lambda:GetAccountSettings` and
   `cloudformation:ValidateTemplate` have no resource-level permissions. They are read-only
   and limited to ap-south-1.
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

`sam deploy` shows the change set and CloudFormation then reports each failure as a
stack event like:
`... is not authorized to perform: logs:SomeAction on resource: arn:aws:logs:ap-south-1:…`.
CloudFormation rolls the stack back. Then:
1. Note the exact action and resource from the event.
2. As root (console), edit `LoopCheckDeployerPolicy`. Add **that action** to the statement
   for that resource, with the same scoped ARN. Never use `*` actions or `AdministratorAccess`.
3. If the stack is in `ROLLBACK_COMPLETE`, delete it with `sam delete` (allowed by the policy)
   and deploy again.

## One-time setup in the AWS Console (as root, MFA on; no access keys)

These steps **create IAM resources** (IAM itself is free). Do them yourself; nothing here
was run on your behalf.

0. **Secure root first.** Sign in as root and open account menu → **Security credentials**.
   - Confirm **MFA** is assigned.
   - Confirm **Access keys** shows none. If one exists and you don't use it, deactivate,
     then delete it.
1. **Note your account ID** (account menu, top right). Fill it into both policies locally;
   this is a file-only edit with no AWS call:
   ```bash
   ACCOUNT_ID=123456789012   # your 12-digit account ID (not a secret, but don't commit the rendered files)
   mkdir -p deploy/.build/iam
   sed "s/ACCOUNT_ID/$ACCOUNT_ID/g" deploy/iam/loopcheck-deployer-policy.json > deploy/.build/iam/deployer.json
   sed "s/ACCOUNT_ID/$ACCOUNT_ID/g" deploy/iam/loopcheck-sam-bootstrap-policy.json > deploy/.build/iam/bootstrap.json
   grep -c ACCOUNT_ID deploy/.build/iam/*.json   # expect 0 for both
   ```
   `deploy/.build/` is git-ignored.
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
- `*` resources appear only for the two read-only actions, pinned to ap-south-1;
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
a root ARN before making any other AWS call, and warns on any other identity.
