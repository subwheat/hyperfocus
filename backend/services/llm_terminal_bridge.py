"""
LLM Terminal Bridge — Code Execution for LLMs
Allows LLMs to execute commands in a controlled sandbox environment.
"""

import asyncio
import os
import re
import tempfile
import time
import shlex
import logging
from typing import Optional, Tuple
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

logger = logging.getLogger("LLM_TERMINAL_BRIDGE")

class CommandCategory(str, Enum):
    READ = "read"
    WRITE = "write"
    EXECUTE = "execute"
    SYSTEM = "system"

COMMAND_WHITELIST = {
    "cat": CommandCategory.READ, "head": CommandCategory.READ, "tail": CommandCategory.READ,
    "grep": CommandCategory.READ, "find": CommandCategory.READ, "ls": CommandCategory.READ,
    "pwd": CommandCategory.READ, "wc": CommandCategory.READ, "tree": CommandCategory.READ,
    "mkdir": CommandCategory.WRITE, "touch": CommandCategory.WRITE, "echo": CommandCategory.WRITE,
    "printf": CommandCategory.WRITE, "tee": CommandCategory.WRITE,
    "python": CommandCategory.EXECUTE, "python3": CommandCategory.EXECUTE,
    "pip": CommandCategory.READ, "date": CommandCategory.SYSTEM, "whoami": CommandCategory.SYSTEM,
    "df": CommandCategory.SYSTEM, "free": CommandCategory.SYSTEM, "nvidia-smi": CommandCategory.SYSTEM,
    "docker": CommandCategory.SYSTEM, "git": CommandCategory.READ, "curl": CommandCategory.READ,
}

BLOCKED_PATTERNS = [
    r"rm\s+-rf", r"sudo", r"su\s+-", r"eval\s+", r"exec\s+", r"\.env\b",
    r"API_KEY", r"SECRET", r"PASSWORD", r"\|\s*sh", r"\|\s*bash", r"wget",
]

ALLOWED_PATHS = ["/app", "/data", "/tmp/llm_sandbox", "/home/ubuntu"]

@dataclass
class ExecutionResult:
    success: bool
    stdout: str
    stderr: str
    return_code: int
    command: str
    execution_time: float
    truncated: bool = False
    blocked_reason: Optional[str] = None

class LLMTerminalBridge:
    def _safe_working_dir(self, working_dir: str) -> str:
        wd = os.path.abspath(working_dir or "/app")
        for allowed in ALLOWED_PATHS:
            ap = os.path.abspath(allowed)
            if wd == ap or wd.startswith(ap + os.sep):
                return wd
        return "/app"

    def __init__(self, timeout: int = 30, max_output: int = 10000, working_dir: str = "/app"):
        self.timeout = timeout
        self.max_output = max_output
        self.working_dir = self._safe_working_dir(working_dir)
        Path("/tmp/llm_sandbox").mkdir(exist_ok=True)
    
    def validate_command(self, command: str) -> Tuple[bool, Optional[str]]:
        for pattern in BLOCKED_PATTERNS:
            if re.search(pattern, command, re.IGNORECASE):
                return False, f"Blocked pattern: {pattern}"
        try:
            parts = shlex.split(command)
            if not parts:
                return False, "Empty command"
            base_cmd = parts[0].split("/")[-1]
        except ValueError as e:
            return False, f"Invalid syntax: {e}"
        if base_cmd not in COMMAND_WHITELIST:
            return False, f"Command not allowed: {base_cmd}"
        if base_cmd == "docker" and (len(parts) < 2 or parts[1] not in ["ps", "logs", "images"]):
            return False, "Docker: only ps/logs/images allowed"
        if base_cmd == "git" and (len(parts) < 2 or parts[1] not in ["status", "log", "diff", "branch"]):
            return False, "Git: only status/log/diff/branch allowed"
        if base_cmd == "pip" and (len(parts) < 2 or parts[1] not in ["list", "show", "freeze"]):
            return False, "Pip: only list/show/freeze allowed"
        return True, None
    
    async def execute(self, command: str) -> ExecutionResult:
        start_time = time.time()
        is_valid, reason = self.validate_command(command)
        if not is_valid:
            return ExecutionResult(False, "", "", -1, command, 0, blocked_reason=reason)
        try:
            process = await asyncio.create_subprocess_shell(
                command, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                cwd=self.working_dir, env={**os.environ, "HOME": "/tmp/llm_sandbox"}
            )
            try:
                stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=self.timeout)
            except asyncio.TimeoutError:
                process.kill()
                return ExecutionResult(False, "", f"Timeout after {self.timeout}s", -1, command, self.timeout, blocked_reason="Timeout")
            stdout_str = stdout.decode("utf-8", errors="replace")
            stderr_str = stderr.decode("utf-8", errors="replace")
            truncated = False
            if len(stdout_str) > self.max_output:
                stdout_str = stdout_str[:self.max_output] + "\n...[truncated]"
                truncated = True
            return ExecutionResult(process.returncode == 0, stdout_str, stderr_str, process.returncode, command, time.time() - start_time, truncated)
        except Exception as e:
            return ExecutionResult(False, "", str(e), -1, command, time.time() - start_time, blocked_reason=str(e))
    
    async def execute_python(self, code: str) -> ExecutionResult:
        with tempfile.NamedTemporaryFile(mode="w", suffix=".py", dir="/tmp/llm_sandbox", delete=False) as f:
            f.write(code)
            script_path = f.name
        try:
            result = await self.execute(f"python3 {script_path}")
            result.command = f"python3 <<CODE>>\n{code[:200]}...\n<<CODE>>"
            return result
        finally:
            try: os.unlink(script_path)
            except: pass
    
    def format_for_llm(self, result: ExecutionResult) -> str:
        if result.blocked_reason:
            return f"⛔ Blocked: {result.blocked_reason}\nCommand: {result.command}"
        status = "✅" if result.success else "❌"
        out = f"{status} {result.command}\nExit: {result.return_code} | Time: {result.execution_time:.2f}s"
        if result.stdout.strip():
            out += f"\n\n```\n{result.stdout.strip()}\n```"
        if result.stderr.strip():
            out += f"\n\n⚠️ Stderr:\n```\n{result.stderr.strip()}\n```"
        return out

# FastAPI Router
from fastapi import APIRouter
from pydantic import BaseModel

router = APIRouter(prefix="/api/terminal-bridge", tags=["terminal-bridge"])
_bridge = None

def get_bridge():
    global _bridge
    if _bridge is None:
        _bridge = LLMTerminalBridge()
    return _bridge

class ExecRequest(BaseModel):
    command: str
    working_dir: Optional[str] = "/app"

class PythonRequest(BaseModel):
    code: str
    working_dir: Optional[str] = "/app"

@router.post("/execute")
async def execute_command(req: ExecRequest):
    bridge = LLMTerminalBridge(working_dir=req.working_dir or "/app")
    result = await bridge.execute(req.command)
    return {"success": result.success, "output": result.stdout, "stderr": result.stderr, 
            "return_code": result.return_code, "time": result.execution_time,
            "blocked": result.blocked_reason, "formatted": bridge.format_for_llm(result)}

@router.post("/execute-python")
async def execute_python(req: PythonRequest):
    bridge = LLMTerminalBridge(working_dir=req.working_dir or "/app")
    result = await bridge.execute_python(req.code)
    return {"success": result.success, "output": result.stdout, "stderr": result.stderr,
            "return_code": result.return_code, "formatted": bridge.format_for_llm(result)}

@router.get("/allowed")
async def allowed_commands():
    return {"commands": list(COMMAND_WHITELIST.keys()), "paths": ALLOWED_PATHS}
