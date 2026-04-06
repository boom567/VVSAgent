from datetime import datetime
from pathlib import Path
import json
import xml.etree.ElementTree as ET


KNOWLEDGE_BASE_DIR = Path(__file__).resolve().parent.parent / "knowledge_base"
KNOWLEDGE_BASE_PATH = KNOWLEDGE_BASE_DIR / "user_profiles.xml"


def _indent_xml(element, level=0):
    indent = "\n" + level * "  "
    if len(element):
        if not element.text or not element.text.strip():
            element.text = indent + "  "
        for child in element:
            _indent_xml(child, level + 1)
        if not element[-1].tail or not element[-1].tail.strip():
            element[-1].tail = indent
    if level and (not element.tail or not element.tail.strip()):
        element.tail = indent


def _ensure_knowledge_base(path: Path = KNOWLEDGE_BASE_PATH):
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        root = ET.Element("knowledge_base")
        ET.SubElement(root, "users")
        tree = ET.ElementTree(root)
        _indent_xml(root)
        tree.write(path, encoding="utf-8", xml_declaration=True)

    tree = ET.parse(path)
    root = tree.getroot()
    users = root.find("users")
    if users is None:
        users = ET.SubElement(root, "users")
    return tree, root, users


def _find_user(users_element, user_name: str):
    for user in users_element.findall("user"):
        if user.get("name") == user_name:
            return user
    return None


def _ensure_child(parent, tag: str):
    child = parent.find(tag)
    if child is None:
        child = ET.SubElement(parent, tag)
    return child


def _parse_preferences(preferences_text: str):
    raw = (preferences_text or "").strip()
    if not raw:
        return {}

    try:
        parsed = json.loads(raw)
        if isinstance(parsed, dict):
            return {str(key): str(value) for key, value in parsed.items()}
    except json.JSONDecodeError:
        pass

    preferences = {}
    normalized = raw.replace("\n", ";")
    for item in normalized.split(";"):
        item = item.strip()
        if not item:
            continue
        if "=" in item:
            key, value = item.split("=", 1)
        elif ":" in item:
            key, value = item.split(":", 1)
        else:
            preferences[item] = "true"
            continue
        preferences[key.strip()] = value.strip()
    return preferences


def _user_to_text(user_element):
    model_name = (user_element.findtext("model_name") or "").strip()
    preferences_node = user_element.find("preferences")
    conclusions_node = user_element.find("conclusions")

    lines = [f"User: {user_element.get('name', '')}"]
    lines.append(f"Model: {model_name or '(not set)'}")
    lines.append("Preferences:")
    if preferences_node is None or not preferences_node.findall("preference"):
        lines.append("- (none)")
    else:
        for preference in preferences_node.findall("preference"):
            lines.append(f"- {preference.get('key', '')}: {(preference.text or '').strip()}")

    lines.append("Conclusions:")
    if conclusions_node is None or not conclusions_node.findall("conclusion"):
        lines.append("- (none)")
    else:
        for conclusion in conclusions_node.findall("conclusion"):
            timestamp = conclusion.get("timestamp", "")
            topic = conclusion.get("topic", "")
            prefix = f"[{timestamp}]"
            if topic:
                prefix += f" ({topic})"
            lines.append(f"- {prefix} {(conclusion.text or '').strip()}")
    return "\n".join(lines)


def _user_to_data(user_element, conclusions_limit: int | None = None):
    preferences_node = user_element.find("preferences")
    conclusions_node = user_element.find("conclusions")

    preferences = {}
    if preferences_node is not None:
        for preference in preferences_node.findall("preference"):
            preferences[preference.get("key", "")] = (preference.text or "").strip()

    conclusions = []
    if conclusions_node is not None:
        conclusion_elements = conclusions_node.findall("conclusion")
        if conclusions_limit is not None:
            conclusion_elements = conclusion_elements[-max(0, conclusions_limit) :]
        for conclusion in conclusion_elements:
            conclusions.append(
                {
                    "timestamp": conclusion.get("timestamp", ""),
                    "topic": conclusion.get("topic", ""),
                    "text": (conclusion.text or "").strip(),
                }
            )

    return {
        "user_name": user_element.get("name", ""),
        "model_name": (user_element.findtext("model_name") or "").strip(),
        "preferences": preferences,
        "conclusions": conclusions,
        "created_at": user_element.get("created_at", ""),
        "updated_at": user_element.get("updated_at", ""),
    }


def register(agent):
    def upsert_user_profile(user_name: str, model_name: str = "", preferences: str = ""):
        name = user_name.strip()
        if not name:
            raise ValueError("user_name is required")

        tree, root, users = _ensure_knowledge_base()
        user = _find_user(users, name)
        created = user is None
        if created:
            user = ET.SubElement(users, "user", {"name": name, "created_at": datetime.now().isoformat(timespec="seconds")})

        user.set("updated_at", datetime.now().isoformat(timespec="seconds"))

        model_node = _ensure_child(user, "model_name")
        resolved_model_name = model_name.strip() or model_node.text or getattr(agent, "model_name", "")
        model_node.text = resolved_model_name

        preferences_node = _ensure_child(user, "preferences")
        parsed_preferences = _parse_preferences(preferences)
        if parsed_preferences:
            existing = {item.get("key"): item for item in preferences_node.findall("preference")}
            for key, value in parsed_preferences.items():
                preference_node = existing.get(key)
                if preference_node is None:
                    preference_node = ET.SubElement(preferences_node, "preference", {"key": key})
                preference_node.text = value

        conclusions_node = _ensure_child(user, "conclusions")
        if not list(conclusions_node):
            conclusions_node.text = "\n    "

        _indent_xml(root)
        tree.write(KNOWLEDGE_BASE_PATH, encoding="utf-8", xml_declaration=True)
        action = "Created" if created else "Updated"
        return f"{action} user profile for {name}. XML stored at {KNOWLEDGE_BASE_PATH}"

    def get_user_profile(user_name: str):
        name = user_name.strip()
        if not name:
            raise ValueError("user_name is required")

        _, _, users = _ensure_knowledge_base()
        user = _find_user(users, name)
        if user is None:
            return f"User not found: {name}"
        return _user_to_text(user)

    def get_user_profile_data(user_name: str, conclusions_limit: int = 5):
        name = user_name.strip()
        if not name:
            raise ValueError("user_name is required")

        _, _, users = _ensure_knowledge_base()
        user = _find_user(users, name)
        if user is None:
            return {"found": False, "user_name": name}

        data = _user_to_data(user, conclusions_limit=max(0, int(conclusions_limit)))
        data["found"] = True
        return data

    def list_user_profiles():
        _, _, users = _ensure_knowledge_base()
        profiles = []
        for user in users.findall("user"):
            model_name = (user.findtext("model_name") or "").strip() or "(not set)"
            preference_count = len(user.find("preferences").findall("preference")) if user.find("preferences") is not None else 0
            conclusion_count = len(user.find("conclusions").findall("conclusion")) if user.find("conclusions") is not None else 0
            profiles.append(
                f"- {user.get('name', '')}: model={model_name}, preferences={preference_count}, conclusions={conclusion_count}"
            )

        if not profiles:
            return f"No user profiles stored. XML path: {KNOWLEDGE_BASE_PATH}"
        return "Stored user profiles:\n" + "\n".join(profiles)

    def add_conversation_conclusion(user_name: str, conclusion: str, topic: str = ""):
        name = user_name.strip()
        summary = conclusion.strip()
        if not name:
            raise ValueError("user_name is required")
        if not summary:
            raise ValueError("conclusion is required")

        tree, root, users = _ensure_knowledge_base()
        user = _find_user(users, name)
        if user is None:
            user = ET.SubElement(users, "user", {"name": name, "created_at": datetime.now().isoformat(timespec="seconds")})
            _ensure_child(user, "model_name").text = getattr(agent, "model_name", "")
            _ensure_child(user, "preferences")

        user.set("updated_at", datetime.now().isoformat(timespec="seconds"))
        conclusions_node = _ensure_child(user, "conclusions")
        conclusion_node = ET.SubElement(
            conclusions_node,
            "conclusion",
            {
                "timestamp": datetime.now().isoformat(timespec="seconds"),
                "topic": topic.strip(),
            },
        )
        conclusion_node.text = summary

        _indent_xml(root)
        tree.write(KNOWLEDGE_BASE_PATH, encoding="utf-8", xml_declaration=True)
        return f"Added conclusion for {name}. XML stored at {KNOWLEDGE_BASE_PATH}"

    def list_conversation_conclusions(user_name: str, limit: int = 10):
        name = user_name.strip()
        if not name:
            raise ValueError("user_name is required")

        _, _, users = _ensure_knowledge_base()
        user = _find_user(users, name)
        if user is None:
            return f"User not found: {name}"

        conclusions_node = user.find("conclusions")
        if conclusions_node is None:
            return f"No conclusions stored for {name}"

        conclusions = conclusions_node.findall("conclusion")
        if not conclusions:
            return f"No conclusions stored for {name}"

        selected = conclusions[-max(1, int(limit)) :]
        lines = [f"Recent conclusions for {name}:"]
        for item in selected:
            timestamp = item.get("timestamp", "")
            topic = item.get("topic", "")
            prefix = f"[{timestamp}]"
            if topic:
                prefix += f" ({topic})"
            lines.append(f"- {prefix} {(item.text or '').strip()}")
        return "\n".join(lines)

    def get_knowledge_base_xml():
        _ensure_knowledge_base()
        return KNOWLEDGE_BASE_PATH.read_text(encoding="utf-8")

    agent.add_skill(
        name="upsert_user_profile",
        func=upsert_user_profile,
        description="Create or update an XML-backed local user profile with model name and preference fields.",
        parameters={
            "user_name": "string",
            "model_name": "string",
            "preferences": "string",
        },
    )
    agent.add_skill(
        name="get_user_profile",
        func=get_user_profile,
        description="Read one user's profile, including model name, preferences, and stored conclusions.",
        parameters={
            "user_name": "string",
        },
    )
    agent.add_skill(
        name="get_user_profile_data",
        func=get_user_profile_data,
        description="Return one user's profile as structured data, including model name, preferences, and recent conclusions.",
        parameters={
            "user_name": "string",
            "conclusions_limit": "integer",
        },
    )
    agent.add_skill(
        name="list_user_profiles",
        func=list_user_profiles,
        description="List all user profiles stored in the local XML knowledge base.",
        parameters={},
    )
    agent.add_skill(
        name="add_conversation_conclusion",
        func=add_conversation_conclusion,
        description="Append a conversation conclusion to a named user in the local XML knowledge base.",
        parameters={
            "user_name": "string",
            "conclusion": "string",
            "topic": "string",
        },
    )
    agent.add_skill(
        name="list_conversation_conclusions",
        func=list_conversation_conclusions,
        description="List recent stored conversation conclusions for one named user.",
        parameters={
            "user_name": "string",
            "limit": "integer",
        },
    )
    agent.add_skill(
        name="get_knowledge_base_xml",
        func=get_knowledge_base_xml,
        description="Return the full raw XML content of the local knowledge base.",
        parameters={},
    )