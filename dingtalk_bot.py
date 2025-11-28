from flask import Flask, request, jsonify
import dashscope
from dashscope import Generation
import requests
import json
import logging
import time
import hmac
import hashlib
import base64
from urllib.parse import quote_plus

# === 配置 ===
DASHSCOPE_API_KEY = "sk-b7c98253f43c4803a279231695584967"  # ← 替换为你自己的
OPENWEATHER_API_KEY = "10d32dcb141261a308068218d8125dcb"
DINGTALK_WEBHOOK = "https://oapi.dingtalk.com/robot/send?access_token=YOUR_ACCESS_TOKEN"  # ← 替换
DINGTALK_SECRET = "SECxxx"  # ← 替换为你的加签密钥（若开启）

dashscope.api_key = DASHSCOPE_API_KEY

app = Flask(__name__)
logging.basicConfig(level=logging.INFO)

# === 钉钉加签验证（必须实现）===
def verify_dingtalk_signature(timestamp: str, sign: str) -> bool:
    if not DINGTALK_SECRET:
        return True  # 未开启加签则跳过
    string_to_sign = f"{timestamp}\n{DINGTALK_SECRET}"
    hmac_code = hmac.new(
        DINGTALK_SECRET.encode('utf-8'),
        string_to_sign.encode('utf-8'),
        hashlib.sha256
    ).digest()
    expected_sign = base64.b64encode(hmac_code).decode('utf-8')
    return expected_sign == sign

# === 获取天气（同你原逻辑，略作健壮性增强）===
def get_weather(city: str) -> dict:
    try:
        geo_resp = requests.get(
            "http://api.openweathermap.org/geo/1.0/direct",
            params={"q": city, "limit": 1, "appid": OPENWEATHER_API_KEY},
            timeout=5
        )
        geo_data = geo_resp.json()
        if not geo_data:
            return {"error": f"未找到城市: {city}"}
        lat, lon = geo_data[0]['lat'], geo_data[0]['lon']

        weather_resp = requests.get(
            "https://api.openweathermap.org/data/2.5/weather",
            params={
                "lat": lat,
                "lon": lon,
                "appid": OPENWEATHER_API_KEY,
                "units": "metric",
                "lang": "zh_cn"
            },
            timeout=5
        )
        weather_data = weather_resp.json()
        return {
            "city": city,
            "temperature": round(weather_data['main']['temp']),
            "description": weather_data['weather'][0]['description'],
            "humidity": weather_data['main']['humidity'],
            "success": True
        }
    except Exception as e:
        return {"error": f"查询失败: {str(e)}", "success": False}

# === Function Calling 工具定义 ===
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "get_weather",
            "description": "查询指定城市的当前天气",
            "parameters": {
                "type": "object",
                "properties": {"city": {"type": "string"}},
                "required": ["city"]
            }
        }
    }
]

# === 主问答逻辑（支持 Function Calling）===
def weather_mcp_with_llm(user_query: str) -> str:
    messages = [{"role": "user", "content": user_query}]
    try:
        response = Generation.call(
            model="qwen-max",
            messages=messages,
            tools=TOOLS,
            tool_choice="auto"
        )
        msg = response.output.choices[0].message

        if msg.tool_calls:
            tool_call = msg.tool_calls[0]
            args = json.loads(tool_call.function.arguments)
            city = args.get("city", "")
            weather_res = get_weather(city)

            messages.append(msg)
            messages.append({
                "role": "tool",
                "content": json.dumps(weather_res, ensure_ascii=False),
                "tool_call_id": tool_call.id
            })

            final_resp = Generation.call(model="qwen-max", messages=messages)
            return final_resp.output.choices[0].message.content
        else:
            return msg.content or "我暂时无法回答这个问题。"
    except Exception as e:
        logging.error(f"LLM 调用异常: {e}")
        try:
            fallback = Generation.call(model="qwen-turbo", messages=messages)
            return fallback.output.text
        except:
            return "系统繁忙，请稍后再试～"

# === 发送钉钉消息（支持 text/markdown）===
def send_dingtalk_message(content: str, msgtype: str = "text", at_mobiles=None):
    if at_mobiles is None:
        at_mobiles = []
    payload = {
        "msgtype": msgtype,
        msgtype: {"content": content},
        "at": {"atMobiles": at_mobiles}
    }
    headers = {'Content-Type': 'application/json'}
    resp = requests.post(DINGTALK_WEBHOOK, json=payload, headers=headers, timeout=5)
    logging.info(f"钉钉响应: {resp.status_code} {resp.text}")

# === Webhook 入口 ===
@app.route('/dingtalk/webhook', methods=['POST'])
def dingtalk_webhook():
    timestamp = request.headers.get('Timestamp')
    sign = request.headers.get('Sign')

    # ✅ 安全校验（钉钉强制要求）
    if not verify_dingtalk_signature(timestamp, sign):
        return jsonify({"errcode": 403, "errmsg": "Invalid signature"}), 403

    data = request.json
    logging.info(f"收到钉钉消息: {data}")

    # 解析是否 @ 了机器人
    at_users = data.get("at", {}).get("atUserIds", [])
    bot_id = "your_bot_dingtalk_id"  # ← 替换为机器人在群中的 userId（可在机器人设置里查，或调试时打印 data 查看）
    if bot_id not in at_users:
        return jsonify({"status": "ignored", "reason": "not @me"}), 200

    text = data.get("text", {}).get("content", "").strip()
    # 去掉 @部分（钉钉会把 @ 内容拼在开头）
    if text.startswith(f"@{bot_id}"):
        text = text[len(f"@{bot_id}"):].strip()

    if not text:
        return jsonify({"status": "empty"}), 200

    try:
        reply = weather_mcp_with_llm(text)
        send_dingtalk_message(reply)
        return jsonify({"status": "success"}), 200
    except Exception as e:
        logging.exception("处理失败")
        send_dingtalk_message(f"❌ 处理出错：{str(e)[:100]}")
        return jsonify({"status": "error", "msg": str(e)}), 500

# === 启动 ===
if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8080)