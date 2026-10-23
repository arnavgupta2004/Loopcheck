"""D4.2: the least-privilege deployer policies stay scoped to LoopCheck and match template.yaml.

Offline only: these tests read JSON/YAML files and never call AWS.
"""

import json
import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
IAM = ROOT / "deploy" / "iam"
DEPLOYER = IAM / "loopcheck-deployer-policy.json"
BOOTSTRAP = IAM / "loopcheck-sam-bootstrap-policy.json"
STACK = "loopcheck-demo"
REGION = "ap-south-1"
BASIC_EXEC = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
# Read-only actions with NO resource-level support (Service Authorization Reference "Resource types"
# column empty; checked against policy_sentry 0.15.2's bundled copy of that reference). IAM only
# matches them with Resource "*"; a scoped ARN such as log-group:* silently never matches.
STAR_RESOURCE_OK = {"lambda:GetAccountSettings", "cloudformation:ValidateTemplate", "logs:DescribeLogGroups"}


class _Loader(yaml.SafeLoader):
    pass


_Loader.add_multi_constructor("!", lambda loader, suffix, node: None)


def _statements(path):
    doc = json.loads(path.read_text())
    assert doc["Version"] == "2012-10-17"
    return doc["Statement"]


def _as_list(v):
    return v if isinstance(v, list) else [v]


def _actions(path):
    return {a for st in _statements(path) for a in _as_list(st["Action"])}


@pytest.mark.parametrize("path", [DEPLOYER, BOOTSTRAP], ids=lambda p: p.name)
def test_policy_is_valid_scoped_and_fits_managed_policy_limit(path):
    sts = _statements(path)
    assert len({st["Sid"] for st in sts}) == len(sts)
    for st in sts:
        assert st["Effect"] == "Allow" and "NotAction" not in st and "NotResource" not in st and "Principal" not in st
        for a in _as_list(st["Action"]):
            assert re.fullmatch(r"[a-z0-9]+:[A-Za-z0-9]+", a), f"wildcard or malformed action {a!r}"
        for r in _as_list(st["Resource"]):
            if r == "*":
                assert set(_as_list(st["Action"])) <= STAR_RESOURCE_OK, st["Sid"]
                assert st["Condition"]["StringEquals"]["aws:RequestedRegion"] == REGION
            else:
                assert r.startswith("arn:aws:")
                region = r.split(":")[3]
                assert region in (REGION, ""), r          # regional ARNs pinned to ap-south-1; IAM/S3 have none
                account = r.split(":")[4]
                assert account in ("ACCOUNT_ID", "", "aws"), r
    rendered = path.read_text().replace("ACCOUNT_ID", "123456789012")
    assert len(re.sub(r"\s", "", rendered)) <= 6144        # customer managed policy size limit


def test_no_admin_or_escalation_actions():
    actions = _actions(DEPLOYER) | _actions(BOOTSTRAP)
    forbidden = {
        "iam:PutRolePolicy", "iam:CreatePolicy", "iam:CreatePolicyVersion", "iam:SetDefaultPolicyVersion",
        "iam:UpdateAssumeRolePolicy", "iam:CreateUser", "iam:CreateAccessKey", "iam:AttachUserPolicy",
        "iam:PutUserPolicy", "iam:CreateLoginProfile", "iam:UpdateLoginProfile", "iam:DeleteRolePermissionsBoundary",
        "lambda:CreateFunctionUrlConfig", "lambda:UpdateFunctionUrlConfig", "lambda:InvokeFunction",
        "lambda:PutFunctionConcurrency",   # unreserved mode only; see docs/d4_2_deployment_identity.md
        "s3:PutBucketAcl", "s3:PutObjectAcl", "s3:PutAccountPublicAccessBlock",
        "sts:AssumeRole", "organizations:CreateAccount",
    }
    assert not actions & forbidden, actions & forbidden
    assert "AdministratorAccess" not in DEPLOYER.read_text() + BOOTSTRAP.read_text()


def test_iam_role_permissions_cannot_grant_more_than_basic_logging():
    by_action = {}
    for st in _statements(DEPLOYER):
        for a in _as_list(st["Action"]):
            if a.startswith("iam:"):
                by_action[a] = st
    for a, st in by_action.items():
        assert _as_list(st["Resource"]) == [f"arn:aws:iam::ACCOUNT_ID:role/{STACK}-*"], a
    for a in ("iam:AttachRolePolicy", "iam:DetachRolePolicy"):
        assert by_action[a]["Condition"] == {"ArnEquals": {"iam:PolicyARN": BASIC_EXEC}}
    assert by_action["iam:PassRole"]["Condition"] == {"StringEquals": {"iam:PassedToService": "lambda.amazonaws.com"}}
    add_perm = next(st for st in _statements(DEPLOYER) if "lambda:AddPermission" in _as_list(st["Action"]))
    assert add_perm["Condition"] == {"StringEquals": {"lambda:Principal": "apigateway.amazonaws.com"}}


def test_policy_matches_the_template_resources():
    tpl = yaml.load((ROOT / "template.yaml").read_text(), Loader=_Loader)
    res = tpl["Resources"]
    # Adding any other resource type needs new deployer permissions: update deploy/iam/ and the docs.
    assert sorted(r["Type"] for r in res.values()) == ["AWS::Logs::LogGroup", "AWS::Serverless::Function", "AWS::Serverless::HttpApi"]
    fn = res["InspectFunction"]["Properties"]
    # No Policies/Role/boundary: SAM then attaches only AWSLambdaBasicExecutionRole, the one ARN the policy allows.
    assert not {"Policies", "Role", "PermissionsBoundary", "FunctionUrlConfig", "AutoPublishAlias", "Layers", "VpcConfig"} & set(fn)
    assert {e["Type"] for e in fn["Events"].values()} == {"HttpApi"}
    text = DEPLOYER.read_text()
    assert f"log-group:/aws/lambda/{STACK}-inspect" in text   # template: !Sub /aws/lambda/${AWS::StackName}-inspect
    assert "${AWS::StackName}-inspect" in (ROOT / "template.yaml").read_text()
    assert f"stack/{STACK}/*" in text and f"function:{STACK}-*" in text
    assert f"samclisourcebucket-*/{STACK}/*" in text         # runbook: --resolve-s3 --s3-prefix "$STACK"


def test_runbook_commands_are_covered():
    """Every AWS action the preflight and runbook issue is allowed (CLI command -> IAM action)."""
    actions = _actions(DEPLOYER)
    needed = {
        "lambda:GetAccountSettings", "lambda:GetFunctionConcurrency", "lambda:DeleteFunctionConcurrency",
        "cloudformation:DescribeStacks", "cloudformation:DescribeStackResource", "cloudformation:DeleteStack",
        "cloudformation:CreateChangeSet", "cloudformation:ExecuteChangeSet", "cloudformation:DescribeChangeSet",
        "logs:FilterLogEvents", "logs:DescribeLogGroups", "s3:ListBucket", "s3:DeleteObject", "s3:PutObject",
    }
    assert needed <= actions, needed - actions


def test_docs_and_defaults_use_the_deployer_profile():
    assert 'PROFILE="${LOOPCHECK_AWS_PROFILE:-loopcheck-deployer}"' in (ROOT / "deploy" / "preflight.sh").read_text()
    assert 'profile = "loopcheck-deployer"' in (ROOT / "deploy" / "samconfig.example.toml").read_text()
    doc = (ROOT / "docs" / "d4_2_deployment_identity.md").read_text()
    for needle in ("aws login --profile loopcheck-deployer", "aws sts get-caller-identity", "LoopCheckDeployerPolicy",
                   "SignInLocalDevelopmentAccess", "ConcurrencyMode=unreserved", "never create access keys", "AdministratorAccess",
                   "AccessDenied on cloudformation:DescribeStacks", "policy_tool.py render --account-id", "policy_tool.py check",
                   "no effect on AWS", "Save changes", "HttpApiStageTagOnCreate", "Recover from ROLLBACK_COMPLETE",
                   "aws cloudformation wait stack-delete-complete", "(Default)"):
        assert needle in doc, needle
    runbook = (ROOT / "docs" / "d4_demo_and_deployment.md").read_text()
    assert "AWS_PROFILE_NAME=loopcheck-deployer" in runbook and "aws configure sso --profile loopcheck " not in runbook


def test_actions_without_resource_level_support_use_star():
    for st in _statements(DEPLOYER) + _statements(BOOTSTRAP):
        acts = set(_as_list(st["Action"]))
        if acts & STAR_RESOURCE_OK:
            assert _as_list(st["Resource"]) == ["*"] and acts <= STAR_RESOURCE_OK, st["Sid"]


# --------------------------------------------------------------------------- deploy/iam/policy_tool.py (offline)
import importlib.util  # noqa: E402

_spec = importlib.util.spec_from_file_location("policy_tool", IAM / "policy_tool.py")
pt = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pt)
ACCT = "012345678901"   # fictitious, with a leading zero on purpose
DENIED_ARN = f"arn:aws:cloudformation:ap-south-1:{ACCT}:stack/loopcheck-demo/*"  # as shown in the AccessDenied message


def test_rendered_policy_allows_the_exact_failing_request():
    pol = json.loads(pt.render("deployer", ACCT))
    assert pt.allowing_statements(pol, "cloudformation:DescribeStacks", DENIED_ARN) == ["LoopCheckStack"]
    # existing stack (real stack ID) is matched too, other stacks are not
    assert pt.allowing_statements(pol, "cloudformation:DescribeStacks", DENIED_ARN[:-1] + "1b2c3d4e-0000-1111-2222-333344445555")
    assert not pt.allowing_statements(pol, "cloudformation:DescribeStacks", DENIED_ARN.replace("loopcheck-demo", "other-stack"))
    assert not pt.allowing_statements(pol, "cloudformation:DescribeStacks", DENIED_ARN.replace("loopcheck-demo/", "loopcheck-demo2/"))
    errors, _ = pt.check(pol, ACCT)
    assert errors == []
    assert f":{ACCT}:" in json.dumps(pol) and "ACCOUNT_ID" not in json.dumps(pol)


@pytest.mark.parametrize("bad", ["", "12345678901", "1234567890123", "0123-4567-8901", "ACCOUNT_ID", " 012345678901"])
def test_render_rejects_bad_account_ids(bad):
    with pytest.raises(ValueError):
        pt.render("deployer", bad)


@pytest.mark.parametrize("label, transform", [
    ("placeholder left in", lambda t: t),
    ("empty substitution (sed with an unset variable)", lambda t: t.replace("ACCOUNT_ID", "")),
    ("leading zero dropped", lambda t: t.replace("ACCOUNT_ID", ACCT.lstrip("0"))),
    ("another account", lambda t: t.replace("ACCOUNT_ID", "999988887777")),
])
def test_check_explains_why_describe_stacks_is_denied(label, transform):
    attached = json.loads(transform(DEPLOYER.read_text()))
    errors, _ = pt.check(attached, ACCT)
    assert any(e.startswith(f"cloudformation:DescribeStacks on {DENIED_ARN}: NOT allowed") for e in errors), label
    assert any("LoopCheckStack:" in e for e in errors), label


def test_check_flags_missing_and_extra_statements():
    pol = json.loads(pt.render("deployer", ACCT))
    pol["Statement"] = [st for st in pol["Statement"] if st["Sid"] != "ReadOnlyCallsWithoutResourceLevelSupport"]
    pol["Statement"].append({"Sid": "Extra", "Effect": "Allow", "Action": "s3:GetObject", "Resource": "arn:aws:s3:::other/*"})
    errors, notes = pt.check(pol, ACCT)
    assert any("logs:DescribeLogGroups on *: NOT allowed" in e for e in errors)
    assert any("missing vs repository: lambda:getaccountsettings on *" in e for e in errors)
    assert any("extra vs repository (review): s3:getobject on arn:aws:s3:::other/*" in n for n in notes)


def test_policy_tool_cli_and_no_network(tmp_path):
    assert pt.main(["render", "--account-id", ACCT, "--out-dir", str(tmp_path)]) == 0
    assert json.loads((tmp_path / "deployer.json").read_text()) and (tmp_path / "bootstrap.json").exists()
    assert pt.main(["check", str(tmp_path / "deployer.json"), "--account-id", ACCT]) == 0
    assert pt.main(["check", str(DEPLOYER), "--account-id", ACCT]) == 1
    assert pt.main(["render", "--account-id", "123", "--out-dir", str(tmp_path / "x")]) == 1
    src = (IAM / "policy_tool.py").read_text()
    assert not re.search(r"^\s*(import|from)\s+(boto3|botocore|subprocess|socket|urllib|http)", src, re.M)


def test_explicit_bucket_rendering_keeps_prefix_scope(tmp_path):
    pol = json.loads(pt.render("deployer", ACCT, "my-loopcheck-artifacts"))
    text = json.dumps(pol)
    assert "samclisourcebucket" not in text
    assert "arn:aws:s3:::my-loopcheck-artifacts/loopcheck-demo/*" in text and "arn:aws:s3:::my-loopcheck-artifacts\"" in text
    assert pt.allowing_statements(pol, "s3:PutObject", "arn:aws:s3:::my-loopcheck-artifacts/loopcheck-demo/abc123")
    assert not pt.allowing_statements(pol, "s3:PutObject", "arn:aws:s3:::my-loopcheck-artifacts/other/abc123")
    assert pt.check(pol, ACCT, bucket="my-loopcheck-artifacts")[0] == []
    with pytest.raises(ValueError):
        pt.render("deployer", ACCT, "Bad_Bucket")
    assert pt.main(["render", "--account-id", ACCT, "--bucket", "my-loopcheck-artifacts", "--out-dir", str(tmp_path)]) == 0
    assert (tmp_path / "deployer.json").exists() and not (tmp_path / "bootstrap.json").exists()


# --------------------------------------------------------------------------- API Gateway V2 stage tagging
# Observed CloudFormation event (AWS::ApiGatewayV2::Stage, CREATE_FAILED):
#   not authorized to perform apigateway:TagResource on resource arn:aws:apigateway:ap-south-1::/apis/ta8wy1elv5/stages
# CloudFormation resource schema for AWS::ApiGatewayV2::Stage: create = apigateway:POST + apigateway:TagResource,
# delete = apigateway:DELETE (rollback/sam delete need no tagging), update adds TagResource/UntagResource.
STAGE_FAILURE_ARN = "arn:aws:apigateway:ap-south-1::/apis/ta8wy1elv5/stages"


def _deployer_rendered():
    return json.loads(pt.render("deployer", ACCT))


def test_stage_tag_on_create_is_allowed_for_the_exact_failure_arn():
    pol = _deployer_rendered()
    assert pt.allowing_statements(pol, "apigateway:TagResource", STAGE_FAILURE_ARN) == ["HttpApiStageTagOnCreate"]
    assert pt.allowing_statements(pol, "apigateway:POST", STAGE_FAILURE_ARN) == ["HttpApiInRegion"]
    st = next(s for s in pol["Statement"] if s["Sid"] == "HttpApiStageTagOnCreate")
    assert st["Action"] == "apigateway:TagResource" and st["Resource"] == "arn:aws:apigateway:ap-south-1::/apis/*/stages"


@pytest.mark.parametrize("arn", [
    "arn:aws:apigateway:ap-south-1::/apis/ta8wy1elv5",                     # the API itself tags via PUT/PATCH
    "arn:aws:apigateway:ap-south-1::/apis/ta8wy1elv5/routes",
    "arn:aws:apigateway:ap-south-1::/apis/ta8wy1elv5/stages/$default",     # existing stage: not needed for create
    "arn:aws:apigateway:ap-south-1::/restapis/abc/stages",
    "arn:aws:apigateway:us-east-1::/apis/ta8wy1elv5/stages",
    "arn:aws:apigateway:ap-south-1::/tags/arn%3Aaws%3Aapigateway",
])
def test_stage_tagging_is_not_granted_elsewhere(arn):
    assert not pt.allowing_statements(_deployer_rendered(), "apigateway:TagResource", arn)


def test_untag_is_not_granted_because_create_rollback_and_delete_do_not_need_it():
    assert "apigateway:UntagResource" not in _actions(DEPLOYER)
    assert not pt.allowing_statements(_deployer_rendered(), "apigateway:UntagResource", STAGE_FAILURE_ARN)


@pytest.mark.parametrize("label, mutate", [
    ("statement missing (the policy that failed in AWS)",
     lambda pol: pol["Statement"].remove(next(s for s in pol["Statement"] if s["Sid"] == "HttpApiStageTagOnCreate"))),
    ("wrong action", lambda pol: next(s for s in pol["Statement"] if s["Sid"] == "HttpApiStageTagOnCreate").update(Action="apigateway:PATCH")),
    ("wrong resource pattern", lambda pol: next(s for s in pol["Statement"] if s["Sid"] == "HttpApiStageTagOnCreate").update(
        Resource="arn:aws:apigateway:ap-south-1::/apis/*/stages/*")),
    ("wrong region", lambda pol: next(s for s in pol["Statement"] if s["Sid"] == "HttpApiStageTagOnCreate").update(
        Resource="arn:aws:apigateway:us-east-1::/apis/*/stages")),
])
def test_checker_fails_without_the_stage_tag_permission(label, mutate):
    pol = _deployer_rendered()
    mutate(pol)
    errors, _ = pt.check(pol, ACCT)
    assert f"apigateway:TagResource on {STAGE_FAILURE_ARN}: NOT allowed" in errors, label
    assert any(e.startswith("missing vs repository: apigateway:tagresource on arn:aws:apigateway:ap-south-1::/apis/*/stages") for e in errors), label
