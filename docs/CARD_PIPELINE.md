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

当前结算集中在 `game/engine.py` 的 `_play_card` / `_equip_card`：

| id / 类型 | 处理 |
|-----------|------|
| `subtype=kill` | `_play_kill` → 闪响应 |
| `subtype=dodge` | 仅响应窗 |
| `peach` / heal | 自疗 |
| `visitor` | 科技 +1 |
| `ladder_plan` | 暴露视野至目标回合结束 |
| `red_coast` | 摸 2，每回合限一次 |
| equipment + slot | `_equip_card` + `game/equipment.apply_equip_bonuses` |
| 其它 `implemented=false` | 拒绝出牌，提示 `needs`，允许重铸 |

装备被动：`deep_sea` / `eco_bottle` / `lightspeed_2` 在 `_incoming_damage` / `_heal`；量子号摸牌阶段补给 3 阶杀。

## 待实现清单（按推荐批次）

### 批次 α — 简单锦囊
- wallfacer_plan, ball_lightning, curtain, sophon, cradle, toxic_water, broadcast…

### 批次 β — 装备补全（已部分落地）
- 已实现：blue_space, natural_selection, bronze_age, quantum, tang, nano/chip/stars, deep_sea, eco_bottle, lightspeed_2
- 待实现：gravity, star_ring, ultimate_law, curvature, solar_observe, plan_part, black_hole, micro_universe…

### 批次 γ — 选择 / 场地
- guzheng_plan, star_ring_city, soap, killer_52, dark_domain, trisolaris_field, crisis_field, sophon_blind, dark_forest_field, curse…

### 批次 δ — 高阶 / 虚境
- dual_vector, zeroing, deadline, realm_bucket 及 `data/catalog/realms.json` 各条目

## 录入节奏

1. 从 docx 补全 `text` / 字段（可先 `implemented: false`）
2. 标 `needs[]`
3. 公共机制就绪后再改 `implemented: true` 并接到分派表
4. 加单牌冒烟到 `scripts/smoke_test.py`
