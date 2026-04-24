"""Configuration — all settings from environment variables."""

import os

# --- Project paths (relative to project root) ---
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Docsify docs directory — source for both docs serving and Q&A indexing
DOCS_DIR = os.getenv("DOCS_DIR", os.path.join(_PROJECT_ROOT, "docs"))

# Widget static files directory
WIDGET_DIR = os.getenv("WIDGET_DIR",
                       os.path.join(_PROJECT_ROOT, "docqa-widget"))

# Index cache directory
INDEX_DIR = os.getenv("INDEX_DIR", os.path.join(_PROJECT_ROOT, "index"))

# --- Server ---
PORT = int(os.getenv("PORT", "8000"))
HOST = os.getenv("HOST", "0.0.0.0")

# --- LLM ---
LLM_API_BASE = os.getenv("LLM_API_BASE", "")
LLM_API_KEY = os.getenv("LLM_API_KEY", "")
LLM_MODEL = os.getenv("LLM_MODEL", "")
LLM_MAX_TOKENS = int(os.getenv("LLM_MAX_TOKENS", "128000"))

# Optional: custom HTTP headers for LLM API (format: "Header1:Value1,Header2:Value2")
LLM_CUSTOM_HEADERS = os.getenv("LLM_CUSTOM_HEADERS", "")

# --- Embedding (optional, for semantic search) ---
EMBEDDING_API_BASE = os.getenv("EMBEDDING_API_BASE", "")
EMBEDDING_API_KEY = os.getenv("EMBEDDING_API_KEY", "")
EMBEDDING_MODEL = os.getenv("EMBEDDING_MODEL", "")

# --- Token budget (llm_wiki style) ---
CONTEXT_TOKEN_BUDGET = int(os.getenv("CONTEXT_TOKEN_BUDGET", "150000"))

# --- System prompt ---
SYSTEM_PROMPT = """你是一个专业的技术文档问答助手。你的任务是根据提供的文档内容准确回答用户的问题。

规则：
1. 只根据提供的文档内容回答，不要编造信息
2. 如果文档中没有相关信息，明确告知用户
3. 引用来源时使用文件名标注，格式：[来源: 文件名.md]，不要使用数字编号
4. 如果某个声明你无法在提供的文档中找到依据，标注 [未验证]
5. 优先给出精确的技术细节（枚举值、状态码、字段名等）
6. 用中文回答，保持专业和简洁
7. 如果问题涉及多个文档，综合所有相关信息给出完整答案
8. 格式紧凑：段落之间只用一个空行；列表项之间不加空行；表格前后不加多余空行；禁止连续两个以上空行
9. 图文结合：当回答涉及业务流程、调用链路、状态转换、数据关系时，必须先用 mermaid 图表做可视化摘要，再辅以文字说明。选图规则：业务流程用 flowchart，交互时序用 sequenceDiagram，状态转换用 stateDiagram-v2，数据关系用 erDiagram。如果文档中已有相关 mermaid 图，优先引用原图；如果原文只有文字描述，则根据源码逻辑自行生成 mermaid 图
"""


def get_custom_headers() -> dict:
    """Parse LLM_CUSTOM_HEADERS into a dict."""
    headers = {}
    if LLM_CUSTOM_HEADERS:
        for pair in LLM_CUSTOM_HEADERS.split(","):
            if ":" in pair:
                key, value = pair.split(":", 1)
                headers[key.strip()] = value.strip()
    return headers