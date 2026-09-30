# Быстрый старт

Нужен Python 3.12 и токен API из кабинета продавца (Настройки, Доступ к API). Токен
в примерах ниже подставляется через переменную окружения, в файлы клиентов его лучше
не вписывать.

## Установка

```
pip install git+https://github.com/S-typy/WB-MCP
wb-mcp --version
```

Или через uv без установки в систему: `uvx --from git+https://github.com/S-typy/WB-MCP wb-mcp`.

## Проверка

```
export WB_TOKEN=eyJ...
wb-mcp --check
wb-mcp --smoke
```

Первая команда собирает сервер и печатает сводку, вторая делает несколько безопасных
запросов к WB и показывает, что токен и лимиты работают.

## Claude Desktop

Файл `claude_desktop_config.json` (в настройках: Developer, Edit Config):

```json
{
  "mcpServers": {
    "wb": {
      "command": "wb-mcp",
      "args": ["--profiles", "orders,content"],
      "env": {"WB_TOKEN": "eyJ..."}
    }
  }
}
```

После перезапуска Claude Desktop инструменты wb_* появятся в списке.

## Claude в терминале

Команда `claude` умеет регистрировать MCP-серверы сама:

```
claude mcp add wb -e WB_TOKEN=eyJ... -- wb-mcp --profiles orders,content
claude mcp get wb
```

## Cursor

Файл `.cursor/mcp.json` в проекте или `~/.cursor/mcp.json`:

```json
{
  "mcpServers": {
    "wb": {"command": "wb-mcp", "args": ["--profiles", "orders"], "env": {"WB_TOKEN": "eyJ..."}}
  }
}
```

## OpenClaw

Сервер ставится на ту же машину, где работает Gateway, токен кладётся в `~/.openclaw/.env`
строкой `WB_TOKEN=eyJ...`, а в определении сервера подставляется через `${WB_TOKEN}`:

```
openclaw mcp add wb \
  --command wb-mcp \
  --arg --profiles --arg orders,content \
  --env 'WB_TOKEN=${WB_TOKEN}'
openclaw mcp doctor wb --probe
```

Команда `add` сама подключается к серверу перед сохранением, `doctor --probe` показывает
список инструментов. То же самое прямо в `openclaw.json`:

```json5
{
  mcp: {
    servers: {
      wb: {
        command: "wb-mcp",
        args: ["--profiles", "orders,content"],
        env: { WB_TOKEN: "${WB_TOKEN}" },
        toolFilter: { include: ["wb_*"] },
      },
    },
  },
}
```

Если сервер крутится на другой машине в HTTP-режиме, вместо `command` указываются
`url: "http://host:8000/mcp"`, `transport: "streamable-http"` и заголовок
`Authorization: Bearer <ключ клиента>` из `[[clients]]`. После правки конфига
`openclaw mcp reload`, чтобы агент увидел новые инструменты на следующем ходу.

## Несколько кабинетов

Вместо переменной окружения — файл настроек, путь передаётся через `--config`:

```toml
[server]
profiles = ["orders", "content"]

[[sellers]]
alias = "main"
token_env = "WB_TOKEN_MAIN"

[[sellers]]
alias = "second"
token_file = "~/.wb-mcp/second.token"
```

В инструментах кабинет указывается аргументом `seller`; при одном кабинете его можно опускать.

## Docker

```
docker build -t wb-mcp .
docker run -d --name wb-mcp -p 8000:8000 -v wb-data:/data -e WBMCP_KEY_AGENT=... wb-mcp
```

Контейнер поднимает HTTP-режим и ждёт настройки в `/data/wb-mcp.toml` с ключами клиентов
в `[[clients]]`; клиенты ходят на `http://host:8000/mcp` с bearer-ключом.

## Что дальше

`wb_sellers` покажет кабинеты и права токенов, `wb_api_search` найдёт нужный метод,
`wb_api_describe` отдаст его схему. Готовые инструменты для частых задач перечислены
в README.
