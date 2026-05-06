# Runbook: переезд на каталог точек из xlsx

Цель: заменить авто-создание `Location` из ATS-потока на статический каталог,
импортируемый из xlsx («База_адресов_с_телефонами.xlsx»). Точки получают
уникальное имя `displayName` (например «Аполо 02»), могут иметь несколько
телефонов; один телефон может относиться к нескольким точкам (выездной
менеджер). Резолв точки на звонке: AI-extract имени из транскрипта →
fallback по телефону → `null` если неоднозначно.

## Что в коммите

Backend:
- `bootstrap.py` переведён на GraphQL `/metadata`.
- `LOCATION` теперь:
  - `skipNameField=true` (не создавать дефолтное `name`),
  - `displayName TEXT` (`isUnique=true`, `isLabelIdentifier=true`, `isNullable=false`),
  - `phone PHONES` (`isNullable=true`),
  - `anydeskId TEXT`.
- Person.locationRel + cache-поля удалены из спеки и адаптера.
- `_resolve_person_and_location` больше не создаёт Location.
- AI port: `extract_location` → `extract_location_name(text, known_names)` (enum-constrained).

Скрипты:
- `scripts/import_locations_xlsx.py` — идемпотентный импорт каталога.
- `scripts/wipe_legacy_locations.py` — снос всех существующих Location.
- `scripts/migrate_drop_legacy_fields.py` — удаление старых полей Person.

## Порядок раскатки

> ⚠️ ATS-токены в `/app/24ondoc/.env` не трогать. Backend перезапускаем
> ровно один раз в самом конце.

### 0. Подготовка

```bash
ssh root@89.124.95.91
cd /app/24ondoc
git fetch origin
git checkout feat/access-control-analytics
git reset --hard origin/feat/access-control-analytics

# Перенести xlsx на сервер
scp /home/oblacko/Загрузки/База_адресов_с_телефонами.xlsx \
    root@89.124.95.91:/tmp/locations.xlsx
```

### 1. Все скрипты прогоняем DRY-RUN сначала

```bash
# Подготовить env-файл с одним только Twenty
grep -E '^(TWENTY_BASE_URL|TWENTY_API_KEY)=' /app/24ondoc/.env > /tmp/migr.env

# Скопировать скрипты + xlsx внутрь backend container
docker cp scripts/import_locations_xlsx.py    24ondoc-backend-1:/tmp/
docker cp scripts/wipe_legacy_locations.py    24ondoc-backend-1:/tmp/
docker cp scripts/migrate_drop_legacy_fields.py 24ondoc-backend-1:/tmp/
docker cp /tmp/locations.xlsx                 24ondoc-backend-1:/tmp/

# openpyxl нужен для парсера xlsx
docker exec 24ondoc-backend-1 pip install --quiet openpyxl

# DRY-RUN — ничего не пишет
docker exec --env-file /tmp/migr.env 24ondoc-backend-1 \
  python3 /tmp/wipe_legacy_locations.py
docker exec --env-file /tmp/migr.env 24ondoc-backend-1 \
  python3 /tmp/migrate_drop_legacy_fields.py
docker exec --env-file /tmp/migr.env 24ondoc-backend-1 \
  python3 /tmp/import_locations_xlsx.py /tmp/locations.xlsx
```

Сверь:
- wipe-скрипт показывает ~140 точек к удалению,
- migrate-скрипт показывает 4 поля person.location*,
- import-скрипт показывает «parsed rows: 402», «existing locations: …»
  и всё, что планирует CREATE/PATCH.

### 2. Применить миграцию схемы (без рестарта backend)

```bash
# Запустить bootstrap — он добавит displayName/anydeskId, снимет phone NOT NULL,
# переключит labelIdentifier на displayName.
docker exec --env-file /tmp/migr.env 24ondoc-backend-1 \
  python3 -m twenty_integration.infrastructure.bootstrap_cli
```

Bootstrap idempotent — можно прогнать несколько раз.

Проверка успеха через GraphQL:
```bash
docker exec --env-file /tmp/migr.env 24ondoc-backend-1 python3 -c "
import os, httpx
r = httpx.post(os.environ['TWENTY_BASE_URL']+'/metadata',
  headers={'Authorization':'Bearer '+os.environ['TWENTY_API_KEY']},
  json={'query': '{ objects(paging:{first:50}, filter:{}) { edges { node { nameSingular labelIdentifierFieldMetadataId fieldsList { name isUnique isNullable } } } } }'},
  timeout=30).json()
for e in r['data']['objects']['edges']:
    if e['node']['nameSingular']=='location':
        print('labelIdentifier:', e['node']['labelIdentifierFieldMetadataId'])
        for f in e['node']['fieldsList']:
            print(' ', f['name'], 'unique=', f['isUnique'], 'nullable=', f['isNullable'])
"
```

Ожидаем: появилось `displayName`, `anydeskId`; `phone.isNullable=true`;
`labelIdentifier` указывает на `displayName.id`.

### 3. Снос legacy Location

```bash
docker exec --env-file /tmp/migr.env 24ondoc-backend-1 \
  python3 /tmp/wipe_legacy_locations.py --apply
```

Лог покажет «deleted N/N». FK `ON DELETE SET NULL` — Person/Task/CallRecord
не пропадают, у них просто обнулится `locationRelId`.

### 4. Удалить legacy-поля Person

```bash
docker exec --env-file /tmp/migr.env 24ondoc-backend-1 \
  python3 /tmp/migrate_drop_legacy_fields.py --apply
```

### 5. Импорт каталога

```bash
docker exec --env-file /tmp/migr.env 24ondoc-backend-1 \
  python3 /tmp/import_locations_xlsx.py /tmp/locations.xlsx --apply
```

Импорт идёт ~5 минут (402 записи, лимит 1.5 RPS). По окончании ожидаем
`created: 402, updated: 0, errors: 0`.

### 6. Рестарт backend

> Только сейчас, и ровно один раз. ATS2 токены в .env не модифицировать.

```bash
docker compose restart backend
docker compose logs -f --tail 100 backend | grep -E "(ATS2|extract_location|location)"
```

Смотри 5–10 минут: AI-резолв точки на звонках работает (имя точки берётся
из enum); CallRecord идёт с `locationRelId` если резолв успешен; на
ambiguous → null без ошибок.

## Откат

Все four шага обратимы:
- bootstrap не откатывает поля автоматически — нужно руками удалить
  `displayName/anydeskId` через GraphQL (`deleteOneField`) и вернуть
  `phone.isNullable=false` через `updateOneField`.
- удалённые Person.location* поля можно создать заново через bootstrap
  предыдущей версии (но данных в них уже не будет).
- импортированные Location удаляются через `wipe_legacy_locations.py`.

В одну сторону катимся через git revert + рестарт backend, в обратную —
не снося Twenty workspace схему через CLI.

## Дальнейшая работа (вне этого деплоя)

- N:M phone↔location требует scan дополнительных номеров локально (REST не
  фильтрует jsonb). Если каталог вырастет до тысяч точек — переехать на
  GraphQL workspace API с прямым фильтром или материализовать индекс
  `phone → list[location_id]` в Redis.
- Person.locationRel удалён; если в UI остались views/dashboards со
  ссылкой на это поле — пересохранить вьюхи.
