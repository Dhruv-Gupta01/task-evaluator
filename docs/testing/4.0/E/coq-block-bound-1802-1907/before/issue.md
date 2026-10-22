## Issue #1802: coq-block-bound not multi-platform

https://github.com/harbor-framework/terminal-bench/issues/1802 (open)

This task only works on linux/amd64 because the environment and verifier Dockerfiles use coqorg/coq:8.18, whose published image is linux/amd64 only.

@swarooprm can you please investigate this and see if you can feasibly modify the task to be multi-platform (also working on linux/arm64)?

## PR #1907: fix(coq-block-bound): build on a multi platform base so the task runs on arm64

https://github.com/harbor-framework/terminal-bench/pull/1907 (open)

## What this fixes

Closes #1802. The coq-block-bound task only worked on linux/amd64 because both the environment and verifier images were based on coqorg/coq:8.18, whose published image is linux/amd64 only. On linux/arm64 the image cannot be pulled, so the task failed.

## The change

Both Dockerfiles now use the ocaml/opam base image, which is published for both linux/amd64 and linux/arm64, and install Coq 8.18 through opam. Because opam builds Coq from source, the task now works on whichever architecture the image is built for.

Everything else is kept the same:
* the pinned Coq 8.18,
* the pinned pytest stack (pytest 9.1.1 and pytest-json-ctrf 0.5.2),
* the separate verifier layout, the sealed test venv, and the /app setup.

Two small adjustments were needed because the base user changed from coq to opam: the passwordless sudoers entry now names the opam user, and the coqc symlinks into /usr/local/bin now find the opam switch by glob so they do not depend on the switch name.

## Validation

Built the environment image on amd64. Inside it, coqc reports version 8.18.0 and the full reference proof in solution/Main.v compiles cleanly. The arm64 support follows from the multi platform base and the opam source build. A buildx run for linux/arm64 in CI is the final confirmation.
