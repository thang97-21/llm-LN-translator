# Extract Japanese OCR text from the Ice Princess Vol.5 page scans.
# Resolves the volume folder by wildcard (avoids non-ASCII path issues),
# then runs the manga-ocr extractor.
$ErrorActionPreference = "Stop"

$work = "C:/Users/Minh Thang/Documents/llm-LN-translator/work"
$dir  = Get-ChildItem -Directory $work | Where-Object { $_.Name -like "*d72740*" } | Select-Object -First 1

if (-not $dir) {
    Write-Error "Could not find volume folder matching *d72740* under $work"
    exit 1
}

$py     = "C:/Users/Minh Thang/Documents/llm-LN-translator/.venv/Scripts/python.exe"
$script = "C:/Users/Minh Thang/Documents/llm-LN-translator/scripts/ocr_extract.py"
$out    = Join-Path $dir.FullName "extracted_text.md"

Write-Output "IN = $($dir.FullName)"
Write-Output "OUT= $out"

& $py $script --input $dir.FullName --output $out --engine manga
Write-Output "EXIT=$LASTEXITCODE"
