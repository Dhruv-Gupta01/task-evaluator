# Hashed expected answers: rules for task authors

**Status: the static check (step 1), the plain-answer warning (step 2) and the damaged-hash test (step 4) are built.
Nothing blocks agent runs yet, and there is no exemption: a task that fails is revised.**

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

### 5. When an answer cannot be hashed, revise the task

There are no exemptions. If a comparison cannot use a hash (a wide numeric tolerance, or a value computed while
grading), change the task so that it can, for example by listing every accepted value (rule 3).

## How to convert a task

You change only `tests/`. The instruction, solution and environment stay as they are.

### Step 1. Work out the hash of each expected answer

Run this once on your own machine for each answer, using **the same method your grader will use**.

For a Python grader:

```python
import hashlib, json

def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()

print(digest(756))
```

For a shell grader, which hashes the raw text:

```bash
printf '%s' "Example Domain" | sha256sum | cut -d' ' -f1
```

The two methods give different hashes for the same text (the Python one hashes the JSON form, quotes
included), so do not mix them.

### Step 2. Store the hash under the right name and delete the plain value

Before:

```json
{ "bad_xid": 756 }
```

After:

```json
{ "bad_xid_sha256": "<64-character hash>" }
```

| Situation | How to store it |
|---|---|
| The output must match | `name_sha256` |
| The output must **not** match (a wrong state) | `name_not_sha256` |
| Several values are accepted | one list: `name_sha256: ["h1", "h2"]` |

The name works as a JSON key, a Python constant (`EXPECTED_SHA256 = {...}`) or a shell variable
(`expected_sha256="..."`). Write the 64 characters in the file; do not compute them while grading.

### Step 3. Change the grader to hash the output and compare

Python:

```python
# before
assert report["bad_xid"] == EXPECTED["bad_xid"]
# after
assert digest(report["bad_xid"]) == EXPECTED["bad_xid_sha256"]
```

Shell:

```bash
actual=$(tr -d '\r\n' < /app/title.txt | sha256sum | cut -d' ' -f1)
[ "$actual" = "$expected_sha256" ]
```

Hash the output in exactly the form you used in step 1. `2` and `2.0` give different hashes, as do a
trailing newline or different letter case.

### Step 4. Remove other plain copies

Check comments, other data files and generator scripts under `tests/`.

### Step 5. Bump the version

A `tests/` change is a minor version bump. Write it as `version` under `[task]` in `task.toml`.

### What the platform then checks

| Step | Passes when |
|---|---|
| Static Checks (`hash_answers`) | there is at least one correctly named hash |
| Static Checks (`tests_plain_answers`) | ideally silent; if it warns, confirm those values are inputs or hash them |
| Oracle | every declared solution scores 1 (3 runs each), so hashing did not break grading |
| Nop | scores 0 |
| Verify hashing | damaging each hash lowers the score |

### Common reasons it fails

- The old plain comparison is still there, so damaging the hash changes nothing.
- The output is formatted differently from what was hashed, so the Oracle scores 0.
- Accepted alternatives are stored under separate names, so the unused ones look unused. Put them in one list.
- A "must not match" value is named `_sha256` instead of `_not_sha256`.

### If your task cannot do this

A task with nothing to hash, or a comparison that cannot use a hash, fails Static Checks. There is no
exemption: the task has to be revised.

## How a task is checked

| Step | What it checks | Result if it fails |
|---|---|---|
| 1. Static check | at least one `_sha256` or `_not_sha256` entry exists | Static Checks fail |
| 2. Search for plain answers | values that look like expected answers still in clear text | warning only |
| 3. Oracle and Nop | the reference solution scores 1 and doing nothing scores 0 | a failed Oracle holds back later stages; Nop holds back nothing |
| 4. Damaged-hash test | each entry really decides the score (below) | the hash proof fails |

### The damaged-hash test

One entry at a time, on a temporary copy, running the reference solution:

- `_sha256` (a single value or a whole list): replace it with a wrong digest. The score must fall below 1.
- `_not_sha256`: replace it with the digest of the right answer. The score must fall below 1.

A list is damaged as a whole, because the reference solution uses only one of the accepted values.

Every solution the task declares (`oracle_solutions`) runs on each damaged copy, and the score has fallen only when
it has fallen for all of them: one solution that still scores 1 shows its path does not use that entry.

## What these rules do not guarantee

- **That every answer is hashed.** No automatic check can tell a plain answer from a test input. No check settles this.
- **That a hashed answer is hidden.** A short or predictable value (an id, a small number, a time on a
  known day) can be recovered by trying values until one matches. pg-wal-recovery's transaction ids were
  recovered from their fingerprints in milliseconds.
- **That the agent cannot get the answer elsewhere**, for example from a reachable server that gives the
  answer for every test input.
- **That a decoy was not used.** Hashing one unimportant value satisfies step 1; no automatic step catches it.

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
