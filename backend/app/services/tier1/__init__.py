"""Tier 1 of the cheat-trial loop: a hinted agent that tries to get reward 1 from the task's
verifier without doing the work, with a read-only copy of tests/ in its container and a memory
of its earlier attempts. A hit is judged, an LLM fixer patches a *hardened copy* of tests/ and
environment/ (the upload is never touched), and the loop goes on until three attempts in a row
fail. See loop.py for the rules."""
