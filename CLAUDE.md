# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this project is

**DeCho Agent** — an all-in-one AI marketing operations agent (Claw-a-thon 2026 entry). One FastAPI server + one no-build React UI. Two data modules (PageSpeed Insights, SEO via GSC+GA4) write to Google Sheets; a chat agent (GreenNode MaaS LLM) routes natural-language requests to actions and analyzes sheet data. A mascot character (DeCho) reacts to app state and answers screen-context questions.

## Commands

```bash
source .venv/bin/activate
pip install -r requirements.txt
python server.py                      # serve UI + API on :8000 (PORT env to change)
python psi_checker.py --once          # one-off PSI run (CLI)
python seo_agent.py --month 2026-05   # one-off SEO report (CLI)
docker build --platform linux/amd64 -t decho-agent .   # AgentBase runs amd64!
```

No JS build step: `static/index.html` contains JSX compiled in-browser by Babel standalone. To syntax-check it: extract the `<script type="text/babel">` body and run `npx esbuild --loader:.jsx=jsx`.

## Architecture

| File | Role |
|---|---|
| `server.py` | FastAPI. All endpoints, SSE chat streams, intent routing, scheduler, sheet-read cache, SEO log capture. ~1200 lines, the heart of the app. |
| `psi_checker.py` | PSI checks: `run_check_iter()` generator yields progress events (used for real-time SSE); 3 parallel workers (`PSI_WORKERS`), 3 retries w/ backoff; writes monthly tab + conditional formatting via service account. |
| `seo_agent.py` | GSC + GA4 monthly report → SEO Sheet (separate sheet, OAuth user token). `run_for_month(y,m)`. Env-driven config with runtime overrides via `runtime_val()`. |
| `runtime_config.py` | Dynamic config overlay (PSI urls/schedule + SEO schedule/tracked) persisted to `runtime_config.json` (gitignored) and synced to PSI Sheet tab `_config` via `on_change` hook → survives container recreate. |
| `sheet_store.py` | PSI Sheet I/O: `_config` tab (config persist), `_logs` tab (run history), `read_results`/`read_results_data` (dashboard data). |
| `static/index.html` | Entire UI: React 18 + Tailwind (CDN). Views: home (Tổng quan), chat, urls (URL Intelligence), dash (PageSpeed Dashboard), alerts, config. DeCho components + `dechoBus` event bus + `dechoCtx` screen context. |
| `SOUL.md` / `AGENT.md` | Agent personality / domain knowledge. Hot-reloaded by mtime (`_persona()` / `_knowledge()` in server.py). Persona goes in every prompt; knowledge only in analysis prompts. |

## Key flows

**Chat (all-in-one)**: UI → `POST /api/agent/chat/stream` (SSE). Server: (1) LLM intent classification (`_unified_prompt`, strict JSON, `/no_think`, 4096 tokens) with `_all_keyword_intent` fallback; (2) dispatch: PSI run (streams per-URL progress from `run_check_iter`), SEO run (streams captured log lines, supports multi-month batch), PSI/SEO analysis (reads sheet → second LLM call streamed as `delta` events), config actions. Event protocol: `{type: step|delta|final|error|done}`.

**Reasoning models**: Qwen may emit `<think>` blocks or put everything in `reasoning_content`. All parsing accumulates the full stream then strips think tags (handles tags split across chunks by holding back 12 chars); falls back to reasoning content when content is empty. Never show chain-of-thought to users.

**DeCho character**: `DechoScene` wrapper probes assets: `static/poses/idle.png` (2D pose + CSS anims) → `static/sprites/idle.png` (12-frame canvas) → `static/decho.glb` (three.js, procedural anims). All driven by `dechoBus` events: `busy(bool)`, `say(text)`, `act('jump'|'nod'|'sad')`. `dechoCtx={view,info,model}` is updated by each view; `POST /api/decho/ask` answers questions with that context.

**Quota protection**: Sheets API = 60 reads/min/user. Server caches reads (`_cached`, TTLs 60s–600s; invalidated after any run completes). Client caches the 3 dashboard fetches 60s (`_agentCache`). Don't add per-request sheet reads without caching.

## Credentials & env

- `.env` (gitignored): `PSI_API_KEY`, `SHEET_ID`, `MAAS_BASE_URL/_API_KEY/_MODEL`, SEO vars.
- PSI Sheet: service account — `service_account.json` file or `SERVICE_ACCOUNT_JSON` env (deploy).
- SEO (GSC/GA4/SEO Sheet): OAuth user token — `token.json` file or `SEO_TOKEN_JSON` env (deploy). No browser flow on server; token must be created locally first.
- Never commit: `.env`, `service_account.json`, `token.json`, `runtime_config.json` (all gitignored; `.dockerignore` keeps them out of images too).

## Gotchas

- AgentBase runtime is **linux/amd64** — always build with `--platform linux/amd64` on Apple Silicon.
- `asyncio.to_thread(next, it)` needs a sentinel default — StopIteration can't cross a Future.
- Gemma sometimes outputs LaTeX (`$\rightarrow$`); UI `md()` converts to unicode, prompts forbid it.
- Schedule changes apply via `_build_schedule()` (called on config save) — scheduler ticks every 30s.
- The two legacy chat endpoints (`/api/chat/stream`, `/api/seo/chat/stream`) still work but the UI only uses `/api/agent/chat/stream`.
- UI chat history lives in browser localStorage (`decho-chat`), capped 100 messages, sent as last-10 history for conversation memory.
