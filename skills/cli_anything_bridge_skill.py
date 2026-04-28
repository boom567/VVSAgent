import json
import shlex
import shutil
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CLI_ANYTHING_ROOT = ROOT / "CLI-Anything"
CLI_HUB_ROOT = CLI_ANYTHING_ROOT / "cli-hub"


def _run_process(argv, cwd=None, timeout_seconds=120):
    try:
        completed = subprocess.run(
            argv,
            cwd=str(cwd) if cwd else None,
            capture_output=True,
            text=True,
            timeout=max(int(timeout_seconds), 1),
            check=False,
        )
        return {
            "ok": completed.returncode == 0,
            "exit_code": completed.returncode,
            "stdout": completed.stdout,
            "stderr": completed.stderr,
            "argv": argv,
            "cwd": str(cwd) if cwd else "",
        }
    except subprocess.TimeoutExpired as exc:
        return {
            "ok": False,
            "exit_code": -1,
            "stdout": exc.stdout or "",
            "stderr": (exc.stderr or "") + "\nProcess timed out.",
            "argv": argv,
            "cwd": str(cwd) if cwd else "",
        }
    except Exception as exc:
        return {
            "ok": False,
            "exit_code": -1,
            "stdout": "",
            "stderr": f"Failed to execute command: {exc}",
            "argv": argv,
            "cwd": str(cwd) if cwd else "",
        }


def _resolve_cli_hub_runner():
    command_path = shutil.which("cli-hub")
    if command_path:
        return [command_path], None

    if CLI_HUB_ROOT.exists() and (CLI_HUB_ROOT / "cli_hub").exists():
        return [sys.executable, "-m", "cli_hub.cli"], CLI_HUB_ROOT

    return None, None


def _resolve_cli_anything_runner(tool_name):
    normalized = str(tool_name or "").strip().lower()
    if not normalized:
        return None, None, "tool_name cannot be empty"

    if normalized.startswith("cli-anything-"):
        command_name = normalized
        slug = normalized[len("cli-anything-") :]
    else:
        slug = normalized
        command_name = f"cli-anything-{slug}"

    command_path = shutil.which(command_name)
    if command_path:
        return [command_path], None, ""

    # Dev fallback: run from local harness source if available.
    harness_root = CLI_ANYTHING_ROOT / slug / "agent-harness"
    package_module = slug.replace("-", "_")
    module_name = f"cli_anything.{package_module}.{package_module}_cli"

    if harness_root.exists() and (harness_root / "cli_anything").exists():
        return [sys.executable, "-m", module_name], harness_root, ""

    return None, None, (
        f"Unable to locate '{command_name}'. Install via cli-hub install {slug} "
        "or ensure local harness exists under CLI-Anything/<slug>/agent-harness."
    )


def _safe_json_output(payload):
    return json.dumps(payload, ensure_ascii=False, indent=2)


def _parse_args(args_text):
    text = str(args_text or "").strip()
    if not text:
        return []
    return shlex.split(text)


def _normalize_alias(name):
    alias = str(name or "").strip().lower().replace("-", "_")
    alias = "".join(ch for ch in alias if ch.isalnum() or ch == "_")
    if not alias:
        return ""
    if alias[0].isdigit():
        alias = f"tool_{alias}"
    return alias


def _collect_registry_entries():
    registry_file = CLI_ANYTHING_ROOT / "registry.json"
    if not registry_file.exists():
        return {}

    try:
        payload = json.loads(registry_file.read_text(encoding="utf-8"))
    except Exception:
        return {}

    entries = {}
    for item in payload.get("clis", []):
        name = str(item.get("name") or "").strip().lower()
        if name:
            entries[name] = item
    return entries


def _collect_local_cli_anything_tools():
    tools = {}
    registry_entries = _collect_registry_entries()
    if CLI_ANYTHING_ROOT.exists():
        for child in sorted(CLI_ANYTHING_ROOT.iterdir()):
            if not child.is_dir():
                continue
            harness_root = child / "agent-harness"
            if not harness_root.exists():
                continue

            raw_name = child.name.lower()
            # Keep aliases consistent with cli-anything command naming.
            if raw_name == "qgis":
                slug = "qgis"
            else:
                slug = raw_name

            details = registry_entries.get(slug, {})
            tools[slug] = {
                "slug": slug,
                "display_name": details.get("display_name") or slug,
                "description": details.get("description") or f"CLI-Anything tool: {slug}",
                "category": details.get("category") or "",
            }

    return tools


def _skill_file_candidates(slug):
    candidates = [
        CLI_ANYTHING_ROOT / "skills" / f"cli-anything-{slug}" / "SKILL.md",
        CLI_ANYTHING_ROOT / slug / "agent-harness" / "cli_anything" / slug.replace("-", "_") / "skills" / "SKILL.md",
    ]
    return candidates


def _extract_skill_templates(slug, max_items=6):
    command_prefix = f"cli-anything-{slug}"
    templates = []
    seen = set()

    for path in _skill_file_candidates(slug):
        if not path.exists():
            continue

        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except Exception:
            continue

        for raw in lines:
            line = raw.strip()
            if command_prefix not in line:
                continue

            # Drop markdown code fence headers and comments.
            if line.startswith("```") or line.startswith("#"):
                continue

            # Remove leading bullets or PowerShell markers.
            line = line.lstrip("-*0123456789. ")
            line = line.replace("`", "").strip()
            if not line:
                continue

            # Keep only realistic command lines.
            idx = line.find(command_prefix)
            if idx < 0:
                continue
            line = line[idx:].strip()

            # Must begin with command prefix and look like an actual command.
            if not line.startswith(command_prefix):
                continue

            # Drop trailing punctuation from prose mentions.
            line = line.strip("'\" ")
            while line.endswith(":") or line.endswith(".") or line.endswith(","):
                line = line[:-1].rstrip()

            # Skip known prose fragments.
            lowered = line.lower()
            if " package" in lowered or "installed" in lowered or "this cli" in lowered:
                continue

            # Keep either base command or command + arguments.
            if line != command_prefix and not line.startswith(command_prefix + " "):
                continue

            if line in seen:
                continue
            seen.add(line)
            templates.append(line)

            if len(templates) >= max_items:
                return templates

    return templates


def register(agent):
    template_cache = {}

    def cli_anything_list(category: str = "", source: str = "", timeout_seconds: int = 60):
        runner, cwd = _resolve_cli_hub_runner()
        if not runner:
            return _safe_json_output(
                {
                    "ok": False,
                    "error": "cli-hub is not available. Install it or keep CLI-Anything/cli-hub in workspace.",
                }
            )

        argv = list(runner) + ["list", "--json"]
        if category.strip():
            argv += ["-c", category.strip()]
        if source.strip():
            argv += ["--source", source.strip()]

        result = _run_process(argv, cwd=cwd, timeout_seconds=timeout_seconds)
        if result["ok"]:
            try:
                result["data"] = json.loads(result["stdout"] or "[]")
            except json.JSONDecodeError:
                result["ok"] = False
                result["error"] = "cli-hub returned non-JSON output while --json was requested."
        return _safe_json_output(result)

    def cli_anything_info(name: str, timeout_seconds: int = 60):
        target = str(name or "").strip()
        if not target:
            return _safe_json_output({"ok": False, "error": "name cannot be empty"})

        runner, cwd = _resolve_cli_hub_runner()
        if not runner:
            return _safe_json_output(
                {
                    "ok": False,
                    "error": "cli-hub is not available. Install it or keep CLI-Anything/cli-hub in workspace.",
                }
            )

        result = _run_process(list(runner) + ["info", target], cwd=cwd, timeout_seconds=timeout_seconds)
        return _safe_json_output(result)

    def cli_anything_install(name: str, timeout_seconds: int = 600):
        target = str(name or "").strip()
        if not target:
            return _safe_json_output({"ok": False, "error": "name cannot be empty"})

        runner, cwd = _resolve_cli_hub_runner()
        if not runner:
            return _safe_json_output(
                {
                    "ok": False,
                    "error": "cli-hub is not available. Install it or keep CLI-Anything/cli-hub in workspace.",
                }
            )

        result = _run_process(list(runner) + ["install", target], cwd=cwd, timeout_seconds=timeout_seconds)
        return _safe_json_output(result)

    def cli_anything_run(
        tool_name: str,
        command_args: str = "",
        json_mode: str = "true",
        timeout_seconds: int = 300,
    ):
        runner, cwd, error_text = _resolve_cli_anything_runner(tool_name)
        if not runner:
            return _safe_json_output({"ok": False, "error": error_text})

        extra_args = _parse_args(command_args)
        force_json = str(json_mode).strip().lower() in {"1", "true", "yes", "on"}
        if force_json and "--json" not in extra_args:
            extra_args = ["--json"] + extra_args

        result = _run_process(list(runner) + extra_args, cwd=cwd, timeout_seconds=timeout_seconds)
        return _safe_json_output(result)

    agent.add_skill(
        name="cli_anything_list",
        func=cli_anything_list,
        description=(
            "List CLI-Anything tools via cli-hub in JSON format. "
            "Optionally filter by category/source."
        ),
        parameters={
            "category": "string",
            "source": "string",
            "timeout_seconds": "integer",
        },
    )

    agent.add_skill(
        name="cli_anything_info",
        func=cli_anything_info,
        description="Show detailed metadata of a CLI-Anything tool from cli-hub.",
        parameters={
            "name": "string",
            "timeout_seconds": "integer",
        },
    )

    agent.add_skill(
        name="cli_anything_install",
        func=cli_anything_install,
        description="Install a CLI-Anything tool by name using cli-hub.",
        parameters={
            "name": "string",
            "timeout_seconds": "integer",
        },
    )

    agent.add_skill(
        name="cli_anything_run",
        func=cli_anything_run,
        description=(
            "Run a CLI-Anything tool command safely (no shell), "
            "with optional automatic --json mode."
        ),
        parameters={
            "tool_name": "string",
            "command_args": "string",
            "json_mode": "string",
            "timeout_seconds": "integer",
        },
    )

    local_tools = _collect_local_cli_anything_tools()
    for slug, info in local_tools.items():
        alias = _normalize_alias(slug)
        if not alias:
            continue

        skill_name = f"ca_{alias}"
        if skill_name in agent.registry.skills:
            continue

        def _make_proxy(tool_slug):
            def _proxy(command_args: str = "", json_mode: str = "true", timeout_seconds: int = 300):
                return cli_anything_run(
                    tool_name=tool_slug,
                    command_args=command_args,
                    json_mode=json_mode,
                    timeout_seconds=timeout_seconds,
                )

            return _proxy

        templates = _extract_skill_templates(slug)
        template_cache[slug] = templates

        summary = f"Auto proxy for cli-anything-{slug}."
        if info.get("description"):
            summary += f" {info['description']}"
        if info.get("category"):
            summary += f" Category: {info['category']}."
        if templates:
            summary += " Templates: " + " | ".join(templates[:3])

        agent.add_skill(
            name=skill_name,
            func=_make_proxy(slug),
            description=summary,
            parameters={
                "command_args": "string",
                "json_mode": "string",
                "timeout_seconds": "integer",
            },
        )

    def cli_anything_local_catalog():
        aliases = [name for name in sorted(agent.registry.skills.keys()) if name.startswith("ca_")]
        return _safe_json_output(
            {
                "ok": True,
                "count": len(aliases),
                "aliases": aliases,
            }
        )

    def cli_anything_templates(tool_name: str = ""):
        wanted = str(tool_name or "").strip().lower()
        if wanted.startswith("cli-anything-"):
            wanted = wanted[len("cli-anything-") :]
        if wanted.startswith("ca_"):
            wanted = wanted[len("ca_") :].replace("_", "-")

        if wanted:
            if wanted not in local_tools:
                return _safe_json_output(
                    {
                        "ok": False,
                        "error": f"Unknown local tool: {wanted}",
                    }
                )
            return _safe_json_output(
                {
                    "ok": True,
                    "tool": wanted,
                    "templates": template_cache.get(wanted, []),
                }
            )

        return _safe_json_output(
            {
                "ok": True,
                "count": len(template_cache),
                "templates": template_cache,
            }
        )

    agent.add_skill(
        name="cli_anything_local_catalog",
        func=cli_anything_local_catalog,
        description="List auto-registered local CLI-Anything proxy tool names (ca_*).",
        parameters={},
    )

    agent.add_skill(
        name="cli_anything_templates",
        func=cli_anything_templates,
        description=(
            "Get recommended command templates extracted from CLI-Anything SKILL.md. "
            "Supports a single tool or all local tools."
        ),
        parameters={
            "tool_name": "string",
        },
    )
