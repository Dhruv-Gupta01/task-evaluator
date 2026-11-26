#!/bin/bash
set -euo pipefail

# Picks one of the correct solutions; task.toml lists them as [metadata] oracle_solutions.
case "${SOLUTION:-1}" in
  1) bash /solution/solve_1.sh ;;
  2) bash /solution/solve_2.sh ;;
  *) echo "unknown SOLUTION" >&2; exit 2 ;;
esac
