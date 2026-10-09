"""Local channels: a directory of markdown files, or stdout.

These exist so the whole pipeline can be exercised end to end without a real
webhook, and so `--dry-run` has somewhere honest to put its output.
"""

from __future__ import annotations

import re
import sys
from datetime import date
from pathlib import Path
from typing import Any

import httpx

from .base import Channel, DeliveryError

UNSAFE = re.compile(r"[^A-Za-z0-9._]+")


def slugify(subject: str) -> str:
    return UNSAFE.sub("-", subject).strip("-").lower()[:80] or "digest"


class FileChannel(Channel):
    kind = "file"

    def send(self, subject: str, markdown: str, *, client: httpx.Client | None = None) -> dict[str, Any]:
        directory = Path(self.config.path)
        stem = f"{date.today().isoformat()}-{slugify(subject)}"
        try:
            directory.mkdir(parents=True, exist_ok=True)
            target = self._free_path(directory, stem)
            target.write_text(f"# {subject}\n\n{markdown}\n", encoding="utf-8")
        except OSError as exc:
            raise DeliveryError(
                f"could not write digest to {directory}: {exc}", retryable=False
            ) from exc
        return {"path": str(target.resolve()), "bytes": target.stat().st_size}

    @staticmethod
    def _free_path(directory: Path, stem: str) -> Path:
        """Never overwrite: two alerts on one day must both survive."""
        candidate = directory / f"{stem}.md"
        suffix = 2
        while candidate.exists():
            candidate = directory / f"{stem}-{suffix}.md"
            suffix += 1
        return candidate


class StdoutChannel(Channel):
    kind = "stdout"

    def send(self, subject: str, markdown: str, *, client: httpx.Client | None = None) -> dict[str, Any]:
        rule = "=" * min(len(subject), 72)
        try:
            # The digest is data, so it goes to stdout; logs go to stderr.
            sys.stdout.write(f"\n{subject}\n{rule}\n{markdown}\n")
            sys.stdout.flush()
        except OSError as exc:
            raise DeliveryError(f"could not write digest to stdout: {exc}", retryable=False) from exc
        return {"written_chars": len(markdown)}
