import asyncio
from pathlib import Path
import tomllib

import httpx
from fastapi.responses import StreamingResponse
from fastapi import FastAPI, Request, Response, WebSocket, WebSocketDisconnect
from wakeonlan import send_magic_packet


def load_config() -> dict:
    config_path = Path(__file__).with_name("config.toml")
    try:
        with config_path.open("rb") as config_file:
            config = tomllib.load(config_file)
    except FileNotFoundError as error:
        raise RuntimeError(
            "config.toml is missing. Copy config.example.toml to config.toml and edit it."
        ) from error

    target = config.get("target", {})
    required = ("ip", "mac", "broadcast", "wake_timeout")
    missing = [key for key in required if key not in target]
    if missing:
        raise RuntimeError(f"Missing configuration values: {', '.join(missing)}")
    if not isinstance(target["mac"], str):
        raise RuntimeError("Configuration value 'mac' must be a string")
    target["mac"] = target["mac"].strip()
    return target


config = load_config()
GPU_HOST_IP = config["ip"]
GPU_HOST_MAC = config["mac"]
SUBNET_BROADCAST = config["broadcast"]
WAKE_TIMEOUT = config["wake_timeout"]

app = FastAPI()
# Configure generous connection limits and timeouts for web UI asset spikes and LLM streaming
limits = httpx.Limits(
    max_keepalive_connections=100, max_connections=500, keepalive_expiry=30.0
)
timeout = httpx.Timeout(
    connect=10.0,
    read=300.0,
    write=300.0,
    pool=30.0,  # Extends connection pool acquisition timeout from 5s to 30s
)

client = httpx.AsyncClient(limits=limits, timeout=timeout)

async def is_service_up(port: int) -> bool:
    """Checks if a specific TCP port is responding on the target machine."""
    try:
        _, writer = await asyncio.wait_for(
            asyncio.open_connection(GPU_HOST_IP, port), timeout=1.5
        )
        writer.close()
        await writer.wait_closed()
        return True
    except Exception:
        return False

async def ensure_host_awake(target_port: int):
    """Triggers WoL and waits for the target port to accept connections."""
    if await is_service_up(target_port):
        return True

    print(f"[Proxy] Target {GPU_HOST_IP}:{target_port} is down. Sending WoL to {GPU_HOST_MAC}...")
    send_magic_packet(GPU_HOST_MAC, ip_address=SUBNET_BROADCAST)

    elapsed = 0
    while elapsed < WAKE_TIMEOUT:
        await asyncio.sleep(2)
        elapsed += 2
        if await is_service_up(target_port):
            print(f"[Proxy] Target {GPU_HOST_IP}:{target_port} came online after {elapsed}s.")
            return True

    raise RuntimeError(f"Host {GPU_HOST_IP}:{target_port} failed to respond within {WAKE_TIMEOUT} seconds.")
@app.websocket("/{path:path}")
async def websocket_proxy(websocket: WebSocket, path: str):
  target_port = websocket.url.port or 8080
  await ensure_host_awake(target_port)

  # Construct the upstream WebSocket target URL
  ws_url = (
      f"ws://{GPU_HOST_IP}:{target_port}/{path}"
      + (f"?{websocket.query_params}" if websocket.query_params else "")
  )

  await websocket.accept()

  import websockets

  # Forward headers except host
  headers = [
      (k, v) for k, v in websocket.headers.items() if k.lower() != "host"
  ]
  headers.append(("host", f"{GPU_HOST_IP}:{target_port}"))

  try:
    async with websockets.connect(
        ws_url, extra_headers=dict(headers)
    ) as target_ws:

      async def forward_to_target():
        try:
          while True:
            data = await websocket.receive_text()
            await target_ws.send(data)
        except WebSocketDisconnect:
          pass

      async def forward_to_client():
        try:
          async for message in target_ws:
            await websocket.send_text(message)
        except Exception:
          pass

      # Run client->target and target->client loops concurrently
      await asyncio.gather(
          forward_to_target(), forward_to_client(), return_exceptions=True
      )
  except Exception as e:
    print(f"[Proxy] WebSocket proxy error: {e}")
  finally:
    try:
      await websocket.close()
    except Exception:
      pass

      
@app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "HEAD", "OPTIONS"])
async def proxy_traffic(request: Request, path: str):
    # Determine which port the incoming request arrived on (e.g., 11434 vs 8080)
    target_port = request.url.port or 11434

    # Inside proxy_traffic() in main.py:
    body = await request.body()
    headers = dict(request.headers)

    # Pass client details through to Open WebUI
    headers["host"] = f"{GPU_HOST_IP}:{target_port}"
    if request.client:
      headers["x-forwarded-for"] = request.client.host
      headers["x-forwarded-proto"] = request.url.scheme

    try:
        await ensure_host_awake(target_port)
    except RuntimeError as e:
        return Response(content=str(e), status_code=504)

    # Build upstream URL matching the target port
    url = f"http://{GPU_HOST_IP}:{target_port}/{path}"
    if request.query_params:
        url += f"?{request.query_params}"

    body = await request.body()
    headers = dict(request.headers)
    headers.pop("host", None)

    req = client.build_request(
        method=request.method,
        url=url,
        headers=headers,
        content=body
    )
    
    r = await client.send(req, stream=True)
    return StreamingResponse(
        r.aiter_raw(),
        status_code=r.status_code,
        headers=dict(r.headers)
    )
