import shutil
import subprocess
from datetime import datetime
from pathlib import Path


def _build_output_path(output_path: str | None):
    if output_path:
        target = Path(output_path).expanduser()
    else:
        photos_dir = Path(__file__).resolve().parent.parent / "photos"
        filename = datetime.now().strftime("photo_%Y%m%d_%H%M%S.jpg")
        target = photos_dir / filename

    target.parent.mkdir(parents=True, exist_ok=True)
    return target


def _resolve_photo_path(output_path: str | None):
    if output_path:
        target = Path(output_path).expanduser()
        if not target.exists():
            raise FileNotFoundError(f"Photo does not exist: {target}")
        return target

    photos_dir = Path(__file__).resolve().parent.parent / "photos"
    if not photos_dir.exists():
        raise FileNotFoundError("No photos directory exists yet. Capture a photo first or provide an output_path.")

    candidates = sorted(
        [path for path in photos_dir.iterdir() if path.is_file()],
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    if not candidates:
        raise FileNotFoundError("No photos found. Capture a photo first or provide an output_path.")

    return candidates[0]


def _capture_with_imagesnap(target: Path, delay_seconds: int):
    command = ["imagesnap", "-w", str(delay_seconds), str(target)]
    result = subprocess.run(command, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip() or "imagesnap failed")


def take_photo(output_path: str = "", delay_seconds: int = 2):
    target = _build_output_path(output_path or None)

    if shutil.which("imagesnap"):
        _capture_with_imagesnap(target, delay_seconds)
        return f"Photo captured successfully: {target}"

    return (
        "Camera skill is installed, but no supported capture backend is available. "
        "Install 'imagesnap' on macOS, then try again. "
        f"Suggested command: brew install imagesnap. Planned output path: {target}"
    )


def show_photo(output_path: str = ""):
    target = _resolve_photo_path(output_path or None)

    if not shutil.which("open"):
        return "The macOS 'open' command is not available on this system."

    command = ["open", "-a", "Preview", str(target)]
    result = subprocess.run(command, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip() or "open failed")

    return f"Opened photo in a new Preview window: {target}"


def register(agent):
    agent.add_skill(
        name="take_photo",
        func=take_photo,
        description=(
            "Capture a photo using the local camera on macOS. "
            "Optionally provide an output_path and delay_seconds before capture."
        ),
        parameters={
            "output_path": "string",
            "delay_seconds": "integer"
        }
    )
    agent.add_skill(
        name="show_photo",
        func=show_photo,
        description=(
            "Open a photo in a new Preview window on macOS. "
            "If output_path is empty, open the newest photo from the local photos directory."
        ),
        parameters={
            "output_path": "string"
        }
    )