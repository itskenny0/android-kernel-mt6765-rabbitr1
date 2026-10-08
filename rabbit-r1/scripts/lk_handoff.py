"""Exact r1 LK display-guard checks shared by building and flash preparation."""
import hashlib

HOOK = 0x1d4d0
BEFORE = bytes.fromhex('e5f758fc')
AFTER = bytes.fromhex('0bf07af9')  # BL 0x287c8, replacing BL 0x2d84
START, END = 0x287c8, 0x28878
PAYLOAD_SHA = 'a0acf0ae1d712d5e1f3b87b2e5b3998fe94735d1517cc1dee774e939ba85871a'
RELOCK_START = 0x28788
RELOCK_SHA = '0d2d701ca7aebb8e0c35c0f1188785e5fcd461c2fa9f710a33ccd268c13bd362'


def digest(data):
    return hashlib.sha256(data).hexdigest()


def valid_payload(payload):
    return len(payload) == END-START and digest(payload) == PAYLOAD_SHA


def has_display_guard(image):
    # Metadata alone must never authorize an old or partially patched LK.
    # The reused code slot is only free while the complete relock refusal is
    # intact, including its unconditional tail call and embedded message.
    return (len(image) == 864000 and image[HOOK:HOOK+4] == AFTER and
            valid_payload(image[START:END]) and
            digest(image[RELOCK_START:START]) == RELOCK_SHA)
