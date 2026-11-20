# Generated: Harbor's task.toml field names

`task_config_fields.json` lists every field of `harbor.models.task.config.TaskConfig`
(nested sections as `section.field`) in Harbor 0.26.0, the version the platform runs (first dumped from 0.23.0; the list was identical on 0.26.0, checked 2026-11-20).
It was dumped from the installed package, not written by hand. Flag rule 15.17 uses
it to show that a Rubric Check failure calling a field such as `environment.network_mode`
or `environment.os` invalid is contradicted by Harbor's own schema.
`tests/test_flags.py` compares it with the Harbor that `HARBOR_BIN` points at when one is present.
