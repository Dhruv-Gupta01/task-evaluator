# At least two correct solutions: rules for task authors

Every task must ship **at least two different correct solutions**. This shows the grader accepts more than the
author's first way of writing the answer.

## What you change

Only `solution/` and one line of `task.toml`.

### 1. Write a second solution

```
solution/
  solve.sh
  solve_1.sh
  solve_2.sh
```

`solve.sh` picks one through the `SOLUTION` variable, and falls back to the first:

```bash
#!/bin/bash
set -euo pipefail
case "${SOLUTION:-1}" in
  1) bash /solution/solve_1.sh ;;
  2) bash /solution/solve_2.sh ;;
  *) echo "unknown SOLUTION" >&2; exit 2 ;;
esac
```

### 2. Declare the correct values in `task.toml`

```toml
[metadata]
oracle_solutions = ["1", "2"]
```

List only correct solutions. A deliberately wrong script may stay in `solution/`, but must not be listed. Do not
set `SOLUTION` under `[solution.env]`: it overrides the value being tested.

### 3. Bump the version

A `solution/` change is a patch version bump.

## What the platform checks

| Step | Passes when |
|---|---|
| Static Checks (`oracle_solutions`) | at least two values are declared, `solution/` reads `$SOLUTION`, and the values do not run identical code |
| Solution Variants card ("Run all solutions") | every declared value scores 1 on every one of 3 runs |

## What "different" means

Two different approaches: another algorithm, library, order of steps, or an equally valid output form that the
instruction allows. A copy with a changed comment or variable name is not a second solution. The platform
catches only identical code; it cannot judge how different two scripts really are.

## If a correct second solution scores 0

The grader is too strict, or the instruction allows less than you think. Fix the grader or the instruction.
