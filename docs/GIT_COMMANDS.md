# Committing and pushing to GitHub

Everything here is run from the project folder:

    cd /workspace/project

## The everyday three commands

    git add -A
    git commit -m "what you changed"
    git push origin main

That is the whole routine. `git add -A` stages every change, `git commit`
records them locally, `git push` sends them to GitHub.

## See where you stand

    git status              # what changed, what is staged
    git log --oneline -5    # the last five commits
    git diff                # the exact lines that changed, not yet staged

## The first time only: identity and remote

Already set on this machine, but on a new machine:

    git config user.name  "Your Name"
    git config user.email "you@example.com"
    git remote add origin https://github.com/rrajareddy234-gif/Shiva-sagar-hostel.git

## Pushing needs a token

GitHub will not accept your account password. It needs a personal access token
with the `repo` scope.

Do NOT put the token on the command line. It ends up in the shell history and in
the process list. Use the helper instead, which reads it from a file:

    echo 'ghp_YOUR_TOKEN' > /workspace/.github-push-token
    chmod 600 /workspace/.github-push-token
    ./scripts/push-to-github.sh

The script checks the token can reach the repository, pushes, and then deletes
the token file. Afterwards, revoke the token at
https://github.com/settings/tokens, because it is still in this chat log.

### Doing it by hand, if you prefer

    git push https://x-access-token:YOUR_TOKEN@github.com/rrajareddy234-gif/Shiva-sagar-hostel.git main

The token must be deleted from your shell history afterwards.

## Undo mistakes

    git restore FILE              # throw away uncommitted changes to one file
    git restore .                 # throw away all uncommitted changes
    git reset --soft HEAD~1       # undo the last commit, keep the changes
    git commit --amend -m "better message"   # fix the last commit message

`git reset --hard` throws work away permanently. Avoid it.

## Branching, when you want to try something risky

    git checkout -b my-experiment   # make a branch and switch to it
    git push -u origin my-experiment
    git checkout main               # go back to main

On GitHub, open a pull request from the branch into `main` to review before
merging.

## What this project has right now

    local commits : 22
    on GitHub     : 20
    waiting       : 2  (26036ad, 7bbcff0)
    branch        : main
    remote        : https://github.com/rrajareddy234-gif/Shiva-sagar-hostel.git

Two commits are ready to go up. They need a repo-scoped token to push.

## A backup that needs no token

    git bundle create /workspace/backup.bundle --all

That writes the whole history to one file. Restore it anywhere with:

    git clone /workspace/backup.bundle restored-project
