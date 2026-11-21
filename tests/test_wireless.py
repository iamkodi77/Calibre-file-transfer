import hashlib
import io
import socket
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from leafline.storage import Library
from leafline.web import create_app
from leafline.wireless import (WirelessService, send_frame, receive_frame, _read_exact,
                               OK, ERROR, NOOP, SEND_BOOK, GET_INITIALIZATION_INFO,
                               GET_DEVICE_INFO, SET_DEVICE_INFO, SET_LIBRARY_INFO, DISPLAY_MESSAGE)

HEADERS = {'X-Requested-With': 'XMLHttpRequest'}


def wait_until(predicate, timeout=6):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        threading.Event().wait(0.02)
    raise AssertionError('Condition did not become true')


class FakeReader(threading.Thread):
    """Exercises the server using the same frames and request order as KOReader."""
    def __init__(self, port, password='', reject=False):
        super().__init__(daemon=True)
        self.sock = socket.create_connection(('127.0.0.1', port), timeout=5)
        self.sock.settimeout(8)
        self.password = password
        self.reject = reject
        self.ready = threading.Event()
        self.received = []
        self.error = None
        self.password_error = False
        self.init = None

    def run(self):
        try:
            opcode, args = receive_frame(self.sock)
            assert opcode == GET_INITIALIZATION_INFO
            self.init = args
            password_hash = hashlib.sha1((self.password + args['passwordChallenge']).encode()).hexdigest() if self.password else ''
            send_frame(self.sock, OK, {'versionOK': True, 'deviceName': 'Test e-reader', 'deviceKind': 'Kobo',
                       'appName': 'KOReader', 'canReceiveBookBinary': True, 'canStreamBooks': True,
                       'canStreamMetadata': True, 'canDeleteMultipleBooks': True, 'canSendOkToSendbook': True,
                       'canAcceptLibraryInfo': True, 'acceptedExtensions': ['epub', 'txt', 'cbz'],
                       'maxBookContentPacketLen': 4096, 'passwordHash': password_hash})
            opcode, args = receive_frame(self.sock)
            if opcode == DISPLAY_MESSAGE:
                self.password_error = args['messageKind'] == 1
                send_frame(self.sock, OK, {})
                return
            assert opcode == GET_DEVICE_INFO
            send_frame(self.sock, OK, {'device_info': {'device_store_uuid': 'reader-id', 'device_name': 'Test e-reader'}, 'version': '1', 'device_version': '1'})
            opcode, args = receive_frame(self.sock)
            assert opcode == SET_DEVICE_INFO and args['device_store_uuid'] == 'reader-id'
            send_frame(self.sock, OK, {})
            opcode, args = receive_frame(self.sock)
            assert opcode == SET_LIBRARY_INFO
            send_frame(self.sock, OK, {})
            self.ready.set()
            while True:
                opcode, args = receive_frame(self.sock)
                if opcode == SEND_BOOK:
                    if self.reject:
                        send_frame(self.sock, ERROR, {'message': 'Reader refused this file'})
                    else:
                        send_frame(self.sock, OK, {})
                        data = _read_exact(self.sock, args['length'])
                        self.received.append((args, data))
                elif opcode == NOOP:
                    send_frame(self.sock, OK, {})
                    if args.get('ejecting'):
                        break
                else:
                    raise AssertionError(f'Unexpected opcode {opcode}')
        except (OSError, ConnectionError) as exc:
            # Closing the server socket in cleanup is normal.
            if self.ready.is_set() and not isinstance(exc, socket.timeout):
                return
            self.error = exc
        except Exception as exc:
            self.error = exc
        finally:
            self.sock.close()


class WirelessTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.library = Library(self.tmp.name)
        self.service = WirelessService(self.library, host='127.0.0.1', port=0, http_port=8000,
                                       advertise_host='127.0.0.1', discovery_ports=(0,), publish_mdns=False)
        self.assertTrue(self.service.start())
        self.client = create_app(self.library, self.service).test_client()
        self.reader = None

    def tearDown(self):
        self.service.stop()
        if self.reader:
            self.reader.join(timeout=3)
        self.tmp.cleanup()

    def connect(self, **kwargs):
        self.reader = FakeReader(self.service.port, **kwargs)
        self.reader.start()
        self.assertTrue(self.reader.ready.wait(5), self.reader.error)
        return wait_until(lambda: self.service.devices())

    def test_discovery_replies_in_calibre_format(self):
        udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        udp.settimeout(3)
        try:
            port = self.service.status()['discovery_ports'][0]
            udp.sendto(b'hello', ('127.0.0.1', port))
            message, _ = udp.recvfrom(1024)
        finally:
            udp.close()
        self.assertIn(b'calibre wireless device client (on ', message)
        self.assertTrue(message.endswith(f';8000,{self.service.port}'.encode()), message)

    def test_handshake_transfer_binary_progress_and_disconnect(self):
        payload = b'\x00\xff' + b'book bytes ' * 8000
        file = self.library.add_file(io.BytesIO(payload), 'A & café story.epub')
        devices = self.connect()
        self.assertEqual(devices[0]['name'], 'Test e-reader')
        self.assertIn('epub', devices[0]['accepted_extensions'])
        self.assertEqual(self.reader.init['currentLibraryUUID'], self.library.setting('library_uuid'))
        response = self.client.post('/api/transfers', json={'device_id': devices[0]['id'], 'file_ids': [file['id']]}, headers=HEADERS)
        self.assertEqual(response.status_code, 202, response.json)
        transfer_id = response.json['queued'][0]['id']
        job = wait_until(lambda: (job if (job := self.library.get_transfer(transfer_id))['status'] in ('sent', 'failed') else None))
        self.assertEqual(job['status'], 'sent', job)
        self.assertEqual(job['bytes_sent'], len(payload))
        args, received = self.reader.received[0]
        self.assertEqual(received, payload)
        self.assertEqual(args['length'], len(payload))
        self.assertEqual(args['metadata']['title'], 'A & café story')
        self.assertEqual(args['lpath'], 'A & café story.epub')
        self.assertTrue(any(e['kind'] == 'transfer' for e in self.library.events()))
        self.assertEqual(self.client.post('/api/devices/' + devices[0]['id'] + '/disconnect', headers=HEADERS).status_code, 200)
        wait_until(lambda: not self.service.devices())
        self.reader.join(timeout=2)
        self.assertIsNone(self.reader.error)

    def test_reject_unsupported_or_empty_files_without_sending(self):
        devices = self.connect()
        image = self.library.add_file(io.BytesIO(b'png'), 'picture.png')
        empty = self.library.add_file(io.BytesIO(b''), 'empty.txt')
        response = self.client.post('/api/transfers', json={'device_id': devices[0]['id'], 'file_ids': [image['id'], empty['id']]}, headers=HEADERS)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(len(response.json['rejected']), 2)
        self.assertIn('did not report support', response.json['rejected'][0]['message'])
        self.assertEqual(self.reader.received, [])
        self.assertEqual(self.library.transfers(), [])

    def test_two_readers_and_mixed_format_queue(self):
        first = self.connect()[0]
        second_reader = FakeReader(self.service.port)
        second_reader.start()
        try:
            self.assertTrue(second_reader.ready.wait(5), second_reader.error)
            devices = wait_until(lambda: (d if len(d := self.service.devices()) == 2 else None))
            second = next(d for d in devices if d['id'] != first['id'])
            text = self.library.add_file(io.BytesIO(b'hello reader'), 'hello.txt')
            image = self.library.add_file(io.BytesIO(b'picture'), 'picture.png')
            mixed = self.client.post('/api/transfers', json={'device_id': first['id'], 'file_ids': [text['id'], image['id']]}, headers=HEADERS)
            self.assertEqual(mixed.status_code, 202)
            self.assertEqual(len(mixed.json['queued']), 1)
            self.assertEqual(len(mixed.json['rejected']), 1)
            other = self.client.post('/api/transfers', json={'device_id': second['id'], 'file_ids': [text['id']]}, headers=HEADERS)
            self.assertEqual(other.status_code, 202)
            ids = [mixed.json['queued'][0]['id'], other.json['queued'][0]['id']]
            wait_until(lambda: all(self.library.get_transfer(i)['status'] == 'sent' for i in ids))
            self.assertEqual(self.reader.received[0][1], b'hello reader')
            self.assertEqual(second_reader.received[0][1], b'hello reader')
        finally:
            try:
                second_reader.sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            second_reader.sock.close()
            second_reader.join(timeout=3)

    def test_reader_error_is_reported_and_session_stays_connected(self):
        devices = self.connect(reject=True)
        file = self.library.add_file(io.BytesIO(b'text'), 'read.txt')
        response = self.client.post('/api/transfers', json={'device_id': devices[0]['id'], 'file_ids': [file['id']]}, headers=HEADERS)
        job_id = response.json['queued'][0]['id']
        job = wait_until(lambda: (job if (job := self.library.get_transfer(job_id))['status'] == 'failed' else None))
        self.assertIn('Reader refused', job['error'])
        self.assertEqual(len(self.service.devices()), 1)

    def test_wrong_wireless_password_gets_calibre_error(self):
        self.service.password = 'correct'
        self.reader = FakeReader(self.service.port, password='incorrect')
        self.reader.start()
        self.reader.join(timeout=5)
        self.assertTrue(self.reader.password_error, self.reader.error)
        wait_until(lambda: not self.service.pending_sessions)
        self.assertEqual(self.service.devices(), [])

    def test_correct_wireless_password_connects(self):
        self.service.password = 'correct'
        devices = self.connect(password='correct')
        self.assertEqual(devices[0]['name'], 'Test e-reader')
        self.assertTrue(self.reader.init['passwordChallenge'])

    def test_framing_handles_unicode_and_partial_network_reads(self):
        left, right = socket.socketpair()
        try:
            send_frame(left, OK, {'name': 'A café café café'})
            self.assertEqual(receive_frame(right), (OK, {'name': 'A café café café'}))
        finally:
            left.close(); right.close()

    def test_mdns_advertises_wireless_and_content_services(self):
        class FakeZeroconf:
            def __init__(self, **kwargs): self.registered, self.options = [], []
            def register_service(self, info, **kwargs):
                self.registered.append(info)
                self.options.append(kwargs)
            def unregister_service(self, info): pass
            def close(self): pass
        with patch('zeroconf.Zeroconf', FakeZeroconf):
            self.service._publish_mdns()
            self.assertEqual(self.service.mdns_state, 'available')
            services = self.service.zeroconf.registered
            self.assertEqual(len(services), 2)
            self.assertEqual(services[0].type, '_calibresmartdeviceapp._tcp.local.')
            self.assertFalse(self.service.zeroconf.options[0]['strict'])
            self.assertEqual(services[0].port, self.service.port)
            self.assertEqual(services[1].type, '_calibre._tcp.local.')
            self.assertEqual(services[1].properties[b'path'], b'/opds')


if __name__ == '__main__':
    unittest.main()
