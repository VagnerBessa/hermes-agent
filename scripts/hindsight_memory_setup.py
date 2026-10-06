#!/usr/bin/env python3
"""Make Hindsight the memory provider of one or more Hermes profiles, non-interactively.

Every profile it touches talks to ONE Hindsight server, the layout that keeps several profiles
from stalling each other (measured in website/docs/guides/hindsight-memory-profiles.md): a memory
process per profile is what exhausted RAM with Mem0.

* default, ``local_external``: every profile points at a Hindsight server you already run
  (``--api-url``, default ``http://127.0.0.1:8890``). The server holds the LLM and embedding
  settings, so profiles cannot disagree about them.
* ``--embedded``: the plugin starts the server itself (``local_embedded``), shared by every profile
  through the hindsight-embed profile ``hermes``. All profiles then get the same LLM settings and
  key: the plugin RESTARTS the shared daemon whenever a profile starts with LLM settings that
  differ from the daemon's (hindsight-integrations/hermes ``_profile_env_drifted``), ~20 s with no
  memory for every profile each time.
* a bank per Hermes profile (``bank_id_template: hermes-{profile}``), or one bank for all of them
  with ``--shared-bank``.

It only drives the public CLI (``hermes -p <profile> plugins install`` / ``config set``) and writes
the plugin's own ``<profile home>/hindsight/config.json`` and the profile ``.env``, like
``hermes memory setup`` does.

Examples::

    python scripts/hindsight_memory_setup.py                       # principal profile only
    python scripts/hindsight_memory_setup.py --profiles default,bento
    python scripts/hindsight_memory_setup.py --all-profiles --dry-run
    python scripts/hindsight_memory_setup.py --embedded              # no server running yet
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

PRINCIPAL = "default"
SHARED_DAEMON_PROFILE = "hermes"
KEY_ENV = "HINDSIGHT_LLM_API_KEY"
DEFAULT_API_URL = "http://127.0.0.1:8890"

# DeepSeek speaks the OpenAI wire format; the plugin calls that provider ``openai_compatible``.
DEFAULT_LLM = {
    "llm_provider": "openai_compatible",
    "llm_base_url": "https://api.deepseek.com/v1",
    "llm_model": "deepseek-v4-flash",
}


def build_provider_config(existing: dict, *, shared_bank: bool, api_url: str | None, llm: dict | None) -> dict:
    """The plugin config for one profile: *existing* keys kept, the one-server layout enforced.
    *api_url* selects ``local_external``; otherwise *llm* configures the shared embedded daemon."""
    config = dict(existing)
    if api_url:
        config.update({"mode": "local_external", "api_url": api_url})
        # A server run with HINDSIGHT_API_LLM_PROVIDER=none stores chunks as ``world`` facts and builds
        # no observations, so the plugin's default (observations only) would always recall nothing.
        config.setdefault("recall_types", "observation,world,experience")
    else:
        config.update({**(llm or {}), "mode": "local_embedded", "profile": SHARED_DAEMON_PROFILE})
    config.update(
        {
            "bank_id": "hermes",
            "memory_mode": config.get("memory_mode", "hybrid"),
            "auto_recall": config.get("auto_recall", True),
            "auto_retain": config.get("auto_retain", True),
            "retain_async": config.get("retain_async", True),
            # The previous turn is already in the conversation; waiting for its retain before the
            # next recall cost each turn up to the plugin's 3 s prefetch cap in our measurements.
            "prefetch_waits_for_retain": config.get("prefetch_waits_for_retain", False),
        }
    )
    if shared_bank:
        config.pop("bank_id_template", None)
    else:
        config["bank_id_template"] = "hermes-{profile}"
    return config


def read_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if path.is_file():
        for line in path.read_text(encoding="utf-8-sig").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, _, value = line.removeprefix("export ").partition("=")
                values[key.strip()] = value.strip().strip("\"'")
    return values


def write_env_value(path: Path, key: str, value: str) -> None:
    """Set *key* in a ``.env`` file, keeping every other line; owner-only, it holds secrets."""
    lines = path.read_text(encoding="utf-8-sig").splitlines() if path.is_file() else []
    out, found = [], False
    for line in lines:
        if line.strip().removeprefix("export ").split("=", 1)[0].strip() == key:
            out.append(f"{key}={value}")
            found = True
        else:
            out.append(line)
    if not found:
        out.append(f"{key}={value}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(out) + "\n", encoding="utf-8")
    os.chmod(path, 0o600)


def write_provider_config(home: Path, config: dict) -> Path:
    path = home / "hindsight" / "config.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(config, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.chmod(path, 0o600)
    return path


def profile_home(name: str) -> Path:
    from hermes_cli.profiles import get_profile_dir

    return get_profile_dir(name)


def all_profile_names() -> list[str]:
    from hermes_cli.profiles import list_profile_names

    return [PRINCIPAL] + [n for n in list_profile_names() if n != PRINCIPAL]


def resolve_llm_key(principal_home: Path, key_source: str) -> str:
    """The LLM key every profile will share: the principal's HINDSIGHT_LLM_API_KEY, else
    *key_source* (DEEPSEEK_API_KEY by default) from the principal's .env or this environment."""
    env = read_env(principal_home / ".env")
    return env.get(KEY_ENV) or env.get(key_source) or os.environ.get(KEY_ENV) or os.environ.get(key_source, "")


def hermes(profile: str, *args: str, dry_run: bool) -> None:
    exe = shutil.which("hermes") or "hermes"
    cmd = [exe, "-p", profile, *args]
    print("  $", " ".join(cmd))
    if not dry_run:
        subprocess.run(cmd, check=True, stdin=subprocess.DEVNULL)


def setup_profile(name: str, *, key: str | None, args: argparse.Namespace) -> None:
    home = profile_home(name)
    print(f"\n[{name}] {home}")
    if not (home / "plugins" / "hindsight").is_dir():
        hermes(name, "plugins", "install", "hindsight", "--enable", dry_run=args.dry_run)
    existing_path = home / "hindsight" / "config.json"
    existing = json.loads(existing_path.read_text(encoding="utf-8")) if existing_path.is_file() else {}
    llm = {**DEFAULT_LLM, **{k: v for k, v in (("llm_base_url", args.llm_base_url), ("llm_model", args.llm_model)) if v}}
    api_url = None if args.embedded else args.api_url
    config = build_provider_config(existing, shared_bank=args.shared_bank, api_url=api_url, llm=llm)
    bank = "hermes" if args.shared_bank else f"hermes-{name}"
    print(f"  hindsight/config.json: mode={config['mode']} server={api_url or 'embedded:' + SHARED_DAEMON_PROFILE} bank={bank}")
    if not args.dry_run:
        write_provider_config(home, config)
        if key is not None:
            write_env_value(home / ".env", KEY_ENV, key)
    hermes(name, "config", "set", "memory.provider", "hindsight", dry_run=args.dry_run)
    if args.builtin_off:
        hermes(name, "config", "set", "memory.memory_enabled", "false", dry_run=args.dry_run)
        hermes(name, "config", "set", "memory.user_profile_enabled", "false", dry_run=args.dry_run)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    target = parser.add_mutually_exclusive_group()
    target.add_argument("--profiles", default=PRINCIPAL, help="comma-separated profiles (default: the principal)")
    target.add_argument("--all-profiles", action="store_true", help="every profile, principal included")
    parser.add_argument("--shared-bank", action="store_true", help="one bank for all profiles instead of one each")
    parser.add_argument("--api-url", default=DEFAULT_API_URL, help=f"running Hindsight server (default {DEFAULT_API_URL})")
    parser.add_argument("--embedded", action="store_true", help="let the plugin run one shared server instead")
    parser.add_argument("--llm-base-url", help=f"--embedded only; default {DEFAULT_LLM['llm_base_url']}")
    parser.add_argument("--llm-model", help=f"--embedded only; default {DEFAULT_LLM['llm_model']}")
    parser.add_argument("--llm-key-from", default="DEEPSEEK_API_KEY", help="--embedded only; env var holding the LLM key")
    parser.add_argument("--builtin-off", action="store_true", help="turn off MEMORY.md/USER.md in these profiles")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    names = all_profile_names() if args.all_profiles else [p.strip() for p in args.profiles.split(",") if p.strip()]
    key = resolve_llm_key(profile_home(PRINCIPAL), args.llm_key_from) if args.embedded else None
    if args.embedded and not key and not args.dry_run:
        print(f"✗ No LLM key: set {KEY_ENV} or {args.llm_key_from} in the principal profile's .env.", file=sys.stderr)
        return 2
    for name in names:
        setup_profile(name, key=key, args=args)
    print("\n✓ Done. Start a new session in each profile; `hermes -p <profile> memory status` confirms.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
