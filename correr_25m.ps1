# correr_25m.ps1 - Corre en orden los pasos 1 a 7 de "Corrida completa de 25M" del README.
#
# Uso, desde la carpeta del proyecto en PowerShell (con Docker/Kafka corriendo):
#     powershell -ExecutionPolicy Bypass -File .\correr_25m.ps1
# Opciones:
#     -Desde 4               retoma desde el paso 4 (si uno fallo y ya lo corregiste)
#     -EscalabilidadRapida   paso 6 con --fraccion 0.5 --repeticiones 1 (mucho mas corto)
#
# Se detiene en el primer paso que falle y dice como retomar. Todo queda registrado, con hora y
# duracion de cada paso, en artefactos\corrida_25m.log. Mientras corre, el PC no se suspende.
param([int]$Desde = 1, [switch]$EscalabilidadRapida)

$ErrorActionPreference = "Continue"
Set-Location $PSScriptRoot
[Console]::OutputEncoding = [Text.Encoding]::UTF8
$env:PYTHONIOENCODING = "utf-8"
$env:PYTHONUTF8 = "1"

$py  = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
$log = Join-Path $PSScriptRoot "artefactos\corrida_25m.log"
New-Item -ItemType Directory -Force (Split-Path $log) | Out-Null

function Anotar([string]$texto) {
    $linea = "[{0:yyyy-MM-dd HH:mm:ss}] {1}" -f (Get-Date), $texto
    Write-Host $linea -ForegroundColor Cyan
    Add-Content -Path $log -Value $linea -Encoding UTF8
}

# Mantener el PC despierto mientras dura la corrida (se libera al terminar o al cerrar la ventana)
Add-Type -Namespace Energia -Name Win -MemberDefinition '[DllImport("kernel32.dll")] public static extern uint SetThreadExecutionState(uint f);'
[Energia.Win]::SetThreadExecutionState([uint32]2147483649) | Out-Null   # ES_CONTINUOUS | ES_SYSTEM_REQUIRED

$escala = @("scripts/escalabilidad.py")
if ($EscalabilidadRapida) { $escala += @("--fraccion", "0.5", "--repeticiones", "1") }

$pasos = @(
    @{ n = 1; nombre = "Descargar MovieLens 25M";              args = @("scripts/descargar_movielens.py") },
    @{ n = 2; nombre = "Ordenar ratings por timestamp";        args = @("scripts/ordenar_ratings.py") },
    @{ n = 3; nombre = "Crear el topic 'ratings' en Kafka";    args = @("scripts/crear_topic.py") },
    @{ n = 4; nombre = "Entrenar ALS batch y evaluar";         args = @("scripts/entrenar_batch.py") },
    @{ n = 5; nombre = "Perfiles de gusto y tipos de espectador"; args = @("scripts/perfilar_usuarios.py") },
    @{ n = 6; nombre = "Experimento de escalabilidad";         args = $escala },
    @{ n = 7; nombre = "Figuras del poster";                   args = @("scripts/figuras_poster.py") }
)

if (-not (Test-Path $py)) { Anotar "No encuentro $py. Corre primero setup_entorno.ps1."; exit 1 }

Anotar "=== Inicio de la corrida 25M (desde el paso $Desde) ==="
& $py -u check_entorno.py 2>&1 | ForEach-Object { $l = "$_"; Write-Host $l; Add-Content -Path $log -Value $l -Encoding UTF8 }
$total = Get-Date

try {
    foreach ($p in $pasos) {
        if ($p.n -lt $Desde) { continue }
        Anotar "PASO $($p.n)/7: $($p.nombre) ..."
        $t0 = Get-Date
        & $py -u @($p.args) 2>&1 | ForEach-Object { $l = "$_"; Write-Host $l; Add-Content -Path $log -Value $l -Encoding UTF8 }
        $codigo = $LASTEXITCODE
        $min = ((Get-Date) - $t0).TotalMinutes
        if ($codigo -ne 0) {
            Anotar ("FALLO el paso {0} tras {1:N1} min (codigo {2}). Revisa el error arriba, corrigelo y retoma con:  powershell -ExecutionPolicy Bypass -File .\correr_25m.ps1 -Desde {0}" -f $p.n, $min, $codigo)
            exit $codigo
        }
        Anotar ("OK paso {0} en {1:N1} min" -f $p.n, $min)
    }
    Anotar ("=== Corrida completa en {0:N1} min. Revisa artefactos\25m\metricas.json y poster\figuras ===" -f ((Get-Date) - $total).TotalMinutes)
}
finally {
    [Energia.Win]::SetThreadExecutionState([uint32]2147483648) | Out-Null   # ES_CONTINUOUS: vuelve a lo normal
}
