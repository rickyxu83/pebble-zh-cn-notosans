#!/usr/bin/env python3
"""Build a PebbleOS Simplified Chinese language pack (.pbl) from po/zh_CN.po
and existing PBF font slots (extracted from a base .pbl or a fonts directory).
"""

from __future__ import annotations

import argparse
import hashlib
import os
import struct
import subprocess
import tempfile
import time
from pathlib import Path

PBPACK_TABLE_ENTRIES = 256
PBPACK_DATA_START = 12 + PBPACK_TABLE_ENTRIES * 16
PBL_NUM_SLOTS = 19  # Slot 1 (.mo) + Slots 2..19 (fonts; 12..18 empty)

# Precomputed MSB-first CRC-32 lookup table (STM32 polynomial 0x04C11DB7)
_CRC32_TABLE = []
for _i in range(256):
    _c = _i << 24
    for _ in range(8):
        _c = ((_c << 1) ^ 0x04C11DB7) & 0xFFFFFFFF if (_c & 0x80000000) else ((_c << 1) & 0xFFFFFFFF)
    _CRC32_TABLE.append(_c)


def stm32_crc32(data: bytes, init: int = 0xFFFFFFFF) -> int:
    """Compute PebbleOS / STM32 hardware CRC-32 (matches tools/stm32_crc.py).

    Full 4-byte little-endian words are fed MSB-first (b3, b2, b1, b0).
    A trailing partial word (< 4 bytes) is padded per PebbleOS's legacy rule:
    `tail = tail[::-1] + b'\\x00' * (4 - len(tail))`, then fed reversed
    (i.e. leading zero bytes followed by the trailing bytes in original order).
    """
    crc = init
    table = _CRC32_TABLE
    mv = memoryview(data)
    full_len = len(data) & ~3
    for i in range(0, full_len, 4):
        crc = table[((crc >> 24) ^ mv[i + 3]) & 0xFF] ^ ((crc << 8) & 0xFFFFFFFF)
        crc = table[((crc >> 24) ^ mv[i + 2]) & 0xFF] ^ ((crc << 8) & 0xFFFFFFFF)
        crc = table[((crc >> 24) ^ mv[i + 1]) & 0xFF] ^ ((crc << 8) & 0xFFFFFFFF)
        crc = table[((crc >> 24) ^ mv[i]) & 0xFF] ^ ((crc << 8) & 0xFFFFFFFF)
    rem = len(data) - full_len
    if rem:
        tail = bytes(mv[full_len:])[::-1] + b"\x00" * (4 - rem)
        for b in reversed(tail):
            crc = table[((crc >> 24) ^ b) & 0xFF] ^ ((crc << 8) & 0xFFFFFFFF)
    return crc


def _is_prime(n: int) -> bool:
    if n < 2:
        return False
    if n % 2 == 0:
        return n == 2
    d = 3
    while d * d <= n:
        if n % d == 0:
            return False
        d += 2
    return True


def _next_prime(n: int) -> int:
    while not _is_prime(n):
        n += 1
    return n


def _pjw_hash(b: bytes) -> int:
    hval = 0
    for c in b:
        hval = ((hval << 4) + c) & 0xFFFFFFFF
        g = hval & 0xF0000000
        if g:
            hval ^= g
            hval ^= g >> 24
    return hval & 0xFFFFFFFF


def _optimize_mo(mo_bytes: bytes) -> bytes:
    """Repack a GNU MO binary with a compact PJW hash table (keeping RAM usage
    below 4.5 KiB on watch kernel heap) and 4-byte aligned length (avoiding
    partial-word CRC-32 edge cases).
    """
    magic, rev, n, oo, to, _hs, _ho = struct.unpack_from("<7I", mo_bytes, 0)
    if magic != 0x950412DE or rev != 0:
        raise RuntimeError(f"Unsupported MO header: magic=0x{magic:08x}, rev={rev}")

    pairs: list[tuple[bytes, bytes]] = []
    for i in range(n):
        ol, op = struct.unpack_from("<II", mo_bytes, oo + i * 8)
        tl, tp = struct.unpack_from("<II", mo_bytes, to + i * 8)
        pairs.append((mo_bytes[op : op + ol], mo_bytes[tp : tp + tl]))

    pairs.sort(key=lambda kv: kv[0])

    # Choose a compact prime hash table size (~1.15x N, e.g. 1103 for 956 entries = 4,412 B)
    hs = _next_prime(max(n + 64, int(n * 1.15)))
    ht = [0] * hs
    for idx, (orig, _trans) in enumerate(pairs):
        h = _pjw_hash(orig)
        step = (h % (hs - 2)) + 1
        pos = h % hs
        while ht[pos] != 0:
            pos = pos + step - (hs if pos >= hs - step else 0)
        ht[pos] = idx + 1

    orig_tbl_off = 28
    trans_tbl_off = orig_tbl_off + n * 8
    hash_tbl_off = trans_tbl_off + n * 8
    data_off = hash_tbl_off + hs * 4

    orig_tbl = bytearray()
    trans_tbl = bytearray()
    orig_blob = bytearray()
    trans_blob = bytearray()

    cur_o = data_off
    for orig, _ in pairs:
        orig_tbl.extend(struct.pack("<II", len(orig), cur_o))
        orig_blob.extend(orig + b"\x00")
        cur_o += len(orig) + 1

    cur_t = cur_o
    for _, trans in pairs:
        trans_tbl.extend(struct.pack("<II", len(trans), cur_t))
        trans_blob.extend(trans + b"\x00")
        cur_t += len(trans) + 1

    header = struct.pack("<7I", 0x950412DE, 0, n, orig_tbl_off, trans_tbl_off, hs, hash_tbl_off)
    ht_bytes = struct.pack(f"<{hs}I", *ht)
    out = bytearray(header + orig_tbl + trans_tbl + ht_bytes + orig_blob + trans_blob)

    # Pad to a 4-byte multiple so Slot 1 and total payload have zero partial-word CRC tails
    rem = len(out) % 4
    if rem:
        out.extend(b"\x00" * (4 - rem))
    return bytes(out)


def compile_po_to_mo(po_path: Path) -> bytes:
    with tempfile.NamedTemporaryFile(suffix=".mo", delete=False) as tmp:
        mo_path = Path(tmp.name)
    try:
        cmd = ["msgfmt", "--check-format", "-o", str(mo_path), str(po_path)]
        res = subprocess.run(cmd, capture_output=True, text=True)
        if res.returncode != 0:
            raise RuntimeError(f"msgfmt failed:\n{res.stderr}")
        return _optimize_mo(mo_path.read_bytes())
    finally:
        mo_path.unlink(missing_ok=True)


def extract_font_slots_from_pbl(pbl_path: Path) -> dict[int, bytes]:
    data = pbl_path.read_bytes()
    num_files, _pack_crc, _ts = struct.unpack_from("<III", data, 0)
    slots: dict[int, bytes] = {}
    for i in range(num_files):
        fid, off, length, _ecrc = struct.unpack_from("<IIII", data, 12 + i * 16)
        if fid >= 2:
            slots[fid] = data[PBPACK_DATA_START + off : PBPACK_DATA_START + off + length]
    return slots


def load_font_slots_from_dir(fonts_dir: Path, num_slots: int) -> dict[int, bytes]:
    slots: dict[int, bytes] = {}
    for fid in range(2, num_slots + 1):
        pbf = fonts_dir / f"slot_{fid:02d}.pbf"
        slots[fid] = pbf.read_bytes() if pbf.exists() else b""
    return slots


def pack_pbl(mo_bytes: bytes, font_slots: dict[int, bytes], timestamp: int) -> bytes:
    num_files = max([PBL_NUM_SLOTS, *font_slots.keys()])
    if num_files > PBPACK_TABLE_ENTRIES:
        raise ValueError(f"Too many slots: {num_files} > {PBPACK_TABLE_ENTRIES}")

    resources: list[tuple[int, bytes]] = [(1, mo_bytes)]
    for fid in range(2, num_files + 1):
        resources.append((fid, font_slots.get(fid, b"")))

    table_entries = bytearray(PBPACK_TABLE_ENTRIES * 16)
    payload = bytearray()

    for idx, (fid, chunk) in enumerate(resources):
        off = len(payload)
        length = len(chunk)
        ecrc = stm32_crc32(chunk) if length > 0 else 0xFFFFFFFF
        struct.pack_into("<IIII", table_entries, idx * 16, fid, off, length, ecrc)
        payload.extend(chunk)

    pack_crc = stm32_crc32(bytes(payload))
    header = struct.pack("<III", num_files, pack_crc, timestamp & 0xFFFFFFFF)
    return header + bytes(table_entries) + bytes(payload)


def main() -> int:
    parser = argparse.ArgumentParser(description="Build PebbleOS zh_CN Noto Sans .pbl package")
    parser.add_argument("--po", type=Path, default=Path("po/zh_CN.po"), help="Path to zh_CN.po")
    src_group = parser.add_mutually_exclusive_group(required=True)
    src_group.add_argument("--base-pbl", type=Path, help="Existing .pbl to reuse PBF font slots from")
    src_group.add_argument("--fonts-dir", type=Path, help="Directory containing slot_02.pbf .. slot_19.pbf")
    parser.add_argument("--out", type=Path, required=True, help="Output .pbl file path")
    parser.add_argument("--timestamp", type=int, default=0, help="Header timestamp (default: 0, matching PebbleOS pbpack.py)")
    parser.add_argument("--dump-fonts", type=Path, default=None, help="Optional dir to dump extracted slot_NN.pbf files")
    args = parser.parse_args()

    mo_bytes = compile_po_to_mo(args.po)
    if args.base_pbl is not None:
        font_slots = extract_font_slots_from_pbl(args.base_pbl)
    else:
        font_slots = load_font_slots_from_dir(args.fonts_dir, PBL_NUM_SLOTS)

    if args.dump_fonts is not None:
        args.dump_fonts.mkdir(parents=True, exist_ok=True)
        for fid, chunk in sorted(font_slots.items()):
            if chunk:
                (args.dump_fonts / f"slot_{fid:02d}.pbf").write_bytes(chunk)

    ts = args.timestamp
    pbl_bytes = pack_pbl(mo_bytes, font_slots, ts)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_bytes(pbl_bytes)

    sha256 = hashlib.sha256(pbl_bytes).hexdigest()
    non_empty_fonts = sum(1 for v in font_slots.values() if v)
    print(f"Built {args.out}")
    print(f"  Size:       {len(pbl_bytes):,} bytes ({len(pbl_bytes) / 1024 / 1024:.2f} MiB)")
    print(f"  MO size:    {len(mo_bytes):,} bytes")
    print(f"  Font slots: {non_empty_fonts} non-empty / {max(font_slots)} total")
    print(f"  Timestamp:  {ts}")
    print(f"  SHA-256:    {sha256}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
