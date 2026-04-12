# MTProxy AutoSwitch

`MTProxy AutoSwitch` поднимает локальный MTProto frontend на `127.0.0.1:1443`, собирает прокси из веб-источников и Telegram, проверяет их и автоматически переключает upstream на лучший доступный вариант.

Проект является форком клиента Flowseal:

`https://github.com/Flowseal/tg-ws-proxy`

В оригинальном проекте основной сценарий работы — локальный proxy frontend. В этом форке добавлены:
- парсинг веб- и Telegram-источников
- дедупликация и фильтрация списков
- фоновая проверка доступности и стабильности
- авто-подбор лучшего upstream-прокси
- экспорт рабочих списков

## Что умеет приложение

- Поднимать локальный MTProto proxy для Telegram на `127.0.0.1:1443`
- Автоматически выбирать лучший upstream MTProto proxy
- Собирать MTProto и SOCKS5 из веб-источников
- Парсить публичные Telegram-каналы через `t.me/s/...`
- Парсить Telegram-каналы, группы, сообщения и ветки через Telegram API после входа в аккаунт
- Проверять прокси в фоне без полного обновления списка
- Делать `deep media check` и строгую media-проверку для сложных сетей
- Отправлять список рабочих прокси себе в `Избранное`
- Использовать local Fake TLS listener
- Экспортировать результаты в папку `list`
- Проверять и устанавливать обновления public-версии

## Что лежит в репозитории

- `mtproxy_gui.py` — интерфейс приложения
- `mtproxy_app_backend.py` — runtime, refresh, экспорт, локальный frontend
- `mtproxy_local_proxy.py` — локальный MTProto/Fake TLS frontend и pool upstream-прокси
- `mtproxy_collector.py` — веб-парсинг и первичная проверка прокси
- `mtproxy_telegram.py` — Telegram API, авторизация, Telegram-источники, media-check
- `mtproxy_updater.py` — автообновление public-сборки
- `config.json` — текущий конфиг
- `public_config.json` — шаблон конфига для public release
- `list/` — экспортированные списки и отчеты

## Как пользоваться

1. Запустите приложение.
2. Нажмите `Обновить`, чтобы собрать и проверить прокси.
3. Нажмите `Пуск`, чтобы поднять локальный proxy frontend.
4. Подключите Telegram к локальному proxy:
   `https://t.me/proxy?server=127.0.0.1&port=1443&secret=<secret>`
5. Если нужно, скопируйте ссылку кнопкой на главном экране.

## Когда нужен вход в Telegram

Вход в Telegram не нужен для:
- обычного веб-парса сайтов
- работы локального proxy frontend

Вход в Telegram нужен для:
- Telegram-источников, где нужен доступ через Telegram API
- приватных каналов, групп и веток
- `deep media check`
- строгой media-проверки
- отправки списка рабочих прокси в `Избранное`

Сессия пользователя хранится локально и в зашифрованном виде.

## Источники

Поддерживаются:
- веб-страницы с прямыми `https://t.me/proxy?...`
- публичные Telegram-страницы `https://t.me/s/...`
- Telegram API-источники вида `https://t.me/<channel>`
- Telegram API-источники вида `https://t.me/<channel>/<message_id>`
- Telegram API-ветки и сообщения из групп, если у аккаунта есть доступ

## Файлы результата

- `list/proxy_list.txt` — рабочие MTProto-прокси
- `list/all_list.txt` — все найденные MTProto-прокси
- `list/rejected_list.txt` — отсеянные MTProto-прокси
- `list/socks5_list.txt` — найденные SOCKS5
- `list/report.json` — подробный отчет

## Сборка Windows

### Public release

```bat
build_release_public.bat
```

Результат:

```text
release-public\MTProxyAutoSwitchPublic\MTProxyAutoSwitchPublic.exe
release-public\MTProxyAutoSwitchPublic.zip
```

### Private/local release

```bat
build_release.bat
```

Результат:

```text
release\MTProxyAutoSwitch\MTProxyAutoSwitch.exe
```

## Сборка macOS

### Public release

```bash
chmod +x build_release_public_macos.sh
./build_release_public_macos.sh
```

### Private/local release

```bash
chmod +x build_release_macos.sh
./build_release_macos.sh
```

Для macOS сборку нужно выполнять на самой macOS. Из-под Windows корректный `.app` не собирается.

## Зависимости для сборки

- Python 3.11+
- `pip install -r requirements.txt`
- `pip install pyinstaller`

Если в проекте используются `customtkinter`, `telethon`, `requests`, `beautifulsoup4`, `cryptography`, они тоже должны быть установлены в окружении сборки.

## Авторы

- Оригинальный проект Flowseal: `https://github.com/Flowseal/tg-ws-proxy`
- Форк и развитие: `https://github.com/pengvench/MTProxyAutoSwitch`
- Telegram автора: `https://t.me/peppe_poppo`
