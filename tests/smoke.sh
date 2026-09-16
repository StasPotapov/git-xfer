#!/bin/sh
# Сквозной прогон git-xfer на одноразовых репозиториях.
# Рабочие репозитории не трогает: стенд, конфиг и state живут во временном
# каталоге, который удаляется в конце.
#
#   sh tests/smoke.sh            # прогнать всё
#   KEEP=1 sh tests/smoke.sh     # оставить стенд для разбора
set -u

HERE=$(cd "$(dirname "$0")" && pwd)
ROOT=$(cd "$HERE/.." && pwd)
WORK=$(mktemp -d "${TMPDIR:-/tmp}/git-xfer-smoke.XXXXXX")
PASS=0
FAIL=0

cleanup() {
  if [ "${KEEP:-0}" = "1" ]; then
    echo "Стенд оставлен: $WORK"
  else
    rm -rf "$WORK"
  fi
}
trap cleanup EXIT

xfer() {
  XDG_CONFIG_HOME="$WORK/config" XDG_STATE_HOME="$WORK/state" \
  PYTHONPATH="$ROOT" python3 -m gitxfer "$@"
}

ok()   { PASS=$((PASS + 1)); printf '  ✓ %s\n' "$1"; }
bad()  { FAIL=$((FAIL + 1)); printf '  ✗ %s\n' "$1"; }
check(){ if [ "$1" = "$2" ]; then ok "$3"; else bad "$3 (ждали «$1», получили «$2»)"; fi; }
has()  { if printf '%s\n' "$2" | grep -q -- "$1"; then ok "$3"; else bad "$3"; fi; }
hasnt(){ if printf '%s\n' "$2" | grep -q -- "$1"; then bad "$3"; else ok "$3"; fi; }

echo "== 1. стенд =="
sh "$HERE/fixture.sh" "$WORK" >/dev/null || { echo "стенд не собрался"; exit 1; }
mkdir -p "$WORK/config/git-xfer"
cat > "$WORK/config/git-xfer/config.toml" <<EOF
[defaults]
scan_limit = 50
patchid_window = 200

[profiles.t]
a = "$WORK/a"
b = "$WORK/b"
branch = "master"

# Старый формат с зашитым направлением — должен продолжать работать.
[profiles.legacy]
source = "$WORK/a"
source_branch = "master"
target = "$WORK/b"
target_branch = "master"
EOF
ok "два несвязанных репозитория и конфиг"

echo "== 2. doctor =="
xfer doctor -p t --to b >/dev/null 2>&1
check 0 $? "на чистом репозитории зелено"
# Незавершённый одиночный cherry-pick: каталога .git/sequencer git не создаёт,
# поэтому ловить его можно только по CHERRY_PICK_HEAD.
CONF=$(git -C "$WORK/a" log --no-merges --format='%H %s' master | grep 'конфликтует' | cut -d' ' -f1)
git -C "$WORK/b" -c protocol.file.allow=always fetch -q --no-tags --no-write-fetch-head \
  -- "$WORK/a" '+refs/heads/master:refs/xfer/probe/head'
git -C "$WORK/b" cherry-pick "$CONF" >/dev/null 2>&1
check "" "$(ls "$WORK/b/.git/sequencer" 2>/dev/null)" "sequencer не создан (single_pick)"
if [ -e "$WORK/b/.git/CHERRY_PICK_HEAD" ]; then ok "CHERRY_PICK_HEAD на месте"; else bad "CHERRY_PICK_HEAD на месте"; fi
OUT=$(xfer doctor -p t --to b 2>&1); CODE=$?
check 2 $CODE "doctor блокирует незавершённый cherry-pick"
has "CHERRY_PICK_HEAD" "$OUT" "блокировка именно по CHERRY_PICK_HEAD"
git -C "$WORK/b" cherry-pick --abort >/dev/null 2>&1
git -C "$WORK/b" update-ref -d refs/xfer/probe/head
# AUTO_MERGE остаётся и после успешного cherry-pick — маркером быть не может.
CLEAN=$(git -C "$WORK/a" log --no-merges --format='%H %s' master | grep 'новая фича' | cut -d' ' -f1)
git -C "$WORK/b" cherry-pick "$CLEAN" >/dev/null 2>&1
xfer doctor -p t --to b >/dev/null 2>&1
check 0 $? "AUTO_MERGE после чистого cherry-pick не блокирует"
git -C "$WORK/b" reset -q --hard HEAD~1

echo "== 3. sync =="
TAGS_BEFORE=$(git -C "$WORK/b" for-each-ref --format='%(refname)' refs/tags/)
xfer sync -p t --to b >/dev/null
check "$(git -C "$WORK/a" rev-parse master)" "$(git -C "$WORK/b" rev-parse refs/xfer/t-to-b/head)" "refs/xfer/t-to-b/head создан"
check "$TAGS_BEFORE" "$(git -C "$WORK/b" for-each-ref --format='%(refname)' refs/tags/)" "refs/tags/* не тронуты"
check "" "$(git -C "$WORK/b" remote -v)" "remote'ов не прибавилось"
check "" "$(ls "$WORK/b/.git/FETCH_HEAD" 2>/dev/null)" "FETCH_HEAD не создан"

echo "== 4. list и plan =="
OUT=$(xfer list -p t --to b)
has "≈ .*shared.txt" "$OUT" "дубль уже имеющегося изменения помечен ≈"
hasnt "merge: влили side" "$OUT" "merge-коммит по умолчанию не показан"
has "merge: влили side" "$(xfer list -p t --to b --allow-merges)" "--allow-merges показывает merge-коммит"
PLAN=$(xfer plan -p t --to b --commits 3,4,5,6,7)
has "✗ .*конфликтует" "$PLAN" "plan предсказал конфликт там, где он есть"
has "станет пустым" "$PLAN" "plan предсказал пустой коммит"
check "" "$(git -C "$WORK/b" status --porcelain=v2)" "plan не тронул рабочее дерево"

echo "== 5. apply с конфликтом и continue =="
OUT=$(xfer apply -p t --to b --commits 3,4,5,6,7 --yes 2>&1); CODE=$?
check 3 $CODE "остановились на конфликте с кодом 3"
printf 'l1\nl2\nRESOLVED\nl4\nl5\n' > "$WORK/b/src/app.py"
git -C "$WORK/b" add src/app.py
OUT=$(xfer continue -p t --to b 2>&1); CODE=$?
check 0 $CODE "continue довёл очередь до конца"
# Дефолты: автором становится тот, кто переносит (в b это Bob Target),
# а сообщение едет один в один — ни трейлера, ни другой приписки.
AUTHORS=$(git -C "$WORK/b" log -4 --format='%an')
check 0 "$(printf '%s\n' "$AUTHORS" | grep -c 'Ann Source' || true)" "автор источника не приехал"
check 4 "$(printf '%s\n' "$AUTHORS" | grep -c 'Bob Target' || true)" "автор — тот, кто переносит, и у конфликтного тоже"
# У коммита два поля, и по умолчанию оба наши: author — кто написал,
# committer — кто применил. «Мы» — это user.name целевого репозитория.
check 4 "$(git -C "$WORK/b" log -4 --format='%cn' | grep -c 'Bob Target' || true)" "коммиттер — тоже тот, кто переносит"
TODAY=$(date +%Y-%m-%d)
check 4 "$(git -C "$WORK/b" log -4 --format='%ad' --date=short | grep -c "$TODAY" || true)" "author date — момент переноса"
BODY=$(git -C "$WORK/b" log -4 --format='%B')
check 0 "$(printf '%s\n' "$BODY" | grep -c "cherry picked from commit" || true)" "сообщение перенесено один в один"
has "chore: бинарный ассет" "$(git -C "$WORK/b" log -4 --format='%s')" "заголовки коммитов на месте"
# Без трейлера «уже переносили» помнит только маппинг в state — проверяем,
# что он и правда за это отвечает, пока state цел.
has "− .*бинарный ассет" "$(xfer list -p t --to b)" "перенесённое помечено − по маппингу из state"

echo "== 6. abort оставляет уже перенесённое =="
printf 'helper\n' > "$WORK/a/src/helper.py"
git -C "$WORK/a" add src/helper.py
git -C "$WORK/a" -c user.name=Ann -c user.email=ann@example.com commit -q -m "src: helper"
printf 'def feature():\n    return 99\n' > "$WORK/a/src/feature.py"
git -C "$WORK/a" -c user.name=Ann -c user.email=ann@example.com commit -q -am "src: feature=99"
printf 'def feature():\n    return 7\n' > "$WORK/b/src/feature.py"
git -C "$WORK/b" -c user.name=Bob -c user.email=bob@example.com commit -q -am "tgt: feature=7"
xfer sync -p t --to b >/dev/null
BEFORE=$(git -C "$WORK/b" rev-parse HEAD)
xfer apply -p t --to b --commits 1,2 --yes >/dev/null 2>&1
check 1 "$(git -C "$WORK/b" rev-list --count "$BEFORE"..HEAD)" "первый коммит серии применён"
xfer abort -p t --to b >/dev/null
check 1 "$(git -C "$WORK/b" rev-list --count "$BEFORE"..HEAD)" "abort откатил только текущий коммит"
check "" "$(git -C "$WORK/b" status --porcelain=v2)" "после abort дерево чистое"

echo "== 7. skip не принимает уехавший HEAD =="
xfer apply -p t --to b --commits 1 --yes >/dev/null 2>&1
git -C "$WORK/b" cherry-pick --abort >/dev/null 2>&1
git -C "$WORK/b" reset -q --hard HEAD~1
OUT=$(xfer skip -p t --to b 2>&1); CODE=$?
check 1 $CODE "skip отказался работать поверх чужого HEAD"
has "HEAD ушёл не туда" "$OUT" "и сказал об этом внятно"
xfer cleanup -p t --to b --state >/dev/null 2>&1

echo "== 8. повторный list и cleanup =="
xfer sync -p t --to b >/dev/null
OUT=$(xfer list -p t --to b)
# Секция 7 закончилась `cleanup --state`: маппинга больше нет, трейлера не
# было — остаётся только эвристика patch-id. Это и есть цена выключенного
# по умолчанию трейлера, и она должна быть видна в тесте.
has "≈ .*бинарный ассет" "$OUT" "без трейлера и state остаётся только patch-id"
xfer cleanup -p t --to b >/dev/null
check "" "$(git -C "$WORK/b" for-each-ref --format='%(refname)' refs/xfer/)" "refs/xfer/* убраны"

echo "== 9. non-TTY =="
OUT=$(xfer apply -p t --to b < /dev/null 2>&1); CODE=$?
check 1 $CODE "без выбора и без терминала — ошибка, а не зависание"
has "stdin не терминал" "$OUT" "и сказано, что делать"
OUT=$(xfer apply -p t --to b -i < /dev/null 2>&1); CODE=$?
check 1 $CODE "-i без терминала — ошибка"

echo "== 10. обратное направление =="
xfer doctor -p t --to a >/dev/null 2>&1
check 0 $? "зеркальный профиль готов"
OUT=$(xfer list -p t --to a)
has "−" "$OUT" "перенесённое опознано и с обратной стороны"

echo "== 11. --dry-run и коды выхода =="
OUT=$(xfer -n apply -p t --to b --commits 1 --yes 2>&1); CODE=$?
check 1 $CODE "apply под --dry-run отказывается"
has "git-xfer plan" "$OUT" "и отправляет в plan"
BEFORE=$(git -C "$WORK/b" for-each-ref --format='%(refname)' refs/xfer/)
OUT=$(xfer -n cleanup -p t --to b 2>&1)
hasnt "^Удалён" "$OUT" "cleanup под --dry-run не рапортует об удалении"
check "$BEFORE" "$(git -C "$WORK/b" for-each-ref --format='%(refname)' refs/xfer/)" "и ничего не удалил"
xfer cleanup -p t --to b >/dev/null 2>&1
OUT=$(xfer -n plan -p t --to b --commits 1 2>&1); CODE=$?
check 1 $CODE "plan под --dry-run без объектов — внятная ошибка"
has "git-xfer sync" "$OUT" "и говорит, что выполнить"
xfer такой-подкоманды-нет >/dev/null 2>&1
check 1 $? "ошибка разбора аргументов не занимает код 2"

echo "== 12. маппинг проверяет достижимость, а не существование =="
xfer cleanup -p t --to b --state >/dev/null 2>&1
printf 'unique-%s\n' "$$" > "$WORK/a/unique.txt"
git -C "$WORK/a" add unique.txt
git -C "$WORK/a" -c user.name=Ann -c user.email=ann@example.com commit -q -m "src: уникальный коммит"
xfer sync -p t --to b >/dev/null
SHA=$(git -C "$WORK/a" rev-parse master)
xfer apply -p t --to b --sha "$SHA" --yes >/dev/null 2>&1
check 0 $? "чистый коммит перенесён"
if grep -q "\"$SHA\"" "$WORK"/state/git-xfer/*.json; then
  ok "маппинг src→dst записан в state"
else
  bad "маппинг src→dst записан в state"
fi
# Коммит убран из истории, но объект жив: cat-file нашёл бы его и соврал.
git -C "$WORK/b" reset -q --hard HEAD~1
OUT=$(xfer list -p t --to b --limit 5)
if printf '%s\n' "$OUT" | grep -q "+ .*уникальный коммит"; then
  ok "откаченный коммит снова показан как новый"
else
  bad "откаченный коммит снова показан как новый"
  printf '%s\n' "$OUT" | sed 's/^/      /'
fi

echo "== 13. алиасы профилей не схлопываются =="
ALIASES=$(PYTHONPATH="$ROOT" python3 -c '
from pathlib import Path
from gitxfer.config import adhoc_profile
mk = lambda n: adhoc_profile(name=n, source=Path("/s"), source_branch="m",
                             target=Path("/t"), target_branch="m").alias
print(mk("proj"), mk("proj/back"), mk("proj-back"))')
set -- $ALIASES
check "proj" "$1" "простое имя остаётся как есть"
if [ "$2" = "$3" ]; then bad "proj/back и proj-back дают разные ref"; else ok "proj/back и proj-back дают разные ref"; fi

echo "== 14. запуск на старом python =="
OLD=""
for CAND in /usr/bin/python3 python3.10 python3.9 python3.8; do
  command -v "$CAND" >/dev/null 2>&1 || continue
  VER=$("$CAND" -c 'import sys; print(sys.version_info[0] * 100 + sys.version_info[1])' 2>/dev/null) || continue
  if [ "$VER" -lt 311 ]; then OLD="$CAND"; break; fi
done
if [ -n "$OLD" ]; then
  OUT=$(PYTHONPATH="$ROOT" "$OLD" -m gitxfer --version 2>&1); CODE=$?
  check 1 $CODE "код выхода 1, а не трейсбек"
  has "нужен Python 3.11" "$OUT" "сказано, какая версия нужна"
  hasnt "ModuleNotFoundError" "$OUT" "ModuleNotFoundError наружу не вылезает"
else
  ok "python старше 3.11 на машине не нашёлся — проверку пропускаем"
fi

echo "== 15. конфиг в произвольном месте =="
CUSTOM="$WORK/custom.local.toml"
xfer init --config "$CUSTOM" >/dev/null 2>&1
check 0 $? "init принимает --config после подкоманды"
if [ -f "$CUSTOM" ]; then ok "шаблон создан по указанному пути"; else bad "шаблон создан по указанному пути"; fi
cat >> "$CUSTOM" <<EOF

[profiles.custom]
a = "$WORK/a"
b = "$WORK/b"
branch = "master"
EOF
xfer doctor --config "$CUSTOM" -p custom --to b >/dev/null 2>&1
check 0 $? "конфиг читается с --config после подкоманды"
xfer --config "$CUSTOM" doctor -p custom --to b >/dev/null 2>&1
check 0 $? "и с --config до подкоманды"
OUT=$(xfer -v --config "$CUSTOM" doctor -p custom --to b 2>&1 >/dev/null | head -1)
has "^+ git " "$OUT" "-v до подкоманды печатает вызовы git"
OUT=$(xfer doctor --config "$CUSTOM" -v -p custom --to b 2>&1 >/dev/null | head -1)
has "^+ git " "$OUT" "-v после подкоманды тоже"

echo "== 16. пара: одно описание, два направления =="
check "$(git -C "$WORK/a" rev-parse master)" \
      "$(git -C "$WORK/b" rev-parse refs/xfer/t-to-b/head)" "--to b тащит из a"
xfer sync -p t --to a >/dev/null
check "$(git -C "$WORK/b" rev-parse master)" \
      "$(git -C "$WORK/a" rev-parse refs/xfer/t-to-a/head)" "--to a тащит из b"
OUT=$(xfer doctor -p t 2>&1 </dev/null); CODE=$?
check 1 $CODE "без --to и без терминала — ошибка, а не молчаливый выбор"
has "\-\-to a" "$OUT" "и сказано, как указать направление"
xfer doctor -p legacy >/dev/null 2>&1
check 0 $? "старый формат source/target работает без --to"

echo "== 17. разовое переопределение веток =="
git -C "$WORK/a" branch -q feature 2>/dev/null
git -C "$WORK/b" branch -q feature 2>/dev/null
git -C "$WORK/b" checkout -q feature
OUT=$(xfer doctor -p t --to b -b feature 2>&1); CODE=$?
check 0 $CODE "-b feature переключает обе стороны без правки конфига"
has "(feature)" "$OUT" "направление показано с новой веткой"
REFS=$(git -C "$WORK/b" for-each-ref --format='%(refname)' refs/xfer/)
if printf '%s\n' "$REFS" | grep -q "t-to-b/head"; then
  ok "ref для ветки из конфига на месте"
else
  bad "ref для ветки из конфига на месте"
fi
xfer sync -p t --to b -b feature >/dev/null 2>&1
NEW=$(git -C "$WORK/b" for-each-ref --format='%(refname)' refs/xfer/ | grep -c "t-to-b-feature")
check 1 "$NEW" "у разового прогона свой ref, чужой он не трогает"
git -C "$WORK/b" checkout -q master

echo "== 18. журнал =="
LOG="$WORK/state/git-xfer/git-xfer.log"
if [ -s "$LOG" ]; then ok "журнал пишется"; else bad "журнал пишется"; fi
if grep -q "git fetch" "$LOG"; then ok "в журнале видны вызовы git"; else bad "в журнале видны вызовы git"; fi
OUT=$(xfer status -p t --to b 2>&1)
has "Журнал:" "$OUT" "status показывает, где журнал"
BEFORE=$(wc -c < "$LOG")
xfer --no-log doctor -p t --to b >/dev/null 2>&1
check "$BEFORE" "$(wc -c < "$LOG")" "--no-log ничего не дописывает"
OWN="$WORK/own.log"
xfer --log-file "$OWN" doctor -p t --to b >/dev/null 2>&1
if [ -s "$OWN" ]; then ok "--log-file уводит журнал в свой файл"; else bad "--log-file уводит журнал в свой файл"; fi
QUIET="$WORK/quiet.toml"
cat > "$QUIET" <<EOF
[defaults]
log = false

[profiles.t]
a = "$WORK/a"
b = "$WORK/b"
branch = "master"
EOF
BEFORE=$(wc -c < "$LOG")
xfer doctor --config "$QUIET" -p t --to b >/dev/null 2>&1
check 0 $? "конфиг с log = false разбирается"
check "$BEFORE" "$(wc -c < "$LOG")" "log = false в конфиге выключает журнал"
OUT=$(xfer status --config "$QUIET" -p t --to b 2>&1)
has "Журнал:  выключен" "$OUT" "status честно говорит, что журнал выключен"

echo "== 19. флаги в обход конфига =="
OUT=$(xfer list --source "$WORK/a" --target "$WORK/b" -b master </dev/null 2>&1); CODE=$?
check 0 $CODE "--source/--target работают, когда конфиг не нужен"
hasnt "укажите направление" "$OUT" "и не требуют --to"
BROKEN="$WORK/broken.toml"
printf '[profiles.x]\na = "/nowhere"\n' > "$BROKEN"
OUT=$(xfer list --config "$BROKEN" --source "$WORK/a" --target "$WORK/b" -b master </dev/null 2>&1); CODE=$?
check 1 $CODE "сломанный конфиг не проглатывается молча"
has "profiles.x" "$OUT" "и названа причина"

echo "== 20. флаг про одну ветку не трогает вторую =="
git -C "$WORK/a" branch -q rel 2>/dev/null
git -C "$WORK/b" branch -q rel 2>/dev/null
OUT=$(xfer doctor -p t --to b --source-branch rel 2>&1)
has "/a (rel)" "$OUT" "источник взял указанную ветку"
has "/b (master)" "$OUT" "а цель осталась из профиля"
OUT=$(xfer doctor -p t --to b --target-branch rel 2>&1)
has "/a (master)" "$OUT" "и зеркально: источник из профиля"
has "/b (rel)" "$OUT" "цель — указанная"
OUT=$(xfer list --source "$WORK/a" --target "$WORK/b" --source-branch master </dev/null 2>&1); CODE=$?
check 0 $CODE "без конфига вторая сторона всё ещё берёт то же имя"

echo "== 21. серию можно доделать под другим именем =="
# Конфликт делаем нарочно: обе стороны заводят один файл с разным текстом.
git -C "$WORK/a" checkout -q -B rel master
printf 'from-a\n' > "$WORK/a/clash.txt"
git -C "$WORK/a" add clash.txt
git -C "$WORK/a" -c user.name=Ann -c user.email=ann@e commit -q -m "rel: clash"
git -C "$WORK/a" checkout -q master
git -C "$WORK/b" checkout -q -B rel master
printf 'from-b\n' > "$WORK/b/clash.txt"
git -C "$WORK/b" add clash.txt
git -C "$WORK/b" -c user.name=Bob -c user.email=bob@e commit -q -m "rel: clash"
xfer cleanup -p t --to b --state >/dev/null 2>&1
xfer apply -p t --to b -b rel --commits 1 --yes >/dev/null 2>&1
check 3 $? "серия с -b rel встала на конфликте"
if [ -e "$WORK/b/.git/CHERRY_PICK_HEAD" ]; then ok "cherry-pick на паузе"; else bad "cherry-pick на паузе"; fi
# Имя направления теперь другое (без -b), но доделать серию это мешать не должно.
OUT=$(xfer abort -p t --to b 2>&1); CODE=$?
check 0 $CODE "abort без -b доделывает серию, начатую с -b"
has "серия начата как" "$OUT" "и честно говорит, что имя другое"
check "" "$(git -C "$WORK/b" status --porcelain=v2)" "после abort дерево чистое"
git -C "$WORK/b" checkout -q master
xfer cleanup -p t --to b --all --state >/dev/null 2>&1

echo "== 22. пример конфига не расходится с тем, что пишет init =="
CHECK=$(PYTHONPATH="$ROOT" python3 -c '
import tomllib
from pathlib import Path
from gitxfer.config import CONFIG_TEMPLATE, load_config
example = Path("'"$ROOT"'/config.example.toml")
a = sorted(tomllib.loads(CONFIG_TEMPLATE).get("defaults", {}))
b = sorted(tomllib.loads(example.read_text()).get("defaults", {}))
cfg = load_config(example)
pair = next(iter(cfg.profiles.values()))
print("OK" if a == b and pair.implied is None else "FAIL")
' 2>&1)
check "OK" "$CHECK" "пример разбирается, ключи те же, профиль — пара"
if grep -q "git xfer " "$ROOT/config.example.toml"; then
  bad "в примере не осталось вызовов через пробел"
else
  ok "в примере не осталось вызовов через пробел"
fi

echo "== 23. политика разбора конфликтов =="
POL="$WORK/policy.toml"
for V in ask mechanical auto; do
  cat > "$POL" <<EOF
[agent]
resolve_conflicts = "$V"

[profiles.t]
a = "$WORK/a"
b = "$WORK/b"
branch = "master"
EOF
  OUT=$(xfer status --config "$POL" -p t --to b 2>&1)
  has "Конфликты: $V" "$OUT" "status показывает политику $V"
done
cat > "$POL" <<EOF
[agent]
resolve_conflicts = "yolo"

[profiles.t]
a = "$WORK/a"
b = "$WORK/b"
branch = "master"
EOF
OUT=$(xfer status --config "$POL" -p t --to b 2>&1); CODE=$?
check 1 $CODE "опечатка в политике — ошибка, а не молчание"
has "resolve_conflicts" "$OUT" "и названа причина"
DEFAULT=$(xfer status -p t --to b 2>&1 | grep "^Конфликты:")
has "mechanical" "$DEFAULT" "по умолчанию mechanical"

echo "== 24. скилл на месте и описан =="
SKILL="$ROOT/skills/git-xfer/SKILL.md"
if [ -f "$SKILL" ]; then ok "SKILL.md лежит в репозитории"; else bad "SKILL.md лежит в репозитории"; fi
head -1 "$SKILL" | grep -q -- "---" && ok "frontmatter на месте" || bad "frontmatter на месте"
grep -q "^name: git-xfer$" "$SKILL" && ok "имя скилла задано" || bad "имя скилла задано"
grep -q "^description: " "$SKILL" && ok "описание задано" || bad "описание задано"
if grep -q "git xfer " "$SKILL"; then
  bad "в скилле нет вызовов через пробел"
else
  ok "в скилле нет вызовов через пробел"
fi
for V in ask mechanical auto; do
  grep -q "\`$V\`" "$SKILL" && ok "скилл знает политику $V" || bad "скилл знает политику $V"
done

echo "== 25. все способы запуска =="
OUT=$(cd "$ROOT" && PYTHONPATH="$ROOT" python3 -m gitxfer --version 2>&1); check 0 $? "python3 -m gitxfer"
has "git-xfer" "$OUT" "и печатает версию"
OUT=$("$ROOT/bin/git-xfer" --version 2>&1); check 0 $? "лаунчер bin/git-xfer"
OUT=$(python3 "$ROOT/gitxfer/__main__.py" --version 2>&1); check 0 $? "__main__.py файлом"
has "git-xfer" "$OUT" "и он тоже печатает версию"
OUT=$(python3 "$ROOT/gitxfer/cli.py" --version 2>&1); CODE=$?
check 1 $CODE "cli.py файлом — внятный отказ, а не трейсбек"
hasnt "Traceback" "$OUT" "без трейсбека"
has "python3 -m gitxfer" "$OUT" "и сказано, как запускать"

echo "== 26. ни один модуль не затеняет стандартную библиотеку =="
CLASH=$(PYTHONPATH="$ROOT" python3 -c '
import sys, pathlib
mods = {p.stem for p in pathlib.Path("'"$ROOT"'/gitxfer").glob("*.py")} - {"__init__", "__main__"}
print(",".join(sorted(mods & sys.stdlib_module_names)) or "нет")')
check "нет" "$CLASH" "имена модулей не совпадают со стандартными"
# Каталог пакета в sys.path не должен ломать subprocess.
OUT=$(cd "$WORK" && PYTHONPATH="$ROOT/gitxfer:$ROOT" python3 -m gitxfer --version 2>&1); CODE=$?
check 0 $CODE "пакет работает, даже если его каталог попал в sys.path"

echo "== 27. все подкоманды отвечают на --help =="
for CMD in init doctor sync list plan apply continue skip abort status cleanup; do
  OUT=$(PYTHONPATH="$ROOT" python3 -m gitxfer "$CMD" --help 2>&1); CODE=$?
  if [ $CODE -eq 0 ] && printf '%s\n' "$OUT" | grep -q "usage: git-xfer $CMD"; then
    ok "$CMD --help"
  else
    bad "$CMD --help (код $CODE)"
  fi
done

echo "== 28. текущего каталога больше нет =="
# Так бывает после переноса или удаления каталога, в котором стоит шелл:
# os.getcwd() начинает бросать FileNotFoundError.
GONE="$WORK/gone"
mkdir -p "$GONE"
OUT=$(cd "$GONE" && rmdir "$GONE" && PYTHONPATH="$ROOT" XDG_CONFIG_HOME="$WORK/config" \
      XDG_STATE_HOME="$WORK/state" python3 -m gitxfer --version 2>&1); CODE=$?
check 0 $CODE "утилита запускается из исчезнувшего каталога"
hasnt "Traceback" "$OUT" "без трейсбека"
has "git-xfer" "$OUT" "и отвечает по делу"
mkdir -p "$GONE"
OUT=$(cd "$GONE" && rmdir "$GONE" && PYTHONPATH="$ROOT" XDG_CONFIG_HOME="$WORK/config" \
      XDG_STATE_HOME="$WORK/state" python3 -m gitxfer list -p t --to b 2>&1); CODE=$?
check 0 $CODE "и полноценная команда тоже"
hasnt "FileNotFoundError" "$OUT" "FileNotFoundError наружу не вылезает"
if grep -q "каталога больше нет" "$WORK/state/git-xfer/git-xfer.log"; then
  ok "журнал отметил, что каталога нет"
else
  bad "журнал отметил, что каталога нет"
fi

echo "== 29. скилл описывает то, что утилита правда говорит =="
SKILL="$ROOT/skills/git-xfer/SKILL.md"
# Симптомы из таблицы «если что-то не работает» должны совпадать с текстами,
# которые утилита выдаёт на самом деле, иначе агент их не опознает.
sk() { grep -qF -- "$1" "$SKILL" && ok "в скилле описано: $2" || bad "в скилле описано: $2"; }

OUT=$(xfer doctor --config "$WORK/nope.toml" -p x 2>&1); MSG=$(printf '%s\n' "$OUT" | head -1)
case "$MSG" in *"конфиг не найден"*) ok "текст «конфиг не найден» не менялся";;
                *) bad "текст «конфиг не найден» не менялся (сейчас: $MSG)";; esac
sk "конфиг не найден" "отсутствующий конфиг"

xfer init --config "$WORK/tpl.toml" >/dev/null 2>&1
OUT=$(xfer doctor --config "$WORK/tpl.toml" -p x 2>&1 | head -1)
case "$OUT" in *"не описан ни один профиль"*) ok "текст про пустой шаблон не менялся";;
                *) bad "текст про пустой шаблон не менялся (сейчас: $OUT)";; esac
sk "не описан ни один профиль" "пустой шаблон"

OUT=$(python3 "$ROOT/gitxfer/cli.py" 2>&1 | head -1)
case "$OUT" in *"часть пакета"*) ok "текст про запуск файла не менялся";;
                *) bad "текст про запуск файла не менялся (сейчас: $OUT)";; esac

sk "is not a git command" "git xfer без PATH"
sk "нужен Python 3.11 или новее" "старый python"
sk "os.getcwd()" "исчезнувший каталог"
sk "uv tool install git+https://github.com/StasPotapov/git-xfer" "установка из открытого репозитория"
# Репозиторий публичный: доступ для клона не нужен, и скилл не должен
# отправлять агента за ним к человеку.
if grep -qiE 'приватн|нужен доступ' "$SKILL"; then
  bad "в скилле не осталось утверждений о приватности репозитория"
else
  ok "в скилле не осталось утверждений о приватности репозитория"
fi

# --- перенос со сменой префикса путей -----------------------------------
# Стенд: m — монорепозиторий с проектом в apps/mobile, n — тот же
# проект в корне отдельного репозитория (см. tests/fixture.sh).

pfx_config() { # a_prefix
  cat > "$WORK/config/git-xfer/config.toml" <<EOF
[defaults]
scan_limit = 30

[profiles.myapp]
a = "$WORK/m"
a_prefix = "$1"
b = "$WORK/n"
branch = "master"
EOF
}
bad_config() { # файл, ключ a, ключ a_prefix
  cat > "$1" <<EOF
[profiles.bad]
a = "$2"
a_prefix = "$3"
b = "$WORK/n"
branch = "master"
EOF
}

echo "== 30. префикс: конфиг и doctor =="
MAIN_CONFIG=$(cat "$WORK/config/git-xfer/config.toml")
pfx_config "apps/mobile"
OUT=$(xfer doctor -p myapp --to b 2>&1); CODE=$?
check 0 $CODE "doctor зелёный на профиле с a_prefix"
has "apps/mobile" "$OUT" "подкаталог виден в направлении"
has "подкаталог: источник" "$OUT" "есть проверка подкаталога"

pfx_config "apps/mobile/"
OUT=$(xfer doctor -p myapp --to b 2>&1); check 0 $? "хвостовой слеш нормализуется"
pfx_config "apps//mobile"
OUT=$(xfer doctor -p myapp --to b 2>&1); check 0 $? "двойной слеш нормализуется"

bad_config "$WORK/p-abs.toml" "$WORK/m" "/абсолютный"
OUT=$(xfer doctor --config "$WORK/p-abs.toml" -p bad --to b 2>&1); CODE=$?
check 1 $CODE "абсолютный префикс — ошибка конфига"
has "не абсолютный" "$OUT" "и сказано почему"

bad_config "$WORK/p-up.toml" "$WORK/m" "../наружу"
OUT=$(xfer doctor --config "$WORK/p-up.toml" -p bad --to b 2>&1); CODE=$?
check 1 $CODE "'..' в префиксе — ошибка конфига"

bad_config "$WORK/p-none.toml" "$WORK/m" "нет-такого"
OUT=$(xfer doctor --config "$WORK/p-none.toml" -p bad --to b 2>&1); CODE=$?
check 2 $CODE "несуществующий подкаталог — предполётная ошибка"
has "переносить нечего" "$OUT" "и сказано, что переносить нечего"

bad_config "$WORK/p-file.toml" "$WORK/m" "README.md"
OUT=$(xfer doctor --config "$WORK/p-file.toml" -p bad --to b 2>&1); CODE=$?
check 2 $CODE "префикс указывает на файл — предполётная ошибка"
has "а не каталог" "$OUT" "и сказано, что это не каталог"

printf '[profiles.bad]\na = "%s"\nb = "%s"\nbranch = "master"\n' \
  "$WORK/m/apps/mobile" "$WORK/n" > "$WORK/p-inside.toml"
OUT=$(xfer doctor --config "$WORK/p-inside.toml" -p bad --to b 2>&1); CODE=$?
check 2 $CODE "путь внутрь репозитория — предполётная ошибка"
has "a_prefix" "$OUT" "и подсказано, как переписать профиль"

# Та же ошибка, но подкаталог записан стороной b, а переносим --to a:
# источник тут — сторона b, и подсказка обязана назвать её, а не a.
printf '[profiles.bad]\na = "%s"\nb = "%s"\nbranch = "master"\n' \
  "$WORK/n" "$WORK/m/apps/mobile" > "$WORK/p-side.toml"
OUT=$(xfer doctor --config "$WORK/p-side.toml" -p bad --to a 2>&1)
has "b_prefix" "$OUT" "подсказка называет реальную сторону пары"
hasnt "a_prefix" "$OUT" "и не советует править чужую сторону"

echo "== 31. префикс: list, окно и patch-id =="
pfx_config "apps/mobile"
OUT=$(xfer list -p myapp --to b 2>&1)
hasnt "chore: сборка" "$OUT" "коммит целиком вне подкаталога не показан"
hasnt "init: монорепо" "$OUT" "и коммит до появления подкаталога тоже"
has "feat: звезда" "$OUT" "коммит внутри подкаталога показан"
has "+\*" "$OUT" "пограничный коммит помечен звёздочкой"
has "вне подкаталога" "$OUT" "в легенде расшифровано, что значит звёздочка"
# Одинаковое содержимое по обе стороны: в m оно под префиксом, в n — в корне.
# Совпавший patch-id доказывает, что --relative срезает префикс правильно.
if printf '%s\n' "$OUT" | grep -q "≈.*init: подпроект"; then
  ok "patch-id сошёлся через границу префикса"
else
  bad "patch-id сошёлся через границу префикса"
fi
# Коммит влитой ветки TREESAME по первому родителю: без --full-history
# упрощение истории спрятало бы его из обхода по pathspec.
has "side: и в подпроекте" "$OUT" "коммит влитой ветки не потерян упрощением истории"
# У merge-коммита diff-tree без -m молчит, и пометка «частичный» не появилась бы.
OUT=$(xfer list -p myapp --to b --allow-merges 2>&1)
printf '%s\n' "$OUT" | grep -q "+\*.*merge: влили side" \
  && ok "merge-коммит на границе тоже помечен" \
  || bad "merge-коммит на границе тоже помечен"

echo "== 32. префикс: перенос вперёд снимает префикс =="
SRC_STAR=$(git -C "$WORK/m" log --format='%H %s' | grep 'feat: звезда' | cut -d' ' -f1)
OUT=$(xfer apply -p myapp --to b --sha "$SRC_STAR" --yes 2>&1); CODE=$?
check 0 $CODE "перенос прошёл"
check 0 "$(git -C "$WORK/n" ls-files | grep -c '^android/' || true)" "лишней вложенности android/ не появилось"
check 1 "$(git -C "$WORK/n" ls-files | grep -c '^icons/star.svg$' || true)" "файл лёг в корень целевого репозитория"
BODY=$(git -C "$WORK/n" log -1 --format=%B)
check 0 "$(printf '%s\n' "$BODY" | grep -c "cherry picked from commit" || true)" "сообщение проекции тоже без трейлера"
check 1 "$(git -C "$WORK/n" log -1 --format=%P | wc -w | tr -d ' ')" "у перенесённого коммита один родитель"
has "Bob Target" "$(git -C "$WORK/n" log -1 --format='%an')" "автором проекции стал тот, кто переносит"

echo "== 33. префикс: пограничный коммит приезжает частично =="
SRC_BOTH=$(git -C "$WORK/m" log --format='%H %s' | grep 'fix: звезда и сборка' | cut -d' ' -f1)
OUT=$(xfer apply -p myapp --to b --sha "$SRC_BOTH" --yes 2>&1); CODE=$?
check 0 $CODE "пограничный коммит перенесён"
check 0 "$(git -C "$WORK/n" ls-files | grep -c '^tools/' || true)" "часть вне подкаталога не приехала"
check 1 "$(git -C "$WORK/n" show --name-only --format= HEAD | grep -c '^icons/star.svg$')" "приехала ровно внутренняя часть"

echo "== 34. префикс: коммит вне подкаталога ничего не двигает =="
SRC_OUT=$(git -C "$WORK/m" log --format='%H %s' | grep 'chore: сборка' | cut -d' ' -f1)
HEAD_BEFORE=$(git -C "$WORK/n" rev-parse HEAD)
OUT=$(xfer apply -p myapp --to b --sha "$SRC_OUT" --yes 2>&1); CODE=$?
check 0 $CODE "прогон завершился без ошибки"
has "подкаталог не затронут" "$OUT" "и сказано почему"
check "$HEAD_BEFORE" "$(git -C "$WORK/n" rev-parse HEAD)" "HEAD цели не сдвинулся"

echo "== 35. префикс: сухой прогон предсказывает то же, что делает apply =="
SRC_APP=$(git -C "$WORK/m" log --format='%H %s' | grep 'fix: правка app.kt' | cut -d' ' -f1)
OUT=$(xfer plan -p myapp --to b --sha "$SRC_APP" "$SRC_OUT" 2>&1); CODE=$?
check 0 $CODE "plan на профиле с префиксом отработал"
has "✗" "$OUT" "конфликт предсказан"
has "станет пустым" "$OUT" "коммит вне подкаталога предсказан пустым"
hasnt "apps/mobile/" "$OUT" "в предсказании нет путей с префиксом источника"
has "app.kt" "$OUT" "а есть путь в координатах цели"

echo "== 36. префикс: конфликт, gc на паузе, continue =="
OUT=$(xfer apply -p myapp --to b --sha "$SRC_APP" --yes 2>&1); CODE=$?
check 3 $CODE "встали на конфликте (там же, где предсказал plan)"
if git -C "$WORK/n" rev-parse --verify --quiet refs/xfer/myapp-to-b/pick >/dev/null; then
  ok "синтетический коммит удержан ссылкой"
else
  bad "синтетический коммит удержан ссылкой"
fi
OUT=$(xfer doctor -p myapp --to b 2>&1)
hasnt "myapp-to-b/pick" "$OUT" "doctor не считает свою же ссылку забытой"
# Ради этой ссылки всё и затевалось: без неё gc снёс бы объекты паузы.
git -C "$WORK/n" gc --prune=now --quiet 2>/dev/null
printf 'fun main() {\n    println("RESOLVED")\n}\n' > "$WORK/n/app.kt"
git -C "$WORK/n" add app.kt
OUT=$(xfer continue -p myapp --to b 2>&1); CODE=$?
check 0 $CODE "continue пережил gc и довёл коммит"
if git -C "$WORK/n" rev-parse --verify --quiet refs/xfer/myapp-to-b/pick >/dev/null; then
  bad "ссылка снята после завершения серии"
else
  ok "ссылка снята после завершения серии"
fi

echo "== 37. префикс: обратное направление надевает префикс =="
MONO_BEFORE=$(git -C "$WORK/m" ls-files | grep -c '^ios/\|^tools/\|^README.md$')
SRC_EXTRA=$(git -C "$WORK/n" log --format='%H %s' | grep 'feat: доп. иконка' | cut -d' ' -f1)
OUT=$(xfer apply -p myapp --to a --sha "$SRC_EXTRA" --yes 2>&1); CODE=$?
check 0 $CODE "обратный перенос прошёл"
check 1 "$(git -C "$WORK/m" ls-files | grep -c '^apps/mobile/icons/extra.svg$' || true)" "файл лёг под префикс"
check 0 "$(git -C "$WORK/m" ls-files | grep -c '^icons/' || true)" "в корень монорепозитория ничего не легло"
check "$MONO_BEFORE" "$(git -C "$WORK/m" ls-files | grep -c '^ios/\|^tools/\|^README.md$')" "файлы вне префикса не удалены"
check 1 "$(git -C "$WORK/m" diff --name-only HEAD~1 HEAD | wc -l | tr -d ' ')" "дифф ровно из одного пути"

echo "== 38. префикс: дедупликация пережила проекцию =="
OUT=$(xfer list -p myapp --to b 2>&1)
printf '%s\n' "$OUT" | grep -q "−.*feat: звезда" \
  && ok "перенесённое помечено как уже перенесённое" \
  || bad "перенесённое помечено как уже перенесённое"
OUT=$(xfer list -p myapp --to a 2>&1)
printf '%s\n' "$OUT" | grep -q "−.*доп. иконка" \
  && ok "обратное направление тоже видит уже перенесённое" \
  || bad "обратное направление тоже видит уже перенесённое"
OUT=$(xfer list -p myapp --to b --no-patch-id 2>&1); check 0 $? "--no-patch-id ничего не ломает"

echo "== 39. префикс: cleanup убирает и служебные ссылки =="
OUT=$(xfer cleanup -p myapp --to b 2>&1); CODE=$?
check 0 $CODE "cleanup отработал"
check 0 "$(git -C "$WORK/n" for-each-ref refs/xfer/ | wc -l | tr -d ' ')" "ссылок refs/xfer не осталось"

echo "== 40. префикс: переименование наружу и abort =="
SRC_MOVE=$(git -C "$WORK/m" log --format='%H %s' | grep 'move: звезда уехала в ios' | cut -d' ' -f1)
OUT=$(xfer apply -p myapp --to b --sha "$SRC_MOVE" --yes 2>&1); CODE=$?
check 0 $CODE "коммит с переименованием наружу перенесён"
# Вторая половина переименования лежит вне префикса, и её просто нет:
# в координатах цели это чистое удаление.
check 1 "$(git -C "$WORK/n" show --name-status --format= HEAD | grep -c '^D.icons/star.svg$')" "в цели это чистое удаление"
check 0 "$(git -C "$WORK/n" ls-files | grep -c '^ios/' || true)" "каталог из другой половины монорепо не приехал"

# abort на префиксном профиле: серия отменена, служебная ссылка снята.
SRC_APP2=$(git -C "$WORK/m" log --format='%H %s' | grep 'fix: правка app.kt' | cut -d' ' -f1)
printf 'fun main() {\n    println("ROLLED")\n}\n' > "$WORK/n/app.kt"
git -C "$WORK/n" add app.kt
git -C "$WORK/n" commit -qm "target: снова разошлись"
OUT=$(xfer apply -p myapp --to b --sha "$SRC_APP2" --yes 2>&1); CODE=$?
check 3 $CODE "снова встали на конфликте"
OUT=$(xfer abort -p myapp --to b 2>&1); CODE=$?
check 0 $CODE "abort отработал"
if git -C "$WORK/n" rev-parse --verify --quiet refs/xfer/myapp-to-b/pick >/dev/null; then
  bad "abort снял служебную ссылку"
else
  ok "abort снял служебную ссылку"
fi
check "" "$(git -C "$WORK/n" status --porcelain)" "рабочее дерево после abort чистое"

echo "== 41. про префикс написано там, где человек и агент это ищут =="
doc() { grep -qF -- "$1" "$2" && ok "$3" || bad "$3"; }
doc "a_prefix" "$ROOT/config.example.toml" "пример конфига описывает a_prefix"
doc "a_prefix" "$ROOT/README.md" "README описывает a_prefix"
doc "a_prefix" "$ROOT/skills/git-xfer/SKILL.md" "скилл описывает a_prefix"
# README обещает, что агент предложит подключить скилл, — значит в скилле
# это должно быть написано, иначе обещание останется словами.
doc "~/.claude/skills" "$ROOT/skills/git-xfer/SKILL.md" "скилл умеет подключить себя"
python3 - "$ROOT" <<'PY' && ok "шаблон init тоже описывает a_prefix" || bad "шаблон init тоже описывает a_prefix"
import sys
sys.path.insert(0, sys.argv[1])
from gitxfer.config import CONFIG_TEMPLATE
sys.exit(0 if "a_prefix" in CONFIG_TEMPLATE else 1)
PY
# Дефолты «сообщение один в один» и «автор — тот, кто переносит» должны быть
# описаны там же, где человек и агент их ищут, иначе сюрприз обеспечен.
doc "keep_author" "$ROOT/config.example.toml" "пример конфига описывает keep_author"
doc "trailer" "$ROOT/config.example.toml" "пример конфига описывает trailer"
doc "keep_author" "$ROOT/README.md" "README описывает keep_author"
doc "--reset-author" "$ROOT/README.md" "README описывает флаги авторства"
doc "git_timeout" "$ROOT/README.md" "README описывает git_timeout"
doc "keep_author" "$ROOT/skills/git-xfer/SKILL.md" "скилл знает про keep_author"
doc "squash" "$ROOT/config.example.toml" "пример конфига описывает squash"
doc "--squash" "$ROOT/README.md" "README описывает squash"
# Скилл обязан СПРАШИВАТЬ про схлопывание, а не решать сам: обещание
# «одним коммитом или по одному» должно быть прописано словами.
doc "одним или по одному" "$ROOT/skills/git-xfer/SKILL.md" "скилл спрашивает про схлопывание"
# Скилл обязан прямым текстом запрещать интерактивные вызовы: именно на них
# агент без терминала встаёт намертво.
doc "Никогда не запускай" "$ROOT/skills/git-xfer/SKILL.md" "скилл запрещает интерактив списком"
doc "Если команда не отвечает" "$ROOT/skills/git-xfer/SKILL.md" "и учит, что делать при зависании"
python3 - "$ROOT" <<'TEMPLATE_PY' && ok "шаблон init описывает новые ключи" || bad "шаблон init описывает новые ключи"
import sys
sys.path.insert(0, sys.argv[1])
from gitxfer.config import CONFIG_TEMPLATE
sys.exit(0 if all(k in CONFIG_TEMPLATE for k in ("keep_author", "trailer", "squash", "git_timeout")) else 1)
TEMPLATE_PY

if sed -n '/Что не входит в эту версию/,$p' "$ROOT/README.md" | grep -q "префикс"; then
  bad "пункт про префикс убран из «что не входит»"
else
  ok "пункт про префикс убран из «что не входит»"
fi

printf '%s\n' "$MAIN_CONFIG" > "$WORK/config/git-xfer/config.toml"

echo "== 42. авторство и трейлер: дефолт, флаги, конфиг =="
src_commit() { # файл содержимое сообщение дата
  printf '%s\n' "$2" > "$WORK/a/$1"
  git -C "$WORK/a" add "$1"
  GIT_AUTHOR_NAME="Ann Source" GIT_AUTHOR_EMAIL=ann@example.com \
  GIT_COMMITTER_NAME="Ann Source" GIT_COMMITTER_EMAIL=ann@example.com \
  GIT_AUTHOR_DATE="$4" GIT_COMMITTER_DATE="$4" git -C "$WORK/a" commit -q -m "$3"
}
src_commit "src/alpha.py" "def alpha(): return 1" "feat: alpha" "2024-05-01T09:00:00+00:00"
src_commit "src/beta.py" "def beta(): return 2" "feat: beta" "2024-05-02T09:00:00+00:00"
ALPHA=$(git -C "$WORK/a" log --format='%H %s' | grep 'feat: alpha' | cut -d' ' -f1)
BETA=$(git -C "$WORK/a" log --format='%H %s' | grep 'feat: beta' | cut -d' ' -f1)
xfer cleanup -p t --to b --state >/dev/null 2>&1
xfer sync -p t --to b >/dev/null

# Флаги разово возвращают прежнее поведение.
xfer apply -p t --to b --sha "$ALPHA" --yes --keep-author --trailer >/dev/null 2>&1
check 0 $? "перенос с --keep-author --trailer прошёл"
check "Ann Source" "$(git -C "$WORK/b" log -1 --format='%an')" "--keep-author вернул автора оригинала"
# keep_author трогает только author: committer'ом в git всегда остаётся тот,
# кто применил коммит, — так эта пара и должна выглядеть.
check "Bob Target" "$(git -C "$WORK/b" log -1 --format='%cn')" "а коммиттером всё равно остался тот, кто переносит"
check "2024-05-01" "$(git -C "$WORK/b" log -1 --format='%ad' --date=short)" "и его author date"
has "cherry picked from commit $ALPHA" "$(git -C "$WORK/b" log -1 --format=%B)" "--trailer вернул трейлер"

# То же самое, но из конфига профиля и без единого флага.
cat > "$WORK/keep.toml" <<EOF
[defaults]
keep_author = true
trailer = true

[profiles.t]
a = "$WORK/a"
b = "$WORK/b"
branch = "master"

[profiles.plain]
a = "$WORK/a"
b = "$WORK/b"
branch = "master"
keep_author = false
trailer = false
EOF
OUT=$(xfer status --config "$WORK/keep.toml" -p t --to b 2>&1)
has "автор исходного коммита сохраняется" "$OUT" "status показывает режим авторства"
has "трейлер" "$OUT" "status показывает режим сообщения"
xfer apply --config "$WORK/keep.toml" -p t --to b --sha "$BETA" --yes >/dev/null 2>&1
check 0 $? "перенос по конфигу с keep_author/trailer прошёл"
check "Ann Source" "$(git -C "$WORK/b" log -1 --format='%an')" "keep_author из конфига сработал"
has "cherry picked from commit $BETA" "$(git -C "$WORK/b" log -1 --format=%B)" "trailer из конфига сработал"

# Профиль перебивает [defaults], флаг перебивает профиль.
OUT=$(xfer status --config "$WORK/keep.toml" -p plain --to b 2>&1)
has "автором станет тот, кто переносит" "$OUT" "профиль перебивает [defaults]"
git -C "$WORK/b" reset -q --hard HEAD~1
xfer cleanup -p t --to b --state >/dev/null 2>&1
xfer apply --config "$WORK/keep.toml" -p t --to b --sha "$BETA" --yes --reset-author --no-trailer >/dev/null 2>&1
check "Bob Target" "$(git -C "$WORK/b" log -1 --format='%an')" "--reset-author перебивает конфиг"
check 0 "$(git -C "$WORK/b" log -1 --format=%B | grep -c 'cherry picked from commit' || true)" "--no-trailer перебивает конфиг"
OUT=$(xfer apply -p t --to b --sha "$BETA" --yes --keep-author --reset-author 2>&1); CODE=$?
check 1 $CODE "--keep-author вместе с --reset-author отвергнуты"

echo "== 44. squash: серия схлопывается в один коммит =="
src_commit "src/gamma.py" "def gamma(): return 3" "feat: gamma" "2024-05-03T09:00:00+00:00"
src_commit "src/delta.py" "def delta(): return 4" "feat: delta" "2024-05-04T09:00:00+00:00"
src_commit "src/eps.py" "def eps(): return 5" "feat: eps" "2024-05-05T09:00:00+00:00"
GAMMA=$(git -C "$WORK/a" log --format='%H %s' | grep 'feat: gamma' | cut -d' ' -f1)
DELTA=$(git -C "$WORK/a" log --format='%H %s' | grep 'feat: delta' | cut -d' ' -f1)
EPS=$(git -C "$WORK/a" log --format='%H %s' | grep 'feat: eps' | cut -d' ' -f1)
xfer cleanup -p t --to b --state >/dev/null 2>&1
xfer sync -p t --to b >/dev/null
BEFORE=$(git -C "$WORK/b" rev-parse HEAD)
OUT=$(xfer apply -p t --to b --sha "$GAMMA" "$DELTA" "$EPS" --yes --squash 2>&1); CODE=$?
check 0 $CODE "перенос со --squash прошёл"
check 1 "$(git -C "$WORK/b" rev-list --count "$BEFORE"..HEAD)" "в цели появился ровно один коммит"
has "Схлопнуто в один коммит" "$OUT" "итог сказал, что схлопнул"
BODY=$(git -C "$WORK/b" log -1 --format=%B)
has "feat: gamma" "$BODY" "сообщение собрано из первого коммита"
has "feat: eps" "$BODY" "и из последнего"
check 3 "$(git -C "$WORK/b" show --name-only --format= HEAD | grep -c '^src/' || true)" "все три файла приехали одним коммитом"
check "Bob Target" "$(git -C "$WORK/b" log -1 --format='%an')" "автор схлопнутого — тот, кто переносит"
# Маппинг обязан указывать на существующий коммит, иначе дедупликация решит,
# что ничего не переносилось.
OUT=$(xfer list -p t --to b)
check 3 "$(printf '%s\n' "$OUT" | grep -c '− .*feat: \(gamma\|delta\|eps\)' || true)" "все три помечены как перенесённые"

echo "== 45. squash: своё сообщение, конфиг, конфликт =="
git -C "$WORK/b" reset -q --hard "$BEFORE"
xfer cleanup -p t --to b --state >/dev/null 2>&1
xfer apply -p t --to b --sha "$GAMMA" "$DELTA" --yes --squash --message "feat: гамма и дельта разом" >/dev/null 2>&1
check "feat: гамма и дельта разом" "$(git -C "$WORK/b" log -1 --format=%s)" "--message задаёт сообщение схлопнутого"
OUT=$(xfer apply -p t --to b --sha "$EPS" --yes --message "просто так" 2>&1); CODE=$?
check 1 $CODE "--message без --squash — ошибка вызова"
has "без --squash" "$OUT" "и объяснено почему"

cat > "$WORK/squash.toml" <<EOF
[defaults]
squash = true

[profiles.t]
a = "$WORK/a"
b = "$WORK/b"
branch = "master"
EOF
OUT=$(xfer status --config "$WORK/squash.toml" -p t --to b 2>&1)
has "вся серия в один коммит" "$OUT" "status показывает режим схлопывания"
git -C "$WORK/b" reset -q --hard "$BEFORE"
xfer cleanup -p t --to b --state >/dev/null 2>&1
xfer apply --config "$WORK/squash.toml" -p t --to b --sha "$GAMMA" "$DELTA" --yes >/dev/null 2>&1
check 1 "$(git -C "$WORK/b" rev-list --count "$BEFORE"..HEAD)" "squash из конфига сработал без флага"
git -C "$WORK/b" reset -q --hard "$BEFORE"
xfer cleanup -p t --to b --state >/dev/null 2>&1
xfer apply --config "$WORK/squash.toml" -p t --to b --sha "$GAMMA" "$DELTA" --yes --no-squash >/dev/null 2>&1
check 2 "$(git -C "$WORK/b" rev-list --count "$BEFORE"..HEAD)" "--no-squash перебивает конфиг"

# Схлопывание живёт поверх обычного цикла, поэтому обязано пережить паузу
# на конфликте: continue доигрывает очередь и сворачивает уже её результат.
git -C "$WORK/b" reset -q --hard "$BEFORE"
xfer cleanup -p t --to b --state >/dev/null 2>&1
printf 'l1\nl2\nTARGET-AGAIN\nl4\nl5\n' > "$WORK/b/src/app.py"
git -C "$WORK/b" -c user.name=Bob -c user.email=bob@example.com commit -q -am "tgt: снова правка app"
CLASH=$(git -C "$WORK/a" log --format='%H %s' master | grep 'конфликтует' | cut -d' ' -f1)
BEFORE2=$(git -C "$WORK/b" rev-parse HEAD)
xfer apply -p t --to b --sha "$GAMMA" "$CLASH" "$DELTA" --yes --squash >/dev/null 2>&1
check 3 $? "squash-серия встала на конфликте"
printf 'l1\nl2\nRESOLVED-SQUASH\nl4\nl5\n' > "$WORK/b/src/app.py"
git -C "$WORK/b" add src/app.py
OUT=$(xfer continue -p t --to b 2>&1); CODE=$?
check 0 $CODE "continue довёл squash-серию"
check 1 "$(git -C "$WORK/b" rev-list --count "$BEFORE2"..HEAD)" "и схлопнул её в один коммит"
has "RESOLVED-SQUASH" "$(cat "$WORK/b/src/app.py")" "разрешение конфликта уехало в итоговый коммит"
check "" "$(git -C "$WORK/b" status --porcelain=v2)" "после схлопывания дерево чистое"

echo "== 46. падение после созданного коммита оставляет годный state =="
# Коммит уже создан, а следующий за ним шаг (amend авторства) упал: state
# обязан знать, что шаг состоялся, иначе continue упрётся в уехавший HEAD,
# а повторный apply продублирует коммит.
git -C "$WORK/b" reset -q --hard "$BEFORE2" 2>/dev/null || git -C "$WORK/b" reset -q --hard HEAD
xfer cleanup -p t --to b --state >/dev/null 2>&1
src_commit "src/zeta.py" "def zeta(): return 6" "feat: zeta" "2024-05-06T09:00:00+00:00"
src_commit "src/eta.py" "def eta(): return 7" "feat: eta" "2024-05-07T09:00:00+00:00"
ZETA=$(git -C "$WORK/a" log --format='%H %s' | grep 'feat: zeta' | cut -d' ' -f1)
ETA=$(git -C "$WORK/a" log --format='%H %s' | grep 'feat: eta' | cut -d' ' -f1)
xfer sync -p t --to b >/dev/null
BEFORE3=$(git -C "$WORK/b" rev-parse HEAD)
XDG_CONFIG_HOME="$WORK/config" XDG_STATE_HOME="$WORK/state" PYTHONPATH="$ROOT" \
  python3 - "$WORK" "$ZETA" "$ETA" <<'BREAK_PY' >/dev/null 2>&1
import sys
from pathlib import Path
from gitxfer import transfer
from gitxfer.config import adhoc_profile
from gitxfer.errors import XferError
from gitxfer.gitcmd import Git
from gitxfer.state import State

work, zeta, eta = Path(sys.argv[1]), sys.argv[2], sys.argv[3]
profile = adhoc_profile(
    name="t", source=work / "a", source_branch="master",
    target=work / "b", target_branch="master",
)
git, state = Git(work / "b"), State.load(work / "b")
calls = {"n": 0}
real = transfer.reset_author
def boom(*args, **kwargs):
    calls["n"] += 1
    # Первый коммит доводим честно, на втором падаем ПОСЛЕ cherry-pick.
    if calls["n"] == 2:
        raise XferError("подстроенный сбой на доводке авторства")
    return real(*args, **kwargs)
transfer.reset_author = boom
try:
    transfer.start(git, profile, state, [zeta, eta], transfer.Options())
except XferError:
    sys.exit(0)
sys.exit(1)
BREAK_PY
check 0 $? "подстроенный сбой на втором коммите случился"
check 2 "$(git -C "$WORK/b" rev-list --count "$BEFORE3"..HEAD)" "оба коммита в истории: второй успел закоммититься"
OUT=$(xfer status -p t --to b 2>&1)
has "сделано 2" "$OUT" "state знает, что оба шага состоялись"
hasnt "HEAD не там" "$OUT" "и не считает HEAD уехавшим"
OUT=$(xfer continue -p t --to b 2>&1); CODE=$?
check 0 $CODE "continue закрывает серию, а не требует разбираться руками"
check 2 "$(git -C "$WORK/b" rev-list --count "$BEFORE3"..HEAD)" "и ничего не продублировал"

echo "== 47. squash: вырожденные случаи и откат =="
git -C "$WORK/b" reset -q --hard "$BEFORE3"
xfer cleanup -p t --to b --state >/dev/null 2>&1
src_commit "src/theta.py" "def theta(): return 8" "feat: theta" "2024-05-08T09:00:00+00:00"
THETA=$(git -C "$WORK/a" log --format='%H %s' | grep 'feat: theta' | cut -d' ' -f1)
xfer sync -p t --to b >/dev/null
# Схлопывать нечего, но сообщение просили — потерять его молча нельзя.
OUT=$(xfer apply -p t --to b --sha "$THETA" --yes --squash --message "feat: одна тета" 2>&1)
check "feat: одна тета" "$(git -C "$WORK/b" log -1 --format=%s)" "--message применён и к одному коммиту"
has "схлопывать было нечего" "$OUT" "и сказано, что схлопывать было нечего"

# Между reset --soft и commit ветка стоит отмотанной: сорвавшийся коммит
# обязан вернуть её, а не оставить серию доступной только через reflog.
git -C "$WORK/b" reset -q --hard "$BEFORE3"
xfer cleanup -p t --to b --state >/dev/null 2>&1
BEFORE4=$(git -C "$WORK/b" rev-parse HEAD)
XDG_CONFIG_HOME="$WORK/config" XDG_STATE_HOME="$WORK/state" PYTHONPATH="$ROOT" \
  python3 - "$WORK" "$ZETA" "$ETA" <<'ROLLBACK_PY' >/dev/null 2>&1
import sys
from pathlib import Path
from gitxfer import transfer
from gitxfer.config import adhoc_profile
from gitxfer.errors import XferError
from gitxfer.gitcmd import Git
from gitxfer.state import State

work, zeta, eta = Path(sys.argv[1]), sys.argv[2], sys.argv[3]
profile = adhoc_profile(
    name="t", source=work / "a", source_branch="master",
    target=work / "b", target_branch="master",
)
git, state = Git(work / "b"), State.load(work / "b")
# Пустое сообщение — git откажется коммитить ровно посередине схлопывания.
transfer.collected_message = lambda *a, **kw: ""
try:
    transfer.start(git, profile, state, [zeta, eta], transfer.Options(squash=True))
except XferError:
    sys.exit(0)
sys.exit(1)
ROLLBACK_PY
check 0 $? "сорвавшееся схлопывание сообщило об ошибке"
check 2 "$(git -C "$WORK/b" rev-list --count "$BEFORE4"..HEAD)" "ветка возвращена: оба коммита серии на месте"
check "" "$(git -C "$WORK/b" status --porcelain=v2)" "и дерево не осталось раскуроченным"
xfer cleanup -p t --to b --state >/dev/null 2>&1
git -C "$WORK/b" reset -q --hard "$BEFORE4"

echo "== 48. серия из прошлой версии доигрывается своими правилами =="
# В state, записанном до появления ключей, их нет — и такая серия обязана
# доиграться прежним поведением (трейлер + автор оригинала), а не новым.
git -C "$WORK/b" reset -q --hard "$BEFORE4"
xfer cleanup -p t --to b --state >/dev/null 2>&1
CLASH=$(git -C "$WORK/a" log --format='%H %s' master | grep 'конфликтует' | cut -d' ' -f1)
printf 'l1\nl2\nTARGET-OLD\nl4\nl5\n' > "$WORK/b/src/app.py"
git -C "$WORK/b" -c user.name=Bob -c user.email=bob@example.com commit -q -am "tgt: правка под конфликт"
xfer sync -p t --to b >/dev/null
xfer apply -p t --to b --sha "$CLASH" "$THETA" --yes >/dev/null 2>&1
check 3 $? "серия встала на конфликте"
python3 - "$WORK" <<'STRIP_PY'
import json, sys
from pathlib import Path
# Убираем ключи из opts — так выглядит state, записанный прошлой версией.
for path in (Path(sys.argv[1]) / "state" / "git-xfer").glob("*.json"):
    data = json.loads(path.read_text(encoding="utf-8"))
    progress = data.get("in_progress")
    if not progress:
        continue
    for key in ("trailer", "keep_author", "squash", "message"):
        progress["opts"].pop(key, None)
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
STRIP_PY
OUT=$(xfer status -p t --to b 2>&1)
has "трейлер" "$OUT" "status показывает опции старой серии, а не новые дефолты"
printf 'l1\nl2\nRESOLVED-OLD\nl4\nl5\n' > "$WORK/b/src/app.py"
git -C "$WORK/b" add src/app.py
xfer continue -p t --to b >/dev/null 2>&1
check 0 $? "continue доиграл серию из прошлой версии"
# Коммит, доигранный из очереди уже после подмены state, идёт прежними
# правилами: с трейлером и с автором оригинала. Конфликтный сюда не входит —
# его cherry-pick стартовал ещё до подмены, и `--continue` лишь доводит
# ровно тот вызов, без -x.
has "cherry picked from commit" "$(git -C "$WORK/b" log -1 --format=%B)" "коммит из очереди получил трейлер, как и начиналось"
check "Ann Source" "$(git -C "$WORK/b" log -1 --format='%an')" "и автора оригинала, как и начиналось"
check "Ann Source" "$(git -C "$WORK/b" log -2 --format='%an' | tail -1)" "конфликтный тоже сохранил автора оригинала"

echo "== 49. обратное направление без трейлера =="
# Маппинг в state заведён на целевой репозиторий и в обратную сторону
# не читается — скилл и README обязаны обещать именно это.
xfer cleanup -p t --to b --state >/dev/null 2>&1
git -C "$WORK/b" reset -q --hard "$BEFORE4"
xfer sync -p t --to b >/dev/null
xfer apply -p t --to b --sha "$ZETA" --yes >/dev/null 2>&1
OUT=$(xfer list -p t --to b)
has "− .*feat: zeta" "$OUT" "в ту же сторону коммит помечен как перенесённый"
OUT=$(xfer list -p t --to a 2>&1)
hasnt "− .*feat: zeta" "$OUT" "а в обратную — не помечен: маппинг односторонний"
doc "маппинг в state заведён на целевой репозиторий" "$ROOT/skills/git-xfer/SKILL.md" "скилл честно про обратное направление"

echo "== 43. новые ключи конфига проверяются =="
bad_key() { # файл строка-ключа
  printf '[defaults]\n%s\n[profiles.x]\na = "%s"\nb = "%s"\nbranch = "master"\n' \
    "$2" "$WORK/a" "$WORK/b" > "$1"
}
bad_key "$WORK/bad-author.toml" 'keep_author = "yes"'
OUT=$(xfer status --config "$WORK/bad-author.toml" -p x --to b 2>&1); CODE=$?
check 1 $CODE "keep_author строкой — ошибка конфига"
has "true или false" "$OUT" "и сказано, что ждали"
bad_key "$WORK/bad-timeout.toml" 'git_timeout = -5'
OUT=$(xfer status --config "$WORK/bad-timeout.toml" -p x --to b 2>&1); CODE=$?
check 1 $CODE "отрицательный git_timeout — ошибка конфига"
bad_key "$WORK/no-timeout.toml" 'git_timeout = 0'
xfer status --config "$WORK/no-timeout.toml" -p x --to b >/dev/null 2>&1
check 0 $? "git_timeout = 0 значит «без ограничения», а не ошибку"
# Потолок должен и правда прерывать зависший git, а не только считаться.
python3 - "$ROOT" <<'TIMEOUT_PY' && ok "git, севший ждать, прерывается по таймауту" || bad "git, севший ждать, прерывается по таймауту"
import sys
sys.path.insert(0, sys.argv[1])
from gitxfer.gitcmd import Git, GitTimeout
git = Git(sys.argv[1], timeout=1)
try:
    # Команда, которая никогда не кончится сама, — так выглядит зависание.
    git.run("-c", "alias.hang=!sleep 30", "hang")
except GitTimeout:
    sys.exit(0)
sys.exit(1)
TIMEOUT_PY

echo
echo "Проверок пройдено: $PASS, провалено: $FAIL"
[ "$FAIL" -eq 0 ]
