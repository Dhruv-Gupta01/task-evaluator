# Hashed expected answers: rules for task authors

**Status: the static check (step 1), the plain-answer warning (step 2) and the damaged-hash test (step 4) are built.
Nothing blocks agent runs yet, and there is no reviewer exemption or sign-off (rule 5, step 5).**

## Why

An agent that gets sight of a task's tests should not be able to copy the answers out of them.
So `tests/` stores a fingerprint of each expected answer, and the grader hashes the agent's output and
compares fingerprints.

## What you must do

### 1. Hash every exact expected answer

Use SHA-256, written as 64 lowercase hex characters. Remove the plain value from `tests/`, including
copies in other files, comments and generator scripts. A hash beside the plain value hides nothing.

Test inputs and settings are not answers and are not hashed.

### 2. Name every fingerprint so it can be found

| Suffix | Meaning | Grader passes when |
|---|---|---|
| `<name>_sha256` | the answer the output **must** match | the digests are equal |
| `<name>_not_sha256` | a wrong state the output **must not** match | the digests differ |

The name works in JSON keys, code constants (`EXPECTED_SHA256`) and shell variables (`expected_sha256`).

### 3. Put several accepted values in one list

When more than one value is correct (two accepted ids, or each second within a one-second tolerance),
store them together:

```json
{ "commit_time_sha256": ["<hash of 13:29:41>", "<hash of 13:29:42>", "<hash of 13:29:43>"] }
```

The grader passes when the output's digest is any one of them.

### 4. Normalise before hashing, the same way on both sides

A hash needs an exact match, so decide the form once and write it in the grader:

- JSON values: `json.dumps(value, sort_keys=True, separators=(",", ":"))`, then UTF-8.
- Numbers: one type and one precision (so `2` and `2.0` cannot differ). Money: a fixed number of decimals.
- Text: say whether surrounding whitespace, line endings and letter case count.
- Files: the exact bytes.

The reference solution must still score 1 after you hash.

### 5. When an answer cannot be hashed, say why

Some comparisons cannot use a hash: a wide numeric tolerance, or a value computed while grading. Request
an exemption that names the value and gives the reason. A reviewer approves it with their name.

A task with nothing to hash (it only tests behaviour) also needs that approved exemption. It does not pass
automatically.

## How a task is checked

| Step | What it checks | Result if it fails |
|---|---|---|
| 1. Static check | at least one `_sha256` entry exists, or an approved exemption | block |
| 2. Search for plain answers | values that look like expected answers still in clear text | warning only |
| 3. Oracle and Nop | the reference solution scores 1 and doing nothing scores 0 | block |
| 4. Damaged-hash test | each entry really decides the score (below) | block |
| 5. Reviewer | no answer is left in plain text, and short answers are acceptable | block |

### The damaged-hash test

One entry at a time, on a temporary copy, running the reference solution:

- `_sha256` (a single value or a whole list): replace it with a wrong digest. The score must fall below 1.
- `_not_sha256`: replace it with the digest of the right answer. The score must fall below 1.

A list is damaged as a whole, because the reference solution uses only one of the accepted values.

## What these rules do not guarantee

- **That every answer is hashed.** No automatic check can tell a plain answer from a test input. Step 5 is
  a person for that reason.
- **That a hashed answer is hidden.** A short or predictable value (an id, a small number, a time on a
  known day) can be recovered by trying values until one matches. pg-wal-recovery's transaction ids were
  recovered from their fingerprints in milliseconds.
- **That the agent cannot get the answer elsewhere**, for example from a reachable server that gives the
  answer for every test input.
- **That a decoy was not used.** Hashing one unimportant value satisfies step 1; step 5 has to catch it.

## Evidence so far

Trial on copies of five tasks (2026-11-26), with a prototype static check that is not part of the platform:

- Plain answers were found in 3 of 5 unhashed tasks. It missed answers written inside `assert` lines and
  in a shell script.
- Hashes were found in 3 of 4 hashed tasks. It missed shell variables.
- A grader that computed a hash but decided on a plain value passed the static check. The damaged-hash
  test caught it.
- All four hashed copies scored 1 with the reference solution and 0 with Nop.
- Damaged-hash test, 33 runs on those four copies. All 12 hashes in the three small tasks dropped the score
  to 0. In pg-wal-recovery, damaging `target`, `bad_xid`, `time_exact` and `affected_accounts` dropped it to 0.
  The three accepted alternatives the reference solution does not use (`sub_xid`, `time_before`, `time_after`)
  and the seven "must not match" hashes stayed at 1, which is why rules 2 and 3 exist. Setting each
  "must not match" hash to the right answer's digest dropped the score to 0.
- On the platform, a pg-wal-recovery copy using the list form of rule 3 and `_not_sha256` passed the damaged-hash
  test with all 11 entries used (11 runs). See [`hash-discussion.md`](hash-discussion.md).
