"""Download a pinned input, retrying transient transport failures."""
import hashlib
import http.client
from pathlib import Path
import ssl
import tempfile
import time
import urllib.error
import urllib.request


def download_locked(item, destination):
    destination = Path(destination)
    if destination.exists():
        raise FileExistsError(destination)
    for attempt in range(3):
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(
                    prefix='.' + destination.name + '.', suffix='.part',
                    dir=destination.parent, delete=False) as output:
                temporary = Path(output.name)
                with urllib.request.urlopen(item['url'], timeout=120) as response:
                    while chunk := response.read(1024 * 1024):
                        output.write(chunk)
            with temporary.open('rb') as stream:
                checksum = hashlib.file_digest(stream, 'sha256').hexdigest()
            if temporary.stat().st_size != item['bytes'] or checksum != item['sha256']:
                raise ValueError('Download checksum mismatch: ' + destination.name)
            # Both files are in the same directory. Publish only verified bytes
            # and never replace a file created by another fetch in the meantime.
            destination.hardlink_to(temporary)
            return
        except urllib.error.HTTPError as error:
            error.close()
            if error.code not in (408, 429, 500, 502, 503, 504) or attempt == 2:
                raise
        except urllib.error.URLError as error:
            if isinstance(error.reason, ssl.SSLCertVerificationError) or attempt == 2:
                raise
        except (ConnectionError, TimeoutError, http.client.IncompleteRead):
            if attempt == 2:
                raise
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        print(f'Retrying download {destination.name} ({attempt + 2}/3)', flush=True)
        time.sleep(attempt + 1)
