"""AES-128 ECB 解密（纯 Python，不加依赖）。只给引力引擎榜单解密用，每页十几 KB，够快。

和助手那边用 JavaScript 写的同一套逻辑一一对应：那套已经用 FIPS-197 标准测试数据和引力引擎真实返回验证过。
"""


def _xt(a):
    a <<= 1
    return (a ^ 0x11B) if a & 0x100 else a


_EXP = [0] * 512
_LOG = [0] * 256
_x = 1
for _i in range(255):
    _EXP[_i] = _x
    _LOG[_x] = _i
    _x ^= _xt(_x)          # 乘 3
for _i in range(255, 512):
    _EXP[_i] = _EXP[_i - 255]


def _mul(a, b):
    return _EXP[_LOG[a] + _LOG[b]] if a and b else 0


SBOX = []
for _v in range(256):
    _inv = _EXP[255 - _LOG[_v]] if _v else 0
    _s = _r = _inv
    for _k in range(4):
        _r = ((_r << 1) | (_r >> 7)) & 0xFF
        _s ^= _r
    SBOX.append(_s ^ 0x63)
INV = [0] * 256
for _i, _v in enumerate(SBOX):
    INV[_v] = _i
M9 = [_mul(a, 9) for a in range(256)]
M11 = [_mul(a, 11) for a in range(256)]
M13 = [_mul(a, 13) for a in range(256)]
M14 = [_mul(a, 14) for a in range(256)]
# 逆行移位：输出第 4c+r 个字节 = 输入第 r + 4*((c-r) mod 4) 个
P = [r + 4 * ((c - r) % 4) for c in range(4) for r in range(4)]


def _expand(key):
    w = [list(key[4 * i:4 * i + 4]) for i in range(4)]
    rcon = 1
    for i in range(4, 44):
        t = list(w[i - 1])
        if i % 4 == 0:
            t = [SBOX[b] for b in t[1:] + t[:1]]
            t[0] ^= rcon
            rcon = _xt(rcon)
        w.append([w[i - 4][j] ^ t[j] for j in range(4)])
    return [sum(w[4 * r:4 * r + 4], []) for r in range(11)]


def _dec_block(block, rk):
    s = [b ^ k for b, k in zip(block, rk[10])]
    for rnd in range(9, 0, -1):
        s = [INV[s[p]] for p in P]
        s = [b ^ k for b, k in zip(s, rk[rnd])]
        o = [0] * 16
        for c in range(0, 16, 4):
            a0, a1, a2, a3 = s[c], s[c + 1], s[c + 2], s[c + 3]
            o[c] = M14[a0] ^ M11[a1] ^ M13[a2] ^ M9[a3]
            o[c + 1] = M9[a0] ^ M14[a1] ^ M11[a2] ^ M13[a3]
            o[c + 2] = M13[a0] ^ M9[a1] ^ M14[a2] ^ M11[a3]
            o[c + 3] = M11[a0] ^ M13[a1] ^ M9[a2] ^ M14[a3]
        s = o
    s = [INV[s[p]] for p in P]
    return [b ^ k for b, k in zip(s, rk[0])]


def ecb_decrypt(key, data):
    """key：16 字节；data：密文字节。返回去掉 PKCS7 填充后的明文字节。"""
    if len(key) != 16:
        raise ValueError("密钥要 16 字节")
    if not data or len(data) % 16:
        raise ValueError("密文长度不对")
    rk = _expand(list(key))
    out = bytearray()
    for o in range(0, len(data), 16):
        out.extend(_dec_block(list(data[o:o + 16]), rk))
    pad = out[-1]
    if not 1 <= pad <= 16 or out[-pad:] != bytes([pad]) * pad:
        raise ValueError("解密失败（填充不对，可能对方换了算法）")
    return bytes(out[:-pad])


def _selftest():
    rk = _expand(list(bytes.fromhex("000102030405060708090a0b0c0d0e0f")))
    got = bytes(_dec_block(list(bytes.fromhex("69c4e0d86a7b0430d8cdb78070b4c55a")), rk)).hex()
    return got == "00112233445566778899aabbccddeeff"


if not _selftest():
    raise RuntimeError("AES 自检没通过")
