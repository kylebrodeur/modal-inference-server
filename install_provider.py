"""Install the modal-inference-server service as a Pi / OMP model provider.

Writes/updates the custom `modal-inference` (or $MODAL_INFERENCE_PROVIDER) provider block in the right config
file for each harness, then verifies it. Safe to re-run (idempotent upsert).

  uv run python install_provider.py            # install into both pi and omp
  uv run python install_provider.py --omp-only # only ~/.omp/agent/models.yml
  uv run python install_provider.py --pi-only  # only the resolved pi agent dir
  uv run python install_provider.py --check    # verify only, write nothing

What it needs:
  MODAL_BASE_URL     deployed URL, e.g. https://<workspace>--modal-inference-server-vllmserver-web.modal.run
  MODAL_PROXY_TOKEN  wk-... bearer from the Modal proxy-auth secret

What it does per harness:
  omp: upserts providers.<provider> in ~/.omp/agent/models.yml (YAML),
       models[] generated from local models.json enabled profiles.
  pi:  upserts the provider in <agent-dir>/models.json (JSON). The agent dir
       resolves from --pi-agent-dir, else $PI_CODING_AGENT_DIR, else ~/.pi/agent.
       apiKey is written as the env reference `$MODAL_PROXY_TOKEN` (never the
       literal token); for a project-scoped agent dir (PI_CODING_AGENT_DIR set
       inside a project) the token is also written to the project env file
       so pi can resolve it at request time.

After install: `omp models find modal-inference` / pi session /model picker.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parent

# Name of the environment variable Pi reads for the modal-inference provider
# token. models.json stores the literal reference `$MODAL_PROXY_TOKEN`; Pi's
# config resolver expands it from process.env at request time.
TOKEN_ENV_VAR = "MODAL_PROXY_TOKEN"

# The provider name Pi/OMP uses to address this service. Configurable so
# operators can namespace it (e.g. `modal-inference/my-model` vs
# `my-org/my-model`); matches the extension's env override.
PROVIDER_NAME = os.getenv("MODAL_INFERENCE_PROVIDER", "modal-inference")


def _service_url() -> str:
    url = (REPO / ".service-url").read_text().strip().rstrip("/") if (REPO / ".service-url").exists() else ""
    return url


def _pi_agent_dir(explicit: str) -> Path:
    """Resolve the Pi agent dir: explicit flag > $PI_CODING_AGENT_DIR > ~/.pi/agent.

    Some operator setups scope Pi to a project-local agent dir via
    PI_CODING_AGENT_DIR, so `modal-inference install` must honor that rather
    than assume the global ~/.pi/agent location.
    """
    if explicit:
        return Path(explicit).expanduser()
    env = os.getenv("PI_CODING_AGENT_DIR")
    if env:
        return Path(env).expanduser()
    return Path.home() / ".pi" / "agent"


def _served_aliases(catalog: dict[str, Any]) -> set[str] | None:
    """Aliases the CURRENTLY DEPLOYED target serves, or None if unknown.

    The catalog lists every enabled model, but a deployment serves exactly one
    target (an alias or a serve group). Advertising the whole catalog makes the
    harness offer models the running container cannot answer: selecting one
    hangs until the client times out rather than failing fast. The deployed
    target is recorded by `modal-inference deploy` in the modal-inference config.
    """
    config_path = Path.home() / ".config" / "modal-inference" / "config.json"
    if not config_path.exists():
        return None
    try:
        deployed = str(json.loads(config_path.read_text()).get("deployed") or "")
    except (json.JSONDecodeError, OSError):
        return None
    if not deployed:
        return None
    groups = catalog.get("serveGroups") if isinstance(catalog.get("serveGroups"), dict) else {}
    group = groups.get(deployed)
    if isinstance(group, dict):
        return {str(a) for a in (group.get("aliases") or [])}
    return {deployed}


def _enabled_profiles(catalog_path: Path, *, served_only: bool = True) -> list[dict[str, Any]]:
    """Provider entries for every enabled alias the deployed target serves.

    Context window comes from the SERVE TARGET that will actually host the
    alias: a member of a serve group is served with the group's container-wide
    context, so advertising the member's own number would mislead the harness
    into sending prompts the running container truncates.
    """
    catalog = json.loads(catalog_path.read_text())
    groups = catalog.get("serveGroups") if isinstance(catalog.get("serveGroups"), dict) else {}
    group_context: dict[str, int] = {}
    for group in groups.values():
        if not isinstance(group, dict):
            continue
        tuning = group.get("tuning") if isinstance(group.get("tuning"), dict) else {}
        context = tuning.get("contextTokens")
        if not isinstance(context, int):
            continue
        for alias in group.get("aliases") or []:
            group_context[str(alias)] = context
    served = _served_aliases(catalog) if served_only else None
    profiles = []
    for alias, profile in catalog.get("models", {}).items():
        if not isinstance(profile, dict) or not profile.get("enabled"):
            continue
        if served is not None and alias not in served:
            continue
        tuning = profile.get("tuning", {})
        active = str(profile.get("activeTuning", "baseline"))
        active_cfg = tuning.get(active) if isinstance(tuning, dict) else None
        context = group_context.get(alias) or (
            int(active_cfg["contextTokens"])
            if isinstance(active_cfg, dict) and isinstance(active_cfg.get("contextTokens"), int)
            else int(profile.get("maxContextTokens", 32768))
        )
        profiles.append(
            {
                "id": alias,
                "name": f"{alias} (private Modal)",
                "contextWindow": context,
                "maxTokens": 32768,
                "reasoning": True,
                "input": ["text"],
            }
        )
    if not profiles:
        raise SystemExit(f"no enabled profiles in {catalog_path} for the deployed target")
    return profiles


def _sync_pi_enabled_models(profiles: list[dict[str, Any]], agent_dir: Path) -> None:
    """Pi's picker only shows models listed in settings.json enabledModels; keep ours in sync."""
    path = agent_dir / "settings.json"
    if not path.exists():
        return
    settings = json.loads(path.read_text())
    enabled = [m for m in settings.get("enabledModels", []) if not m.startswith("modal-inference/")]
    enabled += [f"modal-inference/{p['id']}" for p in profiles]
    settings["enabledModels"] = enabled
    path.write_text(json.dumps(settings, indent=2) + "\n")


# --- OMP: ~/.omp/agent/models.yml ---------------------------------------------


def _write_omp(base_url: str, token: str, profiles: list[dict[str, Any]]) -> Path:
    try:
        import yaml
    except ImportError as exc:  # pragma: no cover
        raise SystemExit("pyyaml required: uv add pyyaml") from exc

    path = Path.home() / ".omp" / "agent" / "models.yml"
    config: dict[str, Any] = {}
    if path.exists():
        loaded = yaml.safe_load(path.read_text()) or {}
        if not isinstance(loaded, dict):
            raise SystemExit(f"{path} is not a valid YAML mapping; refusing to overwrite")
        config = loaded
    providers = config.setdefault("providers", {})
    providers[PROVIDER_NAME] = {
        "baseUrl": f"{base_url}/v1",
        "api": "openai-completions",
        "apiKey": token,
        "authHeader": True,
        "models": profiles,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(config, sort_keys=False, default_flow_style=False))
    return path


def _check_omp(base_url: str, profiles: list[dict[str, Any]]) -> bool:
    path = Path.home() / ".omp" / "agent" / "models.yml"
    if not path.exists():
        return False
    try:
        import yaml
    except ImportError:
        return False
    config = yaml.safe_load(path.read_text()) or {}
    provider = config.get("providers", {}).get(PROVIDER_NAME)
    if not isinstance(provider, dict):
        return False
    if provider.get("baseUrl") != f"{base_url}/v1":
        return False
    configured = {m.get("id") for m in provider.get("models", [])}
    return all(m["id"] in configured for m in profiles)


# --- Pi: ~/.pi/agent/models.json ----------------------------------------------


def _write_pi(base_url: str, profiles: list[dict[str, Any]], agent_dir: Path) -> Path:
    path = agent_dir / "models.json"
    config: dict[str, Any] = {}
    if path.exists():
        try:
            config = json.loads(path.read_text())
        except json.JSONDecodeError:
            raise SystemExit(f"{path} is not a valid JSON; refusing to overwrite") from None
    config["providers"] = config.get("providers", {})
    config["providers"][PROVIDER_NAME] = {
        "baseUrl": f"{base_url}/v1",
        "api": "openai-completions",
        "apiKey": f"${TOKEN_ENV_VAR}",
        "models": profiles,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(config, indent=2))
    return path


def _check_pi(base_url: str, profiles: list[dict[str, Any]], agent_dir: Path) -> bool:
    path = agent_dir / "models.json"
    if not path.exists():
        return False
    try:
        config = json.loads(path.read_text())
    except json.JSONDecodeError:
        return False
    provider = config.get("providers", {}).get(PROVIDER_NAME)
    if not isinstance(provider, dict) or provider.get("baseUrl") != f"{base_url}/v1":
        return False
    if provider.get("apiKey") != f"${TOKEN_ENV_VAR}":
        return False
    configured = {m.get("id") for m in provider.get("models", [])}
    return all(m["id"] in configured for m in profiles)


def _project_env_path(agent_dir: Path) -> Path | None:
    """Resolve the project env file when agent_dir is project-scoped.

    Returns None for a global `~/.pi/agent` install, where the token is expected to
    come from the operator's shell environment instead of a project env file.
    """
    if agent_dir.name == "agent" and agent_dir.parent.name == ".pi" and agent_dir.parent.parent.name.startswith("."):
        return agent_dir.parent.parent / "agent.env"
    return None


def _write_project_env_token(agent_dir: Path, token: str) -> Path | None:
    """Write MODAL_PROXY_TOKEN=<token> into the project env file, preserving other lines."""
    env_path = _project_env_path(agent_dir)
    if env_path is None:
        return None
    line = f"{TOKEN_ENV_VAR}={token}"
    lines: list[str] = []
    if env_path.exists():
        lines = env_path.read_text().splitlines()
    replaced = False
    out: list[str] = []
    for existing in lines:
        if existing.startswith(f"{TOKEN_ENV_VAR}=") or existing == TOKEN_ENV_VAR:
            out.append(line)
            replaced = True
        else:
            out.append(existing)
    if not replaced:
        out.append(line)
    env_path.parent.mkdir(parents=True, exist_ok=True)
    env_path.write_text("\n".join(out) + "\n")
    with contextlib.suppress(OSError):
        env_path.chmod(0o600)
    return env_path


def install(
    base_url: str,
    token: str,
    catalog_path: Path,
    *,
    check: bool = False,
    only: str = "",
    pi_agent_dir: Path | None = None,
) -> int:
    profiles = _enabled_profiles(catalog_path)
    agent_dir = pi_agent_dir or _pi_agent_dir("")
    summary: dict[str, Any] = {"base_url": base_url, "models": [p["id"] for p in profiles]}
    if only != "pi":
        if check:
            summary["omp"] = "installed" if _check_omp(base_url, profiles) else "missing-or-stale"
        else:
            summary["omp"] = f"installed -> {_write_omp(base_url, token, profiles)}"
    if only != "omp":
        if check:
            summary["pi"] = "installed" if _check_pi(base_url, profiles, agent_dir) else "missing-or-stale"
        else:
            summary["pi"] = f"installed -> {_write_pi(base_url, profiles, agent_dir)}"
            env_path = _write_project_env_token(agent_dir, token)
            summary["pi_env"] = (
                f"installed -> {env_path}"
                if env_path
                else "skipped (global agent dir; export MODAL_PROXY_TOKEN in shell)"
            )
            _sync_pi_enabled_models(profiles, agent_dir)
    print(json.dumps(summary, indent=2))
    if check:
        checks = [v == "installed" for k, v in summary.items() if k in ("omp", "pi")]
        return 0 if checks and all(checks) else 1
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Install the Modal inference provider into Pi and OMP.")
    parser.add_argument("--base-url", default=os.getenv("MODAL_BASE_URL", ""))
    parser.add_argument("--token", default=os.getenv("MODAL_PROXY_TOKEN", ""))
    parser.add_argument("--omp-only", action="store_true")
    parser.add_argument("--pi-only", action="store_true")
    parser.add_argument("--check", action="store_true", help="verify existing install without writing")
    parser.add_argument(
        "--pi-agent-dir",
        default="",
        help="Pi agent dir to write models.json into (default: $PI_CODING_AGENT_DIR, else ~/.pi/agent)",
    )
    args = parser.parse_args()
    if args.omp_only and args.pi_only:
        parser.error("choose one of --omp-only / --pi-only")
    if not args.base_url or not args.token:
        raise SystemExit("MODAL_BASE_URL and MODAL_PROXY_TOKEN required (or use `modal-inference install`)")
    only = "omp" if args.omp_only else "pi" if args.pi_only else ""
    return install(
        args.base_url.rstrip("/"),
        args.token,
        REPO / "server" / "models.json",
        check=args.check,
        only=only,
        pi_agent_dir=_pi_agent_dir(args.pi_agent_dir),
    )


if __name__ == "__main__":
    raise SystemExit(main())
