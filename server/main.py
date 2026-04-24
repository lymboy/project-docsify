"""Unified server — docsify docs + Q&A API on one port.

FastAPI replaces nginx entirely. Custom middleware handles:
1. /api/*              → Q&A endpoints
2. /docqa-widget/*     → widget CSS/JS
3. Real files in docs/ → FileResponse (.md with text/plain; charset=utf-8)
4. Everything else     → docsify index.html with widget injected (SPA fallback)
"""

import os
from pathlib import Path
from urllib.parse import unquote

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse, HTMLResponse
from pydantic import BaseModel

from .config import HOST, PORT, DOCS_DIR, WIDGET_DIR, KB_DOCS_PATH, INDEX_PATH
from .indexer import DocIndex, build_index, build_index_with_embeddings, docs_changed
from .qa_engine import ask, ask_stream

app = FastAPI(title="Doc-QA + Docsify", docs_url=None, redoc_url=None)

DOCS_ROOT = Path(DOCS_DIR)
INDEX_HTML = DOCS_ROOT / "index.html"
WIDGET_ROOT = Path(WIDGET_DIR)

_index: DocIndex | None = None
_cached_index_html: str | None = None


def _get_index_html_with_widget() -> str:
    """Read docsify index.html and inject the chat widget before </body>."""
    global _cached_index_html
    if _cached_index_html is not None:
        return _cached_index_html

    with open(INDEX_HTML, "r", encoding="utf-8") as f:
        html = f.read()

    injection = (
        '\n<link rel="stylesheet" href="/docqa-widget/widget.css">\n'
        '<div id="docqa-widget"></div>\n'
        '<script src="/docqa-widget/widget.js"></script>\n'
    )
    _cached_index_html = html.replace("</body>", injection + "</body>")
    return _cached_index_html


@app.middleware("http")
async def docsify_spa_middleware(request: Request, call_next):
    path = request.url.path

    # API routes → FastAPI
    if path.startswith("/api"):
        return await call_next(request)

    # Widget static assets
    if path.startswith("/docqa-widget/"):
        rel = path[len("/docqa-widget/"):]
        if ".." in rel:
            return await call_next(request)
        widget_file = WIDGET_ROOT / rel
        if widget_file.is_file():
            return FileResponse(str(widget_file))
        return await call_next(request)

    # Root → index.html with widget
    if path == "/" or path == "":
        return HTMLResponse(_get_index_html_with_widget())

    # Real file in docs/
    rel_path = unquote(path.lstrip("/"))
    file_path = (DOCS_ROOT / rel_path).resolve()

    if not str(file_path).startswith(str(DOCS_ROOT.resolve())):
        return HTMLResponse(_get_index_html_with_widget())

    if file_path.is_file():
        if file_path.suffix == ".md":
            return FileResponse(str(file_path), media_type="text/plain; charset=utf-8")
        return FileResponse(str(file_path))

    # .md files that don't exist → 404 (docsify falls back to parent sidebar)
    if path.endswith(".md"):
        from fastapi.responses import Response
        return Response(status_code=404, content="Not Found")

    # SPA fallback → index.html with widget
    return HTMLResponse(_get_index_html_with_widget())


class QueryRequest(BaseModel):
    question: str
    history: list[dict] | None = None


class QueryResponse(BaseModel):
    answer: str
    sources: list[str]
    sections_used: int


class RebuildResponse(BaseModel):
    status: str
    file_count: int
    section_count: int
    total_chars: int


@app.on_event("startup")
async def startup():
    global _index
    from .config import EMBEDDING_API_BASE, EMBEDDING_MODEL

    index_file = os.path.join(INDEX_PATH, "sections.json")
    if os.path.exists(index_file):
        _index = DocIndex.load(INDEX_PATH)
        emb_status = "enabled" if _index.embedding_matrix is not None else "disabled"
        graph_nodes = _index.metadata.get("link_graph_nodes", 0)
        graph_edges = _index.metadata.get("link_graph_edges", 0)

        if docs_changed(KB_DOCS_PATH, _index):
            print("[STARTUP] Documents changed, rebuilding index...")
            if EMBEDDING_API_BASE and EMBEDDING_MODEL:
                _index = build_index_with_embeddings(KB_DOCS_PATH, INDEX_PATH)
            else:
                _index = build_index(KB_DOCS_PATH, INDEX_PATH)
        else:
            print("[STARTUP] Index up-to-date")
            print(f"[STARTUP] Sections: {_index.metadata.get('section_count', 0)} | "
                  f"Files: {_index.metadata.get('file_count', 0)} | "
                  f"Embedding: {emb_status} | "
                  f"Graph: {graph_nodes} nodes / {graph_edges} edges")
    else:
        print(f"[STARTUP] No index found, building from {KB_DOCS_PATH}")
        if EMBEDDING_API_BASE and EMBEDDING_MODEL:
            _index = build_index_with_embeddings(KB_DOCS_PATH, INDEX_PATH)
        else:
            _index = build_index(KB_DOCS_PATH, INDEX_PATH)


@app.post("/api/query", response_model=QueryResponse)
async def query(req: QueryRequest):
    if _index is None:
        raise HTTPException(503, "Index not ready")
    if not req.question.strip():
        raise HTTPException(400, "Question cannot be empty.")
    result = ask(_index, req.question, req.history)
    return QueryResponse(**result)


@app.post("/api/query/stream")
async def query_stream(req: QueryRequest):
    if _index is None:
        raise HTTPException(503, "Index not ready")
    if not req.question.strip():
        raise HTTPException(400, "Question cannot be empty.")
    return StreamingResponse(
        ask_stream(_index, req.question, req.history),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@app.post("/api/rebuild", response_model=RebuildResponse)
async def rebuild():
    global _index
    try:
        from .config import EMBEDDING_API_BASE, EMBEDDING_MODEL
        if EMBEDDING_API_BASE and EMBEDDING_MODEL:
            _index = build_index_with_embeddings(KB_DOCS_PATH, INDEX_PATH)
        else:
            _index = build_index(KB_DOCS_PATH, INDEX_PATH)
        return RebuildResponse(status="ok", **_index.metadata)
    except Exception as e:
        raise HTTPException(500, f"Rebuild failed: {str(e)}")


@app.get("/api/status")
async def status():
    if _index is None:
        return {"status": "not_ready"}
    emb_status = "enabled" if _index.embedding_matrix is not None else "disabled"
    meta = dict(_index.metadata)
    meta["embedding"] = emb_status
    return {"status": "ready", "metadata": meta}


@app.get("/api/suggest")
async def suggest(q: str = "", limit: int = 10):
    if _index is None or not q.strip():
        return {"suggestions": []}
    q_lower = q.lower()
    matches = []
    for section in _index.sections:
        if q_lower in section.title.lower() or q_lower in section.content[:200].lower():
            matches.append({
                "title": section.title,
                "source": section.source_file,
                "heading_path": section.heading_path,
            })
            if len(matches) >= limit:
                break
    return {"suggestions": matches}


def main():
    import uvicorn
    uvicorn.run(app, host=HOST, port=PORT)


if __name__ == "__main__":
    main()
