"""Isolated business process using ASGI in memory, with no listening HTTP port."""
from __future__ import annotations

import multiprocessing as mp
import asyncio
import os
import shutil
import threading
from contextlib import ExitStack
from pathlib import Path
from typing import Any
from .process_job import ProcessJob


class RuntimeErrorDetail(RuntimeError):
    pass


class ASGIGateway:
    """Public HTTPX transport, explicitly owned loop, no test-server sockets."""
    def __init__(self, application):
        import httpx
        self.loop = asyncio.new_event_loop()
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=application, raise_app_exceptions=False),
                                        base_url="http://desktop.internal", trust_env=False)

    def __enter__(self):
        return self

    def request(self, method, path, **options):
        return self.loop.run_until_complete(self.client.request(method, path, **options))

    def __exit__(self, *exc):
        self.loop.run_until_complete(self.client.aclose())
        self.loop.run_until_complete(self.loop.shutdown_asyncgens())
        self.loop.close()


def backend_main(connection, data_directory: str) -> None:
    # Set data and security policy BEFORE importing app.py: that module owns
    # SQLite and task-pool globals. Keeping it in a child process prevents UI
    # imports from initializing/mutating a user's database unexpectedly.
    os.environ["TTS_APP_DATA_DIR"] = data_directory
    os.environ["TTS_DESKTOP_PROTECT_SETTINGS"] = "1" if os.name == "nt" else "0"
    os.environ.pop("TTS_APP_API_TOKEN", None)
    application = None
    try:
        import app as application
        # app.py currently has no ASGI lifespan handlers; initialization is
        # explicitly owned above. If handlers are introduced, drive lifespan
        # here too: HTTPX intentionally does not send startup/shutdown events.
        with ASGIGateway(application.app) as client:
            connection.send({"ok": True, "result": {"ready": True}})
            while True:
                message = connection.recv()
                if message.get("op") == "close":
                    break
                try:
                    result = dispatch(client, application, message)
                    connection.send({"ok": True, "result": result})
                except Exception as exc:
                    # Never echo request bodies or generic exception strings:
                    # validation failures may include the user's secret input.
                    error = str(exc) if isinstance(exc, RuntimeErrorDetail) else "操作失败，请检查数据、依赖或供应商状态"
                    connection.send({"ok": False, "error": error})
    except (EOFError, BrokenPipeError):
        pass
    except Exception:
        try:
            connection.send({"ok": False, "error": "业务服务启动失败，请检查数据目录和凭据所属 Windows 用户"})
        except (EOFError, OSError):
            pass
    finally:
        if application is not None:
            # Do not wait indefinitely for cloud I/O during window close. The
            # owner gives the process a grace period, then terminates it. On
            # next startup app.py honestly marks unfinished tasks interrupted.
            application.executor.shutdown(wait=False, cancel_futures=True)
        connection.close()


def dispatch(client, application, message: dict[str, Any]) -> Any:
    if message.get("op") == "copy":
        kind, identifier = message["kind"], message["id"]
        readers = {"samples": application.sample_audio, "noises": application.noise_audio,
                   "exports": application.download_export}
        if kind not in readers:
            raise RuntimeErrorDetail("不支持的文件类型")
        response = readers[kind](identifier)
        destination = Path(message["destination"])
        if destination.resolve().is_relative_to(application.DATA_ROOT):
            raise RuntimeErrorDetail("另存路径不能位于应用数据目录内，以免覆盖源数据")
        destination.parent.mkdir(parents=True, exist_ok=True)
        # API readers validate database provenance and confinement. Copy, never
        # move, so saving an export or playing audio cannot mutate source data.
        import tempfile
        descriptor, temporary = tempfile.mkstemp(prefix=".tts-save-", dir=destination.parent)
        os.close(descriptor)
        try:
            shutil.copyfile(response.path, temporary)
            os.replace(temporary, destination)
        finally:
            Path(temporary).unlink(missing_ok=True)
        return str(destination)
    with ExitStack() as stack:
        options: dict[str, Any] = {}
        if "json" in message:
            options["json"] = message["json"]
        if "params" in message:
            options["params"] = message["params"]
        if "data" in message:
            options["data"] = message["data"]
        if "files" in message:
            options["files"] = [
                (field, (Path(filename).name, stack.enter_context(open(filename, "rb")), "application/octet-stream"))
                for field, filename in message["files"]
            ]
        response = client.request(message.get("method", "GET"), message["path"], **options)
        if response.status_code >= 400:
            try:
                detail = response.json().get("detail")
            except ValueError:
                detail = None
            # Pydantic error lists include 'input'; return only their message.
            if isinstance(detail, list):
                detail = "；".join(str(item.get("msg", "参数无效")) for item in detail)
            if not isinstance(detail, str):
                detail = f"操作失败（{response.status_code}）"
            raise RuntimeErrorDetail(detail[:1000])
        return response.json()


class BusinessRuntime:
    """One owner, one pipe, serialized commands; all requests run off UI thread."""
    def __init__(self, data_directory: Path):
        self.data_directory = data_directory.resolve()
        self._lock = threading.Lock()
        self._closed = threading.Event()
        self._ready_result = None
        context = mp.get_context("spawn")
        self.connection, child = context.Pipe()
        self.process = context.Process(target=backend_main, args=(child, str(self.data_directory)), daemon=True)
        self.process.start()
        child.close()
        try:
            self.job = ProcessJob(self.process.pid)
        except Exception:
            self.process.terminate()
            self.process.join(3)
            self.connection.close()
            raise

    def ready(self, timeout: float = 45) -> dict:
        with self._lock:
            if self._ready_result is None:
                self._ready_result = self._receive(timeout)
            return self._ready_result

    def _receive(self, timeout: float) -> Any:
        if not self.connection.poll(timeout):
            # A timed-out command may still be executing. Retrying it would
            # risk duplicate paid requests; make this runtime unusable instead.
            self._closed.set()
            self.process.terminate()
            self.job.close()
            raise RuntimeErrorDetail("操作超时，服务已停止；请重启应用并检查任务结果，不要盲目重试")
        result = self.connection.recv()
        if not result.get("ok"):
            raise RuntimeErrorDetail(result.get("error", "业务服务不可用"))
        return result["result"]

    def call(self, path: str = "", method: str = "GET", **options) -> Any:
        with self._lock:
            if self._closed.is_set() or not self.process.is_alive():
                raise RuntimeErrorDetail("业务服务已停止，请重启应用")
            try:
                self.connection.send({"path": path, "method": method, **options})
                return self._receive(300)
            except (EOFError, OSError) as exc:
                raise RuntimeErrorDetail("业务服务连接已中断，请重启应用") from exc

    def close(self) -> None:
        if self._closed.is_set():
            if self.process.is_alive():
                self.process.terminate()
        else:
            self._closed.set()
            # Do not acquire the command lock in the UI shutdown path: an
            # upload/catalog call can own it while waiting for external I/O.
            if self._lock.acquire(blocking=False):
                try:
                    if self.process.is_alive():
                        self.connection.send({"op": "close"})
                except OSError:
                    pass
                finally:
                    self._lock.release()
            self.process.join(2)
            if self.process.is_alive():
                self.process.terminate()
        self.job.close()
        self.process.join(3)
        self.connection.close()
