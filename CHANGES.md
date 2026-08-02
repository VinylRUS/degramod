# v4.7.12 — 3 августа 2026

## Безопасность
- **Удалён опасный фолбэк `_fallback_all_true_perms()`**: ранее при выходе из Night Mode / Sanitary Day если у чата не было ни `day_permissions`, ни snapshot, бот выдавал ВСЕ 13 прав в True — включая админские (`can_change_info`, `can_invite_users`, `can_pin_messages`). Любой участник мог менять название чата, пинить сообщения, приглашать людей. Теперь фолбэк — трёхуровневая цепочка: 1) `cs.day_permissions`, 2) системный пресет «Day default», 3) hardcoded-фолбэк = «Day default». Админские права ВСЕГДА False.

## Автопереходы (функционал по ТЗ пользователя)
- **Sanitary → Night**: при выходе из санитарного дня если у чата включён night mode и текущее время попадает в окно — бот сразу входит в night mode, не дожидаясь следующего тика (который раз в минуту). Раньше была минута «голого» day-состояния посреди ночи.
- **Night → Night**: аналогично, если `_exit_night_mode` вызывается когда чат ещё в окне (например, при входе в sanitary) — бот сразу входит обратно в night.
- **Day preset**: во всех случаях выхода (кроме автоперехода в night) восстанавливаются права из day preset, привязанного к чату. Если пресет не задан — используется системный «Day default».

## Day default — разрешённые права
- ✅ Текстовые сообщения (`can_send_messages`)
- ✅ Музыка (`can_send_audios`)
- ✅ Фото (`can_send_photos`)
- ✅ Видео (`can_send_videos`) — НЕ видеосообщения!
- ✅ Стикеры, GIFs, dice (`can_send_other_messages`)
- ✅ Реакции (через `can_send_messages` — отдельного поля в Telegram API нет)
- ❌ Видеосообщения/кружки (`can_send_video_notes`)
- ❌ Документы (`can_send_documents`)
- ❌ Голосовые (`can_send_voice_notes`)
- ❌ Опросы (`can_send_polls`)
- ❌ Превью ссылок (`can_add_web_page_previews`)
- ❌ Смена инфы чата (`can_change_info`) — admin
- ❌ Инвайт юзеров (`can_invite_users`) — admin
- ❌ Пин сообщений (`can_pin_messages`) — admin

## Изменённые файлы
- `bot.py` — удалён `_fallback_all_true_perms`, добавлены `_resolve_day_perms`, `_night_window_active`, `_restore_day_state`, переписаны `_exit_night_mode` (с параметром `allow_auto_enter`) и `_exit_sanitary_day` (с автопереходом в night).
- `web_app.py` — bump `APP_VERSION` до `v4.7.12`.
- `templates/base.html` — запись в changelog.
- `scripts/test_v4712_exit_logic.py` — обновлены test_34/35 (теперь проверяют admin-права OFF), добавлены test_37-42 (автопереходы, приоритет day preset).
- `scripts/test_v460_granular_perms.py` — Test 15 переписан под `_DAY_DEFAULT_HARDCODED`.

## Тесты
- 42/42 в `test_v4712_exit_logic.py` проходят.
- 47/48 в `test_v460_granular_perms.py` (1 сбой — старый version-locked тест на v4.6.1, не связан с фиксом).
- 159/162 в `test_v452_features.py` + `test_v453_night_mode.py` (3 сбоя — version-locked, не связаны с фиксом).
