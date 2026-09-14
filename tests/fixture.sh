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
