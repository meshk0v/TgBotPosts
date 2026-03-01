# Telegram relay на Pyrogram (user account)

Сервис запускается от лица Telegram-аккаунта (не bot token), слушает новые посты в заданных каналах и отправляет их в целевой чат/тему.

## Локальный запуск (без Docker)

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
python3 main.py
```

## Docker запуск

```bash
cp .env.example .env
docker compose up -d --build
```

Тома в `docker-compose.yml`:

- `./data:/app/data` — файлы сессии Pyrogram (`*.session`)
- `./logs:/app/logs` — лог-файл сервиса

Лог записывается в путь из `LOG_FILE` (по умолчанию `/app/logs/relay.log`), то есть физически на сервере это `./logs/relay.log`.

## Первый вход аккаунта в контейнере

При первом запуске нужна авторизация Telegram (телефон, код, возможно 2FA). Если контейнер уже запущен:

```bash
docker compose logs -f relay
```

Если нужно в интерактивном режиме:

```bash
docker compose run --rm relay python main.py
```

После успешного входа сессия сохранится в `./data`, и дальше можно запускать сервис в фоне.

## Настройки `.env`

- `API_ID`, `API_HASH` — с https://my.telegram.org
- `SESSION_NAME` — имя сессии
- `SESSION_DIR` — директория хранения сессии
- `SOURCE_CHATS` — каналы-источники через запятую (`@name` или `-100...`)
- `TARGET_CHAT_ID` — целевой чат
- `TARGET_TOPIC_ID` — ID темы (если нужен топик)
- `COPY_MODE=true/false`
- `LOG_FILE` — путь к лог-файлу

## Поведение отправки

- `COPY_MODE=true`: копирование сообщения (`copy_message`) с маршрутизацией в тему.
- `COPY_MODE=false`: raw MTProto forward (`messages.forwardMessages` с `top_msg_id`).
- Если forward не проходит (ограничения источника/прав), выполняется fallback на copy.
