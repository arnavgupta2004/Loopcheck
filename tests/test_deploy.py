"""D4.1: deployment configuration, logging protections and the read-only preflight."""

import base64
import json
import logging
import os
import re
import shutil
import stat
import subprocess
import zipfile
from pathlib import Path

import pytest
import yaml

from loopcheck import aws_lambda, service
from loopcheck.agent import HUMAN_REVIEW, InspectionAgent

ROOT = Path(__file__).resolve().parents[1]
SECRET = "DUMMY-SECRET-VALUE-0123456789"

ELF_X86_64 = b"\x7fELF\x02\x01\x01" + b"\0" * 11 + (0x3E).to_bytes(2, "little") + b"\0" * 44
ELF_AARCH64 = b"\x7fELF\x02\x01\x01" + b"\0" * 11 + (0xB7).to_bytes(2, "little") + b"\0" * 44
MACHO_ARM64 = b"\xcf\xfa\xed\xfe" + b"\0" * 60
NUMPY_PIN = re.search(r"numpy==([\d.]+)", (ROOT / "deploy" / "requirements-lambda.txt").read_text()).group(1)
WHEEL_LINUX = "Wheel-Version: 1.0\nRoot-Is-Purelib: false\nTag: cp313-cp313-manylinux_2_28_x86_64\n"


def fake_lambda_zip(path, *, cv2_bytes=ELF_X86_64, numpy_bytes=ELF_X86_64,
                    numpy_name="numpy/_core/_multiarray_umath.cpython-313-x86_64-linux-gnu.so",
                    cv_version="5.0.0.93", numpy_version=NUMPY_PIN, wheel=WHEEL_LINUX, omit=(), extra=None, source=None):
    """A small zip with the structure of the real Lambda package (headers only, not loadable)."""
    entries = {
        "cv2/cv2.abi3.so": cv2_bytes,
        "cv2/__init__.py": b"",
        numpy_name: numpy_bytes,
        f"opencv_contrib_python_headless-{cv_version}.dist-info/METADATA": f"Name: opencv-contrib-python-headless\nVersion: {cv_version}\n".encode(),
        f"opencv_contrib_python_headless-{cv_version}.dist-info/WHEEL": wheel.encode(),
        f"numpy-{numpy_version}.dist-info/METADATA": f"Name: numpy\nVersion: {numpy_version}\n".encode(),
        f"numpy-{numpy_version}.dist-info/WHEEL": wheel.encode(),
    }
    for p in Path(source or ROOT / "loopcheck").glob("*.py"):
        entries[f"loopcheck/{p.name}"] = p.read_bytes()
    entries.update(extra or {})
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in entries.items():
            if not any(name.startswith(o) for o in omit):
                z.writestr(name, data)
    return path


# --------------------------------------------------------------------------- template
class _CfnLoader(yaml.SafeLoader):
    pass


for _tag in ("!Ref", "!Sub", "!If", "!Not", "!Equals", "!GetAtt", "!Join", "!Select", "!Split", "!FindInMap"):
    _CfnLoader.add_constructor(_tag, lambda loader, node, t=_tag: {t: loader.construct_sequence(node) if isinstance(node, yaml.SequenceNode) else loader.construct_scalar(node)})


@pytest.fixture(scope="module")
def tpl():
    return yaml.load((ROOT / "template.yaml").read_text(), Loader=_CfnLoader)


def test_only_the_expected_resources_exist(tpl):
    types = {r["Type"] for r in tpl["Resources"].values()}
    assert types == {"AWS::Serverless::HttpApi", "AWS::Serverless::Function", "AWS::Logs::LogGroup"}


_NOVALUE = object()


def _resolve(node, params, conditions):
    """Tiny evaluator for the intrinsics this template uses in properties (!If, !Ref, !Equals, !Not)."""
    if isinstance(node, dict) and len(node) == 1:
        (tag, arg), = node.items()
        if tag == "!Ref":
            return _NOVALUE if arg == "AWS::NoValue" else params.get(arg, node)
        if tag == "!If":
            cond, a, b = arg
            return _resolve(a if conditions[cond] else b, params, conditions)
        if tag == "!Equals":
            x, y = (_resolve(v, params, conditions) for v in arg)
            return x == y
        if tag == "!Not":
            return not _resolve(arg[0], params, conditions)
    if isinstance(node, dict):
        out = {k: _resolve(v, params, conditions) for k, v in node.items()}
        return {k: v for k, v in out.items() if v is not _NOVALUE}
    if isinstance(node, list):
        return [_resolve(v, params, conditions) for v in node]
    return node


def _render(tpl, **overrides):
    params = {k: v.get("Default") for k, v in tpl["Parameters"].items()} | overrides
    conditions = {}
    for name, expr in tpl.get("Conditions", {}).items():
        conditions[name] = _resolve(expr, params, conditions)
    return {name: _resolve(r.get("Properties", {}), params, conditions) for name, r in tpl["Resources"].items()}


CONCURRENCY_MODES = ("reserved", "unreserved")


def test_concurrency_mode_parameter_has_no_numeric_or_zero_option(tpl):
    p = tpl["Parameters"]["ConcurrencyMode"]
    assert p["Type"] == "String" and p["AllowedValues"] == list(CONCURRENCY_MODES) and p["Default"] == "reserved"
    assert "ReservedConcurrency" not in tpl["Parameters"]  # the old numeric parameter (which accepted 0) is gone
    assert not re.search(r"ReservedConcurrentExecutions\s*:\s*0\b", (ROOT / "template.yaml").read_text())


def test_reserved_mode_sets_exactly_two(tpl):
    fn = _render(tpl, ConcurrencyMode="reserved")["InspectFunction"]
    assert fn["ReservedConcurrentExecutions"] == 2


def test_unreserved_mode_omits_the_property(tpl):
    fn = _render(tpl, ConcurrencyMode="unreserved")["InspectFunction"]
    assert "ReservedConcurrentExecutions" not in fn


@pytest.mark.parametrize("mode", CONCURRENCY_MODES)
def test_no_serving_configuration_resolves_to_zero(tpl, mode):
    fn = _render(tpl, ConcurrencyMode=mode)["InspectFunction"]
    assert fn.get("ReservedConcurrentExecutions", None) != 0
    assert fn.get("ReservedConcurrentExecutions", 2) >= 1


def test_preflight_reservation_constant_matches_template(tpl):
    assert re.search(r"^RESERVED=2\b", (ROOT / "deploy" / "preflight.sh").read_text(), re.M)
    assert _render(tpl, ConcurrencyMode="reserved")["InspectFunction"]["ReservedConcurrentExecutions"] == 2


@pytest.mark.parametrize("mode", CONCURRENCY_MODES)
def test_api_throttling_is_identical_in_both_modes(tpl, mode):
    api = _render(tpl, ConcurrencyMode=mode)["InspectApi"]
    assert api["DefaultRouteSettings"] == {"ThrottlingRateLimit": 2, "ThrottlingBurstLimit": 5}


DOC_FILES = ("README.md", "docs/d4_demo_and_deployment.md", "deploy/samconfig.example.toml", "deploy/preflight.sh", "template.yaml")


def test_documented_parameter_overrides_match_the_template(tpl):
    """Every Key=Value in documented --parameter-overrides / parameter_overrides is a real
    template parameter with an allowed value."""
    found = 0
    for f in DOC_FILES[:3]:
        text = (ROOT / f).read_text()
        for m in re.finditer(r"(?:--parameter-overrides|parameter_overrides\s*=\s*\")([^\n\\\"]*)", text):
            for key, value in re.findall(r"(\w+)=(\S+)", m.group(1)):
                found += 1
                assert key in tpl["Parameters"], f"{f}: unknown parameter {key}"
                allowed = tpl["Parameters"][key].get("AllowedValues")
                if allowed:
                    assert value in [str(a) for a in allowed], f"{f}: {key}={value} not in {allowed}"
    assert found >= 8
    for mode in CONCURRENCY_MODES:
        assert f"ConcurrencyMode={mode}" in (ROOT / "docs" / "d4_demo_and_deployment.md").read_text()


def test_concurrency_zero_is_only_ever_mentioned_as_a_warning():
    for f in DOC_FILES:
        for line in (ROOT / f).read_text().splitlines():
            if re.search(r"concurrency[^\n]{0,40}?\b0\b|\b0\b[^\n]{0,20}concurrency|ReservedConcurrency\w*=0", line, re.I):
                assert re.search(r"never|off switch|throttle", line, re.I), f"{f}: {line.strip()}"


@pytest.mark.parametrize("mode", CONCURRENCY_MODES)
def test_both_modes_validate_with_sam(tpl, mode, tmp_path):
    sam = shutil.which("sam")
    if not sam:
        pytest.skip("SAM CLI not installed")
    text = (ROOT / "template.yaml").read_text().replace("    Default: reserved\n", f"    Default: {mode}\n", 1)
    assert f"Default: {mode}" in text
    (tmp_path / "template.yaml").write_text(text)
    env = {k: v for k, v in os.environ.items() if not k.startswith("AWS_")} | {"SAM_CLI_TELEMETRY": "0", "HOME": str(tmp_path)}
    p = subprocess.run([sam, "validate", "--lint", "--template", str(tmp_path / "template.yaml"), "--region", "ap-south-1"],
                       capture_output=True, text=True, env=env, timeout=300)
    assert p.returncode == 0 and "is a valid SAM Template" in p.stdout + p.stderr, p.stdout + p.stderr


def test_api_throttling_is_two_per_second_burst_five(tpl):
    assert tpl["Parameters"]["ThrottleRatePerSecond"]["Default"] == 2
    assert tpl["Parameters"]["ThrottleBurst"]["Default"] == 5
    s = tpl["Resources"]["InspectApi"]["Properties"]["DefaultRouteSettings"]
    assert s == {"ThrottlingRateLimit": {"!Ref": "ThrottleRatePerSecond"}, "ThrottlingBurstLimit": {"!Ref": "ThrottleBurst"}}


def test_least_privilege_and_logging(tpl):
    fn = tpl["Resources"]["InspectFunction"]["Properties"]
    assert "Policies" not in fn and "Role" not in fn  # SAM attaches only AWSLambdaBasicExecutionRole
    assert fn["LoggingConfig"] == {"LogGroup": {"!Ref": "InspectLogGroup"}}
    assert tpl["Resources"]["InspectLogGroup"]["Properties"]["RetentionInDays"] == {"!Ref": "LogRetentionDays"}
    assert set(fn["Environment"]["Variables"]) == {"OPENCV_LOG_LEVEL"}  # no secrets in the environment


def test_runtime_handler_timeout_and_code_artifact(tpl):
    fn = tpl["Resources"]["InspectFunction"]["Properties"]
    assert fn["Runtime"] == "python3.13" and fn["Architectures"] == ["x86_64"] and fn["Timeout"] == 29
    mod, func = fn["Handler"].rsplit(".", 1)
    assert mod == "loopcheck.aws_lambda" and callable(getattr(aws_lambda, func))
    assert fn["Events"]["Inspect"]["Properties"]["TimeoutInMillis"] <= 30000
    # CodeUri must be the zip that the build script writes and verifies, never the unpacked folder
    assert fn["CodeUri"] == "deploy/.build/loopcheck-lambda-x86_64.zip"
    assert 'ZIP="$BUILD/loopcheck-lambda-$ARCH.zip"' in (ROOT / "deploy" / "build_lambda_zip.sh").read_text()


def test_cors_allows_exactly_one_origin_and_never_a_wildcard(tpl):
    cors = tpl["Resources"]["InspectApi"]["Properties"]["CorsConfiguration"]
    assert cors["AllowOrigins"] == [{"!Ref": "AllowedOrigin"}] and set(cors["AllowMethods"]) == {"GET", "POST", "OPTIONS"}
    pattern = tpl["Parameters"]["AllowedOrigin"]["AllowedPattern"]
    for good in ("http://localhost:8501", "https://ui.example.com", "https://demo.example.org:8443"):
        assert re.fullmatch(pattern, good)
    for bad in ("*", "https://*.example.com", "https://ui.example.com/path", "null", ""):
        assert not re.fullmatch(pattern, bad)


def test_lambda_requirements_match_lockfile():
    lines = [ln.strip() for ln in (ROOT / "deploy" / "requirements-lambda.txt").read_text().splitlines() if ln.strip() and not ln.startswith("#")]
    lock = (ROOT / "uv.lock").read_text()
    assert lines[0] == "opencv-contrib-python-headless==5.0.0.93" and len(lines) == 2
    numpy_pin = re.fullmatch(r"numpy==([\d.]+)", lines[1]).group(1)
    assert f'name = "numpy"\nversion = "{numpy_pin}"' in lock


def test_docs_match_the_configuration():
    doc = (ROOT / "docs" / "d4_demo_and_deployment.md").read_text()
    for needle in ("ap-south-1", "loopcheck-demo", "--profile loopcheck", "ConcurrencyMode=reserved", "ConcurrencyMode=unreserved", "2 requests/s", "burst 5",
                   "deploy/preflight.sh", "loopcheck-lambda-x86_64.zip", "sam delete", "not authentication", "cannot guarantee"):
        assert needle in doc, needle
    assert "NOT deployed" in doc or "not deployed" in doc


# --------------------------------------------------------------------------- logging protections
@pytest.fixture
def demo_body():
    d = ROOT / "data" / "demo_cases" / "01_clean"
    return json.dumps({"reference_b64": base64.b64encode((d / "reference.png").read_bytes()).decode(),
                       "inspection_b64": base64.b64encode((d / "inspection.png").read_bytes()).decode(),
                       "options": {"include_images": False}})


def _invoke(body, caplog, path="/inspect", method="POST"):
    caplog.clear()
    with caplog.at_level(logging.INFO, logger="loopcheck"):
        out = aws_lambda.handler({"rawPath": path, "body": body, "requestContext": {"http": {"method": method}}})
    return out, [r.getMessage() for r in caplog.records if r.name == "loopcheck"]


ALLOWED_LOG_KEYS = {"event", "route", "method", "body_chars", "request_id", "reference_bytes", "inspection_bytes", "include_images",
                    "decision", "rule", "tool_calls", "agent_errors", "status", "response_chars", "seconds", "error_code", "error_type", "error_at"}


def test_request_logs_contain_only_metadata(demo_body, caplog, monkeypatch):
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", SECRET)
    out, lines = _invoke(demo_body, caplog)
    assert out["statusCode"] == 200 and len(lines) == 1
    meta = json.loads(lines[0])
    assert set(meta) <= ALLOWED_LOG_KEYS and meta["decision"] == json.loads(out["body"])["decision"]
    b64 = json.loads(demo_body)["reference_b64"]
    for forbidden in (b64[:64], b64[1000:1064], SECRET, "reference_b64"):
        assert forbidden not in lines[0]


@pytest.mark.parametrize("body", ["{not json SECRET-IN-BODY", json.dumps({"reference_b64": "SECRET-IN-BODY", "inspection_b64": "x"})])
def test_rejected_requests_log_error_code_but_not_body(body, caplog):
    out, lines = _invoke(body, caplog)
    assert out["statusCode"] == 400 and len(lines) == 1
    meta = json.loads(lines[0])
    assert meta["error_code"] in ("invalid_json", "invalid_base64") and "SECRET-IN-BODY" not in lines[0]


def test_unhandled_error_logs_type_and_location_but_not_message(demo_body, caplog, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError(f"{SECRET} {json.loads(demo_body)['reference_b64'][:64]}")
    monkeypatch.setattr(aws_lambda, "analyze", boom)
    out, lines = _invoke(demo_body, caplog)
    body = json.loads(out["body"])
    assert out["statusCode"] == 500 and body["decision"] == HUMAN_REVIEW and body["error"]["message"] == "RuntimeError"
    meta = json.loads(lines[0])
    assert meta["error_type"] == "RuntimeError" and any("aws_lambda.py" in f for f in meta["error_at"])
    assert SECRET not in lines[0] and SECRET not in out["body"] and "Traceback" not in out["body"]


def test_safe_frames_never_include_messages():
    try:
        raise ValueError(SECRET)
    except ValueError as exc:
        frames = aws_lambda.safe_frames(exc)
    assert frames and all(SECRET not in f for f in frames)


# --------------------------------------------------------------------------- public responses never leak internals or approve
def test_public_response_strips_tracebacks_and_never_approves(demo_body, monkeypatch, caplog):
    def broken(self, *a, **k):
        raise RuntimeError(SECRET)
    monkeypatch.setattr(InspectionAgent, "tool_compare", broken)
    out, lines = _invoke(demo_body, caplog)
    resp = json.loads(out["body"])
    assert out["statusCode"] == 200 and resp["decision"] == HUMAN_REVIEW and resp["termination_rule"] == "X4"
    assert SECRET not in out["body"] and "Traceback" not in out["body"]
    assert resp["report"]["errors"] == [{"where": "compare:variant=default", "type": "RuntimeError"}]
    assert resp["reasons"] == ["internal failure (rule X4); details are withheld from API responses"]
    assert SECRET not in lines[0] and json.loads(lines[0])["agent_errors"] == ["RuntimeError"]


def test_local_mode_keeps_details_for_debugging(monkeypatch):
    monkeypatch.setattr(InspectionAgent, "tool_compare", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("visible locally")))
    d = ROOT / "data" / "demo_cases" / "01_clean"
    resp, _ = service.analyze((d / "reference.png").read_bytes(), (d / "inspection.png").read_bytes(), include_images=False)
    assert resp["decision"] == HUMAN_REVIEW and "visible locally" in json.dumps(resp["report"]["errors"])


# --------------------------------------------------------------------------- preflight (offline, fake aws/sam)
FAKE_AWS = r"""#!/usr/bin/env bash
echo "aws $*" >> "$CALL_LOG"
if [ -n "${FAKE_CFN_DENIED:-}" ] && [[ "$*" == *"cloudformation describe-stacks"* ]]; then
  echo "An error occurred (AccessDenied) when calling the DescribeStacks operation: User: arn:aws:iam::111122223333:user/loopcheck-deployer is not authorized to perform: cloudformation:DescribeStacks on resource: arn:aws:cloudformation:ap-south-1:111122223333:stack/loopcheck-demo/* because no identity-based policy allows the cloudformation:DescribeStacks action" >&2; exit 254
fi
case "$*" in
  "--version") echo "aws-cli/2.99.0 fake" ;;
  "configure list-profiles") printf 'default\nloopcheck-deployer\n' ;;
  "configure get region --profile loopcheck-deployer") echo "ap-south-1" ;;
  *"sts get-caller-identity"*) if [ -n "${FAKE_STS_FAIL:-}" ]; then echo "An error occurred (InvalidClientTokenId)" >&2; exit 254; fi
                               printf '111122223333\t%s\n' "${FAKE_ARN:-arn:aws:iam::111122223333:user/loopcheck-deployer}" ;;
  *"lambda get-account-settings"*) printf '%s\n' "${FAKE_LIMITS:-1000	1000}" ;;
  *"lambda get-function-concurrency"*) printf '%s\n' "${FAKE_FN_RESERVED:-None}" ;;
  *"cloudformation describe-stacks --stack-name loopcheck-demo"*)
      if [ -n "${FAKE_STACK_EXISTS:-}" ]; then echo "${FAKE_STACK_STATUS:-CREATE_COMPLETE}"; else echo "An error occurred (ValidationError) when calling the DescribeStacks operation: Stack with id loopcheck-demo does not exist" >&2; exit 254; fi ;;
  *"cloudformation describe-stack-resource --stack-name loopcheck-demo --logical-resource-id InspectFunction"*) echo "loopcheck-demo-InspectFunction-ABC123" ;;
  *"cloudformation describe-stacks"*) echo "An error occurred (ValidationError) when calling the DescribeStacks operation: Stack with id aws-sam-cli-managed-default does not exist" >&2; exit 254 ;;
  *) echo "unexpected aws call: $*" >&2; exit 99 ;;
esac
"""
FAKE_SAM = r"""#!/usr/bin/env bash
echo "sam $*" >> "$CALL_LOG"
case "$1" in
  --version) echo "SAM CLI, version 1.170.0" ;;
  validate)
    case "${FAKE_SAM_MODE:-valid}" in
      valid) echo "template.yaml is a valid SAM Template" ;;
      valid_with_update_notice)   # what real SAM prints when a newer version exists (notice on stderr)
        echo "template.yaml is a valid SAM Template"
        printf '\nSAM CLI update available (1.999.0); (1.170.0 installed)\nTo download: https://docs.aws.amazon.com/serverless-application-model/latest/developerguide/serverless-sam-cli-install.html\n' >&2 ;;
      lint_failure)
        echo "[[E3030: Check if properties have a valid value] ('python9' is not one of ...)]"
        echo "Error: Linting failed. At least one linting rule was matched to the provided template." >&2
        printf '\nSAM CLI update available (1.999.0); (1.170.0 installed)\nTo download: https://docs.aws.amazon.com/serverless-application-model/latest/developerguide/serverless-sam-cli-install.html\n' >&2
        exit 1 ;;
      exit0_without_valid_line) echo "something unexpected" ;;
    esac ;;
  *) echo "unexpected sam call: $*" >&2; exit 99 ;;
esac
"""
READ_ONLY = re.compile(r"^(aws (--version|configure list-profiles|configure get region --profile loopcheck-deployer"
                       r"|--profile loopcheck-deployer --region ap-south-1 (sts get-caller-identity|lambda get-account-settings|lambda get-function-concurrency"
                       r"|cloudformation describe-stacks|cloudformation describe-stack-resource) .*)|sam (--version|validate --lint .*))$")


@pytest.fixture
def sandbox(tmp_path):
    """Temp copy of the repo layout with a small fake zip and fake aws/sam on PATH."""
    (tmp_path / "deploy").mkdir()
    for f in ("preflight.sh", "lambda_package.py", "requirements-lambda.txt"):
        shutil.copy(ROOT / "deploy" / f, tmp_path / "deploy" / f)
    shutil.copy(ROOT / "template.yaml", tmp_path / "template.yaml")
    shutil.copytree(ROOT / "loopcheck", tmp_path / "loopcheck", ignore=shutil.ignore_patterns("__pycache__"))
    (tmp_path / "deploy" / ".build").mkdir()
    fake_lambda_zip(tmp_path / "deploy" / ".build" / "loopcheck-lambda-x86_64.zip")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, src in (("aws", FAKE_AWS), ("sam", FAKE_SAM)):
        f = bin_dir / name
        f.write_text(src)
        f.chmod(f.stat().st_mode | stat.S_IEXEC)
    return tmp_path


def _preflight(sandbox, **env):
    log = sandbox / "calls.log"
    full = {"PATH": f"{sandbox / 'bin'}:{os.environ['PATH']}", "CALL_LOG": str(log), "HOME": str(sandbox), **env}
    p = subprocess.run(["bash", str(sandbox / "deploy" / "preflight.sh")], capture_output=True, text=True, env=full, timeout=120)
    calls = log.read_text().splitlines() if log.exists() else []
    return p, calls


def test_preflight_is_read_only_and_prints_account_and_region(sandbox):
    p, calls = _preflight(sandbox, AWS_SECRET_ACCESS_KEY=SECRET)
    out = p.stdout + p.stderr
    assert p.returncode == 0 and "RESULT: PASS" in out, out
    assert "account: 111122223333" in out and "region: ap-south-1" in out and "Nothing was created or modified" in out
    assert "AWS_SECRET_ACCESS_KEY" in out and SECRET not in out  # names only, never values
    assert calls and all(READ_ONLY.match(c) for c in calls), [c for c in calls if not READ_ONLY.match(c)]
    joined = " ".join(calls).replace("--profile loopcheck-deployer", "--profile P")
    assert not any(w in joined for w in ("deploy", "create-", "delete", "put-", "update", "package", " sync", " build", " rm "))


def _no_zero_recommendation(out: str) -> None:
    for line in out.splitlines():
        if re.search(r"concurrency[^\n]{0,40}?\b0\b|=0\b", line, re.I):
            assert re.search(r"never|throttles", line, re.I), line


def test_preflight_recommends_reserved_mode_when_supported(sandbox):
    p, _ = _preflight(sandbox, FAKE_LIMITS="1000\t1000")
    assert p.returncode == 0 and "RECOMMENDED: --parameter-overrides ... ConcurrencyMode=reserved" in p.stdout
    assert "Concurrency: use ConcurrencyMode=reserved" in p.stdout
    _no_zero_recommendation(p.stdout)


def test_preflight_recommends_unreserved_mode_when_reservation_impossible(sandbox):
    p, _ = _preflight(sandbox, FAKE_LIMITS="10\t10")
    out = p.stdout
    assert p.returncode == 0 and "ConcurrencyMode=unreserved" in out and "ConcurrencyMode=reserved " not in out
    assert "OMITTED" in out and "API Gateway throttling (2 req/s, burst 5) still applies" in out
    _no_zero_recommendation(out)


def test_preflight_counts_existing_reservation_back_in_on_update(sandbox):
    # account limit 12, 10 unreserved because this function already reserves 2: re-reserving 2 is fine
    p, calls = _preflight(sandbox, FAKE_LIMITS="12\t10", FAKE_STACK_EXISTS="1", FAKE_FN_RESERVED="2")
    assert "already reserves 2" in p.stdout and "ConcurrencyMode=reserved" in p.stdout
    assert any("lambda get-function-concurrency --function-name loopcheck-demo-InspectFunction-ABC123" in c for c in calls)
    assert all(READ_ONLY.match(c) for c in calls)


def test_preflight_fails_and_stops_on_invalid_identity(sandbox):
    p, calls = _preflight(sandbox, FAKE_STS_FAIL="1")
    assert p.returncode == 1 and "RESULT: FAIL" in p.stdout and "InvalidClientTokenId" in p.stdout
    assert not any("lambda" in c or "cloudformation" in c for c in calls)


def test_preflight_defaults_to_the_dedicated_deployer_profile(sandbox):
    p, calls = _preflight(sandbox)
    assert p.returncode == 0 and "profile=loopcheck-deployer" in p.stdout and "dedicated LoopCheck deployment identity" in p.stdout
    assert any("--profile loopcheck-deployer --region ap-south-1 sts get-caller-identity" in c for c in calls)


def test_preflight_refuses_root_identity_and_stops_before_other_aws_calls(sandbox):
    p, calls = _preflight(sandbox, FAKE_ARN="arn:aws:iam::111122223333:root")
    assert p.returncode == 1 and "RESULT: FAIL" in p.stdout and "ROOT user" in p.stdout
    assert not any("lambda" in c or "cloudformation" in c for c in calls)


def test_preflight_warns_on_other_non_root_identity(sandbox):
    p, _ = _preflight(sandbox, FAKE_ARN="arn:aws:iam::111122223333:user/someone-else")
    assert p.returncode == 0 and "[WARN] not the dedicated LoopCheck deployer identity" in p.stdout


def test_preflight_reports_access_denied_instead_of_stack_does_not_exist(sandbox):
    # The real failure mode: a mis-rendered policy denies DescribeStacks; that must not read as "not created yet".
    p, calls = _preflight(sandbox, FAKE_CFN_DENIED="1")
    out = p.stdout
    assert p.returncode == 1 and "RESULT: FAIL" in out
    assert "[FAIL] cloudformation:DescribeStacks on stack/loopcheck-demo: AccessDenied" in out
    assert "no identity-based policy allows" in out and "policy_tool.py check" in out
    assert "does not exist yet" not in out
    assert all(READ_ONLY.match(c) for c in calls)


def test_preflight_treats_validation_error_as_new_stack(sandbox):
    p, _ = _preflight(sandbox)
    assert "stack loopcheck-demo does not exist yet (describe-stacks allowed" in p.stdout
    assert "no SAM managed stack in ap-south-1" in p.stdout


def test_preflight_explains_rollback_complete_recovery_without_touching_s3(sandbox):
    p, calls = _preflight(sandbox, FAKE_STACK_EXISTS="1", FAKE_STACK_STATUS="ROLLBACK_COMPLETE")
    out = p.stdout
    assert p.returncode == 0 and "RESULT: PASS" in out            # a warning, so the preflight can pass before the delete
    assert "is ROLLBACK_COMPLETE" in out and "would UPDATE" not in out
    assert "aws cloudformation delete-stack --stack-name loopcheck-demo --region ap-south-1 --profile loopcheck-deployer" in out
    assert "aws cloudformation wait stack-delete-complete --stack-name loopcheck-demo" in out
    assert "s3 rm" not in out and "sam delete" not in out
    # read-only: the preflight only prints the delete command, never runs it, and skips function lookups
    assert all(READ_ONLY.match(c) for c in calls) and not any("delete" in c or "get-function-concurrency" in c for c in calls)


def test_preflight_fails_cleanly_without_aws_cli(tmp_path):
    shutil.copytree(ROOT / "deploy", tmp_path / "deploy", ignore=shutil.ignore_patterns(".build"))
    env = {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path)}
    p = subprocess.run(["bash", str(tmp_path / "deploy" / "preflight.sh")], capture_output=True, text=True, env=env, timeout=60)
    if shutil.which("aws", path=env["PATH"]):
        pytest.skip("a real aws CLI is installed in /usr/bin")
    assert p.returncode == 1 and "Cannot continue without the AWS CLI" in p.stdout


# --------------------------------------------------------------------------- preflight: SAM output + zip verification
def _line(out: str, prefix: str) -> list[str]:
    return [ln for ln in out.splitlines() if prefix in ln]


def test_preflight_shows_validation_result_not_sam_update_notice(sandbox):
    p, _ = _preflight(sandbox, FAKE_SAM_MODE="valid_with_update_notice")
    ok_lines = _line(p.stdout, "[ OK ] sam validate --lint")
    assert p.returncode == 0 and len(ok_lines) == 1 and "is a valid SAM Template" in ok_lines[0]
    assert "To download" not in ok_lines[0]
    assert any("update available" in ln and "informational only" in ln for ln in p.stdout.splitlines())


def test_preflight_reports_sam_lint_failure_as_failure(sandbox):
    p, _ = _preflight(sandbox, FAKE_SAM_MODE="lint_failure")
    assert p.returncode == 1 and "RESULT: FAIL" in p.stdout
    assert "sam validate --lint FAILED (exit 1)" in p.stdout and "Linting failed" in p.stdout
    assert not _line(p.stdout, "[ OK ] sam validate")
    assert "To download" not in p.stdout.split("FAILED (exit 1)")[1].split("[ OK ]")[0]  # notice filtered from the error dump


def test_preflight_requires_explicit_valid_line_even_on_exit_zero(sandbox):
    p, _ = _preflight(sandbox, FAKE_SAM_MODE="exit0_without_valid_line")
    assert p.returncode == 1 and "exit 0 but no 'is a valid SAM Template' line" in p.stdout


def test_preflight_verifies_zip_structure_portably(sandbox):
    p, _ = _preflight(sandbox)
    assert "zip verification: PASS" in p.stdout and "opencv_contrib_python_headless==5.0.0.93" in p.stdout


@pytest.mark.parametrize("kwargs, message", [
    ({"cv2_bytes": MACHO_ARM64}, "macOS (Mach-O) binary"),
    ({"omit": ("cv2/cv2.abi3.so",)}, "missing OpenCV binary"),
    ({"cv_version": "4.10.0.84"}, "not 5.x"),
])
def test_preflight_fails_on_invalid_zip(sandbox, kwargs, message):
    fake_lambda_zip(sandbox / "deploy" / ".build" / "loopcheck-lambda-x86_64.zip", **kwargs)
    p, _ = _preflight(sandbox)
    assert p.returncode == 1 and message in p.stdout and "zip verification failed" in p.stdout


def test_preflight_reports_missing_zip(sandbox):
    (sandbox / "deploy" / ".build" / "loopcheck-lambda-x86_64.zip").unlink()
    p, _ = _preflight(sandbox)
    assert p.returncode == 1 and "no deployment zip" in p.stdout
