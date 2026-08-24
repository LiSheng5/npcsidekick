# common/python_client.py — 任意 Python 环境接大脑（无框架依赖）
import base64, json, time, urllib.request


SERVER = "http://127.0.0.1:8765"


def _post(path, payload):
    req = urllib.request.Request(
        SERVER + path,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read().decode("utf-8"))


def talk(npc_id: str, message: str, voice: bool = True):
    """对话；返回 (reply_text, mp3_bytes 或 None)。"""
    data = _post("/api/talk", {"npc_id": npc_id, "message": message, "voice": voice})
    audio = None
    if data.get("audio"):
        audio = base64.b64decode(data["audio"])
    return data["reply"], audio


def state():
    """世界状态镜像：{actors, delivered, tick, log_tail}。"""
    with urllib.request.urlopen(SERVER + "/api/state", timeout=10) as r:
        return json.loads(r.read().decode("utf-8"))


# 事件协议（Codex core/界面协议化对照）: 界面只消费事件, 不做状态差分。
# 客户端持有 log_count 游标, 每次取 since 之后的新事件驱动气泡/表演/交付动画。
def events(since: int = 0):
    """事件流增量: {events:[{type,npc,text|dest|resource|product}...], log_count, tick, delivered}。"""
    with urllib.request.urlopen(SERVER + f"/api/events?since={since}", timeout=10) as r:
        return json.loads(r.read().decode("utf-8"))


def poll_events():
    """事件轮询示例: 用返回的 log_count 当游标, 只处理新增事件。"""
    cursor = 0
    while True:
        data = events(since=cursor)
        for ev in data["events"]:
            if ev["type"] == "say":
                print(f"[气泡] {ev['npc']}: {ev['text']}")
            elif ev["type"] == "deliver":
                print(f"[交付] {ev['npc']} 给了主角 {ev['resource']}")
            else:
                print(f"[事件] {ev}")
        cursor = data["log_count"]   # 游标推进
        time.sleep(2.0)


if __name__ == "__main__":
    reply, audio = talk("cang", "你好")
    print("苍说:", reply)
    print("语音字节数:", len(audio) if audio else 0)
