# Crawl4AI Docker API — Complete Collection

> **Base URL**: `http://localhost:11235`  
> **Auth**: Bearer token (JWT). Get one from `POST /token`.  
> **Content-Type**: `application/json` for all POST bodies.

---

## Quick-start (no auth)

If security is disabled in your `config.yml`, all endpoints work without a token.  
If security is enabled, include the header on every request:

```http
Authorization: Bearer <access_token>
```

---

## Table of Contents

| # | Method | Path | Purpose |
|---|--------|------|---------|
| 1 | POST | `/token` | Get JWT access token |
| 2 | GET | `/health` | Health check |
| 3 | GET | `/schema` | Dump default BrowserConfig + CrawlerRunConfig |
| 4 | POST | `/config/dump` | Parse & dump a config constructor call |
| 5 | POST | `/md` | Convert a URL to Markdown |
| 6 | POST | `/html` | Get preprocessed HTML |
| 7 | POST | `/screenshot` | Capture full-page screenshot |
| 8 | POST | `/pdf` | Generate PDF |
| 9 | POST | `/execute_js` | Execute JS on page |
| 10 | GET | `/llm/{url}` | LLM Q&A on a webpage (sync) |
| 11 | POST | `/llm/job` | Enqueue async LLM extraction job |
| 12 | GET | `/llm/job/{task_id}` | Poll LLM job status |
| 13 | POST | `/crawl` | Crawl URL(s) — sync or stream |
| 14 | POST | `/crawl/stream` | Stream crawl (NDJSON) |
| 15 | POST | `/crawl/job` | Enqueue async crawl job |
| 16 | GET | `/crawl/job/{task_id}` | Poll crawl job status |
| 17 | POST | `/crawl/cre` | CRE deep crawl (sync) |
| 18 | POST | `/crawl/cre/stream` | CRE deep crawl (streaming NDJSON) |
| 19 | POST | `/crawl/cre/job` | Enqueue async CRE deep-crawl job |
| 20 | GET | `/crawl/cre/job/{task_id}` | Poll CRE job status |
| 21 | GET | `/ask` | BM25 search over library docs/code |
| 22 | GET | `/hooks/info` | List available hook points |

---

## 1. POST `/token`

Exchange your API key for a short-lived JWT.

### Request body

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `email` | `string` | ✅ | Your email (domain is validated) |
| `api_token` | `string` | ✅ | The server API token from `config.yml` |

```json
{
  "email": "you@example.com",
  "api_token": "your_api_token_here"
}
```

### Response

| Field | Type | Description |
|-------|------|-------------|
| `email` | `string` | The email you submitted |
| `access_token` | `string` | JWT — attach as `Authorization: Bearer <token>` |
| `token_type` | `string` | Always `"bearer"` |

```json
{
  "email": "you@example.com",
  "access_token": "eyJhbGci...",
  "token_type": "bearer"
}
```

---

## 2. GET `/health`

Quick liveness probe.

### Response

```json
{
  "status": "ok",
  "timestamp": 1712700000.0,
  "version": "0.6.0"
}
```

---

## 3. GET `/schema`

Returns the default field values for `BrowserConfig` and `CrawlerRunConfig`. Useful to discover all available crawler/browser options before building a request.

### Response

```json
{
  "browser": { "headless": true, "viewport_width": 1080, ... },
  "crawler": { "cache_mode": "WRITE_ONLY", "page_timeout": 60000, ... }
}
```

---

## 4. POST `/config/dump`

Parse a Python-style constructor call and return the serialised dict. Useful for client-side config generation.

### Request body

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `code` | `string` | ✅ | A single `CrawlerRunConfig(...)` or `BrowserConfig(...)` expression |

```json
{
  "code": "CrawlerRunConfig(cache_mode='ENABLED', page_timeout=30000)"
}
```

### Response

Returns the serialised config dict.

```json
{ "cache_mode": "ENABLED", "page_timeout": 30000, ... }
```

---

## 5. POST `/md`

Fetch a webpage and convert it to Markdown.

### Request body

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `url` | `string` | ✅ | — | `http/https` URL to crawl. Also accepts `raw:` or `raw://` for inline HTML. |
| `f` | `string` | ❌ | `"fit"` | Filter strategy: `"fit"` (readability), `"raw"` (DOM dump), `"bm25"` (relevance), `"llm"` (AI summary) |
| `q` | `string` | ❌ | `null` | Query string — required/helpful for `bm25` and `llm` filters |
| `c` | `string` | ❌ | `"0"` | Cache control: `"1"` = use cache, `"0"` = bypass |
| `provider` | `string` | ❌ | `null` | LLM provider override e.g. `"openai/gpt-4o-mini"` (only for `llm` filter) |
| `temperature` | `number` | ❌ | `null` | LLM temperature `0.0–2.0` |
| `base_url` | `string` | ❌ | `null` | Custom LLM API base URL |

```json
{
  "url": "https://example.com/article",
  "f": "fit",
  "q": null,
  "c": "0"
}
```

### Response

| Field | Type | Description |
|-------|------|-------------|
| `url` | `string` | The URL that was crawled |
| `filter` | `string` | The filter strategy used |
| `query` | `string \| null` | The query passed in |
| `cache` | `string` | Cache setting used |
| `markdown` | `string` | The extracted/converted Markdown text |
| `success` | `boolean` | `true` if crawl succeeded |

```json
{
  "url": "https://example.com/article",
  "filter": "fit",
  "query": null,
  "cache": "0",
  "markdown": "# Article Title\n\nBody content...",
  "success": true
}
```

---

## 6. POST `/html`

Crawl a URL and return preprocessed HTML ready for schema extraction.

### Request body

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `url` | `string` | ✅ | `http/https` (or `raw:`) URL |

```json
{ "url": "https://example.com" }
```

### Response

| Field | Type | Description |
|-------|------|-------------|
| `html` | `string` | Preprocessed/sanitised HTML |
| `url` | `string` | The crawled URL |
| `success` | `boolean` | `true` if succeeded |

---

## 7. POST `/screenshot`

Capture a full-page PNG screenshot (returned as base-64 string).

### Request body

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `url` | `string` | ✅ | — | `http/https` URL |
| `screenshot_wait_for` | `number` | ❌ | `2` | Seconds to wait after page load before capture |
| `wait_for_images` | `boolean` | ❌ | `false` | Whether to wait for all images to load |
| `output_path` | `string` | ❌ | `null` | Server-side file path to save the PNG. If set, `screenshot` field is omitted from response. |

```json
{
  "url": "https://example.com",
  "screenshot_wait_for": 2,
  "wait_for_images": false
}
```

### Response

| Field | Type | Description |
|-------|------|-------------|
| `success` | `boolean` | |
| `screenshot` | `string` | Base64-encoded PNG (omitted if `output_path` was set) |
| `path` | `string` | Absolute server path (only if `output_path` was set) |

---

## 8. POST `/pdf`

Generate a PDF of the rendered page.

### Request body

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `url` | `string` | ✅ | — | `http/https` URL |
| `output_path` | `string` | ❌ | `null` | Server-side path to save the PDF. If set, `pdf` is omitted from response. |

```json
{ "url": "https://example.com" }
```

### Response

| Field | Type | Description |
|-------|------|-------------|
| `success` | `boolean` | |
| `pdf` | `string` | Base64-encoded PDF bytes (omitted if `output_path` set) |
| `path` | `string` | Absolute server path (only if `output_path` set) |

---

## 9. POST `/execute_js`

Navigate to a URL, execute a list of JS snippets in order, and return the full `CrawlResult`.

### Request body

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `url` | `string` | ✅ | `http/https` URL |
| `scripts` | `string[]` | ✅ | Ordered list of JS expressions (IIFE or async function body). Each must return a value. |

```json
{
  "url": "https://example.com",
  "scripts": [
    "(function() { return document.title; })()",
    "async function() { await new Promise(r => setTimeout(r, 500)); return 'done'; }"
  ]
}
```

### Response

Returns the full `CrawlResult` object:

| Field | Type | Description |
|-------|------|-------------|
| `url` | `string` | Final URL (after redirects) |
| `html` | `string` | Raw page HTML |
| `success` | `boolean` | |
| `cleaned_html` | `string \| null` | Sanitised HTML |
| `markdown` | `MarkdownGenerationResult \| null` | See sub-type below |
| `links` | `{ internal: Link[], external: Link[] }` | Extracted links |
| `media` | `object` | Images/videos/audio found |
| `js_execution_result` | `object \| null` | JS return values keyed by script index |
| `screenshot` | `string \| null` | Base64 PNG if `screenshot:true` was set |
| `pdf` | `string \| null` | Base64 PDF |
| `extracted_content` | `string \| null` | LLM-extracted content (if applicable) |
| `metadata` | `object \| null` | Page metadata |
| `error_message` | `string \| null` | Error if `success=false` |
| `status_code` | `number \| null` | HTTP status |
| `response_headers` | `object \| null` | HTTP headers |
| `network_requests` | `object[] \| null` | Captured network requests |
| `console_messages` | `object[] \| null` | Browser console output |

#### `MarkdownGenerationResult` sub-type

| Field | Type | Description |
|-------|------|-------------|
| `raw_markdown` | `string` | Unfiltered DOM-to-Markdown |
| `markdown_with_citations` | `string` | Markdown with inline link citations |
| `references_markdown` | `string` | Reference section |
| `fit_markdown` | `string \| null` | Filtered/cleaned version |
| `fit_html` | `string \| null` | HTML used to produce `fit_markdown` |

---

## 10. GET `/llm/{url}`

Synchronous LLM Q&A: crawl the URL, then answer the query with the page as context.

### Path parameters

| Param | Description |
|-------|-------------|
| `url` | Full URL (e.g. `https://example.com`). URL-encode slashes if needed, or just pass the bare domain — `https://` is prepended automatically. |

### Query parameters

| Param | Type | Required | Description |
|-------|------|----------|-------------|
| `q` | `string` | ✅ | The question to answer using the page content |
| `provider` | `string` | ❌ | LLM provider e.g. `"openai/gpt-4o"` |
| `temperature` | `number` | ❌ | `0.0–2.0` |
| `base_url` | `string` | ❌ | Custom LLM API base URL |

```
GET /llm/https://example.com?q=What+is+this+page+about&provider=openai/gpt-4o-mini
```

### Response

| Field | Type | Description |
|-------|------|-------------|
| `answer` | `string` | The LLM's answer |

```json
{ "answer": "This page is about..." }
```

---

## 11. POST `/llm/job`

Enqueue an async LLM extraction / Q&A job. Returns immediately with a `task_id`.

### Request body

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `url` | `string` | ✅ | — | `http/https` URL |
| `q` | `string` | ✅ | — | Instruction / query for the LLM |
| `schema` | `string \| null` | ❌ | `null` | JSON schema string for structured extraction |
| `cache` | `boolean` | ❌ | `false` | Use crawler cache |
| `provider` | `string \| null` | ❌ | `null` | LLM provider override |
| `temperature` | `number \| null` | ❌ | `null` | LLM temperature |
| `base_url` | `string \| null` | ❌ | `null` | LLM API base URL |
| `webhook_config` | `WebhookConfig \| null` | ❌ | `null` | See [WebhookConfig](#webhookconfig) |

```json
{
  "url": "https://example.com/product",
  "q": "Extract the product name, price, and description",
  "schema": "{\"type\":\"object\",\"properties\":{\"name\":{\"type\":\"string\"},\"price\":{\"type\":\"number\"}}}",
  "cache": false
}
```

### Response (202 Accepted)

| Field | Type | Description |
|-------|------|-------------|
| `task_id` | `string` | Unique task ID — use with `GET /llm/job/{task_id}` |
| `status` | `string` | Always `"processing"` initially |
| `url` | `string` | The URL being crawled |
| `_links` | `object` | HATEOAS links: `self`, `status` |

---

## 12. GET `/llm/job/{task_id}`

Poll the status of a queued LLM job.

### Path parameters

| Param | Description |
|-------|-------------|
| `task_id` | The `task_id` returned from `POST /llm/job` |

### Response

| Field | Type | Description |
|-------|------|-------------|
| `task_id` | `string` | |
| `status` | `string` | `"processing"`, `"completed"`, or `"failed"` |
| `created_at` | `string` | ISO-8601 timestamp |
| `url` | `string` | The crawled URL |
| `result` | `any` | *(only when `status="completed"`)* Extracted content or LLM answer |
| `error` | `string` | *(only when `status="failed"`)* Error message |
| `_links` | `object` | `self`, `refresh` |

---

## 13. POST `/crawl`

Crawl one or more URLs and return results synchronously. If `crawler_config.stream` is `true`, automatically redirects to streaming NDJSON mode.

### Request body

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `urls` | `string[]` | ✅ | — | 1–100 URLs to crawl |
| `browser_config` | `object` | ❌ | `{}` | Serialised `BrowserConfig` dict (see `GET /schema`) |
| `crawler_config` | `object` | ❌ | `{}` | Serialised `CrawlerRunConfig` dict |
| `hooks` | `HookConfig \| null` | ❌ | `null` | Custom hook code (requires `CRAWL4AI_HOOKS_ENABLED=true`) |

#### Key `browser_config` fields

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `headless` | `boolean` | `true` | Run Chromium headless |
| `viewport_width` | `number` | `1080` | Browser viewport width |
| `viewport_height` | `number` | `600` | Browser viewport height |
| `user_agent` | `string \| null` | `null` | Custom User-Agent string |
| `user_agent_mode` | `string` | `"default"` | `"random"` for random UA rotation |
| `enable_stealth` | `boolean` | `false` | Enable anti-bot stealth mode |
| `proxy` | `string \| null` | `null` | Proxy URL |
| `extra_args` | `string[]` | `[]` | Additional Chromium launch args |
| `ignore_https_errors` | `boolean` | `false` | Skip SSL errors |

#### Key `crawler_config` fields

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `cache_mode` | `string` | `"WRITE_ONLY"` | `"ENABLED"`, `"DISABLED"`, `"WRITE_ONLY"`, `"READ_ONLY"`, `"BYPASS"` |
| `page_timeout` | `number` | `60000` | Page load timeout in ms |
| `wait_for` | `string \| null` | `null` | CSS selector or JS expression to wait for |
| `js_code` | `string \| string[] \| null` | `null` | JS to run after page load |
| `screenshot` | `boolean` | `false` | Capture page screenshot |
| `pdf` | `boolean` | `false` | Generate PDF |
| `stream` | `boolean` | `false` | Stream results via NDJSON |
| `deep_crawl_strategy` | `object \| null` | `null` | Deep crawl config (BFS/DFS/BestFirst) |
| `word_count_threshold` | `number` | `200` | Min word count to keep a page |
| `excluded_tags` | `string[]` | `[]` | HTML tags to strip |
| `exclude_external_links` | `boolean` | `false` | Drop off-domain links |
| `process_iframes` | `boolean` | `false` | Include iframe content |
| `remove_overlay_elements` | `boolean` | `false` | Remove modals/popups |
| `simulate_user` | `boolean` | `false` | Simulate human interaction |
| `wait_until` | `string` | `"domcontentloaded"` | Navigation wait event |

#### `HookConfig` shape

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `code` | `{ [hookPoint: string]: string }` | ✅ | — | Map of hook names to Python async function strings |
| `timeout` | `number` | ❌ | `30` | Max seconds per hook execution (1–120) |

Available hook points: `on_browser_created`, `on_page_context_created`, `before_goto`, `after_goto`, `on_user_agent_updated`, `on_execution_started`, `before_retrieve_html`, `before_return_html`

```json
{
  "urls": ["https://example.com"],
  "browser_config": { "headless": true },
  "crawler_config": {
    "cache_mode": "BYPASS",
    "page_timeout": 30000,
    "word_count_threshold": 100
  }
}
```

### Response

| Field | Type | Description |
|-------|------|-------------|
| `success` | `boolean` | Overall success flag |
| `results` | `CrawlResult[]` | Array of per-URL results (see `/execute_js` for `CrawlResult` shape) |
| `server_processing_time_s` | `number` | Wall-clock time on server (seconds) |
| `server_memory_delta_mb` | `number \| null` | Memory delta during request (MB) |
| `server_peak_memory_mb` | `number \| null` | Peak memory during request (MB) |
| `hooks` | `object \| null` | Hook execution log (if hooks were used) |

---

## 14. POST `/crawl/stream`

Same as `POST /crawl` but always returns a streaming NDJSON response.

- **Response**: `Content-Type: application/x-ndjson`
- Each line is a JSON-serialised `CrawlResult` object
- The **final line** is always `{"status": "completed"}`
- Response header `X-Stream-Status: active` while streaming

---

## 15. POST `/crawl/job`

Enqueue a crawl as a background job. Returns immediately.

### Request body

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `urls` | `string[]` | ✅ | — | URLs to crawl |
| `browser_config` | `object` | ❌ | `{}` | See `/crawl` |
| `crawler_config` | `object` | ❌ | `{}` | See `/crawl` |
| `webhook_config` | `WebhookConfig \| null` | ❌ | `null` | See [WebhookConfig](#webhookconfig) |

### Response (202)

```json
{ "task_id": "crawl_a1b2c3d4" }
```

---

## 16. GET `/crawl/job/{task_id}`

Poll a background crawl job.

### Response

Same structure as `GET /llm/job/{task_id}`.  
When `status="completed"`, `result` contains the full `{ success, results[], server_processing_time_s, ... }` response.

---

## 17. POST `/crawl/cre`

CRE-optimised **deep crawl** — synchronous.

Automatically applies:
- DFS/BFS/BestFirst deep-crawl strategy
- CRE domain filter + keyword scorer
- Stealth browser (random UA, anti-bot flags)
- WAF-safe timing (`domcontentloaded` + 90 s timeout)

Returns all crawled pages in one JSON response (use `/crawl/cre/stream` or `/crawl/cre/job` for large sites).

### Request body

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `url` | `string` | ✅ | — | Seed URL `http/https` |
| `strategy` | `string` | ❌ | `"dfs"` | `"dfs"`, `"bfs"`, or `"best-first"` |
| `max_pages` | `number` | ❌ | `500` | Hard cap on pages crawled (1–5000) |
| `max_depth` | `number` | ❌ | `10` | Max link-hop depth from seed (1–20) |
| `include_news` | `boolean` | ❌ | `false` | Include news/blog/press URLs |
| `no_html` | `boolean` | ❌ | `true` | Strip `html`, `cleaned_html`, `fit_html` to reduce response size |
| `webhook_config` | `WebhookConfig \| null` | ❌ | `null` | *(used only by the `/job` variant)* |

```json
{
  "url": "https://www.cbre.com",
  "strategy": "bfs",
  "max_pages": 200,
  "max_depth": 5,
  "include_news": false,
  "no_html": true
}
```

### Response

| Field | Type | Description |
|-------|------|-------------|
| `success` | `boolean` | |
| `results` | `CrawlResult[]` | All crawled pages (HTML fields stripped if `no_html=true`) |
| `total_pages` | `number` | Count of crawled pages |
| `server_processing_time_s` | `number` | Wall-clock time |
| `server_memory_delta_mb` | `number \| null` | Memory delta |

---

## 18. POST `/crawl/cre/stream`

Same as `POST /crawl/cre` but streams results as NDJSON.

- Same request body as `/crawl/cre`
- `no_html` is respected on each streamed chunk
- Final line: `{"status": "completed"}`

---

## 19. POST `/crawl/cre/job`

Enqueue a CRE deep crawl as a background job. Returns `task_id` immediately.

### Request body

Same as `POST /crawl/cre` (including optional `webhook_config`).

### Response (202)

```json
{ "task_id": "cre_a1b2c3d4" }
```

---

## 20. GET `/crawl/cre/job/{task_id}`

Poll a CRE background job.

Same response shape as other `GET .../job/{task_id}` endpoints.

---

## 21. GET `/ask`

BM25 search over Crawl4AI library source code and documentation. Designed for AI assistants to get library context.

### Query parameters

| Param | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `context_type` | `string` | ❌ | `"all"` | `"code"`, `"doc"`, or `"all"` |
| `query` | `string` | ❌ | `null` | BM25 search query to filter results |
| `score_ratio` | `number` | ❌ | `0.5` | Min score as fraction of max score (0.0–1.0) |
| `max_results` | `number` | ❌ | `20` | Max chunks returned |

```
GET /ask?context_type=code&query=deep+crawl+strategy&max_results=10
```

### Response

| Field | Type | Description |
|-------|------|-------------|
| `code_results` | `{ text: string, score: number }[]` | Code chunks (if `context_type` includes code) |
| `doc_results` | `{ text: string, score: number }[]` | Doc sections (if `context_type` includes doc) |

---

## 22. GET `/hooks/info`

Describe all available crawler hook points.

### Response

```json
{
  "available_hooks": {
    "on_page_context_created": {
      "parameters": [...],
      "description": "Called after page and context are created - ideal for authentication",
      "example": "async def hook(page, context, **kwargs): ..."
    },
    ...
  },
  "timeout_limits": { "min": 1, "max": 120, "default": 30 }
}
```

---

## WebhookConfig

Used by async job endpoints (`/llm/job`, `/crawl/job`, `/crawl/cre/job`).

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `webhook_url` | `string` | ✅ | — | URL that will receive a POST when the job completes |
| `webhook_data_in_payload` | `boolean` | ❌ | `false` | Include full result data in the webhook POST body |
| `webhook_headers` | `{ [key: string]: string } \| null` | ❌ | `null` | Extra headers to send with the webhook POST |

### Webhook POST body (sent to your URL)

| Field | Type | Description |
|-------|------|-------------|
| `task_id` | `string` | |
| `task_type` | `string` | `"crawl"`, `"llm_extraction"`, or `"cre_crawl"` |
| `status` | `string` | `"completed"` or `"failed"` |
| `timestamp` | `string` | ISO-8601 |
| `urls` | `string[]` | URLs that were processed |
| `error` | `string \| null` | Error message if failed |
| `data` | `object \| null` | Full result (only if `webhook_data_in_payload=true`) |

---

## TypeScript Client Snippet

```typescript
const BASE = "http://localhost:11235";

async function getToken(email: string, apiToken: string) {
  const res = await fetch(`${BASE}/token`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ email, api_token: apiToken }),
  });
  const { access_token } = await res.json();
  return access_token;
}

function authHeaders(token: string) {
  return {
    "Content-Type": "application/json",
    Authorization: `Bearer ${token}`,
  };
}

// ── Markdown extraction ──────────────────────────────────────────────────────
async function getMarkdown(url: string, token: string) {
  const res = await fetch(`${BASE}/md`, {
    method: "POST",
    headers: authHeaders(token),
    body: JSON.stringify({ url, f: "fit", c: "0" }),
  });
  return (await res.json()) as {
    url: string; filter: string; query: string | null;
    cache: string; markdown: string; success: boolean;
  };
}

// ── Single crawl ─────────────────────────────────────────────────────────────
async function crawlUrl(url: string, token: string) {
  const res = await fetch(`${BASE}/crawl`, {
    method: "POST",
    headers: authHeaders(token),
    body: JSON.stringify({
      urls: [url],
      browser_config: { headless: true },
      crawler_config: { cache_mode: "BYPASS", page_timeout: 30000 },
    }),
  });
  return await res.json();
}

// ── CRE deep crawl ───────────────────────────────────────────────────────────
async function deepCrawlCRE(url: string, token: string) {
  const res = await fetch(`${BASE}/crawl/cre`, {
    method: "POST",
    headers: authHeaders(token),
    body: JSON.stringify({
      url,
      strategy: "bfs",
      max_pages: 100,
      max_depth: 4,
      include_news: false,
      no_html: true,
    }),
  });
  return await res.json() as {
    success: boolean;
    results: Array<{
      url: string; success: boolean;
      markdown?: { raw_markdown: string; fit_markdown: string | null };
      links?: { internal: any[]; external: any[] };
      metadata?: Record<string, any>;
      error_message?: string;
    }>;
    total_pages: number;
    server_processing_time_s: number;
  };
}

// ── CRE deep crawl — async job + poll ────────────────────────────────────────
async function deepCrawlJob(url: string, token: string) {
  // Enqueue
  const enqueue = await fetch(`${BASE}/crawl/cre/job`, {
    method: "POST",
    headers: authHeaders(token),
    body: JSON.stringify({ url, strategy: "dfs", max_pages: 500, max_depth: 10, no_html: true }),
  });
  const { task_id } = await enqueue.json();

  // Poll until done
  while (true) {
    await new Promise((r) => setTimeout(r, 3000));
    const poll = await fetch(`${BASE}/crawl/cre/job/${task_id}`, {
      headers: authHeaders(token),
    });
    const data = await poll.json();
    if (data.status === "completed") return data.result;
    if (data.status === "failed") throw new Error(data.error);
  }
}

// ── Streaming CRE crawl ──────────────────────────────────────────────────────
async function* streamCRECrawl(url: string, token: string) {
  const res = await fetch(`${BASE}/crawl/cre/stream`, {
    method: "POST",
    headers: authHeaders(token),
    body: JSON.stringify({ url, strategy: "bfs", max_pages: 100, no_html: true }),
  });
  const reader = res.body!.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const lines = buffer.split("\n");
    buffer = lines.pop()!;
    for (const line of lines) {
      if (!line.trim()) continue;
      const obj = JSON.parse(line);
      if (obj.status === "completed") return;
      yield obj; // individual CrawlResult
    }
  }
}

// ── LLM Q&A on a page ────────────────────────────────────────────────────────
async function llmAsk(url: string, question: string, token: string) {
  const res = await fetch(
    `${BASE}/llm/${encodeURIComponent(url)}?q=${encodeURIComponent(question)}`,
    { headers: { Authorization: `Bearer ${token}` } }
  );
  const { answer } = await res.json();
  return answer as string;
}

// ── Screenshot ───────────────────────────────────────────────────────────────
async function screenshot(url: string, token: string): Promise<string> {
  const res = await fetch(`${BASE}/screenshot`, {
    method: "POST",
    headers: authHeaders(token),
    body: JSON.stringify({ url, screenshot_wait_for: 2 }),
  });
  const { screenshot } = await res.json();
  return screenshot; // base64 PNG
}
```

---

## Error responses

All endpoints follow this shape on error:

```json
{
  "detail": "Human-readable error message"
}
```

HTTP status codes:
- `400` — Bad request (invalid params)
- `401` / `403` — Auth/permission error
- `404` — Resource not found (task ID gone)
- `429` — Rate limit exceeded
- `500` — Server/crawl error

---

## Environment / Docker

```yaml
# docker-compose.yml excerpt
ports:
  - "11235:11235"      # API port
env_file:
  - .llm.env           # OPENAI_API_KEY, ANTHROPIC_API_KEY, etc.
```

Static UIs:
- **Playground**: `http://localhost:11235/playground`
- **Monitor dashboard**: `http://localhost:11235/dashboard`
- **OpenAPI docs**: `http://localhost:11235/docs`
- **Metrics**: `http://localhost:11235/metrics` (if Prometheus enabled)
