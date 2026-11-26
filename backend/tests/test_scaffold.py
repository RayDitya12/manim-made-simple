def test_project_layout_exists() -> None:
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    for rel in ["backend/app", "backend/scenes", "backend/fonts", "ui", "desktop", "scripts"]:
        assert (root / rel).is_dir(), f"missing directory: {rel}"
