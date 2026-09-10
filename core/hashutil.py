"""文件 hash 工具（issue #176）。

把 #176 之前散落在 ``core.paddleocr_cache._file_hash`` 与
``core.pdf_splitter._doc_scratch_dir`` 两处的 sha256 流式实现**新增**一个公开
符号,沿项目 #171 PR-4 升格私有 helper 到公开 surface 的惯例。

不删除 :func:`core.paddleocr_cache._file_hash` —— 那是历史 cache 命名的
依赖关系,跨模块改动留作独立 ticket;这里只让 :mod:`core.pdf_splitter` 走
公开符号,停止 sha256 流式代码继续扩散。
"""
from __future__ import annotations

import hashlib
from pathlib import Path

__all__ = ["file_sha256_hex", "file_sha256_short"]


def file_sha256_hex(path: str | Path) -> str:
    """sha256(file contents) → 64-hex。

    抛 ``OSError``(open / read 失败),与 :func:`hashlib` 默认行为一致;
    调用方决定如何降级 —— :func:`core.pdf_splitter._doc_scratch_dir` 不需要
    降级(临时目录失败本就该让上游报),:func:`core.paddleocr_cache` 走
    try/except 走"缓存不可用"。
    """
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def file_sha256_short(path: str | Path, *, prefix: int = 12) -> str:
    """sha256 → 前 ``prefix`` 位 hex(默认 12 位,够 16M 文件不撞)。

    用作 :func:`core.pdf_splitter.parse_split` 的临时目录命名 —— 短而稳定,
    不携带原文件名(中文 / 空格 / 超长路径会撞 OS 限制)。
    """
    return file_sha256_hex(path)[:prefix]
