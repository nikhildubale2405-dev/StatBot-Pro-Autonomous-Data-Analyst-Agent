from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from uuid import uuid4

import docker
from docker.errors import DockerException
from docker.types import Mount

from app.core.config import get_settings


class SandboxUnavailableError(RuntimeError):
    pass


class DockerSandboxRunner:
    def __init__(self) -> None:
        self.settings = get_settings()

    def run(self, stored_file_name: str, code: str) -> dict:
        run_id = str(uuid4())
        run_dir = self.settings.output_dir / run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        script_path = run_dir / "analysis.py"
        script_path.write_text(code, encoding="utf-8")

        started = time.monotonic()
        container = None
        try:
            client = docker.from_env()
            container = client.containers.run(
                self.settings.sandbox_image,
                command=self._command(run_id, stored_file_name),
                detach=True,
                network_disabled=True,
                read_only=True,
                mem_limit=self.settings.sandbox_memory_limit,
                nano_cpus=self.settings.sandbox_nano_cpus,
                pids_limit=128,
                security_opt=["no-new-privileges:true"],
                cap_drop=["ALL"],
                environment={"MPLCONFIGDIR": "/outputs/.mplconfig", "TMPDIR": "/outputs/.tmp"},
                mounts=self._mounts(),
                working_dir="/outputs",
            )
            try:
                result = container.wait(timeout=self.settings.sandbox_timeout_seconds)
            except Exception:
                container.kill()
                return {
                    "success": False,
                    "stdout": "",
                    "stderr": "Sandbox execution timed out.",
                    "error": "Sandbox execution timed out.",
                    "tables": [],
                    "charts": [],
                    "execution_time": round(time.monotonic() - started, 4),
                }

            logs = container.logs(stdout=True, stderr=True).decode("utf-8", errors="replace")
            parsed = self._parse_json_log(logs)
            parsed["execution_time"] = round(time.monotonic() - started, 4)
            if result.get("StatusCode", 1) != 0 and parsed.get("success") is not False:
                parsed["success"] = False
                parsed["stderr"] = logs
                parsed["error"] = f"Sandbox exited with status {result.get('StatusCode')}"
            return parsed
        except DockerException as exc:
            raise SandboxUnavailableError(f"Docker sandbox is unavailable: {exc}") from exc
        finally:
            if container is not None:
                try:
                    container.remove(force=True)
                except Exception:
                    pass

    def _command(self, run_id: str, stored_file_name: str) -> list[str]:
        return [
            "python",
            "/opt/statbot/runner.py",
            "--script",
            f"/outputs/{run_id}/analysis.py",
            "--input-file",
            f"/uploads/{stored_file_name}",
            "--output-dir",
            f"/outputs/{run_id}",
        ]

    def _mounts(self) -> list[Mount]:
        if self.settings.sandbox_mode == "volume":
            return [
                Mount(target="/uploads", source=self.settings.sandbox_uploads_volume, type="volume", read_only=True),
                Mount(target="/outputs", source=self.settings.sandbox_outputs_volume, type="volume", read_only=False),
            ]

        return [
            Mount(target="/uploads", source=str(self.settings.upload_dir.resolve()), type="bind", read_only=True),
            Mount(target="/outputs", source=str(self.settings.output_dir.resolve()), type="bind", read_only=False),
        ]

    @staticmethod
    def _parse_json_log(logs: str) -> dict:
        for line in reversed([line.strip() for line in logs.splitlines() if line.strip()]):
            try:
                return json.loads(line)
            except json.JSONDecodeError:
                continue
        return {
            "success": False,
            "stdout": logs,
            "stderr": "Sandbox did not return structured JSON.",
            "error": "Sandbox did not return structured JSON.",
            "tables": [],
            "charts": [],
        }


class LocalSandboxRunner:
    """Single-container fallback for platforms that do not expose Docker."""

    def __init__(self) -> None:
        self.settings = get_settings()

    def run(self, stored_file_name: str, code: str) -> dict:
        run_id = str(uuid4())
        run_dir = self.settings.output_dir / run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        script_path = run_dir / "analysis.py"
        script_path.write_text(code, encoding="utf-8")

        runner_path = self._runner_path()
        input_path = self.settings.upload_dir / stored_file_name
        started = time.monotonic()
        env = os.environ.copy()
        env.update(
            {
                "MPLBACKEND": "Agg",
                "MPLCONFIGDIR": str(run_dir / ".mplconfig"),
                "TMPDIR": str(run_dir / ".tmp"),
            }
        )

        try:
            completed = subprocess.run(
                [
                    sys.executable,
                    str(runner_path),
                    "--script",
                    str(script_path),
                    "--input-file",
                    str(input_path),
                    "--output-dir",
                    str(run_dir),
                ],
                cwd=str(run_dir),
                capture_output=True,
                check=False,
                env=env,
                text=True,
                timeout=self.settings.sandbox_timeout_seconds,
            )
        except subprocess.TimeoutExpired:
            return {
                "success": False,
                "stdout": "",
                "stderr": "Sandbox execution timed out.",
                "error": "Sandbox execution timed out.",
                "tables": [],
                "charts": [],
                "execution_time": round(time.monotonic() - started, 4),
            }

        logs = "\n".join(part for part in [completed.stdout, completed.stderr] if part)
        parsed = DockerSandboxRunner._parse_json_log(logs)
        parsed["execution_time"] = round(time.monotonic() - started, 4)
        if completed.returncode != 0 and parsed.get("success") is not False:
            parsed["success"] = False
            parsed["stderr"] = logs
            parsed["error"] = f"Sandbox exited with status {completed.returncode}"
        return parsed

    def _runner_path(self) -> Path:
        configured = self.settings.sandbox_local_runner_path
        candidates = [
            configured,
            Path.cwd() / configured,
            Path.cwd().parent / configured,
            Path(__file__).resolve().parents[3] / configured,
        ]
        for candidate in candidates:
            resolved = candidate.resolve()
            if resolved.exists():
                return resolved
        raise SandboxUnavailableError(f"Local sandbox runner was not found: {configured}")


def get_sandbox_runner() -> DockerSandboxRunner | LocalSandboxRunner:
    settings = get_settings()
    if settings.sandbox_mode == "local":
        return LocalSandboxRunner()
    return DockerSandboxRunner()
