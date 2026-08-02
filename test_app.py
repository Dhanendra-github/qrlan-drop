import tempfile
import threading
import unittest
import urllib.request
from pathlib import Path

from app import TransferHTTPServer, TransferSession, available_path, readable_size, safe_filename


class HelpersTest(unittest.TestCase):
    def test_safe_filename_removes_paths_and_windows_illegal_chars(self):
        self.assertEqual(safe_filename("..%2F..%2Fmy%3Abad%3Ffile.txt"), "mybadfile.txt")

    def test_empty_filename_gets_fallback(self):
        self.assertEqual(safe_filename("..."), "received-file")

    def test_available_path_does_not_overwrite(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "photo.jpg").write_bytes(b"x")
            self.assertEqual(available_path(root, "photo.jpg").name, "photo (2).jpg")

    def test_large_sizes_are_readable(self):
        self.assertEqual(readable_size(10_485_760), "10.00 MB")
        self.assertEqual(readable_size(5 * 1024**3), "5.00 GB")


class TransferTest(unittest.TestCase):
    def test_upload_larger_than_old_limit_is_accepted(self):
        with tempfile.TemporaryDirectory() as folder:
            destination = Path(folder)
            events = []
            session = TransferSession("receive", "test-token", receive_folder=destination)
            server = TransferHTTPServer(("127.0.0.1", 0), session, lambda *args: events.append(args))
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                payload = b"x" * (10 * 1024 * 1024 + 1)
                request = urllib.request.Request(
                    f"http://127.0.0.1:{server.server_address[1]}/t/test-token/upload",
                    data=payload,
                    method="POST",
                    headers={"Content-Type": "application/octet-stream", "X-Filename": "large.bin"},
                )
                with urllib.request.urlopen(request, timeout=10) as response:
                    self.assertEqual(response.status, 201)
                self.assertEqual((destination / "large.bin").stat().st_size, len(payload))
                self.assertTrue(any(event[0].startswith("Received:") for event in events))
            finally:
                server.shutdown()
                server.server_close()


if __name__ == "__main__":
    unittest.main()
