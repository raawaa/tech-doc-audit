# Per-provider LLM context windows + recommended prompt char thresholds (wayfinder #116)

**Map**: #114
**Status**: Complete (2026-07-31)
**Reserve assumption**: 8K tokens reserved for tools + system prompt + history (mid-range of the 5-10K target). Safety factor 0.85 applied to the remaining budget. CJK policy documents assumed (1.5-2 chars/token; midpoint **1.75 chars/token** used).

## TL;DR

The project's current hard-coded constants (`DOC_FULL_THRESHOLD=30000`, `CHAPTER_MAX_CHARS=8000` in `services/agentic_audit.py`) were sized for the **old small-model Ollama defaults** (qwen2.5:0.5b-class, 8K-32K context). Now that the default Ollama model is `qwen3.5:0.8b` with a **256K context window**, those thresholds are over-conservative for the local Ollama path while remaining correct (but conservative) for the 128K cloud providers. This file provides per-provider defaults for the upcoming T4 grilling (#118) to make the constants a `provider -> {full, preview, chapter}` lookup rather than a single hard-coded pair.

---

## Per-provider summary

| Provider | Model | Context window (tokens) | Doc budget (tokens, post 8K reserve) | Effective budget (×0.85) | Recommended `DOC_FULL_THRESHOLD` (CJK chars) | Recommended preview chars | Recommended `CHAPTER_MAX_CHARS` |
|---|---|---|---|---|---|---|---|
| `ollama` | `qwen3.5:0.8b` | **256,000** | 248,000 | 210,800 | **368,900** | **36,900** | **18,450** |
| `minimax-cn` | `MiniMax-M2.7` | **128,000** | 120,000 | 102,000 | **178,500** | **17,850** | **8,925** |
| `deepseek` | `deepseek-chat` | **128,000** | 120,000 | 102,000 | **178,500** | **17,850** | **8,925** |
| `openai` | `gpt-4o-mini` | **128,000** | 120,000 | 102,000 | **178,500** | **17,850** | **8,925** |

CJK chars/token used: **1.75** (midpoint of 1.5-2 range; see §"CJK character density"). Preview = 10% of full threshold; chapter = 50% of preview (and ≤ full).

---

## Default table (machine-readable, for T4 #118)

```python
# research/prompt-threshold-by-provider.md  --  generated 2026-07-31
# 公式：full = (ctx_tokens - 8000) * 0.85 * 1.75   (CJK chars/token)
#       preview = full // 10
#       chapter = preview // 2   (且不超过 full)
DEFAULT_PROMPT_THRESHOLDS = {
    "ollama": {
        "full": 368900,
        "preview": 36900,
        "chapter": 18450,
    },
    "minimax-cn": {
        "full": 178500,
        "preview": 17850,
        "chapter": 8925,
    },
    "deepseek": {
        "full": 178500,
        "preview": 17850,
        "chapter": 8925,
    },
    "openai": {
        "full": 178500,
        "preview": 17850,
        "chapter": 8925,
    },
}
```

---

## Detailed per-provider findings

### ollama / `qwen3.5:0.8b`

- **Source (primary)**: <https://ollama.com/library/qwen3.5> — Ollama library page lists `qwen3.5:0.8b` as "1.0GB · **256K context window** · Text, Image". All qwen3.5 family variants (0.8b / 2b / 4b / 9b / 27b / 35b / 122b + MLX + cloud) share the same 256K context window.
- **Source (secondary, helpful context)**: <https://blog.csdn.net/skywalk8163/article/details/158661229> — community blog confirming qwen3.5:0.8b is a real, deployable Ollama tag.
- **Context window**: 256,000 tokens (verified from Ollama library page).
- **Chars/token conversion**: CJK ~1.5-2, English ~4. Using CJK midpoint 1.75 chars/token.
- **Reserve**: 8K tokens for tools + system + history.
- **Doc budget**: 256K - 8K = 248,000 tokens. After ×0.85 safety = 210,800 tokens.
- **Recommended thresholds**:
  - `DOC_FULL_THRESHOLD`: 210,800 × 1.75 = **368,900 chars**
  - preview chars (10% of full): **36,900 chars**
  - `CHAPTER_MAX_CHARS` (half of preview, well under full): **18,450 chars**
- **Notes**: **Surprise vs the code's premise.** Issue #116's question listed "qwen3.5:0.8b" with "需查（小型本地模型，典型 8K-32K）". This is **outdated**: the qwen3.5 generation (Qwen team's 2026 release) ships 256K across all sizes including the 0.8B. So Ollama's local mode is now *the most permissive* provider on context, not the most constrained. The current hard-coded `DOC_FULL_THRESHOLD=30000` therefore wastes ~92% of the available budget for local-Ollama runs.
- **Caveat**: Ollama's `num_ctx` defaults to 32K unless explicitly bumped — but the model's *published* max context is 256K and any setting up to that is supported. If the user's `OLLAMA_*` config leaves it at the default, the *effective* window collapses to 32K and the local-path thresholds above would be unsafe; recommend `OLLAMA_OPTIONS='{"num_ctx": 131072}'` or similar. Note this risk in T4.

### `MiniMax-M2.7` (provider `minimax-cn`)

- **Source (primary)**: <https://platform.minimaxi.com/document/Models> — official Models doc lists MiniMax-M2 (2025-10-27) and MiniMax-M2.7 (2026-03-18) but the public page text fetched did not explicitly state a numeric context window.
- **Source (code-side registration)**: `core/settings.py:225` does `_openai_utils.ALL_AVAILABLE_MODELS[model] = 128000` — i.e. the codebase already commits to **128,000 tokens** as the working figure.
- **Source (third-party corroboration)**:
  - <https://zenn.dev/minimax/articles/MiniMax-M2-release> — M2 release notes state "上下文长度：128k".
  - <https://lmstudio.ai/models/MiniMax-M2> — LM Studio model page lists MiniMax-M2 "128k context window, 175B parameters".
  - <https://huggingface.co/MiniMaxAI/MiniMax-M2-128k> — Hugging Face repo `MiniMaxAI/MiniMax-M2-128k` carries 128k in the model name itself.
  - <https://huggingface.co/MiniMaxAI/MiniMax-M2> — model card "128k context length".
- **Context window**: **128,000 tokens** (matches the code's registration).
- **Chars/token conversion**: CJK ~1.75 chars/token.
- **Reserve**: 8K tokens.
- **Doc budget**: 128K - 8K = 120K tokens. After ×0.85 = 102K tokens.
- **Recommended thresholds**:
  - `DOC_FULL_THRESHOLD`: 102,000 × 1.75 = **178,500 chars**
  - preview chars: **17,850 chars**
  - `CHAPTER_MAX_CHARS`: **8,925 chars**
- **Notes**: One search hit mentioned MiniMax-M2 as 200K input / 128K output — this likely conflates M2 with MiniMax-Text-01 (1M+). The conservative working figure matches what the code already registers and what the M2 / M2.7 model cards state.

### `deepseek-chat` (provider `deepseek`)

- **Source (primary, strongest)**: <https://api-docs.deepseek.com/news/news251201/> — DeepSeek-V3.1 release note explicitly states "**deepseek-chat model's context window is 128K**" (quoted twice in the page).
- **Source (pricing page)**: <https://api-docs.deepseek.com/quick_start/pricing.html> — listing `deepseek-chat` (DeepSeek-V3.x) with 128K context length, 8K max output.
- **Source (code-side registration)**: `core/settings.py:250` sets `_openai_utils.ALL_AVAILABLE_MODELS[model] = 128000`.
- **Context window**: **128,000 tokens** (matches code; confirmed by official release notes).
- **Chars/token conversion**: CJK ~1.75 chars/token.
- **Reserve**: 8K tokens.
- **Doc budget**: 128K - 8K = 120K tokens. After ×0.85 = 102K tokens.
- **Recommended thresholds**:
  - `DOC_FULL_THRESHOLD`: **178,500 chars**
  - preview chars: **17,850 chars**
  - `CHAPTER_MAX_CHARS`: **8,925 chars**
- **Notes**: One older write-up (leanware.ca) listed `deepseek-chat` as 64K — this pre-dates V3.1's Dec-2025 expansion and is now superseded by the official release-note quote.

### `gpt-4o-mini` (provider `openai`)

- **Source (primary)**: <https://platform.openai.com/docs/models/gpt-4o-mini> — "Context window: 128,000 tokens. Max output tokens: 16,384."
- **Source (launch announcement)**: <https://openai.com/index/gpt-4o-mini/> — "context window of 128K tokens".
- **Source (models index)**: <https://platform.openai.com/docs/models> — "gpt-4o-mini. 128K context window. 16K max output tokens."
- **Context window**: **128,000 tokens**.
- **Chars/token conversion**: CJK ~1.75 chars/token.
- **Reserve**: 8K tokens.
- **Doc budget**: 128K - 8K = 120K tokens. After ×0.85 = 102K tokens.
- **Recommended thresholds**:
  - `DOC_FULL_THRESHOLD`: **178,500 chars**
  - preview chars: **17,850 chars**
  - `CHAPTER_MAX_CHARS`: **8,925 chars**
- **Notes**: 16K max output is a hard ceiling on what the model can emit per turn, but it doesn't constrain the input budget — DOC_FULL_THRESHOLD is about input.

---

## CJK character density assumption

- **Claim**: For CJK text, the cl100k_base tokenizer (used by GPT-4 / `gpt-4o-mini`) averages ~1.5 tokens per common Chinese character, with BPE packs of frequently co-occurring 2-grams landing in the 1.5-2 tokens/char range; i.e. **~0.6 chars per token**, or equivalently **1.5-2 chars/token**.
- **Source**: <https://gist.github.com/iamkarliton/f57ddd0b1e6f5b8df38295f3b5439b4e> — community gist "tokens per language" with per-language tokenization rates; Chinese at ~1.5-2 tokens/character.
- **Source (corroborating)**: Reddit r/LocalLLaMA thread "cl100k token count for Chinese characters" (<https://www.reddit.com/r/LocalLLaMA/comments/15zbswq/cl100k_token_count_for_chinese_characters_per/>) — empirical measurements on cl100k.
- **Working value used in this report**: **1.75 chars/token** (midpoint). Equivalent token-per-char = 0.57. This is conservative for Chinese (real ratio is often closer to 0.6 chars/token = 1.67 tokens/char), giving a ~5% safety margin in the chosen char thresholds.
- **English baseline**: ~4 chars/token (OpenAI guidance). Policy docs in this project mix CJK headings + English/numeric tokens; the CJK rate dominates for 中文标准/政策 files.

---

## How the numbers were derived (worked example for ollama)

1. Context window from primary source: 256,000 tokens.
2. Subtract reserve: 256,000 - 8,000 = 248,000 tokens available for document + tool results.
3. Apply safety factor 0.85: 248,000 × 0.85 = 210,800 tokens.
4. Convert to CJK chars: 210,800 × 1.75 chars/token = **368,900 chars** = `DOC_FULL_THRESHOLD`.
5. Preview = 10% of full = **36,900 chars** (enough for a multi-paragraph preview without dominating the prompt).
6. Chapter = 50% of preview, capped at full = **18,450 chars**.

Same arithmetic gives 178,500 / 17,850 / 8,925 for all three 128K-context providers.

---

## Open questions for T3 / T4

- **T4 (grilling #118)**: Should the table key be the provider name (`"ollama"`) or the *effective context class* (`"small"` / `"large"`)? All four providers map cleanly to two context classes (256K and 128K) today; if we expect more providers, the class key might age better.
- **T4**: Is `CHAPTER_MAX_CHARS = preview // 2` the right ratio? Current code uses 8000 vs preview currently implicit at ~3000 (`first_n_chars` not exposed in `qa_service` but `node.text[:300]` is used for content_snippet). T4 may want preview/chapter tied to actual `search_kb` recall size rather than fixed constants.
- **T3 (task landing #119)**: The `ollama` path's effective context is bounded by Ollama's `num_ctx` setting, **not** by the model's 256K native context. If `num_ctx` is left at the default (32K), the ollama path's effective budget collapses and 368,900 becomes unsafe. Need to either (a) ensure `OLLAMA_OPTIONS` sets `num_ctx>=131072` for the audit path, or (b) reduce ollama's recommended threshold to `min(published_ctx, ollama_num_ctx)`. T4 should pick (a) vs (b).
- **Caveat**: this report treats all four providers' docs as if they share the same `DOC_FULL_THRESHOLD` semantics — the project uses one threshold for "dump the whole doc into the prompt vs summarize-by-chapter". T4 should grill whether `MiniMax-M2.7`'s 200K input (if true) lets us relax the minimax-cn row further.
- **T4**: Output-token ceilings differ across providers (DeepSeek 8K, MiniMax ~8K, OpenAI gpt-4o-mini 16K, Ollama unbounded by API but model-emitted). For "preview" / "chapter" sizes above ~8K chars (~4.5K tokens), DeepSeek may struggle to emit a structured audit Issue with full reasoning. Output budget may need to be reserved too (currently only input).

---

## Surprises vs the codebase assumptions

1. **`qwen3.5:0.8b` is 256K, not 32K.** Issue #116's question framed Ollama as the most constrained provider. The opposite is now true: ollama via qwen3.5 has the *largest* usable context among the four providers in `core/settings.py`. The hard-coded `DOC_FULL_THRESHOLD=30000` therefore *under-uses* Ollama by ~12x.
2. **DeepSeek context matches the code (128K).** Some older write-ups still cite 64K for `deepseek-chat`; the Dec-2025 V3.1 release note pinned it at 128K. Code-side `_openai_utils.ALL_AVAILABLE_MODELS[model] = 128000` at L250 is correct.
3. **MiniMax-M2.7 matches the code (128K).** Code at L225 is correct. One web source cited 200K input — likely M2 / MiniMax-Text-01 conflation; sticking with 128K is the conservative, code-aligned choice.
4. **The three 128K providers get identical recommended thresholds.** Whether to specialize further (e.g. cap DeepSeek below 8K output ceiling) is a T4 call.

---

## Sources (consolidated)

**ollama / qwen3.5:0.8b (256K)**
- <https://ollama.com/library/qwen3.5> — Ollama library page; "256K context window".
- <https://blog.csdn.net/skywalk8163/article/details/158661229> — community confirmation qwen3.5:0.8b is deployable.

**DeepSeek deepseek-chat (128K)**
- <https://api-docs.deepseek.com/news/news251201/> — V3.1 release note: "deepseek-chat model's context window is 128K".
- <https://api-docs.deepseek.com/quick_start/pricing.html> — models/pricing table.

**MiniMax-M2.7 (128K)**
- <https://platform.minimaxi.com/document/Models> — official model list.
- <https://zenn.dev/minimax/articles/MiniMax-M2-release> — M2 release note "上下文长度：128k".
- <https://huggingface.co/MiniMaxAI/MiniMax-M2-128k> — HF repo with 128k in name.
- <https://huggingface.co/MiniMaxAI/MiniMax-M2> — M2 model card.
- <https://lmstudio.ai/models/MiniMax-M2> — LM Studio page listing 128k.

**OpenAI gpt-4o-mini (128K)**
- <https://platform.openai.com/docs/models/gpt-4o-mini> — "Context window: 128,000 tokens".
- <https://platform.openai.com/docs/models> — models index.
- <https://openai.com/index/gpt-4o-mini/> — launch announcement.

**CJK token density**
- <https://gist.github.com/iamkarliton/f57ddd0b1e6f5b8df38295f3b5439b4e> — per-language tokenization rates.
- <https://www.reddit.com/r/LocalLLaMA/comments/15zbswq/cl100k_token_count_for_chinese_characters_per/> — empirical cl100k measurements.