"""
Криптография HikCentral: вычисление AES ключа, ne(), генерация appendinfo.
"""
import base64
import hashlib
import math
from Crypto.Cipher import AES
from Crypto.Util.Padding import pad


APPEND_INFO_IV = bytes.fromhex("000102030405060708090a0b0c0d0e0f")


def ne(e: int) -> int:
    """
    Точная копия JS:
        var t = e * Math.sin(e); t = |t|
        var n = e * Math.cos(e); n = |n|
        if (t < n) swap(t, n)
        n = (n != 0) ? n : t
        return parseInt(6.28*n + 4*(t-n), 10)
    """
    t = abs(e * math.sin(e))
    n = abs(e * math.cos(e))
    if t < n:
        t, n = n, t
    if n == 0:
        n = t
    val = 6.28 * n + 4 * (t - n)
    return int(val)


def create_aes_key(aes_source_key: str, challenge: str, iterations: int) -> str:
    """
    q.createAESKey(e, t, n):
        var i = MD5(e+t)        // 32-hex string
        for r=1; r<n; r++ i = MD5(i)
        return i
    Возвращает 32-hex MD5 ключ.
    """
    i = hashlib.md5((aes_source_key + challenge).encode("utf-8")).hexdigest()
    for _ in range(1, iterations):
        i = hashlib.md5(i.encode("utf-8")).hexdigest()
    return i


def create_append_info(token_key_num: int, aes_key_hex: str) -> str:
    """
    q.createToken():
        plaintext = tokenKeyNum + ":" + ne(tokenKeyNum)
        ciphertext = AES-CBC(plaintext, AES_KEY, IV=000102...0e0f, PKCS7)
        return base64(ciphertext)
    """
    plaintext = f"{token_key_num}:{ne(token_key_num)}".encode("utf-8")
    key = bytes.fromhex(aes_key_hex)
    cipher = AES.new(key, AES.MODE_CBC, iv=APPEND_INFO_IV)
    padded = pad(plaintext, AES.block_size, style="pkcs7")
    ct = cipher.encrypt(padded)
    return base64.b64encode(ct).decode("ascii")


def decrypt_field(encrypted_b64: str, aes_key_hex: str) -> str:
    """
    Чувствительные поля (FamilyName, FullName, GivenName и т.д.) приходят
    зашифрованными AES-CBC тем же ключом и IV, что и appendinfo.
    """
    if not encrypted_b64:
        return ""
    try:
        ct = base64.b64decode(encrypted_b64)
    except Exception:
        return encrypted_b64
    if len(ct) == 0 or len(ct) % 16 != 0:
        return encrypted_b64
    key = bytes.fromhex(aes_key_hex)
    cipher = AES.new(key, AES.MODE_CBC, iv=APPEND_INFO_IV)
    try:
        from Crypto.Util.Padding import unpad
        pt = unpad(cipher.decrypt(ct), 16, style="pkcs7")
        return pt.decode("utf-8")
    except Exception:
        return encrypted_b64


# ----------- RC4Drop (для расшифровки сохранённого в localStorage AES-ключа) -----------

def _evp_bytes_to_key(passphrase: bytes, salt: bytes, key_len: int = 32) -> bytes:
    """OpenSSL EVP_BytesToKey с MD5, как у CryptoJS."""
    out = b""
    prev = b""
    while len(out) < key_len:
        prev = hashlib.md5(prev + passphrase + salt).digest()
        out += prev
    return out[:key_len]


def _rc4_decrypt(key: bytes, data: bytes, drop_words: int = 192) -> bytes:
    """RC4Drop — стандартный RC4 со сбросом первых drop_words слов (4 байта = 1 слово)."""
    S = list(range(256))
    j = 0
    for i in range(256):
        j = (j + S[i] + key[i % len(key)]) & 0xFF
        S[i], S[j] = S[j], S[i]

    i = j = 0
    for _ in range(drop_words * 4):
        i = (i + 1) & 0xFF
        j = (j + S[i]) & 0xFF
        S[i], S[j] = S[j], S[i]

    out = bytearray()
    for byte in data:
        i = (i + 1) & 0xFF
        j = (j + S[i]) & 0xFF
        S[i], S[j] = S[j], S[i]
        out.append(byte ^ S[(S[i] + S[j]) & 0xFF])
    return bytes(out)


def cryptojs_rc4drop_decrypt(encrypted_b64: str, passphrase: str) -> str:
    """Расшифровать CryptoJS RC4Drop "Salted__" формат."""
    data = base64.b64decode(encrypted_b64)
    assert data[:8] == b"Salted__", "expected Salted__ header"
    salt = data[8:16]
    ct = data[16:]
    key = _evp_bytes_to_key(passphrase.encode("utf-8"), salt, 32)
    pt = _rc4_decrypt(key, ct, drop_words=192)
    return pt.decode("utf-8")
