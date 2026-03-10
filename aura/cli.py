from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import sys
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING

from . import __version__
from .runtime.ids import new_id, now_ts_ms
from .runtime.protocol import ArtifactRef, EventKind, Op, OpKind

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_DENIED = 2
EXIT_VALIDATION_FAILED = 3
EXIT_TOOL_FAILED = 4
EXIT_CONFIG_ERROR = 5
PLAIN_INPUT_ENV_VAR = "AURA_PLAIN_INPUT"
logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from .runtime.engine import Engine
    from .runtime.event_bus import EventBus
    from .runtime.stores import FileApprovalStore, FileArtifactStore, FileEventLogStore, FileSessionStore


def _pick_from_list_standalone(
    *,
    title: str,
    items: list[object],
    current_index: int = 0,
    view_height: int = 10,
    render_item,
) -> int | None:
    """
    Standalone in-place selector for early CLI flows (before ConsoleUI/prompt_toolkit).

    - Uses raw keyboard input (↑/↓, Enter=choose, Esc=cancel).
    - Returns selected index, or None if cancelled/unavailable.
    """

    try:
        if not (sys.stdin.isatty() and sys.stdout.isatty()):
            return None
    except Exception:
        return None
    if not items:
        return None

    try:
        selected_index = int(current_index)
    except Exception:
        selected_index = 0
    if selected_index < 0 or selected_index >= len(items):
        selected_index = 0

    def _clamp(n: int, lo: int, hi: int) -> int:
        return lo if n < lo else hi if n > hi else n

    def _read_key(fd: int) -> str:
        import os

        try:
            data = os.read(fd, 32)
        except Exception:
            return ""
        try:
            return data.decode("utf-8", errors="ignore")
        except Exception:
            return ""

    def _move_up(n: int) -> None:
        if n <= 0:
            return
        sys.stdout.write(f"\x1b[{n}A")

    def _print_lines(lines: list[str]) -> None:
        for line in lines:
            sys.stdout.write("\r" + line + "\r\n")

    def _rewrite_block(prev_lines: int, new_lines: list[str]) -> None:
        if prev_lines:
            _move_up(prev_lines)
            sys.stdout.write("\r")
            for i in range(prev_lines):
                sys.stdout.write("\x1b[2K")
                sys.stdout.write("\r\n" if i != prev_lines - 1 else "")
            sys.stdout.write("\r")
            _move_up(prev_lines - 1)
        _print_lines(new_lines)

    def _clear_block(lines: int) -> None:
        if lines <= 0:
            return
        _move_up(lines)
        sys.stdout.write("\r")
        for i in range(lines):
            sys.stdout.write("\x1b[2K")
            sys.stdout.write("\r\n" if i != lines - 1 else "")
        sys.stdout.write("\r")
        _move_up(lines - 1)

    try:
        import termios  # type: ignore
        import tty  # type: ignore
    except Exception:
        return None

    fd = None
    old = None
    try:
        fd = sys.stdin.fileno()
        old = termios.tcgetattr(fd)
        tty.setraw(fd)
    except Exception:
        return None

    rendered_lines: list[str] = []
    try:
        while True:
            height = max(1, int(view_height))
            top = _clamp(selected_index - (height // 2), 0, max(0, len(items) - height))
            end_index = min(len(items), top + height)
            lines: list[str] = [title]
            if top > 0:
                lines.append("  ...")
            for i in range(top, end_index):
                cursor = "›" if i == selected_index else " "
                rendered = str(render_item(i, items[i]))
                line = f"{cursor} {rendered}"
                if i == selected_index:
                    line = "\x1b[7m" + line + "\x1b[0m"
                lines.append(line)
            if end_index < len(items):
                lines.append("  ...")

            _rewrite_block(len(rendered_lines), lines)
            sys.stdout.flush()
            rendered_lines = lines

            key = _read_key(fd)
            if key in {"\r", "\n"}:
                return selected_index
            if key in {"\x1b", "q", "Q", "\x03"}:  # Esc / q / Ctrl+C
                return None
            if key in {"\x1b[A", "k", "K", "\x10"}:  # up / Ctrl+P
                selected_index = (selected_index - 1) % len(items)
                continue
            if key in {"\x1b[B", "j", "J", "\x0e"}:  # down / Ctrl+N
                selected_index = (selected_index + 1) % len(items)
                continue
            if key in {"\x1b[D"}:  # left
                selected_index = (selected_index - 1) % len(items)
                continue
            if key in {"\x1b[C"}:  # right
                selected_index = (selected_index + 1) % len(items)
                continue
    finally:
        try:
            if fd is not None and old is not None:
                termios.tcsetattr(fd, termios.TCSADRAIN, old)
        except Exception:
            logger.warning("Failed to restore terminal attributes in standalone selector.", exc_info=True)
        try:
            if rendered_lines:
                _clear_block(len(rendered_lines))
                sys.stdout.flush()
        except Exception:
            logger.warning("Failed to clear selector UI block in standalone selector.", exc_info=True)


def _pick_session_id_standalone(*, session_store: FileSessionStore, project_ref: str) -> str | None:
    sessions = session_store.list_sessions(filters={"project_ref": project_ref, "mode": "chat"})
    if not sessions:
        return None

    def _render(_i: int, meta: object) -> str:
        if not isinstance(meta, dict):
            return str(meta)
        sid = str(meta.get("session_id") or "").strip()
        updated = meta.get("updated_at")
        profile = meta.get("chat_profile_id")
        extra = []
        if profile:
            extra.append(f"model={profile}")
        if isinstance(updated, int):
            extra.append(f"updated_at={updated}")
        suffix = ("  " + " ".join(extra)) if extra else ""
        return f"{sid}{suffix}"

    idx = _pick_from_list_standalone(
        title="Resume session (↑/↓, Enter=resume, Esc=cancel):",
        items=list(sessions[:20]),
        current_index=0,
        view_height=10,
        render_item=_render,
    )
    if idx is None:
        return None
    chosen = sessions[:20][idx]
    sid = chosen.get("session_id") if isinstance(chosen, dict) else None
    return sid if isinstance(sid, str) and sid.strip() else None


def _configure_text_io() -> None:
    """
    Best-effort I/O normalization for interactive terminals.

    On WSL/Linux it's common to have sys.stdin.errors='surrogateescape'. If invalid byte
    sequences are read from the terminal/clipboard, Python preserves them as surrogate
    codepoints in the resulting str, which later crashes when encoding to UTF-8 for persistence.
    """

    try:
        if hasattr(sys.stdin, "reconfigure"):
            sys.stdin.reconfigure(encoding="utf-8", errors="replace")
        if hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")
        if hasattr(sys.stderr, "reconfigure"):
            sys.stderr.reconfigure(encoding="utf-8", errors="backslashreplace")
    except Exception:
        return


def _sanitize_text(text: str) -> str:
    # Replace illegal Unicode surrogate codepoints (U+D800..U+DFFF) with U+FFFD.
    out: list[str] = []
    changed = False
    for ch in text:
        o = ord(ch)
        if 0xD800 <= o <= 0xDFFF:
            out.append("\uFFFD")
            changed = True
        else:
            out.append(ch)
    return "".join(out) if changed else text


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="aura",
        description="Local-first agent CLI.",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )

    subparsers = parser.add_subparsers(dest="command", required=True)

    init_parser = subparsers.add_parser("init", help="Initialize an Aura project directory.")
    init_parser.add_argument(
        "path",
        nargs="?",
        default=".",
        help="Target directory (default: current directory).",
    )
    init_parser.set_defaults(func=_cmd_init)

    chat_parser = subparsers.add_parser("chat", help="Start an interactive session.")
    chat_parser.add_argument(
        "--session",
        dest="session_id",
        default=None,
        help="Resume an existing session by ID.",
    )
    chat_parser.add_argument(
        "--resume",
        dest="resume",
        action="store_true",
        help="Interactively pick a recent session to resume.",
    )
    chat_parser.add_argument(
        "--timeout",
        dest="timeout_s",
        type=float,
        default=None,
        help="Per-request timeout in seconds (no default).",
    )
    chat_parser.add_argument(
        "--system",
        dest="system_prompt",
        default=None,
        help="Optional system prompt override.",
    )
    chat_parser.add_argument(
        "--no-tools",
        dest="enable_tools",
        action="store_false",
        help="Disable tool calling.",
    )
    chat_parser.add_argument(
        "--max-tool-turns",
        dest="max_tool_turns",
        type=int,
        default=30,
        help="Max model+tool turns per user message (default: 30, max: 256).",
    )
    chat_parser.add_argument(
        "--color",
        dest="color",
        choices=["auto", "always", "never"],
        default="auto",
        help="Color mode for console output: auto (TTY only), always, or never.",
    )
    chat_parser.add_argument(
        "--plain-input",
        dest="plain_input",
        action="store_true",
        help=f"Force basic input() mode (disable prompt_toolkit). Same as {PLAIN_INPUT_ENV_VAR}=1.",
    )
    chat_parser.set_defaults(enable_tools=None)
    chat_parser.set_defaults(func=_cmd_chat)

    session_parser = subparsers.add_parser("session", help="Manage sessions.")
    session_subparsers = session_parser.add_subparsers(dest="session_cmd", required=True)
    session_list_parser = session_subparsers.add_parser("list", help="List sessions.")
    session_list_parser.set_defaults(func=_cmd_session_list)
    session_resume_parser = session_subparsers.add_parser("resume", help="Resume a session.")
    session_resume_parser.add_argument("session_id", help="Session ID to resume.")
    session_resume_parser.add_argument(
        "--timeout",
        dest="timeout_s",
        type=float,
        default=None,
        help="Per-request timeout in seconds (no default).",
    )
    session_resume_parser.add_argument(
        "--system",
        dest="system_prompt",
        default=None,
        help="Optional system prompt override.",
    )
    session_resume_parser.add_argument(
        "--no-tools",
        dest="enable_tools",
        action="store_false",
        help="Disable tool calling.",
    )
    session_resume_parser.add_argument(
        "--max-tool-turns",
        dest="max_tool_turns",
        type=int,
        default=30,
        help="Max model+tool turns per user message (default: 30, max: 256).",
    )
    session_resume_parser.add_argument(
        "--color",
        dest="color",
        choices=["auto", "always", "never"],
        default="auto",
        help="Color mode for console output: auto (TTY only), always, or never.",
    )
    session_resume_parser.add_argument(
        "--plain-input",
        dest="plain_input",
        action="store_true",
        help=f"Force basic input() mode (disable prompt_toolkit). Same as {PLAIN_INPUT_ENV_VAR}=1.",
    )
    session_resume_parser.set_defaults(enable_tools=None)
    session_resume_parser.set_defaults(func=_cmd_session_resume)

    debug_parser = subparsers.add_parser("debug", help="Debug utilities.")
    debug_subparsers = debug_parser.add_subparsers(dest="debug_cmd", required=True)
    debug_export_parser = debug_subparsers.add_parser("export", help="Export a replay bundle.")
    debug_export_parser.add_argument("session_id", help="Session ID to export.")
    debug_export_parser.add_argument(
        "-o",
        "--output",
        default=".",
        help="Output directory (default: current directory).",
    )
    debug_export_parser.set_defaults(func=_cmd_debug_export)

    debug_validate_parser = debug_subparsers.add_parser("validate", help="Validate a session log or bundle.")
    debug_validate_parser.add_argument(
        "target",
        help="Session ID (in current project) or bundle directory path.",
    )
    debug_validate_parser.add_argument(
        "--strict",
        action="store_true",
        help="Treat unknown kinds/missing store references as errors.",
    )
    debug_validate_parser.set_defaults(func=_cmd_debug_validate)

    return parser


def _cmd_not_implemented(_: argparse.Namespace) -> int:
    print("Not implemented yet.", file=sys.stderr)
    return EXIT_ERROR


def _cmd_chat(args: argparse.Namespace) -> int:
    from .runtime.engine import EngineBuildError, build_engine_for_session
    from .runtime.event_bus import EventBus
    from .runtime.llm.config import ModelConfig
    from .runtime.llm.config_io import load_model_config_layers_for_dir
    from .runtime.llm.types import ModelRole
    from .runtime.project import RuntimePaths
    from .runtime.session_settings import update_session_settings
    from .runtime.stores import FileApprovalStore, FileArtifactStore, FileEventLogStore, FileSessionStore
    from .runtime.tools.runtime import ToolApprovalMode

    try:
        paths = RuntimePaths.discover()
    except FileNotFoundError as e:
        print(str(e), file=sys.stderr)
        return EXIT_CONFIG_ERROR

    artifact_store = FileArtifactStore(paths.artifacts_dir)
    session_store = FileSessionStore(paths.sessions_dir)
    approval_store = FileApprovalStore(paths.state_dir / "approvals")
    event_log_store = FileEventLogStore(
        paths.events_dir, artifact_store=artifact_store, session_store=session_store
    )
    event_bus = EventBus(event_log_store=event_log_store)

    try:
        layers = load_model_config_layers_for_dir(paths.project_root, require_project=True)
    except Exception as e:
        print(str(e), file=sys.stderr)
        return EXIT_CONFIG_ERROR

    # Default: require approval only for high-risk operations (shell commands).
    default_approval_mode = ToolApprovalMode.STANDARD
    session_id = args.session_id
    resumed = False
    if session_id is None:
        if getattr(args, "resume", False):
            picked = _pick_session_id_standalone(
                session_store=session_store,
                project_ref=str(paths.project_root),
            )
            if picked:
                session_id = picked
                resumed = True
                try:
                    session_meta = session_store.get_session(session_id)
                except FileNotFoundError as e:
                    print(str(e), file=sys.stderr)
                    return EXIT_CONFIG_ERROR
        if session_id is None:
            session_id = session_store.create_session(
                {
                    "project_ref": str(paths.project_root),
                    "mode": "chat",
                    "tool_approval_mode": default_approval_mode.value,
                    "llm_streaming": True,
                }
            )
            session_meta = session_store.get_session(session_id)
    else:
        resumed = True
        try:
            session_meta = session_store.get_session(session_id)
        except FileNotFoundError as e:
            print(str(e), file=sys.stderr)
            return EXIT_CONFIG_ERROR

    # Apply any session-level chat model selection.
    chat_profile_id = session_meta.get("chat_profile_id")
    if isinstance(chat_profile_id, str) and chat_profile_id.strip():
        layers = replace(
            layers,
            session_config=ModelConfig(role_pointers={ModelRole.MAIN: chat_profile_id.strip()}),
        )

    try:
        model_config = layers.merged()
    except Exception as e:
        # If the session stored an invalid profile id, fall back to config defaults.
        layers = replace(layers, session_config=None)
        try:
            model_config = layers.merged()
        except Exception:
            print(str(e), file=sys.stderr)
            return EXIT_CONFIG_ERROR

    enable_tools = getattr(args, "enable_tools", None)
    if enable_tools is None:
        profile = model_config.get_profile_for_role(ModelRole.MAIN)
        if profile is None:
            enable_tools = False
        else:
            caps = profile.capabilities.with_provider_defaults(profile.provider_kind)
            enable_tools = caps.supports_tools is True

    raw_mode = session_meta.get("tool_approval_mode")
    try:
        approval_mode = ToolApprovalMode(str(raw_mode)) if raw_mode else default_approval_mode
    except ValueError:
        approval_mode = default_approval_mode
    if session_meta.get("tool_approval_mode") != approval_mode.value:
        update_session_settings(
            session_store=session_store,
            session_id=session_id,
            model_config=model_config,
            tool_approval_mode=approval_mode.value,
        )

    try:
        orchestrator = build_engine_for_session(
            project_root=paths.project_root,
            session_id=session_id,
            event_bus=event_bus,
            session_store=session_store,
            event_log_store=event_log_store,
            artifact_store=artifact_store,
            approval_store=approval_store,
            model_config=model_config,
            system_prompt=args.system_prompt,
            tools_enabled=bool(enable_tools),
            max_tool_turns=args.max_tool_turns,
        )
    except EngineBuildError as e:
        print(str(e), file=sys.stderr)
        return EXIT_CONFIG_ERROR
    memory_summary = session_meta.get("memory_summary")
    if isinstance(memory_summary, str) and memory_summary.strip():
        orchestrator.memory_summary = memory_summary
    if orchestrator.tool_runtime is not None:
        orchestrator.tool_runtime.set_approval_mode(approval_mode)
    raw_streaming = session_meta.get("llm_streaming")
    if isinstance(raw_streaming, bool):
        orchestrator.set_llm_streaming(raw_streaming)
    else:
        orchestrator.set_llm_streaming(True)
        try:
            update_session_settings(
                session_store=session_store,
                session_id=session_id,
                model_config=model_config,
                llm_streaming=True,
            )
        except Exception:
            logger.warning("Failed to persist default llm_streaming for session_id=%s", session_id, exc_info=True)
    orchestrator.load_history_from_events()
    orchestrator.apply_memory_summary_retention()

    return _run_chat_console_ui(
        orchestrator=orchestrator,
        event_bus=event_bus,
        session_id=session_id,
        approval_store=approval_store,
        event_log_store=event_log_store,
        artifact_store=artifact_store,
        history_file=paths.history_file,
        timeout_s=args.timeout_s,
        print_replay=resumed,
        color=getattr(args, "color", "auto"),
        force_plain_input=bool(getattr(args, "plain_input", False)),
    )


def _run_chat_console_ui(
    *,
    orchestrator: "Engine",
    event_bus: "EventBus",
    session_id: str,
    approval_store: "FileApprovalStore",
    event_log_store: "FileEventLogStore",
    artifact_store: "FileArtifactStore",
    history_file: Path,
    timeout_s: float | None,
    print_replay: bool,
    color: str,
    force_plain_input: bool = False,
) -> int:
    from contextlib import contextmanager

    from .runtime.event_bus import EventFilter, EventLogAppendError
    from .runtime.llm.errors import CancellationToken
    from .runtime.context_mgmt import render_context_left_line
    from .runtime.session_settings import update_session_settings
    from .runtime.tools.runtime import ToolApprovalMode
    from .ui.console_ui import ConsoleUI, ThinkTagParser, UIEvent, UIEventKind

    def _is_tty() -> bool:
        try:
            return bool(sys.stdin.isatty() and sys.stdout.isatty())
        except Exception:
            return False

    def _should_use_prompt_toolkit() -> bool:
        # Let callers (and tests) force plain input mode.
        if force_plain_input:
            return False
        if str(os.environ.get(PLAIN_INPUT_ENV_VAR) or "").strip().lower() in {"1", "true", "yes", "on"}:
            return False
        if not _is_tty():
            return False
        # If builtins.input is patched (e.g. unittest.mock), prefer plain input so tests can drive the CLI.
        try:
            import builtins as _builtins

            mod = getattr(type(getattr(_builtins, "input")), "__module__", "")
            if isinstance(mod, str) and mod.startswith("unittest.mock"):
                return False
        except Exception:
            logger.warning("Failed to inspect builtins.input when deciding prompt_toolkit mode.", exc_info=True)
        return True

    # Input: prompt_toolkit if available and appropriate; otherwise basic input().
    prompt_session = None
    status_bar = {"context": "100% context left"}
    if _should_use_prompt_toolkit():
        try:
            from prompt_toolkit import PromptSession
            from prompt_toolkit.completion import Completer, Completion
            from prompt_toolkit.history import FileHistory
            from prompt_toolkit.key_binding import KeyBindings
            from prompt_toolkit.keys import Keys

            class _SlashCompleter(Completer):
                _cmds = ["/help", "/clear", "/perm", "/model", "/stream", "/compact", "/exit", "/quit"]

                def get_completions(self, document, complete_event):
                    text = document.text_before_cursor
                    if not text.startswith("/"):
                        return
                    for c in self._cmds:
                        if c.startswith(text):
                            yield Completion(c, start_position=-len(text))

            kb = KeyBindings()

            @kb.add(Keys.ControlJ)
            def _(event) -> None:
                event.current_buffer.insert_text("\n")

            @kb.add(Keys.Enter)
            def _(event) -> None:
                event.current_buffer.validate_and_handle()

            # Some terminals map Enter to Ctrl+M. Bind it explicitly to "send".
            @kb.add(Keys.ControlM)
            def _(event) -> None:
                event.current_buffer.validate_and_handle()

            history_path = history_file
            try:
                history_path.parent.mkdir(parents=True, exist_ok=True)
            except Exception:
                logger.warning("Failed to create prompt history directory: %s", history_path.parent, exc_info=True)
            prompt_session = PromptSession(
                message="You> ",
                multiline=True,
                key_bindings=kb,
                completer=_SlashCompleter(),
                history=FileHistory(str(history_path)),
                bottom_toolbar=lambda: status_bar["context"],
                # Make it obvious that multiple lines are part of a single message.
                # (Otherwise users may think each line is a separate "send".)
                prompt_continuation=lambda width, line_number, is_soft_wrap: "... ",
            )
        except Exception:
            prompt_session = None

    enable_color: bool
    color_mode = str(color or "auto").strip().lower()
    if color_mode == "always":
        enable_color = True
    elif color_mode == "never":
        enable_color = False
    else:
        enable_color = _is_tty()

    ui = ConsoleUI(stream=sys.stdout, enable_color=enable_color)
    ui.start()
    ui.print_header(session_id=session_id)

    # Replay (resume) through UI to keep a single output channel.
    if print_replay:
        _emit_replay_to_ui(
            session_id,
            ui=ui,
            event_log_store=event_log_store,
            artifact_store=artifact_store,
        )

    think_parser = ThinkTagParser()

    def _on_runtime_event(event) -> None:
        try:
            if event.kind in {
                EventKind.LLM_REQUEST_STARTED.value,
                EventKind.LLM_RESPONSE_COMPLETED.value,
                EventKind.OPERATION_COMPLETED.value,
            }:
                payload = event.payload if isinstance(event.payload, dict) else {}
                cs = payload.get("context_stats")
                if isinstance(cs, dict):
                    used = cs.get("input_tokens")
                    if not isinstance(used, int):
                        used = cs.get("estimated_input_tokens")
                    limit = cs.get("context_limit_tokens")
                    if not isinstance(limit, int):
                        limit = None
                    status_bar["context"] = render_context_left_line(
                        used_tokens=used if isinstance(used, int) else None,
                        context_limit_tokens=limit if isinstance(limit, int) else None,
                    )
        except Exception:
            logger.warning("Failed to update context status from runtime event.", exc_info=True)
        for uiev in _runtime_event_to_ui_events(event, think_parser=think_parser):
            ui.emit(uiev)

    event_bus.subscribe(_on_runtime_event, EventFilter(session_id=session_id))

    @contextmanager
    def _quiet_stdin_while_waiting():
        # Prevent "type-ahead" from being echoed into the spinner line while we are waiting
        # (the main thread is not reading input during LLM streaming).
        try:
            if not sys.stdin.isatty():
                yield
                return
        except Exception:
            yield
            return

        try:
            import termios  # type: ignore

            fd = sys.stdin.fileno()
            old = termios.tcgetattr(fd)
            new = list(old)
            new[3] = new[3] & ~termios.ECHO
            termios.tcsetattr(fd, termios.TCSADRAIN, new)
        except Exception:
            # If we can't control ECHO, fall back to normal input behavior.
            yield
            return

        try:
            yield
        finally:
            try:
                termios.tcsetattr(fd, termios.TCSADRAIN, old)
                termios.tcflush(fd, termios.TCIFLUSH)
            except Exception:
                logger.warning("Failed to restore terminal echo state after waiting.", exc_info=True)

    def _run_op(op: Op) -> None:
        cancel = CancellationToken()
        with _quiet_stdin_while_waiting():
            try:
                orchestrator.run(op, timeout_s=timeout_s, cancel=cancel)
            except KeyboardInterrupt:
                cancel.cancel()
                ui.emit(UIEvent(UIEventKind.CANCELLED, {"message": "cancelled"}))

    def _prompt(text: str = "You> ") -> str:
        if prompt_session is not None:
            return prompt_session.prompt(text)
        return input(text)

    def _pick_from_list_interactive(
        *,
        title: str,
        items: list[object],
        current_index: int = 0,
        view_height: int = 8,
        render_item,
    ) -> int | None:
        """
        Lightweight in-place selector (no full-screen TUI).

        - Uses raw keyboard input (↑/↓, Enter=choose, Esc=cancel).
        - Renders a small list "dropdown" under the current prompt line.
        - Returns the selected index, or None if cancelled/unavailable.
        """

        if prompt_session is None:
            return None
        try:
            if not (sys.stdin.isatty() and sys.stdout.isatty()):
                return None
        except Exception:
            return None
        if not items:
            return None

        try:
            selected_index = int(current_index)
        except Exception:
            selected_index = 0
        if selected_index < 0 or selected_index >= len(items):
            selected_index = 0

        is_ansi = True
        try:
            is_ansi = bool(sys.stdout.isatty())
        except Exception:
            is_ansi = True

        def _clamp(n: int, lo: int, hi: int) -> int:
            return lo if n < lo else hi if n > hi else n

        def _read_key(fd: int) -> str:
            import os

            try:
                data = os.read(fd, 32)
            except Exception:
                return ""
            try:
                return data.decode("utf-8", errors="ignore")
            except Exception:
                return ""

        def _move_up(n: int) -> None:
            if n <= 0:
                return
            sys.stdout.write(f"\x1b[{n}A")

        def _print_lines(lines: list[str]) -> None:
            for line in lines:
                sys.stdout.write("\r" + line + "\r\n")

        def _rewrite_block(prev_lines: int, new_lines: list[str]) -> None:
            if prev_lines:
                _move_up(prev_lines)
                sys.stdout.write("\r")
                for i in range(prev_lines):
                    sys.stdout.write("\x1b[2K")
                    sys.stdout.write("\r\n" if i != prev_lines - 1 else "")
                sys.stdout.write("\r")
                _move_up(prev_lines - 1)
            _print_lines(new_lines)

        def _clear_block(lines: int) -> None:
            if lines <= 0:
                return
            _move_up(lines)
            sys.stdout.write("\r")
            for i in range(lines):
                sys.stdout.write("\x1b[2K")
                sys.stdout.write("\r\n" if i != lines - 1 else "")
            sys.stdout.write("\r")
            _move_up(lines - 1)

        # Ensure the renderer thread is paused and the prompt line is clean.
        with ui.suspend():
            try:
                import termios  # type: ignore
                import tty  # type: ignore

                fd = sys.stdin.fileno()
                old = termios.tcgetattr(fd)
                tty.setraw(fd)
            except Exception:
                return None

            rendered_lines: list[str] = []
            try:
                while True:
                    height = max(1, int(view_height))
                    top = _clamp(
                        selected_index - (height // 2),
                        0,
                        max(0, len(items) - height),
                    )
                    end_index = min(len(items), top + height)
                    lines: list[str] = [title]
                    if top > 0:
                        lines.append("  ...")
                    for i in range(top, end_index):
                        cursor = "›" if i == selected_index else " "
                        rendered = str(render_item(i, items[i]))
                        line = f"{cursor} {rendered}"
                        if i == selected_index and is_ansi:
                            line = "\x1b[7m" + line + "\x1b[0m"
                        lines.append(line)
                    if end_index < len(items):
                        lines.append("  ...")

                    _rewrite_block(len(rendered_lines), lines)
                    sys.stdout.flush()
                    rendered_lines = lines

                    key = _read_key(fd)
                    if key in {"\r", "\n"}:
                        return selected_index
                    if key in {"\x1b", "q", "Q"}:
                        return None
                    if key in {"\x03"}:  # Ctrl+C
                        return None

                    if key in {"\x1b[A", "k", "K", "\x10"}:  # up / Ctrl+P
                        selected_index = (selected_index - 1) % len(items)
                        continue
                    if key in {"\x1b[B", "j", "J", "\x0e"}:  # down / Ctrl+N
                        selected_index = (selected_index + 1) % len(items)
                        continue
                    if key in {"\x1b[D"}:  # left
                        selected_index = (selected_index - 1) % len(items)
                        continue
                    if key in {"\x1b[C"}:  # right
                        selected_index = (selected_index + 1) % len(items)
                        continue
            finally:
                try:
                    termios.tcsetattr(fd, termios.TCSADRAIN, old)
                except Exception:
                    logger.warning("Failed to restore terminal attributes in interactive picker.", exc_info=True)
                try:
                    if rendered_lines:
                        _clear_block(len(rendered_lines))
                        sys.stdout.flush()
                except Exception:
                    logger.warning("Failed to clear selector UI block in interactive picker.", exc_info=True)

    def _pick_model_profile_interactive(*, cfg, current_profile_id: str | None) -> str | None:
        profile_ids = sorted(cfg.profiles.keys())
        if not profile_ids:
            return None

        try:
            current_index = profile_ids.index(current_profile_id) if current_profile_id in cfg.profiles else 0
        except Exception:
            current_index = 0

        selected = _pick_from_list_interactive(
            title="Select chat model (↑/↓, Enter=choose, Esc=cancel):",
            items=list(profile_ids),
            current_index=current_index,
            view_height=8,
            render_item=lambda _i, pid: f"{'*' if pid == current_profile_id else ' '} {pid}  {cfg.profiles[pid].provider_kind.value} {cfg.profiles[pid].model_name}",
        )
        if selected is None:
            return None
        return profile_ids[int(selected)]

    def _pick_perm_mode_interactive(*, current_mode: str) -> str | None:
        modes: list[tuple[str, str]] = [
            ("strict", "approval required for every tool call"),
            ("standard", "approval required only for shell commands"),
            ("trusted", "no approvals (dangerous)"),
        ]
        idx = 0
        for i, (m, _) in enumerate(modes):
            if m == current_mode:
                idx = i
                break
        selected = _pick_from_list_interactive(
            title="Select tool approval mode (↑/↓, Enter=choose, Esc=cancel):",
            items=list(modes),
            current_index=idx,
            view_height=6,
            render_item=lambda _i, item: f"{'*' if item[0] == current_mode else ' '} {item[0]}  {item[1]}",
        )
        if selected is None:
            return None
        return modes[int(selected)][0]

    def _handle_pending_approvals_ui(*, request_id: str | None) -> None:
        from .runtime.approval import ApprovalStatus

        def _prompt_decision(text: str) -> str:
            if prompt_session is not None:
                try:
                    return prompt_session.prompt(text, multiline=False)
                except TypeError:
                    return prompt_session.prompt(text)
            return input(text)

        def _ansi(code: str) -> str:
            return f"\x1b[{code}m"

        def _colorize_unified_diff(text: str, *, enable_color: bool) -> str:
            if not enable_color:
                return text
            in_diff = False
            out_lines: list[str] = []
            for line in text.splitlines():
                if line.startswith("--- a/"):
                    in_diff = True
                    out_lines.append(_ansi("2;36") + line + _ansi("0"))
                    continue
                if in_diff and line.startswith("+++ b/"):
                    out_lines.append(_ansi("2;36") + line + _ansi("0"))
                    continue
                if in_diff and line.startswith("@@"):
                    out_lines.append(_ansi("35") + line + _ansi("0"))
                    continue
                if (in_diff and line.startswith("+") and not line.startswith("+++")) or line.lstrip().startswith("+"):
                    out_lines.append(_ansi("32") + line + _ansi("0"))
                    continue
                if (in_diff and line.startswith("-") and not line.startswith("---")) or line.lstrip().startswith("-"):
                    out_lines.append(_ansi("31") + line + _ansi("0"))
                    continue
                # Compact preview format (line-numbered): "   12 -text" / "   12 +text"
                stripped = line.lstrip()
                if stripped and stripped[0].isdigit():
                    try:
                        rest = stripped.split(None, 1)[1]
                    except Exception:
                        rest = ""
                    if rest.startswith("-"):
                        out_lines.append(_ansi("31") + line + _ansi("0"))
                        continue
                    if rest.startswith("+"):
                        out_lines.append(_ansi("32") + line + _ansi("0"))
                        continue
                out_lines.append(line)
            return "\n".join(out_lines)

        def _one_line_preview(value: object, *, max_chars: int) -> str:
            if not isinstance(value, str):
                return ""
            s = " ".join(value.splitlines()).strip()
            if len(s) <= max_chars:
                return s
            return s[: max(0, max_chars - 1)].rstrip() + "…"

        def _read_diff_preview_text(record: ApprovalRecord) -> str | None:
            diff_ref_raw = record.diff_ref
            if not isinstance(diff_ref_raw, dict):
                return None
            try:
                ref = ArtifactRef.from_dict(diff_ref_raw)
                raw = orchestrator.artifact_store.get(ref)
                text = raw.decode("utf-8", errors="replace").rstrip()
            except Exception:
                return None
            if not text:
                return None
            max_chars = 8000
            if len(text) > max_chars:
                return text[:max_chars] + f"\n... (truncated, {len(text)} chars total)"
            return text

        def _tighten_diff_preview(text: str) -> str:
            """
            Make approval diff previews compact.

            Many previews are generated by ToolRuntime and may include a title/header block.
            If the preview contains "• Edited ..." lines, show only that compact section.
            """

            lines = [ln.rstrip("\r\n") for ln in str(text).splitlines()]
            # Drop leading/trailing blank lines.
            while lines and not lines[0].strip():
                lines.pop(0)
            while lines and not lines[-1].strip():
                lines.pop()

            start = None
            for i, ln in enumerate(lines):
                if ln.startswith("• Edited "):
                    start = i
                    break
            if start is not None:
                lines = lines[start:]
                while lines and not lines[-1].strip():
                    lines.pop()
            return "\n".join(lines).rstrip()

        def _println(text: str = "") -> None:
            sys.stdout.write(text + "\n")
            sys.stdout.flush()

        while True:
            pending = approval_store.list(session_id=session_id, status=ApprovalStatus.PENDING, request_id=request_id)
            if not pending:
                return
            record = pending[0]
            tool_name = None
            tool_args: dict | None = None
            try:
                raw_calls = record.resume_payload.get("tool_calls") if isinstance(record.resume_payload, dict) else None
                if isinstance(raw_calls, list) and raw_calls and isinstance(raw_calls[0], dict):
                    first = raw_calls[0]
                    tool_name = first.get("tool_name")
                    args_ref = first.get("arguments_ref")
                    if isinstance(args_ref, dict):
                        ref = ArtifactRef.from_dict(args_ref)
                        raw = orchestrator.artifact_store.get(ref)
                        tool_args_any = json.loads(raw.decode("utf-8", errors="replace"))
                        if isinstance(tool_args_any, dict):
                            tool_args = tool_args_any
            except Exception:
                tool_name = tool_name

            # Compact approval prompt (Codex/Goose-ish).
            is_shell_run = tool_name == "shell__run" and isinstance(tool_args, dict)
            with ui.suspend():
                enable_color = bool(getattr(sys.stdout, "isatty", lambda: False)())
                if is_shell_run:
                    cmd = _one_line_preview(tool_args.get("command"), max_chars=240)
                    cwd = _one_line_preview(tool_args.get("cwd") or ".", max_chars=120) or "."
                    preview = f"$ {cmd}" if cmd else "$ <missing command>"

                    _println("Would you like to run the following command?")
                    _println(f"  {preview}")
                    if cwd and cwd != ".":
                        _println(f"  (cwd: {cwd})")
                else:
                    _println("Approval required:")
                    _println(f"  {record.action_summary}")

                diff_text = _read_diff_preview_text(record)
                if diff_text:
                    tightened = _tighten_diff_preview(diff_text)
                    rendered = _colorize_unified_diff(tightened, enable_color=enable_color)
                    if rendered.strip():
                        _println(rendered)

                while True:
                    try:
                        ans = _prompt_decision("Proceed? [y/n] > ").strip().lower()
                    except KeyboardInterrupt:
                        _println("Approval cancelled; still pending.")
                        return
                    if ans in {"y", "yes"}:
                        decision, note = "approve", None
                        # Keep approvals explicit for now; per-command remember/allowlist can be re-enabled later.
                        persist = False
                        break
                    if ans in {"n", "no"}:
                        decision, persist = "deny", False
                        try:
                            note_raw = _prompt_decision("Tell assistant what to do differently (optional)> ").strip()
                        except (EOFError, KeyboardInterrupt):
                            note_raw = ""
                        note = note_raw if note_raw else None
                        break
                    _println("Please type y or n.")
            if persist and tool_name == "shell__run" and isinstance(tool_args, dict):
                try:
                    from .runtime.tools.runtime import add_shell_run_allowlist_rule

                    cmd = tool_args.get("command")
                    cwd = tool_args.get("cwd") if isinstance(tool_args.get("cwd"), str) else None
                    if isinstance(cmd, str) and cmd.strip():
                        add_shell_run_allowlist_rule(
                            project_root=orchestrator.project_root,
                            command_prefix=" ".join(cmd.strip().splitlines()).strip(),
                            cwd=cwd,
                        )
                        ui.emit(UIEvent(UIEventKind.LOG, {"level": "policy", "message": "Saved allowlist rule for this command."}))
                except Exception:
                    logger.warning("Failed to persist shell allowlist rule from approval prompt.", exc_info=True)

            op = Op(
                kind=OpKind.APPROVAL_DECISION.value,
                payload={"approval_id": record.approval_id, "decision": decision, "note": note},
                session_id=session_id,
                request_id=new_id("req"),
                timestamp=now_ts_ms(),
                turn_id=new_id("turn"),
            )
            _run_op(op)

    # Initial approvals (resume case).
    try:
        _handle_pending_approvals_ui(request_id=None)
    except EventLogAppendError as e:
        ui.emit(UIEvent(UIEventKind.ERROR_RAISED, {"code": "event_log", "message": str(e), "recoverable": False}))
        ui.stop()
        return EXIT_CONFIG_ERROR

    while True:
        try:
            user_text = _prompt("You> ")
        except (EOFError, KeyboardInterrupt):
            break

        user_text = _sanitize_text(user_text).strip("\n")
        if not user_text.strip():
            continue

        cmd = user_text.strip()
        if cmd in {"/exit", "/quit"}:
            break
        if cmd in {"/help", "/?"}:
            ui.emit(
                UIEvent(
                    UIEventKind.LOG,
                    {
                        "level": "help",
                        "message": "Enter=send; Ctrl+J=newline (multi-line message; continuation shows '...'); Ctrl+C=cancel; /clear clears; /perm sets tool approval mode; /model selects chat model; /stream toggles LLM streaming; /compact summarizes and prunes history; /exit quits.",
                    },
                )
            )
            continue
        if cmd == "/clear":
            ui.emit(UIEvent(UIEventKind.CLEAR_SCREEN, {}))
            continue
        if cmd == "/compact":
            op = Op(
                kind=OpKind.COMPACT.value,
                payload={},
                session_id=session_id,
                request_id=new_id("req"),
                timestamp=now_ts_ms(),
                turn_id=new_id("turn"),
            )
            _run_op(op)
            continue
        if cmd.startswith("/stream"):
            parts = cmd.split()
            if len(parts) == 1:
                state = "on" if orchestrator.get_llm_streaming() else "off"
                ui.emit(
                    UIEvent(
                        UIEventKind.LOG,
                        {
                            "level": "policy",
                            "message": f"LLM streaming: {state}\nUsage: /stream [on|off|toggle]",
                        },
                    )
                )
                continue
            raw = parts[1].strip().lower()
            if raw in {"toggle", "t"}:
                enabled = not orchestrator.get_llm_streaming()
            elif raw in {"on", "true", "1", "yes", "y"}:
                enabled = True
            elif raw in {"off", "false", "0", "no", "n"}:
                enabled = False
            else:
                ui.emit(UIEvent(UIEventKind.WARNING, {"message": "Usage: /stream [on|off|toggle]"}))
                continue
            orchestrator.set_llm_streaming(enabled)
            try:
                update_session_settings(
                    session_store=orchestrator.session_store,
                    session_id=orchestrator.session_id,
                    model_config=orchestrator.model_config,
                    llm_streaming=enabled,
                )
            except Exception:
                logger.warning(
                    "Failed to persist llm_streaming=%s for session_id=%s",
                    enabled,
                    orchestrator.session_id,
                    exc_info=True,
                )
            ui.emit(
                UIEvent(
                    UIEventKind.LOG,
                    {"level": "policy", "message": f"LLM streaming set to: {'on' if enabled else 'off'}"},
                )
            )
            continue
        if cmd.startswith("/model"):
            from .runtime.llm.types import ModelRole

            parts = cmd.split()
            cfg = orchestrator.model_config
            current_profile_id = cfg.role_pointers.get(ModelRole.MAIN)
            if len(parts) == 1:
                picked = _pick_model_profile_interactive(cfg=cfg, current_profile_id=current_profile_id)
                if not picked:
                    if not cfg.profiles:
                        ui.emit(UIEvent(UIEventKind.WARNING, {"message": "No model profiles configured."}))
                    else:
                        ui.emit(UIEvent(UIEventKind.WARNING, {"message": "Usage: /model [list] | /model <profile-id>"}))
                    continue
                target = picked
            elif parts[1] in {"list", "ls"}:
                current_desc = "(unset)"
                if isinstance(current_profile_id, str) and current_profile_id in cfg.profiles:
                    p = cfg.profiles[current_profile_id]
                    current_desc = f"{current_profile_id} ({p.provider_kind.value} {p.model_name})"
                ui.emit(UIEvent(UIEventKind.LOG, {"level": "policy", "message": f"Chat model: {current_desc}"}))
                if not cfg.profiles:
                    ui.emit(UIEvent(UIEventKind.WARNING, {"message": "No model profiles configured."}))
                    continue
                lines = ["Available profiles:"]
                for pid in sorted(cfg.profiles.keys()):
                    p = cfg.profiles[pid]
                    mark = "*" if pid == current_profile_id else " "
                    lines.append(f"  {mark} {pid}: {p.provider_kind.value} {p.model_name}")
                ui.emit(UIEvent(UIEventKind.LOG, {"level": "policy", "message": "\n".join(lines)}))
                ui.emit(UIEvent(UIEventKind.LOG, {"level": "policy", "message": "Usage: /model <profile-id>"}))
                continue
            elif len(parts) >= 2 and parts[1] in {"set", "use"}:
                target = parts[2].strip() if len(parts) >= 3 else ""
                if not target:
                    picked = _pick_model_profile_interactive(cfg=cfg, current_profile_id=current_profile_id)
                    if not picked:
                        ui.emit(UIEvent(UIEventKind.WARNING, {"message": "Usage: /model [list] | /model <profile-id>"}))
                        continue
                    target = picked
            else:
                target = parts[1].strip()
                if not target:
                    ui.emit(UIEvent(UIEventKind.WARNING, {"message": "Usage: /model [list] | /model <profile-id>"}))
                    continue

            if target not in cfg.profiles:
                ui.emit(UIEvent(UIEventKind.WARNING, {"message": f"Unknown model profile: {target}"}))
                continue
            try:
                orchestrator.set_chat_model_profile(target)
            except Exception as e:
                ui.emit(UIEvent(UIEventKind.WARNING, {"message": f"Failed to switch model: {e}"}))
                continue
            try:
                update_session_settings(
                    session_store=orchestrator.session_store,
                    session_id=orchestrator.session_id,
                    model_config=orchestrator.model_config,
                    chat_profile_id=target,
                )
            except Exception:
                logger.warning(
                    "Failed to persist chat_profile_id=%s for session_id=%s",
                    target,
                    orchestrator.session_id,
                    exc_info=True,
                )
            p = cfg.profiles[target]
            ui.emit(UIEvent(UIEventKind.LOG, {"level": "policy", "message": f"Chat model set to: {target} ({p.provider_kind.value} {p.model_name})"}))
            if orchestrator.tools_enabled:
                caps = p.capabilities.with_provider_defaults(p.provider_kind)
                if caps.supports_tools is not True:
                    ui.emit(UIEvent(UIEventKind.WARNING, {"message": "Selected model does not declare tool support; tool calls may fail."}))
            continue
        if cmd.startswith("/perm"):
            parts = cmd.split()
            tr = orchestrator.tool_runtime
            if tr is None:
                ui.emit(UIEvent(UIEventKind.WARNING, {"message": "Tool runtime not available."}))
                continue
            if len(parts) == 1:
                mode = tr.get_approval_mode().value
                picked = _pick_perm_mode_interactive(current_mode=mode)
                if not picked:
                    continue
                raw = picked
            elif parts[1] in {"list", "ls"}:
                mode = tr.get_approval_mode().value
                ui.emit(
                    UIEvent(
                        UIEventKind.LOG,
                        {
                            "level": "policy",
                            "message": (
                                f"Tool approval mode: {mode}\n"
                                "Modes:\n"
                                "  strict   - approval required for every tool call\n"
                                "  standard - approval required only for shell commands\n"
                                "  trusted  - no approvals (dangerous)"
                            ),
                        },
                    )
                )
                continue
            else:
                raw = parts[1].strip().lower()

            desired: ToolApprovalMode | None = None
            if raw in {"strict", "all", "always", "ask_all", "paranoid"}:
                desired = ToolApprovalMode.STRICT
            elif raw in {"standard", "safe", "default", "ask_risky", "risky"}:
                desired = ToolApprovalMode.STANDARD
            elif raw in {"trusted", "auto", "none", "off", "no"}:
                desired = ToolApprovalMode.TRUSTED

            if desired is None:
                ui.emit(
                    UIEvent(
                        UIEventKind.WARNING,
                        {"message": "Usage: /perm [strict|standard|trusted]"},
                    )
                )
                continue

            tr.set_approval_mode(desired)
            try:
                update_session_settings(
                    session_store=orchestrator.session_store,
                    session_id=orchestrator.session_id,
                    model_config=orchestrator.model_config,
                    tool_approval_mode=desired.value,
                )
            except Exception:
                logger.warning(
                    "Failed to persist tool_approval_mode=%s for session_id=%s",
                    desired.value,
                    orchestrator.session_id,
                    exc_info=True,
                )
            ui.emit(UIEvent(UIEventKind.LOG, {"level": "policy", "message": f"Tool approval mode set to: {desired.value}"}))
            continue

        try:
            _handle_pending_approvals_ui(request_id=None)
        except EventLogAppendError as e:
            ui.emit(UIEvent(UIEventKind.ERROR_RAISED, {"code": "event_log", "message": str(e), "recoverable": False}))
            break

        op = Op(
            kind=OpKind.CHAT.value,
            payload={"text": cmd},
            session_id=session_id,
            request_id=new_id("req"),
            timestamp=now_ts_ms(),
            turn_id=new_id("turn"),
        )
        _run_op(op)

        try:
            _handle_pending_approvals_ui(request_id=op.request_id)
        except (KeyboardInterrupt, EventLogAppendError):
            break

    try:
        event_bus.flush()
    except EventLogAppendError as e:
        ui.emit(UIEvent(UIEventKind.ERROR_RAISED, {"code": "event_log", "message": str(e), "recoverable": False}))
        ui.stop()
        return EXIT_CONFIG_ERROR

    ui.stop()
    return EXIT_OK


def _cmd_session_list(_: argparse.Namespace) -> int:
    from .runtime.project import RuntimePaths
    from .runtime.stores import FileSessionStore

    try:
        paths = RuntimePaths.discover()
    except FileNotFoundError as e:
        print(str(e), file=sys.stderr)
        return EXIT_CONFIG_ERROR

    session_store = FileSessionStore(paths.sessions_dir)
    sessions = session_store.list_sessions()
    for meta in sessions:
        sid = meta.get("session_id")
        updated = meta.get("updated_at")
        mode = meta.get("mode")
        print(f"{sid}\tupdated_at={updated}\tmode={mode}")
    return EXIT_OK


def _cmd_session_resume(args: argparse.Namespace) -> int:
    args2 = argparse.Namespace(
        session_id=args.session_id,
        timeout_s=args.timeout_s,
        system_prompt=args.system_prompt,
        enable_tools=getattr(args, "enable_tools", None),
        max_tool_turns=getattr(args, "max_tool_turns", 30),
        color=getattr(args, "color", "auto"),
        plain_input=bool(getattr(args, "plain_input", False)),
    )
    return _cmd_chat(args2)


def _cmd_debug_export(args: argparse.Namespace) -> int:
    from .runtime.project import RuntimePaths
    from .runtime.stores import FileApprovalStore, FileArtifactStore, FileEventLogStore, FileSessionStore

    try:
        paths = RuntimePaths.discover()
    except FileNotFoundError as e:
        print(str(e), file=sys.stderr)
        return EXIT_CONFIG_ERROR

    artifact_store = FileArtifactStore(paths.artifacts_dir)
    session_store = FileSessionStore(paths.sessions_dir)
    approval_store = FileApprovalStore(paths.state_dir / "approvals")
    event_log_store = FileEventLogStore(
        paths.events_dir, artifact_store=artifact_store, session_store=session_store
    )

    output_dir = Path(args.output).expanduser().resolve()
    try:
        bundle_dir = event_log_store.export_bundle(args.session_id, output_dir)
    except FileNotFoundError as e:
        print(str(e), file=sys.stderr)
        return EXIT_CONFIG_ERROR

    try:
        _export_extra_runtime_dirs(
            bundle_dir=bundle_dir,
            config_dir=paths.config_dir,
            policy_dir=paths.policy_dir,
            state_dir=paths.state_dir,
        )
        _export_approval_artifacts(
            session_id=args.session_id,
            approval_store=approval_store,
            artifact_store=artifact_store,
            bundle_artifacts_dir=bundle_dir / "artifacts",
        )
    except Exception as e:
        print(f"Export failed: {e}", file=sys.stderr)
        return EXIT_ERROR

    print(str(bundle_dir))
    return EXIT_OK


def _cmd_debug_validate(args: argparse.Namespace) -> int:
    from .runtime.project import RuntimePaths
    from .runtime.validate import validate_bundle_dir, validate_project_session

    strict = bool(getattr(args, "strict", False))
    target = str(args.target)
    target_path = Path(target).expanduser()
    if target_path.exists():
        issues = validate_bundle_dir(bundle_dir=target_path, strict=strict)
    else:
        try:
            paths = RuntimePaths.discover()
        except FileNotFoundError as e:
            print(str(e), file=sys.stderr)
            return EXIT_CONFIG_ERROR
        issues = validate_project_session(project_root=paths.project_root, session_id=target, strict=strict)

    errors = [i for i in issues if i.severity == "error"]
    for issue in issues:
        stream = sys.stderr if issue.severity == "error" else sys.stdout
        print(issue.render(), file=stream)

    if errors:
        print(f"Validation failed: {len(errors)} error(s), {len(issues) - len(errors)} warning(s).", file=sys.stderr)
        return EXIT_VALIDATION_FAILED
    if issues:
        print(f"Validation passed with {len(issues)} warning(s).", file=sys.stderr)
    else:
        print("OK")
    return EXIT_OK


def _print_replay(
    session_id: str, *, event_log_store: "FileEventLogStore", artifact_store: "FileArtifactStore"
) -> None:
    print(f"--- Replay session {session_id} ---")
    for event in event_log_store.read(session_id):
        if event.kind == EventKind.OPERATION_STARTED.value and event.payload.get("op_kind") == OpKind.CHAT.value:
            ref_raw = event.payload.get("input_ref")
            if isinstance(ref_raw, dict):
                try:
                    text = artifact_store.open_locator(str(ref_raw.get("locator")))
                    rendered = text.decode("utf-8", errors="replace").rstrip()
                    print(f"You: {rendered}")
                except Exception:
                    logger.warning("Failed to render replay user message for session_id=%s", session_id, exc_info=True)
        if event.kind == EventKind.LLM_RESPONSE_COMPLETED.value:
            ref_raw = event.payload.get("output_ref")
            if isinstance(ref_raw, dict):
                try:
                    text = artifact_store.open_locator(str(ref_raw.get("locator")))
                    rendered = text.decode("utf-8", errors="replace").rstrip()
                    print(f"Assistant: {rendered}")
                except Exception:
                    logger.warning("Failed to render replay assistant message for session_id=%s", session_id, exc_info=True)
    print("--- End replay ---")


def _emit_replay_to_ui(
    session_id: str,
    *,
    ui,
    event_log_store: "FileEventLogStore",
    artifact_store: "FileArtifactStore",
) -> None:
    from .ui.console_ui import UIEvent, UIEventKind

    ui.emit(UIEvent(UIEventKind.LOG, {"level": "replay", "message": f"--- Replay session {session_id} ---"}))
    for event in event_log_store.read(session_id):
        if event.kind == EventKind.OPERATION_STARTED.value and event.payload.get("op_kind") == OpKind.CHAT.value:
            ref_raw = event.payload.get("input_ref")
            if isinstance(ref_raw, dict):
                try:
                    text = artifact_store.open_locator(str(ref_raw.get("locator")))
                    rendered = text.decode("utf-8", errors="replace").rstrip()
                    ui.emit(UIEvent(UIEventKind.USER_SUBMITTED, {"text": rendered}))
                except Exception:
                    logger.warning(
                        "Failed to emit replay user message to UI for session_id=%s",
                        session_id,
                        exc_info=True,
                    )
        if event.kind == EventKind.LLM_RESPONSE_COMPLETED.value:
            ref_raw = event.payload.get("output_ref")
            if isinstance(ref_raw, dict):
                try:
                    text = artifact_store.open_locator(str(ref_raw.get("locator")))
                    rendered = text.decode("utf-8", errors="replace").rstrip()
                    ui.emit(UIEvent(UIEventKind.ASSISTANT_DELTA, {"text": rendered}))
                    ui.emit(UIEvent(UIEventKind.ASSISTANT_COMPLETED, {"finish_reason": None}))
                except Exception:
                    logger.warning(
                        "Failed to emit replay assistant message to UI for session_id=%s",
                        session_id,
                        exc_info=True,
                    )
    ui.emit(UIEvent(UIEventKind.LOG, {"level": "replay", "message": "--- End replay ---"}))


def _runtime_event_to_ui_events(event, *, think_parser) -> list:
    """
    Runtime Event -> UIEvent(s).

    This keeps the UI decoupled: only this adapter knows about runtime EventKind payloads.
    """

    from .ui.console_ui import UIEvent, UIEventKind

    out: list[UIEvent] = []
    kind = event.kind
    payload = event.payload if isinstance(event.payload, dict) else {}

    if kind == EventKind.LLM_REQUEST_STARTED.value:
        try:
            think_parser.reset()
        except Exception:
            logger.warning("Failed to reset think parser on LLM request start.", exc_info=True)
        out.append(UIEvent(UIEventKind.LLM_REQUEST_STARTED, {"request_id": event.request_id}))
        return out

    if kind == EventKind.OPERATION_STARTED.value:
        if payload.get("op_kind") == OpKind.COMPACT.value:
            try:
                think_parser.reset()
            except Exception:
                logger.warning("Failed to reset think parser on compaction start.", exc_info=True)
            out.append(
                UIEvent(
                    UIEventKind.LLM_REQUEST_STARTED,
                    {"request_id": event.request_id, "label": "Compacting"},
                )
            )
        return out

    if kind == EventKind.OPERATION_COMPLETED.value:
        if payload.get("op_kind") == OpKind.COMPACT.value:
            out.append(UIEvent(UIEventKind.ASSISTANT_COMPLETED, {"finish_reason": None}))
            out.append(UIEvent(UIEventKind.LOG, {"level": "policy", "message": "Compaction complete."}))
        return out

    if kind == EventKind.LLM_THINKING_DELTA.value:
        delta = str(payload.get("thinking_delta") or "")
        if delta:
            out.append(UIEvent(UIEventKind.THINKING_DELTA, {"text": delta}))
        return out

    if kind == EventKind.LLM_RESPONSE_DELTA.value:
        delta = str(payload.get("text_delta") or "")
        if not delta:
            return out
        try:
            segments = think_parser.feed(delta)
        except Exception:
            segments = [(False, delta)]
        for is_think, text in segments:
            if not text:
                continue
            if is_think:
                out.append(UIEvent(UIEventKind.THINKING_DELTA, {"text": text}))
            else:
                out.append(UIEvent(UIEventKind.ASSISTANT_DELTA, {"text": text}))
        return out

    if kind == EventKind.LLM_RESPONSE_COMPLETED.value:
        out.append(UIEvent(UIEventKind.ASSISTANT_COMPLETED, {"finish_reason": payload.get("finish_reason")}))
        return out

    if kind == EventKind.TOOL_CALL_START.value:
        out.append(
            UIEvent(
                UIEventKind.TOOL_CALL_STARTED,
                {
                    "tool": payload.get("tool_name") or "tool",
                    "call_id": payload.get("tool_call_id"),
                    "summary": payload.get("summary"),
                    "preset": payload.get("preset"),
                    "subagent_run_id": payload.get("subagent_run_id"),
                },
            )
        )
        return out

    if kind == EventKind.TOOL_CALL_END.value:
        out.append(
            UIEvent(
                UIEventKind.TOOL_CALL_COMPLETED,
                {
                    "tool": payload.get("tool_name") or "tool",
                    "call_id": payload.get("tool_call_id"),
                    "summary": payload.get("summary"),
                    "status": payload.get("status"),
                    "duration_ms": payload.get("duration_ms"),
                    "error_code": payload.get("error_code"),
                    "error": payload.get("error"),
                    "details": payload.get("details") if isinstance(payload.get("details"), list) else None,
                    "ok": not bool(payload.get("error")),
                    "preset": payload.get("preset"),
                    "subagent_run_id": payload.get("subagent_run_id"),
                },
            )
        )
        return out

    if kind in {EventKind.SUBAGENT_APPROVER_STARTED.value, EventKind.SUBAGENT_APPROVER_COMPLETED.value}:
        preset = payload.get("preset")
        tool_name = str(payload.get("tool_name") or "tool")

        def _decision_label(raw: object) -> str:
            s = str(raw or "").strip()
            if s == "require_approval":
                return "escalate"
            if s in {"allow", "deny"}:
                return s
            return s or "unknown"

        msg = ""
        if kind == EventKind.SUBAGENT_APPROVER_STARTED.value:
            inspection = payload.get("inspection")
            action_summary = None
            risk = None
            reason = None
            if isinstance(inspection, dict):
                action_summary = inspection.get("action_summary")
                risk = inspection.get("risk_level")
                reason = inspection.get("reason")
            head = str(action_summary or tool_name).strip()
            msg = f"Approver: started — {head}"
            if isinstance(risk, str) and risk.strip():
                msg += f" (risk={risk.strip()})"
            if isinstance(reason, str) and reason.strip():
                msg += f" — {reason.strip()}"
        else:
            after = payload.get("inspection_after")
            action_summary = None
            decision = None
            reason = None
            if isinstance(after, dict):
                action_summary = after.get("action_summary")
                decision = after.get("decision")
                reason = after.get("reason")
            head = str(action_summary or tool_name).strip()
            msg = f"Approver: {_decision_label(decision)} — {head}"
            if isinstance(reason, str) and reason.strip():
                msg += f" — {reason.strip()}"

        out.append(UIEvent(UIEventKind.APPROVER_EVENT, {"preset": preset, "message": msg}))
        return out

    if kind == EventKind.PLAN_UPDATE.value:
        out.append(
            UIEvent(
                UIEventKind.PLAN_UPDATED,
                {
                    "plan_type": payload.get("plan_type"),
                    "plan": payload.get("plan") if isinstance(payload.get("plan"), list) else [],
                    "explanation": payload.get("explanation"),
                    "updated_at": payload.get("updated_at"),
                },
            )
        )
        return out

    if kind == EventKind.OPERATION_PROGRESS.value:
        msg = payload.get("message")
        if msg:
            out.append(UIEvent(UIEventKind.PROGRESS, {"label": str(msg)}))
        return out

    if kind == EventKind.APPROVAL_REQUIRED.value:
        # Interactive approval prompts are handled by the pending-approvals loop to avoid duplicate/noisy output.
        return out

    if kind == EventKind.OPERATION_CANCELLED.value:
        out.append(UIEvent(UIEventKind.ASSISTANT_COMPLETED, {"finish_reason": None}))
        reason = payload.get("reason") or "cancelled"
        out.append(UIEvent(UIEventKind.CANCELLED, {"message": str(reason)}))
        return out

    if kind == EventKind.LLM_REQUEST_FAILED.value:
        msg = payload.get("error") or str(payload)
        code = payload.get("error_code") or payload.get("code") or "llm_request_failed"
        handled = payload.get("handled")
        if handled == "fallback_to_complete":
            out.append(
                UIEvent(
                    UIEventKind.WARNING,
                    {
                        "message": f"{code}: streaming failed, retried without streaming",
                    },
                )
            )
            return out
        details = payload.get("details")
        if code == "timeout" and isinstance(details, dict):
            phase = details.get("phase")
            timeout_s = details.get("timeout_s")
            extras: list[str] = []
            if phase:
                extras.append(f"phase={phase}")
            if timeout_s is not None:
                extras.append(f"timeout_s={timeout_s}")
            if extras:
                msg = f"{msg} ({', '.join(extras)})"
        out.append(
            UIEvent(
                UIEventKind.ERROR_RAISED,
                {
                    "code": code,
                    "message": msg,
                    "recoverable": bool(payload.get("retryable", True)),
                },
            )
        )
        return out

    if kind == EventKind.OPERATION_FAILED.value:
        if payload.get("op_kind") == OpKind.COMPACT.value:
            out.append(UIEvent(UIEventKind.ASSISTANT_COMPLETED, {"finish_reason": None}))
        # Avoid duplicate error lines for LLM failures: LLM_REQUEST_FAILED already surfaced it.
        if payload.get("type") == "llm_request":
            return out
        out.append(
            UIEvent(
                UIEventKind.ERROR_RAISED,
                {
                    "code": payload.get("error_code") or "operation_failed",
                    "message": payload.get("error") or str(payload),
                    "recoverable": False,
                },
            )
        )
        return out

    return out


def _cmd_init(args: argparse.Namespace) -> int:
    from .runtime.skills import seed_builtin_skills

    project_root = Path(args.path).expanduser().resolve()

    if project_root.exists() and not project_root.is_dir():
        print(f"Error: path exists and is not a directory: {project_root}", file=sys.stderr)
        return EXIT_ERROR

    project_root.mkdir(parents=True, exist_ok=True)

    system_dirs = [
        project_root / ".aura" / "config",
        project_root / ".aura" / "policy",
        project_root / ".aura" / "skills",
        project_root / ".aura" / "sessions",
        project_root / ".aura" / "events",
        project_root / ".aura" / "artifacts",
        project_root / ".aura" / "runs",
        project_root / ".aura" / "state",
        project_root / ".aura" / "index",
        project_root / ".aura" / "cache",
        project_root / ".aura" / "tmp",
        project_root / ".aura" / "state" / "approvals",
    ]

    for directory in system_dirs:
        directory.mkdir(parents=True, exist_ok=True)

    models_path = project_root / ".aura" / "config" / "models.json"
    if not models_path.exists():
        models_path.write_text(
            "\n".join(
                [
                    "{",
                    '  "default_profile": "main",',
                    '  "profiles": {',
                    '    "main": {',
                    '      "provider_kind": "openai_compatible",',
                    '      "base_url": "",',
                    '      "model": "your-model-name",',
                    '      "api_key": "",',
                    '      "timeout_s": 60,',
                    '      "limits": { "context_limit_tokens": null, "max_output_tokens": null },',
                    '      "context_management": {',
                    '        "auto_compact_threshold_ratio": null,',
                    '        "history_budget_ratio": 0.2,',
                    '        "history_budget_fallback_tokens": 8000,',
                    '        "tool_output_budget_tokens": 400',
                    "      },",
                    '      "capabilities": { "supports_tools": true, "supports_streaming": true }',
                    "    },",
                    '    "anthropic": {',
                    '      "provider_kind": "anthropic",',
                    '      "base_url": "",',
                    '      "model": "claude-3-5-sonnet-20241022",',
                    '      "api_key": "replace-me",',
                    '      "max_tokens": 1024,',
                    '      "timeout_s": 60,',
                    '      "limits": { "context_limit_tokens": null, "max_output_tokens": null },',
                    '      "context_management": {',
                    '        "auto_compact_threshold_ratio": null,',
                    '        "history_budget_ratio": 0.2,',
                    '        "history_budget_fallback_tokens": 8000,',
                    '        "tool_output_budget_tokens": 400',
                    "      },",
                    '      "capabilities": { "supports_tools": true, "supports_streaming": true }',
                    "    },",
                    '    "gemini": {',
                    '      "provider_kind": "gemini",',
                    '      "base_url": "",',
                    '      "model": "gemini-2.5-flash-lite",',
                    '      "api_key": "replace-me",',
                    '      "timeout_s": 60,',
                    '      "limits": { "context_limit_tokens": null, "max_output_tokens": null },',
                    '      "context_management": {',
                    '        "auto_compact_threshold_ratio": null,',
                    '        "history_budget_ratio": 0.2,',
                    '        "history_budget_fallback_tokens": 8000,',
                    '        "tool_output_budget_tokens": 400',
                    "      },",
                    '      "capabilities": { "supports_tools": true, "supports_streaming": true },',
                    '      "default_params": {',
                    '        "generationConfig": {',
                    '          "thinkingConfig": { "includeThoughts": true, "thinkingBudget": 8192 }',
                    "        }",
                    "      }",
                    "    },",
                    '    "codex": {',
                    '      "provider_kind": "openai_codex",',
                    '      "base_url": "",',
                    '      "model": "gpt-5.3-codex",',
                    '      "api_key": "replace-me",',
                    '      "timeout_s": 60,',
                    '      "limits": { "context_limit_tokens": null, "max_output_tokens": null },',
                    '      "context_management": {',
                    '        "auto_compact_threshold_ratio": null,',
                    '        "history_budget_ratio": 0.2,',
                    '        "history_budget_fallback_tokens": 8000,',
                    '        "tool_output_budget_tokens": 400',
                    "      },",
                    '      "capabilities": { "supports_tools": true, "supports_streaming": true },',
                    '      "default_params": { "reasoning": { "effort": "high" } }',
                    "    }",
                    "  }",
                    "}",
                    "",
                ]
            ),
            encoding="utf-8",
        )

    mcp_path = project_root / ".aura" / "config" / "mcp.json"
    if not mcp_path.exists():
        mcp_path.write_text(
            "\n".join(
                [
                    "{",
                    '  "_comment": "MCP server configuration. Add servers under mcpServers and set enabled=true. Note: Aura treats MCP tools as high-risk by default (approval-gated unless tool approval mode is trusted).",',
                    '  "mcpServers": {',
                    '    "filesystem": {',
                    '      "_comment": "General-purpose filesystem MCP. Prefer Aura built-in project tools when possible. IMPORTANT: restrict allowed directories.",',
                    '      "enabled": false,',
                    '      "command": "npx",',
                    '      "args": ["-y", "@modelcontextprotocol/server-filesystem", "/path/to/allowed/directory"],',
                    '      "env": {},',
                    '      "cwd": "",',
                    '      "timeout_s": 60',
                    "    },",
                    '    "memory": {',
                    '      "_comment": "General-purpose memory MCP (useful for long-lived notes/recall across sessions).",',
                    '      "enabled": false,',
                    '      "command": "npx",',
                    '      "args": ["-y", "@modelcontextprotocol/server-memory"],',
                    '      "env": {},',
                    '      "cwd": "",',
                    '      "timeout_s": 60',
                    "    },",
                    '    "fetch": {',
                    '      "_comment": "Network-capable fetch MCP. Keep disabled unless you explicitly want network access via MCP.",',
                    '      "enabled": false,',
                    '      "command": "uvx",',
                    '      "args": ["mcp-server-fetch"],',
                    '      "env": {},',
                    '      "cwd": "",',
                    '      "timeout_s": 60',
                    "    },",
                    '    "example": {',
                    '      "enabled": false,',
                    '      "command": "",',
                    '      "args": [],',
                    '      "env": {},',
                    '      "cwd": "",',
                    '      "timeout_s": 60',
                    "    }",
                    "  }",
                    "}",
                    "",
                ]
            ),
            encoding="utf-8",
        )

    skipped = seed_builtin_skills(project_root=project_root)
    if skipped:
        print("Skipped seeding existing skills:")
        for path in skipped:
            print(f"  - {path}")

    print(f"Initialized Aura project at {project_root}")
    return EXIT_OK


def main(argv: list[str] | None = None) -> int:
    _configure_text_io()
    parser = _build_parser()
    args = parser.parse_args(argv)

    try:
        func = getattr(args, "func")
        return int(func(args))
    except KeyboardInterrupt:
        print("Interrupted.", file=sys.stderr)
        return 130


def _export_extra_runtime_dirs(*, bundle_dir: Path, config_dir: Path, policy_dir: Path, state_dir: Path) -> None:
    target_root = bundle_dir / ".aura"
    _copy_tree(config_dir, target_root / "config")
    _copy_tree(policy_dir, target_root / "policy")
    _copy_tree(state_dir, target_root / "state")


def _copy_tree(src: Path, dst: Path) -> None:
    if not src.exists():
        return
    if src.is_dir():
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(src, dst, dirs_exist_ok=True)
        return
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(src, dst)


def _export_approval_artifacts(
    *,
    session_id: str,
    approval_store: FileApprovalStore,
    artifact_store: FileArtifactStore,
    bundle_artifacts_dir: Path,
) -> None:
    bundle_artifacts_dir.mkdir(parents=True, exist_ok=True)
    approvals = approval_store.list(session_id=session_id, status=None, request_id=None)
    seen: set[str] = set()
    for rec in approvals:
        for ref in _iter_artifact_refs(rec.to_dict()):
            if ref.artifact_id in seen:
                continue
            seen.add(ref.artifact_id)
            src = artifact_store.resolve_path(ref)
            dst = bundle_artifacts_dir / Path(ref.locator).name
            if dst.exists():
                continue
            if src.exists() and src.is_file():
                shutil.copyfile(src, dst)


def _iter_artifact_refs(value: object) -> list[ArtifactRef]:
    out: list[ArtifactRef] = []
    required = {"artifact_id", "artifact_kind", "locator", "created_at"}
    if isinstance(value, dict):
        if required <= set(value.keys()):
            try:
                out.append(ArtifactRef.from_dict(value))
            except Exception:
                logger.warning("Failed to parse artifact reference while exporting approval artifacts.", exc_info=True)
        for v in value.values():
            out.extend(_iter_artifact_refs(v))
        return out
    if isinstance(value, list):
        for item in value:
            out.extend(_iter_artifact_refs(item))
        return out
    return out
