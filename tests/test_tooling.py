import hashlib
import io
import json
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import fetch_quadlet


class ToolPreparationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        (self.root / "deploy").mkdir()
        self.package = b"verified-debian-package-fixture"
        self.binary = b"verified-generator-fixture"
        self.metadata = {
            "package_url": "https://example.invalid/tool.deb",
            "package_sha256": hashlib.sha256(self.package).hexdigest(),
            "binary_sha256": hashlib.sha256(self.binary).hexdigest(),
            "member": "./usr/libexec/podman/quadlet",
        }
        self.destination = self.root / "tools/quadlet"
        self.write_lock()
        self.root_patch = patch.object(fetch_quadlet, "ROOT", self.root)
        self.root_patch.start()
        self.addCleanup(self.root_patch.stop)

    def write_lock(self):
        (self.root / "deploy/tools-lock.json").write_text(
            json.dumps({"quadlet": {"armhf": self.metadata}})
        )

    def process(self, *, symlink=False, binary=None):
        stream = io.BytesIO()
        with tarfile.open(fileobj=stream, mode="w") as archive:
            # An unrelated path-traversal entry must never be extracted.
            bad = tarfile.TarInfo("../../outside")
            bad.size = 4
            archive.addfile(bad, io.BytesIO(b"nope"))
            entry = tarfile.TarInfo(self.metadata["member"])
            if symlink:
                entry.type = tarfile.SYMTYPE
                entry.linkname = "/tmp/untrusted"
                archive.addfile(entry)
            else:
                content = self.binary if binary is None else binary
                entry.size = len(content)
                archive.addfile(entry, io.BytesIO(content))
        stream.seek(0)

        class Process:
            stdout = stream

            def wait(self, **kwargs):
                return 0

            def poll(self):
                return 0

        return Process()

    def test_verified_package_extracts_only_pinned_regular_binary(self):
        with (
            patch.object(fetch_quadlet, "urlopen", return_value=io.BytesIO(self.package)),
            patch.object(fetch_quadlet.subprocess, "Popen", return_value=self.process()),
        ):
            result = fetch_quadlet.fetch("armhf", self.destination)
        self.assertEqual(result.read_bytes(), self.binary)
        self.assertEqual(result.stat().st_mode & 0o777, 0o700)
        self.assertFalse((self.root / "outside").exists())
        self.assertEqual(list(self.destination.parent.glob("quadlet-download-*")), [])

    def test_correct_cached_binary_needs_no_network(self):
        self.destination.parent.mkdir()
        self.destination.write_bytes(self.binary)
        with patch.object(fetch_quadlet, "urlopen") as network:
            self.assertEqual(fetch_quadlet.fetch("armhf", self.destination), self.destination)
        network.assert_not_called()

    def test_corrupt_package_is_rejected_before_extraction(self):
        with (
            patch.object(fetch_quadlet, "urlopen", return_value=io.BytesIO(b"corrupt")),
            patch.object(fetch_quadlet.subprocess, "Popen") as extractor,
        ):
            with self.assertRaisesRegex(ValueError, "package checksum"):
                fetch_quadlet.fetch("armhf", self.destination)
        extractor.assert_not_called()
        self.assertFalse(self.destination.exists())

    def test_corrupt_binary_does_not_replace_an_existing_file(self):
        self.destination.parent.mkdir()
        self.destination.write_bytes(b"old file")
        with (
            patch.object(fetch_quadlet, "urlopen", return_value=io.BytesIO(self.package)),
            patch.object(
                fetch_quadlet.subprocess, "Popen", return_value=self.process(binary=b"corrupt")
            ),
        ):
            with self.assertRaisesRegex(ValueError, "binary checksum"):
                fetch_quadlet.fetch("armhf", self.destination)
        self.assertEqual(self.destination.read_bytes(), b"old file")

    def test_symlink_generator_is_rejected(self):
        with (
            patch.object(fetch_quadlet, "urlopen", return_value=io.BytesIO(self.package)),
            patch.object(
                fetch_quadlet.subprocess, "Popen", return_value=self.process(symlink=True)
            ),
        ):
            with self.assertRaisesRegex(ValueError, "archive entry"):
                fetch_quadlet.fetch("armhf", self.destination)
        self.assertFalse(self.destination.exists())


if __name__ == "__main__":
    unittest.main()
