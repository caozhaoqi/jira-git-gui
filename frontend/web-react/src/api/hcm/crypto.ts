

import CryptoJS from 'crypto-js';
import { sm3 } from 'sm-crypto';

const KEY = CryptoJS.enc.Utf8.parse('2e35f242a46d67eeb74aabc37d5e5d06'); // BA_KEY.join('')
const IV = CryptoJS.enc.Utf8.parse('fedcba0987654321'); // IV_EKY.join('')

/** $base64.encode(unescape(encodeURIComponent(json))) —— 等价于对 UTF-8 字节做 base64 */
function b64OfUtf8(s: string): string {
  // 先序列化为 JSON（与 JS JSON.stringify 一致：不转义非 ASCII，保持 UTF-8 字节）
  const json = JSON.stringify(s);
  // 转成 Latin1 表示的 UTF-8 字节串，再做标准 base64
  const utf8 = unescape(encodeURIComponent(json));
  return CryptoJS.enc.Latin1.parse(utf8).toString(CryptoJS.enc.Base64);
}

/** 反向：$base64.decode(...) 得到 Latin1 字节串 -> 还原为 UTF-8 JSON 字符串 */
function utf8OfB64(b64: string): string {
  const latin1 = CryptoJS.enc.Base64.parse(b64).toString(CryptoJS.enc.Latin1);
  return decodeURIComponent(escape(latin1));
}

/** ha 加密：AES-256-CBC(PKCS7) 对 base64(json) 再做 base64 */
export function encryptParam(params: any): string {
  const b64 = b64OfUtf8(typeof params === 'string' ? params : JSON.stringify(params));
  const ct = CryptoJS.AES.encrypt(CryptoJS.enc.Utf8.parse(b64), KEY, {
    iv: IV,
    mode: CryptoJS.mode.CBC,
    padding: CryptoJS.pad.Pkcs7,
  });
  return ct.toString();
}

/** sm3 签名：sm3(hcmParam + 'hcm' + 'cloud')。
 *  注意：本函数内部已拼接 'hcm'+'cloud'，调用方只需传入 hcm_param（密文），切勿再手动拼接，
 *  否则会出现双重拼接导致 s3h 签名错误，平台返回「规则校验不通过 / 数据完整性被破坏」(errcode 40016)。 */
export function signParam(hcmParam: string): string {
  return sm3(hcmParam + 'hcm' + 'cloud');
}

/** 解密响应 hcm_param，依据策略（默认 hb5） */
export function decryptParam(hcmParam: string, strategy = 'hb5'): any {
  if (strategy === 'hb5') {
    const raw = hcmParam.slice(3); // 去掉 3 位随机串
    const restored = raw.replace(/a/g, '5').replace(/!/g, 'a'); // a<->! , 5<->a 还原
    const json = CryptoJS.enc.Base64.parse(restored).toString(CryptoJS.enc.Utf8);
    return JSON.parse(json);
  }
  // ha 分支（对称性兜底）
  const ct = CryptoJS.enc.Base64.parse(hcmParam);
  const pt = CryptoJS.AES.decrypt(
    { ciphertext: ct } as any,
    KEY,
    { iv: IV, mode: CryptoJS.mode.CBC, padding: CryptoJS.pad.Pkcs7 }
  );
  const b64 = pt.toString(CryptoJS.enc.Utf8);
  return JSON.parse(utf8OfB64(b64));
}
