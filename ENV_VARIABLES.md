# Переменные окружения для Render

Этот файл содержит список всех переменных окружения, которые необходимо настроить в панели управления Render для работы бота.

## Обязательные переменные

### Telegram Bot
- **`BOT_TOKEN`** - Токен Telegram бота, полученный от @BotFather

### OpenAI API
- **`OPENAI_API_KEY`** - API ключ OpenAI для использования GPT-4o-mini модели
  - Получить можно на: https://platform.openai.com/api-keys

### Google Calendar OAuth2
- **`GOOGLE_CLIENT_ID`** - Client ID из Google Cloud Console
- **`GOOGLE_CLIENT_SECRET`** - Client Secret из Google Cloud Console
- **`GOOGLE_REDIRECT_URI`** - (Опционально) Redirect URI для OAuth2. По умолчанию: `urn:ietf:wg:oauth:2.0:oob`

  **Инструкция по настройке Google OAuth2:**
  1. Перейдите в [Google Cloud Console](https://console.cloud.google.com/)
  2. Создайте новый проект или выберите существующий
  3. Включите Google Calendar API
  4. Перейдите в "Credentials" → "Create Credentials" → "OAuth client ID"
  5. Выберите "Desktop app" как тип приложения
  6. Скопируйте Client ID и Client Secret

### Amplitude Analytics
- **`AMPLITUDE_API_KEY`** - API ключ Amplitude для аналитики
  - Получить можно на: https://amplitude.com/

## Опциональные переменные

### База данных
- **`DB_PATH`** - Путь к файлу SQLite базы данных (по умолчанию: `tasks.db`)

### Настройки бота
- **`DEFAULT_TZ`** - Часовой пояс по умолчанию (по умолчанию: `Europe/Rome`)
- **`SUMMARY_HOUR`** - Час для ежедневной сводки (по умолчанию: `8`)
- **`SUMMARY_MINUTE`** - Минута для ежедневной сводки (по умолчанию: `0`)
- **`REMIND_MINUTES`** - За сколько минут до задачи отправлять напоминание (по умолчанию: `30`)
- **`REMINDERS_ENABLED`** - Включены ли напоминания по умолчанию (по умолчанию: `1`)
- **`DEFAULT_LANG`** - Язык по умолчанию: `ru` или `en` (по умолчанию: `ru`)

### Singleton режим (для Render)
- **`PRIMARY_INSTANCE_ID`** - ID основного инстанса (для предотвращения дублирования)
- **`INSTANCE_PREFERRED`** - Предпочтительный инстанс: `min` (для использования минимального индекса)

## Пример настройки в Render

1. Перейдите в настройки вашего сервиса на Render
2. Откройте вкладку "Environment"
3. Добавьте все обязательные переменные:
   ```
   BOT_TOKEN=your_telegram_bot_token
   OPENAI_API_KEY=sk-...
   GOOGLE_CLIENT_ID=your_client_id.apps.googleusercontent.com
   GOOGLE_CLIENT_SECRET=your_client_secret
   AMPLITUDE_API_KEY=your_amplitude_api_key
   ```
4. Сохраните изменения и перезапустите сервис

## Проверка настроек

После настройки всех переменных, бот должен:
- ✅ Успешно запускаться без ошибок
- ✅ Отвечать на команду `/start`
- ✅ Парсить задачи с помощью AI
- ✅ Создавать события в Google Calendar (после авторизации)
- ✅ Отправлять события в Amplitude

