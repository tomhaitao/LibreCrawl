import asyncio
import os
import sys
import time
import logging
import sqlite3
import argparse
from typing import Optional
from urllib.parse import urlparse

import aiohttp
import yaml


CONFIG_PATH = "config.yaml"
OUTPUT_DIR = "md"
DB_PATH = "users.db"
LOG_DIR = "logs"

# Module-level logger; same风格 as batch_universities.py
logger = logging.getLogger(__name__)

# Base URL for Jina reader API; the target URL is appended directly
# 例如最终请求: https://r.jina.ai/https://www.example.com
# JINA_BASE_URL = "https://r.jina.ai/"
JINA_BASE_URL = "http://154.201.73.16:3000/"

# Headers required by the user
JINA_HEADERS = {
    "X-Remove-Selector": "header, .class, #id",
    "X-Retain-Images": "none",
    "X-Engine": "browser",
}


def load_universities(config_path: str = CONFIG_PATH):
    """
    从 config.yaml 读取院校配置，返回原始配置项列表（字符串或包含 domain 的 dict）。
    """
    if not os.path.exists(config_path):
        logger.error("Config file not found: %s", config_path)
        return []

    with open(config_path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}

    universities = data.get("universities")
    if not isinstance(universities, list):
        return []

    result = []
    for item in universities:
        if isinstance(item, str):
            value = item.strip()
        elif isinstance(item, dict) and "domain" in item:
            value = str(item["domain"]).strip()
        else:
            continue

        if value:
            result.append(value)

    return result


def normalize_url(entry: str) -> Optional[str]:
    """
    Normalize a config entry into a full URL.

    - If it's already http/https, keep it.
    - Otherwise, treat it as a bare domain and prefix with https://
    """
    if not entry:
        return None

    entry = str(entry).strip()
    if not entry:
        return None

    if entry.startswith(("http://", "https://")):
        return entry

    return "https://" + entry


def entry_to_base_domain(entry: str) -> Optional[str]:
    """将 config 中的一项（域名或 URL）转换成 crawler 使用的 base_domain（即 netloc）。"""
    url = normalize_url(entry)
    if not url:
        return None
    parsed = urlparse(url)
    return parsed.netloc or None


def url_to_filename(url: str) -> str:
    """Convert a URL into a safe filename (ending with .md)."""
    # 确保带协议，方便 urlparse 正确解析
    if not url.startswith(("http://", "https://")):
        url = "https://" + url.lstrip("/")

    parsed = urlparse(url)
    netloc = parsed.netloc or "site"
    path = parsed.path.strip("/")

    if path:
        base = f"{netloc}_{path}"
    else:
        base = netloc

    safe_chars = "-_.()abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
    sanitized = "".join(c if c in safe_chars else "_" for c in base)
    return sanitized + ".md"


async def fetch_markdown_for_url(
    url: str,
    session: aiohttp.ClientSession,
    retries: int = 5,
    backoff: float = 1.5,
) -> str:
    """Call Jina to fetch the given URL and return the markdown text."""
    # Jina 的用法是: BASE + 原始 URL，形成 /https://www.example.com 这样的路径
    jina_url = JINA_BASE_URL + url
    last_error: Optional[Exception] = None

    for attempt in range(1, retries + 1):
        try:
            async with session.get(jina_url, headers=JINA_HEADERS) as resp:
                if resp.status == 429:
                    # Respect Retry-After if present; otherwise exponential-ish backoff
                    retry_after_header = resp.headers.get("Retry-After")
                    try:
                        retry_after = float(retry_after_header) if retry_after_header else None
                    except ValueError:
                        retry_after = None

                    delay = retry_after if retry_after is not None else min(30, backoff**attempt)
                    logger.warning(
                        "429 Too Many Requests for %s (attempt %s/%s); retrying in %.1fs",
                        url,
                        attempt,
                        retries,
                        delay,
                    )
                    await asyncio.sleep(delay)
                    last_error = aiohttp.ClientResponseError(
                        resp.request_info, resp.history, status=429, message="Too Many Requests"
                    )
                    continue

                resp.raise_for_status()
                return await resp.text()

        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            last_error = e
            if attempt >= retries:
                break

            delay = min(30, backoff**attempt)
            logger.warning(
                "Request failed for %s (attempt %s/%s): %s; retrying in %.1fs",
                url,
                attempt,
                retries,
                e,
                delay,
            )
            await asyncio.sleep(delay)

    # Exhausted retries
    if last_error:
        raise last_error
    raise RuntimeError(f"Failed to fetch {url} after {retries} attempts")


def load_urls_from_db(base_domains: list[str]) -> list[str]:
    """
    从 users.db 中读取这些 base_domain 对应的所有已爬取 URL（只取内部链接），去重后返回。
    """
    if not base_domains:
        return []

    if not os.path.exists(DB_PATH):
        logger.error("Database file not found: %s", DB_PATH)
        return []

    placeholders = ",".join("?" for _ in base_domains)
    query = f"""
        SELECT DISTINCT cu.url
        FROM crawled_urls AS cu
        JOIN crawls AS c ON cu.crawl_id = c.id
        WHERE c.base_domain IN ({placeholders})
    """

    urls: list[str] = []
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        cur.execute(query, base_domains)
        for row in cur.fetchall():
            url = row["url"]
            if url:
                urls.append(url)
    except Exception as e:
        logger.error("Error loading URLs from database: %s", e)
        return []
    finally:
        try:
            conn.close()
        except Exception:
            pass

    return urls


async def process_single_url(
    idx: int,
    total: int,
    url: str,
    session: aiohttp.ClientSession,
    semaphore: asyncio.Semaphore,
) -> None:
    """Fetch one URL and write markdown to disk (async)."""
    filename = url_to_filename(url)
    out_path = os.path.join(OUTPUT_DIR, filename)

    if os.path.exists(out_path):
        logger.info("[%s/%s] Skip existing file %s", idx, total, out_path)
        return

    async with semaphore:
        try:
            logger.info("[%s/%s] Fetching %s -> %s", idx, total, url, out_path)
            markdown = await fetch_markdown_for_url(url, session)
            with open(out_path, "w", encoding="utf-8") as f:
                f.write(markdown)
        except Exception as e:
            logger.error("Error processing %s (idx=%s): %s", url, idx, e)


async def main(start_index: int = 1, max_workers: int = 5):
    # 1. 读取 config 中配置的院校根域名 / URL
    universities = load_universities(CONFIG_PATH)
    if not universities:
        logger.warning("No universities found in config.yaml")
        return

    # 2. 转成 crawler 使用的 base_domain 列表，用来匹配 crawls.base_domain
    base_domains = []
    for entry in universities:
        base_domain = entry_to_base_domain(entry)
        if base_domain and base_domain not in base_domains:
            base_domains.append(base_domain)

    if not base_domains:
        logger.warning("No valid base domains could be extracted from config.yaml")
        return

    logger.info("Base domains from config:")
    for d in base_domains:
        logger.info("  - %s", d)

    # 3. 从 users.db 的 crawls / crawled_urls 里，把这些 base_domain 已经爬取到的所有 URL 读出来
    urls = load_urls_from_db(base_domains)
    if not urls:
        logger.warning("No crawled URLs found in users.db for the configured domains")
        return

    logger.info("Total crawled URLs to export as markdown: %s", len(urls))

    # 4. 逐个 URL 调用 Jina 转成 markdown，保存到 md 目录
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    total = len(urls)

    if max_workers < 1:
        logger.warning("Invalid max_workers %s, reset to 1", max_workers)
        max_workers = 1

    if start_index < 1:
        logger.warning("Invalid start_index %s, reset to 1", start_index)
        start_index = 1

    if start_index > total:
        logger.warning(
            "start_index (%s) is greater than total URLs (%s); nothing to do",
            start_index,
            total,
        )
        return

    if start_index > 1:
        logger.info(
            "Skipping URLs with index < %s based on start_index", start_index
        )

    targets = [(idx, url) for idx, url in enumerate(urls, start=1) if idx >= start_index]

    semaphore = asyncio.Semaphore(max_workers)
    timeout = aiohttp.ClientTimeout(total=60)
    connector = aiohttp.TCPConnector(limit=max_workers)

    async with aiohttp.ClientSession(timeout=timeout, connector=connector) as session:
        tasks = [
            asyncio.create_task(
                process_single_url(idx, total, url, session, semaphore)
            )
            for idx, url in targets
        ]
        if tasks:
            await asyncio.gather(*tasks)


def setup_logging():
    """配置同时输出到控制台和日志文件的格式（仿照 batch_universities.py）"""
    logger.setLevel(logging.INFO)

    # 避免重复添加 handler
    if logger.handlers:
        return

    timestamp = time.strftime("%Y%m%d_%H%M%S")
    os.makedirs(LOG_DIR, exist_ok=True)
    log_filename = os.path.join(LOG_DIR, f"batch_md_{timestamp}.log")

    formatter = logging.Formatter(
        "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
    )

    # 文件日志
    file_handler = logging.FileHandler(log_filename, encoding="utf-8")
    file_handler.setLevel(logging.INFO)
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    # 控制台日志
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(logging.INFO)
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Export crawled URLs from users.db to markdown files via Jina."
    )
    parser.add_argument(
        "--start-from",
        type=int,
        default=1,
        help=(
            "1-based index of URL to start from, based on the [idx/total] "
            "value in the log. URLs before this index are skipped."
        ),
    )
    parser.add_argument(
        "--max-workers",
        type=int,
        default=5,
        help="Number of concurrent requests to Jina (default: 5).",
    )
    args = parser.parse_args()

    setup_logging()
    logger.info("=" * 60)
    logger.info("批量 Markdown 导出任务开始")
    logger.info("=" * 60)
    try:
        asyncio.run(main(start_index=args.start_from, max_workers=args.max_workers))
    finally:
        logger.info("=" * 60)
        logger.info("批量 Markdown 导出任务结束")
        logger.info("=" * 60)
