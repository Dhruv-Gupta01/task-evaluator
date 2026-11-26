"""task.toml against Terminal-Bench's template (vendored) and Harbor's own field list.

Missing, unknown, wrong-type and wrong-value fields fail; extra [metadata] fields are flagged only;
network_mode must be "public".
"""
from pathlib import Path

import pytest

from app.services import toml_format as tf
from app.services.static_checks import StaticCheckReport

TEMPLATE = (Path(__file__).parents[1] / "vendor" / "tb_template" / "task-template.toml").read_text()
FILLED = (
    TEMPLATE.replace('author_name = ""', 'author_name = "A"').replace('author_email = ""', 'author_email = "a@b.c"')
    .replace('author_github = ""', 'author_github = "someone"').replace('category = ""', 'category = "Software"')
    .replace('subcategory = ""', 'subcategory = "Testing"').replace("tags = []", 'tags = ["x"]')
    .replace("expert_time_estimate_hours = 0", "expert_time_estimate_hours = 1")
)


def check(tmp_path: Path, text: str) -> dict:
    (tmp_path / "task.toml").write_text(text)
    report = StaticCheckReport()
    tf.check_task_toml_format(tmp_path, report)
    return {r.name: (r.severity, r.message) for r in report.results}


def failed(tmp_path, text) -> str:
    severity, message = check(tmp_path, text)["task_toml_format"]
    assert severity == "fail", message
    return message


def test_the_filled_template_passes(tmp_path):
    results = check(tmp_path, FILLED)
    assert results["task_toml_format"][0] == "info" and "task_toml_extra_fields" not in results


def test_a_missing_field_and_a_missing_section_are_listed_together(tmp_path):
    text = FILLED.replace("cpus = 1", "").replace('[agent]\ntimeout_sec = 120.0', "")
    message = failed(tmp_path, text)
    assert "environment.cpus" in message and "agent.timeout_sec" in message


def test_a_required_field_left_empty_fails(tmp_path):
    assert "metadata.author_name is empty" in failed(tmp_path, FILLED.replace('author_name = "A"', 'author_name = ""'))


def test_the_optional_organization_may_be_empty_or_absent(tmp_path):
    text = "\n".join(line for line in FILLED.splitlines() if not line.startswith("author_organization"))
    assert check(tmp_path, text)["task_toml_format"][0] == "info"


@pytest.mark.parametrize("old,new,named", [
    ("memory_mb = 2048", "memry_mb = 2048", "environment.memry_mb"),
    ("[environment]", "[enviroment]\ncpus = 1\n\n[environment]", "enviroment"),
    ("[agent]\n", "[agent]\ntimeout_secs = 5\n", "agent.timeout_secs"),
])
def test_unknown_fields_and_sections_fail(tmp_path, old, new, named):
    message = failed(tmp_path, FILLED.replace(old, new, 1))
    assert named in message and "unknown" in message


def test_a_typo_gets_a_suggestion(tmp_path):
    assert "memory_mb" in failed(tmp_path, FILLED.replace("memory_mb = 2048", "memry_mb = 2048"))


@pytest.mark.parametrize("old,new,named", [
    ("cpus = 1", 'cpus = "two"', "environment.cpus"),
    ('tags = ["x"]', 'tags = "x"', "metadata.tags"),
    ('tags = ["x"]', "tags = [1, 2]", "metadata.tags"),
    ("timeout_sec = 120.0 # increase", 'timeout_sec = "long" # increase', "verifier.timeout_sec"),
    ('author_name = "A"', "author_name = 7", "metadata.author_name"),
    ("gpus = 0", "gpus = false", "environment.gpus"),
])
def test_wrong_types_fail(tmp_path, old, new, named):
    message = failed(tmp_path, FILLED.replace(old, new, 1))
    assert named in message and "must be" in message


def test_network_mode_must_be_public_and_present(tmp_path):
    assert 'network_mode must be "public"' in failed(tmp_path, FILLED.replace('network_mode = "public"', 'network_mode = "no-network"'))
    assert "environment.network_mode" in failed(tmp_path, FILLED.replace('network_mode = "public" # Terminal-Bench is open internet', ""))
    assert 'agent.network_mode must be "public"' in failed(tmp_path, FILLED.replace("[agent]\n", '[agent]\nnetwork_mode = "no-network"\n'))


def test_the_verifier_must_be_separate_and_allow_internet_is_out_of_date(tmp_path):
    assert '"separate"' in failed(tmp_path, FILLED.replace('environment_mode = "separate"', 'environment_mode = "shared"'))
    assert "allow_internet" in failed(tmp_path, FILLED.replace("gpus = 0", "gpus = 0\nallow_internet = true"))


def test_extra_metadata_is_flagged_but_does_not_fail(tmp_path):
    results = check(tmp_path, FILLED.replace("[metadata]\n", '[metadata]\ndifficulty = "easy"\n'))
    assert results["task_toml_format"][0] == "info"
    severity, message = results["task_toml_extra_fields"]
    assert severity == "warn" and "metadata.difficulty" in message


def test_oracle_solutions_and_the_task_section_are_expected(tmp_path):
    text = FILLED.replace("[metadata]\n", '[task]\nname = "terminal-bench/x"\n\n[metadata]\noracle_solutions = ["1", "2"]\n')
    results = check(tmp_path, text)
    assert results["task_toml_format"][0] == "info" and "task_toml_extra_fields" not in results


def test_a_legacy_top_level_version_is_flagged_but_does_not_fail(tmp_path):
    results = check(tmp_path, 'version = "2.0"\n' + FILLED)
    assert results["task_toml_format"][0] == "info" and "version" in results["task_toml_extra_fields"][1]


def test_free_form_tables_are_not_unknown(tmp_path):
    text = FILLED + '\n[environment.env]\nFOO = "1"\n\n[solution.env]\nBAR = "2"\n'
    assert check(tmp_path, text)["task_toml_format"][0] == "info"


def test_a_file_that_is_not_toml_fails(tmp_path):
    assert "could not be read" in failed(tmp_path, "[metadata\n")
