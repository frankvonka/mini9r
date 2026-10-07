#!/usr/bin/env python3
"""mini9r - keyless OpenAI/Claude/Gemini router on OpenCode Zen free tier.
  python mini9r.py                  # http://127.0.0.1:8320/v1, no key
  python mini9r.py --port 8712
  python mini9r.py --selftest       # offline checks
Endpoints: POST /v1/chat/completions, POST /v1/messages,
POST /v1beta/models/<m>:generateContent, GET /v1/models, GET /health, GET / (=text stats)
"""
from __future__ import annotations
import argparse, json, random, re, string, sys, time, urllib.error, urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ZEN_CHAT = "https://opencode.ai/zen/v1/chat/completions"
ZEN_MODELS = "https://opencode.ai/zen/v1/models"
TIMEOUT, COOLDOWN, MAX_BODY = 120, 600, 16 * 1024 * 1024
UA, CLIENT = "opencode/1.18.31", "desktop"
B62 = string.digits + string.ascii_letters
DEPRECATED = {"mimo-v2.5-free"}

CHAIN = ["space-bunny-free", "mimo-v2.6-flash-free",
    "muse-spark-1.3-contributor-free", "muse-spark-1.2-contributor-free",
    "longcat-2.5-preview-free", "jev-1.13-free", "ling-3.0-flash-fin-free",
    "nemotron-3-ultra-free", "nemotron-3.5-lightning-free",
    "deepseek-v4-flash-free", "big-pickle", "fledge-alpha-free",
    "ling-3.1-flash-free"]
QUARTET = [{"type": "function", "function": {"name": n, "description": n,
    "parameters": {"type": "object", "properties": {}}}} for n in ("bash", "glob", "grep", "read")]
cool: dict[str, float] = {}
S: dict[str, list] = {}  # model -> [req, ok, fail, rtk_saved]
CODE: dict[str, int] = {}  # upstream status -> count, e.g. "200" / "403"
START = time.time()

def log(m): print(f"[{time.strftime('%H:%M:%S')}] {m}", file=sys.stderr, flush=True)
def st(m): return S.setdefault(m, [0, 0, 0, 0])
def ses():
    # ^ses_[0-9a-f]{12}[0-9A-Za-z]{14}$ — 6 hex gets 403 on every model but bunny
    h = "".join("0123456789abcdef"[random.getrandbits(4) % 16] for _ in range(12))
    return f"ses_{h}" + "".join(B62[random.getrandbits(8) % 62] for _ in range(14))
def zh(): return {"Content-Type": "application/json", "User-Agent": UA,
    "x-opencode-client": CLIENT, "x-opencode-session": ses()}

# --- rtk: shrink tool output (ansi, dup lines, b64, head+tail) ---
ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
def rtk_c(t, mx=4000):
    if not isinstance(t, str) or not t: return t, 0
    o = len(t); t = ANSI.sub("", t)
    if len(t) > 200 and t.strip()[:1] in "[{":
        try: t = json.dumps(json.loads(t), separators=(",", ":")) if len(t) > 500 else t
        except Exception: pass
    seen, out = {}, []
    for ln in t.splitlines():
        ln = ln.rstrip()
        if not ln.strip(): continue
        if ln not in seen: seen[ln] = 0; out.append(ln)
        seen[ln] += 1
    t = "\n".join(l if seen[l] == 1 else f"{l}  [x{seen[l]}]" for l in out)
    if len(t) > mx:
        h, tl = int(mx * .7), mx - int(mx * .7)
        t = t[:h] + f"\n... [rtk -{len(t) - mx}c] ...\n" + t[-tl:]
    return t, max(0, o - len(t))
def rtk_walk(msgs):
    sv = 0
    for m in msgs:
        if not isinstance(m, dict): continue
        if m.get("role") == "tool" and isinstance(m.get("content"), str):
            m["content"], s = rtk_c(m["content"]); sv += s
        elif m.get("role") == "user" and isinstance(m.get("content"), list):
            for b in m["content"]:
                if isinstance(b, dict) and b.get("type") == "tool_result" and isinstance(b.get("content"), str):
                    b["content"], s = rtk_c(b["content"]); sv += s
    return sv

def finger(b):
    b = dict(b); b["stream"] = True  # upstream 403s stream:false
    f = []
    for m in b.get("messages", []):  # upstream 403s role:tool, fold into user
        if isinstance(m, dict) and m.get("role") == "tool":
            c = "<tool result>\n" + str(m.get("content", "")) + "\n</tool result>"
            if f and f[-1].get("role") == "user": f[-1]["content"] += "\n\n" + c
            else: f.append({"role": "user", "content": c})
        else: f.append(m)
    b["messages"] = f
    t = b.get("tools")
    if not isinstance(t, list): b["tools"] = list(QUARTET)
    else:
        nms = {(x.get("function") or {}).get("name", "") for x in t}
        for q in QUARTET:
            if q["function"]["name"] not in nms: t.append(q)
    return b

def call(m, b):
    p = finger(dict(b)); p["model"] = m
    r = urllib.request.Request(ZEN_CHAT, data=json.dumps(p).encode(), headers=zh(), method="POST")
    try:
        with urllib.request.urlopen(r, timeout=TIMEOUT) as x: return x.status, x.read(), x.headers.get("Content-Type", "")
    except urllib.error.HTTPError as e: return e.code, e.read(), ""
    except Exception as e: return 599, json.dumps({"error": str(e)}).encode(), ""

def retryable(s, raw):
    if s in (429, 500, 502, 503, 504, 599): return True
    bl = raw[:400].lower()
    return any(x in bl for x in (b"freeusage", b"free tier", b"unavailable", b"rate limit", b"upstream request failed"))

def assemble(raw):
    t, m = "", ""
    for ln in raw.decode("utf-8", "replace").splitlines():
        if not ln.startswith("data: ") or ln == "data: [DONE]": continue
        try: ch = json.loads(ln[6:])
        except Exception: continue
        m = m or ch.get("model", "")
        for c in ch.get("choices", []): t += (c.get("delta", {}) or {}).get("content") or ""
    return {"id": f"mini9r-{int(time.time())}", "object": "chat.completion", "model": m,
        "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": t.strip()}}]}

def live(want):
    n = time.time()
    ch = [m for m in CHAIN if cool.get(m, 0) < n] or list(CHAIN)
    if want and want not in ("auto", "", None) and want in CHAIN: ch = [want] + [x for x in ch if x != want]
    return ch

def forward(b, stream, hdrs):
    sv = 0 if hdrs.get("x-rtk", "").lower() in ("off", "0", "false") else rtk_walk(b.get("messages", []))
    want, last = b.get("model", "auto"), (503, {"error": "no free model available"}, "json")
    ch = live(want)
    for i, m in enumerate(ch):
        h, raw, ct = call(m, b)
        s = st(m); s[0] += 1                    # per-model request count
        if i == 0: s[3] += sv
        CODE[str(h)] = CODE.get(str(h), 0) + 1  # global status tally
        if h == 200 and raw:
            cool.pop(m, None); s[1] += 1
            if "text/event-stream" in ct:
                if stream: return 200, raw, "sse"
                d = assemble(raw)
            else:
                try: d = json.loads(raw)
                except Exception: return 200, {"error": raw.decode("utf-8", "replace")[:500]}, "json"
            d["model"] = d.get("model") or m  # true responder, never the request name
            return 200, d, "json"
        s[2] += 1; cool[m] = time.time() + COOLDOWN; last = (h, _err(raw), "json")
        if not retryable(h, raw): break
    return last

def _err(raw):
    try: return json.loads(raw)
    except Exception: return {"error": raw.decode("utf-8", "replace")[:500]}

# --- format translation ---
def tx(c):
    if isinstance(c, str): return c
    if isinstance(c, list):
        o = []
        for x in c:
            if isinstance(x, dict):
                if isinstance(x.get("text"), str): o.append(x["text"])
                elif isinstance(x.get("content"), str): o.append(x["content"])
            elif isinstance(x, str): o.append(x)
        return "\n".join(o)
    if isinstance(c, dict): return tx(c.get("parts") or c.get("content") or "")
    return ""

def c2o(r):
    ms = []
    if r.get("system"): ms.append({"role": "system", "content": tx(r["system"])})
    for m in r.get("messages", []):
        ms.append({"role": "user" if m.get("role") == "tool" else m.get("role"), "content": tx(m.get("content"))})
    o = {"model": r.get("model", "auto"), "messages": ms}
    if isinstance(r.get("max_tokens"), int): o["max_tokens"] = r["max_tokens"]
    if isinstance(r.get("temperature"), (int, float)): o["temperature"] = r["temperature"]
    return o

def o2c(d, model):
    ch = (d.get("choices") or [{}])[0]; mg = ch.get("message", {}) or {}; ct = []
    if mg.get("content"): ct.append({"type": "text", "text": mg["content"]})
    for tc in mg.get("tool_calls") or []:
        fn = tc.get("function", {}) or {}
        try: inp = json.loads(fn.get("arguments", "{}"))
        except Exception: inp = {}
        ct.append({"type": "tool_use", "id": tc.get("id") or "toolu_0", "name": fn.get("name", ""), "input": inp})
    u = d.get("usage") or {}
    return {"id": "msg_" + str(d.get("id") or int(time.time())).replace("mini9r-", ""), "type": "message",
        "role": "assistant", "model": model, "content": ct or [{"type": "text", "text": ""}],
        "stop_reason": {"stop": "end_turn", "length": "max_tokens", "tool_calls": "tool_use"}.get(ch.get("finish_reason"), "end_turn"),
        "stop_sequence": None, "usage": {"input_tokens": u.get("prompt_tokens", 0), "output_tokens": u.get("completion_tokens", 0)}}

def g2o(r):
    ms = []
    si = r.get("systemInstruction") or r.get("system_instruction")
    if si: ms.append({"role": "system", "content": tx(si)})
    for c in r.get("contents", []):
        ms.append({"role": "assistant" if c.get("role") in ("model", "assistant") else "user", "content": tx(c.get("parts", []))})
    o = {"model": r.get("model", "auto"), "messages": ms}
    if r.get("maxOutputTokens"): o["max_tokens"] = int(r["maxOutputTokens"])
    if isinstance(r.get("temperature"), (int, float)): o["temperature"] = r["temperature"]
    return o

def o2g(d, model):
    ch = (d.get("choices") or [{}])[0]; t = (ch.get("message") or {}).get("content") or ""; u = d.get("usage") or {}
    return {"candidates": [{"content": {"role": "model", "parts": [{"text": t}]},
        "finishReason": "MAX_TOKENS" if ch.get("finish_reason") == "length" else "STOP", "index": 0}],
        "modelVersion": model, "usageMetadata": {"promptTokenCount": u.get("prompt_tokens", 0),
        "candidatesTokenCount": u.get("completion_tokens", 0), "totalTokenCount": u.get("total_tokens", 0)}}

def stats_txt():
    tot = sum(CODE.values())
    L = [f"mini9r up {int(time.time() - START) // 60}m",
         "status " + (" ".join(f"{k}={v}" for k, v in sorted(CODE.items())) if tot else "none yet")]
    for m, (rq, ok, fl, rtk) in sorted(S.items(), key=lambda kv: -kv[1][1]):
        L.append(f"{m:34} req={rq:<4} ok={ok:<4} fail={fl:<4} rtk={rtk}")
        if cool.get(m, 0) > time.time(): L[-1] += "  COOLING"
    return "\n".join(L) + "\n"

class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"; server_version = "mini9r/4.0"
    def log_message(self, *a): pass
    def send(self, s, b, ct="application/json"):
        self.send_response(s); self.send_header("Content-Type", ct)
        self.send_header("Content-Length", str(len(b))); self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        try: self.wfile.write(b)
        except BrokenPipeError: pass
    def js(self, s, o): self.send(s, json.dumps(o).encode())
    def body(self):
        n = int(self.headers.get("Content-Length", 0))
        if n > MAX_BODY: self.js(413, {"error": "body too large"}); return None
        try: return json.loads(self.rfile.read(n) or b"{}")
        except Exception as e: self.js(400, {"error": f"bad request: {e}"}); return None
    def do_OPTIONS(self):  # noqa
        self.send_response(204)
        for k, v in (("Access-Control-Allow-Origin", "*"), ("Access-Control-Allow-Headers", "*"), ("Access-Control-Allow-Methods", "GET, POST, OPTIONS")): self.send_header(k, v)
        self.send_header("Content-Length", "0"); self.end_headers()
    def do_GET(self):  # noqa
        p = self.path.split("?")[0].rstrip("/")
        if p in ("/v1/models", "/models"):
            self.js(200, {"object": "list", "data": [{"id": "auto", "object": "model", "owned_by": "mini9r"}] +
                [{"id": m, "object": "model", "owned_by": "opencode-zen"} for m in live(None)]})
        elif p in ("/health", "/v1/health"):
            self.js(200, {"ok": True, "cooling": [m for m in CHAIN if cool.get(m, 0) > time.time()],
                "hits": {m: v[1] for m, v in S.items()}, "status": dict(CODE)})
        elif p in ("", "/"): self.send(200, stats_txt().encode(), "text/plain; charset=utf-8")
        else: self.js(404, {"error": "not found"})
    def do_POST(self):  # noqa
        path = self.path.split("?")[0]; req = self.body()
        if req is None: return
        hd = {k.lower(): v for k, v in self.headers.items()}
        if "chat/completions" in path:
            s, d, k = forward(req, bool(req.get("stream")), hd)
            if k == "sse": self.send(200, d, "text/event-stream")
            elif isinstance(d, dict) and "choices" in d: self.js(200, d)
            else: self.js(min(s, 502), d)
            return
        if path.endswith("/messages"):
            s, d, _ = forward(c2o(req), False, hd)
            self.js(200, o2c(d, req.get("model", "auto"))) if isinstance(d, dict) and "choices" in d else self.js(min(s, 502), d)
            return
        m = re.match(r".*/models/([^/:]+):generateContent\w*$", path)
        if m:
            s, d, _ = forward(g2o({**req, "model": m.group(1)}), False, hd)
            self.js(200, o2g(d, m.group(1))) if isinstance(d, dict) and "choices" in d else self.js(min(s, 502), d)
            return
        self.js(404, {"error": "use /v1/chat/completions, /v1/messages or /v1beta/models/<m>:generateContent"})

def refresh():
    global CHAIN
    try:
        r = urllib.request.Request(ZEN_MODELS, headers=zh())
        with urllib.request.urlopen(r, timeout=30) as x:
            ids = [m["id"] for m in json.load(x).get("data", []) if m.get("id") and m["id"] not in DEPRECATED]
        fr = [i for i in ids if i.endswith("-free") or i == "big-pickle"]
        if fr: CHAIN = [m for m in CHAIN if m in fr] + [m for m in fr if m not in CHAIN]; log(f"chain: {len(fr)} free")
    except Exception as e: log(f"model list failed: {e}")

def selftest():
    assert re.match(r"^ses_[0-9a-f]{12}[0-9A-Za-z]{14}$", ses()), "ses format"
    o = c2o({"model": "cx", "max_tokens": 5, "system": "s", "messages": [{"role": "user", "content": [{"type": "text", "text": "hi"}]}, {"role": "tool", "content": "t"}]})
    assert o["messages"][1] == {"role": "user", "content": "hi"} and o["max_tokens"] == 5
    b = o2c({"id": "mini9r-1", "choices": [{"finish_reason": "tool_calls", "message": {"content": "x", "tool_calls": [{"id": "t1", "function": {"name": "bash", "arguments": '{"c":"p"}'}}]}}]}, "cx")
    assert b["stop_reason"] == "tool_use" and b["content"][1]["input"] == {"c": "p"}
    g = g2o({"systemInstruction": {"parts": [{"text": "s"}]}, "contents": [{"role": "model", "parts": [{"text": "y"}]}]})
    assert g["messages"][0]["content"] == "s" and g["messages"][1]["role"] == "assistant"
    big = "\n".join(f"INFO {i}" for i in range(900)); out, sv = rtk_c(big)
    assert sv > 0 and "INFO 0" in out and "INFO 899" in out
    print("mini9r selftest OK")

def main():
    ap = argparse.ArgumentParser(); ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8320); ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest: selftest(); return 0
    refresh()
    srv = ThreadingHTTPServer((a.host, a.port), H); srv.daemon_threads = True
    log(f"mini9r on http://{a.host}:{a.port}/v1 (no key)  chain={CHAIN}")
    try: srv.serve_forever()
    except KeyboardInterrupt: pass
    finally: srv.server_close()

if __name__ == "__main__": raise SystemExit(main())
