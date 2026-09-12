#!/bin/bash -l
if [ ! -d .git ]
then
  git init
  git checkout -b precommit_init
  git add .
fi
set -e
uv run --locked prek install --install-hooks
uv run --locked prek run --all-files
