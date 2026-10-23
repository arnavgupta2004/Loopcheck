#!/usr/bin/env bash
# LoopCheck deployment preflight — READ-ONLY. Creates, modifies and deletes nothing.
#
#   bash deploy/preflight.sh
#   LOOPCHECK_AWS_PROFILE=loopcheck LOOPCHECK_AWS_REGION=ap-south-1 bash deploy/preflight.sh
#
# AWS calls made (all read-only): sts get-caller-identity, lambda get-account-settings,
# lambda get-function-concurrency (only if the stack exists), cloudformation describe-stacks (x2),
# cloudformation describe-stack-resource, and `sam validate --lint` (local lint).
# Credentials are never printed: this script never runs `aws configure list`, `env`, or
# anything that echoes key material.
#
# Concurrency: recommends ConcurrencyMode=reserved (ReservedConcurrentExecutions 2) when the
# account can keep >= 10 unreserved executions afterwards, otherwise ConcurrencyMode=unreserved
# (the property is omitted). It never recommends 0, which Lambda treats as "throttle everything".
set -uo pipefail

PROFILE="${LOOPCHECK_AWS_PROFILE:-loopcheck}"
REGION="${LOOPCHECK_AWS_REGION:-ap-south-1}"
STACK="loopcheck-demo"
RESERVED=2   # must equal the literal in template.yaml (tests/test_deploy.py checks)
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
export AWS_PAGER="" SAM_CLI_TELEMETRY=0

FAILS=0; WARNS=0
ok()   { printf '  [ OK ] %s\n' "$*"; }
warn() { printf '  [WARN] %s\n' "$*"; WARNS=$((WARNS + 1)); }
fail() { printf '  [FAIL] %s\n' "$*"; FAILS=$((FAILS + 1)); }
aws_ro() { aws --profile "$PROFILE" --region "$REGION" "$@"; }

echo "LoopCheck preflight  (profile=$PROFILE  region=$REGION  stack=$STACK)  — read-only"

echo "1. Tools"
if command -v aws >/dev/null; then ok "$(aws --version 2>&1 | head -1)"; else fail "AWS CLI v2 not found — install: https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html"; fi
if command -v sam >/dev/null; then ok "$(sam --version 2>&1 | head -1)"; else fail "SAM CLI not found — install: uv tool install aws-sam-cli   (or the official installer)"; fi
if command -v uv >/dev/null; then ok "$(uv --version 2>&1 | head -1)"; else fail "uv not found — needed by deploy/build_lambda_zip.sh"; fi
if command -v python3.13 >/dev/null; then ok "python3.13 found (sam build needs it for runtime python3.13)"; else warn "python3.13 not on PATH: 'sam build' will refuse; install with 'uv python install 3.13' and add it to PATH, or deploy without 'sam build' (runbook step 4b)"; fi
if ! command -v aws >/dev/null; then echo "Cannot continue without the AWS CLI."; echo "RESULT: FAIL ($FAILS failure(s), $WARNS warning(s)). Nothing was created or modified."; exit 1; fi

echo "2. Credential environment (names only, values never printed)"
set_vars=$(for v in AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_SESSION_TOKEN AWS_PROFILE AWS_DEFAULT_PROFILE; do [ -n "${!v:-}" ] && printf '%s ' "$v"; done)
if [ -n "$set_vars" ]; then warn "set in this shell: $set_vars— commands below pass --profile $PROFILE explicitly; consider 'unset' to avoid confusion"; else ok "no AWS credential variables set in the environment"; fi

echo "3. Profile"
if aws configure list-profiles 2>/dev/null | grep -Fxq "$PROFILE"; then ok "profile '$PROFILE' exists"; else
  fail "profile '$PROFILE' not found — create it locally: 'aws configure sso --profile $PROFILE' (recommended) or 'aws configure --profile $PROFILE'"; fi
cfg_region=$(aws configure get region --profile "$PROFILE" 2>/dev/null || true)
if [ "$cfg_region" = "$REGION" ]; then ok "profile region = $REGION"; else warn "profile region is '${cfg_region:-unset}'; every command passes --region $REGION explicitly"; fi

echo "4. Identity (confirm this is the intended account)"
if ident=$(aws_ro sts get-caller-identity --query '[Account, Arn]' --output text 2>&1); then
  read -r ACCOUNT ARN <<<"$ident"
  ok "account: $ACCOUNT"; ok "principal: $ARN"; ok "region: $REGION"
else
  fail "sts get-caller-identity failed: $(printf '%s' "$ident" | head -1 | cut -c1-200)"
  echo "   If you use SSO: aws sso login --profile $PROFILE"
fi

if [ "$FAILS" -eq 0 ]; then
  echo "5. Existing CloudFormation stacks"
  EXISTING_RESERVED=0
  if st=$(aws_ro cloudformation describe-stacks --stack-name "$STACK" --query 'Stacks[0].StackStatus' --output text 2>/dev/null); then
    warn "stack $STACK already exists ($st): 'sam deploy' would UPDATE it"
    fn=$(aws_ro cloudformation describe-stack-resource --stack-name "$STACK" --logical-resource-id InspectFunction --query 'StackResourceDetail.PhysicalResourceId' --output text 2>/dev/null || true)
    if [ -n "$fn" ] && cur=$(aws_ro lambda get-function-concurrency --function-name "$fn" --query 'ReservedConcurrentExecutions' --output text 2>/dev/null); then
      case "$cur" in ''|None|null) ok "existing function has no reserved concurrency (unreserved mode)" ;;
        *) EXISTING_RESERVED="$cur"; ok "existing function already reserves $cur (counted back in below)" ;; esac
    fi
  else ok "stack $STACK does not exist yet"; fi

  echo "6. Lambda concurrency mode (reserved 2 needs >= 10 unreserved executions left in the account)"
  RECOMMENDED_MODE=""
  if lim=$(aws_ro lambda get-account-settings --query '[AccountLimit.ConcurrentExecutions, AccountLimit.UnreservedConcurrentExecutions]' --output text 2>&1); then
    read -r TOTAL UNRES <<<"$lim"
    AVAILABLE=$((UNRES + EXISTING_RESERVED))   # this function's current reservation would be replaced, not added
    if [ $((AVAILABLE - RESERVED)) -ge 10 ]; then
      RECOMMENDED_MODE=reserved
      ok "account limit $TOTAL, unreserved $UNRES: reserving $RESERVED leaves $((AVAILABLE - RESERVED)) (>= 10) — supported"
    else
      RECOMMENDED_MODE=unreserved
      warn "account limit $TOTAL, unreserved $UNRES: reserving $RESERVED would leave $((AVAILABLE - RESERVED)) (< 10) — AWS would reject it"
      echo "         -> use unreserved mode: ReservedConcurrentExecutions is OMITTED, the function shares the account pool"
      echo "            (no per-function cap). API Gateway throttling (2 req/s, burst 5) still applies."
    fi
    echo "  RECOMMENDED: --parameter-overrides ... ConcurrencyMode=$RECOMMENDED_MODE   (never set reserved concurrency to 0: it throttles every request)"
  else warn "could not read Lambda account settings: $(printf '%s' "$lim" | head -1 | cut -c1-160) — default ConcurrencyMode=reserved may be rejected; unreserved is the fallback"; fi
  if aws_ro cloudformation describe-stacks --stack-name aws-sam-cli-managed-default >/dev/null 2>&1; then
    bucket=$(aws_ro cloudformation describe-stack-resource --stack-name aws-sam-cli-managed-default --logical-resource-id SamCliSourceBucket --query 'StackResourceDetail.PhysicalResourceId' --output text 2>/dev/null || echo "?")
    ok "SAM managed artifact bucket exists ($bucket) — shared by all SAM projects in this account/region; it will be reused"
  else ok "no SAM managed stack in $REGION — 'sam deploy --resolve-s3' will create aws-sam-cli-managed-default (one private S3 bucket)"; fi
fi

echo "7. Local build artifact (template.yaml CodeUri)"
ZIP="$ROOT/deploy/.build/loopcheck-lambda-x86_64.zip"
PY="${PYTHON:-python3}"
if [ ! -f "$ZIP" ]; then
  fail "no deployment zip — run: bash deploy/build_lambda_zip.sh (it removes the zip whenever a build fails)"
elif ! command -v "$PY" >/dev/null 2>&1; then
  fail "$PY not found — needed to verify the zip (set PYTHON=/path/to/python3)"
else
  # Portable structural verification (no GNU du/stat/sha256sum): required OpenCV 5 / NumPy compiled
  # files, Linux ELF x86-64 (not macOS) binaries, pinned versions, size, packaged code vs source.
  vout=$("$PY" -B "$ROOT/deploy/lambda_package.py" verify "$ZIP" --requirements "$ROOT/deploy/requirements-lambda.txt" \
         --arch x86_64 --source "$ROOT/loopcheck" 2>&1); vrc=$?
  while IFS= read -r line; do
    case "$line" in
      "INFO "*) ok "${line#INFO }" ;;
      "WARN "*) warn "${line#WARN } (rerun: bash deploy/build_lambda_zip.sh)" ;;
      "ERROR "*) fail "${line#ERROR }" ;;
    esac
  done <<<"$vout"
  if [ "$vrc" -eq 0 ]; then ok "zip verification: PASS"
  else fail "zip verification failed (exit $vrc) — rebuild with: bash deploy/build_lambda_zip.sh"; fi
fi

echo "8. Template validation"
if command -v sam >/dev/null; then
  # stdout and stderr are captured separately: SAM prints an unrelated "SAM CLI update available /
  # To download: ..." notice on stderr after the command, which must never be shown as the result.
  # Success requires exit code 0 AND the explicit "is a valid SAM Template" line on stdout.
  s_out=$(mktemp); s_err=$(mktemp)
  (cd "$ROOT" && sam validate --lint --template template.yaml --region "$REGION" --profile "$PROFILE") >"$s_out" 2>"$s_err"; s_rc=$?
  valid_line=$(grep -m1 "is a valid SAM Template" "$s_out" || true)
  if [ "$s_rc" -eq 0 ] && [ -n "$valid_line" ]; then
    ok "sam validate --lint (exit 0): $valid_line"
  else
    if [ "$s_rc" -eq 0 ]; then fail "sam validate --lint: exit 0 but no 'is a valid SAM Template' line — treating as FAILED"
    else fail "sam validate --lint FAILED (exit $s_rc)"; fi
    cat "$s_out" "$s_err" | grep -v -e "SAM CLI update available" -e "^To download:" -e '^[[:space:]]*$' | tail -15 | sed 's/^/      /'
  fi
  if grep -q "SAM CLI update available" "$s_err"; then
    ok "(info) $(grep -m1 "SAM CLI update available" "$s_err" | sed 's/^[[:space:]]*//') — informational only, not a validation result"
  fi
  rm -f "$s_out" "$s_err"
fi

echo
if [ -n "${RECOMMENDED_MODE:-}" ]; then echo "Concurrency: use ConcurrencyMode=$RECOMMENDED_MODE (see docs/d4_demo_and_deployment.md, runbook step 5)"; fi
if [ "$FAILS" -eq 0 ]; then echo "RESULT: PASS ($WARNS warning(s)). Nothing was created or modified. Review the account/region above before deploying."
else echo "RESULT: FAIL ($FAILS failure(s), $WARNS warning(s)). Nothing was created or modified."; fi
[ "$FAILS" -eq 0 ]
