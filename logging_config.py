"""集中配置日志。

每个入口（main.py、api/server.py、scripts/*）都调用一次 setup_logging()，
避免 logger.info 因为根 logger 还是默认 WARNING 而完全不输出。
"""

import logging
import os


def setup_logging(level: str | None = None) -> None:
    """配置根 logger，可重复调用。

    Args:
        level: 日志级别字符串；不传时读环境变量 LOG_LEVEL，默认 INFO。
    """
    level_name = (level or os.getenv("LOG_LEVEL", "INFO")).upper()
    logging.basicConfig(
        level=getattr(logging, level_name, logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        force=True,
    )

    # httpx/httpcore 的每条 HTTP 200 会淹没业务日志；DEBUG 时保留。
    if getattr(logging, level_name, logging.INFO) > logging.DEBUG:
        for name in ("httpx", "httpx2", "httpcore"): # 它们的默认日志输出都是INFO，如果日志级别大于输出日志级别，那么就会被拦截
            logging.getLogger(name).setLevel(logging.WARNING)
