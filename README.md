# WakeUpThroughServer

Telegram-бот для удалённого управления несколькими компьютерами: включение через Wake-on-LAN, проверка доступности, выключение и перезагрузка по SSH.

## Возможности

- отдельный список компьютеров для каждого Telegram ID;
- администраторы видят все компьютеры;
- подтверждение выключения и перезагрузки;
- сетевые команды не блокируют обработку остальных сообщений;
- повторное подключение к Telegram без ограничения числа попыток;
- журналирование ошибок без показа технических подробностей пользователю;
- автоматический перезапуск процесса через `systemd`.

## Установка

Требуется Python 3.10 или новее.

```bash
python -m venv venv
./venv/bin/pip install -r requirements.txt
cp .env.example .env
```

На Windows путь активации окружения — `venv\Scripts\activate`.

## Конфигурация

`.env` содержит:

| Переменная | Описание |
|---|---|
| `BOT_TOKEN` | токен Telegram-бота от BotFather |
| `ADMIN_USERNAMES` | Telegram-ники администраторов через запятую, `@` необязателен |
| `ADMIN_USER_IDS` | более надёжный список числовых Telegram ID администраторов |
| `COMPUTERS` | JSON-массив компьютеров |
| `EXTRA_COMPUTERS` | необязательный дополнительный JSON-массив компьютеров |
| `LOG_LEVEL` | необязательный уровень журнала, по умолчанию `INFO` |

`@alacrityy0` уже добавлен в код как администратор по умолчанию. После первого `/start` рекомендуется записать показанный ботом числовой ID в `ADMIN_USER_IDS`: ID не меняется при смене Telegram-ника.

Пример одного компьютера:

```json
{
  "name": "MyPC",
  "mac": "AA:BB:CC:DD:EE:FF",
  "ip": "192.168.1.100",
  "broadcast": "192.168.1.255",
  "ssh_user": "User",
  "os": "Windows",
  "users": [123456789],
  "usernames": ["pudj2003"]
}
```

Поля `users` и `usernames` задают обычных пользователей компьютера по числовому ID или Telegram-нику. `broadcast` — широковещательный адрес локальной подсети; если он не указан, используется `255.255.255.255`. Администратору компьютер доступен независимо от этих полей. Если `ssh_user` пуст, бот показывает только включение и статус. Имена компьютеров должны быть уникальными.

## Добавление ещё одного Windows-компьютера

1. Скопируйте на компьютер `get_pc_info.py` и `setup_ssh.bat`.
2. Запустите `python get_pc_info.py`. Если выведено несколько адаптеров, выберите проводной Ethernet-адаптер с включённым Wake-on-LAN.
3. Запустите `setup_ssh.bat` от имени администратора — он установит OpenSSH Server и серверный публичный ключ.
4. Включите Wake-on-LAN в BIOS/UEFI и в свойствах сетевого адаптера Windows.
5. Добавьте выведенный JSON-объект в массив `COMPUTERS` в `.env`. При необходимости внесите Telegram ID владельца в `users`.
6. Обновите файлы на сервере и перезапустите сервис.

## Проверка

```bash
python -m unittest discover -s tests -v
python -m py_compile bot.py get_pc_info.py
```

Запуск вручную:

```bash
python bot.py
```

Команды Telegram:

- `/start` — список доступных компьютеров;
- `/admin` — список всех компьютеров, только для администратора.

## Автозапуск и восстановление

Готовый unit-файл находится в `deploy/wakebot.service`. Он перезапускает бот через 10 секунд после любого завершения и не прекращает попытки после серии ошибок.

```bash
sudo cp deploy/wakebot.service /etc/systemd/system/wakebot.service
sudo systemctl daemon-reload
sudo systemctl enable --now wakebot.service
sudo systemctl restart wakebot.service
systemctl status wakebot.service --no-pager
journalctl -u wakebot.service -n 100 --no-pager
```

Unit рассчитан на каталог `/home/egor/wakeupthroughserver` и окружение `venv` внутри него.
