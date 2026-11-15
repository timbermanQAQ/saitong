"""LLM 封装：OpenAI 兼容接口，只允许基于给定片段作答；未配置 Key 时由调用方走抽取式。

本模块不依赖 kb / team，片段以 ContextPiece 传入，方便单测。
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import Any, Iterable

from . import audit
from .config import Settings, get_settings

_THINK_RE = re.compile(r"<think>\s*(.*?)\s*</think>", re.I | re.S)
_SELF_CLOSING_THINK_RE = re.compile(r"<think\s*/>", re.I)

SYSTEM_PROMPT = (
    "你是「赛题通」，为 2026 OceanBase 数据库大赛参赛队伍答疑。"
    "只能依据【资料片段】作答，不得补充片段里没有的事实、链接或数字。"
    "规则：1) 用中文，先给结论再给要点；2) 每个要点句末标注来源编号，如 [1][2]，只允许标注确实支撑该句的编号；"
    "3) 资料里没有依据时，直接回答「文档里没写」，并说明还需要补充什么信息；"
    "4) 不要输出思考过程，不要复述整段原文。"
)


class LLMUnavailable(RuntimeError):
    """LLM 未配置或调用失败——调用方应降级到抽取式答案。"""


@dataclass(frozen=True)
class ContextPiece:
    """一条待引用的检索片段。"""

    doc_title: str
    heading: str
    source_url: str
    text: str
    track: str = ""
    chunk_id: str = ""

    def as_source(self) -> dict[str, str]:
        return {"doc_title": self.doc_title, "heading": self.heading,
                "source_url": self.source_url, "track": self.track, "id": self.chunk_id}


@dataclass
class LLMResult:
    answer: str
    model: str
    elapsed_ms: float
    cited: list[int]

    def as_dict(self) -> dict[str, Any]:
        return {"model": self.model, "elapsed_ms": self.elapsed_ms, "cited": self.cited}


def strip_think(text: str) -> str:
    """去掉模型思考标签（think 开闭对）及其内容，有时未闭合则截到标签处。"""
    cleaned = _THINK_RE.sub("", text or "")
    cleaned = _SELF_CLOSING_THINK_RE.sub("", cleaned)
    if "<think>" in cleaned.lower():  # 标签未闭合：截到标签处为止
        cut = re.split(r"(?i)<think>", cleaned)
        cleaned = cut[0]
    return re.sub(r"\n{3,}", "\n\n", cleaned).strip()


def is_configured(settings: Settings | None = None) -> bool:
    cfg = settings or get_settings()
    return cfg.llm_enabled


def configured_model(settings: Settings | None = None) -> str | None:
    cfg = settings or get_settings()
    return cfg.llm_model if cfg.llm_enabled else None


def build_prompt(question: str, pieces: Iterable[ContextPiece]) -> list[dict[str, str]]:
    numbered = []
    for index, piece in enumerate(pieces, start=1):
        header = f"[{index}] {piece.doc_title}"
        if piece.heading:
            header += f" › {piece.heading}"
        if piece.source_url:
            header += f" ({piece.source_url})"
        numbered.append(f"{header}\n{piece.text.strip()}")
    user = (
        "【资料片段】\n" + "\n\n---\n\n".join(numbered)
        + f"\n\n【问题】\n{question}\n"
        "请只依据上面的片段回答，并按规则标注 [n]。"
    )
    return [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": user}]


def _client(settings: Settings) -> Any:
    from openai import OpenAI  # 延迟导入：无 LLM 场景不需要该依赖

    return OpenAI(
        base_url=settings.llm_base_url,
        api_key=settings.llm_api_key,
        timeout=settings.llm_timeout,
        max_retries=0,
    )


def answer(question: str, pieces: list[ContextPiece], *,
           settings: Settings | None = None) -> LLMResult:
    """受约束问答；失败或未配置抛 LLMUnavailable，由调用方降级。"""
    cfg = settings or get_settings()
    if not cfg.llm_enabled:
        raise LLMUnavailable("未配置 LLM（需要 LLM_BASE_URL / LLM_API_KEY / LLM_MODEL）")
    if not pieces:
        raise LLMUnavailable("没有可用片段")

    started = time.perf_counter()
    model = cfg.llm_model
    error: str | None = None
    raw = ""
    try:
        client = _client(cfg)
        completion = client.chat.completions.create(
            model=model,
            messages=build_prompt(question, pieces),
            temperature=0.2,
        )
        raw = (completion.choices[0].message.content or "") if completion.choices else ""
        cleaned = strip_think(raw)
        if not cleaned:
            raise LLMUnavailable("模型返回空答案")
    except LLMUnavailable as exc:
        error = str(exc)
        raise
    except Exception as exc:  # openai/网络异常统一降级
        error = f"{type(exc).__name__}: {exc}"
        raise LLMUnavailable(error) from exc
    finally:
        audit.log_event(
            "llm",
            settings=cfg,
            event="chat",
            model=model,
            question=question[:200],
            n_pieces=len(pieces),
            chunk_ids=[piece.chunk_id for piece in pieces if piece.chunk_id],
            elapsed_ms=round((time.perf_counter() - started) * 1000, 1),
            answer_chars=len(raw),
            ok=error is None,
            error=error,
        )
    return LLMResult(
        answer=cleaned,
        model=model,
        elapsed_ms=round((time.perf_counter() - started) * 1000, 1),
        cited=sorted({int(n) for n in re.findall(r"\[(\d{1,2})\]", cleaned) if int(n) <= len(pieces)}),
    )


_TABLE_CELL_RE = re.compile(r"^:?-{2,}:?$")


def _table_cells(line: str) -> list[str] | None:
    """是 Markdown 表格行就返回去空白的单元格，否则 None。"""
    text = line.strip()
    if not text.startswith("|") or text.count("|") < 2:
        return None
    return [cell.strip() for cell in text.strip("|").split("|")]


def _is_separator(cells: list[str]) -> bool:
    return bool(cells) and all(_TABLE_CELL_RE.match(cell.replace(" ", "")) for cell in cells)


def _render_row(cells: list[str], header: list[str] | None) -> str:
    """把一行表格摊成「表头：值；表头：值」，抽取式答案里没有列对齐可言。"""
    pairs = [(header[i] if header and i < len(header) else "", cell) for i, cell in enumerate(cells)]
    filled = [(label, value) for label, value in pairs if value]
    if not filled:
        return ""
    if all(label for label, _ in filled):
        return "；".join(f"{label}：{value}" for label, value in filled)
    return "；".join(value for _, value in filled)


def _strip_unbalanced_bold(line: str) -> str:
    """落单的 `**` 只会让答案像被截断；成对的保留（那是原文的强调）。"""
    return line.replace("**", "") if line.count("**") % 2 else line


def _is_header_row(cells_of: list[list[str] | None], index: int) -> bool:
    """表头行的特征：上一行不是表格行（或本就是首行），下一行是分隔行。"""
    previous = cells_of[index - 1] if index else None
    following = cells_of[index + 1] if index + 1 < len(cells_of) else None
    return previous is None and following is not None and _is_separator(following)


def clean_markdown_noise(text: str) -> str:
    """抽取式答案前的清洗：去表格分隔行、表格行转可读文本、去掉未闭合的加粗符号。"""
    lines = (text or "").splitlines()
    cells_of = [_table_cells(line) for line in lines]
    out: list[str] = []
    header: list[str] | None = None
    for index, cells in enumerate(cells_of):
        if cells is None:
            header = None  # 表格结束
            if lines[index].strip():
                out.append(_strip_unbalanced_bold(lines[index]))
            elif out and out[-1].strip():
                out.append("")
            continue
        if _is_separator(cells):
            continue  # | :--- | :--- | 这种行对纯文本毫无意义
        if header is None and _is_header_row(cells_of, index):
            header = cells  # 表头不当正文，只拿来给数据行当标签
            continue
        rendered = _render_row(cells, header)
        if rendered:
            out.append(_strip_unbalanced_bold(rendered))
    return "\n".join(out).strip()


def extractive_answer(question: str, pieces: list[ContextPiece], *, max_pieces: int = 3) -> str:
    """无 LLM 时的兜底：首条片段全文 + 其余片段首句，逐条带编号。"""

    def sentences(text: str) -> list[str]:
        parts = [part.strip() for part in re.split(r"(?<=[。！？；\n])", text) if part.strip()]
        return parts

    if not pieces:
        return "文档里没检索到相关内容，换个说法或带上赛道名再试一次。"
    lines: list[str] = [f"根据知识库检索结果整理（抽取式答案，共 {len(pieces)} 条相关片段）：", ""]
    for index, piece in enumerate(pieces[:max_pieces], start=1):
        body = clean_markdown_noise(piece.text)
        header = piece.heading or piece.doc_title
        if index == 1:
            content = body
            if not content.startswith("【"):
                content = f"{header}\n{content}"
        else:
            useful = [s for s in sentences(body) if not s.startswith("【")]
            content = " ".join(useful[:2])[:300] or body[:200]
        lines.append(f"{index}. {content} [{index}]")
    lines.append("")
    lines.append("点开下方来源可核对原文。")
    return "\n".join(lines)
