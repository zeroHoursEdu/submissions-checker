"""Executes student code in an isolated Docker container sandbox."""

from __future__ import annotations

import asyncio
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from submissions_checker.core.logging import get_logger

logger = get_logger(__name__)

_DEFAULT_MEMORY = "256m"
_DEFAULT_CPUS = 0.5
_DEFAULT_TIMEOUT = 30


@dataclass
class SandboxResult:
    exit_code: int
    stdout: str
    stderr: str
    output_files: dict[str, str] = field(default_factory=dict)


class DockerSandbox:
    """Runs a script inside a Docker container with strict isolation."""

    async def run(
        self,
        *,
        image: str,
        tool: str,
        script_path: str,
        student_files_dir: Path,
        plugin_dir: Path,
        env: dict[str, str] | None = None,
        memory: str = _DEFAULT_MEMORY,
        cpus: float = _DEFAULT_CPUS,
        timeout: int = _DEFAULT_TIMEOUT,
    ) -> SandboxResult:
        """Run `tool /plugin/{script_path} /submission` inside an isolated container.

        /submission  ← student_files_dir (read-only)
        /plugin      ← plugin_dir (read-only)
        /output      ← temp dir (writable); read back after container exits
        """
        with tempfile.TemporaryDirectory(prefix="sandbox_output_") as output_dir:
            cmd = [
                "docker", "run", "--rm",
                "--network", "none",
                f"--memory={memory}",
                f"--cpus={cpus}",
                "--pids-limit=100",
                "--no-new-privileges",
                "--read-only",
                "--tmpfs", "/tmp:rw,size=64m",
                "-v", f"{student_files_dir}:/submission:ro",
                "-v", f"{plugin_dir}:/plugin:ro",
                "-v", f"{output_dir}:/output:rw",
            ]
            for k, v in (env or {}).items():
                cmd += ["-e", f"{k}={v}"]
            cmd += [image, tool, f"/plugin/{script_path}", "/submission"]

            logger.info(
                "sandbox_starting",
                image=image,
                script=script_path,
                timeout=timeout,
            )

            try:
                proc = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                try:
                    stdout_bytes, stderr_bytes = await asyncio.wait_for(
                        proc.communicate(), timeout=timeout
                    )
                except asyncio.TimeoutError:
                    proc.kill()
                    await proc.communicate()
                    logger.warning("sandbox_timeout", image=image, script=script_path)
                    return SandboxResult(
                        exit_code=-1,
                        stdout="",
                        stderr="Sandbox timed out.",
                    )

                exit_code = proc.returncode or 0
                stdout = stdout_bytes.decode("utf-8", errors="replace")
                stderr = stderr_bytes.decode("utf-8", errors="replace")

                output_files = self._read_output_dir(Path(output_dir))

                logger.info(
                    "sandbox_finished",
                    image=image,
                    script=script_path,
                    exit_code=exit_code,
                    output_files=list(output_files.keys()),
                )
                return SandboxResult(
                    exit_code=exit_code,
                    stdout=stdout,
                    stderr=stderr,
                    output_files=output_files,
                )

            except FileNotFoundError:
                raise RuntimeError(
                    "docker command not found — ensure Docker CLI is installed in the app container"
                )

    def _read_output_dir(self, output_dir: Path) -> dict[str, str]:
        files: dict[str, str] = {}
        if not output_dir.exists():
            return files
        for path in output_dir.iterdir():
            if path.is_file() and path.stat().st_size < 1_048_576:  # max 1 MB per file
                try:
                    files[path.name] = path.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    pass
        return files
