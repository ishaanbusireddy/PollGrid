# PollGrid release procedure

This is the permanent convention for every version, starting with v3.0.
Nothing in it depends on what your local folder is called.

## Conventions

- **Version number** lives in ONE place: `core/version.py` (`VERSION = "3.0"`).
  The site header, `/api/status`, and the zip name all read from it — bump it
  there and nowhere else.
- **Zip naming**: `PollGridv<VERSION>.zip` (e.g. `PollGridv3.0.zip`).
- **Zip layout**: FLAT — project files sit at the zip root (`run.py`,
  `config.yaml`, `core/`, …). No wrapper folder, so extracting *into* a folder
  puts the project directly there, never nested.

## Getting a new version onto your machine (Windows / PowerShell)

Run these from anywhere. Only `$Proj` names your folder — change it once if
you ever rename the folder, and every command still works.

```powershell
$V    = "3.0"                                  # the version you downloaded
$Proj = "$env:USERPROFILE\Downloads\PollGrid"  # your project folder, any name

# 1. Unzip the flat archive straight into the project folder (overwrites code,
#    never touches data\ — your database and .env survive upgrades)
Expand-Archive -Path "$env:USERPROFILE\Downloads\PollGridv$V.zip" -DestinationPath $Proj -Force
```

## Commit and push to GitHub

```powershell
cd $Proj

# First time only (skip if .git already exists):
git init -b main
git remote add origin https://github.com/ishaanbusireddy/PollGrid.git

# Every release:
git add -A
git commit -m "PollGrid v$V"
git push -u origin main
```

If the push is rejected because the remote has newer history, pull first:
`git pull origin main --rebase` then push again.

## Run it

```powershell
cd $Proj
python run.py --no-browser     # serves http://localhost:8811
```

First run on a fresh database: `python scripts/seed_demo.py` for demo data
(synthetic, clearly labeled), and/or `python scripts/bootstrap_real.py` to
pull real data once API keys are set in Settings.

## Bumping a version (for whoever builds the next release)

1. Edit `core/version.py` → `VERSION = "3.1"` (or whatever is next).
2. Verify: tests green (`python -m unittest discover -s tests`), header shows
   the new version, `/api/status` reports it.
3. Build the flat zip from the project root — files at the archive root:
   the archive must contain `run.py`, not `PollGrid/run.py`.
4. Name it `PollGridv<VERSION>.zip`. Done.
