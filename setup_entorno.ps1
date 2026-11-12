# setup_entorno.ps1 - Crea el entorno virtual .venv con Python 3.12 e instala
# las dependencias fijadas de requirements.txt. No ejecuta docker.
#
# Uso (desde la carpeta del proyecto):
#   powershell -ExecutionPolicy Bypass -File .\setup_entorno.ps1
#   powershell -ExecutionPolicy Bypass -File .\setup_entorno.ps1 -Recrear      # borra y recrea .venv
#   powershell -ExecutionPolicy Bypass -File .\setup_entorno.ps1 -SinConector  # no descarga el conector Kafka
#
# Alternativa equivalente sin PowerShell (si no se permite 'powershell -File'):
#   py -3.12 setup_entorno.py [--sin-conector] [--recrear]
#
# (Archivo sin tildes a proposito: PowerShell 5.1 lee los .ps1 sin BOM como ANSI.)

param(
    [switch]$Recrear,
    [switch]$SinConector
)

$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot
$python = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"

function Ejecutar([string]$descripcion, [scriptblock]$bloque) {
    Write-Host "==> $descripcion" -ForegroundColor Cyan
    & $bloque
    if ($LASTEXITCODE -ne 0) { throw "Fallo: $descripcion (codigo $LASTEXITCODE)" }
}

# 1. Verificar el lanzador py y Python 3.12
if (-not (Get-Command py -ErrorAction SilentlyContinue)) {
    throw "No se encontro el lanzador 'py'. Instala Python 3.12 desde python.org (incluye py.exe)."
}
Ejecutar "Verificando Python 3.12" { py -3.12 --version }

# 2. Crear el entorno virtual aislado
if ($Recrear -and (Test-Path ".venv")) {
    Write-Host "==> Borrando .venv existente" -ForegroundColor Yellow
    Remove-Item -Recurse -Force ".venv" -Confirm:$false
}
if (-not (Test-Path $python)) {
    Ejecutar "Creando .venv con py -3.12 -m venv .venv" { py -3.12 -m venv .venv }
} else {
    Write-Host "==> .venv ya existe (usa -Recrear para rehacerlo)" -ForegroundColor Yellow
}

# 3. Instalar dependencias fijadas
Ejecutar "Actualizando pip" { & $python -m pip install --upgrade pip }
Ejecutar "Instalando requirements.txt" { & $python -m pip install -r requirements.txt }
Ejecutar "Verificando pyspark 3.5.x" {
    & $python -c "import pyspark,sys; v=pyspark.__version__; print('pyspark', v); sys.exit(0 if v.startswith('3.5.') else 1)"
}

# 4. Descargar el conector Kafka de Spark (misma version que pyspark) a .ivy2
if (-not $SinConector) {
    Write-Host "==> Descargando conector spark-sql-kafka a .ivy2 (requiere Java 17 e internet)" -ForegroundColor Cyan
    & $python check_entorno.py --descargar-conector | Out-Null
}

# 5. Reporte final del entorno
Write-Host "==> Verificacion del entorno" -ForegroundColor Cyan
& $python check_entorno.py
Write-Host ""
Write-Host "Listo. Activa el entorno con: .\.venv\Scripts\Activate.ps1" -ForegroundColor Green
