# 卡牌录入与效果实现流水线

数据源：[`data/catalog/`](../data/catalog/)。旧 [`data/cards.json`](../data/cards.json) 仅作历史占位，图鉴与对局以 catalog 为准。

## Catalog 字段规范

每张牌（及船/甲/虚境表项）建议包含：

| 字段 | 含义 |
|------|------|
| `id` | 稳定英文 id |
| `name` | 中文名 |
| `type` | `basic` / `trick` / `equipment` |
| `subtype` / `slot` / `tier`… | 类型相关 |
| `text` | 完整规则文案 |
| `phase` | 原路线图批次 A/B/C/D（参考） |
| `implemented` | `true` 时可合法打出并结算；`false` 时出牌提示依赖并可重铸 |
| `needs` | 未实现时依赖的公共机制标签（字符串数组） |

## 效果分派（引擎入口）

当前结算：`game/engine.py` 的 `_play_card` 分派到 [`game/trick_effects.py`](../game/trick_effects.py) 的 `HANDLERS`。

已实装锦囊（Wave 1–5）：智子、帷幕、面壁、广播、剧毒之水、四维空间、死线、归零、摇篮、冬眠、威慑、执剑、二向箔、香皂、古筝、星环城、Killer.5.2、大低谷、DX3906、黑域/黑暗森林/智子盲区/危机/三体、宇宙安全声明、咒语、思想钢印、回归运动、虚境桶及代表虚境效果。

公共能力：`tech_lock` / `cradle` / `hibernation` / `flip` / `fields[]` / `choice` prompt / `interrupt_trick` / `respond_toxic`。

## 待实现清单（装备补全等）

装备：`plan_part` / `black_hole` / `micro_universe` / 未完成船甲等仍可按 Phase B 穿插。

## 录入节奏

1. 从 docx 补全 `text` / 字段（可先 `implemented: false`）
2. 标 `needs[]`
3. 公共机制就绪后再改 `implemented: true` 并接到 `trick_effects.HANDLERS`
4. 加单牌冒烟到 `scripts/smoke_test.py`

