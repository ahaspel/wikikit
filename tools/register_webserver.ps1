# Register (or update) the scheduled task that serves one book's local site at
# logon: `wikikit.site.serve` under the book's own venv, in the book's
# repository, with no console (pythonw).  Re-running updates the task in place.
#
#   powershell -ExecutionPolicy Bypass -File tools\register_webserver.ps1 `
#       -Name britannica-webserver -Book D:\work\britannica-edition -Port 8000
#   powershell -ExecutionPolicy Bypass -File tools\register_webserver.ps1 `
#       -Name dnb-webserver -Book D:\work\dnb-edition -Port 8001
#
# Add -Start to (re)start it now as well: a running instance of the task is
# stopped first.  A server started BY HAND on the same port is not the task's
# and must be stopped first, or two processes share the port.
param(
    [Parameter(Mandatory)] [string] $Name,
    [Parameter(Mandatory)] [string] $Book,
    [Parameter(Mandatory)] [int] $Port,
    [switch] $Start
)
$ErrorActionPreference = 'Stop'

$python = Join-Path $Book '.venv\Scripts\pythonw.exe'
if (-not (Test-Path $python)) { throw "$python not found: run 'uv sync' in $Book first" }
if (-not (Test-Path (Join-Path $Book 'book.env'))) { throw "$Book is not a book's repository (no book.env)" }

$action = New-ScheduledTaskAction -Execute $python `
    -Argument "-m wikikit.site.serve $Port" -WorkingDirectory $Book
$trigger = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME"
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit ([TimeSpan]::Zero) -Hidden -MultipleInstances IgnoreNew `
    -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive

Register-ScheduledTask -TaskName $Name -Action $action -Trigger $trigger -Settings $settings `
    -Principal $principal -Force | Out-Null
Write-Host "$Name : serves $Book on port $Port at logon"

if ($Start) {
    # A running instance is the OLD action: stop it so the new one takes the port.
    if ((Get-ScheduledTask -TaskName $Name).State -eq 'Running') { Stop-ScheduledTask -TaskName $Name }
    Start-ScheduledTask -TaskName $Name
    Write-Host "$Name : started"
}
