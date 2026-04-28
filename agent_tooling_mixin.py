import json
import re

from agent_config import prompt
from agent_core import SystemExecutor, ToolApprovalPending


class AgentToolingMixin:
    def _execute_shell_with_confirmation(self, command: str):
        if self._shell_command_needs_confirmation(command):
            answer = self._read_console_input(f"[Confirm] Run shell command '{command}'? [y/N]: ").strip().lower()
            if answer not in {"y", "yes"}:
                return "Command execution cancelled by user."
        return SystemExecutor.execute_shell(command)

    def _shell_command_needs_confirmation(self, command: str):
        normalized = command.strip().lower()
        if not normalized:
            return False

        delete_patterns = [
            r"(^|\\s)(rm|rmdir|rd|del|erase|unlink)(\\s|$)",
            r"(^|\\s)(remove-item|ri)(\\s|$)",
        ]
        create_patterns = [
            r"(^|\\s)(touch|mkdir|md)(\\s|$)",
            r"(^|\\s)(new-item|ni)(\\s|$)",
            r">",
        ]

        for pattern in delete_patterns + create_patterns:
            if re.search(pattern, normalized):
                return True
        return False

    def _read_console_input(self, prompt_text: str):
        return prompt(prompt_text)

    def _tool_action_needs_confirmation(self, tool_name: str, params):
        normalized_tool = str(tool_name or "").strip().lower()
        if not normalized_tool:
            return False

        params = params if isinstance(params, dict) else {}
        path_keys = {
            "file_path",
            "target_path",
            "path",
            "source_path",
            "destination_path",
        }
        has_path_like_param = any(key in params for key in path_keys)

        file_keywords = ("file", "excel", "sheet", "workbook", "path", "code")
        io_keywords = ("read", "write", "create", "append", "delete", "remove", "save", "update")
        is_file_related = any(keyword in normalized_tool for keyword in file_keywords)
        is_read_write_action = any(keyword in normalized_tool for keyword in io_keywords)

        explicit_tools = {
            "implement_code_task",
            "create_excel_file",
            "list_workbook_sheets",
            "create_sheet",
            "delete_sheet",
            "read_excel_range",
            "write_excel_cells",
            "append_excel_row",
            "find_in_excel",
        }

        return normalized_tool in explicit_tools or (has_path_like_param and is_file_related and is_read_write_action)

    def _request_tool_approval(self, tool_name: str, params, assistant_content: str, iteration_index: int):
        prompt_text = (
            f"[Confirm] Allow tool '{tool_name}' to access files? "
            f"params={json.dumps(params, ensure_ascii=False)}"
        )

        if callable(self.tool_approval_handler):
            decision = self.tool_approval_handler(tool_name, params, prompt_text)
            if decision is None:
                self.pending_tool_call = {
                    "tool_name": tool_name,
                    "params": params,
                    "assistant_content": assistant_content,
                    "iteration_index": iteration_index,
                    "prompt_text": prompt_text,
                }
                raise ToolApprovalPending(tool_name, params, prompt_text)
            return bool(decision)

        answer = self._read_console_input(f"{prompt_text} [y/N]: ").strip().lower()
        return answer in {"y", "yes"}

    def _execute_action_tool(self, tool_name: str, params):
        if tool_name not in self.registry.skills:
            return f"Error: Tool '{tool_name}' not found."

        print(f"[Action]: Calling {tool_name} with {params}")
        func = self.registry.skills[tool_name]["func"]
        try:
            return str(func(**params))
        except Exception as e:
            return f"Error executing tool: {str(e)}"

    def _run_iterations(self, initial_iteration: int, original_user_input: str):
        for i in range(initial_iteration, self.max_iterations):
            print(f"\n--- Iteration {i+1} ---")

            try:
                response = self.chat_completion(
                    model=self._get_active_model_name(),
                    messages=[{"role": "system", "content": self._get_system_prompt()}] + self.history,
                    format="json",
                )
                content = response["message"]["content"]

                json_str = self._extract_json(content)
                if not json_str:
                    self._request_json_retry(content, "response was not complete valid JSON")
                    continue

                try:
                    data = json.loads(json_str)
                except json.JSONDecodeError as exc:
                    self._request_json_retry(content, f"JSON decode error: {exc}")
                    continue
                if data.get("thought"):
                    thought = data.get("thought")
                    print(f"[AI Thought]: {thought}")
                    self.last_thought = thought

                if "final_answer" in data:
                    self.history.append({"role": "assistant", "content": content})
                    self._maybe_store_conversation_conclusion(original_user_input, data["final_answer"])
                    return data["final_answer"]

                elif "action" in data:
                    action = data["action"]
                    tool_name = action["name"]
                    params = self._normalize_action_parameters(tool_name, action.get("parameters", {}))
                    self.last_action = {"name": tool_name, "parameters": params}

                    if self._tool_action_needs_confirmation(tool_name, params):
                        approved = self._request_tool_approval(tool_name, params, content, i)
                        if not approved:
                            observation = "Tool execution skipped by user."
                        else:
                            observation = self._execute_action_tool(tool_name, params)
                    else:
                        observation = self._execute_action_tool(tool_name, params)

                    print(f"[Observation]: {observation[:100]}...")
                    self.history.append({"role": "assistant", "content": content})
                    self.history.append({"role": "user", "content": f"Observation: {observation}"})

                else:
                    raise ValueError("JSON missing both 'action' and 'final_answer'.")

            except ToolApprovalPending:
                raise
            except Exception as e:
                import socket

                error_msg = f"Error in Agent loop: {str(e)}"
                print(error_msg)
                if isinstance(e, (TimeoutError, socket.timeout)) or "timed out" in str(e).lower():
                    print("[Agent] Request timed out, retrying...")
                    continue
                self.history.append({"role": "user", "content": error_msg})
                break

        return "Agent stopped: Max iterations reached or error occurred."

    def resume_pending_approval(self, approved: bool):
        pending = self.pending_tool_call
        if not pending:
            raise ValueError("No pending tool approval.")

        self.pending_tool_call = None
        tool_name = pending["tool_name"]
        params = pending["params"]
        assistant_content = pending["assistant_content"]
        iteration_index = int(pending["iteration_index"])

        if approved:
            observation = self._execute_action_tool(tool_name, params)
        else:
            observation = "Tool execution skipped by user."

        print(f"[Observation]: {observation[:100]}...")
        self.history.append({"role": "assistant", "content": assistant_content})
        self.history.append({"role": "user", "content": f"Observation: {observation}"})

        original_user_input = ""
        for message in self.history:
            if message.get("role") == "user":
                content = str(message.get("content", ""))
                if not content.startswith("Observation:") and not content.startswith("Error in Agent loop:"):
                    original_user_input = content
                    break
        return self._run_iterations(iteration_index + 1, original_user_input)

    def _canonical_parameter_name(self, name: str):
        return re.sub(r"[^a-z0-9]", "", name.lower())

    def _normalize_action_parameters(self, tool_name: str, params):
        if not isinstance(params, dict):
            return params

        expected = self.registry.skills.get(tool_name, {}).get("parameters", {})
        if not expected:
            return params

        canonical_to_actual = {
            self._canonical_parameter_name(parameter_name): parameter_name for parameter_name in expected.keys()
        }

        normalized = {}
        for key, value in params.items():
            if key in expected:
                normalized[key] = value
                continue

            canonical = self._canonical_parameter_name(str(key))
            actual_name = canonical_to_actual.get(canonical)
            if actual_name:
                normalized[actual_name] = value

        return normalized

    def _request_json_retry(self, content: str, reason: str):
        self.history.append({"role": "assistant", "content": content})
        self.history.append(
            {
                "role": "user",
                "content": (
                    f"Your previous response was invalid: {reason}. "
                    "Respond again with exactly one complete JSON object only. "
                    "Use the exact tool name and exact parameter names from the tool definitions."
                ),
            }
        )

    def _extract_json(self, text):
        text = text.strip()
        if not text:
            return None

        try:
            json.loads(text)
            return text
        except json.JSONDecodeError:
            pass

        start = text.find("{")
        if start == -1:
            return None

        decoder = json.JSONDecoder()
        try:
            obj, _ = decoder.raw_decode(text[start:])
            return json.dumps(obj)
        except json.JSONDecodeError:
            return None
