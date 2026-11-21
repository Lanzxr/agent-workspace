param(
    [string]$Source = 'D:\work\AI工作文件夹\内部agent\company-agent-runtime\runtime.env',
    [string]$Distribution = 'Ubuntu-24.04',
    [string]$WslUser = ''
)

$ErrorActionPreference = 'Stop'
if (-not (Test-Path -LiteralPath $Source -PathType Leaf)) {
    throw 'The Windows runtime.env configuration file does not exist.'
}
$resolvedSource = (Resolve-Path -LiteralPath $Source).Path
if ($resolvedSource -notmatch '^([A-Za-z]):[\\/](.*)$') {
    throw 'Source must be a local Windows drive path.'
}
$linuxSource = '/mnt/' + $Matches[1].ToLowerInvariant() + '/' + $Matches[2].Replace('\', '/')
$wslArgs = @('-d', $Distribution)
if ($WslUser) {
    $wslArgs += @('-u', $WslUser)
}
$script = @'
import os
import pathlib
import sys
import tempfile

source = pathlib.Path(sys.argv[1])
directory = pathlib.Path.home() / '.config/company-agent'
directory.mkdir(mode=0o700, parents=True, exist_ok=True)
directory.chmod(0o700)
destination = directory / 'runtime.env'
if destination.is_symlink():
    raise SystemExit('Configuration destination cannot be a symbolic link')
with tempfile.NamedTemporaryFile(dir=directory, delete=False) as stream:
    temporary = pathlib.Path(stream.name)
    os.fchmod(stream.fileno(), 0o600)
    stream.write(source.read_bytes())
    stream.flush()
    os.fsync(stream.fileno())
temporary.replace(destination)
print('RUNTIME_CONFIG_SYNCED: ' + str(destination))
'@
$wslArgs += @('--', 'python3', '-c', $script, $linuxSource)
& wsl.exe @wslArgs
if ($LASTEXITCODE -ne 0) {
    throw 'WSL configuration synchronization failed.'
}
