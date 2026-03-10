# Development Journal

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
