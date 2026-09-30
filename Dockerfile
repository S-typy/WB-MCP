FROM python:3.12-slim

WORKDIR /src
COPY pyproject.toml MANIFEST.in README.md LICENSE ./
COPY wbmcp ./wbmcp
RUN pip install --no-cache-dir ".[crypto]" && rm -rf /src

# данные сервера (реестр, журнал, аудит, файлы) живут на томе
ENV WBMCP_DATA_DIR=/data
VOLUME ["/data"]
WORKDIR /data
EXPOSE 8000

ENTRYPOINT ["wb-mcp"]
CMD ["--transport", "http", "--host", "0.0.0.0", "--port", "8000", "--config", "/data/wb-mcp.toml"]
