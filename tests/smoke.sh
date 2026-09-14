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
LOG=$(git -C "$WORK/b" log -5 --format='%H %an %ad' --date=short)
has "Ann Source 2024-02-05" "$LOG" "автор и author date сохранены"
BODY=$(git -C "$WORK/b" log -4 --format='%B')
COUNT=$(printf '%s\n' "$BODY" | grep -c "cherry picked from commit")
check 4 "$COUNT" "трейлер есть у всех четырёх, включая конфликтный"

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
has "− .*бинарный ассет" "$OUT" "перенесённый коммит помечен − по трейлеру"
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

echo
echo "Проверок пройдено: $PASS, провалено: $FAIL"
[ "$FAIL" -eq 0 ]
