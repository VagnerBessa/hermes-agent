"""hindsight_memory_setup gives every profile it touches the shared-daemon layout."""
import importlib.util
import json
import os
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "hindsight_memory_setup.py"


@pytest.fixture
def setup_mod(monkeypatch):
    spec = importlib.util.spec_from_file_location("hindsight_memory_setup", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    calls = []
    monkeypatch.setattr(mod, "hermes", lambda profile, *args, dry_run: calls.append((profile, *args)))
    mod.calls = calls
    return mod


@pytest.fixture
def homes(monkeypatch, tmp_path):
    root = tmp_path / ".hermes"
    (root / "profiles" / "bento").mkdir(parents=True)
    (root / ".env").write_text("# keys\nDEEPSEEK_API_KEY=sk-principal\nOTHER=1\n", encoding="utf-8")
    (root / "profiles" / "bento" / ".env").write_text("DEEPSEEK_API_KEY=sk-bento\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(root))
    monkeypatch.delenv("HINDSIGHT_LLM_API_KEY", raising=False)
    return {"default": root, "bento": root / "profiles" / "bento"}


def _cfg(home):
    return json.loads((home / "hindsight" / "config.json").read_text(encoding="utf-8"))


def test_profiles_share_one_server_but_keep_their_own_bank(setup_mod, homes):
    assert setup_mod.main(["--profiles", "default,bento", "--api-url", "http://127.0.0.1:9999"]) == 0
    a, b = _cfg(homes["default"]), _cfg(homes["bento"])
    assert a["mode"] == b["mode"] == "local_external"
    assert a["api_url"] == b["api_url"] == "http://127.0.0.1:9999"
    assert "world" in a["recall_types"]  # an LLM-less server has no observations to recall
    assert a["bank_id_template"] == b["bank_id_template"] and "{profile}" in a["bank_id_template"]
    assert "HINDSIGHT_LLM_API_KEY" not in setup_mod.read_env(homes["bento"] / ".env")
    set_provider = [c for c in setup_mod.calls if c[1:] == ("config", "set", "memory.provider", "hindsight")]
    assert sorted(c[0] for c in set_provider) == ["bento", "default"]


def test_embedded_profiles_share_daemon_llm_and_key(setup_mod, homes):
    assert setup_mod.main(["--profiles", "default,bento", "--embedded"]) == 0
    a, b = _cfg(homes["default"]), _cfg(homes["bento"])
    llm_keys = ("mode", "profile", "llm_provider", "llm_base_url", "llm_model")
    assert {k: a[k] for k in llm_keys} == {k: b[k] for k in llm_keys}
    key_a = setup_mod.read_env(homes["default"] / ".env")["HINDSIGHT_LLM_API_KEY"]
    key_b = setup_mod.read_env(homes["bento"] / ".env")["HINDSIGHT_LLM_API_KEY"]
    assert key_a == key_b == setup_mod.read_env(homes["default"] / ".env")["DEEPSEEK_API_KEY"]
    assert "OTHER=1" in (homes["default"] / ".env").read_text(encoding="utf-8")
    if os.name == "posix":
        assert (homes["bento"] / "hindsight" / "config.json").stat().st_mode & 0o077 == 0


def test_shared_bank_drops_the_template_and_keeps_existing_settings(setup_mod, homes):
    setup_mod.write_provider_config(homes["bento"], {"bank_id_template": "x-{profile}", "recall_budget": "high"})
    assert setup_mod.main(["--profiles", "bento", "--shared-bank"]) == 0
    cfg = _cfg(homes["bento"])
    assert "bank_id_template" not in cfg and cfg["recall_budget"] == "high"


def test_principal_only_by_default(setup_mod, homes):
    assert setup_mod.main([]) == 0
    assert (homes["default"] / "hindsight" / "config.json").is_file()
    assert not (homes["bento"] / "hindsight").exists()


def test_embedded_without_llm_key_refuses(setup_mod, homes):
    (homes["default"] / ".env").write_text("OTHER=1\n", encoding="utf-8")
    assert setup_mod.main(["--embedded"]) == 2
    assert not (homes["default"] / "hindsight").exists()
