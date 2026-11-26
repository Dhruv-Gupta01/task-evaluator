# Hashed expected answers: how we got to this design

A record of the discussion of 2026-11-26, from the first request to what was built. The rules for task authors
are in [`hash-rules.md`](hash-rules.md).

## 1. The goal

Hashing expected answers should be **compulsory** for every task on the platform. An agent that gets sight of a
task's `tests/` should not be able to copy the answers: the task stores a fingerprint (SHA-256) of each answer,
and the grader hashes the agent's output and compares fingerprints.

## 2. First idea: look for plain answers

A static check (reading files without running them) that finds expected answers written in clear text. The main
branch has this as Flag 15.39.

**Why it was not enough:** the requirement is not "warn about plain answers" but "verify that the task hashes".

## 3. Second idea: a static check that verifies hashing

Check that fingerprints exist and that the grader's code appears to hash and compare them.

**The doubt:** reading code shows how it looks, not what it does. We tested a prototype to find out.

| Task copy | What it really does | Prototype said |
|---|---|---|
| Original, plain answer | plain | fail (right) |
| Hashed properly | compares digests | pass (right) |
| **Fake hashing**: computes a digest, only prints it, decides on plain values | not hashed | **pass (wrong)** |
| **Hashed properly, written differently** (`hashlib.new("sha256")` in a helper) | hashed | **fail (wrong)** |

## 4. Testing the static check on real tasks

**Unhashed originals** (all should fail):

| Task | Prototype said | Note |
|---|---|---|
| number-stats-csv | plain (1) | right |
| pg-wal-recovery | plain (63 keys) | only because the file is named `expected.json`; noisy |
| collab-editor-desync | plain (782 keys) | not checked how many are real answers |
| smoke-task | "no stored answers" | **missed**: answers are inside `assert` lines |
| webtask | "no stored answers" | **missed**: the answer is a string in `test.sh` |

**Genuinely hashed copies** (all should pass): number-stats-csv, pg-wal-recovery and smoke-task passed. webtask
failed although it was properly hashed, because shell variables were not read. All four scored 1 with the Oracle
and 0 with Nop, so the hashing was real and did not break grading.

**Conclusions**
1. Plain answers cannot be found reliably. In `assert summarize([3,1,2]) == {...}` nothing says which side is the
   input and which is the answer.
2. Hashes *can* be found reliably if they have a fixed name and shape. Hence the `_sha256` convention.
3. Hashed does not mean hidden: pg-wal's transaction ids (756, 757) were recovered from their fingerprints in
   milliseconds, and its commit time too, given the day.

## 5. Why the proof run

The proof run tests behaviour instead of reading code: **damage one stored digest and run the correct solution.**

- Score falls: the grader relies on that digest.
- Score stays 1: the digest is decoration.

The Oracle alone is not enough. A score of 1 only means hashing did not break grading; the fake-hashing copy also
scored 1.

| Run | Question it answers |
|---|---|
| Oracle | Is a correct answer still accepted? |
| Nop | Is doing nothing still rejected? |
| Damaged digest | Does the digest decide anything? |

**Prototype result:** on the two copies the static check got wrong, the proof run was right both times (fake
hashing stayed at 1; the differently written one fell to 0).

### The catch it exposed, and the two rules that came from it

33 damaged-digest runs on the four hashed copies:

| Task | Damaged | Fell to 0 | Stayed at 1 |
|---|---|---|---|
| smoke-task | 8 | 8 | 0 |
| webtask | 1 | 1 | 0 |
| number-stats-csv | 3 | 3 (each failing only its own check) | 0 |
| pg-wal-recovery | 14 | 4 | 10 |

The 10 that stayed at 1 were all legitimate:
- 3 were accepted **alternatives** the correct solution does not produce (the second transaction id, two of three
  times). Rule: put alternatives in **one list**, damaged as a whole.
- 7 were **"must not match"** digests: damaging one only makes it harder to match. Rule: name them
  `_not_sha256`; the proof then gives them a right answer's digest and expects the score to fall. Done by hand,
  all 7 fell to 0.

## 6. Options we considered and why we did not take them

| Option | Why not (or only partly) |
|---|---|
| Static check alone, as the blocker | wrong in both directions; easy to satisfy with the right words |
| Search for plain answers as the blocker | misses inline and shell answers; renaming defeats it; flags inputs. Kept as a **warning** |
| Match the Oracle's output against `tests/` | would catch renamed answers, but not tasks whose result is changed code or state, nor hidden-input answers. **Not tried** |
| Ask a model to find the answers | useful as a suggestion only; can miss or invent. Not tried |
| A declaration file, `tests/answers.json` | exact, but every task needs a new file; the `_sha256` naming was chosen as the lighter form |
| `_sha256` naming (chosen) | hashes become findable without a new file; plain leftovers still need a person |

## 7. What was built

| Part | Where | Behaviour |
|---|---|---|
| `hash_answers` | Static Checks | **fails** a task with no `_sha256` / `_not_sha256` entry under `tests/` |
| `tests_plain_answers` | Static Checks | **warns** about values that look like plain answers; "none seen" is stated as no proof |
| Hash proof | "Verify hashing" in the Static Checks card, `POST /submissions/{id}/hash-proof` | damages each entry and runs the Oracle; needs a passing Oracle first |
| Evidence | summary line and `hash_proof` in the download | which digests were shown to be used |

Code: `backend/app/services/hash_checks.py`. Tests: `test_hash_checks.py`, `test_hash_proof_api.py`.

**Not built**
- **Blocking.** Nothing stops agent runs. A failed `hash_answers` marks Static Checks failed, which is still
  advisory.
- **Reviewer exemption and sign-off** (rules 5 and step 5 of the rules).
- Reading inline asserts or shell strings as plain answers.

## 8. Platform results

The built version, on three copies uploaded to the platform (Build, Oracle, Static Checks, hash proof):

| Task | `hash_answers` | Hash proof | Runs |
|---|---|---|---|
| number-stats-csv, hashed | info (3 found) | **passed**, all 3 used | 3 |
| Fake hashing | info (3 found) | **failed**, all 3 not used | 3 |
| pg-wal in the rules' form (lists, `_not_sha256`) | info (11 found) | **passed**, all 11 used | 11 |

The fake copy is the point of the design: both static findings were quiet about it (its plain values are named
`WANT`, which the plain-answer warning does not see), and only the proof run caught it.

The built static check also does better than the prototype on the trial copies: all five unhashed originals fail for
having no hash (smoke-task and webtask included), and webtask's shell variable is found.

## 9. What none of this can show

- That **every** answer is hashed. A person has to confirm it.
- That a hashed answer is **hidden**. Short or predictable values can be guessed.
- That the agent cannot get the answer **elsewhere**, for example from a reachable server.
- That a **decoy** was not used: hashing one unimportant value satisfies the static check, and passes the proof.

## 10. Decisions still open

1. Should a failed check **block** agent runs, and with a reviewer override?
2. What happens to a task with nothing to hash, or an answer that cannot be hashed (a wide tolerance)?
3. Every existing task now fails `hash_answers`. Is that intended before authors have converted them?
