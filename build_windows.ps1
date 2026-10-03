$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot

$python = Get-Command py -ErrorAction SilentlyContinue
if ($python) {
    $pythonCommand = 'py'
    $pythonPrefix = @('-3')
} else {
    $python = Get-Command python -ErrorAction SilentlyContinue
    if (-not $python) { throw 'Instale Python 3.10+ para Windows e execute novamente.' }
    $pythonCommand = $python.Source
    $pythonPrefix = @()
}

$ffmpeg = Get-Command ffmpeg.exe -ErrorAction SilentlyContinue
$ffprobe = Get-Command ffprobe.exe -ErrorAction SilentlyContinue
$tesseract = Get-Command tesseract.exe -ErrorAction SilentlyContinue
if (-not $ffmpeg -or -not $ffprobe) {
    throw 'FFmpeg não encontrado no PATH. Instale FFmpeg e confirme que ffmpeg.exe e ffprobe.exe funcionam no PowerShell.'
}
if (-not $tesseract) { throw 'Tesseract OCR não encontrado no PATH. Instale Tesseract com idiomas português e inglês.' }
$tessdataSource = Join-Path (Split-Path $tesseract.Source) 'tessdata'
foreach ($language in @('eng', 'por')) {
    if (-not (Test-Path (Join-Path $tessdataSource "$language.traineddata"))) {
        throw "Dados OCR do idioma $language não encontrados em $tessdataSource. Instale os idiomas português e inglês."
    }
}

$venv = Join-Path $PSScriptRoot '.venv-build'
if (-not (Test-Path $venv)) {
    & $pythonCommand @pythonPrefix -m venv $venv
    if ($LASTEXITCODE -ne 0) { throw 'Não foi possível criar o ambiente virtual de build.' }
}
$venvPython = Join-Path $venv 'Scripts\python.exe'
& $venvPython -m pip install --upgrade pip
if ($LASTEXITCODE -ne 0) { throw 'Falha ao atualizar pip.' }
& $venvPython -m pip install -r requirements.txt pyinstaller
if ($LASTEXITCODE -ne 0) { throw 'Falha ao instalar dependências de build.' }

$arguments = @(
    'app.py', '--clean', '--noconfirm', '--onefile', '--name', 'LimpaVideo',
    '--add-data', 'templates;templates',
    '--add-binary', "$($ffmpeg.Source);ffmpeg_bin",
    '--add-binary', "$($ffprobe.Source);ffmpeg_bin",
    '--add-binary', "$($tesseract.Source);ocr_bin"
)
Get-ChildItem -Path $ffmpeg.Source.DirectoryName -Filter '*.dll' -File -ErrorAction SilentlyContinue | ForEach-Object {
    $arguments += @('--add-binary', "$($_.FullName);ffmpeg_bin")
}
$tesseractDirectory = Split-Path $tesseract.Source
Get-ChildItem -Path $tesseractDirectory -Filter '*.dll' -File -ErrorAction SilentlyContinue | ForEach-Object {
    $arguments += @('--add-binary', "$($_.FullName);ocr_bin")
}
$stagedTessdata = Join-Path $PSScriptRoot '.build_tessdata'
if (Test-Path $stagedTessdata) { Remove-Item $stagedTessdata -Recurse -Force }
New-Item -ItemType Directory -Path $stagedTessdata | Out-Null
Copy-Item (Join-Path $tessdataSource 'eng.traineddata') $stagedTessdata
Copy-Item (Join-Path $tessdataSource 'por.traineddata') $stagedTessdata
$arguments += @('--add-data', "$stagedTessdata;tessdata", '--collect-all', 'cv2', '--collect-all', 'pytesseract')
& $venvPython -m PyInstaller @arguments
$buildExitCode = $LASTEXITCODE
Remove-Item $stagedTessdata -Recurse -Force -ErrorAction SilentlyContinue
if ($buildExitCode -ne 0) { throw 'O PyInstaller não conseguiu criar o executável.' }
Write-Host ''
Write-Host 'Executável criado:' (Join-Path $PSScriptRoot 'dist\LimpaVideo.exe') -ForegroundColor Green
Write-Host 'Dê dois cliques no EXE. Ele abrirá o navegador em http://127.0.0.1:7860.'
