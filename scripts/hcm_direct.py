#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""平台 object 直连脚本（绕过 8787 同源代理，服务端直接调用 HCM 网关）。

为什么需要它：
  前端 web-react 因浏览器同源策略无法直接连 HCM 网关，必须走 8787 的 /hcm-api 代理；
  但代理链路一旦出问题（CORS/代理进程回收/转发异常），object 列表/元数据就取不到。
  本脚本在服务端/本地直接用 Python 复刻前端 web-react/src/api/hcm/crypto.ts 的
  AES-256-CBC + SM3 加解密逻辑，POST 到 HCM 网关 /api/<api_name>，带 token cookie，
  不经过任何中间代理，也就没有 CORS / 代理转发那一层报错来源。

加密协议（必须与前端 crypto.ts 完全一致，否则平台返回 40016 规则校验不通过）：
  - 请求体：{ hcm_transfer_strategy: 'ha',
               hcm_param: AES-256-CBC(PKCS7, key, iv).encrypt(base64(utf8(json(params)))),
               s3h: sm3(hcm_param + 'hcm' + 'cloud') }   # 注意：仅此处拼一次 'hcm'+'cloud'
  - 响应体（策略 hb5）：hcm_param = 3位随机串 + 混淆(base64(utf8(json(result))))
        混淆还原：把 'a' 换回 '5'，把 '!' 换回 'a'，再 base64 解码得 json。

配置来源：config/hcm_whitelist.local.json（真实值，已 gitignore）
  - proxy_target.base_url : HCM 网关基址，如 http://73.2.192.1:port
  - token                 : 可选预置 token（也可命令行 --token 覆盖）

用法示例：
  python3 scripts/hcm_direct.py list
  python3 scripts/hcm_direct.py list --query "core.ds" --page 2 --page-size 10
  python3 scripts/hcm_direct.py meta --model main.setting.hcm_model
  python3 scripts/hcm_direct.py list --token "2|1:0|10:..." --base-url http://73.2.192.1:8899
"""
import argparse
import base64
import json
import os
import sys
import urllib.request
import urllib.error

# --------------------------------------------------------------------------- #
#  加解密原语（与 crypto.ts 字节级一致，零外部依赖）
# --------------------------------------------------------------------------- #
KEY = b"2e35f242a46d67eeb74aabc37d5e5d06"  # 32 字节 = AES-256
IV = b"fedcba0987654321"                    # 16 字节


def _pkcs7_pad(data: bytes) -> bytes:
    pad_len = 16 - (len(data) % 16)
    return data + bytes([pad_len]) * pad_len


def _pkcs7_unpad(data: bytes) -> bytes:
    if not data:
        return data
    pad_len = data[-1]
    if 1 <= pad_len <= 16:
        return data[:-pad_len]
    return data


# AES-256-CBC：优先使用 PyCryptodome（Crypto），缺失时回退纯 Python 实现。
try:
    from Crypto.Cipher import AES as _PyAES
    _HAVE_CRYPTO = True
except Exception:
    _HAVE_CRYPTO = False

    # ---- 纯 Python AES-256 回退（仅在无 Crypto 时使用） ----
    _SBOX = [
        0x63, 0x7c, 0x77, 0x7b, 0xf2, 0x6b, 0x6f, 0xc5, 0x30, 0x01, 0x67, 0x2b, 0xfe, 0xd7, 0xab, 0x76,
        0xca, 0x82, 0xc9, 0x7d, 0xfa, 0x59, 0x47, 0xf0, 0xad, 0xd4, 0xa2, 0xaf, 0x9c, 0xa4, 0x72, 0xc0,
        0xb7, 0xfd, 0x93, 0x26, 0x36, 0x3f, 0xf7, 0xcc, 0x34, 0xa5, 0xe5, 0xf1, 0x71, 0xd8, 0x31, 0x15,
        0x04, 0xc7, 0x23, 0xc3, 0x18, 0x96, 0x05, 0x9a, 0x07, 0x12, 0x80, 0xe2, 0xeb, 0x27, 0xb2, 0x75,
        0x09, 0x83, 0x2c, 0x1a, 0x1b, 0x6e, 0x5a, 0xa0, 0x52, 0x3b, 0xd6, 0xb3, 0x29, 0xe3, 0x2f, 0x84,
        0x53, 0xd1, 0x00, 0xed, 0x20, 0xfc, 0xb1, 0x5b, 0x6a, 0xcb, 0xbe, 0x39, 0x4a, 0x4c, 0x58, 0xcf,
        0xd0, 0xef, 0xaa, 0xfb, 0x43, 0x4d, 0x33, 0x85, 0x45, 0xf9, 0x02, 0x7f, 0x50, 0x3c, 0x9f, 0xa8,
        0x51, 0xa3, 0x40, 0x8f, 0x92, 0x9d, 0x38, 0xf5, 0xbc, 0xb6, 0xda, 0x21, 0x10, 0xff, 0xf3, 0xd2,
        0xcd, 0x0c, 0x13, 0xec, 0x5f, 0x97, 0x44, 0x17, 0xc4, 0xa7, 0x7e, 0x3d, 0x64, 0x5d, 0x19, 0x73,
        0x60, 0x81, 0x4f, 0xdc, 0x22, 0x2a, 0x90, 0x88, 0x46, 0xee, 0xb8, 0x14, 0xde, 0x5e, 0x0b, 0xdb,
        0xe0, 0x32, 0x3a, 0x0a, 0x49, 0x06, 0x24, 0x5c, 0xc2, 0xd3, 0xac, 0x62, 0x91, 0x95, 0xe4, 0x79,
        0xe7, 0xc8, 0x37, 0x6d, 0x8d, 0xd5, 0x4e, 0xa9, 0x6c, 0x56, 0xf4, 0xea, 0x65, 0x7a, 0xae, 0x08,
        0xba, 0x78, 0x25, 0x2e, 0x1c, 0xa6, 0xb4, 0xc6, 0xe8, 0xdd, 0x74, 0x1f, 0x4b, 0xbd, 0x8b, 0x8a,
        0x70, 0x3e, 0xb5, 0x66, 0x48, 0x03, 0xf6, 0x0e, 0x61, 0x35, 0x57, 0xb9, 0x86, 0xc1, 0x1d, 0x9e,
        0xe1, 0xf8, 0x98, 0x11, 0x69, 0xd9, 0x8e, 0x94, 0x9b, 0x1e, 0x87, 0xe9, 0xce, 0x55, 0x28, 0xdf,
        0x8c, 0xa1, 0x89, 0x0d, 0xbf, 0xe6, 0x42, 0x68, 0x41, 0x99, 0x2d, 0x0f, 0xb0, 0x54, 0xbb, 0x16,
    ]
    _INV_SBOX = [0] * 256
    for _i, _v in enumerate(_SBOX):
        _INV_SBOX[_v] = _i
    _RCON = [0x01, 0x02, 0x04, 0x08, 0x10, 0x20, 0x40, 0x80, 0x1B, 0x36, 0x6C]

    def _gmul(a, b):
        p = 0
        for _ in range(8):
            if b & 1:
                p ^= a
            hi = a & 0x80
            a = (a << 1) & 0xFF
            if hi:
                a ^= 0x1B
            b >>= 1
        return p

    def _key_expansion(key):
        Nk = 8
        w = [list(key[4 * i:4 * i + 4]) for i in range(Nk)]
        for i in range(Nk, 60):
            temp = list(w[i - 1])
            if i % Nk == 0:
                temp = temp[1:] + temp[:1]
                temp = [_SBOX[b] for b in temp]
                temp[0] ^= _RCON[i // Nk - 1]
            elif i % Nk == 4:
                temp = [_SBOX[b] for b in temp]
            w.append([w[i - Nk][j] ^ temp[j] for j in range(4)])
        return [bytes(w[4 * r] + w[4 * r + 1] + w[4 * r + 2] + w[4 * r + 3]) for r in range(15)]

    def _aes_transform(state, decrypt=False, final=False):
        """单轮的主体变换（不含 AddRoundKey）。state 为长度 16 的字节列表/字节。
        final=True 时跳过 MixColumns/InvMixColumns（最终轮使用）。"""
        state = list(state)
        if not decrypt:
            # SubBytes
            state = [_SBOX[b] for b in state]
            # ShiftRows（列主序，行 r 循环左移 r 位）
            s = state[:]
            for r in range(4):
                for c in range(4):
                    state[r + 4 * c] = s[r + 4 * ((c + r) % 4)]
            if final:
                return state
            # MixColumns
            s = state[:]
            for c in range(4):
                i = 4 * c
                a0, a1, a2, a3 = s[i], s[i + 1], s[i + 2], s[i + 3]
                state[i] = _gmul(a0, 2) ^ _gmul(a1, 3) ^ a2 ^ a3
                state[i + 1] = a0 ^ _gmul(a1, 2) ^ _gmul(a2, 3) ^ a3
                state[i + 2] = a0 ^ a1 ^ _gmul(a2, 2) ^ _gmul(a3, 3)
                state[i + 3] = _gmul(a0, 3) ^ a1 ^ a2 ^ _gmul(a3, 2)
        else:
            if not final:
                # InvMixColumns（逆变换轮才有，最终逆轮无）
                s = state[:]
                for c in range(4):
                    i = 4 * c
                    a0, a1, a2, a3 = s[i], s[i + 1], s[i + 2], s[i + 3]
                    state[i] = _gmul(a0, 14) ^ _gmul(a1, 11) ^ _gmul(a2, 13) ^ _gmul(a3, 9)
                    state[i + 1] = _gmul(a0, 9) ^ _gmul(a1, 14) ^ _gmul(a2, 11) ^ _gmul(a3, 13)
                    state[i + 2] = _gmul(a0, 13) ^ _gmul(a1, 9) ^ _gmul(a2, 14) ^ _gmul(a3, 11)
                    state[i + 3] = _gmul(a0, 11) ^ _gmul(a1, 13) ^ _gmul(a2, 9) ^ _gmul(a3, 14)
            # InvShiftRows（行 r 循环右移 r 位，加密 ShiftRows 的逆）
            s = state[:]
            for r in range(4):
                for c in range(4):
                    state[r + 4 * c] = s[r + 4 * ((c - r) % 4)]
            # InvSubBytes
            state = [_INV_SBOX[b] for b in state]
        return state

    def _ark(state, rk):
        """AddRoundKey。"""
        return bytes(state[i] ^ rk[i] for i in range(16))

    def _aes_cbc_encrypt_fallback(plain):
        rks = _key_expansion(KEY)
        ct = bytearray()
        prev = IV
        padded = _pkcs7_pad(plain)
        for i in range(0, len(padded), 16):
            block = padded[i:i + 16]
            state = [block[j] ^ prev[j] for j in range(16)]
            state = _ark(state, rks[0])                      # 初始 AddRoundKey(0)
            for r in range(1, 14):
                state = _aes_transform(state)
                state = _ark(state, rks[r])
            state = _aes_transform(state, final=True)        # 最终轮（无 MixColumns）
            state = _ark(state, rks[14])
            prev = state
            ct += state
        return bytes(ct)

    def _aes_cbc_decrypt_fallback(cipher_bytes):
        rks = _key_expansion(KEY)
        pt = bytearray()
        prev = IV
        for i in range(0, len(cipher_bytes), 16):
            block = cipher_bytes[i:i + 16]
            # 解密 = 加密的严格逆序（ARK 不能与 InvMixColumns 交换，必须按标准顺序）：
            #   ARK(14) → InvSR InvSB（最终轮逆，无 InvMix）
            #   → [ARK(r) → InvMix InvSR InvSB] r=13..1
            #   → ARK(0)
            state = list(block)
            state = _ark(state, rks[14])
            state = _aes_transform(state, decrypt=True, final=True)
            for r in range(13, 0, -1):
                state = _ark(state, rks[r])
                state = _aes_transform(state, decrypt=True)
            state = _ark(state, rks[0])
            state = bytes([state[j] ^ prev[j] for j in range(16)])
            pt += state
            prev = block
        return _pkcs7_unpad(bytes(pt))


def _aes_cbc_encrypt(plain: bytes) -> bytes:
    if _HAVE_CRYPTO:
        from Crypto.Util.Padding import pad
        return _PyAES.new(KEY, _PyAES.MODE_CBC, IV).encrypt(pad(plain, 16))
    return _aes_cbc_encrypt_fallback(plain)


def _aes_cbc_decrypt(cipher_bytes: bytes) -> bytes:
    if _HAVE_CRYPTO:
        from Crypto.Util.Padding import unpad
        return unpad(_PyAES.new(KEY, _PyAES.MODE_CBC, IV).decrypt(cipher_bytes), 16)
    return _aes_cbc_decrypt_fallback(cipher_bytes)


def _sm3(msg: str) -> str:
    """SM3 摘要（GB/T 32905-2016），返回 64 位十六进制小写。

    采用经过交叉验证的标准实现（与 gmssl / sm-crypto 输出一致）。
    """
    IV = [0x7380166F, 0x4914B2B9, 0x172442D7, 0xDA8A0600,
          0xA96F30BC, 0x163138AA, 0xE38DEE4D, 0xB0FB0E4E]

    def rotl(x, n):
        n &= 31
        return ((x << n) | (x >> (32 - n))) & 0xFFFFFFFF

    def t(j):
        return 0x79CC4519 if j < 16 else 0x7A879D8A

    def ff(x, y, z, j):
        return x ^ y ^ z if j < 16 else (x & y) | (x & z) | (y & z)

    def gg(x, y, z, j):
        return x ^ y ^ z if j < 16 else (x & y) | ((~x & 0xFFFFFFFF) & z)

    def p0(x):
        return x ^ rotl(x, 9) ^ rotl(x, 17)

    def p1(x):
        return x ^ rotl(x, 15) ^ rotl(x, 23)

    data = bytearray(msg.encode("utf-8"))
    ml = (len(data) * 8) & 0xFFFFFFFFFFFFFFFF
    data.append(0x80)
    while (len(data) * 8) % 512 != 448:
        data.append(0x00)
    data += ml.to_bytes(8, "big")

    def bytes_to_word(b):
        return int.from_bytes(b, "big")

    for i in range(0, len(data), 64):
        w = [bytes_to_word(data[i + 4 * j:i + 4 * j + 4]) for j in range(16)]
        for j in range(16, 68):
            w.append(p1(w[j - 16] ^ w[j - 9] ^ rotl(w[j - 3], 15)) ^ rotl(w[j - 13], 7) ^ w[j - 6])
        w1 = [w[j] ^ w[j + 4] for j in range(64)]

        a, b, c, d, e, f, g, h = IV
        for j in range(64):
            ss1 = rotl((rotl(a, 12) + e + rotl(t(j), j)) & 0xFFFFFFFF, 7)
            ss2 = ss1 ^ rotl(a, 12)
            tt1 = (ff(a, b, c, j) + d + ss2 + w1[j]) & 0xFFFFFFFF
            tt2 = (gg(e, f, g, j) + h + ss1 + w[j]) & 0xFFFFFFFF
            d = c
            c = rotl(b, 9)
            b = a
            a = tt1
            h = g
            g = rotl(f, 19)
            f = e
            e = p0(tt2)
        IV = [(x ^ y) & 0xFFFFFFFF for x, y in zip(IV, (a, b, c, d, e, f, g, h))]

    return "".join(f"{x:08x}" for x in IV)


def b64_of_utf8(s: str) -> str:
    """等价 JS：base64.encode(unescape(encodeURIComponent(json)))"""
    raw = s.encode("utf-8")
    return base64.b64encode(raw).decode("ascii")


def utf8_of_b64(b64: str) -> str:
    return base64.b64decode(b64).decode("utf-8")


def encrypt_param(params: dict) -> str:
    b64 = b64_of_utf8(json.dumps(params, ensure_ascii=False))
    ct = _aes_cbc_encrypt(b64.encode("ascii"))
    return base64.b64encode(ct).decode("ascii")


def sign_param(hcm_param: str) -> str:
    # 仅此处拼一次 'hcm'+'cloud'（与 crypto.ts 的 signParam 内部一致）
    return _sm3(hcm_param + "hcm" + "cloud")


def decrypt_param(hcm_param: str, strategy: str = "hb5") -> dict:
    if strategy == "hb5":
        raw = hcm_param[3:]  # 去 3 位随机串
        restored = raw.replace("a", "5").replace("!", "a")  # a<->! , 5<->a 还原
        return json.loads(utf8_of_b64(restored))
    # ha 分支兜底（对称解密）
    ct = base64.b64decode(hcm_param)
    b64 = _aes_cbc_decrypt(ct).decode("ascii")
    return json.loads(utf8_of_b64(b64))


# --------------------------------------------------------------------------- #
#  配置加载
# --------------------------------------------------------------------------- #
def load_config():
    here = os.path.dirname(os.path.abspath(__file__))
    root = os.path.dirname(here)
    candidates = [
        os.path.join(root, "config", "hcm_whitelist.local.json"),
        os.path.join(root, "config", "hcm_whitelist.json"),
    ]
    cfg = {}
    for p in candidates:
        if os.path.exists(p):
            try:
                with open(p, "r", encoding="utf-8") as f:
                    cfg = json.load(f)
                break
            except Exception:
                continue
    base_url = (cfg.get("proxy_target", {}) or {}).get("base_url", "") or ""
    token = (cfg.get("token", "") or "").strip()
    return base_url, token


# --------------------------------------------------------------------------- #
#  直连调用
# --------------------------------------------------------------------------- #
def hcm_call(base_url: str, token: str, api_name: str, params: dict, model: str = ""):
    if not base_url:
        raise SystemExit("错误：未配置 HCM 网关 base_url（config/hcm_whitelist.local.json 的 proxy_target.base_url）")
    hp = encrypt_param(params)
    body = {
        "hcm_transfer_strategy": "ha",
        "hcm_param": hp,
        "s3h": sign_param(hp),  # 单拼，与修复后的 client.ts 一致
    }
    target = f"{base_url.rstrip('/')}/api/{api_name}"
    if model:
        target += f"?model={model}"

    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(target, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    if token:
        # 与代理一致：token 走 cookie
        req.add_header("Cookie", f"token={token}")

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            text = resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        text = e.read().decode("utf-8", "replace")
        raise SystemExit(f"HTTP {e.code} 调用 {api_name} 失败: {text[:500]}")
    except urllib.error.URLError as e:
        raise SystemExit(f"无法连接 HCM 网关 {base_url}: {e.reason}")

    try:
        data_json = json.loads(text)
    except json.JSONDecodeError:
        raise SystemExit(f"响应非 JSON（可能网关异常）: {text[:500]}")

    if isinstance(data_json, dict) and data_json.get("hcm_transfer_strategy") and data_json.get("hcm_param"):
        inner = decrypt_param(data_json["hcm_param"], data_json["hcm_transfer_strategy"])
        if "result" in inner:
            return inner["result"]
        return inner
    # 已明文或带 errcode
    if isinstance(data_json, dict) and data_json.get("errcode"):
        raise SystemExit(f"HCM 业务错误 errcode={data_json.get('errcode')} errmsg={data_json.get('errmsg')}")
    return data_json


# --------------------------------------------------------------------------- #
#  业务封装（对应前端 client.ts 的 hcmObjectList / hcmModelMeta）
# --------------------------------------------------------------------------- #
def object_list(base_url, token, query=None, page_index=1, page_size=20,
                base_object_str="hcm.paas.object", key="main.setting.hcm_model"):
    filter_params = {
        "filter_str": None,
        "page_index": page_index,
        "page_size": page_size,
        "advance_filter_dict": {},
        "show_fields_key": ["class_", "model_category", "update_time"],
        "base_object_str": base_object_str,
        "key": key,
        "query_str": query,
    }
    params = {
        "model": None,
        "filter_str": None,
        "filter_dict": {},
        "page_index": page_index,
        "page_size": page_size,
        "extra_property": {"sorts": [], "filter_params": filter_params, "only_list": False},
        "biz_type": "list",
    }
    return hcm_call(base_url, token, "hcm.paas.object.list", params)


def model_meta(base_url, token, model):
    return hcm_call(base_url, token, "hcm.model.meta", {"model": model}, model=model)


# --------------------------------------------------------------------------- #
#  CLI
# --------------------------------------------------------------------------- #
def main():
    parser = argparse.ArgumentParser(description="HCM object 直连脚本（绕过 8787 代理）")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_list = sub.add_parser("list", help="获取 object 列表 (hcm.paas.object.list)")
    p_list.add_argument("--query", default=None, help="按名称/关键字过滤")
    p_list.add_argument("--page", type=int, default=1)
    p_list.add_argument("--page-size", type=int, default=20)
    p_list.add_argument("--base-object", default="hcm.paas.object")
    p_list.add_argument("--key", default="main.setting.hcm_model")

    p_meta = sub.add_parser("meta", help="获取单对象元数据 (hcm.model.meta)")
    p_meta.add_argument("--model", required=True, help="对象 model，如 main.setting.hcm_model")

    for p in (p_list, p_meta):
        p.add_argument("--base-url", default=None, help="覆盖 HCM 网关基址")
        p.add_argument("--token", default=None, help="覆盖 token")

    args = parser.parse_args()

    base_url, cfg_token = load_config()
    base_url = args.base_url or base_url
    token = args.token or cfg_token

    if args.cmd == "list":
        res = object_list(base_url, token, query=args.query, page_index=args.page,
                          page_size=args.page_size, base_object_str=args.base_object, key=args.key)
    else:
        res = model_meta(base_url, token, args.model)

    print(json.dumps(res, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
