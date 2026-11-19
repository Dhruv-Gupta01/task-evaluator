"""services/network_policy.py: what task.toml declares about the network and what a run used."""

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
    assert "sets its own verifier-phase network policy (no-network); the platform does not override it" in text
    assert "open internet (public network) and the agent's web tools on, as in Terminal-Bench" in text
    assert "web tools switched off" in " | ".join(np_.warnings(info, web_tools_off=True)).replace("built-in ", "")


def test_a_task_that_declares_nothing_gets_only_the_open_internet_note(tmp_path):
    (tmp_path / "task.toml").write_text('[task]\nname = "x"\n')
    notes = np_.warnings(np_.for_task(tmp_path, "public"), web_tools_off=False)
    assert len(notes) == 1 and notes[0].startswith("trials ran with open internet")
    assert np_.warnings(None, False) == []
