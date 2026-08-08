"""Optional system-browser checks for a standalone ARC reader."""

from __future__ import annotations

import base64
from dataclasses import dataclass
import json
import os
from pathlib import Path
import shutil
import socket
import struct
import subprocess
import tempfile
import time
from typing import Any, Final
from urllib.parse import urlsplit
from urllib.request import urlopen

from .html import HTMLRenderError


_BROWSER_CANDIDATES: Final = (
    "chromium",
    "chromium-browser",
    "google-chrome",
    "google-chrome-stable",
    "chrome",
)


@dataclass(frozen=True)
class BrowserValidation:
    """Successful optional browser validation details."""

    executable: str
    timeout_seconds: int


def validate_reader_in_browser(
    html_path: str | Path,
    *,
    browser_executable: str | None = None,
    timeout_seconds: int = 60,
) -> BrowserValidation:
    """Check one standalone reader with a locally installed Chromium browser."""

    if timeout_seconds <= 0:
        raise HTMLRenderError("browser validation timeout must be positive")
    source = Path(html_path).resolve()
    if not source.is_file():
        raise HTMLRenderError("standalone HTML is unreadable")
    executable = _browser_executable(browser_executable)
    report, exceptions = _reader_report(executable, source, timeout_seconds)
    _raise_for_report(report, exceptions)
    return BrowserValidation(executable=executable, timeout_seconds=timeout_seconds)


def _browser_executable(requested: str | None) -> str:
    if requested:
        resolved = shutil.which(requested)
        if resolved is None and Path(requested).is_file():
            resolved = requested
        if resolved is None:
            raise HTMLRenderError(
                f"browser executable is unavailable: {requested}"
            )
        return resolved
    for candidate in _BROWSER_CANDIDATES:
        resolved = shutil.which(candidate)
        if resolved is not None:
            return resolved
    raise HTMLRenderError(
        "browser validation requires a local Chromium-family executable"
    )


def _reader_report(
    executable: str, html_path: Path, timeout_seconds: int
) -> tuple[dict[str, object], tuple[str, ...]]:
    deadline = time.monotonic() + timeout_seconds
    with tempfile.TemporaryDirectory(prefix=".arc-render-browser-") as raw:
        profile = Path(raw) / "profile"
        command = [
            executable,
            "--headless",
            "--no-sandbox",
            "--disable-gpu",
            "--no-first-run",
            "--no-default-browser-check",
            "--remote-debugging-address=127.0.0.1",
            "--remote-debugging-port=0",
            f"--user-data-dir={profile}",
            html_path.as_uri(),
        ]
        try:
            process = subprocess.Popen(
                command,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except OSError as exc:
            raise HTMLRenderError("browser validation could not start") from exc
        try:
            port = _wait_for_debug_port(profile / "DevToolsActivePort", deadline)
            websocket_url = _page_websocket_url(
                port, deadline, html_path.as_uri()
            )
            with _CdpSocket(websocket_url, deadline) as cdp:
                cdp.call("Runtime.enable", {})
                response = cdp.call(
                    "Runtime.evaluate",
                    {
                        "expression": _READER_REPORT_EXPRESSION,
                        "awaitPromise": True,
                        "returnByValue": True,
                    },
                )
                result = response.get("result", {})
                value = result.get("result", {}).get("value")
                if not isinstance(value, dict):
                    detail = result.get("exceptionDetails")
                    raise HTMLRenderError(
                        "browser validation could not evaluate reader state"
                        + (f": {detail}" if detail else "")
                    )
                return dict(value), tuple(cdp.exceptions)
        except TimeoutError as exc:
            raise HTMLRenderError(
                f"browser validation timed out after {timeout_seconds}s"
            ) from exc
        except OSError as exc:
            raise HTMLRenderError("browser validation could not start") from exc
        finally:
            _stop_browser(process)


def _wait_for_debug_port(path: Path, deadline: float) -> int:
    while time.monotonic() < deadline:
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
            if lines:
                return int(lines[0])
        except (OSError, ValueError):
            pass
        time.sleep(0.05)
    raise TimeoutError("DevTools port was not ready")


def _page_websocket_url(
    port: int, deadline: float, expected_url: str
) -> str:
    endpoint = f"http://127.0.0.1:{port}/json/list"
    while time.monotonic() < deadline:
        try:
            with urlopen(endpoint, timeout=_remaining(deadline)) as response:
                pages = json.loads(response.read().decode("utf-8"))
            for page in pages:
                if (
                    page.get("type") == "page"
                    and page.get("url") == expected_url
                    and page.get("webSocketDebuggerUrl")
                ):
                    return str(page["webSocketDebuggerUrl"])
        except (OSError, ValueError, TimeoutError):
            pass
        time.sleep(0.05)
    raise TimeoutError("browser page was not ready")


class _CdpSocket:
    def __init__(self, websocket_url: str, deadline: float) -> None:
        self._deadline = deadline
        self._socket = _websocket_connect(websocket_url, deadline)
        self._next_id = 1
        self.exceptions: list[str] = []

    def __enter__(self) -> _CdpSocket:
        return self

    def __exit__(self, *_args: object) -> None:
        self._socket.close()

    def call(self, method: str, params: dict[str, object]) -> dict[str, Any]:
        identifier = self._next_id
        self._next_id += 1
        _websocket_send(self._socket, json.dumps({
            "id": identifier, "method": method, "params": params
        }))
        while True:
            payload = _websocket_receive(self._socket, self._deadline)
            message = json.loads(payload)
            if message.get("method") == "Runtime.exceptionThrown":
                details = message.get("params", {}).get("exceptionDetails", {})
                text = details.get("text") or details.get("exception", {}).get("description")
                self.exceptions.append(str(text or "runtime exception"))
            if message.get("id") == identifier:
                return message


def _websocket_connect(url: str, deadline: float) -> socket.socket:
    parsed = urlsplit(url)
    if parsed.scheme != "ws" or not parsed.hostname or not parsed.port:
        raise OSError("browser supplied an invalid DevTools socket")
    connection = socket.create_connection(
        (parsed.hostname, parsed.port), timeout=_remaining(deadline)
    )
    key = base64.b64encode(os.urandom(16)).decode("ascii")
    target = (parsed.path or "/") + (f"?{parsed.query}" if parsed.query else "")
    request = (
        f"GET {target} HTTP/1.1\r\n"
        f"Host: {parsed.hostname}:{parsed.port}\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        f"Sec-WebSocket-Key: {key}\r\n"
        "Sec-WebSocket-Version: 13\r\n\r\n"
    ).encode("ascii")
    connection.sendall(request)
    response = _read_until(connection, b"\r\n\r\n", deadline)
    if not response.startswith(b"HTTP/1.1 101"):
        connection.close()
        raise OSError("browser rejected DevTools socket")
    return connection


def _websocket_send(connection: socket.socket, value: str) -> None:
    payload = value.encode("utf-8")
    mask = os.urandom(4)
    size = len(payload)
    if size < 126:
        header = bytes((0x81, 0x80 | size))
    elif size < 65536:
        header = bytes((0x81, 0x80 | 126)) + struct.pack("!H", size)
    else:
        header = bytes((0x81, 0x80 | 127)) + struct.pack("!Q", size)
    masked = bytes(item ^ mask[index % 4] for index, item in enumerate(payload))
    connection.sendall(header + mask + masked)


def _websocket_receive(connection: socket.socket, deadline: float) -> str:
    header = _read_exact(connection, 2, deadline)
    opcode = header[0] & 0x0F
    length = header[1] & 0x7F
    if length == 126:
        length = struct.unpack("!H", _read_exact(connection, 2, deadline))[0]
    elif length == 127:
        length = struct.unpack("!Q", _read_exact(connection, 8, deadline))[0]
    masked = bool(header[1] & 0x80)
    mask = _read_exact(connection, 4, deadline) if masked else b""
    payload = _read_exact(connection, length, deadline)
    if masked:
        payload = bytes(item ^ mask[index % 4] for index, item in enumerate(payload))
    if opcode == 8:
        raise OSError("browser DevTools socket closed")
    if opcode != 1:
        return _websocket_receive(connection, deadline)
    return payload.decode("utf-8")


def _read_exact(connection: socket.socket, size: int, deadline: float) -> bytes:
    values = bytearray()
    while len(values) < size:
        connection.settimeout(_remaining(deadline))
        part = connection.recv(size - len(values))
        if not part:
            raise OSError("browser DevTools socket closed")
        values.extend(part)
    return bytes(values)


def _read_until(connection: socket.socket, marker: bytes, deadline: float) -> bytes:
    values = bytearray()
    while marker not in values:
        connection.settimeout(_remaining(deadline))
        part = connection.recv(1024)
        if not part:
            raise OSError("browser DevTools socket closed")
        values.extend(part)
    return bytes(values)


def _remaining(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("browser validation timed out")
    return remaining


def _stop_browser(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()


def _raise_for_report(
    report: dict[str, object], exceptions: tuple[str, ...]
) -> None:
    ready = report.get("ready")
    errors = report.get("errors")
    omitted = report.get("omitted")
    math_errors = report.get("mathErrors")
    failed_images = report.get("failedImages")
    if ready != "true":
        raise HTMLRenderError(f"browser reader initialization did not complete: {ready}")
    if exceptions:
        raise HTMLRenderError("browser reader raised: " + exceptions[0])
    if isinstance(errors, list) and errors:
        raise HTMLRenderError("browser reader raised: " + str(errors[0]))
    for count, description in (
        (omitted, "unhydrated reader chunks"),
        (math_errors, "math rendering errors"),
        (failed_images, "failed reader images"),
    ):
        if not isinstance(count, int):
            raise HTMLRenderError("browser validation report is malformed")
        if count:
            raise HTMLRenderError(f"browser reader has {count} {description}")


_READER_REPORT_EXPRESSION: Final = """(async () => {
  const errors = [];
  const deadline = Date.now() + 55000;
  while (
    (!document.body || !document.body.dataset.arcRenderReady ||
      document.body.dataset.arcRenderReady === "loading") &&
    Date.now() < deadline
  ) {
    await new Promise(resolve => setTimeout(resolve, 25));
  }
  const ready = document.body.dataset.arcRenderReady || "missing";
  if (ready === "true") {
    window.dispatchEvent(new Event("beforeprint"));
    await new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)));
    await Promise.all(Array.from(document.images).map(async image => {
      image.loading = "eager";
      if (typeof image.decode !== "function") return;
      try {
        await Promise.race([
          image.decode(),
          new Promise(resolve => setTimeout(resolve, 5000))
        ]);
      } catch (_error) {
        // The failedImages check below reports decode failures.
      }
    }));
  }
  return {
    ready,
    errors,
    omitted: document.querySelectorAll(
      ".arc-render-chunk:not(.is-rendered), .arc-render-chunk[aria-busy=\\"true\\"]"
    ).length,
    mathErrors: document.querySelectorAll(".katex-error, .math-error").length,
    failedImages: Array.from(document.images).filter(
      image => !image.complete || image.naturalWidth === 0
    ).length
  };
})()"""


__all__ = ["BrowserValidation", "validate_reader_in_browser"]
