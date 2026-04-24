"""Q&A Engine — Takes a question, finds relevant docs, calls LLM, returns answer.

Implements llm_wiki-style strategies:
- Token budget allocation (proportional, not char truncation)
- Purpose/direction context (anchors answers to user intent)
- Full content delivery (not summaries, within budget)
- Graph-expanded sections included
"""

import json
import re
from typing import Generator

from openai import OpenAI
from .config import (
    LLM_API_BASE, LLM_API_KEY, LLM_MODEL,
    LLM_MAX_TOKENS, SYSTEM_PROMPT,
    CONTEXT_TOKEN_BUDGET,
    BUDGET_PAGES_PCT, BUDGET_HISTORY_PCT, BUDGET_INDEX_PCT, BUDGET_SYSTEM_PCT,
    get_custom_headers, load_purpose,
)
from .indexer import DocIndex, search_hybrid, annotate_mermaid_blocks


# ---------------------------------------------------------------------------
# Token estimation
# ---------------------------------------------------------------------------

def _estimate_tokens(text: str) -> int:
    """Rough token estimation for Chinese + mixed content.

    CJK characters: ~1 token per 1.5 chars
    English/other: ~1 token per 4 chars (whitespace tokenized)
    """
    cjk_count = sum(1 for c in text if '\u4e00' <= c <= '\u9fff' or '\u3400' <= c <= '\u4dbf')
    other_count = len(text) - cjk_count
    return int(cjk_count / 1.5 + other_count / 4)


# ---------------------------------------------------------------------------
# Context assembly with token budget (llm_wiki style)
# ---------------------------------------------------------------------------

def build_context(sections: list[dict], token_budget: int) -> tuple[str, list[str], int]:
    """Assemble context from search results, respecting token budget.

    Like llm_wiki: deliver full content within budget, not summaries.
    Prioritize by score, fill until budget exhausted.

    Returns (context_string, source_file_list, tokens_used).
    """
    context_parts = []
    tokens_used = 0
    sources = []
    source_files = set()

    # Sort by score descending
    sorted_results = sorted(sections, key=lambda x: -x["score"])

    for i, result in enumerate(sorted_results):
        section = result["section"]
        heading = " > ".join(section.heading_path) if section.heading_path else section.title
        clean_content = annotate_mermaid_blocks(section.content)
        source_tag = f"[来源: {section.source_file}]"
        part = f"[文档{i+1}] {heading} {source_tag}\n\n{clean_content}"

        part_tokens = _estimate_tokens(part)

        if tokens_used + part_tokens > token_budget:
            # Try to fit a truncated version if we have >30% budget remaining
            if tokens_used < token_budget * 0.3:
                break
            # Truncate content to fit remaining budget
            remaining = token_budget - tokens_used
            # Rough char truncation based on ratio
            ratio = remaining / part_tokens
            max_chars = int(len(clean_content) * ratio * 0.8)  # 80% safety margin
            if max_chars > 200:
                truncated = clean_content[:max_chars] + "\n\n[...内容已截断]"
                part = f"[文档{i+1}] {heading} {source_tag}\n\n{truncated}"
                part_tokens = _estimate_tokens(part)
                if tokens_used + part_tokens <= token_budget:
                    context_parts.append(part)
                    tokens_used += part_tokens
                    if section.source_file not in source_files:
                        sources.append(section.source_file)
                        source_files.add(section.source_file)
            break

        context_parts.append(part)
        tokens_used += part_tokens
        if section.source_file not in source_files:
            sources.append(section.source_file)
            source_files.add(section.source_file)

    return "\n\n---\n\n".join(context_parts), sources, tokens_used


def _build_messages(index: DocIndex, question: str, context: str,
                    history: list[dict] = None, context_tokens: int = 0,
                    index_tokens: int = 0) -> list[dict]:
    """Build messages array with token budget allocation.

    Budget allocation (like llm_wiki):
    - System: BUDGET_SYSTEM_PCT% — system prompt
    - Index overview: BUDGET_INDEX_PCT% — knowledge map
    - History: BUDGET_HISTORY_PCT% — recent chat turns
    - Pages: BUDGET_PAGES_PCT% — document content
    """
    messages = []

    # 1. System prompt
    messages.append({"role": "system", "content": SYSTEM_PROMPT})

    # 2. Purpose/direction context (like llm_wiki's purpose.md)
    purpose = load_purpose()
    if purpose:
        messages.append({
            "role": "system",
            "content": f"以下是本知识库的目标和范围：\n\n{purpose}"
        })

    # 3. Index overview (like llm_wiki's index.md — knowledge map)
    index_overview = _build_index_overview(index)
    messages.append({
        "role": "system",
        "content": f"以下是项目的知识索引概览：\n\n{index_overview}"
    })

    # 4. Chat history (respect budget)
    if history:
        history_budget = int(CONTEXT_TOKEN_BUDGET * BUDGET_HISTORY_PCT / 100)
        history_msgs = []
        history_tokens = 0
        for msg in reversed(history[-10:]):  # Look at last 5 turns
            msg_tokens = _estimate_tokens(msg.get("content", ""))
            if history_tokens + msg_tokens > history_budget:
                break
            history_msgs.insert(0, msg)
            history_tokens += msg_tokens
        messages.extend(history_msgs)

    # 5. Context and question
    # Mark which sections came from graph expansion
    messages.append({
        "role": "user",
        "content": f"参考以下文档内容回答问题（已使用 {context_tokens} tokens 用于文档内容，剩余空间用于回答）：\n\n{context}\n\n---\n\n问题：{question}"
    })

    return messages


def _get_llm_client() -> OpenAI:
    """Create an OpenAI client with custom headers if configured."""
    http_client = None
    custom_headers = get_custom_headers()
    if custom_headers:
        import httpx
        http_client = httpx.Client(headers=custom_headers)
    return OpenAI(base_url=LLM_API_BASE, api_key=LLM_API_KEY, http_client=http_client)


# ---------------------------------------------------------------------------
# Public API: streaming + non-streaming
# ---------------------------------------------------------------------------

def ask(index: DocIndex, question: str, history: list[dict] = None) -> dict:
    """Non-streaming Q&A pipeline."""
    from .config import SEARCH_TOP_K, GRAPH_EXPAND_K

    search_results = search_hybrid(index, question, top_k=SEARCH_TOP_K, graph_expand_k=GRAPH_EXPAND_K)

    if not search_results:
        return {
            "answer": "抱歉，在文档中没有找到与您问题相关的内容。请尝试换个方式描述问题。",
            "sources": [],
            "sections_used": 0,
        }

    # Token budget for pages
    pages_budget = int(CONTEXT_TOKEN_BUDGET * BUDGET_PAGES_PCT / 100)
    context, sources, context_tokens = build_context(search_results, pages_budget)

    client = _get_llm_client()
    messages = _build_messages(index, question, context, history, context_tokens)

    try:
        response = client.chat.completions.create(
            model=LLM_MODEL,
            messages=messages,
            temperature=0.3,
        )
        answer = response.choices[0].message.content
    except Exception as e:
        answer = f"LLM 调用失败: {str(e)}"

    # Separate graph-expanded sources for transparency
    search_sources = [r["section"].source_file for r in search_results if r.get("source") == "search"]
    graph_sources = [r["section"].source_file for r in search_results if r.get("source") == "graph"]

    return {
        "answer": answer,
        "sources": sources,
        "sections_used": len(search_results),
        "graph_expanded": len(graph_sources) > 0,
    }


def ask_stream(index: DocIndex, question: str, history: list[dict] = None) -> Generator[str, None, None]:
    """Streaming Q&A pipeline: search → build context → stream LLM response."""
    from .config import SEARCH_TOP_K, GRAPH_EXPAND_K

    search_results = search_hybrid(index, question, top_k=SEARCH_TOP_K, graph_expand_k=GRAPH_EXPAND_K)

    if not search_results:
        yield _sse({"type": "error", "content": "抱歉，在文档中没有找到与您问题相关的内容。"})
        yield _sse({"type": "done", "sections_used": 0})
        return

    pages_budget = int(CONTEXT_TOKEN_BUDGET * BUDGET_PAGES_PCT / 100)
    context, sources, context_tokens = build_context(search_results, pages_budget)

    # Send sources first
    graph_expanded = any(r.get("source") == "graph" for r in search_results)
    yield _sse({"type": "sources", "sources": sources})
    yield _sse({"type": "sections_used", "count": len(search_results), "graph_expanded": graph_expanded})
    yield _sse({"type": "budget", "pages_tokens": context_tokens, "total_budget": CONTEXT_TOKEN_BUDGET})

    client = _get_llm_client()
    messages = _build_messages(index, question, context, history, context_tokens)

    try:
        response = client.chat.completions.create(
            model=LLM_MODEL,
            messages=messages,
            temperature=0.3,
            stream=True,
        )
        for chunk in response:
            if chunk.choices and chunk.choices[0].delta.content:
                yield _sse({"type": "token", "content": chunk.choices[0].delta.content})
    except Exception as e:
        yield _sse({"type": "error", "content": f"LLM 调用失败: {str(e)}"})

    yield _sse({"type": "done", "sections_used": len(search_results)})


def _sse(data: dict) -> str:
    """Format a dict as an SSE data event."""
    return f"data: {json.dumps(data, ensure_ascii=False)}\n\n"


def _build_index_overview(index: DocIndex) -> str:
    """Build a condensed overview of all documents (like llm_wiki's index.md)."""
    file_sections = {}
    for section in index.sections:
        f = section.source_file
        if f not in file_sections:
            file_sections[f] = []
        file_sections[f].append(section.title)

    lines = [f"知识库共 {len(file_sections)} 篇文档，{len(index.sections)} 个章节：\n"]
    for filepath, titles in sorted(file_sections.items()):
        lines.append(f"- {filepath}")
        for t in titles[:8]:
            lines.append(f"  - {t}")
        if len(titles) > 8:
            lines.append(f"  - ... 还有 {len(titles) - 8} 个章节")

    return "\n".join(lines)
