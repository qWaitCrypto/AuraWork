from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any


def _read_json_body(handler: BaseHTTPRequestHandler) -> dict[str, Any]:
    length_raw = handler.headers.get("content-length")
    try:
        length = int(length_raw) if length_raw else 0
    except Exception:
        length = 0
    body = handler.rfile.read(max(0, length)) if length > 0 else b""
    try:
        data = json.loads(body.decode("utf-8", errors="replace"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _json_response(handler: BaseHTTPRequestHandler, status: int, payload: dict[str, Any]) -> None:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("content-type", "application/json; charset=utf-8")
    handler.send_header("content-length", str(len(data)))
    handler.end_headers()
    handler.wfile.write(data)


def _find_last_tool_message(messages: Any) -> dict[str, Any] | None:
    if not isinstance(messages, list):
        return None
    for item in reversed(messages):
        if not isinstance(item, dict):
            continue
        if item.get("role") == "tool":
            return item
    return None


def _tool_call_shell_echo() -> dict[str, Any]:
    tool_call_id = "call_shell_echo"
    args = {"command": "echo hello-from-aura-agno-smoke"}
    return {
        "id": tool_call_id,
        "type": "function",
        "function": {
            "name": "shell__run",
            "arguments": json.dumps(args, ensure_ascii=False),
        },
    }


def _build_final_answer_from_tool_message(tool_msg: dict[str, Any]) -> str:
    content = tool_msg.get("content")
    if not isinstance(content, str) or not content.strip():
        return "Tool finished."
    try:
        obj = json.loads(content)
    except Exception:
        return f"Tool finished (raw): {content[:500]}"
    if not isinstance(obj, dict):
        return "Tool finished."
    result = obj.get("result")
    if not isinstance(result, dict):
        return f"Tool finished: {json.dumps(obj, ensure_ascii=False)[:500]}"
    stdout = result.get("stdout")
    stderr = result.get("stderr")
    exit_code = result.get("exit_code")
    parts: list[str] = ["Tool executed."]
    if isinstance(exit_code, int):
        parts.append(f"exit_code={exit_code}")
    if isinstance(stdout, str) and stdout.strip():
        parts.append("stdout:\n" + stdout.rstrip())
    if isinstance(stderr, str) and stderr.strip():
        parts.append("stderr:\n" + stderr.rstrip())
    return "\n".join(parts).strip() + "\n"


class _OpenAIStubHandler(BaseHTTPRequestHandler):
    server_version = "AuraOpenAIStub/0.1"

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A003
        # Keep smoke tests quiet by default.
        return

    def do_GET(self) -> None:  # noqa: N802
        if self.path.rstrip("/") in {"/health", "/v1/health"}:
            _json_response(self, 200, {"ok": True})
            return
        _json_response(self, 404, {"error": {"message": "not found"}})

    def do_POST(self) -> None:  # noqa: N802
        if self.path.rstrip("/") != "/v1/chat/completions":
            _json_response(self, 404, {"error": {"message": "not found"}})
            return

        req = _read_json_body(self)
        messages = req.get("messages")
        tools = req.get("tools")
        model = req.get("model") or "stub"
        stream = bool(req.get("stream"))

        tool_msg = _find_last_tool_message(messages)

        if tool_msg is None and (not isinstance(tools, list) or not tools):
            assistant_message = {"role": "assistant", "content": "stub: ok\n"}
            finish_reason = "stop"
        elif tool_msg is None:
            tool_calls = [_tool_call_shell_echo()]
            assistant_message: dict[str, Any] = {"role": "assistant", "content": "", "tool_calls": tool_calls}
            finish_reason = "tool_calls"
        else:
            assistant_message = {"role": "assistant", "content": _build_final_answer_from_tool_message(tool_msg)}
            finish_reason = "stop"

        now = int(time.time())
        if stream:
            self.send_response(200)
            self.send_header("content-type", "text/event-stream; charset=utf-8")
            self.send_header("cache-control", "no-cache")
            # The OpenAI Python SDK consumes the stream until EOF after seeing `[DONE]`.
            # Close the connection after emitting `[DONE]` so clients don't hang until timeout.
            self.send_header("connection", "close")
            self.end_headers()

            def _send(data: dict[str, Any]) -> None:
                chunk = json.dumps(data, ensure_ascii=False)
                self.wfile.write(f"data: {chunk}\n\n".encode("utf-8"))
                self.wfile.flush()

            base: dict[str, Any] = {
                "id": f"chatcmpl_stub_{now}",
                "object": "chat.completion.chunk",
                "created": now,
                "model": model,
            }

            if finish_reason == "tool_calls":
                tc = assistant_message.get("tool_calls") or []
                first = tc[0] if isinstance(tc, list) and tc else {}
                delta_tc = {
                    "index": 0,
                    "id": first.get("id") or "call_stub",
                    "type": "function",
                    "function": {
                        "name": (first.get("function") or {}).get("name") or "shell__run",
                        "arguments": (first.get("function") or {}).get("arguments") or "{}",
                    },
                }
                _send(
                    {
                        **base,
                        "choices": [{"index": 0, "delta": {"role": "assistant", "tool_calls": [delta_tc]}, "finish_reason": None}],
                    }
                )
                _send({**base, "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]})
            else:
                text = assistant_message.get("content") or ""
                if not isinstance(text, str):
                    text = str(text)
                # Emit a few chunks to exercise client-side streaming.
                parts = [text[:20], text[20:]]
                first = True
                for part in parts:
                    if not part:
                        continue
                    delta: dict[str, Any] = {"content": part}
                    if first:
                        delta["role"] = "assistant"
                        first = False
                    _send({**base, "choices": [{"index": 0, "delta": delta, "finish_reason": None}]})
                _send({**base, "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]})

            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
            self.close_connection = True
            return

        resp: dict[str, Any] = {
            "id": f"chatcmpl_stub_{now}",
            "object": "chat.completion",
            "created": now,
            "model": model,
            "choices": [
                {
                    "index": 0,
                    "message": assistant_message,
                    "finish_reason": finish_reason,
                }
            ],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        }
        _json_response(self, 200, resp)


@dataclass(slots=True)
class OpenAIStubServer:
    host: str = "127.0.0.1"
    port: int = 0

    _server: ThreadingHTTPServer | None = None
    _thread: threading.Thread | None = None

    def start(self) -> None:
        if self._server is not None:
            return
        httpd = ThreadingHTTPServer((self.host, int(self.port)), _OpenAIStubHandler)
        self._server = httpd
        self.port = int(httpd.server_address[1])
        t = threading.Thread(target=httpd.serve_forever, name="openai-stub", daemon=True)
        self._thread = t
        t.start()

    def stop(self) -> None:
        if self._server is None:
            return
        try:
            self._server.shutdown()
        except Exception:
            pass
        try:
            self._server.server_close()
        except Exception:
            pass
        self._server = None
        self._thread = None

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}/v1"

    def __enter__(self) -> "OpenAIStubServer":
        self.start()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:  # noqa: ANN001
        self.stop()


def main() -> None:
    srv = OpenAIStubServer(host="127.0.0.1", port=19840)
    srv.start()
    try:
        print(srv.base_url)
        while True:
            time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    finally:
        srv.stop()


if __name__ == "__main__":
    main()
