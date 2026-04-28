import subprocess


class SkillRegistry:
    def __init__(self):
        self.skills = {}

    def register(self, name, func, description, parameters):
        self.skills[name] = {
            "func": func,
            "description": description,
            "parameters": parameters,
        }

    def get_tools_definition(self):
        tools_list = []
        for name, info in self.skills.items():
            tools_list.append(
                {
                    "name": name,
                    "description": info["description"],
                    "parameters": info["parameters"],
                }
            )
        return tools_list


class SystemExecutor:
    @staticmethod
    def execute_shell(command: str):
        try:
            print(f"  [System] Executing: {command}")
            result = subprocess.check_output(command, shell=True, stderr=subprocess.STDOUT, text=True)
            return result if result else "Command executed successfully (no output)."
        except subprocess.CalledProcessError as e:
            return f"Error: {e.output}"
        except Exception as e:
            return f"System Error: {str(e)}"


class ToolApprovalPending(Exception):
    def __init__(self, tool_name: str, parameters: dict, prompt_text: str):
        super().__init__(prompt_text)
        self.tool_name = tool_name
        self.parameters = parameters
        self.prompt_text = prompt_text
