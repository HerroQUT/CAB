#!/usr/bin/env bash
# Builds classifier/build/: handler + Linux x86_64 wheels for the Lambda runtime.
# boto3 is bundled because the s3vectors client needs a newer boto3 than some
# Lambda runtime images ship with.
set -euo pipefail
cd "$(dirname "$0")/../classifier"
rm -rf build && mkdir build
pip install -r requirements.txt --target build \
  --platform manylinux2014_x86_64 --implementation cp --python-version 3.12 \
  --only-binary=:all: --upgrade --quiet
cp handler.py build/
echo "built $(du -sh build | cut -f1) in classifier/build"
