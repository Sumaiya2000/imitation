#!/usr/bin/env bash

# If you change these, also change .circleci/config.yml.
SRC_FILES=(src/ tests/ experiments/ setup.py)

set -x  # echo commands
set -e  # quit immediately on error

echo "Source format checking"
flake8 ${SRC_FILES[@]}
black --check ${SRC_FILES[@]}
codespell -I .codespell.skip --skip='*.pyc,tests/data/*,*.ipynb,*.csv' ${SRC_FILES[@]}

if [ -x "`which circleci`" ]; then
    circleci config validate
fi

if [ "$skipexpensive" != "true" ]; then
  echo "Building docs (validates docstrings)"
  pushd docs/
  make clean
  make html
  popd

  echo "Type checking"
  # Tell pytype Python path explicitly using -P: otherwise it gets lost
  # when the package is installed in editable mode (`pip -e .`). Note CI
  # checks still run using full install (`pip .`) which is more robust
  # but cumbersome for a Git commit hook.
  pytype -P src/:. ${SRC_FILES[@]}
fi
