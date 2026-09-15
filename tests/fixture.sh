#!/bin/sh
# Стенд для smoke.sh: два несвязанных репозитория с одинаковым стартовым
# деревом и разошедшимися историями. Внутри есть всё, на чём git-xfer может
# споткнуться: конфликтующая правка, дубль уже имеющегося изменения,
# корневой коммит, бинарный файл, переименование и merge-коммит.
#
#   sh tests/fixture.sh <каталог>   → <каталог>/a (источник), <каталог>/b (цель)
set -e
ROOT="$1"
rm -rf "$ROOT/a" "$ROOT/b"
mkdir -p "$ROOT/a" "$ROOT/b"
export GIT_AUTHOR_NAME="Ann Source" GIT_AUTHOR_EMAIL=ann@example.com
export GIT_COMMITTER_NAME="Ann Source" GIT_COMMITTER_EMAIL=ann@example.com

seed() {
  cd "$1"
  git init -q -b master .
  git config user.name "$2"; git config user.email "$3"
  printf 'project\n' > README.md
  mkdir -p src docs
  printf 'l1\nl2\nl3\nl4\nl5\n' > src/app.py
  printf 'notes\n' > docs/notes.md
  git add -A
  GIT_AUTHOR_DATE="$4" GIT_COMMITTER_DATE="$4" git commit -q -m "init: скелет проекта"
}

ci() { # msg date
  GIT_AUTHOR_DATE="$2" GIT_COMMITTER_DATE="$2" git commit -q -m "$1"
}

seed "$ROOT/a" "Ann Source" ann@example.com "2024-01-01T09:00:00+00:00"
seed "$ROOT/b" "Bob Target" bob@example.com "2024-01-02T09:00:00+00:00"

# --- target: свои коммиты
cd "$ROOT/b"
export GIT_AUTHOR_NAME="Bob Target" GIT_AUTHOR_EMAIL=bob@example.com
export GIT_COMMITTER_NAME="Bob Target" GIT_COMMITTER_EMAIL=bob@example.com
printf 'l1\nl2\nTARGET\nl4\nl5\n' > src/app.py
git add -A; ci "target: правка app" "2024-02-10T09:00:00+00:00"
printf 'shared\n' > shared.txt
git add -A; ci "общая правка: shared.txt" "2024-02-11T09:00:00+00:00"

# --- source: коммиты на перенос
cd "$ROOT/a"
export GIT_AUTHOR_NAME="Ann Source" GIT_AUTHOR_EMAIL=ann@example.com
export GIT_COMMITTER_NAME="Ann Source" GIT_COMMITTER_EMAIL=ann@example.com
printf 'def feature():\n    return 42\n' > src/feature.py
git add -A; ci "feat: новая фича" "2024-02-01T09:00:00+00:00"

printf 'l1\nl2\nSOURCE\nl4\nl5\n' > src/app.py
git add -A; ci "fix: правка app (конфликтует)" "2024-02-02T09:00:00+00:00"

printf 'shared\n' > shared.txt
git add -A; ci "общая правка: shared.txt" "2024-02-03T09:00:00+00:00"

mkdir -p assets
printf '\000\001\002\003BINARY\377\376' > assets/logo.bin
git add -A; ci "chore: бинарный ассет" "2024-02-04T09:00:00+00:00"

git mv docs/notes.md docs/guide.md
printf 'notes\nдополнение\n' > docs/guide.md
git add -A; ci "docs: переименование и правка" "2024-02-05T09:00:00+00:00"

git checkout -q -b side
printf 'side\n' > side.txt
git add -A; ci "side: ветка" "2024-02-06T09:00:00+00:00"
git checkout -q master
GIT_AUTHOR_DATE="2024-02-07T09:00:00+00:00" GIT_COMMITTER_DATE="2024-02-07T09:00:00+00:00" \
  git merge -q --no-ff -m "merge: влили side" side
printf 'tail\n' > tail.txt
git add -A; ci "chore: хвостовой коммит" "2024-02-08T09:00:00+00:00"
echo "fixture ready"

# --- стенд для переноса со сменой префикса ------------------------------
# m — монорепозиторий, проект лежит в apps/mobile и появляется там
#     не сразу (второй коммит): так проверяется проекция родителя в пустое
#     дерево. n — тот же проект в корне отдельного репозитория.
# Стартовое содержимое проекта в m и n совпадает байт в байт: на этом
# проверяется, что patch-id с --relative сходится по обе стороны.
rm -rf "$ROOT/m" "$ROOT/n"
mkdir -p "$ROOT/m" "$ROOT/n"

app_files() { # каталог-корень проекта
  mkdir -p "$1/icons" "$1/illustrations"
  printf 'fun main() {\n    println("app")\n}\n' > "$1/app.kt"
  printf '<svg id="logo"/>\n' > "$1/icons/logo.svg"
  printf '<svg id="hero"/>\n' > "$1/illustrations/hero.svg"
}

# --- m: монорепозиторий
mkdir -p "$ROOT/m"
cd "$ROOT/m"
git init -q -b master .
git config user.name "Ann Source"; git config user.email ann@example.com
export GIT_AUTHOR_NAME="Ann Source" GIT_AUTHOR_EMAIL=ann@example.com
export GIT_COMMITTER_NAME="Ann Source" GIT_COMMITTER_EMAIL=ann@example.com
mkdir -p ios tools
printf 'monorepo\n' > README.md
printf 'import UIKit\n' > ios/app.swift
printf 'echo build\n' > tools/build.sh
git add -A; ci "init: монорепо" "2024-03-01T09:00:00+00:00"

app_files "$ROOT/m/apps/mobile"
git add -A; ci "init: подпроект" "2024-03-02T09:00:00+00:00"

printf '<svg id="star"/>\n' > apps/mobile/icons/star.svg
git add -A; ci "feat: звезда" "2024-03-03T09:00:00+00:00"

printf 'echo build --release\n' > tools/build.sh
git add -A; ci "chore: сборка" "2024-03-04T09:00:00+00:00"

printf '<svg id="star" v="2"/>\n' > apps/mobile/icons/star.svg
printf 'echo build --release --fast\n' > tools/build.sh
git add -A; ci "fix: звезда и сборка" "2024-03-05T09:00:00+00:00"

git mv apps/mobile/icons/star.svg ios/star.svg
git add -A; ci "move: звезда уехала в ios" "2024-03-06T09:00:00+00:00"

printf 'fun main() {\n    println("MONO")\n}\n' > apps/mobile/app.kt
git add -A; ci "fix: правка app.kt" "2024-03-07T09:00:00+00:00"

mkdir -p apps/mobile/assets
printf '\000\001\002BINARY\377' > apps/mobile/assets/logo.bin
git add -A; ci "chore: бинарник" "2024-03-08T09:00:00+00:00"

# Ветка, влитая merge-ом: после слияния её коммит TREESAME по первому
# родителю, и без --full-history git спрятал бы его из обхода по pathspec.
# Сам merge задевает обе половины монорепо — на нём проверяется пометка
# «частичный» у merge-коммита.
git checkout -q -b side
printf '<svg id="side"/>\n' > apps/mobile/icons/side.svg
printf 'import Side\n' > ios/side.swift
git add -A; ci "side: и в подпроекте, и снаружи" "2024-03-09T09:00:00+00:00"
git checkout -q master
GIT_AUTHOR_DATE="2024-03-10T09:00:00+00:00" GIT_COMMITTER_DATE="2024-03-10T09:00:00+00:00" \
  git merge -q --no-ff -m "merge: влили side" side

# --- n: личный репозиторий, проект в корне
cd "$ROOT/n"
git init -q -b master .
git config user.name "Bob Target"; git config user.email bob@example.com
export GIT_AUTHOR_NAME="Bob Target" GIT_AUTHOR_EMAIL=bob@example.com
export GIT_COMMITTER_NAME="Bob Target" GIT_COMMITTER_EMAIL=bob@example.com
app_files "$ROOT/n"
git add -A; ci "init: личный репозиторий" "2024-03-02T10:00:00+00:00"

printf 'fun main() {\n    println("SOLO")\n}\n' > app.kt
git add -A; ci "fix: своя правка app.kt" "2024-03-09T09:00:00+00:00"

printf '<svg id="extra"/>\n' > icons/extra.svg
git add -A; ci "feat: доп. иконка" "2024-03-10T09:00:00+00:00"

echo "fixture ready (prefix)"
