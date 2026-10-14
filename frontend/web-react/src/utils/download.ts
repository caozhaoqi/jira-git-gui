/** 通用「把内容保存到本地文件」工具（浏览器与 Electron 均可用）。
 *
 * Electron 下 `a[download]` 会落到系统下载目录，因此导出无需经过后端写盘。
 */

/** 触发浏览器保存 Blob 到本地。 */
export function saveBlob(blob: Blob, filename: string) {
  const a = document.createElement('a');
  const url = URL.createObjectURL(blob);
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
  // 立刻 revoke 在部分浏览器会打断下载，延后释放
  setTimeout(() => URL.revokeObjectURL(url), 10_000);
}

/** 保存文本文件（按 mime 类型构造 Blob）。 */
export function saveTextFile(text: string, filename: string, mime = 'text/plain;charset=utf-8') {
  saveBlob(new Blob([text], { type: mime }), filename);
}
