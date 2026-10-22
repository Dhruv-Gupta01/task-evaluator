## Issue #1803: hof-topology-interpenetration not multi-platform

https://github.com/harbor-framework/terminal-bench/issues/1803 (open)

This task only works on linux/amd64 because the environment pins an ubuntu:24.04 digest that is a single linux/amd64 manifest.

@scaleai-bot can you please investigate this and see if you can feasibly modify the task to be multi-platform (also working on linux/arm64)?

## PR #1916: fix(hof-topology-interpenetration): unpin single-arch ubuntu digest

https://github.com/harbor-framework/terminal-bench/pull/1916 (open)

Did I write this PR description answering these questions and by my human hand? Yes.

What changes did I make?

I changed the base image in `tasks/hof-topology-interpenetration/environment/Dockerfile` from the pinned `ubuntu:24.04` sha256 digest to the multi-architecture index digest `sha256:33ceb71981b602c1a7443a53469e4dba065f7503eab3078a2d7a57a2ab987517` for the same `ubuntu:24.04` image.

Why did I make them?

I made this change because the pinned digest only supports amd64, which causes ARM machines to run the image through emulation instead of using a native ARM image. On my Mac, I measured the emulated build at 72.7 seconds compared with 19.5 seconds using the native image. I also checked the other tasks in the repository that pin digests, apart from the CUDA-based GPU task, and all of them point to multi-architecture images, so this appears to be the only affected task. I kept the image pinned by digest to preserve the reproducibility work from #1615 and #1824 rather than switching to a bare tag. The new digest points to the multi-architecture index, so Docker can select the correct image for each host architecture while keeping the build pinned.
