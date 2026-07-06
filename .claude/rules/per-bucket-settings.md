# 設定以 rate-limit 桶為單位（canonical = 桶名）

> **Status**: 設計決策（2026-07-06 定案）。實作追蹤於 [#23](https://github.com/PsychQuant/claude-hot-limit/issues/23)（roadmap，gated on 觀測校準資料）。

## 決策（CRITICAL）

任何**每-model 的設定 / 門檻**（`MAX` / `MIN_GAP` / `FANOUT_WIDE_MIN` 及未來新增者），**單位一律是 rate-limit「家族桶」，不是具體 model 版本**。

- **canonical key = 桶名**：`model_bucket()` 的輸出 `<family>-<major>` —— `opus-4` / `sonnet-5` / `sonnet-4` / `haiku-4`。
- **model 版本名（`OPUS4.8` / `SONNET5` …）只是 alias**，輸入時透過 `model_bucket()` 解析到桶再生效（「以名稱輸入、以桶生效」）。
- **禁止**把設定綁到具體版本（`opus-4-8` vs `opus-4-5` 各設一份）——它們共用同一個桶、共用計數，兩個門檻會自相矛盾。

## 為什麼（技術約束，不可繞過）

Anthropic 的 rate limit 是**家族級**的（官方文檔；本 plugin `model_bucket()` #6 已據此正規化）：

| model 版本 | 實際 rate-limit 桶 |
|---|---|
| `opus-4-5` / `4-6` / `4-8` | **共用 `opus-4`** |
| `sonnet-4-5` / `4-6` | 共用 `sonnet-4` |
| `sonnet-5` | 獨立 `sonnet-5` |
| `haiku-4-x` | `haiku-4` |

計數（`recent_heat` / launches ledger / `rate_state_heat`）本來就用 `model_bucket()` 收斂成桶。**設定的單位必須跟計數的單位對齊**——都是桶。若設定綁版本、計數綁桶，兩者 mismatch → 門檻套不準。

## 實作契約（給 #23 或任何 per-bucket 設定）

1. **輸入層**接受 model 名稱或桶名；一律先過 `model_bucket()` 正規化成桶再查設定。
2. **儲存/查找 key** 用桶名（`opus-4` 等）。傾向 per-bucket 檔案旗標：`<data_dir>/max-override.<bucket>`（如 `max-override.opus-4`），fallback → 全域 `max-override` → env → 預設（保留向後相容）。
3. **同家族多版本名都被設** → 不允許歧義：以桶為 key 天然去重（都解析到同一桶），最後寫入者生效即可，不需要「取最嚴格」之類的衝突規則。
4. `model_bucket()` 對未知/舊命名 → 回原值（不 over-merge）；per-bucket 設定沿用此 fail-open：查不到桶設定 → 用全域單一值。

## 反例（不要這樣做）

- ❌ `CLAUDE_HOT_LIMIT_MAX_OPUS_4_8` 與 `_MAX_OPUS_4_5` 各一份（同桶兩門檻）。
- ❌ 設定 key 用 exact model-id（`claude-opus-4-8`）—— minor 版本一升就對不上。
- ✅ 設定 key 用桶（`opus-4`）；UI 顯示可用好懂的版本名 alias。

## Refs

- `model_bucket()`：`plugins/claude-hot-limit/hooks/pacing-guard.py`（家族桶正規化，#6）。
- 單一門檻現狀已在 README 標明（commit `4731298`）。
- roadmap + 設計討論：#23。
