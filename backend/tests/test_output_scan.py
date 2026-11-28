"""The output scan (services/output_scan.py): an agent can leave an API key in its output, or write code that sends
the hidden questions to a hosted model while it is graded (Singularity-Bench NANO-K12). Claude Code and Codex are
given our key inside their container. The platform scans the output Harbor collected for key-shaped strings and
AI-provider addresses. The key itself is never stored or shown: only its kind and the file."""
import json
from pathlib import Path

import pytest

from app.services import output_scan
from app.services.harbor_runner import _parse_trial

OPENAI_KEY = "sk-" + "A1b2C3d4" * 4        # looks like a key; is not one
ANTHROPIC_KEY = "sk-ant-" + "x9Y8z7W6" * 4
GOOGLE_KEY = "AIza" + "B" * 35


# ---- 10: the scanner ------------------------------------------------------------------------

def test_a_key_shaped_string_is_found_by_kind():
    found = output_scan.scan_text(f"client = Client(api_key='{OPENAI_KEY}')")
    assert found["keys"] == ["openai-key"]


def test_an_anthropic_key_is_not_also_called_an_openai_key():
    assert output_scan.scan_text(ANTHROPIC_KEY)["keys"] == ["anthropic-key"]


def test_a_google_key_is_found():
    assert output_scan.scan_text(f"KEY={GOOGLE_KEY}")["keys"] == ["google-key"]


@pytest.mark.parametrize("text,host", [
    ("requests.post('https://api.openai.com/v1/chat/completions')", "api.openai.com"),
    ("base = 'https://api.anthropic.com'", "api.anthropic.com"),
    ("u = 'https://generativelanguage.googleapis.com/v1beta/models'", "generativelanguage.googleapis.com"),
    ("https://openrouter.ai/api/v1", "openrouter.ai"),
    ("https://api.groq.com/openai/v1", "api.groq.com"),
    ("https://api.fireworks.ai/inference/v1", "api.fireworks.ai"),
])
def test_an_ai_provider_address_is_found(text, host):
    assert output_scan.scan_text(text)["hosts"] == [host]


def test_a_saved_credential_variable_is_found():
    found = output_scan.scan_text("key = os.environ['OPENAI_API_KEY']\nother = getenv('ANTHROPIC_API_KEY')")
    assert found["key_vars"] == ["ANTHROPIC_API_KEY", "OPENAI_API_KEY"]


@pytest.mark.parametrize("text", [
    "print('hello world')",
    "import sklearn  # sk-learn is not a key",
    "see https://docs.openai.com/overview or https://example.com/api",
    "sk-short",
    "token = 'abc'",
])
def test_ordinary_text_is_clean(text):
    assert output_scan.scan_text(text) == {"keys": [], "hosts": [], "key_vars": []}


def make_artifacts(trial: Path, files: dict[str, bytes | str]):
    for rel, content in files.items():
        p = trial / "artifacts" / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(content if isinstance(content, bytes) else content.encode())
    return trial


def test_scanning_a_trial_reports_files_and_never_the_secret(tmp_path):
    make_artifacts(tmp_path, {
        "app/solution.py": f"import requests\nrequests.post('https://api.openai.com/v1/x', headers={{'k': '{OPENAI_KEY}'}})\n",
        "app/notes.txt": "nothing here",
    })
    result = output_scan.scan_trial(tmp_path)
    assert result["keys"] == {"openai-key": ["app/solution.py"]}
    assert result["hosts"] == {"api.openai.com": ["app/solution.py"]}
    blob = json.dumps(result)
    assert OPENAI_KEY not in blob and OPENAI_KEY[:12] not in blob


def test_binary_oversized_and_missing_are_skipped(tmp_path):
    make_artifacts(tmp_path, {
        "blob.bin": b"\x00\x01" + OPENAI_KEY.encode(),
        "big.txt": b"x" * (3 * 1024 * 1024) + OPENAI_KEY.encode(),
    })
    assert output_scan.scan_trial(tmp_path) is None
    assert output_scan.scan_trial(tmp_path / "no-such-trial") is None
    assert output_scan.scan_trial(None) is None


def test_the_number_of_files_read_is_capped(tmp_path):
    files = {f"f{i:04d}.txt": "clean" for i in range(output_scan.MAX_FILES + 50)}
    files["zz-last.txt"] = OPENAI_KEY  # sorts after the cap
    make_artifacts(tmp_path, files)
    assert output_scan.scan_trial(tmp_path) is None


def test_flags_name_the_kind_and_file_and_say_what_to_do(tmp_path):
    make_artifacts(tmp_path, {"a/run.py": f"k='{OPENAI_KEY}'\nu='https://api.openai.com'"})
    flags = output_scan.flags(output_scan.scan_trial(tmp_path))
    text = " ".join(flags)
    assert "openai-key" in text and "a/run.py" in text and "api.openai.com" in text
    assert OPENAI_KEY not in text
    assert "advisory" in text.lower() or "person" in text.lower()
    assert output_scan.flags(None) == []


def test_a_trial_report_gets_the_flag_and_not_the_secret(tmp_path):
    trial = tmp_path / "trial-1"
    (trial / "verifier").mkdir(parents=True)
    (trial / "result.json").write_text(json.dumps({"verifier_result": {"rewards": {"reward": 1.0}}}))
    make_artifacts(trial, {"app/answer.py": f"import requests\nK = '{OPENAI_KEY}'\nrequests.post('https://api.openai.com/v1/chat')"})
    outcome = _parse_trial(trial / "result.json")
    flags = " ".join(outcome.report["flags"])
    assert "openai-key" in flags and "api.openai.com" in flags
    assert outcome.report["output_scan"]["keys"] == {"openai-key": ["app/answer.py"]}
    assert OPENAI_KEY not in json.dumps(outcome.report) and OPENAI_KEY not in outcome.logs


def test_a_trial_with_clean_output_has_no_scan_entry(tmp_path):
    trial = tmp_path / "trial-1"
    (trial / "verifier").mkdir(parents=True)
    (trial / "result.json").write_text(json.dumps({"verifier_result": {"rewards": {"reward": 0.0}}}))
    make_artifacts(trial, {"app/answer.py": "print('hello')"})
    outcome = _parse_trial(trial / "result.json")
    assert "output_scan" not in outcome.report
    assert not any("api" in f.lower() and "key" in f.lower() for f in outcome.report["flags"])
