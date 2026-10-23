// -*- coding: utf-8 -*-
/** CF 日志结果表的「本地分页 / 匹配序号」纯计算（与 React 组件解耦，便于离线回归）。

为什么值得单独拆一层：实时刷新（SSE 合并）会把 ``result.rows`` 变成**客户端累积集**，
服务端翻页（``page_index``）一发查询就把累积集冲掉，所以这种情况必须走本地分页；
而「已加载全部日志」徽标仍要按服务端 ``total`` 判断。两件事共用一个 ``isFull`` 会互相污染：

- 把 ``isFull`` 改成 true → 流开着时平台还有几百条没取回，却对用户谎报「全量已加载」；
- 只判 ``isFull`` → 实时刷新期间恒为 false（``total`` 是平台总行数而 ``rows`` 只有一页），
  累积行堆到 5000 条上限全量渲染，既拖慢渲染、新行混在长列表里也看不出来。

故拆成 ``localPaged``（分页门槛）与 ``isFull``（徽标口径）两个判断，切片数学集中在本文件。

另一个坑：行号 / React key / 展开态 / 匹配高亮定位**全都得以 ``pageStart`` 为基准**。
拿「页内下标」当 key，一翻到第 2 页就和第 1 页撞 key；匹配序号从 0 起算，
翻页后「第 N 处匹配」的高亮会整体错位到别的行上。
*/
import { logRowContent, logRowTime, logRowType } from './logFields';
import type { CfLogsRow } from '../api/types';

/** 定位 text 中所有 q 的出现区间（与高亮渲染同口径）。 */
export function findMatches(text: string, q: string, caseSensitive: boolean): Array<[number, number]> {
  if (!q) return [];
  const hay = caseSensitive ? text : text.toLowerCase();
  const needle = caseSensitive ? q : q.toLowerCase();
  const out: Array<[number, number]> = [];
  let idx = hay.indexOf(needle);
  while (idx !== -1) {
    out.push([idx, idx + needle.length]);
    idx = hay.indexOf(needle, idx + needle.length);
  }
  return out;
}

export interface LocalPageSlice<T> {
  /** 当前页要显示的行（未分页时就是全量 filtered）。 */
  display: T[];
  /** 当前页首行在 filtered 里的下标（未分页时为 0）。 */
  pageStart: number;
  totalPages: number;
  /** 收敛到合法范围后的页码。 */
  localPage: number;
}

/**
 * 本地分页切片。
 *
 * @param filtered   排序 + 过滤后的全量行集
 * @param pageSize   每页条数（result.page_size）
 * @param localPaged 是否走本地分页（``isFull || merged``）；false 时原样返回全量
 * @param localPage  用户当前停在本地的第几页（越界时收敛到末页）
 */
export function sliceLocalPage<T>(
  filtered: T[],
  pageSize: number,
  localPaged: boolean,
  localPage: number,
): LocalPageSlice<T> {
  const size = Math.max(1, Math.trunc(pageSize) || 1);
  const want = Math.max(1, Math.trunc(localPage) || 1);
  if (!localPaged || filtered.length <= size) {
    return { display: filtered, pageStart: 0, totalPages: 1, localPage: want };
  }
  const totalPages = Math.ceil(filtered.length / size);
  const page = want > totalPages ? totalPages : want;
  const pageStart = (page - 1) * size;
  return {
    display: filtered.slice(pageStart, pageStart + size),
    pageStart,
    totalPages,
    localPage: page,
  };
}

/** 一行的匹配处数（内容 + 时间 + 类型；与 view memo 的计数口径一致）。 */
export function rowMatchCount(
  row: CfLogsRow,
  q: string,
  caseSensitive: boolean,
  fallbackType: string,
): number {
  if (!q) return 0;
  return (
    findMatches(logRowContent(row), q, caseSensitive).length +
    findMatches(logRowTime(row), q, caseSensitive).length +
    findMatches(logRowType(row, fallbackType), q, caseSensitive).length
  );
}

/**
 * ``filtered[0, startIdx)`` 的累计匹配数 —— ``rowOrd`` 的起点。
 * 从 0 起算会让翻页后的「第 N 处匹配」高亮整体错位，必须按页首行偏移起算。
 */
export function countMatchesBefore(
  rows: CfLogsRow[],
  startIdx: number,
  q: string,
  caseSensitive: boolean,
  fallbackType: string,
): number {
  if (!q || startIdx <= 0) return 0;
  let n = 0;
  const end = Math.min(startIdx, rows.length);
  for (let i = 0; i < end; i++) {
    n += rowMatchCount(rows[i], q, caseSensitive, fallbackType);
  }
  return n;
}
