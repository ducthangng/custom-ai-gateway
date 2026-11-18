# AI Gateway

A middleman between a chat UI and LLM providers. For every prompt it:

1. **Checks the cache**: asks the KV cache plugin for an exact-match answer. On a hit, the answer comes back without calling the router or a provider.
2. **Asks the router ("JJ")** which model, thinking level, max output tokens and sampling to use. If there's no router, or it returns nothing, fails, or is too slow, it uses the settings from the UI.
3. **Calls the provider** (Anthropic, OpenAI or any OpenAI-compatible server, Gemini, or an offline mock) and streams the answer to the UI.
4. **Records** the conversation and one request-log row (routing decision, cache result, latency, tokens, pipeline steps) in **Postgres**.

```
UI (web/) ──SSE──▶ POST /api/chat
                     │
                     ├─ cache plugin   ──▶ hit? stream it, done (router skipped)
                     ├─ router plugin  ──▶ RouteDecision | None   (None = use the UI settings)
                     ├─ provider SDK   ──▶ stream text + thinking
                     ├─ cache plugin   ◀── store complete answers
                     └─ Postgres       ◀── conversation, messages, request log + steps
```

Each stage is streamed to the UI as a `step` event. In **Auto by JJ** mode the right-hand panel shows only these steps, as a vertical stepper (Check cache, Ask JJ, Model, Thinking level, Max output tokens, Temperature · Top P, Generate answer, Save). Every resolved value is labelled with who chose it: **by JJ**, **by you**, **model default** or **from cache**. Steps are stored with the request, so "Show steps" on any earlier answer replays them.

**Auto by JJ** hands the model, thinking level, max output tokens and sampling to JJ. If JJ can't decide, the gateway uses the values last set in Manual mode. **Manual** skips JJ, and the right-hand panel shows the Run settings instead of the stepper. Auto and Manual requests use separate cache entries. The router's display name comes from `GATEWAY_ROUTER_NAME` (default `JJ`) or a `display_name` attribute on your router class.

The router and the cache are both **fail-open**: an error, a timeout or a bad answer from either one never breaks a chat.

## Run it

```bash
# 1. Postgres
cd gateway && docker compose up -d postgres

# 2. Gateway (API on :8090; serves the built UI at / when web/dist exists)
cp .env.example .env           # add the API keys you have
uv sync
uv run uvicorn gateway.main:app --port 8090 --reload

# 3. UI dev server with hot reload (proxies /api to :8090)
cd ../web && npm install && npm run dev      # http://localhost:5173
```

You don't need any API key to try it: pick **Echo (mock)** in Run settings. Providers without a key show as disabled in the model picker.

The whole stack in containers: `docker compose up --build`, then open http://localhost:8090.

## Plug in your cache and router

Both are plain Python classes that the gateway loads from a dotted path at startup. You don't need to edit gateway code.

```bash
GATEWAY_CACHE_CLASS=my_cache.redis_kv:RedisKV
GATEWAY_ROUTER_CLASS=my_router.semantic:SemanticRouter
```

**Cache.** The interface is in [gateway/plugins/cache.py](gateway/plugins/cache.py). The gateway builds the key (a hash of provider, model, params, system prompt and full history) and the value (a JSON string). Your class only stores strings:

```python
class RedisKV:
    async def startup(self): ...                       # optional
    async def get(self, key: str) -> str | None: ...
    async def set(self, key: str, value: str, ttl_s: int | None = None) -> None: ...
    async def shutdown(self): ...                      # optional
```

`gateway.plugins.cache:MemoryCache` is an in-process LRU you can use until yours is ready. Lookups time out after `GATEWAY_CACHE_TIMEOUT_S` (50 ms) and count as a miss. Truncated (`max_tokens`), refused and failed answers are never cached.

**Router.** The interface is in [gateway/plugins/router.py](gateway/plugins/router.py):

```python
class SemanticRouter:
    async def route(self, req: RouteRequest) -> RouteDecision | None:
        # req.prompt, req.history, req.requested (user's pick), req.params,
        # req.catalog (all models + capabilities), req.available_providers
        return RouteDecision(provider="anthropic", model="claude-haiku-5-5",
                             params={"level": "low"}, reason="small talk", score=0.82)
```

Return `None` to keep the user's model. The gateway ignores a decision whose model isn't in `catalog.yaml` or whose provider has no key. Param overrides are fitted to the chosen model: unsupported levels fall back to the model default, temperature is dropped for models that don't accept it, and `max_tokens` is capped. [examples/semantic_router_http.py](examples/semantic_router_http.py) shows the shape of a router that calls an external HTTP service.

### Built-in plugins

```bash
GATEWAY_ROUTER_CLASS=gateway.plugins.rules_router:RulesRouter   # JJ, rules from routes.yaml (GATEWAY_ROUTES_PATH)
GATEWAY_CACHE_CLASS=gateway.plugins.redis_cache:RedisCache      # GATEWAY_REDIS_URL=redis://localhost:6379/0
```

**JJ** ([gateway/routing/](gateway/routing/), [routes.yaml](routes.yaml)): each route has keywords (whole words, any case) and regexes; the route with the most distinct matches wins, ties go to the first route, and below `min_score` JJ returns `None`. A route lists candidate models (`provider/model` from `catalog.yaml`) in order of preference; the first whose provider has a key is used, with the route's `params` as overrides. The reason shown in the UI names the route, the matched words and the `routes.yaml` version.

**RedisCache** ([gateway/plugins/redis_cache.py](gateway/plugins/redis_cache.py)) shares answers between replicas and survives restarts. Client retries are off on purpose: a slow Redis should cost one timeout and count as a miss.

## Quality gates (CI, see ../Jenkinsfile)

```bash
uv run pytest -q                                                     # quick_tests/: 3 small tests, every push
uv run python -m evals.run --split test --gate evals/baseline.json   # JJ accuracy on the held-out split
uv run python -m release_check.run                                   # release check: 2 checks, PASS/FAIL
```

- **Quick tests** (`quick_tests/`, run on every push by job `ai-gateway`):
  - `test_router.py`: a coding question goes to route `code`; a joke goes to route `chitchat`.
  - `test_cache.py`: the Redis cache gives back what was saved (on fakeredis, no server).
- **Eval gate:** `evals/dev.jsonl` (rules were written against it) and `evals/test.jsonl` (held out). CI fails if test accuracy drops more than 2 points below `evals/baseline.json`.
- **Release check** (`release_check/run.py`, Jenkins job `qualify`):
  - `models_exist`: every model in `routes.yaml` is in `catalog.yaml`.
  - `routes_unchanged`: 100 saved prompts still get the route stored in `release_check/expected_routes.json`. After an intended routing change, accept it with `python -m release_check.run --save`.

The bigger test suite, the coverage gate and the old release check tool are archived in `deprecated/`.

In the UI, **Routing: Manual** skips the router for that chat.

## Demo: break it, push, fix it

Each demo is a one-line edit. Make it, run the command (or push and watch Jenkins), see the failure,
undo the edit (`git checkout -- <file>`), and it is green again. All four were checked on 2026-11-18.

**Quick tests** (job `ai-gateway`, stage `Quick tests`; locally `uv run pytest -q`)

| Demo | Edit | What fails |
|---|---|---|
| Cache forgets to save | `gateway/plugins/redis_cache.py`, in `set()`: replace `await self.r.set(key, value, ex=ttl_s or None)` with `pass` | `test_cache_gives_back_what_was_saved`: `assert None == 'answer'` |
| Route renamed | `routes.yaml`: `- name: chitchat` → `- name: smalltalk` | `test_joke_goes_to_chitchat_route`: `assert 'smalltalk' == 'chitchat'` |

**Release check** (job `qualify`; locally `uv run python -m release_check.run`)

| Demo | Edit | What fails |
|---|---|---|
| Model that does not exist | `routes.yaml`, route `code`: `anthropic/claude-sonnet-5-5` → `anthropic/claude-sonnet-9` | `FAIL models_exist`: `route 'code' uses anthropic/claude-sonnet-9, which is not in catalog.yaml`. The quick tests still pass |
| Keyword deleted | `routes.yaml`, route `chitchat`: delete `joke,` from the keywords | `FAIL routes_unchanged`: `'tell me a joke about programmers': expected chitchat, got none` (and 1 more). The quick tests and the CI eval gate (93.8%) still pass: only the release check catches it |

## Models

[catalog.yaml](catalog.yaml) is the source of truth for providers and models: ids, labels, thinking levels, whether temperature/top P are accepted, and output limits. The UI builds its model picker and settings from it. Restart the gateway after editing it.

- **Anthropic**: thinking is adaptive, and the "Thinking level" sets `output_config.effort`. The current API rejects temperature/top P, so those controls are hidden for Claude models. Opus 5.5, Sonnet 5.5 and Fable 5.2 have server-side refusal fallback turned on (`refusal_fallback: default`): if the model declines, the API retries on a fallback model in the same call. The request log's `served_model` shows which model answered. Remove the line to turn it off.
- **OpenAI / OpenAI-compatible**: the level maps to `reasoning_effort`. Ollama is preconfigured at `localhost:11434`.
- **Gemini 2.5**: the level maps to a thinking token budget.

## API

| Method | Path | |
|---|---|---|
| POST | `/api/chat` | Server-Sent Events: `meta` (ids + step list), `step`*, `model`, `thinking`*, `text`*, then `done` or `error` |
| GET | `/api/catalog` | providers (with `configured`) and models |
| GET/PUT | `/api/settings` | the user's default run settings (model, level, routing, ...) |
| GET | `/api/conversations` · `/api/conversations/{id}` | chat history |
| PATCH/DELETE | `/api/conversations/{id}` | rename / delete |
| GET | `/api/requests` · `/api/stats` | request log and summary (cache hit rate, latency p50/p95) |

Interactive docs are at `/docs`.

## Data (Postgres)

| Table | What |
|---|---|
| `conversations` | title, last-used run settings |
| `messages` | user and assistant turns, with thinking text and errors |
| `requests` | one row per call: requested vs. actual model, routed_by, cache status, status, tokens, TTFT, latency, pipeline `steps` |
| `app_settings` | the default run settings |

Tables are created on startup (`create_all`), and later columns are added by the `ADDITIVE_COLUMNS` list in [db.py](gateway/db.py) (for example `requests.steps`). Switch to Alembic once the schema changes more often.

## Tests

```bash
uv run pytest -q          # quick_tests/: no API keys, Postgres or Redis needed
uv run ruff check .
```
