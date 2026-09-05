"""Integration coverage for multiple-file links, history, and QR import."""
import tempfile
import unittest
from pathlib import Path
import qrcode
from desktop import LocalStore, decode_qr_image, valid_transfer_url
from test_regressions import HTTPFixture


class FluentTransfers(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.fixture = HTTPFixture(self.root, 'send')
        self.records = []
        self.fixture.server.on_record = self.records.append
        self.first = self.root / 'first & file.txt'
        self.second = self.root / 'second.bin'
        self.first.write_bytes(b'first payload')
        self.second.write_bytes(bytes(range(256))*1024)
        self.fixture.session.file_paths = (self.first, self.second)

    def tearDown(self):
        self.fixture.close()
        self.temp.cleanup()

    def test_selected_files_download_with_integrity_and_history(self):
        status, body = self.fixture.request('GET')
        self.assertEqual(status, 200)
        self.assertIn(b'first &amp; file.txt', body)
        self.assertEqual(self.records, [])
        for i, path in enumerate((self.first, self.second)):
            status, body = self.fixture.request('GET', 'download?file='+str(i))
            self.assertEqual(status, 200)
            self.assertEqual(body, path.read_bytes())
        self.fixture.server.wait_for_transfers(3)
        self.assertEqual([r['name'] for r in self.records], [self.first.name,self.second.name])
        self.assertTrue(all(r['status']=='Completed' and r['direction']=='Sent' for r in self.records))

    def test_invalid_file_selectors_and_wrong_token_are_not_exposed(self):
        for value in ('-1','2','../../private','nope','99999999999999999999'):
            self.assertEqual(self.fixture.request('GET','download?file='+value)[0],404)
        connection = self.fixture.connection()
        connection.request('GET','/t/wrong/download?file=0')
        self.assertEqual(connection.getresponse().status,404)
        connection.close()
        self.assertEqual(self.records,[])

    def test_deleted_file_records_failure(self):
        self.second.unlink()
        self.assertEqual(self.fixture.request('GET','download?file=1')[0],410)
        self.assertEqual(self.records[0]['status'],'Failed')
        self.assertEqual(self.records[0]['name'],self.second.name)
        self.assertEqual(self.records[0]['path'],'')

    def test_upload_history_uses_actual_collision_safe_path(self):
        self.fixture.session.mode='receive'
        self.fixture.request('POST','upload',b'new data',{'X-Filename':self.first.name})
        self.fixture.server.wait_for_transfers(3)
        row=self.records[0]
        self.assertEqual(row['direction'],'Received')
        self.assertEqual(row['status'],'Completed')
        self.assertNotEqual(Path(row['path']),self.first)
        self.assertEqual(Path(row['path']).read_bytes(),b'new data')


class SavedDataAndQR(unittest.TestCase):
    def test_history_survives_restart_and_corruption_is_backed_up(self):
        with tempfile.TemporaryDirectory() as folder:
            store=LocalStore(folder)
            store.save('history',[{'name':'sample'}])
            self.assertEqual(LocalStore(folder).load('history',[]),[{'name':'sample'}])
            (Path(folder)/'history.json').write_text('{broken',encoding='utf-8')
            self.assertEqual(store.load('history',[]),[])
            self.assertEqual(len(list(Path(folder).glob('history.unreadable-*.json'))),1)
            store.save('history',[])

    def test_generated_local_qr_decodes_and_unsafe_qr_is_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'qr.png'
            link='http://192.168.1.30:45123/t/example-token/'
            qrcode.make(link).save(path)
            self.assertEqual(decode_qr_image(path),link)
            qrcode.make('javascript:alert(1)').save(path)
            with self.assertRaises(ValueError):decode_qr_image(path)

    def test_link_validation(self):
        for value in ('http://localhost:8000/','http://192.168.1.2:8080/','https://host.local/'):
            self.assertTrue(valid_transfer_url(value),value)
        for value in ('file:///C:/secret','javascript:alert(1)','https://example.com/','http://user:pass@192.168.1.2/','http://192.168.1.2:999999/','http://0.0.0.0/'):
            self.assertFalse(valid_transfer_url(value),value)
