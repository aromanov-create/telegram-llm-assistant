# My Assistant Bot

Telegram-бот, который превращает Telegram в личную точку входа для повседневных задач через LLM.

Проект предназначен для сценария, где пользователю удобнее написать, надиктовать или прислать фото/видео в Telegram, а дальше бот:
- принимает запрос;
- при необходимости расшифровывает голос через Whisper;
- передаёт задачу в `claude` CLI;
- возвращает итоговый ответ обратно в Telegram.

## Зачем это вообще нужно

Идея сервиса простая: Telegram уже у большинства всегда под рукой, а значит он может быть удобным интерфейсом к персональному AI-ассистенту.

Вместо отдельного UI, браузерной вкладки или ручного запуска CLI-команд вы можете использовать обычный чат с ботом как универсальный вход для:
- быстрых поручений;
- разбора входящих материалов;
- голосовых заметок и черновиков;
- задач, которые проще сформулировать на ходу с телефона.

## Практические сценарии использования

Бот можно использовать как:

- личного ассистента для быстрых текстовых задач;
- голосовой inbox для мыслей, поручений и напоминаний;
- прослойку между Telegram и `claude` CLI;
- инструмент для первичного разбора фото и видео;
- мобильный интерфейс к локально запущенной автоматизации.

Примеры практического применения:

- надиктовать идею, а в ответ получить структурированный план;
- прислать фото документа, скриншота или интерфейса и попросить кратко объяснить, что на нём;
- отправить видео и попросить кратко описать содержимое по первому кадру;
- переслать сообщение и попросить сформулировать ответ;
- использовать бота как персональную точку входа в свои рабочие процессы, завязанные на `claude` CLI.

## Что умеет бот

Бот принимает:
- текстовые сообщения
- голосовые сообщения
- фото
- видео

Голос расшифровывается через Whisper, после чего запрос передаётся в `claude` CLI. Для фото и видео бот передаёт Claude путь к локальному файлу или извлечённому кадру.

## Как это устроено

В проекте есть два user-level systemd сервиса:

- `assistant-bot.service` — основной бот
- `ssh-proxy-tunnel.service` — пример unit-файла для SSH SOCKS5 туннеля

`assistant-bot.service` стартует из директории проекта, читает секреты из `assistant-bot.env`, работает с автоперезапуском и использует локальный HTTP-прокси `127.0.0.1:8118`.

В коде дополнительно есть watchdog: если Telegram API недоступен слишком долго, процесс завершается, а `systemd` поднимает его заново.

## Структура

- `bot.py` — основной код бота
- `assistant-bot.service` — unit-файл user service для бота
- `ssh-proxy-tunnel.service` — пример unit-файла для SSH туннеля
- `assistant-bot.env.example` — пример env-файла
- `assistant-bot.env` — локальный env-файл с секретами, не коммитится

## Конфигурация

Скопируйте шаблон env-файла:

```bash
cp assistant-bot.env.example assistant-bot.env
```

Заполните `assistant-bot.env`:

```env
BOT_TOKEN=<telegram bot token>
ALLOWED_USER_ID=<telegram user id>
```

Права на файл должны быть ограничены:

```bash
chmod 600 assistant-bot.env
```

## Требования

На машине должны быть доступны:

- `python3`
- Python-библиотеки, которые использует `bot.py`
- `ffmpeg`
- `claude` CLI
- доступный локальный HTTP-прокси на `127.0.0.1:8118`
- настроенный SSH host alias, если вы используете `ssh-proxy-tunnel.service`

## Установка systemd service

Скопировать unit-файлы в user systemd:

```bash
install -m 644 assistant-bot.service ~/.config/systemd/user/assistant-bot.service
install -m 644 ssh-proxy-tunnel.service ~/.config/systemd/user/ssh-proxy-tunnel.service
systemctl --user daemon-reload
```

Включить автозапуск:

```bash
systemctl --user enable ssh-proxy-tunnel.service
systemctl --user enable assistant-bot.service
```

Запустить:

```bash
systemctl --user start ssh-proxy-tunnel.service
systemctl --user start assistant-bot.service
```

## Полезные команды

Статус сервисов:

```bash
systemctl --user status assistant-bot.service
systemctl --user status ssh-proxy-tunnel.service
```

Рестарт бота:

```bash
systemctl --user restart assistant-bot.service
```

Логи бота:

```bash
journalctl --user -u assistant-bot.service -f
```

Логи туннеля:

```bash
journalctl --user -u ssh-proxy-tunnel.service -f
```

Проверка синтаксиса Python:

```bash
python3 -m py_compile bot.py
```

## Ручной запуск без systemd

Для локальной проверки можно запустить напрямую:

```bash
set -a
source assistant-bot.env
set +a
python3 bot.py
```

Но основной режим эксплуатации для этого проекта — через `systemd`.

