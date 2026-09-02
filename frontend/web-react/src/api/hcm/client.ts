export class HcmApiError extends Error {
  status?: number;
  errcode?: number;
  constructor(message: string, status?: number, errcode?: number) {
    super(message);
    this.name = 'HcmApiError';
    this.status = status;
    this.errcode = errcode;
  }
}

export interface HcmEnv {
  key: string;
  name: string;
  server_url: string;
  source: string;
  has_preset_token: boolean;
}

/**
 * 获取可选服务器环境列表（后端从 cf_accounts / hcm_whitelist 汇总，脱敏仅暴露 name+url）。
 *
 * ⚠️ HCM 实际调用统一走后端直连 `/api/hcm/direct`（后端做加解密+签名），
 * 旧的「前端客户端加密」路径（crypto.ts 的 encryptParam/signParam/decryptParam + hcmCall）
 * 已弃用并删除，避免把 crypto-js / sm-crypto（约 284KB）打进前端包。
 */
export async function hcmEnvs(): Promise<HcmEnv[]> {
  const res = await fetch('/api/hcm/envs', { method: 'GET' });
  if (!res.ok) throw new HcmApiError(`获取环境列表失败: HTTP ${res.status}`, res.status);
  const data = await res.json().catch(() => ({ envs: [] }));
  return (data?.envs || []) as HcmEnv[];
}
