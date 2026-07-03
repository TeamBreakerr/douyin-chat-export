"""Characterization tests for the MPEG-CENC AES-128-CTR video decryptor.

We synthesize minimal but structurally-real MP4s (ftyp/moov>trak>mdia>minf>stbl>
{stsz,stsc,stco,senc} + mdat), encrypt sample bytes with the exact CENC scheme
the decryptor expects, and assert the decryptor round-trips them back to plaintext.
This exercises box parsing, absolute sample-offset computation, senc parsing,
and both the subsample and full-sample CTR paths — the whole fragile pipeline.
"""
import struct

from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from extractor import cenc


def _box(box_type: bytes, payload: bytes) -> bytes:
    return struct.pack(">I", 8 + len(payload)) + box_type + payload


def _full(box_type: bytes, version_flags: bytes, body: bytes) -> bytes:
    return _box(box_type, version_flags + body)


def _ctr(key: bytes, iv8: bytes, data: bytes) -> bytes:
    iv16 = iv8.ljust(16, b"\x00")
    c = Cipher(algorithms.AES(key), modes.CTR(iv16), backend=default_backend())
    return c.decryptor().update(data)


def _build_mp4(key, samples, *, has_sub):
    """Build a single-track CENC MP4.

    samples: list of dicts with keys:
      - iv: 8-byte IV
      - plain: the plaintext sample bytes
      - subs: list of (clear_len, prot_len) if has_sub else []
    Returns (encrypted_file_bytes, expected_plain_by_sample).
    """
    # Encrypt each sample to produce the stored (encrypted) mdat bytes.
    stored = []
    for s in samples:
        if not has_sub:
            stored.append(_ctr(key, s["iv"], s["plain"]))
            continue
        # Subsample: concat protected chunks, single CTR stream, re-interleave.
        pos = 0
        prot_plain = b""
        for clear, prot in s["subs"]:
            pos += clear
            prot_plain += s["plain"][pos:pos + prot]
            pos += prot
        prot_enc = _ctr(key, s["iv"], prot_plain)
        out = bytearray()
        pos = 0
        dp = 0
        for clear, prot in s["subs"]:
            out += s["plain"][pos:pos + clear]
            pos += clear
            out += prot_enc[dp:dp + prot]
            dp += prot
            pos += prot
        stored.append(bytes(out))

    sizes = [len(b) for b in stored]

    # stbl child boxes
    stsz = _full(b"stsz", b"\x00\x00\x00\x00",
                 struct.pack(">I", 0) + struct.pack(">I", len(sizes))
                 + b"".join(struct.pack(">I", n) for n in sizes))
    # one chunk holding all samples
    stsc = _full(b"stsc", b"\x00\x00\x00\x00",
                 struct.pack(">I", 1) + struct.pack(">III", 1, len(sizes), 1))

    senc_flags = b"\x00\x00\x02" if has_sub else b"\x00\x00\x00"
    senc_body = struct.pack(">I", len(samples))
    for s in samples:
        senc_body += s["iv"]
        if has_sub:
            senc_body += struct.pack(">H", len(s["subs"]))
            for clear, prot in s["subs"]:
                senc_body += struct.pack(">HI", clear, prot)
    senc = _full(b"senc", b"\x00" + senc_flags, senc_body)

    def assemble(chunk_offset):
        stco = _full(b"stco", b"\x00\x00\x00\x00",
                     struct.pack(">I", 1) + struct.pack(">I", chunk_offset))
        stbl = _box(b"stbl", stsz + stsc + stco + senc)
        minf = _box(b"minf", stbl)
        mdia = _box(b"mdia", minf)
        trak = _box(b"trak", mdia)
        moov = _box(b"moov", trak)
        ftyp = _box(b"ftyp", b"isom" + struct.pack(">I", 0) + b"isom")
        mdat_payload = b"".join(stored)
        mdat = _box(b"mdat", mdat_payload)
        return ftyp, moov, mdat

    # First pass to learn the absolute offset of the mdat payload, then rebuild
    # with the real stco offset (offset value is 4 bytes; sizes don't change).
    ftyp, moov, mdat = assemble(0)
    mdat_data_offset = len(ftyp) + len(moov) + 8  # 8 = mdat box header
    ftyp, moov, mdat = assemble(mdat_data_offset)
    return ftyp + moov + mdat, [s["plain"] for s in samples], mdat_data_offset


def test_subsample_decrypt_roundtrip():
    key = bytes.fromhex("000102030405060708090a0b0c0d0e0f")
    samples = [
        {"iv": b"\x11\x22\x33\x44\x55\x66\x77\x88",
         "plain": b"HDR0" + b"ENCRYPTEDPAYLOAD_1", "subs": [(4, 18)]},
        {"iv": b"\xaa\xbb\xcc\xdd\xee\xff\x00\x11",
         "plain": b"NALHEADER" + b"secretbytes" + b"MORE" + b"moresecret",
         "subs": [(9, 11), (4, 10)]},
    ]
    encrypted, expected, off = _build_mp4(key, samples, has_sub=True)
    plain_file = cenc.decrypt_cenc_mp4(encrypted, key.hex())

    # The decrypted mdat region must equal the concatenated plaintext samples.
    cursor = off
    for exp in expected:
        assert plain_file[cursor:cursor + len(exp)] == exp
        cursor += len(exp)


def test_full_sample_decrypt_roundtrip():
    key = bytes.fromhex("0f0e0d0c0b0a09080706050403020100")
    samples = [
        {"iv": b"\x01\x02\x03\x04\x05\x06\x07\x08",
         "plain": b"whole-sample-plaintext-no-subsamples", "subs": []},
    ]
    encrypted, expected, off = _build_mp4(key, samples, has_sub=False)
    plain_file = cenc.decrypt_cenc_mp4(encrypted, key.hex())
    assert plain_file[off:off + len(expected[0])] == expected[0]


def test_bad_key_length_rejected():
    import pytest
    with pytest.raises(ValueError):
        cenc.decrypt_cenc_mp4(b"\x00" * 64, "0011")  # 2-byte key


def test_no_moov_rejected():
    import pytest
    ftyp = _box(b"ftyp", b"isom")
    with pytest.raises(ValueError):
        cenc.decrypt_cenc_mp4(ftyp, "00" * 16)


def test_box_parser_64bit_size():
    # size==1 → 64-bit extended size follows
    payload = b"data"
    box = struct.pack(">I", 1) + b"free" + struct.pack(">Q", 16 + len(payload)) + payload
    boxes = cenc.parse_boxes(box, 0, len(box))
    assert boxes[0][0] == "free"
    assert boxes[0][2] == 16  # header length
