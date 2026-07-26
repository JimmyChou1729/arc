from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "plugins/arc/skills/arc/scripts"
old_dont_write_bytecode = sys.dont_write_bytecode
sys.dont_write_bytecode = True
sys.path.insert(0, str(SCRIPTS))
try:
    from _arc_workflows import workflow_io
finally:
    sys.path.remove(str(SCRIPTS))
    sys.dont_write_bytecode = old_dont_write_bytecode


def test_json_object_reader_requires_an_object_root(tmp_path: Path) -> None:
    path = tmp_path / "value.json"
    path.write_text('{"key": "value"}', encoding="utf-8")

    assert workflow_io.read_json_object(path) == {"key": "value"}

    path.write_text("[1, 2]", encoding="utf-8")
    with pytest.raises(workflow_io.NonObjectJsonError, match="JSON root must be an object"):
        workflow_io.read_json_object(path)

    path.write_text("{", encoding="utf-8")
    with pytest.raises(json.JSONDecodeError):
        workflow_io.read_json_object(path)


def test_json_object_writer_preserves_pretty_contract_and_uses_atomic_bytes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    written: dict[str, object] = {}

    def record_write(path: str | Path, content: bytes) -> None:
        written.update(path=Path(path), content=content)

    monkeypatch.setattr(workflow_io, "atomic_write_bytes", record_write)
    destination = tmp_path / "result.json"

    workflow_io.write_json_object(
        destination,
        {"z": "物理", "a": 1},
        sort_keys=True,
    )

    assert written == {
        "path": destination,
        "content": '{\n  "a": 1,\n  "z": "物理"\n}\n'.encode(),
    }
    with pytest.raises(TypeError, match="must be an object"):
        workflow_io.write_json_object(destination, [])  # type: ignore[arg-type]


@pytest.mark.parametrize("value", [True, False, 1.0, "1", None])
def test_strict_integer_rejects_bool_and_numeric_coercion(value: object) -> None:
    with pytest.raises(ValueError, match="workers must be a positive integer"):
        workflow_io.require_strict_int(
            value,
            "workers",
            minimum=1,
            requirement="a positive integer",
        )

    assert (
        workflow_io.require_strict_int(
            1,
            "workers",
            minimum=1,
            requirement="a positive integer",
        )
        == 1
    )


def test_safe_id_is_bounded_for_every_caller() -> None:
    class ConfigError(ValueError):
        pass

    assert workflow_io.require_safe_id("run_001", "run_id") == "run_001"
    too_long = "a" * 129
    with pytest.raises(ConfigError, match=r"run_id must match \^"):
        workflow_io.require_safe_id(
            too_long,
            "run_id",
            error_type=ConfigError,
        )
    with pytest.raises(ConfigError, match=r"variant_id must match \^"):
        workflow_io.require_safe_id(
            too_long,
            "variant_id",
            error_type=ConfigError,
        )
