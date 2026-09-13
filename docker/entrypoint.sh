#!/bin/bash
set -e
if [ "$#" -eq 0 ]; then
  exec takoperator run
else
  exec "$@"
fi
