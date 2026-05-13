#!/bin/bash
GIT_REV=$(git rev-parse --short HEAD)
GIT_DIRTY=$(git diff --quiet && git diff --cached --quiet || echo "-dirty")
BUILD_TIME=$(date +"%Y%m%d%H%M%S")
VERSION="${GIT_REV}${GIT_DIRTY}.${BUILD_TIME}"

echo "Building version: $VERSION"

esphome -s build_version "$VERSION" compile "$1"
