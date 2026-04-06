from pathlib import Path


def _resolve_image_path(image_path: str | None):
    if image_path:
        target = Path(image_path).expanduser()
        if not target.exists():
            raise FileNotFoundError(f"Image does not exist: {target}")
        if not target.is_file():
            raise ValueError(f"Image path is not a file: {target}")
        return target

    photos_dir = Path(__file__).resolve().parent.parent / "photos"
    if not photos_dir.exists():
        raise FileNotFoundError("No photos directory exists yet. Capture a photo first or provide image_path.")

    candidates = sorted(
        [path for path in photos_dir.iterdir() if path.is_file()],
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    if not candidates:
        raise FileNotFoundError("No photos found. Capture a photo first or provide image_path.")

    return candidates[0]


def register(agent):
    def analyze_image(image_path: str = "", prompt_text: str = "Describe this image in detail."):
        target = _resolve_image_path(image_path or None)
        response = agent.client.chat(
            model=agent.model_name,
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
            return f"Image was sent successfully, but the model returned no text. Image path: {target}"

        return f"Image: {target}\nAnalysis: {content}"

    agent.add_skill(
        name="analyze_image",
        func=analyze_image,
        description=(
            "Analyze an image using the current multimodal model. "
            "Provide image_path to analyze a specific file, or leave it empty to analyze the newest local photo. "
            "Use prompt_text to specify what to look for in the image."
        ),
        parameters={
            "image_path": "string",
            "prompt_text": "string"
        }
    )