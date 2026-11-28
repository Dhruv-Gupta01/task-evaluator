"""services/network_policy.py: what task.toml declares about the network and what a run used. Tasks run on the
public network only (the user's rule, 2026-11-28): a phase override is reported as overridden, never kept."""

from app.services import network_policy as np_


def decl(**sections):
    return np_.declared(sections)


def test_nothing_declared_is_public():
    d = decl(environment={"cpus": 1})
    assert (d["mode"], d["source"], d["allowed_hosts"], d["deprecated_key"]) == ("public", "default", [], False)


def test_legacy_allow_internet_maps_like_harbor():
    assert decl(environment={"allow_internet": False})["mode"] == "no-network"
    assert decl(environment={"allow_internet": True})["mode"] == "public"
    d = decl(environment={"allow_internet": False})
    assert d["source"] == "allow_internet" and d["deprecated_key"]


def test_network_mode_wins_over_the_legacy_key():
    d = decl(environment={"allow_internet": False, "network_mode": "public"})
    assert (d["mode"], d["source"]) == ("public", "network_mode")


def test_allowlist_and_phase_overrides():
    d = decl(environment={"network_mode": "allowlist", "allowed_hosts": ["pypi.org", "files.pythonhosted.org"]},
             verifier={"network_mode": "no-network"}, agent={"timeout_sec": 5})
    assert d["mode"] == "allowlist" and d["allowed_hosts"] == ["pypi.org", "files.pythonhosted.org"]
    assert d["phase_overrides"] == {"verifier": {"mode": "no-network", "allowed_hosts": []}}


def test_effective_follows_the_platform_setting_and_says_when_it_overrides():
    offline = decl(environment={"network_mode": "no-network"})
    forced = np_.effective(offline, "public")
    assert forced["mode"] == "public" and forced["forced_by_platform"] and forced["overrides_task"]
    assert np_.effective(decl(environment={}), "public")["overrides_task"] is False  # same as the default: nothing lost
    own = np_.effective(offline, "")
    assert own["mode"] == "no-network" and not own["forced_by_platform"] and not own["overrides_task"]
    phase = np_.effective(decl(environment={}, verifier={"network_mode": "no-network"}), "public")
    assert phase["overrides_task"] and phase["phase_overrides_overridden"] == {"verifier": {"mode": "no-network", "allowed_hosts": []}}
    assert phase["phase_overrides_kept"] == {}
    assert np_.effective(decl(environment={}, verifier={"network_mode": "public"}), "public")["overrides_task"] is False
    hosts = np_.effective(decl(environment={"network_mode": "allowlist", "allowed_hosts": ["a.org"]}), "public")
    assert hosts["overrides_task"] and hosts["allowed_hosts"] == []


def test_for_task_reads_the_file(tmp_path):
    (tmp_path / "task.toml").write_text('[environment]\nallow_internet = false\n')
    info = np_.for_task(tmp_path, "public")
    assert info["declared"]["mode"] == "no-network" and info["effective"]["overrides_task"]
    (tmp_path / "task.toml").write_text("[[[")
    assert np_.for_task(tmp_path, "public") is None and np_.for_task(tmp_path / "missing", "public") is None


def test_warnings_say_declared_versus_ran_and_the_open_internet_conditions(tmp_path):
    (tmp_path / "task.toml").write_text('[environment]\nallow_internet = false\n[verifier]\nnetwork_mode = "no-network"\n')
    info = np_.for_task(tmp_path, "public")
    text = " | ".join(np_.warnings(info, web_tools_off=False))
    assert "declares network no-network (via `allow_internet`) but the platform ran it with network_mode=public" in text
    assert "sets its own verifier-phase network policy (no-network); the platform overrides it to public" in text
    assert "open internet (public network) and the agent's web tools on, as in Terminal-Bench" in text
    assert "web tools switched off" in " | ".join(np_.warnings(info, web_tools_off=True)).replace("built-in ", "")


def test_a_task_that_declares_nothing_gets_only_the_open_internet_note(tmp_path):
    (tmp_path / "task.toml").write_text('[task]\nname = "x"\n')
    notes = np_.warnings(np_.for_task(tmp_path, "public"), web_tools_off=False)
    assert len(notes) == 1 and notes[0].startswith("trials ran with open internet")
    assert np_.warnings(None, False) == []


# ---- the run itself: every phase on the public network --------------------------------------

def _task(tmp_path, toml: str):
    (tmp_path / "environment").mkdir(parents=True)
    (tmp_path / "environment" / "Dockerfile").write_text("FROM scratch\n")
    (tmp_path / "task.toml").write_text(toml)
    return tmp_path


def test_a_phase_override_is_removed_so_every_phase_runs_public(tmp_path, monkeypatch):
    import tomllib

    from app.services import harbor_runner

    monkeypatch.setattr(harbor_runner.settings, "harbor_network_mode", "public")
    monkeypatch.setattr(harbor_runner.settings, "agent_timeout_sec", 0)
    task = _task(tmp_path / "t", '[environment]\nnetwork_mode = "public"\n\n[agent]\ntimeout_sec = 60.0\n'
                 'network_mode = "allowlist"\nallowed_hosts = ["pypi.org"]\n\n[verifier]\nnetwork_mode = "no-network"\n')
    root, notes = harbor_runner._prepare_task(task, tmp_path / "copy")
    data = tomllib.loads((root / "task.toml").read_text())
    assert data["environment"]["network_mode"] == "public"
    assert "network_mode" not in data["agent"] and "allowed_hosts" not in data["agent"]
    assert data["agent"]["timeout_sec"] == 60.0  # other keys of the section are kept
    assert "network_mode" not in data.get("verifier", {})
    assert "agent" in notes and "verifier" in notes and "public" in notes
    assert 'network_mode = "no-network"' in (task / "task.toml").read_text()  # the upload is never edited


def test_a_task_already_public_everywhere_is_not_copied(tmp_path, monkeypatch):
    from app.services import harbor_runner

    monkeypatch.setattr(harbor_runner.settings, "harbor_network_mode", "public")
    monkeypatch.setattr(harbor_runner.settings, "agent_timeout_sec", 0)
    task = _task(tmp_path / "t", '[environment]\nnetwork_mode = "public"\n\n[verifier]\nnetwork_mode = "public"\n')
    root, notes = harbor_runner._prepare_task(task, tmp_path / "copy")
    assert root == task and notes is None


def test_the_agent_timeout_and_the_network_override_are_both_applied(tmp_path, monkeypatch):
    import tomllib

    from app.services import harbor_runner

    monkeypatch.setattr(harbor_runner.settings, "harbor_network_mode", "public")
    monkeypatch.setattr(harbor_runner.settings, "agent_timeout_sec", 28800)
    task = _task(tmp_path / "t", '[agent]\ntimeout_sec = 60.0\nnetwork_mode = "no-network"\n')
    root, _ = harbor_runner._prepare_task(task, tmp_path / "copy")
    agent = tomllib.loads((root / "task.toml").read_text())["agent"]
    assert agent["timeout_sec"] == 28800.0 and "network_mode" not in agent
