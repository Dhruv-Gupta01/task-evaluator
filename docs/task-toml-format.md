# task.toml: the format a task must follow

Static Check `task_toml_format` compares your `task.toml` with Terminal-Bench's task template
(`backend/vendor/tb_template/task-template.toml`) and Harbor's field list. **A failing task is revised; there is
no exemption.** Comments and field order are not compared.

## A file that passes

```toml
artifacts = ["/app/output.json"]          # above the first [section]

[metadata]
author_name = "..."
author_email = "..."
author_github = "..."
author_organization = ""                  # optional
category = "Software"
subcategory = "..."
tags = ["..."]
expert_time_estimate_hours = 1
oracle_solutions = ["1", "2"]             # this platform's rule: docs/oracle-solutions.md

[verifier]
timeout_sec = 120.0
environment_mode = "separate"             # needs tests/Dockerfile

[agent]
timeout_sec = 120.0

[environment]
build_timeout_sec = 600.0
cpus = 1
memory_mb = 2048
storage_mb = 10240
gpus = 0
network_mode = "public"
```

## What fails

| Problem | Example |
|---|---|
| A template field is missing | no `cpus`, no `[agent]` section |
| A required text field is empty | `author_name = ""` |
| A field or section Harbor does not know | `memry_mb`, `[enviroment]` (Harbor ignores these silently, so the setting does nothing) |
| Wrong type | `cpus = "two"`, `tags = "python"` |
| `network_mode` is not `"public"` | in `[environment]`, or any override in `[agent]` or `[verifier]` |
| `environment_mode` is not `"separate"` | a shared verifier |
| `allow_internet` is present | out of date; use `network_mode = "public"` |

## What is only flagged (`task_toml_extra_fields`, does not fail)

- Extra fields under `[metadata]`.
- A top-level `version` key (Harbor does not read it).
