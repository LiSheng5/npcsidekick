// NpcBrain.cs — Unity 客户端：把 NPCSidekick 大脑接进 Unity 游戏
// 用法：挂在一个 GameObject 上，填 npcId / voice；对话时调 Talk()，Update 里自动轮询镜像。
// 参考实现逻辑与 Godot 的 Villager.gd 一致（轮询镜像 + 表演 + 交付 + 气泡）。
using System;
using System.Text;
using UnityEngine;
using UnityEngine.Networking;

public class NpcBrain : MonoBehaviour
{
    [Tooltip("大脑服务器地址，勿带末尾斜杠")]
    public string serverUrl = "http://127.0.0.1:8765";
    [Tooltip("村民 id（服务器按它注入人格）")]
    public string npcId = "cang";
    [Tooltip("请求语音（true 则响应含 audio base64 mp3）")]
    public bool voice = true;
    [Tooltip("轮询镜像间隔秒")]
    public float pollInterval = 2f;

    // 最近一次世界状态镜像（供表演逻辑读取）
    public string Position { get; private set; } = "";
    public string State { get; private set; } = "idle";

    public event Action<string> OnReply;      // 收到对话回复文本
    public event Action<byte[]> OnAudio;      // 收到语音 mp3 字节（可为 null）
    private float _timer;

    // —— 对话：POST /api/talk ——
    public void Talk(string message)
    {
        var payload = "{\"npc_id\":\"" + npcId + "\",\"message\":\"" + EscapeJson(message)
                      + (voice ? "\",\"voice\":true}" : "\"}");
        StartCoroutine(Post("/api/talk", payload, (code, text) =>
        {
            if (code != 200) return;
            var data = ParseJson(text);
            var reply = data["reply"].ToString();
            OnReply?.Invoke(reply);
            if (data.ContainsKey("audio") && !string.IsNullOrEmpty(data["audio"].ToString()))
                OnAudio?.Invoke(Convert.FromBase64String(data["audio"].ToString()));
        }));
    }

    // —— 轮询镜像：GET /api/state ——
    IEnumerator PollState()
    {
        using (var req = UnityWebRequest.Get(serverUrl + "/api/state"))
        {
            yield return req.SendWebRequest();
            if (req.result == UnityWebRequest.Result.Success)
            {
                var data = ParseJson(req.downloadHandler.text);
                if (data.ContainsKey("actors") &&
                    data["actors"] is Newtonsoft.Json.Linq.JObject actors &&
                    actors[npcId] is Newtonsoft.Json.Linq.JObject me)
                {
                    Position = me["position"]?.ToString() ?? "";
                    State = me["state"]?.ToString() ?? "idle";
                    // 交付：读 me["inventory"] / delivered 增量，触发送货动画
                }
            }
        }
    }

    void Update()
    {
        _timer += Time.deltaTime;
        if (_timer >= pollInterval) { _timer = 0f; StartCoroutine(PollState()); }
    }

    // —— HTTP 小工具 ——
    IEnumerator Post(string path, string json, Action<long, string> done)
    {
        using (var req = new UnityWebRequest(serverUrl + path, "POST"))
        {
            req.uploadHandler = new UploadHandlerRaw(Encoding.UTF8.GetBytes(json));
            req.downloadHandler = new DownloadHandlerBuffer();
            req.SetRequestHeader("Content-Type", "application/json");
            yield return req.SendWebRequest();
            done?.Invoke(req.responseCode, req.downloadHandler.text);
        }
    }
    static string EscapeJson(string s) => s.Replace("\", "\\").Replace("\"", "\\"");
    static Newtonsoft.Json.Linq.JObject ParseJson(string s) =>
        string.IsNullOrEmpty(s) ? new Newtonsoft.Json.Linq.JObject() : Newtonsoft.Json.Linq.JObject.Parse(s);
    static string GetString(JObject o, string key) => o.ContainsKey(key) ? o[key].ToString() : "";
}
