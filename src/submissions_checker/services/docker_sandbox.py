"""Executes student code in an isolated Docker container sandbox."""

from __future__ import annotations

import asyncio
import os
import tempfile
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from submissions_checker.core.logging import get_logger

logger = get_logger(__name__)

_DEFAULT_MEMORY = "256m"
_DEFAULT_CPUS = 0.5
_DEFAULT_TIMEOUT = 30
MAX_OUTPUT_FILES = 1_000


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
            # Subject images drop to a non-root user (e.g. uid 10001), so the bind-mounted
            # /output (a host temp dir, created 0700) must be writable by that user for the
            # check to emit result.json. Widen perms on this ephemeral dir only.
            os.chmod(output_dir, 0o777)
            container_name = f"submission-check-{uuid.uuid4().hex}"
            cmd = [
                "docker",
                "run",
                "--rm",
                "--name",
                container_name,
                "--network",
                "none",
                f"--memory={memory}",
                f"--cpus={cpus}",
                "--pids-limit=100",
                # No Linux capabilities, and no way to regain any through setuid binaries
                # baked into a subject image: the checker only needs to read two mounts
                # and write /output.
                "--cap-drop=ALL",
                "--security-opt=no-new-privileges",
                "--read-only",
                "--tmpfs",
                "/tmp:rw,size=64m",
                "-v",
                f"{student_files_dir}:/submission:ro",
                "-v",
                f"{plugin_dir}:/plugin:ro",
                "-v",
                f"{output_dir}:/output:rw",
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
                except TimeoutError:
                    # proc.kill() only stops the local `docker run` CLI wrapper — the
                    # container itself keeps running under the daemon (--rm only removes
                    # it once it stops). Kill the named container directly so a hung
                    # script doesn't keep consuming CPU/memory past the timeout.
                    await self._kill_container(container_name)
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

            except FileNotFoundError as exc:
                raise RuntimeError(
                    "docker command not found — ensure Docker CLI is installed in the app container"
                ) from exc

    async def _kill_container(self, name: str) -> None:
        """Best-effort `docker kill` on a timed-out sandbox container. Swallow
        failures — the container may have already exited on its own right as the
        timeout fired, which is a harmless race, not an error."""
        try:
            kill_proc = await asyncio.create_subprocess_exec(
                "docker",
                "kill",
                name,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await kill_proc.communicate()
        except Exception as exc:
            logger.warning("sandbox_container_kill_failed", container=name, error=str(exc))

    def _read_output_dir(self, output_dir: Path) -> dict[str, str]:
        files: dict[str, str] = {}
        if not output_dir.exists():
            return files
        for path in output_dir.iterdir():
            if len(files) >= MAX_OUTPUT_FILES:
                break
            if path.is_file() and path.stat().st_size < 1_048_576:  # max 1 MB per file
                try:
                    files[path.name] = path.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    pass
        return files
