# OPEN-83-GIT-G — Validator fixture test runner
# Non-destructive fixture runner. Expected return codes are part of the test contract.
# Cross-platform: Windows PowerShell / PowerShell Core on Ubuntu GitHub Actions.

$ErrorActionPreference = "Stop"

$RepoRoot = (Resolve-Path (Join-Path (Join-Path $PSScriptRoot "..") "..")).Path
Set-Location $RepoRoot

$Open83Dir = Join-Path $RepoRoot "open83"
$TestsDir = Join-Path $Open83Dir "tests"
$PackagesDir = Join-Path $Open83Dir "packages"

$WindowsVenvPython = Join-Path (Join-Path (Join-Path $RepoRoot ".venv") "Scripts") "python.exe"
$UnixVenvPython = Join-Path (Join-Path (Join-Path $RepoRoot ".venv") "bin") "python"

if (Test-Path $WindowsVenvPython) {
    $Python = $WindowsVenvPython
} elseif (Test-Path $UnixVenvPython) {
    $Python = $UnixVenvPython
} else {
    $Python = "python"
}

$Validator = Join-Path (Join-Path $Open83Dir "tools") "validate_resume_package.py"
$Schema = Join-Path (Join-Path $Open83Dir "schemas") "resume_package.schema.v1.0.0.json"

$cases = @(
    @{
        Name="valid"
        Package=(Join-Path (Join-Path $TestsDir "valid") "resume_package.json")
        OutDir=(Join-Path $PackagesDir "fixture-valid")
        CleanMode="package"
        ExpectedRc=0
        ExpectedDecision="decision=GO"
        ExpectedNeedle="errors=0"
    },
    @{
        Name="missing_file"
        Package=(Join-Path (Join-Path $TestsDir "missing_file") "resume_package.json")
        OutDir=(Join-Path $PackagesDir "fixture-missing-file")
        CleanMode="package"
        ExpectedRc=10
        ExpectedDecision="decision=NO_GO"
        ExpectedNeedle="missing_required_files=1"
    },
    @{
        Name="secret_detected"
        Package=(Join-Path (Join-Path $TestsDir "secret_detected") "resume_package.json")
        OutDir=(Join-Path $PackagesDir "fixture-secret-detected")
        CleanMode="package"
        ExpectedRc=10
        ExpectedDecision="decision=NO_GO"
        ExpectedNeedle="secret_scan_failures=1"
    },
    @{
        Name="hash_conflict"
        Package=(Join-Path (Join-Path $TestsDir "hash_conflict") "resume_package.json")
        OutDir=(Join-Path $TestsDir "hash_conflict")
        CleanMode="fixture_lock"
        ExpectedRc=20
        ExpectedDecision="decision=STOP_CONFLICT"
        ExpectedNeedle="errors=1"
    }
)

$passed = 0
$failed = 0

foreach ($case in $cases) {
    Write-Host "===== FIXTURE CASE: $($case.Name) ====="

    if ($case.CleanMode -eq "package") {
        if (Test-Path $case.OutDir) {
            Remove-Item -Recurse -Force $case.OutDir
        }
    } else {
        foreach ($generated in @("resume_package.manifest.json", "validation_report.json")) {
            $path = Join-Path $case.OutDir $generated
            if (Test-Path $path) {
                Remove-Item -Force $path
            }
        }
    }

    $output = & $Python $Validator `
        --package $case.Package `
        --schema $Schema `
        --root-dir $Open83Dir `
        --out-dir $case.OutDir 2>&1

    $rc = $LASTEXITCODE
    $text = ($output | Out-String)
    $output | ForEach-Object { Write-Host $_ }

    $ok = $true

    if ($rc -ne $case.ExpectedRc) {
        Write-Host "FIXTURE_FAIL: unexpected return code. expected=$($case.ExpectedRc) actual=$rc"
        $ok = $false
    }

    if ($text -notlike "*$($case.ExpectedDecision)*") {
        Write-Host "FIXTURE_FAIL: missing expected decision $($case.ExpectedDecision)"
        $ok = $false
    }

    if ($text -notlike "*$($case.ExpectedNeedle)*") {
        Write-Host "FIXTURE_FAIL: missing expected marker $($case.ExpectedNeedle)"
        $ok = $false
    }

    if ($ok) {
        Write-Host "FIXTURE_PASS: $($case.Name)"
        $passed += 1
    } else {
        Write-Host "FIXTURE_FAIL: $($case.Name)"
        $failed += 1
    }
}

Write-Host "===== FIXTURE TEST ESSENTIAL OUTPUT ====="
Write-Host "tests_total=$($cases.Count)"
Write-Host "tests_passed=$passed"
Write-Host "tests_failed=$failed"

if ($failed -eq 0) {
    Write-Host "go_no_go=GO_FIXTURES_VALIDATED"
    exit 0
}

Write-Host "go_no_go=NO_GO_FIXTURES_FAILED"
exit 10
