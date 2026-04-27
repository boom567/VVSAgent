from aiagent import build_agent_from_config, safe_calculate


if __name__ == "__main__":
    agent = build_agent_from_config()
    agent.add_skill(
        name="calculator",
        func=safe_calculate,
        description="Evaluate a mathematical expression.",
        parameters={"expression": "string"},
    )

    print("Coding Agent console started.")
    print("Use natural language to describe coding tasks.")
    print("For end-to-end coding workflow, ask the agent to call tool: implement_code_task")
    agent.chat_loop()
