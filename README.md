# git-xfer

[Русская версия](README.ru.md).

Transfers commits between two **unrelated** git repositories — repositories
that have no common ancestor and never had a common remote.

The typical case: the same project lives in two working copies (different
hosts, different machines, different clients). The histories are independent,
but changes must go both ways. Standard tools do not help here: `merge` needs
a common history, mirroring transfers the whole repository, and plain
`git cherry-pick` can neither show what was already transferred nor survive
a conflict in the middle of a series.

## The shortest path — via an agent

**The first time**, give the agent the link and say it in words:

> Install the tool `https://github.com/StasPotapov/git-xfer`. It contains
> a skill — use it to transfer commit `a1b2c3d` from `~/dev/repo-a`
> to `~/dev/repo-b`

The agent does the rest by itself: `skills/git-xfer/SKILL.md` is a
self-contained instruction. With it, the agent installs the tool, offers to
connect the skill, asks for the paths and branches, creates the config, checks
readiness, shows what was already transferred and where conflicts will be,
and applies after your "yes".

**After that**, you do not need the link or the installation — you call the
skill directly. In Claude Code this is `/git-xfer`, or just words: "transfer
this commit to my personal repository". A hash or a subject is enough.

`SKILL.md` is plain text, so this is not only about Claude Code: a link to it
is enough for any agent that can run commands in a terminal.

**As a Claude Code plugin** — one installation brings both the skill and the CLI:

```
/plugin marketplace add StasPotapov/sp-tools
/plugin install git-xfer@sp-tools
```

The skill is then called `/git-xfer:git-xfer`, and `git-xfer` is in the agent's `PATH`. You need
`python3` 3.11 or newer. The plugin does not put the utility in the `PATH` of your terminal: for manual
work, install it as described in [Installation](#installation). New version: `/plugin marketplace update sp-tools`,
then `claude plugin update git-xfer@sp-tools` (in the terminal). Do not install the plugin and the skill symlink together —
the skill will load twice.

### Why a tool, if the agent can already cherry-pick

With plain `cherry-pick`, the agent has nothing to rely on. It does not
remember what it transferred last time — and it silently duplicates a commit
or skips a needed one. It cannot tell a conflict from "the commit became
empty because this change is already there". It hits a conflict on the third
commit of ten and leaves the repository in the middle of an operation. You
have to check everything — which is exactly what you wanted to get rid of.

`git-xfer` takes from the agent everything that can be computed: what was
already transferred, where conflicts will be, how to finish a series after
a pause. The agent decides *what* to transfer and resolves conflicts.

## Installation

You need Python ≥ 3.11 (for `tomllib`) and git ≥ 2.45 (for `cherry-pick --empty`).
`git-xfer doctor` and every `apply` (it runs the same preflight checks) check the git
version first and fail on an older git with `версия git — X, нужен 2.45+` (git version: X, need 2.45+).
There are no third-party dependencies — only the standard library.

```bash
uv tool install git+https://github.com/StasPotapov/git-xfer
# or
pipx install git+https://github.com/StasPotapov/git-xfer
```

Call it **with a hyphen** — `git-xfer`. The form `git xfer` with a space also
works, but it depends on how git resolves subcommands.

<details>
<summary><b>Without installation: a clone</b> — a symlink, a wrapper for a specific python, or running as a module</summary>

```bash
git clone https://github.com/StasPotapov/git-xfer.git ~/tools/git-xfer
python3 --version          # must be 3.11 or newer
```

Then use any of three ways. The update is the same in every case: `git pull`
in the clone, nothing to reinstall.

**1. A symlink in `PATH`**

```bash
mkdir -p ~/.local/bin
ln -s ~/tools/git-xfer/bin/git-xfer ~/.local/bin/git-xfer
# if ~/.local/bin is not in PATH yet:
echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.zshrc && exec zsh
```

Do not change the file name: `git-xfer` is both the command name and the
name by which git finds the subcommand. A shell alias instead of a symlink
does not work — git does not see it.

**2. A wrapper instead of a symlink — when you need a specific interpreter**

```bash
cat > ~/.local/bin/git-xfer <<'EOF'
#!/bin/sh
exec python3.11 "$HOME/tools/git-xfer/bin/git-xfer" "$@"
EOF
chmod +x ~/.local/bin/git-xfer
```

This helps where an old `python3` comes first in `PATH` — on macOS this is
usually the system 3.9.

**3. No `PATH` at all — as a module**

```bash
PYTHONPATH=~/tools/git-xfer python3 -m gitxfer list -p myproj --to b
```

In this variant neither `git-xfer` nor `git xfer` works — only
`python3 -m gitxfer`.

</details>

<details>
<summary><b>It does not work — what to check</b></summary>

**`git: 'xfer' is not a git command`** — the utility is not in `PATH`. From a
clone that you only downloaded, `git xfer` cannot work: git looks in `PATH`
for an executable file `git-xfer`. Install it or add a symlink. Until then,
call it from the clone directory: `python3 -m gitxfer …` or `./bin/git-xfer …`.

**`command -v git-xfer` prints nothing** — the symlink exists, but
`~/.local/bin` is not in `PATH`. To check: `echo $PATH | tr : '\n' | grep local/bin`.

**`attempted relative import with no known parent package`** — you ran a
package file directly (`python3 gitxfer/cli.py`). This is not allowed. Recent
versions answer this with a hint instead of a traceback.

**`нужен Python 3.11 или новее`** (Python 3.11 or newer is needed) — the `python3`
in `PATH` is too old (on macOS the system one is usually 3.9). Install through
`uv`/`pipx`, they pin the interpreter, or call `python3.11 -m gitxfer …`.

**`FileNotFoundError` from `os.getcwd()`** — the directory where the terminal
is located was moved or deleted. A `cd` to an existing directory is enough.
Recent versions do not fail on this.

**Something failed for an unclear reason** — there is a log: `git-xfer status`
shows its path, by default `~/.local/state/git-xfer/git-xfer.log`. It contains
every git call with its exit code and the full traceback of the last error.

</details>

## Config

The standard location is `~/.config/git-xfer/config.toml` (`XDG_CONFIG_HOME`
is respected). The utility only reads it; you edit it by hand.
`git-xfer init` creates a template.

**A profile is a pair of repositories, not a direction.** You select the
direction at call time with the `--to` flag, so one pair needs one profile,
not two.

```toml
[defaults]
scan_limit = 30         # how many source commits to show
dedup_window = 5000     # how deep we remember what was already transferred
patchid_window = 1000   # window of the ≈ heuristic
trailer = false         # do not append (cherry picked from commit …)
keep_author = false     # the author of the transferred commit is the person who transfers
keep_coauthors = false  # Co-authored-by lines are removed
squash = false          # do not squash the series into one commit
git_timeout = 600       # limit for one git call, seconds (0 — no limit)
log = true              # run log

[profiles.myproj]
a = "/path/to/repo-a"
b = "/path/to/repo-b"
branch = "master"          # one branch on both sides

[profiles.myproj-release]  # the same pair, other branches
a = "/path/to/repo-a"
b = "/path/to/repo-b"
a_branch = "release/1.x"   # if the branches have different names
b_branch = "stable"

[profiles.myapp]           # the project is in a subdirectory of a monorepo
a = "/path/to/monorepo"
a_prefix = "apps/mobile"
b = "/path/to/personal"    # here the project is at the root — b_prefix is not needed
branch = "master"
```

`--to b` takes from `a` to `b`, `--to a` — back. Without the flag, in a
terminal, the utility asks.

You can override `trailer`, `keep_author`, `keep_coauthors`, `squash` and the windows inside a profile —
the profile overrides `[defaults]`, and a call flag overrides the profile.

Four keys decide how the transferred commit looks. All four are `false`
by default. For the first three this means "the commit is yours, with no
traces of others", for `squash` — "you get as many commits as you selected":

| key | `false` — default | `true` |
|---|---|---|
| `trailer` | no `(cherry picked from commit <sha>)` note | `(cherry picked from commit <sha>)` is appended to the end of the message |
| `keep_author` | the author is you, `author date` is the moment of the transfer | the author and their date stay from the original commit |
| `keep_coauthors` | `Co-authored-by:` lines are removed from the message | the message stays as is |
| `squash` | you get as many commits as you selected | the whole selected series becomes one commit |

One-time flags have the same names and work both ways: `--trailer` /
`--no-trailer`, `--keep-author` / `--reset-author`, `--keep-coauthors` /
`--strip-coauthors`, `--squash` / `--no-squash`. What is in effect right now —
three lines in the output of `git-xfer status`.

### What arrives in the target repository

By default a commit is transferred as if you wrote it yourself: **the message
is the same**, but without notes and without `Co-authored-by:` lines, **the
author and the committer are you**, the author date is the moment of the transfer.

A git commit has two fields, and people mix them up all the time: **author** —
who wrote the change (and `author date`), **committer** — who applied it in
this history. A normal `cherry-pick` keeps the author from the original and
sets you as the committer — hence "two people in the commit". "You" means
`user.name` and `user.email` of the repository **to which** we transfer, on
the machine where the transfer runs.

| `keep_author` | author | committer |
|---|---|---|
| `false` — default | you, date — the moment of the transfer | you |
| `true` | the original author and their date | you |

The committer has no separate setting: in git it is always the person who
applied the commit, and to replace it would mean to attribute the work to
another person.

You can turn all of this back on, together or separately:

```toml
[defaults]
trailer = true         # + the line (cherry picked from commit <sha>)
keep_author = true     # author and author date stay from the original
keep_coauthors = true  # Co-authored-by lines stay in the message
```

For one run, without editing the config: `--trailer` / `--no-trailer`,
`--keep-author` / `--reset-author`, `--keep-coauthors` / `--strip-coauthors`.
`git-xfer status` shows what is in effect now in the lines "Сообщение:"
(Message) and "Авторство:" (Authorship) — and if a series has already
started, it shows the options of that series: the series finishes with the
options it started with.

### Several commits as one

You get as many commits in the target as you selected. If you need one common commit:

```bash
git-xfer apply -p myproj --to b --commits 1-6 --yes \
  --squash --message "feat: profile screen"
```

Without `--message`, the message is the messages of the series in a row, from
old to new. Permanently — `squash = true` in `[defaults]` or in the profile;
a one-time exception — `--no-squash`.

Two combinations that you should know about in advance:

- with `trailer` on, the combined message has one
  `(cherry picked from commit …)` line for each included commit — they go
  as is and do not merge into one. Give `--message` if you do not need them;
- with `keep_author` on, the author of the squashed commit is the author of
  the **first** commit of the series (and its author date): one commit can
  have only one author.

Squashing is not a separate mechanism: the series is applied as usual, commit
by commit, and only at the very end it is folded (`reset --soft` to the start
point plus one `commit`). So conflicts, the pause and `continue` work without
exceptions, and what is folded is the already resolved result. One consequence:
`abort` in the middle of a squash series leaves the already applied commits
separate — at that moment there is nothing to fold yet.

The cost of the disabled trailer is deduplication. The trailer is the most
reliable channel: it lives in the target history itself and so it works from
any machine and in both directions. Without it, the utility remembers "already
transferred" only by its state (the directory `~/.local/state/git-xfer`, tied
to the target repository on this machine) and by patch-id — a heuristic that
marks a commit with `≈` but does not hide it.

This also tells you what not to expect from the state: it is **one-way**.
The file belongs to the repository into which you transferred, and it is not
read when the direction changes — on the way back, already transferred commits
are marked `≈`, not `−`. If you transfer in both directions, from two machines,
or clean the state — turn on `trailer = true`.

### Project in a subdirectory

Sometimes the same project is at the root in one repository and in a
subdirectory of a monorepo in the other. Then the paths in commits do not
match: `apps/mobile/icons/logo.svg` against just `icons/logo.svg`, and a
transfer "as is" would create extra nesting. You declare the subdirectory
with the key `<side>_prefix`, like `a_prefix` in the example above.

**The path of a side is always the repository root.** You cannot put the
subdirectory directly in `a`: `doctor` notices this and shows how to rewrite
the profile. For one run, without a config, the flags `--source-prefix` and
`--target-prefix` do the same.

Then everything works as usual, but with three consequences:

- **`--to b` strips the prefix, `--to a` adds it.**
- **Commits that do not touch the subdirectory are not in the list.** So
  `--limit` counts the commits of the project, not the commits of the monorepo —
  otherwise a window of 30 commits could contain no needed commit at all.
- **A commit that touches both the subdirectory and files outside it is
  transferred partially** — only the inside part. Such a line is marked in
  the list with an asterisk (`+ *`), so that this does not happen silently.

A transfer back deletes nothing outside the subdirectory: the base of the
three-way merge contains only the subtree under the prefix, so for the merge
the other directories of the monorepo look like "added by us" and stay in place.

### Commits to skip

Internal commits, drafts, changes for one side only — you can describe them
with rules, and by default they are not transferred:

```toml
[defaults.skip]
subject = ["(?i)internal", "^wip\\b"]  # regexes on the commit subject
commits = ["a1b2c3d"]                  # specific hashes, short ones are allowed
paths = ["*.lock", ".idea/*"]          # a commit made only of such files

[profiles.myproj.skip]                 # a profile has its own, in addition to the common ones
subject = ["^chore\\(ci\\)"]
```

A rule does not hide a commit — it marks it with `×` and a reason.
Then:

- `list --new` and the "not in target" block of `compare` show it separately,
  it is not among the candidates;
- `--commits all` skips it and reports this in one line;
- if you name it explicitly (by number, range, `--sha`, in interactive selection),
  in a terminal you get the question "transfer anyway?", and without a terminal —
  a refusal with code `1`, until you add `--include-skipped`.

`paths` are relative to the project root — the subdirectory of the side is
already stripped, so one rule works in both directions of the pair. A commit
is excluded only if **all** its files match the masks: in masks `*` also
matches `/`. A typo in a key or a broken regex is a config error, not a
silently disabled rule.

<details>
<summary><b>Config in its own place, branches outside the profile, work with no config at all</b></summary>

Your own path to the config, if you do not want to touch `~/.config`:

```bash
git-xfer init --config ~/tools/git-xfer.local.toml
git-xfer list --config ~/tools/git-xfer.local.toml -p myproj --to b
```

`--config` works both before the subcommand and after it. To not type it each
time — `alias gx='git-xfer --config ~/tools/git-xfer.local.toml'`. If you
keep the config inside the clone, name the file `something.local.toml` —
`.gitignore` excludes such files, and real paths do not get into the history.

You do not need a profile for one run on another branch:

```bash
git-xfer list -p myproj --to b -b feature/login    # both sides
git-xfer list -p myproj --to b --source-branch feature/login --target-branch integration
```

A flag for one side does not touch the other side: with the profile, `-p myproj --to b
--source-branch feature/login` takes `feature/login` from the source and
puts it into the target branch from the config. The name "the same" is used
only where nobody knows about the other side — that is, without a profile.
A one-time run has its own service ref, so it does not affect normal profiles.

You can also work with no config at all:

```bash
git-xfer list --source ~/dev/repo-a --target ~/dev/repo-b -b master
git-xfer apply --ask          # asks for repositories and branches
```

</details>

<details>
<summary><b>Working state and log</b> — where they are and how to turn them off</summary>

The utility keeps the patch-id cache, the mapping of transferred commits and
an unfinished queue in `~/.local/state/git-xfer/` (`XDG_STATE_HOME` is
respected) — outside the working repositories, nothing gets into git.
The run log is there too: which git commands ran, with which code, how long
they took, on which commit the series stopped and with which error.

```bash
git-xfer status -p myproj --to b     # shows the path to the log and to the state
tail -50 ~/.local/state/git-xfer/git-xfer.log
```

The log rotates (2 MB, 5 files) and is on by default. To turn it off —
`log = false` in `[defaults]` or `--no-log` for one run; to send it to your own
file — `log_file` in the config or `--log-file`.

</details>

## How to use it

You fill in the config once. Then:

```bash
git-xfer list   -p myproj --to b              # what can be taken
git-xfer plan   -p myproj --to b --commits 3,4   # where conflicts will be
git-xfer apply  -p myproj --to b --commits 3,4 --yes
# conflict → edit by hand, git add …
git-xfer continue -p myproj --to b
```

Source objects are fetched automatically, so you can start right
with `list`.

When the question is not "what to take" but "how did they diverge at all" — `compare`:

```bash
git-xfer compare -p myproj --to b          # what is missing in b and what is only in b
git-xfer compare -p myproj --to b --files  # plus the list of differing files
git-xfer compare -p myproj --to b --json   # the same for an agent and scripts
```

It shows which project files differ now, which commits are missing
in the target (the numbers are the same as in `list`) and which commits exist only in the target.
Under the table there are hints: the files of a commit already match on both sides
(most likely there is no reason to transfer it), the same files were changed in the target (expect
a conflict), the commit is older than the last transferred one. Everything is computed
in the target repository — the source is not touched. The [skill](#skill-for-claude-code) can
advise what to transfer based on this data. The numbers in `--commits` come from the output of `list` with the same `--limit`
and profile; instead of numbers you can use `--sha <hash>`. When you run it without selection
flags in a terminal, `apply` asks for everything itself — the profile, the direction
and the commits.

One limitation that you should know about in advance: commits go to the branch
that is currently checked out in the target repository, and the profile must name
exactly that branch. The branch in the profile is a preset for the common case, not the only
one possible: every call accepts `--target-branch <branch>` (and `--source-branch`,
and `-b` for both at once), so you do not need a profile for each branch.

```bash
git-xfer status -p myproj --to b          # the profile has master, feature/login is checked out
git-xfer apply -p myproj --to b --target-branch feature/login --commits 1 --yes
```

`status` prints the line `Ветка цели:` (Target branch): it shows both what is written
in the profile and what is actually checked out. If they differ, `doctor`
and `apply` stop with code `2` and touch nothing — switch yourself
or name the checked-out branch with a flag. If the branch does not exist at all, `doctor` says
this too, in a separate message: a typo in the name and the wish to create a new
branch look the same, and only a person can tell them apart.

Branch flags are part of the transfer address: such a call keeps the source objects
under its own ref `refs/xfer/*`. So call `cleanup` with the same flags
that you used for the transfer, or `git-xfer cleanup --all`.

### Marks in the list

| | meaning | how it is known |
|---|---|---|
| `+` | new | no channel matched |
| `−` | already transferred | the local mapping in the state, and if `trailer` is on — the trailer too |
| `≈` | looks transferred | the patch-id matched |

`≈` is a heuristic, and it **never hides a commit silently**: the line stays
in the list, you can select it, and during the transfer such a commit usually becomes
empty and is dropped automatically.

Next to the status there can be more marks — not about deduplication, but about scope
and rules; they combine with any of the three marks (`+*×`):

| | meaning |
|---|---|
| `*` | the commit also touches files outside the subdirectory; only the inside part arrives |
| `×` | the commit matched a `skip` rule — by default it is not transferred |

`*` appears only for profiles with a prefix ([project in a subdirectory](#project-in-a-subdirectory)),
`×` — only with rules in the config ([commits to skip](#commits-to-skip)).

<details>
<summary><b>What it looks like in full</b> — an interactive run in both directions</summary>

A run without arguments asks for the profile and the direction:

```console
$ git-xfer apply
Профиль:
  1. myproj
     /Users/me/dev/repo-a ↔ /Users/me/dev/repo-b  (master)
  2. myproj-release
     /Users/me/dev/repo-a ↔ /Users/me/dev/repo-b  (release/1.x / stable)
  выбор [1]: 1
Куда переносим:
  1. /Users/me/dev/repo-a (master)  →  /Users/me/dev/repo-b (master)
  2. /Users/me/dev/repo-b (master)  →  /Users/me/dev/repo-a (master)
  выбор [1]: 1
```

Then it shows what can be taken:

```console
 1 + d772cb8 2024-02-08 Ann Source chore: хвостовой коммит
 2 + 0f6ff9a 2024-02-06 Ann Source side: ветка
 3 + a2c0b9a 2024-02-05 Ann Source docs: переименование и правка
 4 + 47c2151 2024-02-04 Ann Source chore: бинарный ассет
 5 ≈ bacc39d 2024-02-03 Ann Source общая правка: shared.txt
 6 + f62bac9 2024-02-02 Ann Source fix: правка app (конфликтует)

  + новый: 5   ≈ совпал patch-id: 1   − уже перенесён: 0   (страница 1/1, коммитов 6)
Выбор [1,3,5-7 | all | !N | /текст — фильтр | n/p — страница | q — отмена]: 3,4
```

Empty input means **cancel**, not "select all". The whole input is checked
before anything happens.

Before it does anything, it prints the actual order of application —
the list is newest-first, like `git log`, and it is applied in reverse:

```console
Порядок применения (old → new), коммитов: 2
    1. 47c2151 chore: бинарный ассет
    2. a2c0b9a docs: переименование и правка
Перенести 2 коммит(ов)? [y/N]: y

[1/2] 47c2151da5f9 chore: бинарный ассет
[2/2] a2c0b9af6e39 docs: переименование и правка

Перенесено: 2; пусто: 0; пропущено: 0
```

Back is the same: at the "Куда переносим" (Where to transfer) step you select the second item.
The utility recognizes transferred commits on both sides and does not send them around in circles:

```console
$ git-xfer list -p myproj --to a
 1 − d3405fd 2024-02-05 Ann Source docs: переименование и правка
 2 − 7c72adc 2024-02-04 Ann Source chore: бинарный ассет
 3 + 758dd2e 2024-02-10 Bob Target target: правка app

  + новый: 1   ≈ совпал patch-id: 0   − уже перенесён: 2
```

The full cycle for one side, if you need all steps explicitly:

```bash
git-xfer doctor -p myproj --to b     # preflight checks
git-xfer sync   -p myproj --to b     # fetch objects source → target
git-xfer list   -p myproj --to b
git-xfer plan   -p myproj --to b -i
git-xfer apply  -p myproj --to b -i
git-xfer continue -p myproj --to b   # after the conflict is resolved
git-xfer cleanup  -p myproj --to b   # remove service refs
```

</details>

<details>
<summary><b>Conflict</b> — what to do and how this differs from the standard cherry-pick</summary>

The series stops on the conflicting commit and returns code `3`. The working tree
at this moment is the normal `cherry-pick` state: markers in the files,
an unfinished operation.

```bash
# edit the files, then
git add <files>
git-xfer continue -p myproj --to b   # finishes the commit and runs the rest of the queue
git-xfer skip     -p myproj --to b   # drop this commit, go on
git-xfer abort    -p myproj --to b   # cancel the current commit
```

`abort` here is more honest than the standard one: it rolls back **only** the commit
where the series stopped, and everything already transferred stays in place. The standard
`cherry-pick --abort` on a series would also remove all conflicts resolved earlier.

If you committed the resolution yourself (`git cherry-pick --continue` by hand),
`git-xfer continue` recognizes this and accepts it as is.

</details>

## Subcommands

| command | what it does |
|---|---|
| `init` | create a config template |
| `doctor` | preflight checks |
| `sync` | transfer objects source → target |
| `list` | table of source commits (`--new` — only those not transferred yet) |
| `compare` | what is missing in the target and what exists only there (`--files`, `--json`) |
| `plan` | dry run: where conflicts will be, without touching the working tree; computes from the target branch, even if another one is checked out |
| `apply` | transfer the selected commits |
| `continue` / `skip` / `abort` | control the pause on a conflict |
| `status` | state of an unfinished transfer |
| `cleanup` | remove `refs/xfer/*` (`--all`, `--state`) |

Common flags: `-p/--profile`, `--config`, `-v` (print git calls),
`-n/--dry-run`, `--no-log` and `--log-file`. The direction and one-time
overrides are available in all subcommands except `init`: `--to a|b`,
`-b/--branch`, `--source-branch` / `--target-branch`, `--source` /
`--target`, `--source-prefix` / `--target-prefix`, `--ask`.

<details>
<summary><b><code>apply</code> flags and exit codes</b></summary>

| flag | default |
|---|---|
| `--trailer` / `--no-trailer` | no `(cherry picked from commit …)` trailer (key `trailer`) |
| `--keep-author` / `--reset-author` | the author is reset to the person who transfers (key `keep_author`) |
| `--keep-coauthors` / `--strip-coauthors` | `Co-authored-by:` lines are removed (key `keep_coauthors`) |
| `--squash` / `--no-squash` | no squashing: commit for commit (key `squash`) |
| `--message TEXT` | message of the squashed commit; without it — the messages of the series in a row |
| `--empty=drop\|keep\|stop` | `drop` — a commit that became empty is already transferred |
| `--hooks` | hooks are disabled (`core.hooksPath=/dev/null`) |
| `--gpg-sign` | `--no-gpg-sign` |
| `--keep-committer-date` | committer date = now |
| `--allow-merges` | merge commits do not even get into the table |
| `--include-skipped` | commits under `skip` rules are not taken (also available in `plan`) |

A pair with a config key works as usual: the value from `[defaults]`,
on top of it — from the profile, on top of that — the flag of this call.

| code | meaning |
|---|---|
| `0` | success |
| `1` | error in the call, argument parsing or config |
| `2` | a preflight check failed |
| `3` | conflict, the transfer is paused — waiting for `continue` |
| `4` | git error |
| `130` | Ctrl-C |

</details>

## Skill for Claude Code

Installation and the normal scenario are described [at the beginning](#the-shortest-path--via-an-agent);
this section covers what the skill does inside and how to configure its behavior.

The instruction in `skills/git-xfer/SKILL.md` is self-contained: a link to this
repository is enough for the agent. Its order is strict — `doctor`, `sync`,
`list` (or `compare`, when you ask to compare and advise), analysis of the
marks and diffs, a proposed set of commits with reasons,
a dry run, and only then `apply`, after your "yes". It asks
at most twice: the target branch (if you did not name it) and one final
confirmation — from where, to where, which
commits, what the dry run shows. Then it finishes by itself: it stashes uncommitted
changes in the target and restores them after the transfer, switches to the needed branch
and then switches back. Interactive
flags (`-i`, `--ask`) are forbidden for it: it has no terminal, and it must not
hang on a question. It identifies common failures by a table of symptoms
and does not guess.

The only thing worth configuring is how independently it
resolves conflicts:

```toml
[agent]
resolve_conflicts = "mechanical"
```

| value | what the agent does on a conflict |
|---|---|
| `ask` | edits nothing: shows the essence and proposes a resolution |
| `mechanical` | fixes mechanical ones itself (imports, adjacent lines, formatting, generated code), gives semantic ones to you |
| `auto` | resolves everything itself and reports afterwards |

The skill reads this setting; the utility itself does nothing with it — but it checks
the value (a typo does not stay silent) and shows it in `git-xfer
status`. A one-time instruction in words overrides the config.

Turn on `auto` deliberately: a mistake in a semantic conflict goes as a commit
into the working repository and looks like a successful transfer.

## Under the hood

<details>
<summary><b>How it works</b> — fetch by path, a queue of one commit at a time, prefix projection, four deduplication channels</summary>

**Objects are transferred with `fetch` by a local path**, without `git remote add`:

```
git -C <TARGET> -c protocol.file.allow=always \
  fetch --no-tags --no-write-fetch-head -- <SOURCE> \
  '+refs/heads/<branch>:refs/xfer/<profile>/head'
```

`--no-tags` is mandatory — otherwise foreign tags settle in `refs/tags/*` of the working
repository. The created ref in `refs/xfer/*` keeps the objects from `gc`,
and the `cleanup` subcommand removes it. No remotes, no `FETCH_HEAD`,
no tags are added to the target repository.

**Commits are applied one at a time**, not with one command `cherry-pick A B C`.
With one rev, git goes into `single_pick()` and does not create the `.git/sequencer`
directory at all — so our own queue in the state gives exact progress,
options for each commit and, most importantly, an honest `abort`: it rolls back only
the current commit, while the standard `cherry-pick --abort` on a series removes
all already resolved conflicts.

**Cherry-pick across unrelated histories works without exceptions** — it is
a three-way merge with the base `C^`, no merge-base is computed. For a root
commit the base is the empty tree.

**A prefix change is a projection of the commit, not a second backend.**
`cherry-pick` does not rewrite paths, so before it a temporary commit
is synthesized: it takes the subtree `<sha>:<prefix>`, the same subtree
of the first parent, if necessary nests both under the prefix of the other
side, and joins the pair of trees into a commit with one parent. The diff
is exactly the needed set of paths, and all the rest of the mechanism — the queue,
the conflict, `continue`/`abort`, the dry run — works without a single exception.
The trailer, if it is on, is written not with the `-x` flag (that would point
to the synthetic commit), but directly into the projection message, and it points to the original.

While the transfer is paused, `CHERRY_PICK_HEAD` points to this
synthetic commit: `git show CHERRY_PICK_HEAD` shows the correct
message and author, but it has no parent from the real history. The ref
`refs/xfer/<profile>/pick` keeps it from `gc`; the ref is removed at the
end of the series, on `abort` and by the `cleanup` subcommand.

**Deduplication has four channels**, from most to least reliable: the `-x` trailer
in the target history; the trailer of the source commit itself that points to a commit
that is already in the target (this is how the reverse direction looks); the local
src→dst mapping in the state; patch-id. The two trailer channels work only
if the trailer is on (`trailer = true`) — by default there is none, and what remains is
the mapping and patch-id. The mapping also works only in the direction in which
you transferred: the state belongs to the target repository (`sha1(realpath(target))`),
and in the reverse direction it is a different file.

**Squashing is a `reset --soft` at the end, not a separate backend.** The series
is replayed the normal way, with its own queue, conflicts and `continue`;
when the queue is empty, HEAD is moved back to the start point (`progress.head_before`)
keeping the tree and the index, and one commit is made. The `src→dst` mapping
is rewritten to point to it for all included commits — otherwise deduplication
would refer to commits that are no longer in the history.

**Author and co-authors are rewritten after the commit, not during it.**
`cherry-pick` has no `--reset-author` of its own: it always takes the author and the message
from the source commit. So right after each successful step — and after
`continue` too — there is one `git commit --amend`: `--reset-author` and the message
without `Co-authored-by:` trailers, while the commit is still the tip of the branch. Only
the trailers in the last paragraph are removed, the rest of the message stays byte for byte.
There is no such step if the author is kept (`keep_author = true`) and there is nothing to remove:
there are no co-authors or `keep_coauthors = true`. The service commits of
git-xfer (this amend and the squash) always bypass hooks, even with
`--hooks`: `--hooks` means "run the hooks on the transferred commit", and
`cherry-pick` does this, while `pre-commit`, which `cherry-pick` does not have at all,
would break the series at the finishing step.

**patch-id is computed by the tool itself, `git cherry` is not used.** In `cmd_cherry()`
the `<limit>` argument is applied only *after* `get_patch_ids()`, so with
unrelated histories patch-ids are computed for the whole target history — on a large
repository this takes minutes. Here instead there is the `patchid_window` window and a cache in the state.

**The dry run does not touch the working tree**: `git merge-tree --write-tree`
in a chain over the whole queue, the output tree becomes the base for the next
commit. One exception: for a profile with a prefix, `plan` (and any command with
`-n`) still writes loose objects into the target repository — the trees and commits
of the projection. No ref points to them, they do not get into the history,
and `git gc` collects them, like the source objects after `cleanup`.

</details>

<details>
<summary><b>Limitations and side effects</b></summary>

- **The first `sync` fetches a full pack** from the tip of the source branch. There is no common
  ancestor, so negotiation has nothing to work with. Blobs are deduplicated by SHA,
  so in practice these are commit and tree objects — tens of percent of the
  repository size, but not a whole second repository.
- **`≈` is a heuristic.** `patch-id --stable` ignores whitespace differences,
  and identical diffs in different places of the history give the same id. So
  such a commit is marked, but not hidden.
- **Merge commits are not transferred by default.** `-m 1` squashes the merged
  branch into one commit — almost never what you need. The `--allow-merges` flag
  exists, but use it deliberately.
- **`--keep-committer-date` breaks the git assumption** of non-decreasing
  committer timestamps; `git log --since` and similar will start to lie.
- **Hooks are disabled.** What is transferred is an already checked commit, and a failing linter
  must not break the series in the middle of the queue. To turn them back on — `--hooks`.
- **Submodules** are transferred as a gitlink: the pointer is updated, the contents
  are not. `doctor` warns.
- **`rerere`** can silently reuse a past conflict resolution.
  `doctor` warns.
- **Differences in `core.autocrlf` / `.gitattributes`** between the repositories break
  both patch-id and the merge itself. `doctor` warns.
- **A commit on the subdirectory boundary arrives half-way** — the part outside the
  prefix stays in the source. The line is marked `*`, but the decision is yours.
- **A rename across the subdirectory boundary** becomes a deletion
  (the file moved outside) or a full addition (it moved inside): outside
  the prefix the second half of the pair simply does not exist.
- **Rename detection on a reverse transfer** sees the whole repository outside the
  prefix as "added files" and in a rare case can pair a file
  deleted inside the prefix with a similar one outside. You resolve it by hand,
  like a normal conflict.
- After `cleanup` the source objects become unreachable, and `git fsck`
  shows them as `dangling`. This is expected; `git gc` frees them.
- The config is only read. A profile with `source == target` is blocked.

</details>

<details>
<summary><b>Testing</b> — an end-to-end run on throwaway repositories</summary>

It does not touch the working repositories, everything lives in a temporary directory
and is deleted at the end:

```bash
sh tests/smoke.sh          # 356 checks
KEEP=1 sh tests/smoke.sh   # keep the test bench for analysis
```

The test bench (`tests/fixture.sh`) builds two pairs of unrelated repositories.
The first has the same initial tree and diverged histories:
a conflicting change, a duplicate of an existing change, a root commit,
a binary file, a rename and a merge commit. The second is a monorepo
with a project in a subdirectory and the same project at the root of a separate repository:
commits inside the subdirectory, outside it and on the boundary, a rename across
the boundary and a branch merged with a merge commit.

</details>

<details>
<summary><b>Not in this version</b></summary>

- a `format-patch` + `am -3` backend for the case "nothing at all may be added
  to the target repository" (the extension point is in `transfer.py`);
- an initial move of the whole history of a subdirectory at once — `git subtree split`
  does this; here selected commits are transferred.

</details>

## License

MIT.
