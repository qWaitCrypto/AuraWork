from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path


class SnapshotError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class SnapshotResult:
    commit: str
    label: str | None = None


class GitSnapshotBackend:
    """
    Internal Git-backed snapshot backend.

    Uses an internal git-dir under `.aura/state/git` and always calls git with
    explicit `--git-dir` and `--work-tree` to avoid touching any user `.git`.
    """

    def __init__(self, *, project_root: Path, git_dir: Path | None = None) -> None:
        self._project_root = project_root.expanduser().resolve()
        self._git_dir = (git_dir or (self._project_root / ".aura" / "state" / "git")).expanduser().resolve()

    @property
    def git_dir(self) -> Path:
        return self._git_dir

    def is_initialized(self) -> bool:
        return (self._git_dir / "HEAD").exists()

    def list_labels(self, *, max_results: int = 50) -> list[dict[str, str | None]]:
        if max_results < 1:
            raise ValueError("max_results must be >= 1.")
        if not self.is_initialized():
            return []
        text = self._git_stdout(
            "for-each-ref",
            "--sort=-committerdate",
            "--format=%(refname:short)\t%(objectname)\t%(committerdate:iso8601)\t%(subject)",
            "refs/tags",
        )
        out: list[dict[str, str | None]] = []
        for line in text.splitlines():
            if not line.strip():
                continue
            parts = line.split("\t")
            label = parts[0].strip() if len(parts) > 0 else ""
            commit = parts[1].strip() if len(parts) > 1 else ""
            committed_at = parts[2].strip() if len(parts) > 2 else ""
            subject = parts[3].strip() if len(parts) > 3 else ""
            if not label:
                continue
            out.append(
                {
                    "label": label,
                    "commit": commit or None,
                    "committed_at": committed_at or None,
                    "subject": subject or None,
                }
            )
            if len(out) >= max_results:
                break
        return out

    def read_text(self, *, ref: str, path: str) -> str:
        ref = ref.strip()
        if not ref:
            raise ValueError("Missing ref.")
        path = path.strip().replace("\\", "/").lstrip("/")
        if not path:
            raise ValueError("Missing path.")
        if any(part in {"..", ""} for part in Path(path).parts):
            raise ValueError("Invalid path.")
        if not self.is_initialized():
            raise SnapshotError("No snapshots yet.")
        return self._git_stdout("show", f"{ref}:{path}")

    def diff(self, *, a: str, b: str, path: str | None = None) -> str:
        a = a.strip()
        b = b.strip()
        if not a or not b:
            raise ValueError("Missing ref(s) for diff.")
        if not self.is_initialized():
            raise SnapshotError("No snapshots yet.")
        args: list[str] = ["diff", a, b]
        if isinstance(path, str) and path.strip():
            normalized = path.strip().replace("\\", "/").lstrip("/")
            if any(part in {"..", ""} for part in Path(normalized).parts):
                raise ValueError("Invalid path.")
            args += ["--", normalized]
        return self._git_stdout(*args)

    def snapshot_create(self, *, reason: str) -> SnapshotResult:
        self._ensure_repo()
        self._ensure_excludes()
        self._git("add", "-A")
        msg = f"snapshot: {reason}".strip()
        if not msg:
            msg = "snapshot"
        self._git("commit", "-m", msg, "--allow-empty")
        commit = self._git_stdout("rev-parse", "HEAD").strip()
        return SnapshotResult(commit=commit)

    def snapshot_label(self, *, label: str) -> SnapshotResult:
        self._ensure_repo()
        label = label.strip()
        if not label:
            raise SnapshotError("Missing label.")
        self._git("tag", "-f", label)
        commit = self._git_stdout("rev-parse", "HEAD").strip()
        return SnapshotResult(commit=commit, label=label)

    def snapshot_diff(self, *, a: str, b: str) -> str:
        self._ensure_repo()
        return self._git_stdout("diff", a, b)

    def snapshot_rollback(self, *, target: str) -> None:
        self._ensure_repo()
        self._git("reset", "--hard", target)

    def create_worktree(self, *, task_id: str, node_id: str) -> Path:
        """
        Create an isolated worktree for a specific DAG node (design doc §10.2).

        Returns:
            Path to the worktree: .aura/worktrees/<task_id>/<node_id>
        """
        self._ensure_repo()
        self._ensure_excludes()

        safe_task_id = self._sanitize_component(task_id)
        safe_node_id = self._sanitize_component(node_id)

        wt_path = self._project_root / ".aura" / "worktrees" / safe_task_id / safe_node_id
        if wt_path.exists():
            raise SnapshotError(f"Worktree already exists: {wt_path}")

        wt_path.parent.mkdir(parents=True, exist_ok=True)
        branch = f"node/{safe_task_id}_{safe_node_id}"

        self._git("worktree", "add", str(wt_path), "-b", branch, "HEAD")
        return wt_path

    def remove_worktree(self, *, task_id: str, node_id: str) -> None:
        """Remove a node worktree and clean up the corresponding branch."""
        if not self.is_initialized():
            return

        safe_task_id = self._sanitize_component(task_id)
        safe_node_id = self._sanitize_component(node_id)
        wt_path = self._project_root / ".aura" / "worktrees" / safe_task_id / safe_node_id
        branch = f"node/{safe_task_id}_{safe_node_id}"

        try:
            self._git("worktree", "remove", "--force", str(wt_path))
        except SnapshotError:
            pass

        try:
            self._git("branch", "-D", branch)
        except SnapshotError:
            pass

    def list_worktrees(self) -> list[dict[str, str | None]]:
        """List worktrees for the internal snapshot repo."""
        if not self.is_initialized():
            return []

        output = self._git_stdout("worktree", "list", "--porcelain")
        worktrees: list[dict[str, str | None]] = []
        current: dict[str, str | None] = {}

        for line in output.splitlines():
            if not line.strip():
                continue
            if line.startswith("worktree "):
                if current:
                    worktrees.append(current)
                current = {"path": line[9:]}
                continue
            if line.startswith("branch "):
                current["branch"] = line[7:]
                continue
            if line.startswith("HEAD "):
                current["head"] = line[5:]
                continue
            if line == "bare":
                current["bare"] = "true"
                continue

        if current:
            worktrees.append(current)

        return worktrees

    def worktree_commit(self, *, task_id: str, node_id: str, message: str) -> str:
        """Commit all changes in a node worktree and return the new commit hash."""
        if not self.is_initialized():
            raise SnapshotError("No snapshots yet.")

        safe_task_id = self._sanitize_component(task_id)
        safe_node_id = self._sanitize_component(node_id)
        wt_path = self._project_root / ".aura" / "worktrees" / safe_task_id / safe_node_id
        if not wt_path.exists():
            raise SnapshotError(f"Worktree not found: {wt_path}")

        msg = message.strip()
        if not msg:
            msg = "worktree commit"

        env = dict(os.environ)
        env.update({"GIT_TERMINAL_PROMPT": "0"})
        try:
            add_proc = subprocess.run(
                ["git", "-C", str(wt_path), "add", "-A"],
                check=False,
                capture_output=True,
                text=True,
                env=env,
            )
            if add_proc.returncode != 0:
                stderr = add_proc.stderr.strip() if add_proc.stderr else ""
                raise SnapshotError(f"git failed ({add_proc.returncode}): add -A\n{stderr}".rstrip())
            proc = subprocess.run(
                ["git", "-C", str(wt_path), "commit", "-m", msg, "--allow-empty"],
                check=False,
                capture_output=True,
                text=True,
                env=env,
            )
        except FileNotFoundError as e:
            raise SnapshotError("git executable not found.") from e
        if proc.returncode != 0:
            stderr = proc.stderr.strip() if proc.stderr else ""
            raise SnapshotError(f"git failed ({proc.returncode}): commit\n{stderr}".rstrip())

        proc = subprocess.run(
            ["git", "-C", str(wt_path), "rev-parse", "HEAD"],
            check=False,
            capture_output=True,
            text=True,
            env=env,
        )
        if proc.returncode != 0:
            stderr = proc.stderr.strip() if proc.stderr else ""
            raise SnapshotError(f"git failed ({proc.returncode}): rev-parse HEAD\n{stderr}".rstrip())
        return (proc.stdout or "").strip()

    def _ensure_repo(self) -> None:
        self._git_dir.mkdir(parents=True, exist_ok=True)
        head = self._git_dir / "HEAD"
        if not head.exists():
            self._git("init")
            self._git("config", "user.name", "Aura")
            self._git("config", "user.email", "aura@local")
        self._ensure_excludes()

    def _ensure_excludes(self) -> None:
        info = self._git_dir / "info"
        info.mkdir(parents=True, exist_ok=True)
        exclude = info / "exclude"
        patterns = [
            ".git",
            ".aura/state/git/",
            ".aura/cache/",
            ".aura/index/",
            ".aura/tmp/",
            ".aura/events/",
            ".aura/sessions/",
            ".aura/artifacts/",
            ".aura/worktrees/",
        ]
        existing = ""
        if exclude.exists():
            try:
                existing = exclude.read_text(encoding="utf-8", errors="replace")
            except OSError:
                existing = ""
        lines = [ln.strip() for ln in existing.splitlines() if ln.strip()]
        changed = False
        for pat in patterns:
            if pat not in lines:
                lines.append(pat)
                changed = True
        if changed or not exclude.exists():
            exclude.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def _sanitize_component(self, value: str) -> str:
        v = value.strip()
        if not v:
            raise SnapshotError("Invalid id: empty.")
        out: list[str] = []
        for ch in v:
            if ch.isalnum() or ch in {".", "_", "-"}:
                out.append(ch)
            else:
                out.append("-")
        sanitized = "".join(out).strip("-")
        if not sanitized or sanitized in {".", ".."}:
            raise SnapshotError(f"Invalid id: {value!r}")
        return sanitized

    def _git(self, *args: str) -> None:
        self._run_git(args, capture=False)

    def _git_stdout(self, *args: str) -> str:
        return self._run_git(args, capture=True)

    def _run_git(self, args: tuple[str, ...] | list[str], *, capture: bool) -> str:
        cmd = ["git", f"--git-dir={self._git_dir}", f"--work-tree={self._project_root}", *args]
        env = dict(os.environ)
        env.update({"GIT_TERMINAL_PROMPT": "0"})
        try:
            proc = subprocess.run(
                cmd,
                check=False,
                capture_output=True,
                text=True,
                env=env,
            )
        except FileNotFoundError as e:
            raise SnapshotError("git executable not found.") from e
        if proc.returncode != 0:
            stderr = proc.stderr.strip() if proc.stderr else ""
            raise SnapshotError(f"git failed ({proc.returncode}): {' '.join(args)}\n{stderr}".rstrip())
        if capture:
            return proc.stdout or ""
        return ""
