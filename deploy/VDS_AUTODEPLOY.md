# Production auto-deployment — 24.11.2026

VDS 193.124.115.164 теперь работает на https://силенок.рф/ (443), gibdd.service и отдельном PostgreSQL 17.12. Push main проходит 85 PostgreSQL-тестов CI и запрашивает ограниченный SSH deployment; на VDS выполняется повторный SQLite-прогон на временных фикстурах. Рабочая БД только PostgreSQL, фикстуры не являются рабочим хранилищем.

/etc/gibdd/production.enabled выбирает root-owned update-production-vds.sh через /usr/local/sbin/gibdd-test-deploy (историческое имя диспетчера). Выбор режима и deployment сериализованы lock-файлами. Код/зависимости/тесты запускаются без root. Перед переключением создаются backups/<UTC>-<SHA>/database.dump и media.tar.gz. При ошибке готовности возвращаются прежний systemd drop-in, pg_restore и media. Данные не очищаются; production-контроллер не принимает --reset-data. Исторический SQLite сохранён после миграции как закрытый архив, но не используется рабочим приложением.

Рабочие секреты: /etc/gibdd/production.env, root:gibddtest 0640; PostgreSQL-data /var/lib/gibdd-postgresql/data 0700/gibddpg. Порт 55432 слушает только 127.0.0.1. Доступ к служебному Unix socket — только gibddpg; приложение использует SCRAM по TCP. Рабочие ключи APK отдельно: /var/lib/gibdd-signing root-only и GitHub Secrets APK_RELEASE_*.

Проверка: systemctl status gibdd gibdd-postgresql; curl --fail https://силенок.рф/health; cat /opt/gibdd-test-20261117/deployment.json. Корень сайта и прежний /api/health обслуживает pond; их обновление не входит в этот deployment. Старый порт 4443 перенаправляет на основной HTTPS-домен. Исторический системный пользователь/каталоги с test сохранены для непрерывности прав и резервных копий; это не режим API и не метка APK.

Ниже — историческая инструкция тестового режима. Она применяется только при выключенном production-маркере; удалять маркер на рабочей системе без согласованного плана отката нельзя.

---

# Автообновление существующей тестовой VDS

Сервер: 193.124.115.164. Служба: gibdd-test.service. Адрес: https://силенок.рф:4443/. Это тестовый стенд SQLite; рабочая система использует PostgreSQL по docs/PRODUCTION.md.

## Процесс

Push main запускает .github/workflows/server-vds.yml: полный pytest на изолированном PostgreSQL 17 в GitHub Actions → отдельное задание SSH deployment. Pull request выполняет только тесты. Ручной запуск доступен в Actions; deploy=false оставляет только тестирование.

На VDS контроллер /usr/local/sbin/gibdd-test-deploy забирает main напрямую из GitHub. Запрос устаревшего SHA пропускается, чтобы старый workflow не откатил более новую версию. Повторный запрос уже установленного SHA только проверяет health. Выполнение защищено flock и очередью deployment-задания.

До остановки API создаются новый каталог releases/<UTC>-<SHA>, отдельное окружение environments/<hash-lock>, выполняются тесты на временных SQLite. Затем API кратковременно останавливается, БД с вложениями сохраняется в backups/<UTC>-<SHA>/data.tar.gz, systemd drop-in переключает код и окружение, проверяются /health и состояние службы. При ошибке запуска сохраняются failed-data и восстанавливаются прежние данные и systemd drop-in. Deployment state записывается в deployment.json. Успешный автоматический deployment НЕ очищает БД.

Базовый каталог /opt/gibdd-test-20261117; текущие данные staging-ready. Backups принадлежат root с закрытыми правами. Резервные копии и старые releases автоматически не удаляются: срок хранения и обслуживание определяет администратор. Контроллер отказывается начинать обновление при свободном месте меньше 512 MiB; отслеживайте ёмкость и утверждённые сроки удаления копий. Это резервирование тестового стенда, не готовая политика production-backup.

## Доступ GitHub

Secret VDS_DEPLOY_SSH_KEY — отдельный ed25519-ключ. Variables VDS_DEPLOY_HOST, VDS_DEPLOY_PORT, VDS_DEPLOY_USER, VDS_DEPLOY_KNOWN_HOSTS содержат параметры подключения и закреплённый ключ сервера. Root-пароль в GitHub не передаётся. Host key checking включён.

Ключ подключается как gibdd-deploy. authorized_keys принадлежит root и содержит restrict и forced command /usr/local/bin/gibdd-test-deploy-request. Разрешён только один SHA из 40 hex-символов. Shell, port forwarding и аргумент очистки недоступны. Контроллер проверяет, что SHA совпадает с main фиксированного репозитория. Прикладной код, pip и тесты не выполняются от root; production-секреты не выдаются test job.

## Установка/ротация

Администратор получает проверенные deploy/update-test-vds.sh, request-test-deploy.sh, install-test-vds-cd.sh и публичный ключ GitHub. На существующей VDS запускает install-test-vds-cd.sh /путь/к/публичному/ключу. Приватный ключ добавляет только в GitHub Secret. Контроллеры установлены вне обновляемого checkout и принадлежат root: изменения root-контроллера в репозитории не применяются автоматически, их администратор проверяет и устанавливает отдельно.

## Ручные действия администратора

Обычное обновление: sudo /usr/local/sbin/gibdd-test-deploy SHA

Явная очистка: sudo /usr/local/sbin/gibdd-test-deploy SHA --reset-data

Вторая команда разрешена только root-администратору, никогда CI-пользователю. Она предварительно сохраняет БД с вложениями и создаёт пустой staging-ready. Первый новый EMPLOYEE получает CHIEF. Не выполняйте очистку при обычных обновлениях.

Проверка: systemctl status gibdd-test; cat /opt/gibdd-test-20261117/deployment.json; curl --fail http://127.0.0.1:8441/health. Историю проверок смотрите в GitHub Actions и deployment-tests.xml внутри release. При обновлении возможен короткий 502 во время переключения. При восстановлении snapshot нельзя терять принятые после него сообщения: восстановление выполняется в согласованное окно и с учётом актуальных отзывов согласия.
