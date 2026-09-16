import asyncio
from pathlib import Path

from submissions_checker.workers.tasks.review_tasks import (
    DEFAULT_SOURCE_EXTENSIONS,
    collect_lab_data,
)


def _write(root: Path, rel: str, text: str) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


def test_default_extensions_cover_the_shipped_subjects() -> None:
    for ext in (".py", ".java", ".cpp", ".c", ".h", ".kt", ".js", ".ts", ".go", ".rs", ".cs"):
        assert ext in DEFAULT_SOURCE_EXTENSIONS


def test_collects_java_and_cpp_by_default(tmp_path: Path) -> None:
    _write(tmp_path, "README.md", "task")
    _write(tmp_path, "src/Main.java", "class Main {}")
    _write(tmp_path, "lab/a.cpp", "int main(){}")
    _write(tmp_path, "build/out.class", "binary")
    task, code = asyncio.run(collect_lab_data(str(tmp_path)))
    assert task == "task"
    assert "class Main" in code and "int main" in code
    assert "out.class" not in code


def test_explicit_extensions_override_default(tmp_path: Path) -> None:
    _write(tmp_path, "a.py", "print(1)")
    _write(tmp_path, "b.sql", "select 1")
    _, code = asyncio.run(collect_lab_data(str(tmp_path), ["SQL"]))
    assert "select 1" in code and "print(1)" not in code


def test_total_size_is_capped(tmp_path: Path) -> None:
    _write(tmp_path, "big.py", "x" * 1000)
    _, code = asyncio.run(collect_lab_data(str(tmp_path), max_chars=300))
    assert len(code) <= 300 + 100  # cap plus the truncation marker
    assert "[truncated]" in code
