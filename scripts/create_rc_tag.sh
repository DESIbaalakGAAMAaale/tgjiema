#!/usr/bin/env bash
set -Eeuo pipefail

TAG="${1:?usage: $0 rc-vX.Y.Z}"

if [[ ! "${TAG}" =~ ^rc-v[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
  echo "INVALID_RC_TAG_NAME: ${TAG}" >&2
  exit 30
fi

HEAD_SHA="$(git rev-parse HEAD)"
CURRENT_BRANCH="$(git branch --show-current)"

if [ "${CURRENT_BRANCH}" != "master" ]; then
  echo "NOT_ON_MASTER: ${CURRENT_BRANCH}" >&2
  exit 31
fi

if [ -n "$(git status --porcelain)" ]; then
  echo "WORKTREE_NOT_CLEAN" >&2
  exit 33
fi

if git show-ref --verify --quiet "refs/tags/${TAG}"; then
  echo "LOCAL_TAG_ALREADY_EXISTS: ${TAG}" >&2
  exit 34
fi

if git ls-remote --exit-code --tags origin "refs/tags/${TAG}" >/dev/null 2>&1; then
  echo "REMOTE_TAG_ALREADY_EXISTS: ${TAG}" >&2
  exit 35
fi

git fetch origin master --tags --prune
REMOTE_SHA="$(git rev-parse origin/master)"

if [ "${HEAD_SHA}" != "${REMOTE_SHA}" ]; then
  echo "HEAD_NOT_ORIGIN_MASTER: head=${HEAD_SHA} remote=${REMOTE_SHA}" >&2
  exit 32
fi

git tag -s -a "${TAG}" "${HEAD_SHA}" -m "${TAG} release candidate"
git verify-tag "${TAG}"

TAG_OBJECT="$(git rev-parse "${TAG}")"
PEELED_SHA="$(git rev-parse "${TAG}^{}")"
TAG_TYPE="$(git cat-file -t "${TAG}")"

if [ "${PEELED_SHA}" != "${HEAD_SHA}" ]; then
  echo "TAG_PEELED_SHA_MISMATCH: tag=${PEELED_SHA} head=${HEAD_SHA}" >&2
  exit 36
fi

if [ "${TAG_OBJECT}" = "${PEELED_SHA}" ] || [ "${TAG_TYPE}" != "tag" ]; then
  echo "TAG_IS_NOT_ANNOTATED" >&2
  exit 37
fi

printf 'tag=%s\n' "${TAG}"
printf 'tag_object=%s\n' "${TAG_OBJECT}"
printf 'source_sha=%s\n' "${PEELED_SHA}"
