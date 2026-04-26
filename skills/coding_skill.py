from __future__ import annotations

import ast
import json
import py_compile
import subprocess
from pathlib import Path


def _extract_json(text: str):
    payload = (text or "").strip()
    if not payload:
        return None

    try:
        return json.loads(payload)
    except json.JSONDecodeError:
        pass

    start = payload.find("{")
    if start == -1:
        return None

    decoder = json.JSONDecoder()
    try:
        obj, _ = decoder.raw_decode(payload[start:])
    except json.JSONDecodeError:
        return None
    return obj


def _strip_markdown_fence(text: str):
    raw = (text or "").strip()
    if raw.startswith("```") and raw.endswith("```"):
        lines = raw.splitlines()
        if len(lines) >= 2:
            return "\n".join(lines[1:-1]).strip()
    return raw


def _call_model_json(agent, prompt: str):
    response = agent.chat_completion(
        model=agent._get_active_model_name(),
        messages=[
            {
                "role": "system",
                "content": "Return exactly one JSON object. Do not output markdown.",
            },
            {
                "role": "user",
                "content": prompt,
            },
        ],
        format="json",
    )
    content = response.get("message", {}).get("content", "")
    data = _extract_json(content)
    return data if isinstance(data, dict) else {}


def _generate_initial_code(agent, description: str, language: str, target_path: Path):
    prompt = (
        "You are a senior coding agent. Generate production-ready code from requirements.\n"
        "Return JSON with keys: summary, code\n"
        f"Language: {language}\n"
        f"Target file: {target_path}\n"
        f"Requirements:\n{description}\n"
        "Constraints:\n"
        "- Keep implementation focused on the requirements only.\n"
        "- Include concise comments only for non-obvious logic.\n"
    )
    data = _call_model_json(agent, prompt)
    code = data.get("code", "") if isinstance(data, dict) else ""
    if not code:
        return {
            "summary": "Model returned empty JSON code field; fallback to raw text.",
            "code": "",
        }
    return {
        "summary": str(data.get("summary", "initial draft")).strip() or "initial draft",
        "code": _strip_markdown_fence(str(code)),
    }


def _review_logic(agent, description: str, language: str, code: str):
    prompt = (
        "Review whether code satisfies the task logically.\n"
        "Return JSON with keys: is_logically_correct (bool), issues (array of strings).\n"
        "Be strict and list concrete defects only.\n"
        f"Language: {language}\n"
        f"Requirements:\n{description}\n"
        f"Code:\n{code}"
    )
    data = _call_model_json(agent, prompt)
    issues = []
    if isinstance(data, dict):
        raw_issues = data.get("issues", [])
        if isinstance(raw_issues, list):
            issues = [str(item).strip() for item in raw_issues if str(item).strip()]
        is_correct = bool(data.get("is_logically_correct"))
    else:
        is_correct = False

    if is_correct and not issues:
        return True, []
    return False, issues


def _fix_code(agent, description: str, language: str, current_code: str, problems: list[str]):
    joined_problems = "\n".join(f"- {item}" for item in problems)
    prompt = (
        "You are fixing code after validation failures.\n"
        "Return JSON with keys: summary, code\n"
        "The output code must address all listed problems.\n"
        f"Language: {language}\n"
        f"Requirements:\n{description}\n"
        f"Problems:\n{joined_problems}\n"
        f"Current code:\n{current_code}"
    )
    data = _call_model_json(agent, prompt)
    code = data.get("code", "") if isinstance(data, dict) else ""
    if code:
        return {
            "summary": str(data.get("summary", "updated draft")).strip() or "updated draft",
            "code": _strip_markdown_fence(str(code)),
        }
    return {
        "summary": "Model did not return code during fix; keep existing draft.",
        "code": current_code,
    }


def _check_python_syntax(code: str):
    try:
        ast.parse(code)
        return True, "ok"
    except SyntaxError as exc:
        line = exc.lineno or "?"
        msg = exc.msg or "syntax error"
        return False, f"SyntaxError at line {line}: {msg}"


def _check_python_compile(target_path: Path):
    try:
        py_compile.compile(str(target_path), doraise=True)
        return True, "ok"
    except py_compile.PyCompileError as exc:
        return False, str(exc)
    except Exception as exc:
        return False, str(exc)


def _run_command(command: str, cwd: Path):
    if not command.strip():
        return True, "skipped"

    try:
        result = subprocess.run(
            command,
            shell=True,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=60,
        )
    except Exception as exc:
        return False, f"execution failed: {exc}"

    output = (result.stdout or "") + ("\n" + result.stderr if result.stderr else "")
    output = output.strip()[:2400]
    if result.returncode == 0:
        return True, output or "command finished with no output"
    return False, output or f"command exited with code {result.returncode}"


def register(agent):
    def implement_code_task(
        description: str,
        target_path: str,
        language: str = "python",
        run_command: str = "",
        max_rounds: int = 3,
    ):
        task = (description or "").strip()
        if not task:
            raise ValueError("description is required")

        path_text = (target_path or "").strip()
        if not path_text:
            raise ValueError("target_path is required")

        target = Path(path_text).expanduser().resolve()
        target.parent.mkdir(parents=True, exist_ok=True)

        lang = (language or "python").strip().lower()
        rounds = max(1, min(int(max_rounds), 4))

        draft = _generate_initial_code(agent, task, lang, target)
        code = draft.get("code", "")

        history = []
        for index in range(1, rounds + 1):
            target.write_text(code, encoding="utf-8")

            problems = []

            if target.suffix == ".py" or lang == "python":
                syntax_ok, syntax_info = _check_python_syntax(code)
                if not syntax_ok:
                    problems.append(f"syntax check failed: {syntax_info}")

                compile_ok, compile_info = _check_python_compile(target)
                if not compile_ok:
                    problems.append(f"compile check failed: {compile_info}")
            else:
                syntax_ok = True
                syntax_info = "non-python syntax check skipped"
                compile_ok = True
                compile_info = "non-python compile check skipped"

            logic_ok, logic_issues = _review_logic(agent, task, lang, code)
            if not logic_ok:
                if logic_issues:
                    problems.extend(f"logic issue: {issue}" for issue in logic_issues)
                else:
                    problems.append("logic review reported unresolved issues")

            run_ok, run_info = _run_command(run_command, cwd=target.parent)
            if not run_ok:
                problems.append(f"runtime check failed: {run_info}")

            round_report = {
                "round": index,
                "summary": draft.get("summary", ""),
                "syntax": syntax_info,
                "compile": compile_info,
                "runtime": run_info,
                "problems": problems,
            }
            history.append(round_report)

            if not problems:
                return {
                    "success": True,
                    "target_path": str(target),
                    "rounds": index,
                    "history": history,
                    "message": "Code generated, validated, fixed if needed, and checks passed.",
                }

            if index < rounds:
                draft = _fix_code(agent, task, lang, code, problems)
                code = draft.get("code", code)

        return {
            "success": False,
            "target_path": str(target),
            "rounds": rounds,
            "history": history,
            "message": "Checks still failing after max rounds. Review the latest round problems.",
        }

    agent.add_skill(
        name="implement_code_task",
        func=implement_code_task,
        description=(
            "Generate code from requirements, then run syntax/logic checks, auto-fix, "
            "and compile/execute validation in multiple rounds."
        ),
        parameters={
            "description": "string",
            "target_path": "string",
            "language": "string",
            "run_command": "string",
            "max_rounds": "integer",
        },
    )
