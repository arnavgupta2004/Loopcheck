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
# Only these read-only actions have no resource-level support and may use Resource "*".
STAR_RESOURCE_OK = {"lambda:GetAccountSettings", "cloudformation:ValidateTemplate"}


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
                   "SignInLocalDevelopmentAccess", "ConcurrencyMode=unreserved", "never create access keys", "AdministratorAccess"):
        assert needle in doc, needle
    runbook = (ROOT / "docs" / "d4_demo_and_deployment.md").read_text()
    assert "AWS_PROFILE_NAME=loopcheck-deployer" in runbook and "aws configure sso --profile loopcheck " not in runbook
