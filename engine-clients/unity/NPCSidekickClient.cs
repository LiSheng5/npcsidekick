// NPCSidekickClient.cs — Unity 客户端骨架 v0.1（协议见 engine-clients/common/PROTOCOL.md）
// ------------------------------------------------------------------
// 两个实战客户端(Godot DialogueUI / GTA5 SHVDN mod)踩出来的模式已内置:
//   超时竞争法 · 请求序号防串台 · 本地话术兜底 · 走开取消。
// TODO(接入者): 按游戏补全标 [TODO] 的位置。Unity 2021+ / .NET standard 2.1。
// ------------------------------------------------------------------
using System;
using System.Collections;
using System.Text;
using UnityEngine;
using UnityEngine.Networking;

namespace NPCSidekick
{
    public class NPCSidekickClient : MonoBehaviour
    {
        [Header("服务端(多世界见 PROTOCOL.md §1)")]
        public string baseUrl = "http://127.0.0.1:8765";
        public string worldId = "unity";          // 各实例唯一; 不匹配 → HTTP 409
        public string npcId = "cang";

        [Header("超时竞争法(PROTOCOL §3.1): 大脑慢是常态,120s 上限")]
        public float httpTimeoutSec = 120f;

        private int _reqSeq = 0;                  // 请求序号防串台(PROTOCOL §3.2)
        private Vector3 _talkPos;                 // 走开取消基准点

        // ── 对话入口 ─────────────────────────────────────
        public void Talk(string message)
        {
            _talkPos = transform.position;
            int seq = ++_reqSeq;                  // ★ 每次请求取号
            StartCoroutine(PostTalk(message, seq));
        }

        private IEnumerator PostTalk(string message, int seq)
        {
            var body = new StringBuilder("{");
            body.Append("\"npc_id\":\"").Append(npcId).Append("\",");
            body.Append("\"message\":\"").Append(JsonEscape(message)).Append("\",");
            body.Append("\"thinking\":\"off\",");
            body.Append("\"voice\":true,");
            body.Append("\"world_id\":\"").Append(worldId).Append("\"}");
            var req = new UnityWebRequest(baseUrl + "/api/talk", "POST");
            req.SetRequestHeader("Content-Type", "application/json; charset=utf-8");
            req.uploadHandler = new UploadHandlerRaw(Encoding.UTF8.GetBytes(body.ToString()));
            req.downloadHandler = new DownloadHandlerBuffer();
            req.timeout = (int)(httpTimeoutSec * 1000);
            yield return req.SendWebRequest();

            // ★ 防串台: 走开/新一轮开始后, 迟到的响应直接丢弃(PROTOCOL §3.2)
            if (seq != _reqSeq) yield break;
            // ★ 走开取消: 玩家离开交互距离 = 本轮作废(PROTOCOL §0-4)
            if (Vector3.Distance(transform.position, _talkPos) > 10f) yield break;

            if (req.result != UnityWebRequest.Result.Success)
            {
                OnReply(LocalFallback(req.responseCode));   // ★ 零惩罚兜底 §3.3
                yield break;
            }
            string reply = ExtractJsonString(req.downloadHandler.text, "reply");
            string audio = ExtractJsonString(req.downloadHandler.text, "audio");
            OnReply(string.IsNullOrEmpty(reply) ? LocalFallback(0) : reply);
            if (!string.IsNullOrEmpty(audio)) PlayVoice(audio);   // 失败静默,不碰文字
        }

        // [TODO] 接你的对话 UI(打字机/头像), 参考模式: 先显示"思考中…已等X秒"
        private void OnReply(string text) { Debug.Log($"[NPCSidekick] {text}"); }

        // [TODO] base64 mp3 → 临时文件 → 平台播放器; 任何失败静默跳过(PROTOCOL §3.5)
        private void PlayVoice(string audioB64) { }

        // 本地话术兜底表(PROTOCOL §3.3) — 玩家永远有话说
        private static string LocalFallback(long code)
        {
            switch (code)
            {
                case 404: return "(大脑上没有这个人设…查 --adapter 和 personas)";
                case 409: return "(接错世界实例了…检查端口和 world_id)";
                default: return "(大脑没连上…服务器开了吗?)";
            }
        }

        // [TODO] 世界镜像轮询: 每 2s GET /api/state 同步 NPC activity(§2.2);
        //        事件流可先用 /api/events 轮询, NPC 多了换 SSE /api/events/stream(§2.4)
        // [TODO] 动态人设: 街头 NPC 用 POST /api/npc/register(persistent:false)(§2.5)

        private static string JsonEscape(string s)
        {
            var sb = new StringBuilder();
            foreach (char c in s)
            {
                if (c == '"') sb.Append("\\\"");
                else if (c == '\\') sb.Append("\\\\");
                else if (c == '\n') sb.Append("\\n");
                else if (c < ' ') sb.Append("\\u").Append(((int)c).ToString("x4"));
                else sb.Append(c);
            }
            return sb.ToString();
        }

        // 极简 JSON 取值(受控服务端响应够用; 要完整解析换 Newtonsoft)
        private static string ExtractJsonString(string json, string key)
        {
            if (string.IsNullOrEmpty(json)) return null;
            int k = json.IndexOf("\"" + key + "\"", StringComparison.Ordinal);
            if (k < 0) return null;
            int colon = json.IndexOf(':', k + key.Length + 2);
            int q = json.IndexOf('"', colon);
            if (colon < 0 || q < 0) return null;
            var sb = new StringBuilder();
            for (int i = q + 1; i < json.Length; i++)
            {
                char c = json[i];
                if (c == '\\')
                {
                    char e = json[i + 1];
                    if (e == '"') sb.Append('"');
                    else if (e == 'n') sb.Append('\n');
                    else i++;   // 其他转义按需补
                    continue;
                }
                if (c == '"') break;
                sb.Append(c);
            }
            return sb.ToString();
        }
    }
}
