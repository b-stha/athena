import contextlib
import hashlib
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import URLError

from athena_ovos import setup


class ModelSetupTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.destination = setup.resolve_model_path(self.root)
        self.data = b"verified model bytes"
        self.digest = hashlib.sha256(self.data).hexdigest()

    def test_download_is_verified_before_replacing_old_model(self):
        self.destination.parent.mkdir(parents=True)
        self.destination.write_bytes(b"old model")
        with patch.object(setup, "urlopen", return_value=io.BytesIO(self.data)) as request:
            model = setup.download_model(self.destination, "https://example.test/model", self.digest)
        self.assertEqual(model, self.destination)
        self.assertEqual(model.read_bytes(), self.data)
        request.assert_called_once_with("https://example.test/model", timeout=30)
        self.assertEqual(list(model.parent.iterdir()), [model])

    def test_checksum_failure_preserves_old_model_and_removes_partial_file(self):
        self.destination.parent.mkdir(parents=True)
        self.destination.write_bytes(b"old model")
        with patch.object(setup, "urlopen", return_value=io.BytesIO(b"wrong model")):
            with self.assertRaisesRegex(ValueError, "checksum"):
                setup.download_model(self.destination, expected_sha256=self.digest)
        self.assertEqual(self.destination.read_bytes(), b"old model")
        self.assertEqual(list(self.destination.parent.iterdir()), [self.destination])

    def test_valid_cached_model_does_not_require_network(self):
        self.destination.parent.mkdir(parents=True)
        self.destination.write_bytes(self.data)
        with patch.object(setup, "urlopen") as request:
            self.assertEqual(setup.download_model(self.destination, expected_sha256=self.digest), self.destination)
        request.assert_not_called()

    def test_network_failure_leaves_no_model(self):
        with patch.object(setup, "urlopen", side_effect=URLError("offline")):
            with self.assertRaises(URLError):
                setup.download_model(self.destination)
        self.assertFalse(self.destination.exists())
        self.assertEqual(list(self.destination.parent.iterdir()), [])

    def test_interrupted_download_preserves_old_model_and_cleans_up(self):
        class InterruptedResponse(io.BytesIO):
            def read(self, size=-1):
                if self.tell():
                    raise OSError("download interrupted")
                return super().read(size)

        self.destination.parent.mkdir(parents=True)
        self.destination.write_bytes(b"old model")
        with patch.object(setup, "urlopen", return_value=InterruptedResponse(self.data)):
            with self.assertRaisesRegex(OSError, "interrupted"):
                setup.download_model(self.destination, expected_sha256=self.digest)
        self.assertEqual(self.destination.read_bytes(), b"old model")
        self.assertEqual(list(self.destination.parent.iterdir()), [self.destination])

    def test_invalid_checksum_is_rejected_before_download(self):
        with patch.object(setup, "urlopen") as request:
            with self.assertRaisesRegex(ValueError, "SHA256"):
                setup.download_model(self.destination, expected_sha256="invalid")
        request.assert_not_called()
        self.assertFalse(self.destination.parent.exists())

    def test_existing_custom_model_is_used_without_copying(self):
        custom = self.root / "custom.onnx"
        custom.write_bytes(self.data)
        output = io.StringIO()
        with patch.object(setup, "download_model") as download, contextlib.redirect_stdout(output):
            status = setup.main(["--root", str(self.root), "--model", str(custom)])
        self.assertEqual(status, 0)
        self.assertEqual(output.getvalue().strip(), str(custom.resolve()))
        self.assertFalse(self.destination.exists())
        download.assert_not_called()

    def test_missing_or_empty_custom_model_returns_setup_error(self):
        custom = self.root / "custom.onnx"
        for empty in (False, True):
            with self.subTest(empty=empty):
                if empty:
                    custom.touch()
                error = io.StringIO()
                with contextlib.redirect_stderr(error):
                    status = setup.main(["--model", str(custom)])
                self.assertEqual(status, 1)
                self.assertIn("existing nonempty file", error.getvalue())


if __name__ == "__main__":
    unittest.main()
