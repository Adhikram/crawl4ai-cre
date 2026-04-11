import os
import json
import asyncio
from typing import List, Tuple, Dict
from functools import partial
from uuid import uuid4
from datetime import datetime, timezone
from base64 import b64encode

import logging
from typing import Optional, AsyncGenerator
from urllib.parse import unquote
from fastapi import HTTPException, Request, status
from fastapi.background import BackgroundTasks
from fastapi.responses import JSONResponse
from redis import asyncio as aioredis

from crawl4ai import (
    AsyncWebCrawler,
    CrawlerRunConfig,
    LLMExtractionStrategy,
    CacheMode,
    BrowserConfig,
    MemoryAdaptiveDispatcher,
    RateLimiter,
    LLMConfig,
    BFSDeepCrawlStrategy,
    DFSDeepCrawlStrategy,
    BestFirstCrawlingStrategy,
)
from crawl4ai.utils import perform_completion_with_backoff
from crawl4ai.content_filter_strategy import (
    PruningContentFilter,
    BM25ContentFilter,
    LLMContentFilter
)
from crawl4ai.markdown_generation_strategy import DefaultMarkdownGenerator
from crawl4ai.content_scraping_strategy import LXMLWebScrapingStrategy

from utils import (
    TaskStatus,
    FilterType,
    get_base_url,
    is_task_id,
    should_cleanup_task,
    decode_redis_hash,
    get_llm_api_key,
    validate_llm_provider,
    get_llm_temperature,
    get_llm_base_url,
    get_redis_task_ttl
)
from webhook import WebhookDeliveryService

import psutil, time

logger = logging.getLogger(__name__)

# --- Helper to get memory ---
def _get_memory_mb():
    try:
        return psutil.Process().memory_info().rss / (1024 * 1024)
    except Exception as e:
        logger.warning(f"Could not get memory info: {e}")
        return None


async def hset_with_ttl(redis, key: str, mapping: dict, config: dict):
    """Set Redis hash with automatic TTL expiry.

    Args:
        redis: Redis client instance
        key: Redis key (e.g., "task:abc123")
        mapping: Hash field-value mapping
        config: Application config containing redis.task_ttl_seconds
    """
    await redis.hset(key, mapping=mapping)
    ttl = get_redis_task_ttl(config)
    if ttl > 0:
        await redis.expire(key, ttl)


async def handle_llm_qa(
    url: str,
    query: str,
    config: dict,
    provider: Optional[str] = None,
    temperature: Optional[float] = None,
    base_url: Optional[str] = None,
) -> str:
    """Process QA using LLM with crawled content as context."""
    from crawler_pool import get_crawler, release_crawler
    crawler = None
    try:
        if not url.startswith(('http://', 'https://')) and not url.startswith(("raw:", "raw://")):
            url = 'https://' + url
        # Extract base URL by finding last '?q=' occurrence
        last_q_index = url.rfind('?q=')
        if last_q_index != -1:
            url = url[:last_q_index]

        # Get markdown content (use default config)
        from utils import load_config
        cfg = load_config()
        browser_cfg = BrowserConfig(
            extra_args=cfg["crawler"]["browser"].get("extra_args", []),
            **cfg["crawler"]["browser"].get("kwargs", {}),
        )
        crawler = await get_crawler(browser_cfg)
        result = await crawler.arun(url)
        if not result.success:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=result.error_message
            )
        content = result.markdown.fit_markdown or result.markdown.raw_markdown

        # Create prompt and get LLM response
        prompt = f"""Use the following content as context to answer the question.
    Content:
    {content}

    Question: {query}

    Answer:"""

        resolved_provider = provider or config["llm"]["provider"]
        response = perform_completion_with_backoff(
            provider=resolved_provider,
            prompt_with_variables=prompt,
            api_token=get_llm_api_key(config, resolved_provider),
            temperature=temperature or get_llm_temperature(config, resolved_provider),
            base_url=base_url or get_llm_base_url(config, resolved_provider),
            base_delay=config["llm"].get("backoff_base_delay", 2),
            max_attempts=config["llm"].get("backoff_max_attempts", 3),
            exponential_factor=config["llm"].get("backoff_exponential_factor", 2)
        )

        return response.choices[0].message.content
    except Exception as e:
        logger.error(f"QA processing error: {str(e)}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(e)
        )
    finally:
        if crawler:
            await release_crawler(crawler)

async def process_llm_extraction(
    redis: aioredis.Redis,
    config: dict,
    task_id: str,
    url: str,
    instruction: str,
    schema: Optional[str] = None,
    cache: str = "0",
    provider: Optional[str] = None,
    webhook_config: Optional[Dict] = None,
    temperature: Optional[float] = None,
    base_url: Optional[str] = None
) -> None:
    """Process LLM extraction in background."""
    # Initialize webhook service
    webhook_service = WebhookDeliveryService(config)

    try:
        # Validate provider
        is_valid, error_msg = validate_llm_provider(config, provider)
        if not is_valid:
            await hset_with_ttl(redis, f"task:{task_id}", {
                "status": TaskStatus.FAILED,
                "error": error_msg
            }, config)

            # Send webhook notification on failure
            await webhook_service.notify_job_completion(
                task_id=task_id,
                task_type="llm_extraction",
                status="failed",
                urls=[url],
                webhook_config=webhook_config,
                error=error_msg
            )
            return
        api_key = get_llm_api_key(config, provider)  # Returns None to let litellm handle it
        llm_strategy = LLMExtractionStrategy(
            llm_config=LLMConfig(
                provider=provider or config["llm"]["provider"],
                api_token=api_key,
                temperature=temperature or get_llm_temperature(config, provider),
                base_url=base_url or get_llm_base_url(config, provider)
            ),
            instruction=instruction,
            schema=json.loads(schema) if schema else None,
        )

        cache_mode = CacheMode.ENABLED if cache == "1" else CacheMode.WRITE_ONLY

        async with AsyncWebCrawler() as crawler:
            result = await crawler.arun(
                url=url,
                config=CrawlerRunConfig(
                    extraction_strategy=llm_strategy,
                    scraping_strategy=LXMLWebScrapingStrategy(),
                    cache_mode=cache_mode
                )
            )

        if not result.success:
            await hset_with_ttl(redis, f"task:{task_id}", {
                "status": TaskStatus.FAILED,
                "error": result.error_message
            }, config)

            # Send webhook notification on failure
            await webhook_service.notify_job_completion(
                task_id=task_id,
                task_type="llm_extraction",
                status="failed",
                urls=[url],
                webhook_config=webhook_config,
                error=result.error_message
            )
            return

        try:
            content = json.loads(result.extracted_content)
        except json.JSONDecodeError:
            content = result.extracted_content

        result_data = {"extracted_content": content}

        await hset_with_ttl(redis, f"task:{task_id}", {
            "status": TaskStatus.COMPLETED,
            "result": json.dumps(content)
        }, config)

        # Send webhook notification on successful completion
        await webhook_service.notify_job_completion(
            task_id=task_id,
            task_type="llm_extraction",
            status="completed",
            urls=[url],
            webhook_config=webhook_config,
            result=result_data
        )

    except Exception as e:
        logger.error(f"LLM extraction error: {str(e)}", exc_info=True)
        await hset_with_ttl(redis, f"task:{task_id}", {
            "status": TaskStatus.FAILED,
            "error": str(e)
        }, config)

        # Send webhook notification on failure
        await webhook_service.notify_job_completion(
            task_id=task_id,
            task_type="llm_extraction",
            status="failed",
            urls=[url],
            webhook_config=webhook_config,
            error=str(e)
        )

async def handle_markdown_request(
    url: str,
    filter_type: FilterType,
    query: Optional[str] = None,
    cache: str = "0",
    config: Optional[dict] = None,
    provider: Optional[str] = None,
    temperature: Optional[float] = None,
    base_url: Optional[str] = None
) -> str:
    """Handle markdown generation requests."""
    crawler = None
    try:
        # Validate provider if using LLM filter
        if filter_type == FilterType.LLM:
            is_valid, error_msg = validate_llm_provider(config, provider)
            if not is_valid:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=error_msg
                )
        decoded_url = unquote(url)
        if not decoded_url.startswith(('http://', 'https://')) and not decoded_url.startswith(("raw:", "raw://")):
            decoded_url = 'https://' + decoded_url

        if filter_type == FilterType.RAW:
            md_generator = DefaultMarkdownGenerator()
        else:
            content_filter = {
                FilterType.FIT: PruningContentFilter(),
                FilterType.BM25: BM25ContentFilter(user_query=query or ""),
                FilterType.LLM: LLMContentFilter(
                    llm_config=LLMConfig(
                        provider=provider or config["llm"]["provider"],
                        api_token=get_llm_api_key(config, provider),  # Returns None to let litellm handle it
                        temperature=temperature or get_llm_temperature(config, provider),
                        base_url=base_url or get_llm_base_url(config, provider)
                    ),
                    instruction=query or "Extract main content"
                )
            }[filter_type]
            md_generator = DefaultMarkdownGenerator(content_filter=content_filter)

        cache_mode = CacheMode.ENABLED if cache == "1" else CacheMode.WRITE_ONLY

        from crawler_pool import get_crawler, release_crawler
        from utils import load_config as _load_config
        _cfg = _load_config()
        browser_cfg = BrowserConfig(
            extra_args=_cfg["crawler"]["browser"].get("extra_args", []),
            **_cfg["crawler"]["browser"].get("kwargs", {}),
        )
        crawler = await get_crawler(browser_cfg)
        result = await crawler.arun(
            url=decoded_url,
            config=CrawlerRunConfig(
                markdown_generator=md_generator,
                scraping_strategy=LXMLWebScrapingStrategy(),
                cache_mode=cache_mode
            )
        )

        if not result.success:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=result.error_message
            )

        return (result.markdown.raw_markdown
               if filter_type == FilterType.RAW
               else result.markdown.fit_markdown)

    except Exception as e:
        logger.error(f"Markdown error: {str(e)}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(e)
        )
    finally:
        if crawler:
            await release_crawler(crawler)

async def handle_llm_request(
    redis: aioredis.Redis,
    background_tasks: BackgroundTasks,
    request: Request,
    input_path: str,
    query: Optional[str] = None,
    schema: Optional[str] = None,
    cache: str = "0",
    config: Optional[dict] = None,
    provider: Optional[str] = None,
    webhook_config: Optional[Dict] = None,
    temperature: Optional[float] = None,
    api_base_url: Optional[str] = None
) -> JSONResponse:
    """Handle LLM extraction requests."""
    base_url = get_base_url(request)
    
    try:
        if is_task_id(input_path):
            return await handle_task_status(
                redis, input_path, base_url
            )

        if not query:
            return JSONResponse({
                "message": "Please provide an instruction",
                "_links": {
                    "example": {
                        "href": f"{base_url}/llm/{input_path}?q=Extract+main+content",
                        "title": "Try this example"
                    }
                }
            })

        return await create_new_task(
            redis,
            background_tasks,
            input_path,
            query,
            schema,
            cache,
            base_url,
            config,
            provider,
            webhook_config,
            temperature,
            api_base_url
        )

    except Exception as e:
        logger.error(f"LLM endpoint error: {str(e)}", exc_info=True)
        return JSONResponse({
            "error": str(e),
            "_links": {
                "retry": {"href": str(request.url)}
            }
        }, status_code=status.HTTP_500_INTERNAL_SERVER_ERROR)

async def handle_task_status(
    redis: aioredis.Redis,
    task_id: str,
    base_url: str,
    *,
    keep: bool = False,
    config: dict = None,
) -> JSONResponse:
    """Handle task status check requests.

    Args:
        keep:   When True, never auto-delete the Redis key on read. The caller
                is responsible for deletion via the /ack endpoint after it has
                safely persisted the result. Use this for CRE jobs polled by
                external processors so a DB-write failure can be retried.
        config: Application config dict. When supplied, the configured
                redis.task_ttl_seconds is honoured for the manual cleanup
                guard (previously was hardcoded to 3600 s regardless of config).
    """
    task = await redis.hgetall(f"task:{task_id}")
    if not task:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Task not found"
        )

    task = decode_redis_hash(task)
    response = create_task_response(task, task_id, base_url)

    if task["status"] in [TaskStatus.COMPLETED, TaskStatus.FAILED]:
        if not keep:
            ttl_seconds = get_redis_task_ttl(config) if config else 3600
            if should_cleanup_task(task["created_at"], ttl_seconds=ttl_seconds):
                await redis.delete(f"task:{task_id}")

    return JSONResponse(response)

async def create_new_task(
    redis: aioredis.Redis,
    background_tasks: BackgroundTasks,
    input_path: str,
    query: str,
    schema: Optional[str],
    cache: str,
    base_url: str,
    config: dict,
    provider: Optional[str] = None,
    webhook_config: Optional[Dict] = None,
    temperature: Optional[float] = None,
    api_base_url: Optional[str] = None
) -> JSONResponse:
    """Create and initialize a new task."""
    decoded_url = unquote(input_path)
    if not decoded_url.startswith(('http://', 'https://')) and not decoded_url.startswith(("raw:", "raw://")):
        decoded_url = 'https://' + decoded_url

    from datetime import datetime
    task_id = f"llm_{int(datetime.now().timestamp())}_{id(background_tasks)}"

    task_data = {
        "status": TaskStatus.PROCESSING,
        "created_at": datetime.now().isoformat(),
        "url": decoded_url
    }

    # Store webhook config if provided
    if webhook_config:
        task_data["webhook_config"] = json.dumps(webhook_config)

    await hset_with_ttl(redis, f"task:{task_id}", task_data, config)

    background_tasks.add_task(
        process_llm_extraction,
        redis,
        config,
        task_id,
        decoded_url,
        query,
        schema,
        cache,
        provider,
        webhook_config,
        temperature,
        api_base_url
    )

    return JSONResponse({
        "task_id": task_id,
        "status": TaskStatus.PROCESSING,
        "url": decoded_url,
        "_links": {
            "self": {"href": f"{base_url}/llm/{task_id}"},
            "status": {"href": f"{base_url}/llm/{task_id}"}
        }
    })

def create_task_response(task: dict, task_id: str, base_url: str) -> dict:
    """Create response for task status check."""
    response = {
        "task_id": task_id,
        "status": task["status"],
        "created_at": task["created_at"],
        "url": task["url"],
        "_links": {
            "self": {"href": f"{base_url}/llm/{task_id}"},
            "refresh": {"href": f"{base_url}/llm/{task_id}"}
        }
    }

    if task["status"] == TaskStatus.COMPLETED:
        response["result"] = json.loads(task["result"])
    elif task["status"] == TaskStatus.FAILED:
        response["error"] = task["error"]

    return response

async def stream_results(crawler: AsyncWebCrawler, results_gen: AsyncGenerator) -> AsyncGenerator[bytes, None]:
    """Stream results with heartbeats and completion markers."""
    import json
    from utils import datetime_handler
    from crawler_pool import release_crawler

    try:
        async for result in results_gen:
            try:
                server_memory_mb = _get_memory_mb()
                result_dict = result.model_dump()
                result_dict['server_memory_mb'] = server_memory_mb
                # Ensure fit_html is JSON-serializable
                if "fit_html" in result_dict and not (result_dict["fit_html"] is None or isinstance(result_dict["fit_html"], str)):
                    result_dict["fit_html"] = None
                # If PDF exists, encode it to base64
                if result_dict.get('pdf') is not None:
                    result_dict['pdf'] = b64encode(result_dict['pdf']).decode('utf-8')
                logger.info(f"Streaming result for {result_dict.get('url', 'unknown')}")
                data = json.dumps(result_dict, default=datetime_handler) + "\n"
                yield data.encode('utf-8')
            except Exception as e:
                logger.error(f"Serialization error: {e}")
                error_response = {"error": str(e), "url": getattr(result, 'url', 'unknown')}
                yield (json.dumps(error_response) + "\n").encode('utf-8')

        yield json.dumps({"status": "completed"}).encode('utf-8')
        
    except asyncio.CancelledError:
        logger.warning("Client disconnected during streaming")
    finally:
        if crawler:
            await release_crawler(crawler)

async def handle_crawl_request(
    urls: List[str],
    browser_config: dict,
    crawler_config: dict,
    config: dict,
    hooks_config: Optional[dict] = None
) -> dict:
    """Handle non-streaming crawl requests with optional hooks."""
    # Track request start
    request_id = f"req_{uuid4().hex[:8]}"
    crawler = None
    try:
        from monitor import get_monitor
        await get_monitor().track_request_start(
            request_id, "/crawl", urls[0] if urls else "batch", browser_config
        )
    except:
        pass  # Monitor not critical

    start_mem_mb = _get_memory_mb() # <--- Get memory before
    start_time = time.time()
    mem_delta_mb = None
    peak_mem_mb = start_mem_mb
    hook_manager = None

    try:
        urls = [('https://' + url) if not url.startswith(('http://', 'https://')) and not url.startswith(("raw:", "raw://")) else url for url in urls]
        browser_config = BrowserConfig.load(browser_config)
        crawler_config = CrawlerRunConfig.load(crawler_config)

        dispatcher = MemoryAdaptiveDispatcher(
            memory_threshold_percent=config["crawler"]["memory_threshold_percent"],
            rate_limiter=RateLimiter(
                base_delay=tuple(config["crawler"]["rate_limiter"]["base_delay"])
            ) if config["crawler"]["rate_limiter"]["enabled"] else None
        )
        
        from crawler_pool import get_crawler, release_crawler
        crawler = await get_crawler(browser_config)
        
        # Attach hooks if provided
        hooks_status = {}
        if hooks_config:
            from hook_manager import attach_user_hooks_to_crawler, UserHookManager
            hook_manager = UserHookManager(timeout=hooks_config.get('timeout', 30))
            hooks_status, hook_manager = await attach_user_hooks_to_crawler(
                crawler,
                hooks_config.get('code', {}),
                timeout=hooks_config.get('timeout', 30),
                hook_manager=hook_manager
            )
            logger.info(f"Hooks attachment status: {hooks_status['status']}")
        
        base_config = config["crawler"]["base_config"]
        # Iterate on key-value pairs in global_config then use hasattr to set them
        for key, value in base_config.items():
            if hasattr(crawler_config, key):
                current_value = getattr(crawler_config, key)
                # Only set base config if user didn't provide a value
                if current_value is None or current_value == "":
                    setattr(crawler_config, key, value)

        results = []
        func = getattr(crawler, "arun" if len(urls) == 1 else "arun_many")
        partial_func = partial(func, 
                                urls[0] if len(urls) == 1 else urls, 
                                config=crawler_config, 
                                dispatcher=dispatcher)
        results = await partial_func()
        
        # Ensure results is always a list
        if not isinstance(results, list):
            results = [results]

        end_mem_mb = _get_memory_mb() # <--- Get memory after
        end_time = time.time()
        
        if start_mem_mb is not None and end_mem_mb is not None:
            mem_delta_mb = end_mem_mb - start_mem_mb # <--- Calculate delta
            peak_mem_mb = max(peak_mem_mb if peak_mem_mb else 0, end_mem_mb) # <--- Get peak memory
        logger.info(f"Memory usage: Start: {start_mem_mb} MB, End: {end_mem_mb} MB, Delta: {mem_delta_mb} MB, Peak: {peak_mem_mb} MB")

        # Process results to handle PDF bytes
        processed_results = []
        for result in results:
            try:
                # Check if result has model_dump method (is a proper CrawlResult)
                if hasattr(result, 'model_dump'):
                    result_dict = result.model_dump()
                elif isinstance(result, dict):
                    result_dict = result
                else:
                    # Handle unexpected result type
                    logger.warning(f"Unexpected result type: {type(result)}")
                    result_dict = {
                        "url": str(result) if hasattr(result, '__str__') else "unknown",
                        "success": False,
                        "error_message": f"Unexpected result type: {type(result).__name__}"
                    }
                
                # if fit_html is not a string, set it to None to avoid serialization errors
                if "fit_html" in result_dict and not (result_dict["fit_html"] is None or isinstance(result_dict["fit_html"], str)):
                    result_dict["fit_html"] = None
                    
                # If PDF exists, encode it to base64
                if result_dict.get('pdf') is not None and isinstance(result_dict.get('pdf'), bytes):
                    result_dict['pdf'] = b64encode(result_dict['pdf']).decode('utf-8')
                    
                processed_results.append(result_dict)
            except Exception as e:
                logger.error(f"Error processing result: {e}")
                processed_results.append({
                    "url": "unknown",
                    "success": False,
                    "error_message": str(e)
                })
            
        response = {
            "success": True,
            "results": processed_results,
            "server_processing_time_s": end_time - start_time,
            "server_memory_delta_mb": mem_delta_mb,
            "server_peak_memory_mb": peak_mem_mb
        }

        # Track request completion
        try:
            from monitor import get_monitor
            await get_monitor().track_request_end(
                request_id, success=True, pool_hit=True, status_code=200
            )
        except:
            pass

        # Add hooks information if hooks were used
        if hooks_config and hook_manager:
            from hook_manager import UserHookManager
            if isinstance(hook_manager, UserHookManager):
                try:
                    # Ensure all hook data is JSON serializable
                    hook_data = {
                        "status": hooks_status,
                        "execution_log": hook_manager.execution_log,
                        "errors": hook_manager.errors,
                        "summary": hook_manager.get_summary()
                    }
                    # Test that it's serializable
                    json.dumps(hook_data)
                    response["hooks"] = hook_data
                except (TypeError, ValueError) as e:
                    logger.error(f"Hook data not JSON serializable: {e}")
                    response["hooks"] = {
                        "status": {"status": "error", "message": "Hook data serialization failed"},
                        "execution_log": [],
                        "errors": [{"error": str(e)}],
                        "summary": {}
                    }
        
        return response

    except Exception as e:
        logger.error(f"Crawl error: {str(e)}", exc_info=True)

        # Track request error
        try:
            from monitor import get_monitor
            await get_monitor().track_request_end(
                request_id, success=False, error=str(e), status_code=500
            )
        except:
            pass

        # Measure memory even on error if possible
        end_mem_mb_error = _get_memory_mb()
        if start_mem_mb is not None and end_mem_mb_error is not None:
            mem_delta_mb = end_mem_mb_error - start_mem_mb

        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=json.dumps({ # Send structured error
                "error": str(e),
                "server_memory_delta_mb": mem_delta_mb,
                "server_peak_memory_mb": max(peak_mem_mb if peak_mem_mb else 0, end_mem_mb_error or 0)
            })
        )
    finally:
        if crawler:
            await release_crawler(crawler)

async def handle_stream_crawl_request(
    urls: List[str],
    browser_config: dict,
    crawler_config: dict,
    config: dict,
    hooks_config: Optional[dict] = None
) -> Tuple[AsyncWebCrawler, AsyncGenerator, Optional[Dict]]:
    """Handle streaming crawl requests with optional hooks."""
    hooks_info = None
    crawler = None
    try:
        browser_config = BrowserConfig.load(browser_config)
        # browser_config.verbose = True # Set to False or remove for production stress testing
        browser_config.verbose = False
        crawler_config = CrawlerRunConfig.load(crawler_config)
        crawler_config.scraping_strategy = LXMLWebScrapingStrategy()
        crawler_config.stream = True

        # Deep crawl streaming supports exactly one start URL
        if crawler_config.deep_crawl_strategy is not None and len(urls) != 1:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    "Deep crawling with stream currently supports exactly one URL per request. "
                    f"Received {len(urls)} URLs."
                ),
            )

        from crawler_pool import get_crawler, release_crawler
        crawler = await get_crawler(browser_config)

        # Attach hooks if provided
        if hooks_config:
            from hook_manager import attach_user_hooks_to_crawler, UserHookManager
            hook_manager = UserHookManager(timeout=hooks_config.get('timeout', 30))
            hooks_status, hook_manager = await attach_user_hooks_to_crawler(
                crawler,
                hooks_config.get('code', {}),
                timeout=hooks_config.get('timeout', 30),
                hook_manager=hook_manager
            )
            logger.info(f"Hooks attachment status for streaming: {hooks_status['status']}")
            # Include hook manager in hooks_info for proper tracking
            hooks_info = {'status': hooks_status, 'manager': hook_manager}

        # Deep crawl with single URL: use arun() which returns an async generator
        # mirroring the Python library's streaming behavior
        if crawler_config.deep_crawl_strategy is not None and len(urls) == 1:
            results_gen = await crawler.arun(
                urls[0],
                config=crawler_config,
            )
        else:
            # Default multi-URL streaming via arun_many
            dispatcher = MemoryAdaptiveDispatcher(
                memory_threshold_percent=config["crawler"]["memory_threshold_percent"],
                rate_limiter=RateLimiter(
                    base_delay=tuple(config["crawler"]["rate_limiter"]["base_delay"])
                )
            )
            results_gen = await crawler.arun_many(
                urls=urls,
                config=crawler_config,
                dispatcher=dispatcher
            )

        return crawler, results_gen, hooks_info

    except Exception as e:
        # Release crawler on setup error (for successful streams,
        # release happens in stream_results finally block)
        if crawler:
            await release_crawler(crawler)
        logger.error(f"Stream crawl error: {str(e)}", exc_info=True)
        # Raising HTTPException here will prevent streaming response
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(e)
        )
        
async def handle_crawl_job(
    redis,
    background_tasks: BackgroundTasks,
    urls: List[str],
    browser_config: Dict,
    crawler_config: Dict,
    config: Dict,
    webhook_config: Optional[Dict] = None,
) -> Dict:
    """
    Fire-and-forget version of handle_crawl_request.
    Creates a task in Redis, runs the heavy work in a background task,
    lets /crawl/job/{task_id} polling fetch the result.
    """
    task_id = f"crawl_{uuid4().hex[:8]}"

    # Store task data in Redis
    task_data = {
        "status": TaskStatus.PROCESSING,         # <-- keep enum values consistent
        "created_at": datetime.now(timezone.utc).replace(tzinfo=None).isoformat(),
        "url": json.dumps(urls),                 # store list as JSON string
        "result": "",
        "error": "",
    }

    # Store webhook config if provided
    if webhook_config:
        task_data["webhook_config"] = json.dumps(webhook_config)

    await hset_with_ttl(redis, f"task:{task_id}", task_data, config)

    # Initialize webhook service
    webhook_service = WebhookDeliveryService(config)

    async def _runner():
        try:
            result = await handle_crawl_request(
                urls=urls,
                browser_config=browser_config,
                crawler_config=crawler_config,
                config=config,
            )
            await hset_with_ttl(redis, f"task:{task_id}", {
                "status": TaskStatus.COMPLETED,
                "result": json.dumps(result),
            }, config)

            # Send webhook notification on successful completion
            await webhook_service.notify_job_completion(
                task_id=task_id,
                task_type="crawl",
                status="completed",
                urls=urls,
                webhook_config=webhook_config,
                result=result
            )

            await asyncio.sleep(5)  # Give Redis time to process the update
        except Exception as exc:
            await hset_with_ttl(redis, f"task:{task_id}", {
                "status": TaskStatus.FAILED,
                "error": str(exc),
            }, config)

            # Send webhook notification on failure
            await webhook_service.notify_job_completion(
                task_id=task_id,
                task_type="crawl",
                status="failed",
                urls=urls,
                webhook_config=webhook_config,
                error=str(exc)
            )

    background_tasks.add_task(_runner)
    return {"task_id": task_id}


async def handle_list_active_cre_jobs(redis: aioredis.Redis) -> dict:
    """Scan Redis for all CRE crawl tasks and return their status + url.

    Scans for keys matching ``task:cre_*`` using SCAN (non-blocking, cursor-
    based).  Returns every task regardless of status so the caller can decide
    which ones to act on (processing / completed / failed).

    Returns:
        {"jobs": [{"task_id": str, "status": str, "url": str,
                   "created_at": str, "error": str}], "total": int}
    """
    jobs: list[dict] = []
    cursor = 0
    while True:
        cursor, keys = await redis.scan(cursor, match="task:cre_*", count=200)
        for raw_key in keys:
            key_str: str = raw_key.decode("utf-8") if isinstance(raw_key, bytes) else raw_key
            task_id = key_str.removeprefix("task:")
            raw_task = await redis.hgetall(raw_key)
            if not raw_task:
                continue
            task = decode_redis_hash(raw_task)
            jobs.append({
                "task_id": task_id,
                "status": task.get("status", "unknown"),
                "url": task.get("url", ""),
                "created_at": task.get("created_at", ""),
                "error": task.get("error", ""),
            })
        if cursor == 0:
            break
    return {"jobs": jobs, "total": len(jobs)}


async def handle_list_completed_cre_jobs(
    redis: aioredis.Redis,
    limit: int = 500,
) -> dict:
    """Return lightweight metadata for *completed* CRE jobs only.

    Difference from ``handle_list_active_cre_jobs``:

    * **Status filter** — returns only ``status=completed`` entries.
    * **No result blob** — fetches only the metadata fields (status, url,
      created_at) via ``HMGET`` so the heavy ``result`` field is never
      deserialised or transmitted.  This keeps the response small even when
      hundreds of 4-MB result blobs sit in Redis.
    * **Limit** — stops scanning once ``limit`` completed entries are found.

    Intended use: the external poll processor calls this first to get a cheap
    list of task IDs that are ready, then fetches each result individually via
    ``GET /crawl/cre/job/{task_id}`` only for IDs it hasn't yet persisted.

    Returns:
        {"jobs": [{"task_id": str, "status": "completed", "url": str,
                   "created_at": str}], "total": int}
    """
    jobs: list[dict] = []
    cursor = 0
    while True:
        cursor, keys = await redis.scan(cursor, match="task:cre_*", count=200)
        for raw_key in keys:
            if len(jobs) >= limit:
                break
            key_str: str = raw_key.decode("utf-8") if isinstance(raw_key, bytes) else raw_key
            task_id = key_str.removeprefix("task:")

            # Fetch only lightweight fields — deliberately skip "result"
            fields = await redis.hmget(raw_key, "status", "url", "created_at")
            status_b, url_b, created_at_b = fields

            if not status_b:
                continue
            status_str: str = status_b.decode("utf-8") if isinstance(status_b, bytes) else status_b

            if status_str != TaskStatus.COMPLETED:
                continue

            jobs.append({
                "task_id": task_id,
                "status": status_str,
                "url": url_b.decode("utf-8") if url_b else "",
                "created_at": created_at_b.decode("utf-8") if created_at_b else "",
            })

        if cursor == 0 or len(jobs) >= limit:
            break

    return {"jobs": jobs, "total": len(jobs)}


# ─────────────────────────────────────────────────────────────
#  CRE deep-crawl helpers
# ─────────────────────────────────────────────────────────────

def _build_cre_configs(
    url: str,
    strategy: str,
    max_pages: int,
    max_depth: int,
    include_news: bool,
    stream: bool = False,
):
    """Return (browser_cfg, crawler_cfg) with all CRE defaults applied."""
    from urllib.parse import urlparse as _up
    from crawl4ai.deep_crawling.cre_filters import build_cre_filter_chain
    from crawl4ai.deep_crawling.cre_scorers import build_cre_composite_scorer

    base_domain = _up(url).hostname or url
    filter_chain = build_cre_filter_chain(base_domain=base_domain, allow_news=include_news)
    scorer = build_cre_composite_scorer()

    strategy_kwargs = dict(
        max_depth=max_depth,
        max_pages=max_pages,
        filter_chain=filter_chain,
        url_scorer=scorer,
    )
    if strategy == "bfs":
        deep_crawl_strategy = BFSDeepCrawlStrategy(**strategy_kwargs)
    elif strategy == "best-first":
        deep_crawl_strategy = BestFirstCrawlingStrategy(**strategy_kwargs)
    else:
        deep_crawl_strategy = DFSDeepCrawlStrategy(**strategy_kwargs)

    browser_cfg = BrowserConfig(
        user_agent_mode="random",
        enable_stealth=True,
        extra_args=["--enable-cookies", "--disable-cookie-encryption"],
    )
    crawler_cfg = CrawlerRunConfig(
        deep_crawl_strategy=deep_crawl_strategy,
        scraping_strategy=LXMLWebScrapingStrategy(),
        simulate_user=True,
        wait_until="domcontentloaded",
        page_timeout=90_000,
        stream=stream,
    )
    return browser_cfg, crawler_cfg


def _project_cre_result(r, seed_url: str) -> dict:
    """
    Thin wrapper around :func:`~crawl4ai.deep_crawling.cre_filters.project_cre_result`.

    Delegates to the canonical implementation in ``cre_filters`` so the API
    and CLI always produce identical output.
    """
    from crawl4ai.deep_crawling.cre_filters import project_cre_result
    return project_cre_result(r, seed_url=seed_url)


async def handle_cre_crawl_request(
    url: str,
    strategy: str = "dfs",
    max_pages: int = 500,
    max_depth: int = 10,
    include_news: bool = False,
    no_html: bool = True,
) -> dict:
    """
    Synchronous CRE deep-crawl — returns all results at once.

    Each result in ``results`` now includes:

    * ``crawl_depth``  — number of URL path segments (proxy for link depth).
    * ``rank_factors`` — full CRE relevance breakdown::

        {
          # Base enhanced-ranking scores (mirrors enhanced-page-ranking.ts)
          "investment_criteria_field_score": ...,
          "financial_data_density_score":    ...,
          "business_context_score":          ...,
          ...
          "base_total_score":                ...,

          # Detailed breakdowns
          "investment_criteria_breakdown":   {...},
          "financial_data_breakdown":        {...},
          "business_context_breakdown":      {...},
          "matched_keywords":                {...},
          "url_analysis":                    {...},

          # CRE-specific ranking layer (mirrors CREPageRankingService.ts)
          "cre_ranking": {
            "cre_keyword_score":         ...,
            "cre_content_type_score":    ...,
            "cre_investment_info_score": ...,
            "cre_property_focus_score":  ...,
            "cre_financial_terms_score": ...,
            "non_cre_penalty":           ...,
            "cre_page_type":             "loan_program" | "investment_criteria" | ...,
            "cre_keywords_found":        [...],
            "cre_content_indicators":    [...],
          },

          # Combined CRE-weighted total (mirrors rankPagesForCompany() total_score)
          "total_score": ...,
        }
    """
    from crawler_pool import get_crawler, release_crawler

    browser_cfg, crawler_cfg = _build_cre_configs(
        url, strategy, max_pages, max_depth, include_news
    )

    start_time = time.time()
    start_mem_mb = _get_memory_mb()
    crawler = None
    try:
        crawler = await get_crawler(browser_cfg)
        results = await crawler.arun(url, config=crawler_cfg)
        if not isinstance(results, list):
            results = [results]

        end_mem_mb = _get_memory_mb()
        mem_delta_mb = (
            (end_mem_mb - start_mem_mb)
            if start_mem_mb is not None and end_mem_mb is not None
            else None
        )

        processed = [_project_cre_result(r, url) for r in results]

        return {
            "success": True,
            "results": processed,
            "total_pages": len(processed),
            "server_processing_time_s": time.time() - start_time,
            "server_memory_delta_mb": mem_delta_mb,
        }
    except Exception as e:
        logger.error(f"CRE crawl error: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(e),
        )
    finally:
        if crawler:
            await release_crawler(crawler)


async def handle_cre_stream_crawl_request(
    url: str,
    strategy: str = "dfs",
    max_pages: int = 500,
    max_depth: int = 10,
    include_news: bool = False,
):
    """Set up a streaming CRE deep-crawl. Returns (crawler, async_generator)."""
    from crawler_pool import get_crawler, release_crawler

    browser_cfg, crawler_cfg = _build_cre_configs(
        url, strategy, max_pages, max_depth, include_news, stream=True
    )
    crawler = None
    try:
        crawler = await get_crawler(browser_cfg)
        results_gen = await crawler.arun(url, config=crawler_cfg)
        return crawler, results_gen
    except Exception as e:
        if crawler:
            await release_crawler(crawler)
        logger.error(f"CRE stream crawl error: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(e),
        )


async def handle_cre_crawl_job(
    redis,
    background_tasks,
    url: str,
    strategy: str = "dfs",
    max_pages: int = 500,
    max_depth: int = 10,
    include_news: bool = False,
    no_html: bool = True,
    config: dict = None,
    webhook_config: Optional[Dict] = None,
) -> dict:
    """Fire-and-forget CRE deep-crawl job stored in Redis."""
    task_id = f"cre_{uuid4().hex[:8]}"
    task_data = {
        "status": TaskStatus.PROCESSING,
        "created_at": datetime.now(timezone.utc).replace(tzinfo=None).isoformat(),
        "url": url,
        "result": "",
        "error": "",
    }
    if webhook_config:
        task_data["webhook_config"] = json.dumps(webhook_config)

    await hset_with_ttl(redis, f"task:{task_id}", task_data, config)
    webhook_service = WebhookDeliveryService(config)

    async def _runner():
        try:
            result = await handle_cre_crawl_request(
                url=url,
                strategy=strategy,
                max_pages=max_pages,
                max_depth=max_depth,
                include_news=include_news,
                no_html=no_html,
            )
            await hset_with_ttl(redis, f"task:{task_id}", {
                "status": TaskStatus.COMPLETED,
                "result": json.dumps(result),
            }, config)
            await webhook_service.notify_job_completion(
                task_id=task_id,
                task_type="cre_crawl",
                status="completed",
                urls=[url],
                webhook_config=webhook_config,
                result=result,
            )
            await asyncio.sleep(5)
        except Exception as exc:
            await hset_with_ttl(redis, f"task:{task_id}", {
                "status": TaskStatus.FAILED,
                "error": str(exc),
            }, config)
            await webhook_service.notify_job_completion(
                task_id=task_id,
                task_type="cre_crawl",
                status="failed",
                urls=[url],
                webhook_config=webhook_config,
                error=str(exc),
            )

    background_tasks.add_task(_runner)
    return {"task_id": task_id}