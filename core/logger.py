"""项目级统一日志配置。

用法：
    from core.logger import get_logger
    logger = get_logger(__name__)
    logger.warning("something happened: %s", detail)
"""

import logging
import sys

_configured = False


def get_logger(name: str) -> logging.Logger:
    """获取带统一格式的 logger。

    首次调用时自动配置根 logger（stderr 输出 + INFO 级别）。
    子模块传 ``__name__`` 即可。
    """
    global _configured
    if not _configured:
        _configured = True
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(
            logging.Formatter(
                "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
                datefmt="%H:%M:%S",
            )
        )
        root = logging.getLogger()
        root.setLevel(logging.INFO)
        root.handlers.clear()
        root.addHandler(handler)
    return logging.getLogger(name)
