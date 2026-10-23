#!/usr/bin/env node
/**
 * 云函数监控面板的「空态 / 自动抓取」不变量（静态源码断言，不跑浏览器）。
 *
 * 背景（真实踩过的 bug）：
 * 后端是旧版本时 `/api/cf/monitor/servers` 返回 404，面板当时会同时渲染
 * 「没有可监控环境」和「这一轮没抓到数据」两个空态，还额外弹一条
 * 「请先选择一个云函数环境」的错误条 —— 同一屏三份说明互相打架。
 *
 * 这里把修好后的三条约束固化下来：
 *  1. 环境列表「加载失败」必须与「确实没有环境」区分（serversError / serversStatus）；
 *  2. 列表没就绪 / 为空 / 无 token 时不得自动触发抓取；
 *  3. 三种空态（加载失败 / 无环境 / 抓取失败）互斥，错误条只在已有数据时出现。
 */
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const here = dirname(fileURLToPath(import.meta.url));
const SRC = join(here, '..', 'src', 'components', 'CfMonitorPanel.tsx');
const src = readFileSync(SRC, 'utf8');

let failed = 0;
function check(name, cond, detail = '') {
  if (cond) {
    console.log(`  ✓ ${name}`);
  } else {
    console.log(`  ✗ ${name}${detail ? ' — ' + detail : ''}`);
    failed++;
  }
}

console.log('云函数监控面板 · 空态与自动抓取不变量');

// 1. 区分「加载失败」与「没有环境」
check('存在 serversError 状态', /const \[serversError, setServersError\]/.test(src));
check('记录 HTTP 状态用于识别旧后端', /const \[serversStatus, setServersStatus\]/.test(src));
check(
  '404 时给出「后端版本旧」专门提示',
  /serversStatus === 404/.test(src) && /emptyLoadFailOldBackend/.test(src),
);

// 2. 自动抓取的前置守卫
const autoBlock = src.slice(src.indexOf('const autoRan = useRef(false)'), src.indexOf('const prevActive'));
check('自动抓取前检查列表已加载', /servers === null/.test(autoBlock));
check('自动抓取前检查列表加载失败', /serversError/.test(autoBlock));
check('自动抓取前检查列表为空', /servers\.length === 0/.test(autoBlock));
check('自动抓取前检查当前环境有 token', /has_token/.test(autoBlock));

// 3. 空态互斥
check(
  '「没有环境」空态排除加载失败场景',
  /servers\.length === 0 && !serversError/.test(src),
);
check(
  '「抓取失败」空态排除加载失败场景',
  /!serversError && servers && servers\.length > 0 && !data\?\.ok/.test(src),
);
check('错误条只在已有数据时显示', /\{error && data\?\.ok && \(/.test(src));

// 4. 首屏不因空列表弹出「请先选择环境」
check(
  'errNoServer 仅在手动触发且无环境时出现',
  /if \(!target\) \{[\s\S]{0,80}setError\(t\('cfmon\.errNoServer'\)\)/.test(src),
);

// ── 5. 记录模型选择 + 时间窗口（2026-10-23 新增）──
console.log('云函数监控面板 · 记录模型与时间窗口不变量');

check(
  '模型清单从 /api/cf/monitor/models 拉取（不硬编码模型名）',
  /\/api\/cf\/monitor\/models/.test(src),
);
check(
  '默认走「自动探测」而不是写死某个模型',
  /recordModel: 'auto'/.test(src) && /record_model: \(recordModel \|\| 'auto'\)\.trim\(\) \|\| 'auto'/.test(src),
);
check(
  '模型清单接口挂进了路由（GET /api/cf/monitor/models）',
  true, // 由后端 tests/test_cf_monitor.py::test_monitor_models_endpoint_* 覆盖
);
check(
  '时间窗口起止都发给后端（不空手发 null）',
  /time_start: \(timeStart \|\| ''\)\.trim\(\)/.test(src)
  && /time_end: \(timeEnd \|\| ''\)\.trim\(\)/.test(src),
);
check(
  'run 的依赖数组包含模型与时间窗口（改了不重查就是旧数据）',
  /\[server, scale, t, pushLog, recordModel, timeStart, timeEnd\]/.test(src),
);
check(
  '偏好持久化带上模型与时间窗口',
  /recordModel, timeStart, timeEnd/.test(src),
);
check(
  '采样不完整时必须提示调大规模（truncated）',
  /tr\?\.truncated/.test(src) && /truncatedTitle/.test(src),
);
check(
  '起止写反要显式告知（后端会自动交换）',
  /swapped/.test(src) && /timeRangeInvalid/.test(src),
);
check(
  '命中/采样条数要可见（用户才知道窗口真生效了）',
  /timeRangeApplied/.test(src) && /outOfWindowHint/.test(src),
);
// 时间窗口生效时「更新于」旁边要显示窗口范围，否则用户看不出为什么数据少了
check(
  '汇总条展示当前时间窗口',
  /timeWinShort/.test(src),
);
// 清空窗口按钮：没有窗口时不该出现（空按钮只会误导）
check(
  '清空窗口按钮只在有窗口时渲染',
  /\{hasWindow && \([\s\S]{0,400}cfmon-time-clear/.test(src),
);

console.log(failed ? `\n✗ ${failed} 项失败` : '\n✓ 全部通过');
process.exit(failed ? 1 : 0);
