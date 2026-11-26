#!/usr/bin/env bash
# Builds build/lambda/: the digestbot package, its dependencies and the AWS
# config, ready for `aws cloudformation package`. Targets Lambda's arm64
# Python 3.13 runtime, so it works the same from a Mac or from CI.
#
# boto3 is not bundled: the Lambda Python runtime already includes it.
set -euo pipefail
cd "$(dirname "$0")/../.."

out=build/lambda
rm -rf "$out"
mkdir -p "$out"

# Wheels only, for Linux arm64: pydantic-core is compiled, and a wheel built
# for this machine would not load on Lambda. tzdata is included because the
# runtime image may not ship a time-zone database for zoneinfo.
python3 -m pip install --quiet --target "$out" \
  --platform manylinux2014_aarch64 --implementation cp --python-version 3.13 \
  --only-binary=:all: \
  "httpx>=0.27" "feedparser>=6.0.14" "beautifulsoup4>=4.12" "pydantic>=2.7" "tzdata>=2024.1"

cp -R digestbot "$out/"
cp deploy/aws/config.toml "$out/config.toml"
find "$out" -name __pycache__ -prune -exec rm -rf {} +

echo "built $out ($(du -sh "$out" | cut -f1))"
