"""
Job endpoints (enqueue + poll) for long-running LL​M extraction and raw crawl.
Relies on the existing Redis task helpers in api.py
"""

from typing import Dict, Optional, Callable
from fastapi import APIRouter, BackgroundTasks, Depends, Request
from fastapi.security import HTTPAuthorizationCredentials
from pydantic import BaseModel, HttpUrl

from api import (
    handle_llm_request,
    handle_crawl_job,
    handle_cre_crawl_job,
    handle_task_status,
    handle_list_active_cre_jobs,
    handle_list_completed_cre_jobs,
)
from auth import security
from schemas import WebhookConfig, CRECrawlRequest

# ------------- dependency placeholders -------------
_redis = None        # will be injected from server.py
_config = None
_token_dep: Callable = lambda credentials=None: None  # dummy until injected

# public router
router = APIRouter()


async def _late_token(credentials: HTTPAuthorizationCredentials = Depends(security)):
    """Late-bound token dependency.

    FastAPI resolves the HTTPBearer credentials first, then passes them to
    whichever _token_dep was injected at startup (jwt_required or no-op).
    This avoids calling _token_dep() inside a bare lambda, which would bypass
    FastAPI's DI and hand jwt_required a Depends object instead of real creds.
    """
    return _token_dep(credentials)


# === init hook called by server.py =========================================
def init_job_router(redis, config, token_dep) -> APIRouter:
    """Inject shared singletons and return the router for mounting."""
    global _redis, _config, _token_dep
    _redis, _config, _token_dep = redis, config, token_dep
    return router


# ---------- payload models --------------------------------------------------
class LlmJobPayload(BaseModel):
    url:    HttpUrl
    q:      str
    schema: Optional[str] = None
    cache:  bool = False
    provider: Optional[str] = None
    webhook_config: Optional[WebhookConfig] = None
    temperature: Optional[float] = None
    base_url: Optional[str] = None


class CrawlJobPayload(BaseModel):
    urls:           list[HttpUrl]
    browser_config: Dict = {}
    crawler_config: Dict = {}
    webhook_config: Optional[WebhookConfig] = None


# ---------- LL​M job ---------------------------------------------------------
@router.post("/llm/job", status_code=202)
async def llm_job_enqueue(
        payload: LlmJobPayload,
        background_tasks: BackgroundTasks,
        request: Request,
        _td: Dict = Depends(_late_token),
):
    webhook_config = None
    if payload.webhook_config:
        webhook_config = payload.webhook_config.model_dump(mode='json')

    return await handle_llm_request(
        _redis,
        background_tasks,
        request,
        str(payload.url),
        query=payload.q,
        schema=payload.schema,
        cache=payload.cache,
        config=_config,
        provider=payload.provider,
        webhook_config=webhook_config,
        temperature=payload.temperature,
        api_base_url=payload.base_url,
    )


@router.get("/llm/job/{task_id}")
async def llm_job_status(
    request: Request,
    task_id: str,
    _td: Dict = Depends(_late_token),
):
    return await handle_task_status(_redis, task_id, base_url=str(request.base_url))


# ---------- CRAWL job -------------------------------------------------------
@router.post("/crawl/job", status_code=202)
async def crawl_job_enqueue(
        payload: CrawlJobPayload,
        background_tasks: BackgroundTasks,
        _td: Dict = Depends(_late_token),
):
    webhook_config = None
    if payload.webhook_config:
        webhook_config = payload.webhook_config.model_dump(mode='json')

    return await handle_crawl_job(
        _redis,
        background_tasks,
        [str(u) for u in payload.urls],
        payload.browser_config,
        payload.crawler_config,
        config=_config,
        webhook_config=webhook_config,
    )


@router.get("/crawl/job/{task_id}")
async def crawl_job_status(
    request: Request,
    task_id: str,
    _td: Dict = Depends(_late_token),
):
    return await handle_task_status(_redis, task_id, base_url=str(request.base_url))


# ---------- CRE deep-crawl job -----------------------------------------------

@router.post("/crawl/cre/job", status_code=202)
async def cre_crawl_job_enqueue(
    payload: CRECrawlRequest,
    background_tasks: BackgroundTasks,
    _td: Dict = Depends(_late_token),
):
    """
    Submit a CRE deep-crawl as a background job.

    Returns a task_id immediately. Poll GET /crawl/cre/job/{task_id} for status,
    or supply webhook_config to receive a POST notification on completion.
    """
    webhook_config = None
    if payload.webhook_config:
        webhook_config = payload.webhook_config.model_dump(mode="json")

    return await handle_cre_crawl_job(
        redis=_redis,
        background_tasks=background_tasks,
        url=str(payload.url),
        strategy=payload.strategy,
        max_pages=payload.max_pages,
        max_depth=payload.max_depth,
        include_news=payload.include_news,
        no_html=payload.no_html,
        config=_config,
        webhook_config=webhook_config,
    )


@router.get("/crawl/cre/job/{task_id}")
async def cre_crawl_job_status(
    request: Request,
    task_id: str,
    _td: Dict = Depends(_late_token),
):
    """Poll the status/result of a CRE deep-crawl job.

    This endpoint never auto-deletes the Redis key on read (keep=True).
    The caller must explicitly acknowledge the job via
    DELETE /crawl/cre/job/{task_id}/ack once it has safely persisted the
    result.  This prevents silent data loss when a DB write fails between
    the poll and the ack.
    """
    return await handle_task_status(
        _redis,
        task_id,
        base_url=str(request.base_url),
        keep=True,
        config=_config,
    )


@router.delete("/crawl/cre/job/{task_id}/ack", status_code=200)
async def cre_job_acknowledge(
    task_id: str,
    _td: Dict = Depends(_late_token),
):
    """Acknowledge a completed CRE job — removes it from Redis.

    Call this AFTER you have successfully written the job result to your own
    database.  Safe to call multiple times: returns ``acknowledged=false`` if
    the key no longer exists (already expired or double-acked).

    Workflow::

        task_id = submitCREJob(url)
        # ...later, in poll cycle...
        result  = GET /crawl/cre/job/{task_id}     # keep=True, never deletes
        writeToDatabase(result)                      # your persistence layer
        DELETE /crawl/cre/job/{task_id}/ack          # explicit delete only on success

    Response:
        {"acknowledged": bool, "task_id": str}
    """
    key = f"task:{task_id}"
    deleted = await _redis.delete(key)
    return {"acknowledged": bool(deleted), "task_id": task_id}


@router.get("/crawl/cre/jobs/active")
async def cre_crawl_jobs_active(
    _td: Dict = Depends(_late_token),
):
    """List all CRE crawl tasks in Redis regardless of status.

    Returns every task:cre_* key (processing / completed / failed).
    Use for reconciliation — not for normal polling (prefer /jobs/completed).

    Response:
        {"jobs": [{"task_id", "status", "url", "created_at", "error"}],
         "total": int}
    """
    return await handle_list_active_cre_jobs(_redis)


@router.get("/crawl/cre/jobs/completed")
async def cre_crawl_jobs_completed(
    limit: int = 500,
    _td: Dict = Depends(_late_token),
):
    """Return lightweight metadata for completed CRE jobs only.

    Unlike /jobs/active this endpoint:

    * Filters to ``status=completed`` entries only.
    * Does **not** include the result payload — just task_id, url, created_at.
    * Supports a ``limit`` query parameter (default 500).

    Intended workflow for external poll processors::

        1. GET /crawl/cre/jobs/completed          # cheap: no result blobs
        2. Match returned task_ids against your DB  # find which ones YOU submitted
        3. For each unprocessed task_id:
             GET /crawl/cre/job/{task_id}          # fetch full result on-demand
             persist result to your DB
             DELETE /crawl/cre/job/{task_id}/ack   # explicit ack

    This pattern keeps individual responses small and makes every step
    independently retryable.

    Response:
        {"jobs": [{"task_id", "status", "url", "created_at"}], "total": int}
    """
    return await handle_list_completed_cre_jobs(_redis, limit=limit)
