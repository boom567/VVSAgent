from aiagent import ModularAgent, safe_calculate


if __name__ == "__main__":
    agent = ModularAgent(model_name="deepseek-v3.1:671b-cloud", provider="ollama")
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
