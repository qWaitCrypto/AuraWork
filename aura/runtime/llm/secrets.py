from __future__ import annotations

import os
from pathlib import Path
import json

from .errors import CredentialResolutionError
from .types import CredentialRef


def resolve_credential(credential_ref: CredentialRef) -> str:
    if credential_ref.kind == "env":
        value = os.environ.get(credential_ref.identifier)
        if not value:
            raise CredentialResolutionError(
                f"Missing required environment variable '{credential_ref.identifier}'.",
                credential_ref=credential_ref.to_redacted_string(),
            )
        return value

    if credential_ref.kind in {"inline", "plaintext"}:
        if not credential_ref.identifier:
            raise CredentialResolutionError(
                "Missing inline credential value.",
                credential_ref=credential_ref.to_redacted_string(),
            )
        return credential_ref.identifier

    if credential_ref.kind == "codex_cli":
        auth_path = _codex_auth_json_path(credential_ref.identifier)
        try:
            raw = Path(auth_path).read_text(encoding="utf-8")
        except FileNotFoundError as e:
            raise CredentialResolutionError(
                f"Missing Codex auth.json at {auth_path!r}.",
                credential_ref=credential_ref.to_redacted_string(),
            ) from e
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as e:
            raise CredentialResolutionError(
                f"Invalid JSON in Codex auth.json at {auth_path!r}.",
                credential_ref=credential_ref.to_redacted_string(),
            ) from e
        tokens = data.get("tokens")
        if not isinstance(tokens, dict):
            raise CredentialResolutionError(
                f"Missing 'tokens' object in Codex auth.json at {auth_path!r}.",
                credential_ref=credential_ref.to_redacted_string(),
            )
        access_token = tokens.get("access_token")
        if not isinstance(access_token, str) or not access_token.strip():
            raise CredentialResolutionError(
                f"Missing 'tokens.access_token' in Codex auth.json at {auth_path!r}.",
                credential_ref=credential_ref.to_redacted_string(),
            )
        return access_token.strip()

    raise CredentialResolutionError(
        f"Unsupported credential_ref kind '{credential_ref.kind}'.",
        credential_ref=credential_ref.to_redacted_string(),
    )


def _codex_auth_json_path(identifier: str) -> str:
    # Allow explicit override to point at a specific auth.json path.
    if isinstance(identifier, str) and identifier.strip():
        return identifier.strip()
    codex_home = os.environ.get("CODEX_HOME")
    if codex_home:
        return str(Path(codex_home) / "auth.json")
    return str(Path.home() / ".codex" / "auth.json")
