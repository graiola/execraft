# Ubuntu Ollama satellite

This guide configures an Ubuntu machine as a private Ollama inference worker for
an `Execraft` Execraft host.

## 1. Install Ollama

```bash
curl -fsSL https://ollama.com/install.sh | sh
sudo systemctl enable --now ollama
ollama --version
```

For NVIDIA systems, confirm that the host driver can see the GPU:

```bash
nvidia-smi
```

## 2. Install a coding model

```bash
ollama pull qwen3-coder:30b
ollama run qwen3-coder:30b 'Reply with exactly: ready'
```

`qwen3-coder:30b` is an example rather than a requirement. Choose a model that
fits the satellite's memory and the workloads you intend to run.

## 3. Bind Ollama to the private network

Ollama binds to loopback by default. Add a systemd override:

```bash
sudo systemctl edit ollama.service
```

Add:

```ini
[Service]
Environment="OLLAMA_HOST=0.0.0.0:11434"
Environment="OLLAMA_CONTEXT_LENGTH=65536"
```

Then reload and restart:

```bash
sudo systemctl daemon-reload
sudo systemctl restart ollama
sudo systemctl status ollama --no-pager
```

Confirm the effective environment and listener:

```bash
sudo systemctl show ollama --property=Environment
ss -ltnp | grep 11434
```

A larger context increases memory use. Use `ollama ps` to verify context size
and whether the model is fully on the GPU.

## 4. Restrict the firewall

With UFW, allow only the Execraft host. Replace the example IP:

```bash
sudo ufw allow from 192.168.50.10 to any port 11434 proto tcp
sudo ufw status verbose
```

Do not expose `11434/tcp` to the Internet or an untrusted LAN.

## 5. Test the endpoint

On the satellite:

```bash
curl http://127.0.0.1:11434/api/version
curl http://127.0.0.1:11434/v1/models
```

From the Execraft host:

```bash
curl http://192.168.50.20:11434/api/version
curl http://192.168.50.20:11434/v1/models
```

When both work, register the endpoint using
[satellite_nodes.md](satellite_nodes.md).

## Model storage

The standard Linux installation stores models under Ollama's service account.
To use another disk, create a writable directory and set `OLLAMA_MODELS` in the
same systemd override:

```bash
sudo mkdir -p /srv/ollama-models
sudo chown -R ollama:ollama /srv/ollama-models
sudo systemctl edit ollama.service
```

```ini
[Service]
Environment="OLLAMA_MODELS=/srv/ollama-models"
```

Restart Ollama after changing the location.

## Diagnostics

```bash
ollama list
ollama ps
journalctl -u ollama -n 100 --no-pager
journalctl -u ollama -f
```

If the API is reachable but inference is unexpectedly slow, check `ollama ps`
for CPU offload, reduce context/model size, and confirm the GPU driver is
healthy before changing `Execraft` configuration.
