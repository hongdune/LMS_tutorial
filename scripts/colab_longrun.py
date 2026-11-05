"""Manual experiment (not a test): long-running execution over a Jupyter WebSocket,
counting dropped connections and lost stream lines. Used for the 2026-11-05 measurement
in CLAUDE.md chapter 3. Needs `requests` and `tornado`; never prints tokens.

Two rules the script must keep, or it measures itself:
- header.session == URL session_id (contract 2; the Colab proxy drops the WS otherwise);
- never cancel a pending tornado read_message() (asyncio.wait_for on it drops the message
  that was arriving at the timeout).
Each run: new kernel, warm-up (torch import, CUDA init) on its own session_id, then a
cell printing `line<i>` once per second on a fresh session_id.

usage:
  longrun.py bridge <port> <lms_token> <origin> <runs> <seconds> [drop_at] [reconnect_delay]
  longrun.py direct <colab_session> - - <runs> <seconds> [drop_at] [reconnect_delay]
drop_at>0: the client closes its WS at that many seconds into the cell and reconnects
with the same session_id after reconnect_delay (scenario C-A).
"""
import asyncio, json, sys, time, uuid
from urllib.parse import urlencode
import requests
from tornado import httpclient, websocket

mode, a1, a2, a3 = sys.argv[1:5]
runs, seconds = int(sys.argv[5]), int(sys.argv[6])
drop_at = float(sys.argv[7]) if len(sys.argv) > 7 else 0
delay = float(sys.argv[8]) if len(sys.argv) > 8 else 0

if mode == "bridge":
    base = f"http://127.0.0.1:{a1}"
    H = {"Authorization": f"token {a2}", "Origin": a3}
    extra_q = []
else:
    e = json.load(open("/root/.config/colab-cli/sessions.json"))[a1]
    base, tok = e["url"], e["token"]
    H = {"X-Colab-Runtime-Proxy-Token": tok}
    extra_q = [("colab-runtime-proxy-token", tok)]


def new_kernel():
    for _ in range(10):
        try:
            r = requests.post(f"{base}/api/kernels", headers=H, json={"name": "python3"}, timeout=120)
            if r.status_code == 201:
                return r.json()["id"]
        except requests.RequestException:
            pass
        time.sleep(3)
    raise RuntimeError("POST /api/kernels failed")


async def connect(kid, sid):
    url = base.replace("http", "ws", 1) + f"/api/kernels/{kid}/channels?" + urlencode([("session_id", sid)] + extra_q)
    return await websocket.websocket_connect(httpclient.HTTPRequest(url, headers=H), ping_interval=None)


def msg(sid, msg_type, content, channel="shell"):
    mid = uuid.uuid4().hex
    return mid, json.dumps({"header": {"msg_id": mid, "msg_type": msg_type, "username": "lms", "session": sid,
                                       "date": "", "version": "5.3"},
                            "parent_header": {}, "metadata": {}, "channel": channel, "content": content})


def execute(sid, code):
    return msg(sid, "execute_request", {"code": code, "silent": False, "store_history": True,
                                        "user_expressions": {}, "allow_stdin": False, "stop_on_error": True})


async def wait_ready(ws, sid):
    for _ in range(60):
        mid, m = msg(sid, "kernel_info_request", {})
        await ws.write_message(m)
        t0 = time.time()
        while time.time() - t0 < 2:
            try:
                r = await asyncio.wait_for(ws.read_message(), 2)
            except asyncio.TimeoutError:
                break
            if r is None:
                raise RuntimeError("closed while waiting for kernel_info")
            d = json.loads(r)
            if d.get("parent_header", {}).get("msg_id") == mid and d["header"]["msg_type"] == "kernel_info_reply":
                return
    raise RuntimeError("kernel not ready")


async def run_cell(kid, sid, ws, code, expected_lines, timeout):
    """Returns (ws, result dict). Reconnects (same sid) on drop when drop_at is set, or
    reports the natural drop."""
    mid, m = execute(sid, code)
    await ws.write_message(m)
    t0 = time.time()
    lines, reply, idle, drops, client_drop_done = [], False, False, [], False
    buf = ""
    fut = None
    while time.time() - t0 < timeout:
        if drop_at and not client_drop_done and time.time() - t0 >= drop_at:
            client_drop_done = True
            ws.close()
            fut = None
            await asyncio.sleep(delay)
            ws = await connect(kid, sid)
            drops.append(("client", round(time.time() - t0, 1)))
            continue
        if fut is None:
            fut = asyncio.ensure_future(ws.read_message())
        done, _ = await asyncio.wait({fut}, timeout=1.0)  # never cancel a pending read
        if done:
            r, fut = fut.result(), None
        else:
            if idle and reply:
                break
            if idle and time.time() - idle_at > 3:
                break
            continue
        if r is None:
            drops.append(("natural", round(time.time() - t0, 1), ws.close_code))
            if mode == "direct":
                # reconnect with the same session_id, like the bridge (B) / browser (A) would
                try:
                    ws = await connect(kid, sid)
                    continue
                except Exception as e:
                    drops.append(("reconnect-failed", str(e)[:80]))
                    break
            break  # bridge closed the browser WS (1011): count as fatal
        d = json.loads(r)
        if d.get("parent_header", {}).get("msg_id") != mid:
            continue
        t = d["header"]["msg_type"]
        if t == "stream":
            buf += d["content"]["text"]
        elif t == "execute_reply":
            reply = d["content"]["status"]
        elif t == "status" and d["content"]["execution_state"] == "idle":
            idle, idle_at = True, time.time()
        if reply and idle:
            break
    got = sorted({int(x[4:]) for x in buf.split() if x.startswith("line")})
    missing = sorted(set(range(expected_lines)) - set(got))
    return ws, {"secs": round(time.time() - t0, 1), "lines": len(got), "missing": missing[:10],
                "nmissing": len(missing), "reply": reply, "idle": idle, "drops": drops}


CELL = """
import time, torch
x = torch.randn(2048, 2048, device='cuda' if torch.cuda.is_available() else 'cpu')
for i in range({n}):
    t = time.time()
    while time.time() - t < 1:
        y = x @ x
        if torch.cuda.is_available(): torch.cuda.synchronize()
    print(f"line{{i}}", flush=True)
"""


async def main():
    for n in range(runs):
        kid = new_kernel()
        try:
            # warm-up on its own session_id (torch import + CUDA init take ~50 s)
            wsid = uuid.uuid4().hex
            ws = await connect(kid, wsid)
            await wait_ready(ws, wsid)
            mid, m = execute(wsid, "import torch; torch.zeros(1, device='cuda' if torch.cuda.is_available() else 'cpu')")
            await ws.write_message(m)
            t0 = time.time()
            while time.time() - t0 < 180:
                r = await ws.read_message()
                if r is None:
                    ws = await connect(kid, wsid); continue
                d = json.loads(r)
                if d.get("parent_header", {}).get("msg_id") == mid and d["header"]["msg_type"] == "execute_reply":
                    break
            ws.close()
            sid = uuid.uuid4().hex
            ws = await connect(kid, sid)
            await wait_ready(ws, sid)
            ws, res = await run_cell(kid, sid, ws, CELL.format(n=seconds), seconds, seconds + 120)
            print(json.dumps({"run": n + 1, "mode": mode, "drop_at": drop_at, "delay": delay, **res}), flush=True)
            ws.close()
        except Exception as e:
            print(json.dumps({"run": n + 1, "error": f"{type(e).__name__}: {e}"[:200]}), flush=True)
        try:
            requests.delete(f"{base}/api/kernels/{kid}", headers=H, timeout=30)
        except requests.RequestException:
            pass
        await asyncio.sleep(2)


asyncio.run(main())
