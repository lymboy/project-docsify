FROM python:3.11-slim

WORKDIR /app

# Install Python dependencies
COPY server/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY server/ ./server/
COPY docqa-widget/ ./docqa-widget/

# Copy docsify docs content
COPY docs/ ./docs/

# Pre-built index (rebuilds automatically on startup if docs changed)
COPY index/ ./index/

# Default environment — override at runtime: docker run -e LLM_API_KEY=xxx
ENV DOCS_DIR=/app/docs \
    KB_DOCS_PATH=/app/docs \
    INDEX_PATH=/app/index \
    PURPOSE_FILE=/app/docs/purpose.md \
    HOST=0.0.0.0 \
    PORT=8000 \
    LLM_API_KEY= \
    EMBEDDING_API_KEY=

EXPOSE 8000

CMD ["python", "-m", "server"]
