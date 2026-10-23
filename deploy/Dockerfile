# Alternative packaging: Lambda container image (use if the zip ever exceeds 250 MiB unzipped,
# e.g. after adding dependencies). Not built or pushed in D4: the build host had no Docker daemon.
#
#   docker build --platform linux/amd64 -f deploy/Dockerfile -t loopcheck-lambda .
#   docker run --rm -p 9000:8080 loopcheck-lambda      # local Lambda Runtime Interface Emulator
#   curl -s localhost:9000/2015-03-31/functions/function/invocations \
#        -d '{"rawPath":"/health","requestContext":{"http":{"method":"GET"}}}'
#
# For reproducibility, pin the base image by digest at build time
# (docker buildx imagetools inspect public.ecr.aws/lambda/python:3.13).
FROM public.ecr.aws/lambda/python:3.13

COPY deploy/requirements-lambda.txt /tmp/requirements-lambda.txt
RUN pip install --no-cache-dir --only-binary=:all: -r /tmp/requirements-lambda.txt \
 && python -c "import cv2, sys; v = cv2.__version__; print('OpenCV', v); sys.exit(0 if v.startswith('5.') else 1)"

COPY loopcheck/ ${LAMBDA_TASK_ROOT}/loopcheck/

CMD ["loopcheck.aws_lambda.handler"]
