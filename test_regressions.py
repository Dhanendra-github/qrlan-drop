"""Regression tests for cancellation, failure reporting and session destinations."""
import errno
import http.client
import json
import socket
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app import CHUNK_SIZE, QRLANApp, TransferHTTPServer, TransferService, TransferSession


class HTTPFixture:
    def __init__(self, folder, mode='receive', notify=None):
        self.events = []
        self.errors = []
        self.session = TransferSession(mode, 'regression-token', file_path=folder / 'source.bin', receive_folder=folder)
        owner = self
        class CheckedServer(TransferHTTPServer):
            def handle_error(self, *_args):
                import traceback
                owner.errors.append(traceback.format_exc())
        self.server = CheckedServer(('127.0.0.1', 0), self.session, notify or (lambda *args: self.events.append(args)))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def connection(self):
        return http.client.HTTPConnection('127.0.0.1', self.server.server_address[1], timeout=3)

    def request(self, method, tail='', body=None, headers=None):
        connection = self.connection()
        try:
            connection.request(method, '/t/regression-token/' + tail, body, headers or {})
            response = connection.getresponse()
            return response.status, response.read()
        finally:
            connection.close()

    def close(self):
        self.server.cancel_active()
        self.server.shutdown()
        self.server.server_close()
        if not self.server.wait_for_transfers(3):
            raise AssertionError('Transfer workers did not exit')


class UploadRegressions(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.folder = Path(self.temp.name)
        self.fixture = HTTPFixture(self.folder)

    def tearDown(self):
        self.fixture.close()
        self.assertEqual(self.fixture.errors, [])
        self.temp.cleanup()

    def upload(self, data=b'payload', name='file.txt'):
        return self.fixture.request('POST', 'upload', data, {'X-Filename': name})

    def test_interrupted_upload_reports_failure_and_removes_partial_files(self):
        conn = self.fixture.connection()
        conn.connect()
        conn.putrequest('POST', '/t/regression-token/upload')
        conn.putheader('X-Filename', 'interrupted.txt')
        conn.putheader('Content-Length', '100000')
        conn.endheaders()
        conn.send(b'short')
        conn.sock.shutdown(socket.SHUT_WR)
        response = conn.getresponse()
        self.assertEqual(response.status, 400)
        self.assertIn('interrupted', json.loads(response.read())['error'])
        conn.close()
        self.assertEqual(list(self.folder.iterdir()), [])
        self.assertTrue(any(e[0].startswith('Upload failed:') for e in self.fixture.events))
        self.assertFalse(any(e[0].startswith('Received:') for e in self.fixture.events))

    def test_unwritable_destination_returns_actionable_error(self):
        with patch.object(Path, 'touch', side_effect=PermissionError('read only')):
            status, body = self.upload()
        self.assertEqual(status, 403)
        self.assertIn('folder', json.loads(body)['error'])
        self.assertTrue(any(e[0].startswith('Upload failed:') for e in self.fixture.events))
        self.assertEqual(list(self.folder.iterdir()), [])

    def test_folder_creation_failure_is_handled(self):
        with patch.object(Path, 'mkdir', side_effect=PermissionError('no access')):
            status, body = self.upload()
        self.assertEqual(status, 403)
        self.assertIn('folder', json.loads(body)['error'])

    def test_disk_full_during_commit_cleans_up_and_reports_storage_error(self):
        with patch.object(Path, 'replace', side_effect=OSError(errno.ENOSPC, 'full')):
            status, body = self.upload()
        self.assertEqual(status, 507)
        self.assertIn('full', json.loads(body)['error'])
        self.assertEqual(list(self.folder.iterdir()), [])

    def test_failed_temporary_open_removes_only_our_reservation(self):
        open_file = Path.open
        def denied(path, mode='r', *args, **kwargs):
            if mode == 'xb':
                raise PermissionError('Cannot create partial file')
            return open_file(path, mode, *args, **kwargs)
        with patch.object(Path, 'open', denied):
            status, _ = self.upload()
        self.assertEqual(status, 403)
        self.assertEqual(list(self.folder.iterdir()), [])

    def test_reservation_race_never_removes_a_file_created_by_someone_else(self):
        def another_writer(path, *args, **kwargs):
            path.write_bytes(b'belongs to another writer')
            raise FileExistsError('already exists')
        with patch.object(Path, 'touch', another_writer):
            status, _ = self.upload()
        self.assertEqual(status, 500)
        self.assertEqual((self.folder / 'file.txt').read_bytes(), b'belongs to another writer')

    def test_success_reports_actual_session_path_even_after_ui_folder_changes(self):
        status, body = self.upload()
        self.assertEqual(status, 201)
        done = next(e for e in self.fixture.events if e[0].startswith('Received:'))
        self.assertEqual(done[3], self.folder / json.loads(body)['name'])
        fake_ui = fake_app(self.folder / 'a different folder')
        QRLANApp._update_progress(fake_ui, *done)
        self.assertEqual(fake_ui.last_received_path, self.folder / 'file.txt')

    def test_zero_byte_and_duplicate_name_uploads_still_work(self):
        self.assertEqual(self.upload(b'first')[0], 201)
        self.assertEqual(self.upload(b'')[0], 201)
        self.assertEqual((self.folder / 'file.txt').read_bytes(), b'first')
        self.assertEqual((self.folder / 'file (2).txt').read_bytes(), b'')

    def test_simultaneous_same_name_uploads_preserve_both_payloads(self):
        barrier = threading.Barrier(3)
        results = []
        def send(data):
            barrier.wait()
            results.append(self.upload(data))
        threads = [threading.Thread(target=send, args=(data,)) for data in (b'one', b'two')]
        for thread in threads: thread.start()
        barrier.wait()
        for thread in threads: thread.join(5)
        self.assertEqual(sorted(status for status, _ in results), [201, 201])
        self.assertEqual({p.read_bytes() for p in self.folder.iterdir()}, {b'one', b'two'})


class CancellationRegressions(unittest.TestCase):
    def test_stop_interrupts_blocked_upload_and_discards_late_notifications(self):
        with tempfile.TemporaryDirectory() as folder:
            started = threading.Event()
            notices = []
            service = TransferService(lambda *args: notices.append(args))
            with patch('app.local_ipv4', return_value='127.0.0.1'):
                service.start(TransferSession('receive', 'stop-token', receive_folder=Path(folder)))
            server = service.server
            callback = server.notify
            def report(*args):
                callback(*args)
                if args[0] == 'Receiving…': started.set()
            server.notify = report
            connection = http.client.HTTPConnection('127.0.0.1', server.server_address[1], timeout=3)
            try:
                connection.putrequest('POST', '/t/stop-token/upload')
                connection.putheader('Content-Length', '1000000')
                connection.putheader('X-Filename', 'cancelled.bin')
                connection.endheaders()
                connection.send(b'first bytes')
                self.assertTrue(started.wait(3))
                service.stop()
                self.assertTrue(server.wait_for_transfers(3))
                self.assertEqual(list(Path(folder).iterdir()), [])
                service.poll()
                self.assertEqual(notices, [])
                self.assertEqual(service.url, '')
                with self.assertRaises((OSError, http.client.HTTPException)):
                    connection.send(b'more bytes')
                    connection.getresponse()
            finally:
                connection.close()
                service.stop()

    def test_cancel_before_commit_never_leaves_a_completed_file(self):
        with tempfile.TemporaryDirectory() as folder:
            fixture = HTTPFixture(Path(folder))
            callback = fixture.server.notify
            def cancel_at_end(*args):
                callback(*args)
                if args[0] == 'Receiving…' and args[1] == args[2] and args[2]:
                    fixture.server.cancel_active()
            fixture.server.notify = cancel_at_end
            try:
                with self.assertRaises((OSError, http.client.HTTPException)):
                    fixture.request('POST', 'upload', b'complete body', {'X-Filename': 'cancel-at-commit.txt'})
                self.assertTrue(fixture.server.wait_for_transfers(3))
                self.assertEqual(list(Path(folder).iterdir()), [])
                self.assertFalse(any(e[0].startswith('Received:') for e in fixture.events))
            finally: fixture.close()

    def test_stopped_download_does_not_report_success(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / 'source.bin').write_bytes(b'x' * (CHUNK_SIZE * 8))
            fixture = HTTPFixture(root, 'send')
            callback = fixture.server.notify
            def cancel_after_chunk(*args):
                callback(*args)
                if args[0] == 'Downloading…' and args[1] >= CHUNK_SIZE:
                    fixture.server.cancel_active()
            fixture.server.notify = cancel_after_chunk
            try:
                with self.assertRaises((OSError, http.client.HTTPException)):
                    fixture.request('GET', 'download')
                self.assertTrue(fixture.server.wait_for_transfers(3))
                self.assertFalse(any(e[0].startswith('Downloaded:') for e in fixture.events))
                self.assertEqual(fixture.errors, [])
            finally: fixture.close()

    def test_old_session_events_cannot_overwrite_new_session(self):
        notices = []
        service = TransferService(lambda *args: notices.append(args))
        service.events.put((service.generation, 'old status', 0, 0, None))
        service.stop()
        service.events.put((service.generation, 'current status', 0, 0, None))
        service.poll()
        self.assertEqual([event[0] for event in notices], ['current status'])


class DownloadRegressions(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / 'source.bin').write_bytes(b'complete download')
        self.fixture = HTTPFixture(self.root, 'send')

    def tearDown(self):
        self.fixture.close()
        self.assertEqual(self.fixture.errors, [])
        self.temp.cleanup()

    def test_deleted_source_landing_page_and_download_return_gone(self):
        (self.root / 'source.bin').unlink()
        for tail in ('', 'download'):
            status, body = self.fixture.request('GET', tail)
            self.assertEqual(status, 410)
            self.assertIn(b'File unavailable', body)

    def test_unreadable_source_returns_useful_error_before_headers(self):
        with patch.object(Path, 'open', side_effect=PermissionError('unreadable')):
            status, body = self.fixture.request('GET', 'download')
        self.assertEqual(status, 403)
        self.assertIn(b'File unavailable', body)

    def test_normal_download_keeps_byte_integrity(self):
        status, body = self.fixture.request('GET', 'download')
        self.assertEqual(status, 200)
        self.assertEqual(body, b'complete download')


def fake_app(folder):
    class Variable:
        value = None
        def set(self, value): self.value = value
    orb = SimpleNamespace(active=None)
    orb.set_active = lambda active: setattr(orb, 'active', active)
    return SimpleNamespace(status=Variable(), receive_folder=folder, last_received_path=None,
                           status_orb=orb, service=SimpleNamespace(url='http://local/'),
                           progress_value=Variable(), progress_text=Variable())


class StatusRegressions(unittest.TestCase):
    def test_failed_upload_resets_progress_and_activity(self):
        fake = fake_app(Path('.'))
        QRLANApp._update_progress(fake, 'Receiving…', 5, 10)
        self.assertEqual(fake.progress_value.value, 50)
        QRLANApp._update_progress(fake, 'Upload failed: interrupted', 0, 0)
        self.assertEqual(fake.progress_value.value, 0)
        self.assertFalse(fake.status_orb.active)
        self.assertIn('interrupted', fake.status.value)


if __name__ == '__main__':
    unittest.main()
