"""Validate syntax for supported protocol and Home Assistant Python versions."""

import ast
from pathlib import Path

import pytest


@pytest.mark.parametrize("version", [(3, 11), (3, 13)])
def test_modules_parse_on_supported_python(version: tuple[int, int]) -> None:
    root = Path(__file__).resolve().parents[1]
    files = [root / name for name in ("nl_ltpdu.py", "nl_ltpdu_v3.py", "nl_api.py", "cli.py")]
    files.extend((root / "custom_components/nanoleaf_lights").glob("*.py"))
    for path in files:
        ast.parse(path.read_text(), filename=str(path), feature_version=version)
