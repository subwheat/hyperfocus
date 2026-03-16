"""
Hyperfocus Terminal Service
===========================
Real PTY terminal with WebSocket communication.
Phase 1: Basic PTY support
Phase 2: Sandbox isolation, quotas, command allowlist
"""

import asyncio
import fcntl
import os
import pty
import select
import signal
import struct
import termios
from datetime import datetime, timezone
from typing import Callable, Optional

from sqlalchemy import select as sa_select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import settings
from ..models import TerminalSession, generate_uuid


class PTYProcess:
    """Wrapper for a PTY subprocess."""

    def __init__(
        self,
        session_id: str,
        shell: str = "/bin/bash",
        cols: int = 80,
        rows: int = 24,
    ):
        self.session_id = session_id
        self.shell = shell
        self.cols = cols
        self.rows = rows
        self.pid: Optional[int] = None
        self.fd: Optional[int] = None
        self._closed = False

    def spawn(self) -> int:
        """Spawn the PTY process."""
        # Create pseudo-terminal
        pid, fd = pty.fork()

        if pid == 0:
            # Child process
            env_file = "/data/workspaces/acp-lab/worktree/.env"
            if os.path.exists(env_file):
                with open(env_file) as f:
                    for line in f:
                        line = line.strip()
                        if line and not line.startswith("#") and "=" in line:
                            k, v = line.split("=", 1)
                            os.environ[k.strip()] = v.strip()
            os.execvp(self.shell, [self.shell])
        else:
            # Parent process
            self.pid = pid
            self.fd = fd

            # Set initial window size
            self.resize(self.cols, self.rows)

            # Make non-blocking
            flags = fcntl.fcntl(fd, fcntl.F_GETFL)
            fcntl.fcntl(fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)

            return pid

    def resize(self, cols: int, rows: int):
        """Resize the terminal window."""
        if self.fd is not None:
            self.cols = cols
            self.rows = rows
            # TIOCSWINSZ = Terminal IO Control Set Window Size
            winsize = struct.pack("HHHH", rows, cols, 0, 0)
            fcntl.ioctl(self.fd, termios.TIOCSWINSZ, winsize)

    def write(self, data: bytes):
        """Write data to the PTY."""
        if self.fd is not None and not self._closed:
            os.write(self.fd, data)

    def read(self, timeout: float = 0.1) -> Optional[bytes]:
        """Read data from the PTY (non-blocking)."""
        if self.fd is None or self._closed:
            return None

        try:
            readable, _, _ = select.select([self.fd], [], [], timeout)
            if readable:
                return os.read(self.fd, 4096)
        except (OSError, IOError):
            return None
        return None

    def is_alive(self) -> bool:
        """Check if the process is still running."""
        if self.pid is None:
            return False
        try:
            os.kill(self.pid, 0)
            return True
        except OSError:
            return False

    def terminate(self):
        """Terminate the PTY process."""
        self._closed = True
        if self.fd is not None:
            try:
                os.close(self.fd)
            except OSError:
                pass
            self.fd = None

        if self.pid is not None:
            try:
                os.kill(self.pid, signal.SIGTERM)
                # Give it a moment to terminate gracefully
                for _ in range(10):
                    try:
                        pid, status = os.waitpid(self.pid, os.WNOHANG)
                        if pid != 0:
                            break
                    except ChildProcessError:
                        break
                    asyncio.get_event_loop().run_until_complete(asyncio.sleep(0.1))
                else:
                    # Force kill if still running
                    os.kill(self.pid, signal.SIGKILL)
                    os.waitpid(self.pid, 0)
            except (OSError, ChildProcessError):
                pass
            self.pid = None

    def get_exit_code(self) -> Optional[int]:
        """Get exit code if process has terminated."""
        if self.pid is None:
            return None
        try:
            pid, status = os.waitpid(self.pid, os.WNOHANG)
            if pid != 0:
                if os.WIFEXITED(status):
                    return os.WEXITSTATUS(status)
                elif os.WIFSIGNALED(status):
                    return -os.WTERMSIG(status)
        except ChildProcessError:
            return 0
        return None


class TerminalService:
    """Service for terminal session management."""

    def __init__(self):
        self._sessions: dict[str, PTYProcess] = {}
        self._enabled = settings.terminal_enabled

    @property
    def enabled(self) -> bool:
        return self._enabled

    # ─────────────────────────────────────────────────────────────────────────
    # Session Management
    # ─────────────────────────────────────────────────────────────────────────

    async def create_session(
        self,
        db: AsyncSession,
        workspace_id: str,
        cols: int = 80,
        rows: int = 24,
    ) -> TerminalSession:
        """Create a new terminal session."""
        if not self._enabled:
            raise TerminalServiceError("Terminal service is disabled")

        # Check session limit
        active_count = len(
            [s for s in self._sessions.values() if s.is_alive()]
        )
        if active_count >= settings.terminal_max_sessions:
            raise TerminalServiceError(
                f"Maximum terminal sessions ({settings.terminal_max_sessions}) reached"
            )

        # Create session record
        session_id = generate_uuid()
        session = TerminalSession(
            id=session_id,
            workspace_id=workspace_id,
            is_active=True,
        )
        db.add(session)

        # Spawn PTY process
        pty_process = PTYProcess(
            session_id=session_id,
            shell=settings.terminal_shell,
            cols=cols,
            rows=rows,
        )
        pid = pty_process.spawn()

        # Update session with PID
        session.pid = pid
        await db.commit()
        await db.refresh(session)

        # Track session
        self._sessions[session_id] = pty_process

        return session

    async def get_session(
        self,
        db: AsyncSession,
        session_id: str,
        workspace_id: str,
    ) -> Optional[TerminalSession]:
        """Get terminal session by ID."""
        result = await db.execute(
            sa_select(TerminalSession).where(
                TerminalSession.id == session_id,
                TerminalSession.workspace_id == workspace_id,
                TerminalSession.is_active == True,
            )
        )
        return result.scalar_one_or_none()

    async def list_sessions(
        self,
        db: AsyncSession,
        workspace_id: str,
    ) -> list[TerminalSession]:
        """List active terminal sessions."""
        result = await db.execute(
            sa_select(TerminalSession).where(
                TerminalSession.workspace_id == workspace_id,
                TerminalSession.is_active == True,
            )
        )
        return list(result.scalars().all())

    async def close_session(
        self,
        db: AsyncSession,
        session_id: str,
        workspace_id: str,
    ) -> bool:
        """Close a terminal session."""
        session = await self.get_session(db, session_id, workspace_id)
        if not session:
            return False

        # Terminate PTY process
        if session_id in self._sessions:
            pty_process = self._sessions[session_id]
            exit_code = pty_process.get_exit_code()
            pty_process.terminate()
            del self._sessions[session_id]

            # Update session record
            session.is_active = False
            session.ended_at = datetime.now(timezone.utc)
            session.exit_code = exit_code
            await db.commit()

        return True

    # ─────────────────────────────────────────────────────────────────────────
    # PTY I/O
    # ─────────────────────────────────────────────────────────────────────────

    def get_pty(self, session_id: str) -> Optional[PTYProcess]:
        """Get PTY process for session."""
        return self._sessions.get(session_id)

    def write_to_session(self, session_id: str, data: bytes):
        """Write data to terminal session."""
        pty_process = self._sessions.get(session_id)
        if pty_process and pty_process.is_alive():
            pty_process.write(data)

    def read_from_session(
        self,
        session_id: str,
        timeout: float = 0.1,
    ) -> Optional[bytes]:
        """Read data from terminal session."""
        pty_process = self._sessions.get(session_id)
        if pty_process and pty_process.is_alive():
            return pty_process.read(timeout)
        return None

    def resize_session(self, session_id: str, cols: int, rows: int):
        """Resize terminal session."""
        pty_process = self._sessions.get(session_id)
        if pty_process:
            pty_process.resize(cols, rows)

    def is_session_alive(self, session_id: str) -> bool:
        """Check if session is still alive."""
        pty_process = self._sessions.get(session_id)
        return pty_process is not None and pty_process.is_alive()

    # ─────────────────────────────────────────────────────────────────────────
    # Cleanup
    # ─────────────────────────────────────────────────────────────────────────

    async def cleanup_dead_sessions(self, db: AsyncSession):
        """Clean up dead sessions."""
        dead_sessions = []
        for session_id, pty_process in list(self._sessions.items()):
            if not pty_process.is_alive():
                exit_code = pty_process.get_exit_code()
                pty_process.terminate()
                dead_sessions.append((session_id, exit_code))
                del self._sessions[session_id]

        # Update database
        for session_id, exit_code in dead_sessions:
            await db.execute(
                update(TerminalSession)
                .where(TerminalSession.id == session_id)
                .values(
                    is_active=False,
                    ended_at=datetime.now(timezone.utc),
                    exit_code=exit_code,
                )
            )
        await db.commit()

    async def shutdown(self):
        """Shutdown all terminal sessions."""
        for session_id, pty_process in list(self._sessions.items()):
            pty_process.terminate()
        self._sessions.clear()


class TerminalServiceError(Exception):
    """Exception for terminal service errors."""

    pass


# Singleton instance
terminal_service = TerminalService()
