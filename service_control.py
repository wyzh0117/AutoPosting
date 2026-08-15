#!/usr/bin/env python3
"""Small macOS Tk controller for the local AutoPosting web service."""

from __future__ import annotations

import argparse
import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.request
import webbrowser
from pathlib import Path
from typing import Callable

import tkinter as tk


PROJECT_ROOT = Path(__file__).resolve().parent
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8080


class ServiceController:
    """Start/stop only an AutoPosting web process on the configured port."""

    def __init__(
        self,
        project_root: str | Path = PROJECT_ROOT,
        host: str = DEFAULT_HOST,
        port: int = DEFAULT_PORT,
        python_executable: str = sys.executable,
        browser_opener: Callable[[str], object] = webbrowser.open,
    ) -> None:
        self.project_root = Path(project_root).resolve()
        self.host = host
        self.port = port
        self.python_executable = python_executable
        self.browser_opener = browser_opener
        self.process: subprocess.Popen[bytes] | None = None
        self.open_browser_when_ready = False
        self._browser_opened = False

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}/"

    @property
    def log_path(self) -> Path:
        return self.project_root / "output" / "service-control.log"

    def healthy(self) -> bool:
        request = urllib.request.Request(
            f"http://{self.host}:{self.port}/api/status",
            headers={"Accept": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=0.8) as response:
                if response.status != 200:
                    return False
                payload = json.loads(response.read().decode("utf-8"))
        except (OSError, ValueError, json.JSONDecodeError):
            return False
        return (
            isinstance(payload, dict)
            and payload.get("ok") is True
            and payload.get("service") == "autoposting"
        )

    def _listening_pids(self) -> list[int]:
        result = subprocess.run(
            ["lsof", f"-tiTCP:{self.port}", "-sTCP:LISTEN"],
            capture_output=True,
            text=True,
            check=False,
        )
        pids: list[int] = []
        for line in result.stdout.splitlines():
            try:
                pids.append(int(line.strip()))
            except ValueError:
                continue
        return pids

    @staticmethod
    def _command_for_pid(pid: int) -> str:
        result = subprocess.run(
            ["ps", "-p", str(pid), "-o", "command="],
            capture_output=True,
            text=True,
            check=False,
        )
        return result.stdout.strip()

    @staticmethod
    def _cwd_for_pid(pid: int) -> Path | None:
        result = subprocess.run(
            ["lsof", "-a", "-p", str(pid), "-d", "cwd", "-Fn"],
            capture_output=True,
            text=True,
            check=False,
        )
        for line in result.stdout.splitlines():
            if line.startswith("n"):
                return Path(line[1:]).resolve()
        return None

    def _is_autoposting_command(self, command: str) -> bool:
        service_command = "autoposting.cli web" in command or "autoposting web" in command
        return service_command and f"--port {self.port}" in command

    def _is_managed_pid(self, pid: int) -> bool:
        return (
            self._is_autoposting_command(self._command_for_pid(pid))
            and self._cwd_for_pid(pid) == self.project_root
        )

    def _managed_pids(self) -> list[int]:
        return [
            pid
            for pid in self._listening_pids()
            if self._is_managed_pid(pid)
        ]

    def _occupied_by_other_process(self) -> str | None:
        for pid in self._listening_pids():
            if not self._is_managed_pid(pid):
                return f"端口 {self.port} 已被其他进程占用（PID {pid}）"
        return None

    def start(self, open_browser: bool = True) -> str:
        if self.healthy():
            if not self._managed_pids():
                raise RuntimeError(f"端口 {self.port} 上运行的不是本项目服务")
            if open_browser:
                self._open_browser()
            return "already_running"

        occupied = self._occupied_by_other_process()
        if occupied:
            raise RuntimeError(occupied)

        managed = self._managed_pids()
        if managed:
            self.open_browser_when_ready = open_browser
            return "starting"

        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        environment = os.environ.copy()
        source_path = str(self.project_root / "src")
        old_pythonpath = environment.get("PYTHONPATH")
        environment["PYTHONPATH"] = (
            source_path
            if not old_pythonpath
            else os.pathsep.join((source_path, old_pythonpath))
        )
        command = [
            self.python_executable,
            "-m",
            "autoposting.cli",
            "web",
            "--db",
            str(self.project_root / "data" / "autoposting.db"),
            "--sources",
            str(self.project_root / "config" / "sources.example.json"),
            "--packages",
            str(self.project_root / "output" / "packages"),
            "--host",
            self.host,
            "--port",
            str(self.port),
        ]
        log = self.log_path.open("ab")
        try:
            self.process = subprocess.Popen(
                command,
                cwd=self.project_root,
                env=environment,
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        finally:
            log.close()
        self.open_browser_when_ready = open_browser
        self._browser_opened = False
        return "starting"

    def _open_browser(self) -> None:
        if self._browser_opened:
            return
        self.browser_opener(self.url)
        self._browser_opened = True

    def poll(self) -> str:
        if self.healthy():
            if not self._managed_pids():
                return "occupied"
            if self.open_browser_when_ready:
                self._open_browser()
                self.open_browser_when_ready = False
            return "running"
        if self.process is not None and self.process.poll() is not None:
            return "failed"
        if self._managed_pids():
            return "starting"
        if self._listening_pids():
            return "occupied"
        return "stopped"

    def stop(self) -> bool:
        pids = self._managed_pids()
        if self.process is not None and self.process.poll() is None:
            if self.process.pid not in pids:
                pids.append(self.process.pid)
        if not pids:
            return False

        for pid in sorted(set(pids)):
            if (self.process is None or pid != self.process.pid) and not self._is_managed_pid(pid):
                continue
            try:
                if self.process is not None and pid == self.process.pid:
                    self.process.terminate()
                else:
                    os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                continue

        if self.process is not None and self.process.pid in pids:
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and any(
            self._pid_exists(pid) for pid in pids if self.process is None or pid != self.process.pid
        ):
            time.sleep(0.1)
        for pid in pids:
            if self._pid_exists(pid) and (
                (self.process is not None and pid == self.process.pid)
                or self._is_managed_pid(pid)
            ):
                # ponytail: bounded graceful wait, then kill only a verified
                # AutoPosting PID; a service supervisor can restart it later.
                try:
                    os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
        if self.process is not None and self.process.poll() is None:
            try:
                self.process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                pass
        return True

    @staticmethod
    def _pid_exists(pid: int) -> bool:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True

    def wait_until_healthy(self, timeout: float = 20) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.healthy():
                return True
            time.sleep(0.2)
        return False

    def wait_until_down(self, timeout: float = 5) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not self.healthy():
                return True
            time.sleep(0.1)
        return False


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind((DEFAULT_HOST, 0))
        return int(sock.getsockname()[1])


class ServiceControlWindow:
    def __init__(self, controller: ServiceController) -> None:
        self.controller = controller
        self.root = tk.Tk()
        self.root.title("AutoPosting 服务控制")
        self.root.geometry("460x250")
        self.root.minsize(400, 220)
        self.root.protocol("WM_DELETE_WINDOW", self.close)

        self.status_var = tk.StringVar()
        self.detail_var = tk.StringVar(value=f"服务地址：{controller.url}")
        self.toggle_button = tk.Button(
            self.root,
            text="启动服务",
            command=self.toggle,
            width=18,
            height=2,
            font=("Arial", 15, "bold"),
        )
        self.toggle_button.pack(pady=(28, 16))
        tk.Label(self.root, textvariable=self.status_var, font=("Arial", 13)).pack()
        tk.Label(self.root, textvariable=self.detail_var, fg="#555555").pack(pady=(8, 0))
        tk.Label(
            self.root,
            text="启动成功后会自动打开浏览器；关闭窗口不会停止服务。",
            fg="#777777",
            wraplength=410,
        ).pack(pady=(16, 0))
        self.busy = False
        self.refresh()

    def refresh(self) -> None:
        state = self.controller.poll()
        if state == "running":
            self.status_var.set("运行中")
            self.toggle_button.configure(text="停止服务", state=tk.NORMAL, bg="#ffe5e5")
        elif state == "starting":
            self.status_var.set("启动中…")
            self.toggle_button.configure(text="启动中…", state=tk.DISABLED, bg="#eeeeee")
        elif state == "failed":
            self.status_var.set("启动失败，请查看 output/service-control.log")
            self.toggle_button.configure(text="启动服务", state=tk.NORMAL, bg="#e5f1ff")
        elif state == "occupied":
            self.status_var.set(f"端口 {self.controller.port} 已被其他程序占用")
            self.toggle_button.configure(text="不可控制", state=tk.DISABLED, bg="#eeeeee")
        else:
            self.status_var.set("已停止")
            self.toggle_button.configure(text="启动服务", state=tk.NORMAL, bg="#e5f1ff")
        self.root.after(1000, self.refresh)

    def toggle(self) -> None:
        if self.busy:
            return
        state = self.controller.poll()
        if state == "running":
            self.busy = True
            self.toggle_button.configure(text="停止中…", state=tk.DISABLED)
            threading.Thread(target=self._stop_worker, daemon=True).start()
            return
        try:
            self.controller.start(open_browser=True)
            self.status_var.set("启动中…")
        except (OSError, RuntimeError) as exc:
            self.status_var.set(str(exc))

    def _stop_worker(self) -> None:
        try:
            self.controller.stop()
        finally:
            self.root.after(0, self._stop_finished)

    def _stop_finished(self) -> None:
        self.busy = False
        self.status_var.set("已停止")
        self.refresh()

    def close(self) -> None:
        self.root.destroy()

    def run(self) -> None:
        self.root.mainloop()


def run_self_check(port: int = 0) -> None:
    test_port = port or _free_port()
    opened: list[str] = []
    controller = ServiceController(port=test_port, browser_opener=opened.append)
    assert not controller.healthy(), "test port is unexpectedly healthy"
    assert controller.start(open_browser=True) == "starting"
    assert controller.wait_until_healthy(), "service did not start"
    assert controller.poll() == "running"
    assert opened == [controller.url], "browser was not opened exactly once"
    assert controller.poll() == "running"
    assert opened == [controller.url], "browser was opened more than once"
    assert controller.stop(), "service did not report a managed process"
    assert controller.wait_until_down(), "service did not stop"
    print(f"service-control self-check passed on port {test_port}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Control the local AutoPosting web service")
    parser.add_argument("--port", type=int)
    parser.add_argument("--self-check", action="store_true")
    args = parser.parse_args()
    if args.self_check:
        run_self_check(args.port or 0)
        return 0
    ServiceControlWindow(ServiceController(port=args.port or DEFAULT_PORT)).run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
