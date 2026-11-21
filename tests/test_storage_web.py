import base64
import io
import tempfile
import unittest
import zipfile
from urllib.parse import quote
from xml.etree import ElementTree as ET

from leafline.storage import Library, clean_filename
from leafline.web import create_app
from leafline.wireless import WirelessService

# A real, tiny PNG.
PNG = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/aL0AAAAASUVORK5CYII=')
HEADERS = {'X-Requested-With': 'XMLHttpRequest'}


def make_epub():
    out = io.BytesIO()
    with zipfile.ZipFile(out, 'w') as z:
        z.writestr('META-INF/container.xml', '''<?xml version="1.0"?><container xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles><rootfile full-path="OEBPS/content.opf"/></rootfiles></container>''')
        z.writestr('OEBPS/content.opf', '''<?xml version="1.0"?><package xmlns="http://www.idpf.org/2007/opf" version="3.0"><metadata xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:title>The Little &amp; Lovely Book</dc:title><dc:creator>A. Reader</dc:creator></metadata><manifest><item id="cover" href="images/cover.png" media-type="image/png" properties="cover-image"/></manifest></package>''')
        z.writestr('OEBPS/images/cover.png', PNG)
    out.seek(0)
    return out


class WebTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.library = Library(self.tmp.name)
        self.wireless = WirelessService(self.library, publish_mdns=False)
        self.app = create_app(self.library, self.wireless)
        self.app.testing = True
        self.client = self.app.test_client()

    def tearDown(self):
        self.tmp.cleanup()

    def upload(self, name, content):
        return self.client.post('/api/files', data={'files': (io.BytesIO(content), name)}, headers=HEADERS, content_type='multipart/form-data')

    def test_arbitrary_files_round_trip_and_path_safety(self):
        payload = b'\x00\xffany arbitrary bytes\x01'
        response = self.upload('../folder/strange & café data.xyz', payload)
        self.assertEqual(response.status_code, 201, response.json)
        file = response.json['files'][0]
        self.assertEqual(file['filename'], 'strange & café data.xyz')
        self.assertEqual(clean_filename(r'C:\folder\safe.epub'), 'safe.epub')
        self.assertEqual(file['category'], 'other')
        self.assertEqual(file['size'], len(payload))
        download = self.client.get('/get/' + file['id'] + '/' + quote(file['filename']))
        self.assertEqual(download.status_code, 200)
        self.assertEqual(download.data, payload)
        self.assertIn('attachment', download.headers['Content-Disposition'])
        self.assertEqual(download.headers['X-Content-Type-Options'], 'nosniff')
        # Range requests matter for readers resuming downloads.
        partial = self.client.get('/get/' + file['id'] + '/' + quote(file['filename']), headers={'Range': 'bytes=2-5'})
        self.assertEqual(partial.status_code, 206)
        self.assertEqual(partial.data, payload[2:6])
        partial.close()
        download.close()
        self.assertEqual(len(self.library.list_files()), 1)
        self.assertTrue(self.library.file_path(file['id']).is_file())
        self.assertEqual(self.client.delete('/api/files/' + file['id'], headers=HEADERS).status_code, 200)
        self.assertFalse(self.library.file_path(file['id']).exists())
        self.assertEqual(self.client.get('/get/' + file['id'] + '/anything').status_code, 404)

    def test_epub_metadata_covers_opds_and_search(self):
        response = self.client.post('/api/files', data={'files': (make_epub(), 'first.epub')}, headers=HEADERS, content_type='multipart/form-data')
        self.assertEqual(response.status_code, 201)
        file = response.json['files'][0]
        self.assertEqual(file['title'], 'The Little & Lovely Book')
        self.assertEqual(file['author'], 'A. Reader')
        self.assertTrue(file['has_cover'])
        cover = self.client.get('/api/files/' + file['id'] + '/cover')
        self.assertEqual(cover.data, PNG)
        self.assertEqual(cover.mimetype, 'image/png')
        cover.close()
        feed = self.client.get('/opds')
        self.assertEqual(feed.status_code, 200)
        self.assertIn('opds-catalog', feed.content_type)
        root = ET.fromstring(feed.data)
        ns = {'a': 'http://www.w3.org/2005/Atom'}
        entry = root.find('a:entry', ns)
        self.assertEqual(entry.find('a:title', ns).text, file['title'])
        links = entry.findall('a:link', ns)
        acquisition = next(x for x in links if x.get('rel') == 'http://opds-spec.org/acquisition')
        self.assertTrue(acquisition.get('href').endswith('/first.epub'))
        self.assertEqual(acquisition.get('type'), 'application/epub+zip')
        acquired = self.client.get(acquisition.get('href'))
        self.assertEqual(acquired.status_code, 200)
        acquired.close()
        image = next(x for x in links if x.get('rel') == 'http://opds-spec.org/image')
        self.assertEqual(image.get('type'), 'image/png')
        self.assertEqual(len(ET.fromstring(self.client.get('/opds?q=lovely').data).findall('a:entry', ns)), 1)
        self.assertEqual(len(ET.fromstring(self.client.get('/opds?q=notfound').data).findall('a:entry', ns)), 0)
        self.assertEqual(self.client.get('/opds/search.xml').status_code, 200)

    def test_images_invalid_epub_and_zero_byte_files_are_still_stored(self):
        photo = self.upload('photo.png', PNG).json['files'][0]
        self.assertTrue(photo['has_cover'])
        photo_cover = self.client.get('/api/files/' + photo['id'] + '/cover')
        self.assertEqual(photo_cover.data, PNG)
        photo_cover.close()
        broken = self.upload('broken.epub', b'not an epub').json['files'][0]
        self.assertEqual(broken['title'], 'broken')
        self.assertFalse(broken['has_cover'])
        empty = self.upload('empty.bin', b'').json['files'][0]
        self.assertEqual(empty['size'], 0)
        empty_download = self.client.get('/get/' + empty['id'] + '/empty.bin')
        self.assertEqual(empty_download.data, b'')
        empty_download.close()
        # Uploaded markup is not served as an inline image.
        unsafe = self.upload('bad.svg', b'<svg onload="alert(1)"/>').json['files'][0]
        self.assertFalse(unsafe['has_cover'])

    def test_edit_delete_searchable_metadata_and_xml_safety(self):
        file = self.upload('hello.txt', b'hi').json['files'][0]
        updated = self.client.patch('/api/files/' + file['id'], json={'title': 'Hello \u0001 & world', 'author': 'Me'}, headers=HEADERS)
        self.assertEqual(updated.status_code, 200)
        self.assertEqual(updated.json['file']['title'], 'Hello & world')
        root = ET.fromstring(self.client.get('/opds?q=world').data)
        self.assertEqual(len(root.findall('{http://www.w3.org/2005/Atom}entry')), 1)
        self.assertEqual(self.client.patch('/api/files/' + file['id'], json={'title': ''}, headers=HEADERS).status_code, 400)
        self.assertEqual(self.client.put('/api/settings', json={'library_name': 'My reads'}, headers=HEADERS).json['library_name'], 'My reads')
        self.assertEqual(self.client.get('/api/status').json['library_name'], 'My reads')

    def test_csrf_header_and_optional_http_auth(self):
        self.assertEqual(self.client.post('/api/files', data={}).status_code, 403)
        protected = create_app(self.library, self.wireless, http_user='reader', http_password='secret').test_client()
        self.assertEqual(protected.get('/').status_code, 401)
        token = base64.b64encode(b'reader:secret').decode()
        self.assertEqual(protected.get('/opds', headers={'Authorization': 'Basic ' + token}).status_code, 200)
        wrong = base64.b64encode(b'reader:wrong').decode()
        self.assertEqual(protected.get('/opds', headers={'Authorization': 'Basic ' + wrong}).status_code, 401)

    def test_persistence_across_library_instances(self):
        file = self.upload('persist.cbz', b'comic').json['files'][0]
        self.library.set_library_name('Bookshelf')
        reopened = Library(self.tmp.name)
        self.assertEqual(reopened.setting('library_name'), 'Bookshelf')
        self.assertEqual(reopened.get_file(file['id'])['filename'], 'persist.cbz')
        self.assertEqual(reopened.file_path(file['id']).read_bytes(), b'comic')


if __name__ == '__main__':
    unittest.main()
