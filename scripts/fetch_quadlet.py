#!/usr/bin/env python3
"""Fetch one pinned Debian generator, without installing/upgrading Podman.

The resulting binary is an optional, app-scoped compatibility tool for the ARMv7
Bookworm Pi. It is not committed or copied into the application image.
"""

import argparse
import hashlib
import json
import os
import subprocess
import tarfile
import tempfile
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]


def digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def fetch(architecture: str, destination: Path) -> Path:
    lock = json.loads((ROOT / "deploy/tools-lock.json").read_text())["quadlet"][architecture]
    if destination.is_file() and digest(destination) == lock["binary_sha256"]:
        return destination
    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix="quadlet-download-", dir=destination.parent
    ) as temporary:
        package = Path(temporary) / "podman.deb"
        with urlopen(lock["package_url"], timeout=30) as source, package.open("wb") as target:
            total = 0
            while chunk := source.read(1024 * 1024):
                total += len(chunk)
                if total > 64 * 1024 * 1024:
                    raise ValueError("Tool package exceeds size limit.")
                target.write(chunk)
        if digest(package) != lock["package_sha256"]:
            raise ValueError("Quadlet package checksum mismatch; installation refused.")
        candidate = Path(temporary) / "quadlet"
        with tempfile.TemporaryFile() as errors:
            process = subprocess.Popen(
                ["dpkg-deb", "--fsys-tarfile", str(package)], stdout=subprocess.PIPE, stderr=errors
            )
            try:
                with tarfile.open(fileobj=process.stdout, mode="r|") as archive:
                    for member in archive:
                        if member.name == lock["member"]:
                            if not member.isfile() or member.size > 20 * 1024 * 1024:
                                raise ValueError("Unexpected generator archive entry.")
                            candidate.write_bytes(archive.extractfile(member).read())
                if process.wait(timeout=30) != 0:
                    raise ValueError("Could not read the Debian tool package.")
            finally:
                if process.stdout:
                    process.stdout.close()
                if process.poll() is None:
                    process.kill()
                    process.wait()
        if not candidate.exists() or digest(candidate) != lock["binary_sha256"]:
            raise ValueError("Quadlet binary checksum mismatch; installation refused.")
        candidate.chmod(0o700)
        os.replace(candidate, destination)
    return destination


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--architecture", choices=["armhf"], default="armhf")
    parser.add_argument(
        "--output", type=Path, default=ROOT / ".local/tooling/quadlet/armhf/quadlet"
    )
    args = parser.parse_args()
    try:
        print(fetch(args.architecture, args.output))
        return 0
    except (OSError, ValueError, URLError, subprocess.SubprocessError, tarfile.TarError):
        parser.exit(
            1, "Could not prepare the verified Quadlet tool. Check network access and dpkg-deb.\n"
        )


if __name__ == "__main__":
    raise SystemExit(main())
