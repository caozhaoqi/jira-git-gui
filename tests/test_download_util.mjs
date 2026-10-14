// 前端「保存文件到本地」工具（utils/download.ts）无头测试。
//
// 为什么需要它：台帐导出的最后一环是「把生成的 CSV/JSON 落盘」。这一步依赖
// document/URL 这些浏览器 API，在 Node 里跑不了真下载，所以用桩对象验证
// 「真的创建了带正确文件名与 MIME 的 <a download> 并触发点击」——
// 这是导出功能唯一无法靠纯函数测试覆盖的部分。
//
// 运行：node tests/test_download_util.mjs
import { saveBlob, saveTextFile } from '../frontend/web-react/src/utils/download.ts';

let failed = 0;
function check(name, condition) {
  if (condition) {
    console.log(`  ✓ ${name}`);
  } else {
    failed += 1;
    console.log(`  ✗ ${name}`);
  }
}

// ---- DOM 桩 ----------------------------------------------------------------- //
const created = [];
const revoked = [];
let clicked = 0;
let appended = 0;
let removed = 0;

const anchor = {
  href: '',
  download: '',
  click() {
    clicked += 1;
  },
  remove() {
    removed += 1;
  },
};

globalThis.document = {
  createElement(tag) {
    created.push(tag);
    return anchor;
  },
  body: {
    appendChild(el) {
      appended += 1;
      return el;
    },
  },
};

let blobParts = null;
let blobType = null;
globalThis.Blob = class {
  constructor(parts, opts) {
    blobParts = parts;
    blobType = opts?.type;
  }
};

globalThis.URL = {
  createObjectURL() {
    return 'blob:test-url';
  },
  revokeObjectURL(url) {
    revoked.push(url);
  },
};

// ---- 断言 ------------------------------------------------------------------- //
console.log('saveTextFile');
saveTextFile('a,b\r\n1,2', 'U_SyncRawGdyy_20260923_134705.csv', 'text/csv;charset=utf-8');
check('creates an anchor element', created.length === 1 && created[0] === 'a');
check('wires the blob url', anchor.href === 'blob:test-url');
check('sets the download filename', anchor.download === 'U_SyncRawGdyy_20260923_134705.csv');
check('appends then removes the anchor', appended === 1 && removed === 1);
check('clicks the anchor', clicked === 1);
check('passes the text as the blob body', blobParts?.length === 1 && blobParts[0] === 'a,b\r\n1,2');
check('passes the mime type through', blobType === 'text/csv;charset=utf-8');
check('does not revoke the url synchronously (would abort the download)', revoked.length === 0);

console.log('saveBlob');
saveTextFile('{"ok":true}', 'x.json', 'application/json;charset=utf-8');
check('second save reuses the same pipeline', created.length === 2 && clicked === 2);
check('json mime is honoured', blobType === 'application/json;charset=utf-8');
saveBlob(new Blob(['x'], { type: 'application/octet-stream' }), 'raw.bin');
check('saveBlob accepts a prebuilt Blob', anchor.download === 'raw.bin' && clicked === 3);

console.log(failed === 0 ? '\n全部通过' : `\n${failed} 条失败`);
// revoke 走的是 10s 延时定时器，这里直接退出避免进程被挂住
process.exit(failed === 0 ? 0 : 1);
