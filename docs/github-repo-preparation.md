# GitHub Repository Preparation Checklist

Reusable checklist for preparing a public GitHub repository with conservative defaults.

## 1. Confirm The Target Repository

Always verify the repository before changing settings:

```powershell
$GH = "C:\Program Files\GitHub CLI\gh.exe"
$REPO = "owner/repository"

& $GH repo view $REPO --json nameWithOwner,isFork,visibility,parent,defaultBranchRef
```

For forks, keep the original project as `upstream` and push only to your fork:

```powershell
git remote -v
git remote set-url --push upstream DISABLED
```

## 2. Protect Local Secrets

Before the first public commit, make sure local/runtime files are ignored:

```gitignore
data/
.venv/
venv/
__pycache__/
.pytest_cache/
*.py[cod]
```

Check that no local config, printer IPs, API tokens, or generated runtime state are staged:

```powershell
git status --short
git diff --cached --stat
```

## 3. Basic Repository Settings

Recommended public repo defaults:

```powershell
& $GH repo edit $REPO `
  --description "Short project description." `
  --enable-issues=true `
  --enable-projects=false `
  --enable-wiki=false `
  --enable-discussions=false `
  --enable-squash-merge=true `
  --enable-merge-commit=false `
  --enable-rebase-merge=false `
  --delete-branch-on-merge=true `
  --allow-update-branch=true `
  --squash-merge-commit-message pr-title-description
```

This leaves PRs with squash merge only, keeping history easier to read.

## 4. Repository Topics

Add searchable topics:

```powershell
& $GH repo edit $REPO `
  --add-topic topic-one `
  --add-topic topic-two `
  --add-topic topic-three
```

## 5. Security Settings

Enable Dependabot alerts and security updates:

```powershell
& $GH api --method PUT "repos/$REPO/vulnerability-alerts"
& $GH api --method PUT "repos/$REPO/automated-security-fixes"
```

Enable secret scanning and push protection where available:

```powershell
& $GH repo edit $REPO --enable-secret-scanning=true
& $GH repo edit $REPO --enable-secret-scanning-push-protection=true
```

Verify:

```powershell
& $GH api "repos/$REPO" --jq ".security_and_analysis"
```

## 6. Main Branch Ruleset

Create a conservative `main` ruleset:

```powershell
$body = @{
  name = "Protect main"
  target = "branch"
  enforcement = "active"
  conditions = @{
    ref_name = @{
      include = @("refs/heads/main")
      exclude = @()
    }
  }
  rules = @(
    @{ type = "deletion" },
    @{ type = "non_fast_forward" },
    @{
      type = "pull_request"
      parameters = @{
        required_approving_review_count = 0
        dismiss_stale_reviews_on_push = $false
        require_code_owner_review = $false
        require_last_push_approval = $false
        required_review_thread_resolution = $false
        allowed_merge_methods = @("squash")
      }
    }
  )
} | ConvertTo-Json -Depth 20

$body | & $GH api --method POST "repos/$REPO/rulesets" --input -
```

Verify:

```powershell
& $GH api "repos/$REPO/rulesets"
```

## 7. Add CI Before Requiring Checks

Add a workflow such as `.github/workflows/tests.yml`:

```yaml
name: Tests

on:
  pull_request:
  push:
    branches:
      - main

jobs:
  pytest:
    runs-on: ubuntu-latest

    steps:
      - uses: actions/checkout@v4

      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"

      - name: Install dependencies
        run: |
          python -m pip install --upgrade pip
          python -m pip install -r requirements.txt -r requirements-dev.txt

      - name: Compile Python files
        run: python -m py_compile main.py models/schemas.py

      - name: Run tests
        run: python -m pytest tests
```

Commit and push the workflow, then verify it runs:

```powershell
git add .github/workflows/tests.yml
git commit -m "Add CI test workflow"
git push

& $GH run list --repo $REPO --limit 5
```

Only after the workflow exists on `main`, update the ruleset to require the CI status check.

## 8. Final Verification

```powershell
& $GH repo view $REPO --json description,hasIssuesEnabled,hasProjectsEnabled,hasWikiEnabled,mergeCommitAllowed,squashMergeAllowed,rebaseMergeAllowed,deleteBranchOnMerge,isFork,visibility,repositoryTopics,defaultBranchRef
& $GH api "repos/$REPO/rulesets"
& $GH api "repos/$REPO" --jq ".security_and_analysis"
git status --short
```

Expected shape:

- Public repo settings match your intended collaboration style.
- Only squash merge is enabled.
- Secret scanning and push protection are enabled where available.
- `main` blocks deletion and force pushes.
- Changes to `main` go through PRs.
- Local working tree is clean.
