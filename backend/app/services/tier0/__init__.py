"""Tier 0 of the cheat-trial loop: human-written static probes.

No agent and no model. A probe is a script that builds the files an exploit would leave
behind, the verifier is run on them, and the reward is read. Mutants (mutants.py) say
whether a reward of 0 means the defence held or the probe was broken.

Adapted from the Tier 0 harness in the BTA benchmark handover (adversarial/, see
PROVENANCE.md). Changes from the handover: it lives in the backend as a package, compose
tasks whose `main` is built from environment/Dockerfile are allowed, the task is matched
by its `[task] name` instead of its folder, mutants have a role (defence or fault), and
the judging logic is in runner.py so the platform and the CLI share it.
"""
