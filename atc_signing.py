"""Fixed-request AirTraffic metadata signatures; never connects or writes.

Algorithm adapted from rustunes, MIT licensed, copyright 2026 Feim Biba.
It supports the documented fixed host request only, not Apple's native signer.
"""
import hashlib
import hmac
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

HOST_REQUEST = bytes.fromhex(
    '0101111111111111111111111111111111110440bc2785e0dbf166361e07980a'
    '5ea48dba95b3b8ea265d62aefea51bb7b190e0b77126290ad39bb13fecc08c25'
    'a9561c517ac11e64905da029e61bdfd0ba22c313')
WRAP_KEY = bytes.fromhex('7776eb98ab6ce602e8e892e41ba7e915')
KEY_XOR = bytes.fromhex('c6c7c4c5cacbc8c9cecfcccdb2b3b0b1')
TAG_XOR = bytes.fromhex('f9fafbfcfdfefff0f1f2f3f4f5f6f7e8e9eaebec')


def sign_metadata(reply, operation):
    if not isinstance(reply, bytes) or len(reply) != 83 or reply[:2] != b'\x01\x01' or reply[18] != 64:
        raise ValueError('Unsupported device signing response')
    if not isinstance(operation, bytes) or not 0 < len(operation) <= 4 * 1024 * 1024:
        raise ValueError('Invalid metadata length')
    decryptor = Cipher(algorithms.AES(bytes([0x95]) * 16), modes.CBC(reply[2:18])).decryptor()
    decoded = decryptor.update(reply[19:]) + decryptor.finalize()
    mixed = bytes(decoded[i + 1] ^ decoded[i + 39] ^ KEY_XOR[i] for i in range(16))
    encryptor = Cipher(algorithms.AES(WRAP_KEY), modes.ECB()).encryptor()
    key = encryptor.update(mixed) + encryptor.finalize()
    tag = hmac.new(key, operation, hashlib.sha1).digest()
    return b'\x01' + bytes(tag[i] ^ decoded[i + 18] ^ TAG_XOR[i] for i in range(20))
