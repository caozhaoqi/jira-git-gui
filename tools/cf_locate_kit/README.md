# 云函数「错误定位」改造包（不改 hcm-core 框架源码）

约束：hcm-core 框架（`core/service/*`、`errors.py`、`handlers.py`）与前端都**不动**，
只改「你自己的云函数脚本」+ 一条环境配置。

## 为什么必须这么做（源码事实）

| 事实 | 位置 |
|------|------|
| 每次报错生成毫秒级 `error_code`（服务端日志索引） | `core/service/handlers.py:477` |
| 默认 `hide_error_msg=True` 会**重建** AppException，只保留 `err_msg` + `description`，`add_info` 挂的东西全丢 | `handlers.py:541-554` |
| `AppException.err_obj` 支持 `add_info(key,value)` 任意挂载 | `errors.py:38-43` |
| 云函数即 `BasePrivateApiService` 子类，入口 `execute(self, **kwargs)` | `core/service/__init__.py:1767` |

结论：默认配置下**只有 err_msg / description 能透传到前端**，所以定位信息必须编码进文本。
（若把 `hcm_cloud.hide_error_msg` 配成 `False`，`error_info` 也能透传，二者兼容。）

## 约定格式（与 jira-git-gui 定位面板严格对齐）

```
[定位] model=<模型> id=<对象ID> field=<字段> value=<值> stage=<阶段> || <人话原因>
```

- 面板按空格分词解析，因此 `value` **必须不含空格**（snippet 的 `_tok()` 已保证）。
- `stage` 建议取值：`field_read` / `field_validate` / `biz_calc` / `db_write`。

## 文件说明

| 文件 | 用途 |
|------|------|
| `locate_snippet.py` | 可粘贴进任意云函数的自包含助手（`safe_get` / `assert_field` / `locate` / `locate_guard`），零 import |
| `cf_error_locator.py` | 诊断云函数，注册为 `private.cf_error_locator`，按 model+id+field 反查对象当前数据 |
| `../cf_locate_retrofit.py` | 开发期 CLI：scan（字段扫描+元数据对照）/ audit（只读规范审计）/ diff（预览）/ apply（写盘+备份） |

## 三条使用路径（从省事到彻底）

**1. 手动改（最可控）**
把 `locate_snippet.py` 里 `CF_LOCATE_BEGIN/END` 之间的代码贴到云函数顶部，
然后把关键取值 `emp.get('id_card')` → `safe_get(emp, 'id_card', '身份证号')`。

**2. 命令行批量改造（推荐）**
```bash
# 先看会改什么（只读，不写盘）
python3 tools/cf_locate_retrofit.py diff --dir <云函数目录>

# 确认后应用（原文件自动备份为 .bak）
python3 tools/cf_locate_retrofit.py apply --dir <云函数目录>
```
默认行为：注入 snippet + 把 `x = obj.get('field')` 换成 `safe_get` + 给 `execute` 套兜底 try。
**务必 diff 评审 + 回归测试后再部署。**

**3. 只扫描风险（不改代码）**
```bash
# 字段清单
python3 tools/cf_locate_retrofit.py scan --dir <云函数目录>

# 与元数据对照，标出元数据里不存在/疑似已删除的字段
python3 tools/cf_locate_retrofit.py scan --dir <云函数目录> --meta meta.json
```
`meta.json` 取 `hcm.model.meta` 返回的 `fields`（数组，或含 `fields` 的对象）。

## 配套：让报错更好查（可选但强烈建议）

把环境配置 `hcm_cloud.hide_error_msg` 设为 `False`（或开启 operation_mode），
`error_info` 就能透传到前端，定位面板可展示结构化字段而不仅是文本。

## 验证状态

- 对 `hcm-core/cloud_functions` 全部 **88 个**云函数跑 `apply`：改造后 **88/88 仍可被 ast 解析**（PARSE_OK=88 FAIL=0）。
- 改造内容为临时目录副本验证，**未改动** `hcm-cloud-vue` 源文件。
