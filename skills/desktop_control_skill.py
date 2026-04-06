import subprocess
from datetime import datetime
import json
from pathlib import Path
import time


DESKTOP_TMP_DIR = Path(__file__).resolve().parent.parent / "desktop_tmp"


def _desktop_file_path(output_path: str | None = None, prefix: str = "screen", suffix: str = ".png"):
    if output_path:
        target = Path(output_path).expanduser()
    else:
        filename = datetime.now().strftime(f"{prefix}_%Y%m%d_%H%M%S{suffix}")
        target = DESKTOP_TMP_DIR / filename

    target.parent.mkdir(parents=True, exist_ok=True)
    return target


def _require_pyautogui():
    try:
        import pyautogui
    except ImportError as exc:
        raise RuntimeError(
            "Desktop mouse and keyboard control requires the pyautogui package in the agent conda environment."
        ) from exc

    pyautogui.FAILSAFE = True
    pyautogui.PAUSE = 0.05
    return pyautogui


def _run_osascript(script: str, language: str = "AppleScript"):
    command = ["/usr/bin/osascript"]
    if language != "AppleScript":
        command.extend(["-l", language])
    command.extend(["-e", script])
    result = subprocess.run(command, capture_output=True, text=True)
    if result.returncode != 0:
        details = result.stderr.strip() or result.stdout.strip() or "osascript failed"
        raise RuntimeError(
            "Desktop control failed. Check macOS Accessibility permission for the terminal or Python process. "
            f"Details: {details}"
        )
    return result.stdout.strip()


def _button_event_names(button: str):
    normalized = button.strip().lower()
    mapping = {
        "left": ("kCGEventLeftMouseDown", "kCGEventLeftMouseUp", "kCGMouseButtonLeft"),
        "right": ("kCGEventRightMouseDown", "kCGEventRightMouseUp", "kCGMouseButtonRight"),
        "middle": ("kCGEventOtherMouseDown", "kCGEventOtherMouseUp", "kCGMouseButtonCenter"),
    }
    if normalized not in mapping:
        raise ValueError("button must be one of: left, right, middle")
    return mapping[normalized]


def _move_mouse_with_osascript(x: int, y: int):
    script = f"""
ObjC.import('ApplicationServices');
var point = $.CGPointMake({int(x)}, {int(y)});
var event = $.CGEventCreateMouseEvent(null, $.kCGEventMouseMoved, point, $.kCGMouseButtonLeft);
$.CGEventPost($.kCGHIDEventTap, event);
""".strip()
    _run_osascript(script, language="JavaScript")


def _click_mouse_with_osascript(x: int, y: int, button: str, clicks: int):
    down_event, up_event, mouse_button = _button_event_names(button)
    script = f"""
ObjC.import('ApplicationServices');
function post(eventType) {{
  var point = $.CGPointMake({int(x)}, {int(y)});
  var event = $.CGEventCreateMouseEvent(null, $[eventType], point, $.{mouse_button});
  $.CGEventPost($.kCGHIDEventTap, event);
}}
for (var i = 0; i < {max(1, int(clicks))}; i++) {{
  post('{down_event}');
  post('{up_event}');
}}
""".strip()
    _run_osascript(script, language="JavaScript")


def _drag_mouse_with_osascript(x: int, y: int):
    script = f"""
ObjC.import('ApplicationServices');
ObjC.import('Cocoa');
var start = $.NSEvent.mouseLocation;
var startPoint = $.CGPointMake(start.x, start.y);
var endPoint = $.CGPointMake({int(x)}, {int(y)});
var downEvent = $.CGEventCreateMouseEvent(null, $.kCGEventLeftMouseDown, startPoint, $.kCGMouseButtonLeft);
$.CGEventPost($.kCGHIDEventTap, downEvent);
var dragEvent = $.CGEventCreateMouseEvent(null, $.kCGEventLeftMouseDragged, endPoint, $.kCGMouseButtonLeft);
$.CGEventPost($.kCGHIDEventTap, dragEvent);
var upEvent = $.CGEventCreateMouseEvent(null, $.kCGEventLeftMouseUp, endPoint, $.kCGMouseButtonLeft);
$.CGEventPost($.kCGHIDEventTap, upEvent);
""".strip()
    _run_osascript(script, language="JavaScript")


def _get_mouse_position_with_osascript():
    script = """
ObjC.import('Cocoa');
var loc = $.NSEvent.mouseLocation;
loc.x + ',' + loc.y;
""".strip()
    output = _run_osascript(script, language="JavaScript")
    x_text, y_text = output.split(",", 1)
    return int(float(x_text)), int(float(y_text))


def _type_text_with_osascript(text: str):
    script = f'tell application "System Events" to keystroke {json.dumps(text)}'
    _run_osascript(script)


def _press_hotkey_with_osascript(keys: str):
    normalized = [item.strip().lower() for item in keys.replace("+", ",").split(",") if item.strip()]
    if not normalized:
        raise ValueError("keys is required, for example: command,l")

    modifier_map = {
        "command": "command down",
        "cmd": "command down",
        "control": "control down",
        "ctrl": "control down",
        "option": "option down",
        "alt": "option down",
        "shift": "shift down",
    }
    key_code_map = {
        "return": 36,
        "enter": 36,
        "tab": 48,
        "space": 49,
        "delete": 51,
        "escape": 53,
        "esc": 53,
        "left": 123,
        "right": 124,
        "down": 125,
        "up": 126,
    }

    modifiers = [modifier_map[item] for item in normalized[:-1] if item in modifier_map]
    main_key = normalized[-1]
    using_clause = f" using {{{', '.join(modifiers)}}}" if modifiers else ""

    if main_key in key_code_map:
        script = f'tell application "System Events" to key code {key_code_map[main_key]}{using_clause}'
    else:
        script = f'tell application "System Events" to keystroke {json.dumps(main_key)}{using_clause}'

    _run_osascript(script)


def _capture_screen_to_path(target: Path):
    result = subprocess.run(["/usr/sbin/screencapture", "-x", str(target)], capture_output=True, text=True)
    if result.returncode != 0:
        details = result.stderr.strip() or result.stdout.strip() or "screencapture failed"
        raise RuntimeError(
            "Screen capture failed. Check macOS Screen Recording permission for the terminal or Python process. "
            f"Details: {details}"
        )
    return target


def _normalize_action_plan(plan):
    if not isinstance(plan, dict):
        raise ValueError("Action plan must be a JSON object")

    summary = str(plan.get("summary", "")).strip()
    actions = plan.get("actions", [])
    if not isinstance(actions, list):
        raise ValueError("Action plan field 'actions' must be a list")

    normalized_actions = []
    for index, action in enumerate(actions, start=1):
        if not isinstance(action, dict):
            continue

        action_type = str(action.get("type", "")).strip().lower()
        normalized = {
            "index": index,
            "type": action_type,
            "reason": str(action.get("reason", "")).strip(),
        }

        for key in ("x", "y", "text", "keys", "button", "duration_seconds", "seconds", "clicks", "interval_seconds"):
            if key in action:
                normalized[key] = action[key]

        normalized_actions.append(normalized)

    return {
        "summary": summary,
        "actions": normalized_actions,
    }


def _format_action_plan(plan, screenshot_path: Path):
    lines = [f"Captured screen: {screenshot_path}"]
    if plan.get("summary"):
        lines.append(f"Summary: {plan['summary']}")

    lines.append("Actions:")
    if not plan.get("actions"):
        lines.append("- (none)")
        return "\n".join(lines)

    for action in plan["actions"]:
        pieces = [f"- #{action['index']} {action.get('type', 'unknown')}"]
        if "x" in action and "y" in action:
            pieces.append(f"x={action['x']} y={action['y']}")
        if action.get("text"):
            pieces.append(f"text={action['text']}")
        if action.get("keys"):
            pieces.append(f"keys={action['keys']}")
        if action.get("reason"):
            pieces.append(f"reason={action['reason']}")
        lines.append(" | ".join(pieces))
    return "\n".join(lines)


def _confirm_desktop_execution(agent, plan_text: str):
    prompt_reader = getattr(agent, "_read_console_input", None)
    if not callable(prompt_reader):
        return True

    answer = prompt_reader(
        "[Confirm] Execute desktop action plan on the live screen? [y/N]: "
    ).strip().lower()
    return answer in {"y", "yes"}


def _execute_desktop_action(action, move_mouse, click_mouse, drag_mouse, type_text, press_hotkey):
    action_type = action.get("type", "")
    if action_type == "move_mouse":
        return move_mouse(
            x=int(action["x"]),
            y=int(action["y"]),
            duration_seconds=float(action.get("duration_seconds", 0.2)),
        )

    if action_type == "click_mouse":
        return click_mouse(
            x=int(action["x"]),
            y=int(action["y"]),
            button=str(action.get("button", "left")),
            clicks=int(action.get("clicks", 1)),
            interval_seconds=float(action.get("interval_seconds", 0.1)),
        )

    if action_type == "drag_mouse":
        return drag_mouse(
            x=int(action["x"]),
            y=int(action["y"]),
            duration_seconds=float(action.get("duration_seconds", 0.5)),
            button=str(action.get("button", "left")),
        )

    if action_type == "type_text":
        return type_text(
            text=str(action.get("text", "")),
            interval_seconds=float(action.get("interval_seconds", 0.02)),
        )

    if action_type == "press_hotkey":
        return press_hotkey(str(action.get("keys", "")))

    if action_type == "wait":
        seconds = float(action.get("seconds", 0.5))
        time.sleep(max(0.0, seconds))
        return f"Waited {seconds} seconds."

    raise ValueError(f"Unsupported desktop action type: {action_type}")


def register(agent):
    def capture_screen(output_path: str = ""):
        target = _desktop_file_path(output_path or None)
        _capture_screen_to_path(target)
        return f"Captured screen: {target}"

    def analyze_screen(
        prompt_text: str = "Describe the current screen and identify actionable UI elements.",
        image_path: str = "",
    ):
        if image_path:
            target = Path(image_path).expanduser()
            if not target.exists():
                raise FileNotFoundError(f"Image does not exist: {target}")
        else:
            target = _desktop_file_path(prefix="screen_analysis")
            _capture_screen_to_path(target)

        model_name = agent.model_name
        get_active_model = getattr(agent, "_get_active_model_name", None)
        if callable(get_active_model):
            model_name = get_active_model()

        response = agent.client.chat(
            model=model_name,
            messages=[
                {
                    "role": "user",
                    "content": prompt_text,
                    "images": [str(target)],
                }
            ],
        )
        content = response["message"].get("content", "")
        if not content:
            return f"Captured screen: {target}\nThe model returned no textual analysis."

        return f"Captured screen: {target}\nAnalysis: {content}"

    def plan_screen_actions(
        goal: str,
        image_path: str = "",
        max_actions: int = 3,
        execute_actions: bool = False,
    ):
        if not goal.strip():
            raise ValueError("goal is required")

        if image_path:
            target = Path(image_path).expanduser()
            if not target.exists():
                raise FileNotFoundError(f"Image does not exist: {target}")
        else:
            target = _desktop_file_path(prefix="screen_plan")
            _capture_screen_to_path(target)

        model_name = agent.model_name
        get_active_model = getattr(agent, "_get_active_model_name", None)
        if callable(get_active_model):
            model_name = get_active_model()

        prompt_text = f"""You are planning desktop UI actions on macOS based on a screenshot.

User goal:
{goal}

Return exactly one JSON object with this schema:
{{
  "summary": "brief understanding of the current UI",
  "actions": [
    {{
      "type": "click_mouse|move_mouse|drag_mouse|type_text|press_hotkey|wait",
      "x": 0,
      "y": 0,
      "text": "text to type when needed",
      "keys": "command,l when needed",
      "button": "left",
      "duration_seconds": 0.2,
      "seconds": 0.5,
      "clicks": 1,
      "interval_seconds": 0.1,
      "reason": "why this step helps"
    }}
  ]
}}

Rules:
- Use at most {max(1, int(max_actions))} actions.
- Only include coordinates if the screenshot gives enough confidence.
- Prefer safe, simple actions.
- If the goal cannot be completed safely from the screenshot, return an empty actions list.
- Do not include any markdown.
"""

        response = agent.client.chat(
            model=model_name,
            messages=[
                {
                    "role": "user",
                    "content": prompt_text,
                    "images": [str(target)],
                }
            ],
            format="json",
        )
        content = response["message"].get("content", "")
        if not content:
            return f"Captured screen: {target}\nThe model returned no action plan."

        try:
            plan = _normalize_action_plan(json.loads(content))
        except json.JSONDecodeError:
            plan = _normalize_action_plan(json.loads(agent._extract_json(content)))

        formatted_plan = _format_action_plan(plan, target)
        if not execute_actions:
            return formatted_plan

        if not plan["actions"]:
            return formatted_plan + "\n\nNo actions were executed because the plan is empty."

        if not _confirm_desktop_execution(agent, formatted_plan):
            return formatted_plan + "\n\nDesktop action execution cancelled by user."

        results = []
        for action in plan["actions"]:
            try:
                results.append(_execute_desktop_action(action, move_mouse, click_mouse, drag_mouse, type_text, press_hotkey))
            except Exception as exc:
                results.append(f"Action #{action['index']} failed: {exc}")
                break

        return formatted_plan + "\n\nExecution results:\n- " + "\n- ".join(results)

    def get_mouse_position():
        try:
            pyautogui = _require_pyautogui()
            position = pyautogui.position()
            return f"Mouse position: x={position.x}, y={position.y}"
        except RuntimeError:
            x, y = _get_mouse_position_with_osascript()
            return f"Mouse position: x={x}, y={y}"

    def move_mouse(x: int, y: int, duration_seconds: float = 0.2):
        try:
            pyautogui = _require_pyautogui()
            pyautogui.moveTo(int(x), int(y), duration=float(duration_seconds))
        except RuntimeError:
            _move_mouse_with_osascript(int(x), int(y))
        return f"Moved mouse to x={int(x)}, y={int(y)}"

    def click_mouse(
        x: int,
        y: int,
        button: str = "left",
        clicks: int = 1,
        interval_seconds: float = 0.1,
    ):
        try:
            pyautogui = _require_pyautogui()
            pyautogui.click(
                x=int(x),
                y=int(y),
                clicks=max(1, int(clicks)),
                interval=float(interval_seconds),
                button=button,
            )
        except RuntimeError:
            _click_mouse_with_osascript(int(x), int(y), button, max(1, int(clicks)))
        return f"Clicked mouse at x={int(x)}, y={int(y)}, button={button}, clicks={max(1, int(clicks))}"

    def drag_mouse(x: int, y: int, duration_seconds: float = 0.5, button: str = "left"):
        if button != "left":
            raise ValueError("drag_mouse currently supports only the left mouse button")

        try:
            pyautogui = _require_pyautogui()
            pyautogui.dragTo(int(x), int(y), duration=float(duration_seconds), button=button)
        except RuntimeError:
            _drag_mouse_with_osascript(int(x), int(y))
        return f"Dragged mouse to x={int(x)}, y={int(y)}, button={button}"

    def type_text(text: str, interval_seconds: float = 0.02):
        try:
            pyautogui = _require_pyautogui()
            pyautogui.write(text, interval=float(interval_seconds))
        except RuntimeError:
            _type_text_with_osascript(text)
        return f"Typed text ({len(text)} chars)."

    def press_hotkey(keys: str):
        normalized = [item.strip().lower() for item in keys.replace("+", ",").split(",") if item.strip()]
        if not normalized:
            raise ValueError("keys is required, for example: command,l")

        try:
            pyautogui = _require_pyautogui()
            pyautogui.hotkey(*normalized)
        except RuntimeError:
            _press_hotkey_with_osascript(keys)
        return f"Pressed hotkey: {', '.join(normalized)}"

    agent.add_skill(
        name="capture_screen",
        func=capture_screen,
        description="Capture the current macOS screen to a PNG file.",
        parameters={
            "output_path": "string",
        },
    )
    agent.add_skill(
        name="analyze_screen",
        func=analyze_screen,
        description="Capture the current screen and analyze the UI using the multimodal model, or analyze a provided image_path.",
        parameters={
            "prompt_text": "string",
            "image_path": "string",
        },
    )
    agent.add_skill(
        name="plan_screen_actions",
        func=plan_screen_actions,
        description=(
            "Capture the current screen, ask the multimodal model to generate a small desktop action plan, "
            "and optionally execute the planned mouse/keyboard actions after confirmation."
        ),
        parameters={
            "goal": "string",
            "image_path": "string",
            "max_actions": "integer",
            "execute_actions": "boolean",
        },
    )
    agent.add_skill(
        name="get_mouse_position",
        func=get_mouse_position,
        description="Get the current mouse cursor position on the screen.",
        parameters={},
    )
    agent.add_skill(
        name="move_mouse",
        func=move_mouse,
        description="Move the mouse cursor to an absolute screen coordinate.",
        parameters={
            "x": "integer",
            "y": "integer",
            "duration_seconds": "number",
        },
    )
    agent.add_skill(
        name="click_mouse",
        func=click_mouse,
        description="Click the mouse at an absolute screen coordinate.",
        parameters={
            "x": "integer",
            "y": "integer",
            "button": "string",
            "clicks": "integer",
            "interval_seconds": "number",
        },
    )
    agent.add_skill(
        name="drag_mouse",
        func=drag_mouse,
        description="Drag the mouse cursor to an absolute screen coordinate.",
        parameters={
            "x": "integer",
            "y": "integer",
            "duration_seconds": "number",
            "button": "string",
        },
    )
    agent.add_skill(
        name="type_text",
        func=type_text,
        description="Type text into the currently focused application.",
        parameters={
            "text": "string",
            "interval_seconds": "number",
        },
    )
    agent.add_skill(
        name="press_hotkey",
        func=press_hotkey,
        description="Press a keyboard shortcut such as command,l or ctrl,shift,esc.",
        parameters={
            "keys": "string",
        },
    )