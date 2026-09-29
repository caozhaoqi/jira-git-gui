/**
 * 目录选择：Electron 走原生 showOpenDialog（经 preload 的 electronAPI.openDirectory），
 * 浏览器模式无原生对话框，兜底 window.prompt 手输路径。
 * 返回所选绝对路径；取消 / 关闭 / 空输入返回 null。
 */
export async function pickDirectory(title?: string): Promise<string | null> {
  const api = (
    window as unknown as {
      electronAPI?: {
        openDirectory?: (title?: string) => Promise<string | null>;
      };
    }
  ).electronAPI;
  if (api?.openDirectory) {
    try {
      const p = await api.openDirectory(title);
      return p && String(p).trim() ? String(p).trim() : null;
    } catch {
      return null;
    }
  }
  const p = window.prompt(title || '');
  return p && p.trim() ? p.trim() : null;
}
