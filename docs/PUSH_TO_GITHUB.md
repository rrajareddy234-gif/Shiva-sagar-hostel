# Getting the code onto your GitHub

## Where the code is right now

Inside the development workspace, at `/workspace/project`. It is a proper git
repository with **18 commits** and **72 files**. Nothing is lost.

It is **not on GitHub yet.** Two things block it:

| Blocker | What it means |
|---|---|
| The token I hold is **read-only** | It cannot create a repository, and it cannot push. Both return `403`. |
| I have no write credential for your account | Pushing needs a token with `repo` scope, which only you can create. |

I have prepared everything so the push is one command once you give me a token.

## Important: your repository is public

`rrajareddy234-gif/Shiva-sagar-hostel` is currently **public and empty**.

This code contains your business logic, your prices and your booking rules. It
will later contain customer data paths. **It should be private.** Before pushing
anything, please make it private:

> GitHub → your repo → **Settings** → scroll to bottom → **Change visibility** →
> **Make private**

## Option A: you give me a token, I do everything

1. Open **https://github.com/settings/tokens** → **Generate new token (classic)**
2. Name it `sagar-pg-push`
3. Expiry: 90 days is fine
4. Tick **one** box: **`repo`** (full control of private repositories)
5. Click **Generate**, copy the token (it starts with `ghp_`)
6. Send it to me

Then I will, without further help from you:

- create the private repo (or use `Shiva-sagar-hostel` if you made it private)
- push all 18 commits
- set up a branch, not a direct push to `main`, so you can review first
- remove the token from the git config afterwards

**If you would rather not paste a token into chat**, tell me and I will show you
where to write it into a file on the server instead.

## Option B: you run one command yourself

If you prefer to do it yourself, run these three commands on your own computer.
You do not need to retype the code — it travels in the bundle file I created.

```bash
# 1. Download these two files from the workspace to your computer:
#      /workspace/sagar-pg-ai-manager.bundle      (the whole project, 147 KB)
#      (that single file is enough)

# 2. Restore the project from the bundle
git clone sagar-pg-ai-manager.bundle sagar-pg-ai-manager
cd sagar-pg-ai-manager

# 3. Push it to your repository
git remote set-url origin https://github.com/rrajareddy234-gif/Shiva-sagar-hostel.git
git push -u origin main
```

GitHub will ask for your username and password. **Use the token as the password**,
not your GitHub password — GitHub stopped accepting account passwords in 2021.
Create a token exactly as in Option A, step 1–5, and paste it at the password
prompt.

## What the bundle is

A single file that contains the entire project and its full history — all 18
commits. I verified it restores cleanly by cloning it back and checking the file
count. It is 147 KB, so it can be emailed or copied on a USB stick.

Location: `/workspace/sagar-pg-ai-manager.bundle`

## Safety check already done

I checked whether any secret would leak onto GitHub:

- `.gitignore` excludes `.env`, `*.key`, `*.pem`, `secrets/`, `credentials/`,
  backups and data dumps.
- No real secret is tracked. The only matches are dummy test values
  (`test-app-secret`, `test-verify-token`, `test-job-token`) in test files.
- `.env.example` is tracked on purpose — it lists which settings exist but
  contains no values.

## Which should you choose?

**Option A**, unless you would rather keep the token to yourself. Option A takes
you about two minutes and no typing of code.

The only thing I need from you is the token. Nothing else.
