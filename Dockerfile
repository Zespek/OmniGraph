# omnigraph MCP server as a shared HTTP service (issue #1143).
#
# Build:  docker build -t omnigraph .
# Run:    docker run -p 8080:8080 -v "$(pwd)/omnigraph-out:/data" omnigraph \
#             /data/graph.json --transport http --host 0.0.0.0 --api-key "$SECRET"
#
# Builds from source so the image includes the Streamable HTTP transport even
# before it lands on PyPI. The graph.json is mounted at runtime (-v), never
# baked into the image.
FROM python:3.12-slim

WORKDIR /app
COPY . /app

# The [mcp] extra pulls mcp + starlette + uvicorn, which the HTTP transport needs.
RUN pip install --no-cache-dir ".[mcp]"

# Run as a non-root user — the server is network-exposed.
RUN useradd --create-home --uid 10001 omnigraph
USER omnigraph

EXPOSE 8080

ENTRYPOINT ["python", "-m", "omnigraph.serve"]
CMD ["/data/graph.json", "--transport", "http", "--host", "0.0.0.0", "--port", "8080"]
