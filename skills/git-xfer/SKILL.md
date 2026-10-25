---
name: git-xfer
description: Transfers commits between two unrelated git repositories through the git-xfer CLI — shows what can be taken, proposes a set, predicts conflicts and applies it after confirmation. Works in both directions. Use it when the person asks to move, transfer, port, cherry-pick or sync commits between two working copies of one project that have no shared history and no shared remote ("move this commit to the other repo", "port this fix to B", "sync the repositories"). Russian triggers too: «перенеси коммит», «перетащи правку в другой репозиторий», «перекинь коммиты», «синхронизируй репозитории».
when_to_use: When the person says "move the commits from A to B", "carry this fix over to the second repository", "what is new in that repo", "sync the repositories", "bring it back", "compare the repositories", "what is missing in <repo>", "advise what to transfer", mentions git-xfer or a profile from its config, or asks to sort out a stuck transfer ("it stopped on a conflict", "continue does not go through"). Also in Russian: «перенеси коммиты из A в B», «перекинь эту правку во второй репозиторий», «что нового в том репо», «синхронизируй репозитории», «затащи обратно», «сравни репозитории», «чего не хватает в <репо>», «посоветуй, что перенести», «он встал на конфликте», «continue не идёт».
allowed-tools: Bash, Read, Edit, AskUserQuestion
---

# git-xfer — transferring commits between unrelated repositories

The `git-xfer` CLI transfers commits between two repositories with no shared history.
A profile in its config is a **pair** of repositories; the flag
`--to a` / `--to b` sets the direction.

## 0. Make sure the tool is installed and configured

Skip this section if `git-xfer --version` already responds.

### Installation

```bash
command -v git-xfer && git-xfer --version
python3 --version              # need 3.11 or newer
git --version                  # need 2.45 or newer
```

Check the git version first. Below 2.45, `git-xfer doctor` and every `apply`
(same preflight) fail with the line «версия git — X, нужен 2.45+ ради `cherry-pick --empty`»
(git version is X, 2.45+ is needed for `cherry-pick --empty`). Tell the person
to update git (macOS: `brew install git`; Debian/Ubuntu: a newer git from the
distro backports or the git-core PPA) and stop.

No command — install it from the `StasPotapov/git-xfer` repository. It is public,
no access is needed for the clone. **Ask where to install and which way** — this changes
the person's environment:

```bash
# way 1 — straight from the repository, if uv or pipx is available
# (they pin the interpreter):
uv tool install git+https://github.com/StasPotapov/git-xfer

# way 2 — clone and symlink, no installation:
git clone https://github.com/StasPotapov/git-xfer.git <dir>
mkdir -p ~/.local/bin && ln -s <dir>/bin/git-xfer ~/.local/bin/git-xfer
```

After the symlink, check that the directory is visible: `command -v git-xfer`. Empty —
then `~/.local/bin` is not in `PATH`; tell the person.

You can also work with no installation at all, from the clone directory:
`python3 -m gitxfer ...` or `<clone>/bin/git-xfer ...`. Do not run the package files
directly (`python3 gitxfer/cli.py`) — that does not work.

Call it **with the hyphen** — `git-xfer`. The `git xfer` form with a space also
works, but it depends on how git resolves subcommands.

### Install the skill so that next time one command is enough

The skill came as a plugin (`/git-xfer:git-xfer`) — this step is not needed: both the skill and `git-xfer`
are already in place. If you read this file from a link and not from `~/.claude/skills`,
**offer the person to install it**, so that you do not have to give the link
every time. Ask: it is their environment.

```bash
mkdir -p ~/.claude/skills
ln -s <clone>/skills/git-xfer ~/.claude/skills/git-xfer
```

A symlink, not a copy: it updates together with `git pull` in the clone. After that
the skill is called as `/git-xfer`. If it was installed through `uv tool install` and there is no clone,
say that a clone is needed for this, and do not clone silently.

### Config

```bash
git-xfer status -p <any> --to b    # shows whether the config is read
```

No config (`конфиг не найден` — config not found) — create the template and fill it in:

```bash
git-xfer init                        # ~/.config/git-xfer/config.toml
```

**A profile is a PAIR of repositories, not a direction.** The direction
is chosen at call time with the `--to` flag. Ask the person for the paths to the two
repositories and the branches, then write:

```toml
[profiles.myproj]
a = "/path/to/first"
b = "/path/to/second"
branch = "master"          # one branch on both sides
# a_branch / b_branch — if the branches have different names
```

**If the project lives in a subdirectory of one of the repositories** — in the monorepo
it is in `apps/mobile`, and in the second repository the same thing is at the root —
this is a separate key, not a separate profile:

```toml
[profiles.myapp]
a = "/path/to/monorepo"     # ALWAYS the repository root
a_prefix = "apps/mobile"    # the subdirectory — a separate key
b = "/path/to/personal"     # here the project is at the root, no b_prefix needed
branch = "master"
```

The transfer itself strips `a_prefix` and adds `b_prefix`, so no extra
`apps/mobile/…` nesting appears in the target. A side path that points
inside a repository is an error: `doctor` shows a ready
fragment to rewrite the profile with. **Do not invent the prefix yourself:**
if you see a monorepo and the profile has no `*_prefix`, ask the person
which subdirectory they need.

The branch in the profile is a preset for the common case, not the only possible one:
every call accepts `-b`, `--source-branch` and `--target-branch`. So
do not create a profile per branch and do not rewrite the branch in an existing one.

**Do not transfer chore commits** — if the person asks for this
("do not carry commits with the word 'chore'"), these are `skip` rules:

```toml
[defaults.skip]                  # for all pairs; per profile — [profiles.<name>.skip]
subject = ["(?i)chore"]          # regexes on the subject line
commits = ["a1b2c3d"]            # specific hashes
paths = ["*.lock", ".idea/*"]    # a commit whose files are ALL under the masks
```

Profile rules are added to the shared ones. Propose where to put them and show
the fragment before writing it.

A profile is not required: a one-off transfer works with flags, with
no config at all.

```bash
git-xfer list --source /path/a --target /path/b -b master
```

The config is the person's file. **Do not rewrite existing profiles** and do not change
`[defaults]` without a request; you may add a new profile after you show exactly what
you add.

## Iron rule: non-interactive calls only

**Every command must finish on its own.** You have no terminal: a command
that sits waiting for input never returns — no answer, no error, the conversation
just stops. This is the most common way to break the work, and the fault here is not
the tool's but the call's.

`git-xfer` protects itself: it turns on interactive paths only with
a real terminal, and it interrupts a hung git by timeout (`git_timeout`,
600 s by default). But for the commands you call outside it, you are responsible.

Give everything explicitly:

- direction — `-p <profile> --to a|b` (or `--source`/`--target` and `-b`);
- commits — `--commits 1,3,5-7` or `--sha <hash>...`, **never** `-i`;
- confirmation — `--yes` (only after the person said "yes").

**Never run** — not with `git-xfer`, not with git, not with anything else:

| forbidden | why | instead |
|---|---|---|
| `git-xfer apply --ask`, `apply -i`, `plan -i` | wait for a person at a terminal | `--commits` / `--sha` and `--yes` |
| `git commit` without `-m`, `git rebase -i`, `git add -p`, `git mergetool` | open an editor | `-m "..."`, edit the file, `git add <file>` |
| `git cherry-pick --continue` by hand | opens the message editor | `git-xfer continue` |
| `less`, `more`, `man`, `vim`, `nano` | a pager or editor holds the terminal | `cat`, `head -50`, `git --no-pager ...` |
| `tail -f`, `watch`, `ping`, a server in the foreground | never end by design | `tail -80 <file>` once |
| `gh auth login`, `ssh` to a new host, `sudo` | ask for a password or confirmation | ask the person to do it themselves |
| an installation that asks for confirmation | waits for `y/n` | a flag like `-y`, or better — ask the person |

Git inside `git-xfer` is already defused: editor and pager
(`GIT_EDITOR`, `GIT_PAGER`), login and password prompt (`GIT_TERMINAL_PROMPT=0`,
`GIT_ASKPASS`) and ssh without `BatchMode` (`GIT_SSH_COMMAND`, only if the person
has not set their own). Protect your own git calls yourself: `--no-pager` for every
`log`/`show`/`diff`, `--no-edit` where git wants a message.

## If a command does not respond

Interrupt it and investigate; do not repeat the same command — the second time it hangs
exactly the same way.

1. **Look at which command it was.** It is in the list above — the cause is found,
   call it another way.
2. **`git-xfer` itself got stuck** — look into the log (`tail -80 <path from status>`):
   the last line `git ... → код` (→ exit code) shows which git call it is stuck on.
   If it says `git не уложился` (git did not finish in time) — the ceiling fired, and the text says on what.
3. **Long, but not forever.** On a large repository the first `list` computes
   patch-ids over the whole history and can take tens of seconds — this is not a hang
   but work, and the result is cached. If there is no time to wait: `--no-patch-id`
   (only the `≈` mark disappears) and `--limit 20`. The second run is faster.
4. **Target repository with git-lfs or hooks** — the most common external
   cause: lfs goes to the network. Tell the person; do not deal with their network
   and accounts yourself.

## Exit codes — read them, they carry meaning

| code | meaning | what to do |
|---|---|---|
| `0` | success | continue with the plan |
| `1` | call or config error | read the text, fix the command |
| `2` | preflight check failed | sort out the cause with the person, **do not bypass it** |
| `3` | conflict, series paused | see the section on conflicts |
| `4` | git error | look into the log |
| `130` | interrupted | do nothing |

## What to find out before the transfer — and not bother the person afterwards

The person wants one thing: to say **what** and **where**, to make sure you understood
correctly — and then for everything to happen on its own. So the person's decisions
are collected **in two rounds**, and the second one is the last: after it, until
the final result, you ask nothing more.

| round | when | what you ask | default you propose |
|---|---|---|---|
| **first** | right after `status` | direction (if the request leaves it unclear) and the **target branch** | branch — the one checked out now |
| **second, last** | after `plan` | one confirmation of the whole transfer (step 5) | "yes, one by one" |

Before `list` there is nothing to ask about commits — they are not visible yet; past `status`
there is no reason to delay the branch question — `doctor`, `sync` and `list` itself depend on it.

The commit set is not a choice among options but an analysis of the table, so it
is proposed as text (step 4). If the person named the commits themselves, the set
is not discussed separately: it simply goes into the confirmation.

**The confirmation is a summary, not "shall we?".** It contains everything that will happen
to the person's repository, and only in it can the person correct that:

- from → to: both repositories and both branches, by name;
- what: hashes and subjects of the commits, and what `plan` said about them (conflicts,
  empty ones);
- uncommitted changes in the target — "I will put 3 files into stash and restore them afterwards"
  (section 1b);
- branch switch — "I will switch `X` → `Y`, then go back to `X`"; branch
  creation — "branch `Y` does not exist, I will create it from `X`" (section 1a).

The question is one `AskUserQuestion` with the options "Yes, one by one", "Yes, as one
commit" (only if there is more than one commit; the message of the squashed commit goes
into the option description) and "No, adjust". The squash mode is not asked
as a separate question — it is one of the answer options.

**After "yes" — on your own to the end.** stash, branch switch, `doctor`,
`apply`, conflicts within the policy, restoring the branch and the changes from stash —
without a single question. Stop and ask only when you cannot
go on or the decision is not yours: `doctor` returned code `2` for a reason that
this flow does not remove; a semantic conflict under policy `ask`/`mechanical`;
the changes from stash did not come back cleanly.

**Ask only what you do not know yet.** Anything the person already said —
in the request itself, in the config they explicitly asked you to use, or
in an answer a minute ago — do not ask again: this is not politeness, it is noise.
"Move the last commit to `release/2.0` as one piece" leaves
no open question except the confirmation.

Answers turn into call flags, not into config edits:
`--to`, `--target-branch`, `--commits`/`--sha`, `--squash`/`--message`.

What this looks like for the most common request:

> "Move this commit to `feature/login`"

The branch is named — do not ask for it. There is one commit — nothing to ask about
squashing either. The target has `master` checked out, with two uncommitted
changes. Confirmation:

> Transfer `~/dev/a` (`master`) → `~/dev/b` (`feature/login`): `a1b2c3d fix: …`,
> plan — no conflicts. `~/dev/b` is now on `master` with 2 uncommitted
> files: I will put them into stash, switch to `feature/login`, transfer,
> go back to `master` and restore the changes. Go ahead?

After "yes":

```bash
git -C <target> stash push -m "git-xfer autostash <profile> <time>"
git -C <target> switch feature/login
git-xfer doctor -p <profile> --to b --target-branch feature/login
git-xfer apply  -p <profile> --to b --target-branch feature/login --sha <hash> --yes
git -C <target> switch master
git -C <target> stash pop --index stash@{N}     # N — found by the label
```

> "Move these four commits"

The branch is not named — ask for it in the first round. There are several commits — the mode
goes into the confirmation as an answer option.

## Workflow

### 1. Understand the pair and the direction

```bash
git-xfer status -p <profile> --to <a|b>
```

It shows the direction, the target branch, the paths to state and to the log, and whether there is
an unfinished series. **If there is a series — finish it first** (section on
conflicts); do not start a new one.

If the profile is unknown — read the config (`~/.config/git-xfer/config.toml`,
respect `XDG_CONFIG_HOME`) and show the person the list of pairs. Do not guess
the direction: ask through `AskUserQuestion` if the request does not make it
unambiguous.

### 1a. Target branch — ask, do not take it from the config silently

**The branch in the profile is not a verdict but a preset.** Commits land in the branch
of the target repository that is checked out now, and it is often not the one
written in the config: the person works in a feature branch, and the profile says
`master`. Silently committing "where the config says" is the most painful way
to ruin someone's day.

`status` prints the line `Ветка цели:` (Target branch:): it shows both what is in the profile
and what is checked out now. Next — the list of branches, so there is something to
choose from:

```bash
git -C <target> --no-pager branch --format='%(refname:short)' --sort=-committerdate
```

**Ask through `AskUserQuestion`**, with real branch names in the options,
not descriptions. The order is:

1. **checked out now** (`<name>`) — recommended: the person works in it,
   and it is exactly where the commits land if nothing changes;
2. **from the profile config** (`<name>`) — what is written in the pair;
3. **same as in the source** (`<name of the branch we take from>`) — convenient when the branch
   exists with the same name on both sides; offer this option when the name
   differs from the first two;
4. **other** — the person names it (the "Other" option in the question).

Options often have the same names — then do not duplicate them in the question,
show each once.

Options 1 and 2 are often the same name — **this is no reason not to ask**.
The question costs one screen, and a commit that went to the wrong branch of a working
repository costs the person an evening. So ask **every time**, except
two cases:

- **the branch is named in the request itself** ("move this commit to `release/2.0`") —
  take it, do not ask again;
- **the person already answered this question in this conversation** — do not ask
  a second time in a row.

In both cases still say in one line where the transfer goes:
"transferring to `release/2.0`" — so that a mistake surfaces before `apply`, not after.

Pass the chosen branch **explicitly, to every command**:

```bash
git-xfer list -p <profile> --to <a|b> --target-branch <branch>
```

`--target-branch` changes only the target; the source branch stays from the profile.
Need a different one for the source too — `--source-branch`; both at once — `-b`.

Branch flags are part of the address: such a transfer has its own `refs/xfer/*`. So
call `cleanup` with the same flags too, otherwise it says «ссылок не найдено» (no refs found),
and `doctor` will then keep mentioning forgotten refs forever. To remove everything at once —
`git-xfer cleanup --all`.

**If the chosen branch is not checked out**, `apply` does not go to it: commits
land in the checked-out one. What to do about it depends on whether the branch exists
at all (`git -C <target> rev-parse --verify --quiet refs/heads/Y`; `doctor`
says the same):

| branch state | what you do |
|---|---|
| exists, but `X` is checked out (`doctor`: `сейчас 'X', а нужна 'Y'` — currently 'X', but 'Y' is needed) | confirmation item "I will switch `X` → `Y`, then go back to `X`"; after "yes" — `git -C <target> switch Y`, after the transfer — `switch X` |
| does not exist (`doctor`: `ветки 'Y' в этом репозитории нет` — there is no branch 'Y' in this repository) | **a separate line** in the confirmation: "branch `Y` does not exist — I will create it from `X`, the transferred commits will be its first"; after "yes" — `git -C <target> switch -c Y`, after the transfer — `switch X` |

Do neither **before** the confirmation — it is someone else's working
tree. Name the branch creation in the summary separately and directly: a typo in the name
looks exactly like the wish to create a new one, and only the person
sees the difference.

Always go back to `X`: the person worked there, and their
uncommitted changes (section 1b) must return there. A dirty
tree does not block the switch — the stash happens before `switch`.

You may run `plan` before the switch: it computes the forecast from the target branch
in the flag, not from the checked-out one.

If branch `Y` does not exist, `list` and `plan` with `--target-branch Y` fail: there is
nothing to look at yet. Before the confirmation call them with `--target-branch X` — the new branch
starts exactly from `X`, so both the list and the forecast are the same. Pass
the set as hashes (`--sha`), and `doctor` and `apply` after `switch -c Y` —
already with `--target-branch Y`.

**Do not edit** the config for the chosen branch. A one-off choice is a flag; if the person
wants to make it permanent, they will ask, and then it is a separate profile, not an edit
of the existing one.

### 1b. Uncommitted changes in the target — stash and restore them yourself

`doctor` and `apply` require a clean target tree. A dirty one is no reason
to stop and chase the person: the changes go into stash for the duration of the
transfer and come back afterwards. This is not asked separately — it is a line
in the confirmation.

**Check right after `status`**, before `list`:

```bash
git -C <target> status --porcelain --untracked-files=no
git -C <target> stash list --format='%gd %s'
```

The first one is empty — nothing to stash, the rest of this section is not needed. Do not stash
untracked files (do not add `-u` without a request): `doctor` does not block on them, it
only warns that the transfer may overwrite them — relay that
warning. If the second output already has a line
`git-xfer autostash` — these are changes that did not come back last time: tell
the person in one line and restore them together with the current ones at the end (or right away,
if there is no transfer). Do not delete them silently.

**Stash with a label**, only after "yes":

```bash
git -C <target> stash push -m "git-xfer autostash <profile> <YYYY-MM-DDTHH:MM:SS>"
git -C <target> stash list --format='%gd %s' | grep -F 'git-xfer autostash <profile> <time>'
```

Restore **exactly your own**, so search by the label, not with a bare `stash pop`:
the person may have their own stashes, and `pop` without an argument opens the top one,
whatever it is. For the same reason check that the stash was created: on a clean
tree git answers `No local changes to save` with code `0` and creates
nothing — then there is nothing to restore either.

**Order**: stash → `switch` (if needed) → `doctor` → `apply` → `switch`
back (if you switched) → restore the changes.

**Restore** — find the number again by the label (it may have shifted):

```bash
git -C <target> stash pop --index stash@{N}
```

`--index` also restores what was in the index (`git add`). If git
refused precisely because of the index (`Conflicts in index`) — repeat without
`--index` and tell the person that the staged state is lost, but the changes are not.

**Pop with a conflict** — the transferred commits edited the same lines as
the uncommitted work. git leaves the stash in place (`The stash entry is
kept`) and markers in the files. Resolve by the same `resolve_conflicts` policy
as transfer conflicts (section "Conflicts"): the lower half
(`Stashed changes`) is the person's work, the upper half (`Updated upstream`) is what
is now in the branch. If you did not switch, this is the transferred content; if you transferred
into another branch and came back — this is the content of the original branch, not the transfer,
and then the conflict is not about the transfer at all. After resolving and checking the markers:

```bash
git -C <target> reset -q                # clear unmerged state and everything pop left in the index
git -C <target> stash drop stash@{N}    # N — again by the label
```

After a conflict the staged state is not restored, even with `--index`:
the changes come back, but all of them unstaged. Say this in the summary.

**Do not commit** the result — it is the person's uncommitted work, it must
stay uncommitted. If the policy does not let you decide yourself — do not touch the stash,
show the conflict and wait: the changes are safe while the stash is in place.

**The transfer stopped on a conflict (code `3`)** — do not touch the stash until the series
ends (`continue` ran through, `skip` or `abort`); do not switch the branch back either
— HEAD must not move between the pause and `continue`. The changes
come back at the very end.

**In the summary** always say what happened to the changes: restored; restored,
but the staged state is lost; or they lie in `stash@{N}` with the label — and why.

### 2. Check readiness

```bash
git-xfer doctor -p <profile> --to <a|b>
```

Before the confirmation `doctor` surveys the situation, it does not deliver a verdict: on
a dirty tree and a branch that is not checked out it returns code `2`, and this is normal —
sections 1a and 1b remove them. The final `doctor` runs after the stash and
`switch`, right before `apply`.

Code `2` there — transfer nothing. The most common causes:

- **branch ≠ configured** or **dirty tree** after you already
  switched and stashed — then something went wrong: look at
  `git status` and do not bypass it;
- **unfinished cherry-pick** — see the section on conflicts;
- warnings (`rerere.enabled`, `core.autocrlf`, submodules) do not block,
  but relay them to the person — they explain future oddities.

### 3. See what can be taken

```bash
git-xfer list -p <profile> --to <a|b>
```

Table: number, mark, short hash, date, author, subject.

| mark | meaning |
|---|---|
| `+` (U+002B) | new, not transferred |
| `−` (U+2212, minus, not hyphen) | already transferred — mapping in state, or the trailer if it is on |
| `≈` (U+2248) | patch-id matched: most likely the same change |

A profile with a prefix gets a second mark — about scope, not about
deduplication:

| mark | meaning |
|---|---|
| `*` (U+002A) | the commit also touches files outside the subdirectory — only the inside part arrives |

One more — about the rules from the config:

| mark | meaning |
|---|---|
| `×` (U+00D7) | the commit matches `skip` (chore, hash in the list, only chore files) — not transferred by default |

**Tell the person about `*` before `apply`, always**: part of the change
stays in the source, and this is their decision, not yours. Commits that do not touch
the subdirectory at all do not appear in the list — so `--limit` counts project commits,
not monorepo commits.

Row numbers are what goes into `--commits`, and they are valid **only with the same
`--limit` and profile**. If you change `--limit` — reread the list. If nothing
was found, show `--limit 100`: the default window is 30 commits.

### 3a. Compare and advise what to transfer

When the request is not "transfer this" but "compare", "what is missing",
"what is worth transferring" — start with `compare` instead of a bare `list`:

```bash
git-xfer compare -p <profile> --to <a|b> --target-branch <branch> --json
```

The JSON is everything the advice rests on:

| key | what it is |
|---|---|
| `files` | which project files differ now: `differ`, `source_only`, `target_only` |
| `missing[]` | source commits that are not in the target, newest-first; `number` — the same as in `list` |
| `missing[].files` | the commit's files with `added`/`deleted` lines (`null` — binary) |
| `missing[].in_sync` | all its files are now identical on both sides |
| `missing[].after_last_transferred` | newer than the last transferred one; `null` — no boundary in the window |
| `missing[].overlaps_target_only` | "target only" commits that touched the same files |
| `missing[].skipped` | the reason, if it matches a `skip` rule; otherwise `null` |
| `target_only[]` | what exists only in the target (reverse pass) |
| `warnings[]` | what could not be computed (for example, a broken source state) — relay it to the person |

Next, look at the changes themselves. The source objects are already in the target
repository, so read everything from there:

```bash
git -C <target> --no-pager show --stat <sha>     # for each candidate
git -C <target> --no-pager show <sha>            # full diff — for small ones
```

What to weigh for each commit:

- **`in_sync: true` or `≈`** — the changes are already in the target (or they were later
  reverted in the source itself): no reason to transfer, it becomes empty;
- **a commit and its revert** — skip both; **a fixup to an already transferred one**
  (`fixup!`, "typo in …" right after `−`) — take it, otherwise the target keeps
  the error;
- **dependencies**: B edits what A added — take them together and say
  so; taking only B is an almost guaranteed conflict;
- **source-specific**: CI, environment addresses and settings, version
  bump, files that are not in the target and should not be (look at
  `files.source_only`) — into "questionable", not into the set;
- **`overlaps_target_only`** — the same files were edited only in the target: expect
  a conflict, warn in advance;
- **`*`** — only part arrives, say which;
- **`after_last_transferred: false`** — old history before the last
  sync, most likely skipped on purpose: do not propose by default,
  but mention how many such commits there are;
- **`skipped`** — do not propose at all, only list them in one line.

"Target only" commits do not need to be transferred — that is the other direction. But
mention them: if one edits the same place as a candidate, there will be
a conflict, and it is better for the person to know before `plan`.

The answer to the person — three groups, each commit with a short reason:

```
Take (4):         3 a1b2c3d fix: …        — a fix, not in the target
Questionable (2): 5 d4e5f6a ci: …         — edits .gitlab-ci.yml, target CI is different
Skip (3):         7 0a1b2c3 ≈ …           — already in the target
Chore (1):        1 9f8e7d6 × …           — skip rule
```

and a ready selection by hashes — `--sha a1b2c3d …`: it does not depend on `--limit`.
If the person asked only for advice — stop here. If they asked
to transfer — then the usual path: `plan` on "Take" and the confirmation (step 5),
where "Questionable" goes as a separate line — the person adds them with the answer
"No, adjust" if they want.

### 4. Propose a set

Analyze the table and propose a concrete set to the person with reasoning: what
we take, what we skip and why. Do not wait for a separate answer on the set — right away
run `plan` on it and bring it to the confirmation (step 5): it is accepted or
corrected there. By default take only `+`. Propose a commit with `≈`
only if the person asks for exactly that one — and warn that it
will most likely become empty and be dropped.

**Do not propose a commit with `×`.** If the person named such a commit themselves —
ask through `AskUserQuestion`: "`<hash> <subject>` is marked as chore
(rule: `<reason>`). Transfer it anyway?" — and only after "yes" add
`--include-skipped`. Without the flag the tool does not take such a commit (code `1`),
and `--commits all` skips it on its own. Do not add or remove `skip` rules in the config
until the person asks.

Merge commits are not shown in the list and are not transferred. Do not add
`--allow-merges` on your own initiative: `-m 1` squashes the merged branch into one
commit, and that is almost never what is needed.

### 4a. If there is more than one commit — squash into one or one by one

**More than one commit is selected — do not decide yourself**, but do not ask a separate
question either: these are answer options in the confirmation (step 5). The person often
wants one commit to appear in the other repository, not six of their
drafts; but just as often the history matters as it is. The options show
exactly what arrives:

- **one by one** (default) — as many as were selected arrive, each
  with its own message;
- **as one commit** (`--squash`) — the whole series folds into one, the messages
  are collected in a row; set your own message with `--message "..."`.

Also propose the message text if squashing is chosen: a collected wall
of six drafts is usually worse than one meaningful line. Show it to
the person before `apply`, not after.

No need to ask when: there is one commit; the person said "as one commit"
or "as is" themselves; the config has `squash = true` and they know about it —
then just say which mode you use.

Squashing sits on top of the normal transfer: conflicts, pause and `continue`
work exactly the same, the series folds at the very end. But `abort` in the middle
of a squash series leaves the already applied commits **separate** — this is not
a breakage, there was simply nothing to fold yet.

### 5. Show the dry run

```bash
git-xfer plan -p <profile> --to <a|b> --commits <selection>
```

The working tree is not touched; a dirty tree and a branch that is not checked out do not
block `plan` — run it before the stash and `switch`. Show the person the result as
it is: where the conflicts will be and in which files, which commits become empty,
the actual application order (the list is newest-first,
applied in reverse).

**Ask for the confirmation here — once, for everything**: the summary from the section
"What to find out before the transfer" plus the `plan` result, options "Yes, one by one" /
"Yes, as one commit" / "No, adjust". A "no" with a correction — a new
summary with the correction and a new question, not a piecemeal re-ask.

### 6. Apply — and finish the job

After "yes", without questions:

1. stash with a label, if the target has uncommitted changes (1b);
2. `switch` / `switch -c`, if the branch is not checked out (1a);
3. `doctor` — must return `0`;
4. `apply`;
5. `switch` back to the original branch;
6. restore the changes from stash by the label (1b);
7. summary in one message: what was transferred / empty / skipped, where the target
   HEAD is now, what happened to the uncommitted changes.

```bash
git-xfer apply -p <profile> --to <a|b> --commits <selection> --yes
# as one commit, if the person chose so:
git-xfer apply -p <profile> --to <a|b> --commits <selection> --yes \
  --squash --message "feat: short meaningful message"
```

Use `--yes` only after an explicit "yes". Code `0` — continue down the list (restore
the branch and the changes, summary). Code `3` — a conflict: do not touch the branch and the stash until
the series is finished.

**What arrives in the target by default:** the commit message without a single addition
and without `Co-authored-by:` lines; the author and committer is the person who transfers,
author date — the moment of the transfer; as many commits arrive as were selected.
A git commit has two fields: author (who wrote it) and committer (who applied it).
`keep_author = true` puts the original author with their date back into author;
the committer is in any case the person who transfers — this is git, not a setting.
With `keep_author` a squashed commit gets the author of the first commit of the series,
and with `trailer` on — one trailer per included commit.

If someone needs it otherwise — the keys `keep_author`, `keep_coauthors`, `trailer` and `squash`
in the config (one-off: `--keep-author`, `--keep-coauthors`, `--trailer`,
`--squash`). What is in effect now, `git-xfer status` prints in the lines
«Сообщение:» (Message:), «Авторство:» (Authorship:)
and «Схлопывание:» (Squash:); for a started series it shows the series' own options,
not the config.

The trailer `(cherry picked from commit ...)` is off for a reason, and there is
a price: without it the tool remembers "already transferred" only by its own
state (this machine) and by patch-id (the `≈` heuristic). If the person is surprised
that an already transferred commit shows as `+` again — the cause is almost always
this: the state was deleted (`cleanup --state`) or the transfer runs from another machine.
Do not turn anything on yourself: say that `trailer = true` would help, and leave
the decision to the person.

## Conflicts

The transfer stopped, code `3`. How free you are to fix it yourself is set by the policy
from the config; `git-xfer status` prints it in the line `Конфликты: <политика>` (Conflicts: <policy>).
A one-off instruction from the person ("fix it yourself", "touch nothing") overrides the config.

| policy | what you do |
|---|---|
| `ask` | you edit nothing: show the essence, propose a resolution, wait for the answer |
| `mechanical` | fix mechanical ones yourself and show what you did; take semantic ones to the person |
| `auto` | fix everything yourself, including semantic ones, and report afterwards |

For a profile with a prefix, `git show CHERRY_PICK_HEAD` on a pause shows
a synthetic commit: message and author are correct, but it has no parent from the real
history. This is normal, not a broken state — resolve the conflict as
usual.

**What counts as mechanical.** Both sides edited different lines of one hunk;
order or set of imports; indentation and formatting; generated files
and lock files; adding different items to one list or dictionary; a conflict
entirely in comments. The sign is simple: **both edits are needed, and you can merge them
without deciding which one is right.**

**What is not mechanical — and under `mechanical` goes to the person.** Two
different implementations of the same thing; a diverged condition or edge
case; a changed signature or contract; edits where one side
undoes the other; a conflict you do not understand. **If in doubt — it is
semantic.** The cost of a mistake here is silently committed wrong code in a working
repository, and it will look like a successful transfer.

Procedure:

1. `git-xfer status -p ... --to ...` — on which commit it stopped, how many
   remain and which policy applies;
2. `git -C <target> diff --name-only --diff-filter=U` — what conflicts;
3. read the files and **understand both changes**. Message of the commit being transferred:
   `git -C <target> log -1 --format=%B CHERRY_PICK_HEAD`. History of the file on the target
   side: `git -C <target> log -3 --oneline -- <file>`. In the file itself
   the upper half (`<<<<<<< HEAD`) is the target side, the lower one is the transferred side;
4. then by the policy. Whatever you choose — **tell the person what exactly
   was in the conflict and how it is resolved**; do not pass silently even under `auto`;
5. `git -C <target> add <files>`;
6. `git-xfer continue -p <profile> --to <a|b>` — finishes the commit and runs the rest
   of the queue. The series can stop again on the next commit — this is normal,
   repeat from step 2.

Conflict markers (`<<<<<<<`, `=======`, `>>>>>>>`) must disappear from the file
completely. Check before `add`:

```bash
git -C <target> grep -n '^<<<<<<<\|^>>>>>>>' -- <file>
```

Empty output and code `1` — no markers, all good (for `grep` this is the normal
"nothing found", not an error). Any output — keep fixing the file.

Other exits: `git-xfer skip` (skip this commit, continue with the rest),
`git-xfer abort` (roll back **only the current** commit; already transferred ones stay).

The series is over — `continue` returned `0`, or there was `skip`/`abort` on
the last commit — finish the tail from step 6: branch back, changes from stash,
summary. Do not forget the stash on pause: it waits exactly for this moment.

What not to do:
- **do not call `cleanup --state` to "get rid of" the problem** — it forgets
  the unfinished series and leaves the repository as it is. Only on an explicit request;
- do not touch `git cherry-pick --abort` by hand when `git-xfer` runs the series —
  it has its own `abort`, which does not wipe what is already resolved;
- do not move HEAD between the pause and `continue` — the tool notices this and refuses
  to guess, and it will be right.

## If something does not work

| symptom | cause and what to do |
|---|---|
| `git: 'xfer' is not a git command` | the tool is not in `PATH` — see section 0 |
| `command -v git-xfer` is silent after the symlink | `~/.local/bin` is not in `PATH`, tell the person |
| `attempted relative import with no known parent package` | a package file was run directly; use `python3 -m gitxfer` or `bin/git-xfer` |
| `нужен Python 3.11 или новее` (Python 3.11 or newer is required) | the `python3` in `PATH` is old; install through `uv`/`pipx` or call `python3.11 -m gitxfer` |
| `FileNotFoundError` from `os.getcwd()` | the directory the terminal sits in was deleted or moved — `cd` into an existing one |
| `конфиг не найден` (config not found) | `git-xfer init` and fill it in, or work through `--source`/`--target` |
| `не описан ни один профиль` (no profile is defined) | the template is created, but no profiles are written in |
| code `2` on `doctor` | the repository is not ready; the cause is in the output, sort it out with the person |

If you do not understand what happened — **look into the log**, that is what it is for.

## When something is unclear — the log

```bash
git-xfer status -p <profile> --to <a|b>   # shows the path
tail -80 ~/.local/state/git-xfer/git-xfer.log
```

It has every git call with its exit code and time, every step of the series, and errors
with a traceback. This is the first place to look on code `4` and on any
"why did it behave like that".

## Reverse direction

The same pair, a different `--to`.

**With the trailer** (`trailer = true`) transferred commits are visible from both sides: the record
lives in the target history itself, so the reverse list honestly marks such
commits `−`, and they do not go around in circles.

**Without the trailer — and this is the default now** — the reverse direction does not
recognize them that way: the mapping in state is keyed to the target repository of the transfer
that already happened, and it is not read when the direction changes. On the way back
such commits show as `≈` (patch-id matched), not `−`. This is not a failure:
tell the person that there is nothing here to mark them `−` with, and that `≈` on transfer
will almost certainly become an empty commit and be dropped. Do not transfer
anything "just in case" yourself.

## One-off branches — without editing the config

```bash
git-xfer list -p <profile> --to b -b feature/login          # both sides
git-xfer list -p <profile> --to b --source-branch feature/x # source only
git-xfer list --source <path> --target <path> -b master     # no config at all
```

A flag for one side does not touch the other: with a profile, the other side's branch
comes from the config.

## What you must not do

- run `apply` without the person's explicit consent to a specific set of commits;
- bypass `doctor` that returned code `2` right before `apply`;
- resolve a semantic conflict silently if the policy is not `auto`;
- leave conflict markers in a committed file;
- transfer commits "just in case" that the person did not ask for;
- add `--include-skipped` without an explicit "yes" for a specific commit with `×`;
- commit to a branch the person did not choose — the branch from the config is
  not consent by itself;
- switch or create a branch in a working repository without naming it
  in the transfer confirmation; or not switch back to the original one afterwards;
- take from stash what is not yours: only the entry with the label `git-xfer autostash`,
  no bare `stash pop`, no `stash drop` of other entries, no `stash clear`;
- hide changes with `reset --hard`, `checkout -- .` or `--force` instead of
  stash; use `stash -u` without a request;
- commit the changes that came back from stash — they stay uncommitted;
- leave the person's changes in stash and not say so;
- invent `a_prefix`/`b_prefix` for the person if the project clearly lives
  in a subdirectory and the profile says nothing about it.
