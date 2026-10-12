$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

# Sauvegarde de l'existant avant toute modification par Claude Code.
New-Item -ItemType Directory -Force -Path "baseline_before_claude/data" | Out-Null
$files = @("app.py", "requirements.txt", "README.md", "secrets.example.toml", "cloudinary_pdf_tools.py")
foreach ($file in $files) {
    if (Test-Path $file) {
        Copy-Item $file "baseline_before_claude/$file" -Force
    }
}
foreach ($dir in @("sourcing", "tests")) {
    if (Test-Path $dir) {
        if (Test-Path "baseline_before_claude/$dir") { Remove-Item "baseline_before_claude/$dir" -Recurse -Force }
        Copy-Item $dir "baseline_before_claude/$dir" -Recurse -Force
    }
}
foreach ($dataFile in @("verified_manufacturers.csv", "known_suppliers.txt")) {
    if (Test-Path "data/$dataFile") {
        Copy-Item "data/$dataFile" "baseline_before_claude/data/$dataFile" -Force
    }
}

if ((Get-Command git -ErrorAction SilentlyContinue) -and (Test-Path ".git")) {
    git status --short
    $branch = "claude/improve-ai-sourcing"
    # Windows PowerShell 5.1 transforme la sortie d'erreur d'un programme natif en erreur fatale quand
    # $ErrorActionPreference = "Stop" (git ecrit "Switched to..." sur stderr). On le suspend pour git.
    $previousPreference = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    git switch -c $branch *> $null
    if ($LASTEXITCODE -ne 0) {
        git switch $branch *> $null
    }
    $switchCode = $LASTEXITCODE
    $ErrorActionPreference = $previousPreference
    if ($switchCode -ne 0) {
        throw "Impossible de basculer sur la branche $branch."
    }
}

if (-not (Get-Command claude -ErrorAction SilentlyContinue)) {
    throw "Claude Code n'est pas installe ou n'est pas dans PATH."
}

# Le prompt est lu depuis le fichier par Claude Code : pas de probleme de guillemets sous PowerShell 5.1.
claude --permission-mode acceptEdits "Lis PROMPT_CLAUDE_CODE.md puis execute la mission qui y est decrite, en respectant CLAUDE.md."
