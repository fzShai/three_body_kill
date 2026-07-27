# 卡牌录入与效果实现流水线

数据源：[`data/catalog/`](../data/catalog/)。旧 [`data/cards.json`](../data/cards.json) 仅作历史占位，图鉴与对局以 catalog 为准。

**规则优先级：**

1. 用户定稿覆盖（帷幕 / 冬眠 / 香皂 / Killer.5.2 / 计划的一部分·黑洞·小宇宙·死神永生改为正面状态）
2. 其余以根目录 `三体杀牌库兼指引性文档.docx`「牌库详解」为准
3. 原文缺失（万象、回光）仅占位，不强行编效果

## Catalog 字段规范

每张牌（及船/甲/虚境表项）建议包含：

| 字段 | 含义 |
|------|------|
| `id` | 稳定英文 id |
| `name` | 中文名 |
| `type` | `basic` / `trick` / `equipment` |
| `subtype` / `slot` / `tier`… | 类型相关 |
| `text` | 完整规则文案（须对齐 docx 或用户定稿，禁止擅自简化语义） |
| `phase` | 原路线图批次 A/B/C/D（参考） |
| `implemented` | `true` 时可合法打出并结算；`false` 时出牌提示依赖并可重铸 |
| `needs` | 未实现时依赖的公共机制标签（字符串数组） |

## 效果分派（引擎入口）

当前结算：`game/engine.py` 的 `_play_card` 分派到 [`game/trick_effects.py`](../game/trick_effects.py) 的 `HANDLERS`。

用户定稿例外摘要：

- **帷幕**：清负面后摸 1
- **冬眠**：可重铸；上限 -2；状态「不可被他人牌选中」至自己下回合开始
- **香皂**：循环 x 次询问谁 +1 血（x=科等）
- **Killer.5.2**：指定猎物；至多 3 名他人各摸 1，摸到杀则对猎物结算；自己摸 1
- **计划的一部分 / 黑洞 / 小宇宙 / 死神永生**：锦囊赋予正面状态（非甲）

公共能力：`tech_lock` / `cradle` / `hibernation` / `flip` / `fields[]` / `choice` / `soap_heal` / `interrupt_trick` / `respond_toxic`。

## 录入节奏

1. 从 docx 补全 `text` / 字段（可先 `implemented: false`）
2. 标 `needs[]`
3. 公共机制就绪后再改 `implemented: true` 并接到 `trick_effects.HANDLERS`
4. 加单牌冒烟到 `scripts/smoke_test.py`
5. 同步 `static/data/cards.json`（可由 catalog 导出）
