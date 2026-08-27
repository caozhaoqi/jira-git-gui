// -*- coding: utf-8 -*-
// HCM 元数据浏览器「加载数目不对」回归测试：复现 web/hcm-meta.html 的
// loadList（稳健分页 + 去重）与 metaKindOf（类型归类）逻辑，用 mock 网关验证。
//
// 覆盖四种网关行为：
//   A 正常分页（honor page/page_size, count=199）
//   B 忽略 page（永远返回前 20, count=199）—— 旧逻辑会重复拼接成 200 条（虚高）
//   C 忽略 page（一次全返 199, count=199）
//   D 无 count + 忽略 page（永远前 20）—— 旧逻辑会无限膨胀到 1000 条
//
// 运行：node tests/test_hcm_meta_count.js

function itemKey(it) {
  if (!it) return JSON.stringify(it);
  return it.id || it.key || it.name || JSON.stringify(it);
}
function metaKindOf(it) {
  const cand = [it && it.meta_key, it && it.biz_type, it && it.kind, it && it.type];
  for (const c of cand) if (c === "list" || c === "info" || c === "view") return c;
  const hay = [it && it.name, it && it.id, it && it.key].filter(Boolean).join(" ");
  const m = hay.match(/meta\.(list|info|view)/i);
  return m ? m[1].toLowerCase() : "other";
}

// 修复后的 loadList 核心（与 web/hcm-meta.html 对齐）
function newLoadList(fetchPage, ps) {
  let page = 1, acc = [], total = null;
  const seen = new Set();
  let partial = false;
  while (true) {
    const d = fetchPage(page, ps);
    const res = (d && d.result && typeof d.result === "object") ? d.result : d;
    const pageList = Array.isArray(res) ? res : (res.list || []);
    if (total == null) {
      const c = (res && res.count != null) ? res.count : (d && d.count != null ? d.count : null);
      if (c != null) total = c;
    }
    let added = 0;
    for (const it of pageList) {
      const k = itemKey(it);
      if (seen.has(k)) continue;
      seen.add(k); acc.push(it); added++;
    }
    if (pageList.length === 0 || added === 0 || (total != null && acc.length >= total) || page >= 50) {
      if (total != null && acc.length < total) partial = true;
      break;
    }
    page++;
  }
  return { acc, total: (total != null) ? total : acc.length, partial };
}

function makeGateway(mode) {
  const TOTAL = 199;
  const all = Array.from({ length: TOTAL }, (_, i) => ({
    id: "m" + i,
    name: "Employee.meta." + (i % 3 === 0 ? "list" : i % 3 === 1 ? "info" : "view") + "." + i + ".json",
    meta_key: i % 3 === 0 ? "list" : i % 3 === 1 ? "info" : "view"
  }));
  return (page, ps) => {
    if (mode === "A") {
      const start = (page - 1) * ps;
      return { result: { list: all.slice(start, start + ps), count: TOTAL } };
    }
    if (mode === "B") return { result: { list: all.slice(0, 20), count: TOTAL } };
    if (mode === "C") return { result: { list: all.slice(), count: TOTAL } };
    if (mode === "D") return { result: { list: all.slice(0, 20) } };
  };
}

let fail = 0;
function check(name, cond, extra) {
  console.log((cond ? "  OK  " : " FAIL ") + name + (extra ? "  " + extra : ""));
  if (!cond) fail++;
}

console.log("== A: 正常分页 ==");
let r = newLoadList(makeGateway("A"), 50);
check("加载=199 无重复", r.acc.length === 199, "len=" + r.acc.length);
check("系统总数=199", r.total === 199, "total=" + r.total);
check("非 partial", r.partial === false);

console.log("== B: 网关忽略 page（旧逻辑会虚高到 200）==");
r = newLoadList(makeGateway("B"), 2000);
check("去重后=20（不再虚高）", r.acc.length === 20, "len=" + r.acc.length);
check("系统总数仍=199", r.total === 199, "total=" + r.total);
check("partial=true（提示仅首页）", r.partial === true);

console.log("== C: 忽略 page 但一次全返 ==");
r = newLoadList(makeGateway("C"), 2000);
check("加载=199", r.acc.length === 199, "len=" + r.acc.length);
check("非 partial", r.partial === false);

console.log("== D: 无 count + 忽略 page（旧逻辑膨胀到 1000）==");
r = newLoadList(makeGateway("D"), 2000);
check("去重后=20（不再膨胀）", r.acc.length === 20, "len=" + r.acc.length);
check("无count时系统总数=已加载(20)", r.total === 20, "total=" + r.total);
check("非 partial（无count无法判断）", r.partial === false);

console.log("== 类型归类 metaKindOf ==");
const sample = [
  { id: "1", name: "Employee.meta.list.1.json", meta_key: "list" },
  { id: "2", name: "Employee.meta.info.2.json" },
  { id: "3", name: "Employee.meta.view.3.json", biz_type: "view" },
  { id: "4", name: "Employee" }
];
const dist = { list: 0, info: 0, view: 0, other: 0 };
sample.forEach(it => dist[metaKindOf(it)]++);
check("dist.list=1", dist.list === 1, JSON.stringify(dist));
check("dist.info=1", dist.info === 1, JSON.stringify(dist));
check("dist.view=1", dist.view === 1, JSON.stringify(dist));
check("dist.other=1", dist.other === 1, JSON.stringify(dist));

console.log(fail === 0 ? "\nALL HCM META COUNT CHECKS PASSED" : "\n" + fail + " CHECK(S) FAILED");
process.exit(fail === 0 ? 0 : 1);
