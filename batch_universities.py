import os
import time
import sys
import logging
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed

import yaml

# Module-level logger; will show up as "__main__" when run as a script
logger = logging.getLogger(__name__)

BASE_URL = "http://localhost:6789"  # 根据实际端口修改
MAX_CONCURRENT_SITES = 10           # 最多并发爬取多少个站点
CONFIG_PATH = "config.yaml"         # 配置文件路径
LOG_DIR = "logs"                    # 日志输出目录


def load_universities(config_path: str = CONFIG_PATH):
    """从 config.yaml 读取院校域名列表"""
    if not os.path.exists(config_path):
        raise FileNotFoundError(f"未找到配置文件: {config_path}")

    with open(config_path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}

    universities = data.get("universities")
    if not isinstance(universities, list):
        raise ValueError("config.yaml 中的 `universities` 字段必须是列表")

    # 允许列表项是字符串或包含 domain 字段的字典
    result = []
    for item in universities:
        if isinstance(item, str):
            domain = item.strip()
        elif isinstance(item, dict) and "domain" in item:
            domain = str(item["domain"]).strip()
        else:
            continue

        if domain:
            result.append(domain)

    if not result:
        raise ValueError("config.yaml 中 `universities` 列表为空或无有效域名")

    return result


def crawl_site(base_url: str):
    """对一个站点启动爬虫并返回所有页面 URL 列表（单站点独立 Session）"""
    # 每个站点使用独立的 HTTP Session，这样后端会为每个站点创建独立的 WebCrawler 实例
    session = requests.Session()

    # 如果只给了域名，补上协议
    if not base_url.startswith(("http://", "https://")):
        base_url = "https://" + base_url

    # 1）启动爬虫
    resp = session.post(f"{BASE_URL}/api/start_crawl", json={"url": base_url})
    data = resp.json()
    if not data.get("success"):
        raise RuntimeError(f"start_crawl 失败: {data.get('error') or data}")

    logger.info("=" * 60)
    logger.info("开始爬取: %s, crawl_id=%s", base_url, data.get("crawl_id"))
    logger.info("=" * 60)

    # 2）轮询爬虫状态，直到完成
    while True:
        time.sleep(1)
        status = session.get(f"{BASE_URL}/api/crawl_status").json()
        stats = status.get("stats", {})
        logger.info(
            "[%s] crawled=%s discovered=%s status=%s",
            base_url,
            stats.get("crawled"),
            stats.get("discovered"),
            status.get("status"),
        )

        if status.get("status") == "completed":
            # 这里不加 url_since 参数，会返回当前爬虫的全部 URL 列表
            urls = status.get("urls", []) or []
            # 每个元素都是一个 dict，包含很多字段，这里只取 URL 字符串
            return [u.get("url") for u in urls]

        # 根据需要也可以加超时保护
        # TODO: 自行加上最大等待时间

def main():
    # 从配置文件中读取院校列表（域名或完整 URL 都行）
    universities = load_universities()

    all_results = {}

    # 按站点维度并发启动多个爬虫
    max_workers = min(MAX_CONCURRENT_SITES, len(universities))

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        # 提交所有任务（每个院校一个独立 Session / Crawler）
        future_to_uni = {
            executor.submit(crawl_site, uni): uni
            for uni in universities
        }

        # 等待所有任务完成
        for future in as_completed(future_to_uni):
            uni = future_to_uni[future]
            try:
                urls = future.result()
                all_results[uni] = urls
                logger.info("院校 %s 共爬到 %s 个页面 URL", uni, len(urls))
            except Exception as e:
                logger.error("爬取 %s 失败: %s", uni, e)

    # 这里可以把结果写文件 / 入库 / 进一步处理
    # 例如简单打印前几个
    for uni, urls in all_results.items():
        logger.info("=" * 60)
        logger.info("%s (%s URLs)", uni, len(urls))
        logger.info("=" * 60)
        for u in urls[:20]:
            logger.info("URL: %s", u)


def setup_logging():
    """配置同时输出到控制台和文件的日志格式"""
    logger.setLevel(logging.INFO)

    # 避免重复添加 handler（在某些环境中脚本可能被多次导入）
    if logger.handlers:
        return

    timestamp = time.strftime("%Y%m%d_%H%M%S")
    os.makedirs(LOG_DIR, exist_ok=True)
    log_filename = os.path.join(LOG_DIR, f"batch_universities_{timestamp}.log")

    # 格式示例：2025-11-30 16:11:33,189 - __main__ - INFO - message
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
    setup_logging()
    logger.info("=" * 60)
    logger.info("批量院校爬取任务开始")
    logger.info("=" * 60)
    try:
        main()
    finally:
        logger.info("=" * 60)
        logger.info("批量院校爬取任务结束")
        logger.info("=" * 60)
