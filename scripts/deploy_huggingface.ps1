param(
  [Parameter(Mandatory = $true)]
  [string]$SpaceId
)

$ErrorActionPreference = "Stop"
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"
$OutputEncoding = [System.Text.UTF8Encoding]::new()

if (-not (Get-Command hf -ErrorAction SilentlyContinue)) {
  throw "The Hugging Face CLI was not found. Install it with: python -m pip install huggingface_hub[cli]"
}

hf auth whoami *> $null
if ($LASTEXITCODE -ne 0) {
  throw "Hugging Face CLI is not logged in. Run: hf auth login"
}

$repoRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..")).Path
Set-Location $repoRoot

$readme = Get-Content -Raw -LiteralPath (Join-Path $repoRoot "README.md")
if ($readme -notmatch "(?m)^sdk:\s*docker\s*$") {
  throw "README.md must contain 'sdk: docker' in the Hugging Face metadata before deploying."
}
if ($readme -notmatch "(?m)^app_port:\s*7860\s*$") {
  throw "README.md must contain 'app_port: 7860' in the Hugging Face metadata before deploying."
}

git -c safe.directory=$repoRoot rev-parse --is-inside-work-tree | Out-Null

$remoteUrl = "https://huggingface.co/spaces/$SpaceId"
$remotes = @(git -c safe.directory=$repoRoot remote)
if ($remotes -contains "space") {
  git -c safe.directory=$repoRoot remote set-url space $remoteUrl
} else {
  git -c safe.directory=$repoRoot remote add space $remoteUrl
}

$branch = git -c safe.directory=$repoRoot branch --show-current
if (-not $branch) {
  $branch = "master"
}

git -c safe.directory=$repoRoot push space "${branch}:main"
