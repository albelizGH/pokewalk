# pokewalk-hook.ps1 - avisa a pokewalk de cada comando que corrés (para que los Pokémon reaccionen).
#
# Se carga desde tu perfil de PowerShell (lo agrega install.py). Envuelve la función `prompt`
# sin cambiar lo que devuelve ni el estado de `$?` / `$LASTEXITCODE`, así que convive con
# Oh My Posh, starship, etc. (tiene que cargarse DESPUÉS de ellos: al final del perfil).
#
# Solo escribe si pokewalk está corriendo (mira que %LOCALAPPDATA%\pokewalk\alive se haya
# tocado hace menos de 20 s), así que no deja archivos creciendo cuando no lo usás.
# Por comando guarda: hora, texto del comando, si salió bien, duración y carpeta.

if (-not $global:__PokewalkHooked) {
    $global:__PokewalkHooked = $true
    $global:__PokewalkDir = Join-Path $env:LOCALAPPDATA 'pokewalk'
    $global:__PokewalkLastId = -1
    $global:__PokewalkOrigPrompt = (Get-Command prompt -CommandType Function).ScriptBlock

    function global:prompt {
        $ok = $?
        $code = $global:LASTEXITCODE
        try {
            $alive = Join-Path $global:__PokewalkDir 'alive'
            $age = ([DateTime]::UtcNow - [IO.File]::GetLastWriteTimeUtc($alive)).TotalSeconds
            $h = Get-History -Count 1
            if ($h -and $h.Id -ne $global:__PokewalkLastId) {
                $global:__PokewalkLastId = $h.Id
                if ($age -lt 20) {
                    $ev = [ordered]@{
                        t   = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds()
                        cmd = $h.CommandLine
                        ok  = $ok
                        dur = [math]::Round(($h.EndExecutionTime - $h.StartExecutionTime).TotalSeconds, 1)
                        cwd = (Get-Location).Path
                    }
                    $line = ($ev | ConvertTo-Json -Compress) + "`n"
                    [IO.File]::AppendAllText((Join-Path $global:__PokewalkDir 'events.jsonl'),
                                             $line, (New-Object Text.UTF8Encoding $false))
                }
            }
        } catch {}
        # devolver el estado original para que el prompt real (Oh My Posh...) vea lo mismo que sin hook
        $global:LASTEXITCODE = $code
        if (-not $ok) { Write-Error 'pokewalk' -ErrorAction Ignore } else { $null = 1 }
        & $global:__PokewalkOrigPrompt
    }
}
