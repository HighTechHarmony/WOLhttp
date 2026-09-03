# Wake-on-LAN HTTP/WebSocket Proxy

Small FastAPI proxy that wakes a powered-down host over Wake-on-LAN, waits for
the requested service port, and forwards HTTP and WebSocket traffic to it.

## Use case

This project is intended for a home lab or small private LAN where local LLM
services such as Ollama or OpenWebUI run on one machine with a powerful GPU. That machine may consume significant standby power even when nobody is using it, so the proxy provides a
lightweight, always-available endpoint for other computers, applications, and
people on the LAN.

When a request arrives, the proxy checks whether the target service is already
available. If it is asleep, the proxy sends a Wake-on-LAN packet, waits for the
service to come up, and then forwards the request. This makes the GPU host
available on demand while allowing it to power down during idle periods.

The design assumes the proxy and GPU host are on the same network (or that proxied directed broadcasts for WOL ports are in place as necessary), and that the
target machine is configured to wake from the relevant network adapter. It is
not intended to replace a production gateway or expose local LLM services to
the public internet.

## Requirements

- Python 3.11 or newer
- A target machine that supports Wake-on-LAN
- Network access from the proxy host to the target machine

## Setup

```sh
python -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
cp config.example.toml config.toml
```

Edit `config.toml` with the target host's private IP address, MAC address, and
subnet broadcast address. This file is ignored by Git so network-specific
values are not published.

## Run

```sh
uvicorn main:app --host 0.0.0.0 --port 8000
```

The proxy chooses the upstream port from the incoming request's port. Run the
HTTP proxy on port `11434` and the WebSocket proxy on port `8080` when those are
the ports used by the target services.

## Run with systemd

The included `proxy.example.service` is a starting point for running the proxy
as a system service. Before installing it:

1. Copy the project to its deployment directory, such as `/opt/ollama-proxy`.
2. Create the virtual environment there and install the dependencies.
3. Copy `config.example.toml` to `config.toml` and edit the target IP, MAC,
	broadcast address, and wake timeout.
4. Edit the unit file's `User`, `WorkingDirectory`, and `ExecStart` paths to
	match the deployment. A dedicated unprivileged user is preferable to
	running the service as `root`; that user must be able to read the project
	and `config.toml`.

Install and start the service with:

```sh
sudo cp proxy.example.service /etc/systemd/system/ollama-proxy.service
sudo systemctl daemon-reload
sudo systemctl enable --now ollama-proxy.service
```

Check its status and follow its logs with:

```sh
sudo systemctl status ollama-proxy.service
sudo journalctl -u ollama-proxy.service -f
```

The example unit listens on proxy port `11434`. One Uvicorn process cannot bind
to both `11434` and `8080`, so copy the unit to a second service, change its
service name and `ExecStart` port to `8080`, and enable both services when both
HTTP and WebSocket endpoints are needed. The listen port must match the
corresponding upstream service port.

## Security notes

This service does not provide authentication or authorization. Do not expose
it directly to the public internet; place it behind an authenticated reverse
proxy or restrict access with a firewall.

The proxy forwards request headers and streams upstream responses. Review the
deployment network and forwarded headers before using it with sensitive
services.