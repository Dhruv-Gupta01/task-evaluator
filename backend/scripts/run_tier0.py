"""Run Tier 0 probes (static exploit scripts with mutant checks) on one task folder.

    uv run python scripts/run_tier0.py --task <dir with task.toml>
    uv run python scripts/run_tier0.py --task <dir> --probe I-a --probe I-b
    uv run python scripts/run_tier0.py --list

Needs Docker. Builds the task's environment image and its verifier image, so the first run
is slow. Exit code 1 if any probe was `exploited` or `fail_open`, 2 on a usage problem.
Results are written as JSON (default: ./tier0-<slug>.json).
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.tier0 import mutants, probes, record, replay, runner, selftest, taskspec  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--task", help="folder containing task.toml")
    ap.add_argument("--probe", action="append", default=[], help="probe id, repeatable")
    ap.add_argument("--platform", default="linux/amd64")
    ap.add_argument("--no-sanitize", dest="sanitize", action="store_false", default=True)
    ap.add_argument("--keep", action="store_true")
    ap.add_argument("--out")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--selftest", action="store_true", help="check the probe suite (and this task's mutant anchors); no Docker")
    ap.add_argument("--replay-from", help="a storage/submissions/<id> folder whose stored rollouts are regraded")
    ap.add_argument("--replay-limit", type=int, default=3)
    ap.add_argument("--no-probes", action="store_true", help="skip the probes (use with --replay-from)")
    args = ap.parse_args()

    if args.list:
        for p in probes.all_probes():
            scope = "all tasks" if "*" in p.applies_to else ",".join(p.applies_to)
            extra = []
            if p.positive_control:
                extra.append("defence control: " + ",".join(p.positive_control))
            if p.fault_mutants:
                extra.append("fault check: " + ",".join(p.fault_mutants))
            print(f"{p.pid:<5} {p.title}  [{scope}]" + (f"  ({'; '.join(extra)})" if extra else ""))
        print()
        for mid, m in sorted(mutants.REGISTRY.items()):
            print(f"{mid:<26} {m.role:<8} {m.defence}")
        return 0
    if args.selftest:
        spec = taskspec.load(args.task) if args.task else None
        problems = selftest.run(spec)
        print("selftest:", "ok" if not problems else f"{len(problems)} problem(s)")
        for p in problems:
            print("  -", p)
        return 1 if problems else 0
    if not args.task:
        ap.error("--task is required (or --list / --selftest)")

    try:
        spec = taskspec.load(args.task)
    except (ValueError, FileNotFoundError) as exc:
        print(f"cannot drive this task: {exc}", file=sys.stderr)
        return 2
    selected = probes.for_task(spec.slug, only=set(args.probe) or None)
    if not selected:
        print(f"no probes match {spec.slug!r}", file=sys.stderr)
        return 2
    print(f"task {spec.slug}: artifacts {spec.artifacts}, sidecars {spec.sidecars or 'none'}, "
          f"agent user {spec.agent_user or 'root'}, {len(selected)} probes\n")

    def show(i, n, res):
        ctl = {True: " (control ok)", False: " (CONTROL FAILED)", None: ""}[res.positive_control_ok]
        print(f"[{i}/{n}] {res.probe_id:<4} {res.verdict.upper():<12} reward={res.reward}{ctl}  "
              f"{res.duration_sec}s  {res.title}", flush=True)
        for note in res.notes:
            print(f"        - {note}")
        for f in res.fault_results:
            print(f"        * fault {f['mutant']}: reward {f['reward']} -> {f['outcome']}")

    opts = runner.Options(platform=args.platform, sanitize=args.sanitize, keep=args.keep)
    if args.replay_from:
        items = [(b, k, "this upload") for b, k in replay.collect(args.replay_from, spec, limit=args.replay_limit)]
        print(f"replaying {len(items)} stored rollout(s)")
        for r in runner.run_replays(spec, items, opts):
            print(f"  {r['kind']:<7} {r['key'].split('/')[-2]:<24} recorded {r['recorded']} replayed {r['replayed']} -> {r['verdict']}")
    if args.no_probes:
        return 0
    results = runner.run_suite(spec, selected, opts, show)
    out = Path(args.out) if args.out else Path(f"tier0-{spec.slug}.json")
    payload = record.write(out, results, {"task": spec.slug, "platform": args.platform, "sanitize": args.sanitize})
    s = payload["summary"]
    print(f"\nheld {s['held']}, exploited {s['exploited']}, fail_open {s['fail_open']}, "
          f"non-evidential {s['non_evidential']}; unproven {s['held_but_positive_control_failed'] or 'none'}")
    print(f"written: {out}")
    return 1 if (s["exploited"] or s["fail_open"]) else 0


if __name__ == "__main__":
    raise SystemExit(main())
