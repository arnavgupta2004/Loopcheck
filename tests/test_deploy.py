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
case "$*" in
  "--version") echo "aws-cli/2.99.0 fake" ;;
  "configure list-profiles") printf 'default\nloopcheck\n' ;;
  "configure get region --profile loopcheck") echo "ap-south-1" ;;
  *"sts get-caller-identity"*) if [ -n "${FAKE_STS_FAIL:-}" ]; then echo "An error occurred (InvalidClientTokenId)" >&2; exit 254; fi
                               printf '111122223333\tarn:aws:iam::111122223333:user/demo\n' ;;
  *"lambda get-account-settings"*) printf '%s\n' "${FAKE_LIMITS:-1000	1000}" ;;
  *"lambda get-function-concurrency"*) printf '%s\n' "${FAKE_FN_RESERVED:-None}" ;;
  *"cloudformation describe-stacks --stack-name loopcheck-demo"*)
      if [ -n "${FAKE_STACK_EXISTS:-}" ]; then echo "CREATE_COMPLETE"; else echo "Stack does not exist" >&2; exit 254; fi ;;
  *"cloudformation describe-stack-resource --stack-name loopcheck-demo --logical-resource-id InspectFunction"*) echo "loopcheck-demo-InspectFunction-ABC123" ;;
  *"cloudformation describe-stacks"*) echo "Stack does not exist" >&2; exit 254 ;;
  *) echo "unexpected aws call: $*" >&2; exit 99 ;;
esac
"""
FAKE_SAM = r"""#!/usr/bin/env bash
echo "sam $*" >> "$CALL_LOG"
case "$1" in
  --version) echo "SAM CLI, version 1.170.0" ;;
  validate) echo "template.yaml is a valid SAM Template" ;;
  *) echo "unexpected sam call: $*" >&2; exit 99 ;;
esac
"""
READ_ONLY = re.compile(r"^(aws (--version|configure list-profiles|configure get region --profile loopcheck"
                       r"|--profile loopcheck --region ap-south-1 (sts get-caller-identity|lambda get-account-settings|lambda get-function-concurrency"
                       r"|cloudformation describe-stacks|cloudformation describe-stack-resource) .*)|sam (--version|validate --lint .*))$")


@pytest.fixture
def sandbox(tmp_path):
    """Temp copy of the repo layout with a small fake zip and fake aws/sam on PATH."""
    (tmp_path / "deploy").mkdir()
    shutil.copy(ROOT / "deploy" / "preflight.sh", tmp_path / "deploy" / "preflight.sh")
    shutil.copy(ROOT / "template.yaml", tmp_path / "template.yaml")
    shutil.copytree(ROOT / "loopcheck", tmp_path / "loopcheck", ignore=shutil.ignore_patterns("__pycache__"))
    (tmp_path / "deploy" / ".build").mkdir()
    with zipfile.ZipFile(tmp_path / "deploy" / ".build" / "loopcheck-lambda-x86_64.zip", "w") as z:
        z.writestr("cv2/cv2.abi3.so", b"\0")
        z.writestr("numpy/_core/_multiarray_umath.cpython-313-x86_64-linux-gnu.so", b"\0")
        for p in (ROOT / "loopcheck").glob("*.py"):
            z.writestr(f"loopcheck/{p.name}", p.read_bytes())
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
    assert not any(w in " ".join(calls) for w in ("deploy", "create-", "delete", "put-", "update", "package", " sync", " build", " rm "))


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


def test_preflight_fails_cleanly_without_aws_cli(tmp_path):
    shutil.copytree(ROOT / "deploy", tmp_path / "deploy", ignore=shutil.ignore_patterns(".build"))
    env = {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path)}
    p = subprocess.run(["bash", str(tmp_path / "deploy" / "preflight.sh")], capture_output=True, text=True, env=env, timeout=60)
    if shutil.which("aws", path=env["PATH"]):
        pytest.skip("a real aws CLI is installed in /usr/bin")
    assert p.returncode == 1 and "Cannot continue without the AWS CLI" in p.stdout
