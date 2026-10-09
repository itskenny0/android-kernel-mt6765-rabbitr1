#!/usr/bin/env python3
"""Exercise fetch retries and verified publication without network access."""
import hashlib
import http.client
import io
from pathlib import Path
import ssl
import tempfile
import unittest
from unittest.mock import patch
import urllib.error

from locked_download import download_locked


class InterruptedBody(io.BytesIO):
    def read(self, count=-1):
        if self.tell():
            raise http.client.IncompleteRead(b'', 9)
        return super().read(3)


class Downloads(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(
            prefix='source-download-', dir='/rabbitr1/.tmp')
        self.addCleanup(self.directory.cleanup)
        self.dest = Path(self.directory.name) / 'input.bin'
        self.payload = b'pinned source data'
        self.item = dict(url='https://example.invalid/input', bytes=len(self.payload),
                         sha256=hashlib.sha256(self.payload).hexdigest())

    def fetch(self, responses):
        with patch('locked_download.urllib.request.urlopen', side_effect=responses) as opened, \
                patch('locked_download.time.sleep') as slept:
            download_locked(self.item, self.dest)
        self.assertEqual(self.dest.read_bytes(), self.payload)
        self.assertEqual(list(self.dest.parent.iterdir()), [self.dest])
        return opened.call_count, [call.args[0] for call in slept.call_args_list]

    def failure(self, responses, error, calls=1):
        with patch('locked_download.urllib.request.urlopen', side_effect=responses) as opened, \
                patch('locked_download.time.sleep'):
            with self.assertRaises(error):
                download_locked(self.item, self.dest)
        self.assertEqual(opened.call_count, calls)
        self.assertFalse(self.dest.exists())
        self.assertEqual(list(self.dest.parent.iterdir()), [])

    def test_success(self):
        self.assertEqual(self.fetch([io.BytesIO(self.payload)]), (1, []))

    def test_connection_reset_retries(self):
        self.assertEqual(self.fetch([
            urllib.error.URLError(ConnectionResetError(104, 'reset')),
            io.BytesIO(self.payload)]), (2, [1]))

    def test_partial_body_restarts(self):
        self.assertEqual(self.fetch([InterruptedBody(self.payload), io.BytesIO(self.payload)]),
                         (2, [1]))

    def test_timeout_exhaustion(self):
        self.failure([TimeoutError('timeout') for _ in range(3)], TimeoutError, 3)

    def test_transient_http_retries(self):
        error = urllib.error.HTTPError(self.item['url'], 503, 'busy', {}, io.BytesIO())
        self.assertEqual(self.fetch([error, io.BytesIO(self.payload)]), (2, [1]))
        self.assertTrue(error.fp.closed)

    def test_permanent_http_stops(self):
        self.failure([urllib.error.HTTPError(self.item['url'], 404, 'missing', {},
                                            io.BytesIO())], urllib.error.HTTPError)

    def test_certificate_error_stops(self):
        self.failure([urllib.error.URLError(ssl.SSLCertVerificationError('untrusted'))],
                     urllib.error.URLError)

    def test_wrong_checksum_stops(self):
        self.failure([io.BytesIO(b'x' * len(self.payload))], ValueError)

    def test_short_body_stops(self):
        self.failure([io.BytesIO(self.payload[:-1])], ValueError)

    def test_existing_file_preserved(self):
        self.dest.write_bytes(b'local work')
        with patch('locked_download.urllib.request.urlopen') as opened:
            with self.assertRaises(FileExistsError):
                download_locked(self.item, self.dest)
        opened.assert_not_called()
        self.assertEqual(self.dest.read_bytes(), b'local work')

    def test_concurrent_publication_preserved(self):
        def response(*args, **kwargs):
            self.dest.write_bytes(b'another fetch')
            return io.BytesIO(self.payload)
        with patch('locked_download.urllib.request.urlopen', side_effect=response) as opened, \
                patch('locked_download.time.sleep') as slept:
            with self.assertRaises(FileExistsError):
                download_locked(self.item, self.dest)
        self.assertEqual(opened.call_count, 1)
        slept.assert_not_called()
        self.assertEqual(self.dest.read_bytes(), b'another fetch')
        self.assertEqual(list(self.dest.parent.iterdir()), [self.dest])


if __name__ == '__main__':
    unittest.main()
