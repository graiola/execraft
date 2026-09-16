# Windows Ollama satellite

This guide uses **native Ollama for Windows**, which is the simplest current
configuration and supports NVIDIA and AMD Radeon GPUs. Use WSL 2 only when you
already have a Linux Ollama installation that you specifically want to retain.

## 1. Install Ollama

Install Ollama for Windows from the official installer, then open PowerShell:

```powershell
ollama --version
ollama pull qwen3-coder:30b
ollama run qwen3-coder:30b "Reply with exactly: ready"
```

Ollama runs in the background and serves `http://localhost:11434` by default.

## 2. Configure context and network binding

Quit Ollama from the taskbar before changing its environment variables. In
**Edit environment variables for your account**, create:

```text
OLLAMA_HOST=0.0.0.0:11434
OLLAMA_CONTEXT_LENGTH=65536
```

Optionally move model storage to a larger disk:

```text
OLLAMA_MODELS=D:\OllamaModels
```

Start Ollama again from the Start menu.

A larger context consumes more VRAM. If the model starts spilling to CPU, lower
`OLLAMA_CONTEXT_LENGTH` or choose a smaller model. Check the effective processor
and context with:

```powershell
ollama ps
```

## 3. Restrict the Windows firewall

Determine the Execraft host's IP, then create an inbound rule restricted to
that address. Run PowerShell as Administrator:

```powershell
$DevHost = "192.168.50.10"
New-NetFirewallRule `
  -DisplayName "Ollama from Execraft host" `
  -Direction Inbound `
  -Action Allow `
  -Protocol TCP `
  -LocalPort 11434 `
  -RemoteAddress $DevHost
```

Do not create an unrestricted public rule for port `11434`.

Check the listener:

```powershell
Get-NetTCPConnection -LocalPort 11434 -State Listen
```

## 4. Test locally and remotely

On Windows:

```powershell
Invoke-RestMethod http://127.0.0.1:11434/api/version
Invoke-RestMethod http://127.0.0.1:11434/v1/models
```

From the Execraft host:

```bash
curl http://192.168.50.20:11434/api/version
curl http://192.168.50.20:11434/v1/models
```

Then register the endpoint in `Execraft` using
[satellite_nodes.md](satellite_nodes.md).

## Optional: keep Ollama inside WSL 2

Native Windows Ollama is preferred for new satellites. If Ollama already runs
inside Ubuntu on WSL 2 with default NAT networking, the repository includes
idempotent forwarding helpers under `tools/satellite/`:

```text
Install-OllamaWslProxy.ps1
Refresh-OllamaWslProxy.ps1
Remove-OllamaWslProxy.ps1
```

Run the install script from an elevated PowerShell session and follow its help
for parameters. The proxy forwards the Windows port to the current WSL address,
which may change across WSL restarts; use the refresh script when necessary.

Do **not** use the NAT forwarding scripts with WSL mirrored networking. In that
mode, configure the Windows/Hyper-V firewall for the WSL service instead.

## Diagnostics

Check Ollama itself:

```powershell
ollama list
ollama ps
```

Windows application logs are under:

```text
%LOCALAPPDATA%\Ollama
```

For WSL-based installations:

```powershell
wsl -d Ubuntu -- systemctl status ollama --no-pager
wsl -d Ubuntu -- journalctl -u ollama -n 100 --no-pager
```

If local API calls work but remote calls do not, check `OLLAMA_HOST`, the
firewall scope, the satellite IP, and whether another network profile is
blocking inbound traffic.
