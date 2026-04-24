"""Document Indexer — Ingest docsify markdown into a searchable index.

Borrowed from llm_wiki's "Ingest" concept: compile raw docs into a structured,
searchable index that the Q&A engine can use for context retrieval.

Key difference from llm_wiki: we don't re-generate a wiki — our docs ARE
already the "compiled wiki". We just build a search index on top.

Now includes a document link graph for 2-hop expansion (llm_wiki's key advantage):
- Markdown cross-references between documents
- Source file overlap (sections from same .md are related)
- Both signals feed into graph expansion after initial search
"""

import os
import json
import pickle
import re
import hashlib
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

import jieba
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity
import numpy as np


def extract_mermaid_text(code: str) -> str:
    """Extract meaningful business text from mermaid diagram code.

    Strips syntax keywords (flowchart, subgraph, -->, etc.) and keeps
    node labels, participant names, subgraph titles — the actual
    business knowledge encoded in the diagram.
    """
    lines = code.split("\n")
    texts = []

    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue

        syntax_only = re.match(
            r'^(flowchart|sequenceDiagram|graph|classDiagram|stateDiagram|erDiagram|gantt|pie|mindmap|gitGraph|journey|direction|end|```|mermaid)(\s|$)',
            stripped,
        )
        if syntax_only:
            continue

        subgraph_match = re.match(r'subgraph\s+(?:\w+\s+)?["\']?(.+?)["\']?\s*$', stripped)
        if subgraph_match:
            texts.append(subgraph_match.group(1).strip())
            continue

        participant_match = re.match(r'participant\s+\w+\s+as\s+(.+)', stripped)
        if participant_match:
            texts.append(participant_match.group(1).strip())
            continue
        participant_simple = re.match(r'participant\s+(.+)', stripped)
        if participant_simple:
            texts.append(participant_simple.group(1).strip())
            continue

        for match in re.finditer(r'\[["\']?(.*?)["\']?\]', stripped):
            label = match.group(1)
            label = re.sub(r'<br\s*/?>', ' ', label)
            label = re.sub(r'<[^>]+>', '', label)
            label = re.sub(r':::\w+', '', label)
            label = label.strip()
            if label and not re.match(r'^[\W\d_]+$', label):
                texts.append(label)

        for match in re.finditer(r'\|([^|]+)\|', stripped):
            annotation = match.group(1).strip()
            if annotation and not re.match(r'^[\W\d_]+$', annotation):
                texts.append(annotation)

        note_match = re.match(r'(?:Note\s+(?:right|left|over)\s+.*?:\s*)(.+)', stripped, re.IGNORECASE)
        if note_match:
            texts.append(note_match.group(1).strip())

        class_match = re.match(r'class\s+(\w+)', stripped)
        if class_match:
            texts.append(class_match.group(1))

        state_match = re.match(r'state\s+["\'](.+?)["\']\s+as\s+\w+', stripped)
        if state_match:
            texts.append(state_match.group(1).strip())

    return " ".join(texts)


def strip_mermaid_blocks(text: str) -> str:
    """Replace mermaid code blocks with extracted business text (for TF-IDF indexing)."""
    def replace_block(match):
        code = match.group(1)
        extracted = extract_mermaid_text(code)
        if extracted.strip():
            return f"[图: {extracted.strip()}]"
        return ""
    return re.sub(r'```mermaid\s*\n(.*?)```', replace_block, text, flags=re.DOTALL)


def annotate_mermaid_blocks(text: str) -> str:
    """Keep mermaid code blocks but add a short annotation above each one (for LLM context)."""
    def replace_block(match):
        code = match.group(1)
        extracted = extract_mermaid_text(code)
        annotation = f"<!-- 图表摘要: {extracted.strip()} -->" if extracted.strip() else ""
        if annotation:
            return f"{annotation}\n```mermaid\n{code}```"
        return f"```mermaid\n{code}```"
    return re.sub(r'```mermaid\s*\n(.*?)```', replace_block, text, flags=re.DOTALL)


@dataclass
class DocSection:
    """A searchable section of documentation."""
    id: str
    title: str
    content: str
    source_file: str
    heading_path: list = field(default_factory=list)
    char_count: int = 0

    def __post_init__(self):
        self.char_count = len(self.content)


@dataclass
class DocIndex:
    """The compiled index of all document sections."""
    sections: list = field(default_factory=list)
    tfidf_matrix: any = None
    tfidf_vectorizer: any = None
    embedding_matrix: any = None
    # Link graph: section_id → set of linked section_ids
    # Two signals: markdown links + source file overlap
    link_graph: dict = field(default_factory=dict)
    metadata: dict = field(default_factory=dict)

    def save(self, path: str):
        os.makedirs(path, exist_ok=True)
        with open(os.path.join(path, "sections.json"), "w", encoding="utf-8") as f:
            json.dump([s.__dict__ for s in self.sections], f, ensure_ascii=False, indent=2)
        with open(os.path.join(path, "tfidf.pkl"), "wb") as f:
            pickle.dump((self.tfidf_matrix, self.tfidf_vectorizer), f)
        if self.embedding_matrix is not None:
            with open(os.path.join(path, "embeddings.pkl"), "wb") as f:
                pickle.dump(self.embedding_matrix, f)
        # Save link graph as JSON (sets → lists for serialization)
        graph_ser = {k: list(v) for k, v in self.link_graph.items()}
        with open(os.path.join(path, "link_graph.json"), "w", encoding="utf-8") as f:
            json.dump(graph_ser, f, ensure_ascii=False)
        with open(os.path.join(path, "metadata.json"), "w", encoding="utf-8") as f:
            json.dump(self.metadata, f, ensure_ascii=False, indent=2)

    @classmethod
    def load(cls, path: str) -> "DocIndex":
        idx = cls()
        with open(os.path.join(path, "sections.json"), "r", encoding="utf-8") as f:
            idx.sections = [DocSection(**s) for s in json.load(f)]
        with open(os.path.join(path, "tfidf.pkl"), "rb") as f:
            idx.tfidf_matrix, idx.tfidf_vectorizer = pickle.load(f)
        emb_path = os.path.join(path, "embeddings.pkl")
        if os.path.exists(emb_path):
            with open(emb_path, "rb") as f:
                idx.embedding_matrix = pickle.load(f)
        # Load link graph (lists → sets)
        graph_path = os.path.join(path, "link_graph.json")
        if os.path.exists(graph_path):
            with open(graph_path, "r", encoding="utf-8") as f:
                graph_data = json.load(f)
            idx.link_graph = {k: set(v) for k, v in graph_data.items()}
        with open(os.path.join(path, "metadata.json"), "r", encoding="utf-8") as f:
            idx.metadata = json.load(f)
        return idx


def parse_markdown_sections(filepath: str, docs_root: str = "") -> list[DocSection]:
    """Parse a markdown file into sections by ## headings."""
    with open(filepath, "r", encoding="utf-8") as f:
        content = f.read()

    content = strip_mermaid_blocks(content)

    lines = content.split("\n")
    sections = []
    current_h1 = ""
    current_headings = []
    current_content_lines = []

    def flush_section():
        nonlocal current_content_lines
        if not current_content_lines:
            return
        text = "\n".join(current_content_lines).strip()
        if not text or len(text) < 20:
            current_content_lines = []
            return
        # Relative path from docs_root (e.g. "01-overview/01-项目概览.md")
        if docs_root:
            rel_path = os.path.relpath(filepath, docs_root)
        else:
            rel_path = os.path.relpath(filepath, os.path.dirname(filepath))
        section_id = f"{rel_path}#{'#'.join(current_headings)}"
        sections.append(DocSection(
            id=section_id,
            title=current_headings[-1] if current_headings else rel_path,
            content=text,
            source_file=rel_path,
            heading_path=list(current_headings),
        ))
        current_content_lines = []

    for i, line in enumerate(lines):
        h1_match = re.match(r"^# (.+)", line)
        h2_match = re.match(r"^## (.+)", line)
        h3_match = re.match(r"^### (.+)", line)

        if h1_match:
            flush_section()
            current_h1 = h1_match.group(1).strip()
            current_headings = [current_h1]
        elif h2_match:
            flush_section()
            current_headings = [current_h1, h2_match.group(1).strip()]
        elif h3_match:
            flush_section()
            current_headings = [current_h1, h3_match.group(1).strip()]
        else:
            current_content_lines.append(line)

    flush_section()
    return sections


def _parse_markdown_links(filepath: str, docs_root: str) -> list[str]:
    """Parse a markdown file for internal links to other .md files.

    Returns a list of resolved relative paths (matching DocSection.source_file format).
    """
    with open(filepath, "r", encoding="utf-8") as f:
        content = f.read()

    # Match [text](path.md) or [text](path.md#anchor) — exclude external URLs and images
    links = re.findall(r'\[([^\]]*)\]\(([^)]*\.md[^)]*)\)', content)
    resolved = []
    file_dir = os.path.dirname(filepath)

    for link_text, link_path in links:
        if link_path.startswith("http"):
            continue
        # Strip anchor
        link_path = link_path.split("#")[0]
        if not link_path:
            continue
        # Resolve relative path
        full_path = os.path.normpath(os.path.join(file_dir, link_path))
        rel_path = os.path.relpath(full_path, docs_root)
        resolved.append(rel_path)

    return resolved


def _build_link_graph(sections: list[DocSection], docs_root: str) -> dict:
    """Build a link graph from markdown cross-references and source file overlap.

    Two signals (like llm_wiki's 4-signal model, adapted for docsify):
    - Markdown links: explicit cross-references between documents
    - Source overlap: sections from the same source file are related (×4 weight in llm_wiki)

    Returns: {section_id: set(linked_section_ids)}
    """
    graph = defaultdict(set)

    # 1. Source file overlap: all sections in the same .md are connected
    file_to_sections = defaultdict(list)
    for section in sections:
        file_to_sections[section.source_file].append(section.id)

    for source_file, section_ids in file_to_sections.items():
        # Connect all sections in the same file to each other
        for sid in section_ids:
            for other_sid in section_ids:
                if sid != other_sid:
                    graph[sid].add(other_sid)

    # 2. Markdown links between documents
    # Group sections by source file for efficient lookup
    source_to_sections = defaultdict(list)
    for section in sections:
        source_to_sections[section.source_file].append(section.id)

    # Find all markdown files and parse their links
    docs_root_path = Path(docs_root)
    md_files = sorted(docs_root_path.rglob("*.md"))
    md_files = [f for f in md_files if f.name not in ("_sidebar.md",)]

    for md_file in md_files:
        try:
            linked_files = _parse_markdown_links(str(md_file), str(docs_root_path))
            rel_path = os.path.relpath(str(md_file), str(docs_root_path))
            # For each linked file, connect sections of this file to sections of linked file
            this_sections = source_to_sections.get(rel_path, [])
            for linked_file in linked_files:
                linked_sections = source_to_sections.get(linked_file, [])
                for ts in this_sections:
                    for ls in linked_sections:
                        graph[ts].add(ls)
        except Exception:
            pass

    return dict(graph)


def tokenize_chinese(text: str) -> str:
    """Tokenize Chinese text with jieba for TF-IDF."""
    words = jieba.cut(text)
    return " ".join(words)


def _compute_docs_hash(docs_path: str) -> str:
    """Compute a SHA256 hash over all markdown files for change detection.

    Like llm_wiki's incremental caching: if hash matches, skip rebuild.
    """
    docs_root = Path(docs_path)
    md_files = sorted(docs_root.rglob("*.md"))
    md_files = [f for f in md_files if f.name not in ("_sidebar.md",)]

    hasher = hashlib.sha256()
    for f in md_files:
        # Include relative path + modification time + file size
        rel = f.relative_to(docs_root)
        stat = f.stat()
        hasher.update(str(rel).encode())
        hasher.update(str(stat.st_mtime).encode())
        hasher.update(str(stat.st_size).encode())

    return hasher.hexdigest()


def docs_changed(docs_path: str, index: DocIndex) -> bool:
    """Check if documents have changed since last index build."""
    stored_hash = index.metadata.get("docs_hash", "")
    if not stored_hash:
        return True  # No hash stored, assume changed
    current_hash = _compute_docs_hash(docs_path)
    return current_hash != stored_hash


def build_index(docs_path: str, index_path: str) -> DocIndex:
    """Build a searchable index from all markdown files in docs_path."""
    docs_root = Path(docs_path)
    md_files = sorted(docs_root.rglob("*.md"))
    md_files = [f for f in md_files if f.name not in ("_sidebar.md",)]

    all_sections = []
    file_count = 0
    for f in md_files:
        try:
            sections = parse_markdown_sections(str(f), str(docs_root))
            all_sections.extend(sections)
            file_count += 1
        except Exception as e:
            print(f"  [WARN] Failed to parse {f}: {e}")

    if not all_sections:
        raise ValueError(f"No document sections found in {docs_path}")

    print(f"  Parsed {file_count} files, {len(all_sections)} sections")

    # Build link graph (llm_wiki's graph expansion foundation)
    print("  Building link graph...")
    link_graph = _build_link_graph(all_sections, str(docs_root))
    edge_count = sum(len(v) for v in link_graph.values())
    print(f"  Link graph: {len(link_graph)} nodes, {edge_count} edges")

    # Tokenize all sections for Chinese-aware TF-IDF
    tokenized = [tokenize_chinese(s.content) for s in all_sections]

    vectorizer = TfidfVectorizer(
        max_features=10000,
        ngram_range=(1, 2),
        sublinear_tf=True,
    )
    tfidf_matrix = vectorizer.fit_transform(tokenized)

    index = DocIndex(
        sections=all_sections,
        tfidf_matrix=tfidf_matrix,
        tfidf_vectorizer=vectorizer,
        link_graph=link_graph,
        metadata={
            "source_path": str(docs_path),
            "file_count": file_count,
            "section_count": len(all_sections),
            "total_chars": sum(s.char_count for s in all_sections),
            "link_graph_nodes": len(link_graph),
            "link_graph_edges": edge_count,
            "docs_hash": _compute_docs_hash(docs_path),
        },
    )

    index.save(index_path)
    print(f"  Index saved to {index_path}")
    print(f"  Stats: {index.metadata}")
    return index


def search_index(index: DocIndex, query: str, top_k: int = 5) -> list[dict]:
    """Legacy search: TF-IDF cosine similarity + keyword boost."""
    query_tokenized = tokenize_chinese(query)
    query_vec = index.tfidf_vectorizer.transform([query_tokenized])
    scores = cosine_similarity(query_vec, index.tfidf_matrix).flatten()

    query_lower = query.lower()
    for i, section in enumerate(index.sections):
        if query_lower in section.title.lower():
            scores[i] *= 1.5
        if query_lower in section.content.lower():
            scores[i] *= 1.2

    top_indices = np.argsort(scores)[::-1][:top_k]
    results = []
    for idx in top_indices:
        if scores[idx] > 0:
            results.append({"section": index.sections[idx], "score": float(scores[idx])})
    return results


# ---------------------------------------------------------------------------
# Embedding-based indexing & hybrid search
# ---------------------------------------------------------------------------

def _embed_sections(sections: list[DocSection], batch_size: int = 64) -> np.ndarray:
    """Embed all sections using the configured embedding API."""
    from .config import (
        EMBEDDING_API_BASE, EMBEDDING_API_KEY, EMBEDDING_MODEL,
        EMBEDDING_BATCH_SIZE, EMBEDDING_CUSTOM_HEADERS,
    )

    if not EMBEDDING_API_BASE or not EMBEDDING_MODEL:
        raise ValueError("EMBEDDING_API_BASE and EMBEDDING_MODEL must be set")

    from openai import OpenAI
    import httpx

    custom_headers = {}
    raw = EMBEDDING_CUSTOM_HEADERS.strip() if EMBEDDING_CUSTOM_HEADERS else ""
    if raw:
        for pair in raw.split(","):
            pair = pair.strip()
            if ":" in pair:
                k, v = pair.split(":", 1)
                custom_headers[k.strip()] = v.strip()
    if not custom_headers:
        from .config import LLM_CUSTOM_HEADERS
        raw = LLM_CUSTOM_HEADERS.strip()
        if raw:
            for pair in raw.split(","):
                pair = pair.strip()
                if ":" in pair:
                    k, v = pair.split(":", 1)
                    custom_headers[k.strip()] = v.strip()

    http_client = httpx.Client(headers=custom_headers) if custom_headers else None
    api_key = EMBEDDING_API_KEY or os.getenv("LLM_API_KEY", "")
    client = OpenAI(base_url=EMBEDDING_API_BASE, api_key=api_key, http_client=http_client)

    texts = [s.content for s in sections]
    all_embeddings = []
    for i in range(0, len(texts), batch_size):
        batch = texts[i:i + batch_size]
        resp = client.embeddings.create(model=EMBEDDING_MODEL, input=batch)
        for item in resp.data:
            all_embeddings.append(item.embedding)
        print(f"  Embedded {min(i + batch_size, len(texts))}/{len(texts)} sections")

    return np.array(all_embeddings, dtype=np.float32)


def build_index_with_embeddings(docs_path: str, index_path: str) -> DocIndex:
    """Build index with both TF-IDF and embedding vectors + link graph."""
    from .config import EMBEDDING_BATCH_SIZE

    index = build_index(docs_path, index_path)

    print("  Building embedding index...")
    try:
        index.embedding_matrix = _embed_sections(index.sections, EMBEDDING_BATCH_SIZE)
        print(f"  Embedding matrix shape: {index.embedding_matrix.shape}")
        index.metadata["embedding_model"] = os.getenv("EMBEDDING_MODEL", "")
        index.metadata["embedding_dim"] = index.embedding_matrix.shape[1]
    except Exception as e:
        print(f"  [WARN] Embedding failed: {e}. Falling back to TF-IDF only.")
        index.embedding_matrix = None

    index.save(index_path)
    return index


def _embed_query(query: str) -> np.ndarray:
    """Embed a single query string. Returns (dim,) array."""
    from .config import (
        EMBEDDING_API_BASE, EMBEDDING_API_KEY, EMBEDDING_MODEL,
        EMBEDDING_CUSTOM_HEADERS,
    )
    from openai import OpenAI
    import httpx

    custom_headers = {}
    raw = EMBEDDING_CUSTOM_HEADERS.strip() if EMBEDDING_CUSTOM_HEADERS else ""
    if raw:
        for pair in raw.split(","):
            pair = pair.strip()
            if ":" in pair:
                k, v = pair.split(":", 1)
                custom_headers[k.strip()] = v.strip()
    if not custom_headers:
        from .config import LLM_CUSTOM_HEADERS
        raw = LLM_CUSTOM_HEADERS.strip()
        if raw:
            for pair in raw.split(","):
                pair = pair.strip()
                if ":" in pair:
                    k, v = pair.split(":", 1)
                    custom_headers[k.strip()] = v.strip()

    http_client = httpx.Client(headers=custom_headers) if custom_headers else None
    api_key = EMBEDDING_API_KEY or os.getenv("LLM_API_KEY", "")
    client = OpenAI(base_url=EMBEDDING_API_BASE, api_key=api_key, http_client=http_client)
    resp = client.embeddings.create(model=EMBEDDING_MODEL, input=[query])
    return np.array(resp.data[0].embedding, dtype=np.float32)


# ---------------------------------------------------------------------------
# 2-hop graph expansion (llm_wiki's key advantage)
# ---------------------------------------------------------------------------

def _graph_expand(index: DocIndex, seed_ids: set, max_extra: int = 6) -> list[str]:
    """Expand search results via 2-hop graph traversal.

    Like llm_wiki's Phase 2 graph expansion: take seed sections (from search),
    traverse link graph with decay, return additional section IDs not in seeds.

    Signals used:
    - Direct link (weight ×3.0): sections linked via markdown links
    - Source overlap (weight ×4.0): sections from same source file
    (Both are already encoded in link_graph during build time)

    Decay: 2nd hop nodes get 0.5× score multiplier.
    """
    if not index.link_graph:
        return []

    # Score all reachable nodes
    candidate_scores = defaultdict(float)

    # 1-hop: direct neighbors of seeds
    for seed_id in seed_ids:
        neighbors = index.link_graph.get(seed_id, set())
        for neighbor in neighbors:
            if neighbor not in seed_ids:
                candidate_scores[neighbor] += 1.0

    # 2-hop: neighbors of 1-hop, with decay
    hop1_ids = set(candidate_scores.keys())
    for hop1_id in hop1_ids:
        neighbors = index.link_graph.get(hop1_id, set())
        for neighbor in neighbors:
            if neighbor not in seed_ids and neighbor not in hop1_ids:
                candidate_scores[neighbor] += 0.5  # decay

    # Sort by score, take top max_extra
    sorted_candidates = sorted(candidate_scores.items(), key=lambda x: -x[1])
    return [cid for cid, _ in sorted_candidates[:max_extra]]


# ---------------------------------------------------------------------------
# Full hybrid search with graph expansion
# ---------------------------------------------------------------------------

def search_hybrid(index: DocIndex, query: str, top_k: int = 5, rrf_k: int = 60,
                  graph_expand_k: int = 6) -> list[dict]:
    """Hybrid search: TF-IDF + Embedding (RRF) + 2-hop graph expansion.

    Three phases (like llm_wiki):
    1. Text search (TF-IDF + embedding via RRF)
    2. Graph expansion (2-hop from search seeds)
    3. Merge: graph-expanded sections get a relevance boost
    """
    # --- Phase 1: TF-IDF branch ---
    query_tokenized = tokenize_chinese(query)
    query_vec = index.tfidf_vectorizer.transform([query_tokenized])
    tfidf_scores = cosine_similarity(query_vec, index.tfidf_matrix).flatten()

    query_lower = query.lower()
    for i, section in enumerate(index.sections):
        if query_lower in section.title.lower():
            tfidf_scores[i] *= 3.0
        elif any(w in section.title.lower() for w in query_lower.split() if len(w) > 1):
            tfidf_scores[i] *= 2.0
        if query_lower in section.content.lower():
            tfidf_scores[i] *= 1.3
        if section.source_file.lower().endswith("readme.md") or "概览" in section.title:
            tfidf_scores[i] *= 0.6
        if section.char_count < 200:
            tfidf_scores[i] *= 0.5

    tfidf_ranking = np.argsort(tfidf_scores)[::-1]

    # Embedding branch
    emb_ranking = None
    if index.embedding_matrix is not None:
        try:
            query_emb = _embed_query(query)
            q_norm = query_emb / (np.linalg.norm(query_emb) + 1e-8)
            emb_norms = np.linalg.norm(index.embedding_matrix, axis=1, keepdims=True)
            emb_normalized = index.embedding_matrix / (emb_norms + 1e-8)
            emb_scores = (emb_normalized @ q_norm).flatten()
            emb_ranking = np.argsort(emb_scores)[::-1]
        except Exception as e:
            print(f"  [WARN] Embedding query failed: {e}")

    # --- RRF merge ---
    rrf_scores = np.zeros(len(index.sections), dtype=np.float64)
    for rank, idx in enumerate(tfidf_ranking):
        rrf_scores[idx] += 1.0 / (rrf_k + rank + 1)
    if emb_ranking is not None:
        for rank, idx in enumerate(emb_ranking):
            rrf_scores[idx] += 1.0 / (rrf_k + rank + 1)

    # Take top-K from text search
    initial_ranking = np.argsort(rrf_scores)[::-1][:top_k]

    # Build section_id → index mapping
    id_to_idx = {s.id: i for i, s in enumerate(index.sections)}

    # --- Phase 2: Graph expansion ---
    seed_ids = set(index.sections[idx].id for idx in initial_ranking if rrf_scores[idx] > 0)
    expanded_ids = _graph_expand(index, seed_ids, max_extra=graph_expand_k)

    # Convert expanded IDs to indices, give them a moderate RRF score boost
    expanded_indices = []
    for eid in expanded_ids:
        if eid in id_to_idx:
            idx = id_to_idx[eid]
            if rrf_scores[idx] == 0:
                # Graph-expanded section not in text search results
                # Give it a moderate score (average of top-K min score)
                min_score = min(rrf_scores[idx] for idx in initial_ranking if rrf_scores[idx] > 0)
                rrf_scores[idx] = min_score * 0.7  # graph boost: 70% of weakest text result
            expanded_indices.append(idx)

    # --- Phase 3: Final merge ---
    # Combine initial + expanded, sort by score
    all_candidate_indices = list(initial_ranking) + expanded_indices
    # Deduplicate
    all_candidate_indices = list(dict.fromkeys(all_candidate_indices))
    all_candidate_indices.sort(key=lambda idx: -rrf_scores[idx])

    results = []
    for idx in all_candidate_indices:
        if rrf_scores[idx] > 0:
            results.append({
                "section": index.sections[idx],
                "score": float(rrf_scores[idx]),
                "source": "graph" if idx in expanded_indices else "search",
            })

    return results
