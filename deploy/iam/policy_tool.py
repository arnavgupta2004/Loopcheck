#!/usr/bin/env python3
"""Render and check the LoopCheck deployer IAM policies. OFFLINE ONLY: never calls AWS.

  python3 deploy/iam/policy_tool.py render --account-id 123456789012
      -> deploy/.build/iam/deployer.json and bootstrap.json, ready to paste into the IAM console.
  python3 deploy/iam/policy_tool.py check attached.json --account-id 123456789012
  ... add --bucket my-existing-bucket to either command when deploying with --s3-bucket instead of --resolve-s3
      -> compares a policy copied from the IAM console (Policies -> LoopCheckDeployerPolicy ->
         Permissions -> JSON) with the repository policy, and tests the requests sam deploy makes.

The account ID is handled as a 12-character string, so a leading zero is never lost. Rendering
fails instead of writing a policy whose account field is empty, partial or still "ACCOUNT_ID"
(such a policy saves fine in IAM but matches nothing, which surfaces later as
"no identity-based policy allows the cloudformation:DescribeStacks action").
Standard library only (Python >= 3.8).
"""

import argparse
import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
PLACEHOLDER = "ACCOUNT_ID"
POLICIES = {"deployer": HERE / "loopcheck-deployer-policy.json", "bootstrap": HERE / "loopcheck-sam-bootstrap-policy.json"}
MANAGED_POLICY_LIMIT = 6144  # characters, whitespace excluded
SAM_BUCKET_PATTERN = "aws-sam-cli-managed-default-samclisourcebucket-*"
STACK, REGION = "loopcheck-demo", "ap-south-1"
# Requests sam deploy / sam delete / the preflight make, as IAM authorizes them ({a} = account).
# A not-yet-created stack is authorized as stack/<name>/* (exactly what the AccessDenied message shows).
KEY_REQUESTS = [
    ("cloudformation:DescribeStacks", "arn:aws:cloudformation:ap-south-1:{a}:stack/loopcheck-demo/*"),
    ("cloudformation:CreateChangeSet", "arn:aws:cloudformation:ap-south-1:{a}:stack/loopcheck-demo/*"),
    ("cloudformation:CreateChangeSet", "arn:aws:cloudformation:ap-south-1:aws:transform/Serverless-2016-10-31"),
    ("cloudformation:DescribeChangeSet", "arn:aws:cloudformation:ap-south-1:{a}:stack/loopcheck-demo/*"),
    ("cloudformation:ExecuteChangeSet", "arn:aws:cloudformation:ap-south-1:{a}:stack/loopcheck-demo/*"),
    ("cloudformation:DescribeStackEvents", "arn:aws:cloudformation:ap-south-1:{a}:stack/loopcheck-demo/*"),
    ("cloudformation:DeleteStack", "arn:aws:cloudformation:ap-south-1:{a}:stack/loopcheck-demo/*"),
    ("lambda:CreateFunction", "arn:aws:lambda:ap-south-1:{a}:function:loopcheck-demo-InspectFunction-AbC123"),
    ("iam:CreateRole", "arn:aws:iam::{a}:role/loopcheck-demo-InspectFunctionRole-AbC123"),
    ("iam:PassRole", "arn:aws:iam::{a}:role/loopcheck-demo-InspectFunctionRole-AbC123"),
    ("logs:CreateLogGroup", "arn:aws:logs:ap-south-1:{a}:log-group:/aws/lambda/loopcheck-demo-inspect"),
    # AWS::ApiGatewayV2::Stage create tags the stage at the Stages collection (observed AccessDenied
    # resource; CloudFormation schema: create = apigateway:POST + apigateway:TagResource).
    ("apigateway:POST", "arn:aws:apigateway:ap-south-1::/apis/ta8wy1elv5/stages"),
    ("apigateway:TagResource", "arn:aws:apigateway:ap-south-1::/apis/ta8wy1elv5/stages"),
    ("logs:DescribeLogGroups", "*"),
    ("lambda:GetAccountSettings", "*"),
]


def _as_list(v):
    return v if isinstance(v, list) else [v]


def _glob(pattern: str, value: str, *, case: bool) -> bool:
    rx = "".join(".*" if c == "*" else "." if c == "?" else re.escape(c) for c in pattern)
    return re.fullmatch(rx, value, 0 if case else re.IGNORECASE) is not None


def allowing_statements(policy: dict, action: str, resource: str) -> list:
    """Sids of Allow statements whose Action and Resource match (Conditions are not evaluated)."""
    out = []
    for st in _as_list(policy.get("Statement", [])):
        if st.get("Effect") != "Allow":
            continue
        if any(_glob(a, action, case=False) for a in _as_list(st.get("Action", []))) and \
           any(_glob(r, resource, case=True) for r in _as_list(st.get("Resource", []))):
            out.append(st.get("Sid", "?"))
    return out


def account_problems(policy: dict, account: str) -> list:
    """ARN account fields that are not exactly `account` (placeholder, empty, truncated, other)."""
    problems = []
    for st in _as_list(policy.get("Statement", [])):
        for r in _as_list(st.get("Resource", [])):
            parts = r.split(":")
            if len(parts) < 6 or parts[0] != "arn":
                if r != "*":
                    problems.append(f"{st.get('Sid', '?')}: not an ARN: {r}")
                continue
            service, acct = parts[2], parts[4]
            if service == "s3" or (service == "apigateway" and acct == "") or acct == "aws":
                continue  # these ARNs carry no account ID by design
            if acct != account:
                why = "placeholder not replaced" if PLACEHOLDER in acct else "empty account ID" if acct == "" else f"account {acct!r} != {account!r}"
                problems.append(f"{st.get('Sid', '?')}: {why}: {r}")
    return problems


def render(name: str, account: str, bucket: str = None) -> str:
    """Policy text with the account ID (and optionally an explicit artifact bucket) filled in."""
    if not re.fullmatch(r"\d{12}", account or ""):
        raise ValueError(f"account ID must be exactly 12 digits (keep leading zeros), got {account!r}")
    text = POLICIES[name].read_text().replace(PLACEHOLDER, account)
    if bucket is not None:
        if not re.fullmatch(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]", bucket):
            raise ValueError(f"not a valid S3 bucket name: {bucket!r}")
        text = text.replace(SAM_BUCKET_PATTERN, bucket)  # only this bucket, still only the loopcheck-demo/ prefix
    policy = json.loads(text)
    if PLACEHOLDER in text or account_problems(policy, account):
        raise ValueError(f"{name}: rendering left bad account fields: {account_problems(policy, account)}")
    if len(re.sub(r"\s", "", text)) > MANAGED_POLICY_LIMIT:
        raise ValueError(f"{name}: exceeds the {MANAGED_POLICY_LIMIT}-character managed policy limit")
    return text


def _pairs(policy: dict) -> set:
    return {(a.lower(), r) for st in _as_list(policy.get("Statement", [])) if st.get("Effect") == "Allow"
            for a in _as_list(st.get("Action", [])) for r in _as_list(st.get("Resource", []))}


def check(attached: dict, account: str, expected_name: str = "deployer", bucket: str = None) -> tuple:
    errors, notes = [], []
    errors += account_problems(attached, account)
    for action, res in KEY_REQUESTS if expected_name == "deployer" else []:
        arn = res.format(a=account)
        sids = allowing_statements(attached, action, arn)
        (notes if sids else errors).append(f"{action} on {arn}: " + (f"allowed by {', '.join(sids)}" if sids else "NOT allowed"))
    expected = json.loads(render(expected_name, account, bucket))
    missing = sorted(_pairs(expected) - _pairs(attached))
    extra = sorted(_pairs(attached) - _pairs(expected))
    errors += [f"missing vs repository: {a} on {r}" for a, r in missing]
    notes += [f"extra vs repository (review): {a} on {r}" for a, r in extra]
    return errors, notes


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("render", help="fill in the account ID and write deploy/.build/iam/*.json")
    r.add_argument("--account-id", required=True)
    r.add_argument("--out-dir", default=str(ROOT / "deploy" / ".build" / "iam"))
    r.add_argument("--bucket", help="explicit artifact bucket used with sam deploy --s3-bucket")
    c = sub.add_parser("check", help="check a policy JSON copied from the IAM console")
    c.add_argument("attached")
    c.add_argument("--account-id", required=True)
    c.add_argument("--expect", choices=sorted(POLICIES), default="deployer")
    c.add_argument("--bucket", help="explicit artifact bucket used with sam deploy --s3-bucket")
    args = ap.parse_args(argv)

    try:
        if args.cmd == "render":
            out = Path(args.out_dir)
            out.mkdir(parents=True, exist_ok=True)
            for name in POLICIES:
                if name == "bootstrap" and args.bucket:
                    continue  # an explicit, existing bucket needs no SAM bootstrap permissions
                (out / f"{name}.json").write_text(render(name, args.account_id, args.bucket))
                print(f"wrote {out / f'{name}.json'}")
            print(f"check: stack ARN in policy = arn:aws:cloudformation:{REGION}:{args.account_id}:stack/{STACK}/*")
            return 0
        attached = json.loads(Path(args.attached).read_text())
        errors, notes = check(attached, args.account_id, args.expect, args.bucket)
    except (ValueError, OSError, json.JSONDecodeError) as e:
        print(f"ERROR {e}")
        return 1
    for n in notes:
        print(f"OK    {n}" if "allowed by" in n else f"NOTE  {n}")
    for e in errors:
        print(f"ERROR {e}")
    print("CHECK: PASS" if not errors else f"CHECK: FAIL ({len(errors)} problem(s))")
    return 0 if not errors else 1


if __name__ == "__main__":
    sys.exit(main())
