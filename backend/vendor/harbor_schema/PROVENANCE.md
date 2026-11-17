# Generated: Harbor's task.toml field names

`task_config_fields.json` lists every field of `harbor.models.task.config.TaskConfig`
(nested sections as `section.field`) in Harbor 0.23.0, the version the platform runs.
It was dumped from the installed package, not written by hand. Flag rule 15.17 uses
it to show that a Rubric Check failure calling a field such as `environment.network_mode`
or `environment.os` invalid is contradicted by Harbor's own schema.
`tests/test_flags.py` compares it with the installed Harbor when one is present.
