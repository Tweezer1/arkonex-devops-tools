# OPEN-83-GIT-G — Validator fixture test runner
# Non-destructive fixture runner. Expected return codes are part of the test contract.

$ErrorActionPreference = "Stop"

$RepoRoot = Resolve-Path (Join-Path $PSScriptRoot "..\..")
Set-Location $RepoRoot

$Python = ".\.venv\Scripts\python.exe"
if (!(Test-Path $Python)) {
    $Python = "python"
}

$Validator = "open83\tools\validate_resume_package.py"
$Schema = "open83\schemas\resume_package.schema.v1.0.0.json"

$cases = @(
    @{ Name="valid"; Package="open83\tests\valid\resume_package.json"; OutDir="open83\packages\fixture-valid"; ExpectedRc=0; ExpectedDecision="decision=GO"; ExpectedNeedle="errors=0" },
    @{ Name="missing_file"; Package="open83\tests\missing_file\resume_package.json"; OutDir="open83\packages\fixture-missing-file"; ExpectedRc=10; ExpectedDecision="decision=NO_GO"; ExpectedNeedle="missing_required_files=1" },
    @{ Name="secret_detected"; Package="open83\tests\secret_detected\resume_package.json"; OutDir="open83\packages\fixture-secret-detected"; ExpectedRc=10; ExpectedDecision="decision=NO_GO"; ExpectedNeedle="secret_scan_failures=1" },
    @{ Name="hash_conflict"; Package="open83\tests\hash_conflict\resume_package.json"; OutDir="open83\tests\hash_conflict"; ExpectedRc=20; ExpectedDecision="decision=STOP_CONFLICT"; ExpectedNeedle="errors=1" }
)

$passed = 0
$failed = 0

foreach ($case in $cases) {
    Write-Host "===== FIXTURE CASE: $($case.Name) ====="

    if ($case.OutDir -like "open83\packages\*") {
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
        --root-dir open83 `
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
