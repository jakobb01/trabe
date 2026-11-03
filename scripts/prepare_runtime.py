#!/usr/bin/env python3
"""Verify/extract the pinned pure-Python dependency for copy-only ARMv7 builds."""

import hashlib
import os
import shutil
import tempfile
import zipfile
from pathlib import Path
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]
VERSION = "1.9.2"
SHA256 = "e1a673830a9c7bfa47b1cd3d5e4178f4c9651d80a4eab02c9c23a1c3ec6250ce"
TREE_SHA256 = "32d816b4e2c7e8018628977de2d9fa1fa8c2778773e3c5d2b8f3bbfee342bd23"
URL = "https://files.pythonhosted.org/packages/d5/d2/cc4dc1271e464942db7ee278baae2daa99ee77cb2af744025c04da585a3e/websocket_client-1.9.2-py3-none-any.whl"


def prepare(root: Path = ROOT) -> Path:
    destination = root / "deploy/runtime"
    digest = hashlib.sha256()
    for path in sorted(p for p in destination.rglob("*") if p.is_file()):
        digest.update(
            str(path.relative_to(destination)).encode()
            + b"\0"
            + hashlib.sha256(path.read_bytes()).digest()
        )
    if digest.hexdigest() == TREE_SHA256:
        return destination
    cache = root / ".local/tooling/wheels"
    cache.mkdir(parents=True, exist_ok=True)
    wheel = cache / f"websocket_client-{VERSION}-py3-none-any.whl"
    if not wheel.exists() or hashlib.sha256(wheel.read_bytes()).hexdigest() != SHA256:
        with urlopen(URL, timeout=30) as response:
            payload = response.read(2_000_001)
        if len(payload) > 2_000_000 or hashlib.sha256(payload).hexdigest() != SHA256:
            raise ValueError("WebSocket dependency checksum mismatch.")
        wheel.write_bytes(payload)
    with zipfile.ZipFile(wheel) as archive:
        entries = [item for item in archive.infolist() if not item.is_dir()]
        for item in entries:
            path = Path(item.filename)
            if (
                path.is_absolute()
                or ".." in path.parts
                or path.parts[0] not in ("websocket", f"websocket_client-{VERSION}.dist-info")
                or (item.external_attr >> 16) & 0o170000 == 0o120000
            ):
                raise ValueError("Unexpected file in WebSocket dependency wheel.")
        if (
            destination.is_dir()
            and all(
                (destination / item.filename).is_file()
                and (destination / item.filename).read_bytes() == archive.read(item)
                for item in entries
            )
            and len(list(p for p in destination.rglob("*") if p.is_file())) == len(entries)
        ):
            return destination
        destination.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="runtime-", dir=destination.parent) as temporary:
            stage = Path(temporary) / "files"
            for item in entries:
                target = stage / item.filename
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(archive.read(item))
                target.chmod(0o644)
            if destination.exists():
                shutil.rmtree(destination)
            os.replace(stage, destination)
    return destination


if __name__ == "__main__":
    try:
        print(prepare())
    except Exception:
        raise SystemExit(
            "Could not prepare the verified WebSocket runtime; check network/tool cache."
        ) from None
