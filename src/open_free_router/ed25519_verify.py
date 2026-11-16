# -*- coding: utf-8 -*-
"""PRD-466/T2：纯 Python RFC 8032 Ed25519（验证为主，含测试用 keygen/sign）。

本仓库零加密依赖（pyproject 只有 pyyaml/requests/ruamel），按最小依赖纪律
自实现 RFC 8032 Ed25519。算术用**仿射坐标**直接实现曲线方程
  -x² + y² = 1 + d·x²y²  （d = -121665/121666，p = 2^255-19）
  加法：x3=(x1y2+y1x2)/(1+d·x1x2y1y2)，y3=(y1y2+x1x2)/(1-d·x1x2y1y2)
  倍点：x3=2x1y1/(1+d·x1²y1²)，y3=(y1²+x1²)/(1-d·x1²y1²)
（投影坐标的 hwcd-3 公式口径在 a=-1 曲线上极易写错——首次自测连错两次后
改走仿射，直接对曲线方程自查；仿射每步一次模逆，单目录验签性能足够。）

验证主口径（RFC 8032 §5.1.7 cofactorless 形式）：校验 s·B == R + h(R||A||M)·A。
任何异常一律 False——验签永不抛错（T2 丢弃路径依赖此语义）。
"""
from __future__ import annotations

import hashlib
import secrets

_P = 2 ** 255 - 19
_L = 2 ** 252 + 27742317777372353535851937790883648493
_D = (-121665 * pow(121666, _P - 2, _P)) % _P


def _inv(x: int) -> int:
    return pow(x, _P - 2, _P)


def _recover_x(y: int, sign_bit: int) -> int:
    if y >= _P:
        raise ValueError("y out of range")
    x2 = (y * y - 1) * _inv(_D * y * y + 1) % _P
    x = pow(x2, (_P + 3) // 8, _P)
    if (x * x - x2) % _P != 0:
        x = x * pow(2, (_P - 1) // 4, _P) % _P
    if (x * x - x2) % _P != 0:
        raise ValueError("no sqrt")
    if (x & 1) != sign_bit:
        x = _P - x
    return x


# 基点 B：y = 4/5，x 取偶数半
_BY = 4 * pow(5, _P - 2, _P) % _P
_BX = _recover_x(_BY, 0)
_B = (_BX, _BY)
_IDENT = (0, 1)


def _pt_add(p1: tuple, p2: tuple) -> tuple:
    x1, y1 = p1
    x2, y2 = p2
    xden = (1 + _D * x1 * x2 * y1 * y2) % _P
    yden = (1 - _D * x1 * x2 * y1 * y2) % _P
    return ((x1 * y2 + y1 * x2) * _inv(xden) % _P,
            (y1 * y2 + x1 * x2) * _inv(yden) % _P)


def _pt_double(p: tuple) -> tuple:
    x1, y1 = p
    xden = (1 + _D * x1 * x1 * y1 * y1) % _P
    yden = (1 - _D * x1 * x1 * y1 * y1) % _P
    return (2 * x1 * y1 * _inv(xden) % _P,
            (y1 * y1 + x1 * x1) * _inv(yden) % _P)


def _pt_mul(k: int, p: tuple) -> tuple:
    """倍加（仿射，每步一次模逆）。"""
    q = _IDENT
    while k > 0:
        if k & 1:
            q = _pt_add(q, p)
        p = _pt_double(p)
        k >>= 1
    return q


def _compress(point: tuple) -> bytes:
    x, y = point
    return int.to_bytes(y | ((x & 1) << 255), 32, "little")


def _decompress(data: bytes) -> tuple:
    if len(data) != 32:
        raise ValueError("bad point length")
    y = int.from_bytes(data, "little")
    sign_bit = y >> 255
    y &= (1 << 255) - 1
    x = _recover_x(y, sign_bit)
    return (x, y)


def keygen(seed: bytes | None = None) -> tuple[bytes, bytes]:
    """测试用：返回 (公钥 32B, 私钥 32B)。"""
    sk = seed or secrets.token_bytes(32)
    h = hashlib.sha512(sk).digest()
    a = int.from_bytes(h[:32], "little") & ((1 << 254) - 8) | (1 << 254)
    pk = _compress(_pt_mul(a, _B))
    return pk, sk


def sign(sk: bytes, msg: bytes) -> bytes:
    """测试用/源站模拟：RFC 8032 签名（确定性，r 由前半摘要派生）。"""
    h = hashlib.sha512(sk).digest()
    a = int.from_bytes(h[:32], "little") & ((1 << 254) - 8) | (1 << 254)
    pk = _compress(_pt_mul(a, _B))
    r = int.from_bytes(hashlib.sha512(h[32:] + msg).digest(), "little") % _L
    big_r = _compress(_pt_mul(r, _B))
    k = int.from_bytes(hashlib.sha512(big_r + pk + msg).digest(), "little") % _L
    s = (r + k * a) % _L
    return big_r + int.to_bytes(s, 32, "little")


def verify(pk: bytes, msg: bytes, signature: bytes) -> bool:
    """RFC 8032 验签（cofactorless 形式）：校验 s·B == R + h(R||A||M)·A。

    RFC 8032 §5.1.7 允许两种形式：8sB == 8(R + kA)（两侧同乘 8）或
    sB == R + kA（cofactorless）。首次实现曾错写成「8sB == R + 8kA」
    （左侧乘 8 右侧没乘）——两侧倍数不一致的等式永远不成立，被 RFC 官方
    测试向量当场抓住。此处取 cofactorless 形式（与 PyNaCl 交叉验证过）。
    永不抛错——验签异常一律 False（T2 丢弃路径依赖此语义）。
    """
    try:
        if len(pk) != 32 or len(signature) != 64:
            return False
        a = _decompress(pk)
        r_bytes = signature[:32]
        s = int.from_bytes(signature[32:], "little")
        if s >= _L:
            return False
        big_r = _decompress(r_bytes)
        k = int.from_bytes(hashlib.sha512(r_bytes + pk + msg).digest(),
                           "little") % _L
        lhs = _pt_mul(s, _B)
        rhs = _pt_add(big_r, _pt_mul(k, a))
        return _compress(lhs) == _compress(rhs)
    except (ValueError, TypeError):
        return False
