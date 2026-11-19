# Past client-feedback checklist

Real feedback across multiple delivery rounds on this platform (pg-wal-recovery,
then harrow-ledger-migration). Each item below is phrased as something to
actually go check on a new deliverable, not just background reading. Most are
one `grep` or one file read away from a yes/no answer.

## Checksum / packaging integrity

- [ ] `CHECKSUM.sha256` uses **relative paths only**, computed **after**
      everything else is final (not before a late edit), and self-verifies
      100% (`shasum -a 256 -c CHECKSUM.sha256`). A past round shipped a
      checksum file whose quoted hash didn't match the actual folder.
- [ ] The task checksum(s) quoted in `README.md`/`results.json` are
      **independently reproduced** (see SKILL.md's dirhash recipe), not just
      copied from the platform's own report.
- [ ] Zero host-machine usernames or absolute paths survive anywhere in the
      package — `grep -rl "<username>"` across the *whole* deliverable
      directory, not a spot check. One round shipped with 26 files still
      containing `/home/<user>/...` paths after a redaction pass that only
      checked a few files.
- [ ] Zero of *your own* working-directory paths (scratchpad, temp venvs,
      extraction dirs) leaked into any shipped file.
- [ ] The deliverable folder/zip name is clean — no spaces, no parenthetical
      suffixes like `(1)`, `(3)` (these leak in from re-downloaded files if
      you're not careful about which copy you zip from).
- [ ] No rubric file shipped ambiguously — if the task has one, either omit
      it or label it clearly as a reviewer guide with the right extension and
      line endings; most tasks correctly have none, confirmed by Static
      Checks.

## Evidence completeness

- [ ] `results.json` has **pass@1 through pass@n**, not just a couple of
      k-values. Compute all of them with the standard estimator.
- [ ] Each rollout folder has the agent's **actual output artifacts**
      (whatever the task's own `artifacts` list in `task.toml` names — e.g. a
      written script, exported files, a submitted report), not just a
      verifier log that says "assertion error" with no detail.
- [ ] `verifier/ctrf.json` (or equivalent) has the **full** test output with
      real assertion diffs, not truncated to "N failed."
- [ ] Static-check log, TB-official-static-check log, and the build log are
      all **included as files**, not just summarized.
- [ ] The rubric review actually used is the richer, named one
      (`task-implementation.toml`, 35 criteria) if that's what was intended —
      check `rubric` in the JSON, don't assume; it's easy to accidentally run
      against Harbor's bare default (11 criteria) instead.
- [ ] Failure Analysis actually used the richer, named rubric
      (`trial-analysis.toml` + `--job-prompt trial-analysis-job.txt`, 6
      criteria) if intended, not Harbor's bare default (2 criteria:
      `reward_hacking`, `task_specification` only).
- [ ] The Cheat Trial has its **full raw job folder** included
      (`agent/trajectory.json`, verifier output, artifacts), not just a short
      log excerpt. "Only a log excerpt was saved" was flagged explicitly as a
      regression in one round.
- [ ] One frozen task version across the whole run set: Build, every Oracle
      run, Nop, every Agent Trial, and the Cheat Trial should trace to a
      checksum you can explain (canonical, or canonical+edit for the Cheat
      Trial) — not a mix of runs against different machines/copies. A past
      round had Oracle run locally on a Mac while Nop and the rollouts ran
      elsewhere, on different task copies.
- [ ] Oracle run count matches what was actually promised/expected (e.g. "5
      runs" in the README should mean 5 files are actually there, not 4).

## Prose / overclaiming

- [ ] No claim about the Cheat Trial's outcome goes further than what the
      actual transcript supports. Don't write "the anti-cheat design held" or
      "trap held" from the reward number alone — read `trajectory.json` first.
      This exact overclaim was flagged and had to be deleted in a prior round.
- [ ] If the Cheat Trial ran under different settings (agent version, model,
      reasoning effort) than the scored rollouts, **say so explicitly** with
      the real values — don't claim consistency without checking.
- [ ] No invented taxonomy in the verdict (e.g. a "Medium difficulty band"
      that isn't a real published spec) — state the actual numbers plainly
      (pass rate, agent, model, oracle result) instead.
- [ ] No internal platform jargon or code references anywhere client-facing
      (gate-ID names like "B7"/"C11", `harbor_runner.py`, cost-tier framing
      like "we used the cheapest model" — describe the actual mechanism
      instead, not the cost reason).
- [ ] No draft/placeholder language ships: `[fill]`, "I'll add this," TODO,
      "not included yet" without a resolution.
- [ ] "How to reproduce" tells the reader to run Harbor directly against the
      task folder — never "upload to [internal platform name]," which the
      client has no access to.
- [ ] The results table doesn't invent a client bar that was never stated;
      if there's a real stated bar, quote it exactly, otherwise report the
      number as-is with no comparison.
- [ ] A rubric/judge criterion that fails should be checked against its own
      stated reasoning before being reported as a real finding — if the
      judge's explanation doesn't actually address the criterion's question
      (common with cheap models on nuanced criteria), say so and cross-check
      against independent stronger signals before asserting it's a genuine
      issue either way.

## Platform-side (less relevant once already fixed upstream, check if unsure)

- [ ] One Harbor job per run set, not mixed machines/local-vs-platform runs.
- [ ] `pass@k` computed for every k from 1 to n with the standard estimator,
      not just powers-of-2/multiples-of-5 (Harbor's own job-level reporting
      only fills those in by default).
- [ ] Host paths and usernames stripped at export time, for every log and
      every JSON file, not selectively.
- [ ] Known infra-noise failures (timeout, rate limit, Docker orchestration
      error) get one automatic retry and are flagged as such, not silently
      treated as a genuine task-solving failure or silently dropped.
