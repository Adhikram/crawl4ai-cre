"""
Quick standalone test for PDF URL crawling.
Run from the project root with the .venv active:
    python test_pdf.py
"""
import asyncio
import json
from crawl4ai import AsyncWebCrawler, BrowserConfig, CrawlerRunConfig
from crawl4ai.async_configs import CacheMode
from crawl4ai.deep_crawling.cre_filters import is_bot_challenge_response, retry_if_bot_challenge

PDF_URLS = [
    "https://3650capital.com/wp-content/uploads/pdf/3650_OnePager_RECS.pdf",
    "https://3650capital.com/wp-content/uploads/2024/02/3650_OnePager_SCF_Commercial_Multifamily_Feb-2024.pdf",
    "https://3650capital.com/wp-content/uploads/pdf/3650_OnePager_SSIS.pdf",
]

SEED_URL = "https://3650capital.com/"  # Visit first to establish session cookies


async def main():
    # Match the anti-bot settings used by the deep crawl CLI
    browser_cfg = BrowserConfig(
        verbose=False,
        headless=True,
        enable_stealth=True,
        user_agent_mode="random",
    )
    run_cfg = CrawlerRunConfig(
        cache_mode=CacheMode.BYPASS,
        verbose=True,
        simulate_user=True,
        page_timeout=90_000,
    )

    async with AsyncWebCrawler(config=browser_cfg) as crawler:
        # Warm the cookie jar.  Use the same retry logic as the deep crawl so
        # the sgcaptcha JS challenge has time to complete and set its cookie.
        print(f"Warming cookies via {SEED_URL} ...")
        seed = await crawler.arun(SEED_URL, config=run_cfg)
        if is_bot_challenge_response(seed):
            print(f"  challenged (status={seed.status_code}) — retrying with delays...")
            seed = await retry_if_bot_challenge(
                seed, SEED_URL, crawler, run_cfg,
                retry_delays=(2.0, 5.0, 10.0),
            )
        print(f"  seed success={seed.success}  status={seed.status_code}\n")

        results = []
        for url in PDF_URLS:
            print(f"\n{'='*60}")
            print(f"Testing: {url.split('/')[-1]}")
            result = await crawler.arun(url, config=run_cfg)
            # If still challenged, retry with delays (same as deep crawl strategy)
            if is_bot_challenge_response(result):
                print(f"  challenged — retrying...")
                result = await retry_if_bot_challenge(
                    result, url, crawler, run_cfg,
                    retry_delays=(2.0, 5.0, 10.0),
                )
            md = result.markdown.raw_markdown if result.markdown else ""
            entry = {
                "url": url,
                "success": result.success,
                "status_code": result.status_code,
                "html_len": len(result.html or ""),
                "markdown_len": len(md),
                "markdown_preview": md[:500] if md else "",
                "error": result.error_message,
            }
            results.append(entry)
            print(f"  success       : {result.success}")
            print(f"  status_code   : {result.status_code}")
            print(f"  html_len      : {len(result.html or '')}")
            print(f"  markdown_len  : {len(md)}")
            if md:
                print(f"  preview       :\n{md[:400]}")
            else:
                print(f"  error         : {result.error_message}")

        out = "data/test_pdf_results.json"
        with open(out, "w") as f:
            json.dump(results, f, indent=2, ensure_ascii=False)
        print(f"\n\nResults written to {out}")


if __name__ == "__main__":
    asyncio.run(main())
