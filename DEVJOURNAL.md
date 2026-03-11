# Development Journal

## 2026-03-09: OpenAI Codex Caching Improvements

### Problem
The Codex Responses API (`chatgpt.com/backend-api/codex`) was getting poor cache hit rates.
JSONL token log showed alternating 0%/high% patterns within tool loops, and nearly 0% on
the first API call of each new user message.

### Root Cause Analysis
1. **`store: False`** was hardcoded — each API call was stateless with no server-side response
   storage. Prefix caching relied on a short TTL (~5 min) window, frequently expired between
   user messages.
2. **No `previous_response_id` chaining** — every call sent the full conversation history from
   scratch. The server had no way to reuse its KV cache from the previous turn.
3. The `store: False` enforcement was added in commit `74c662b` ("Harden Codex auth refresh")
   as a defensive assertion, not a documented API requirement.

### Changes Made

#### `run_agent.py`
- **`store: True`** in `_build_api_kwargs()` — responses are now stored server-side so the
  KV cache persists across calls.
- **`previous_response_id` chaining** — after each successful API call, `response.id` is saved.
  The next call in the tool loop sends only the delta input (new tool results) plus
  `previous_response_id`, letting the server reconstruct context from its stored response
  instead of reprocessing the full history.
- **Delta input calculation** — tracks `_codex_payload_len` (cumulative count of input items
  + output items across calls). On subsequent calls, only `all_input_items[payload_len:]` is
  sent. Falls back to full input if the chain breaks.
- **Cross-message persistence** — after each successful call, `response.id` is saved to the
  session DB via `update_codex_response_id()`. On session resume (gateway creates fresh
  AIAgent per message), the stored ID is loaded and `_codex_payload_len` is recomputed from
  `conversation_history`, enabling cache hits on `call=1` of every message.
- **Removed `store: False` enforcement** in `_preflight_codex_api_kwargs()` — the ValueError
  assertion was removed, and `previous_response_id` was added to `allowed_keys`.

#### `hermes_state.py`
- **Schema v5 migration** — adds `codex_last_response_id TEXT` column to `sessions` table.
- **`update_codex_response_id(session_id, response_id)`** method — persists the response ID.

### Expected Impact
- **Within tool loops**: calls 2+ should show near-100% cache hits (server reuses KV cache
  from previous response instead of reprocessing).
- **Across messages**: call=1 should show high cache hits (server chains from the stored
  response of the previous message's last call).
- **Token savings**: cached tokens are ~10x cheaper than full input tokens on OpenAI.

### Risks
- The Codex endpoint (`chatgpt.com/backend-api/codex`) is an internal/unofficial API. Behavior
  of `store: true` and `previous_response_id` may differ from the public `api.openai.com`.
- If the endpoint rejects `store: true`, it will return a 400 error — the agent will log it
  and the error handling will surface it.
- `previous_response_id` chain breaks if a stored response is evicted server-side (e.g., after
  30 days or on server restart). The code falls back to full input in this case.

### Result: BLOCKED by Codex endpoint
The Codex endpoint returns `400 - {'detail': 'Store must be set to false'}`. The internal
API explicitly rejects `store: true`, which means `previous_response_id` chaining is also
unavailable (requires stored responses to reference).

**All caching changes were reverted.** The original `store: False` enforcement in the preflight
function was correct — it reflects a real API constraint, not just a defensive assertion.

The Codex endpoint relies solely on automatic prefix caching with a short TTL. There is no
way to improve cache hit rates beyond what prefix matching provides. The alternating 0%/high%
pattern within tool loops is inherent to this architecture.

---

## 2026-03-09: Dashboard Timezone Display (display-only)

### Approach
All timestamp formatting is handled in the dashboard's JavaScript (`index.html`), not in the
backend Python code. The backend stores naive ISO timestamps (server-local time). The dashboard
JS functions `fmtTime()`, `fmtIsoTime()`, and `fmtIsoTimeShort()` use `toLocaleString()` /
`toLocaleTimeString()` which render in the browser's local timezone.

### Panels fixed
- **Usage**: raw `esc(e.ts)` → `fmtIsoTime(e.ts)` (full date+time)
- **Activity**: raw `.slice(11,19)` → `fmtIsoTimeShort(e.ts)` (time-only, browser-local)
- **Sessions, Messages, Cron**: already used `fmtTime()` / `fmtIsoTime()` — no changes needed.

---

## 2026-03-08: WhatsApp Bridge & Token Tracking

### lodash vulnerability fix (`7e1a07f`)
`@appium/logger` pinned `lodash@4.17.21` (prototype pollution). `npm audit fix` couldn't resolve it because the pin is an exact version constraint. Added `overrides` in `package.json` to force `lodash >= 4.17.22`.

### WhatsApp send_message + home channel (`73f4d3f`)
Three gaps found during WhatsApp bridge bring-up:

- **`tools/send_message_tool.py`** had no WhatsApp case in `_send_to_platform()` — fell through to "not yet implemented." Added `_send_whatsapp()` that POSTs to the local bridge at `localhost:{bridge_port}/send`, normalizes chat IDs to Baileys JID format (`@s.whatsapp.net`, `@g.us`, `@lid`).
- **`gateway/config.py`** `_apply_env_overrides()` loaded `WHATSAPP_ENABLED` but never read `WHATSAPP_HOME_CHANNEL`. Added it to match the Telegram/Discord/Slack pattern.
- **`scripts/whatsapp-bridge/bridge.js`** — whitespace-only cleanup.

### WhatsApp LID addressing (env-only fix)
WhatsApp migrated to Linked ID (LID) addressing. Messages arrive as `249786724823173@lid` instead of `14153596361@s.whatsapp.net`. Both `bridge.js` and `gateway/run.py` allowlist checks compared the LID against phone numbers — always failed silently.

**Fix:** Added the LID to `WHATSAPP_ALLOWED_USERS` in `~/.hermes/.env`. No code change.

**Policy decision:** We explicitly chose NOT to add permissive LID pass-through logic. The allowlist stays strict — if a new user/device shows up with a different LID, add it to the env var. Simpler, safer, no new auth surface.

**Note:** The home channel (`WHATSAPP_HOME_CHANNEL`) stays as the phone number (`14153596361`), not the LID. Outbound sends need phone-number JIDs; LIDs are only for inbound sender identification.

### Token tracking — DB wiring (uncommitted)
`gateway/run.py` line ~1197 called `update_session()` without passing token counts, even though the agent accumulates them (`session_prompt_tokens`, `session_completion_tokens`) and the DB schema + `update_token_counts()` method already exist.

**Fix:** `_run_agent()` now includes `input_tokens` and `output_tokens` in its return dict (read from `agent_holder[0]`). `_handle_message()` passes them through to `update_session()`. The `/insights` command should now show real numbers.

### Token + cache usage log (uncommitted)
Rather than adding cache columns to the DB schema (migration, more plumbing), we added a simple append-only JSONL log at `~/.hermes/token_usage.jsonl`. One line per API call:

```json
{"ts":"...","session_id":"...","model":"gpt-5.4","prompt_tokens":8630,"completion_tokens":412,"cached_tokens":7200,"cache_write_tokens":0,"api_call":3}
```

Written at `run_agent.py` right after the existing cache stats console print. Includes both regular token counts and Anthropic prompt cache read/write breakdowns. Wrapped in try/except — never interferes with the agent. Easy to query with `jq`.

**Design rationale:** One user, modest volume, no need for DB overhead. The JSONL is human-readable, greppable, and disposable. If we ever need aggregation, we can backfill the DB from it.
