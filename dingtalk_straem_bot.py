import json
import time
import logging
import threading
import websocket
import requests
import hmac
import hashlib
import base64
import urllib.parse
import warnings
import re
# 抑制 akshare 的非关键警告（如列缺失）
warnings.filterwarnings("ignore", category=FutureWarning)

# === 配置（请务必替换）===
APP_KEY = "dingsn8oslqn0wl5sl8h"          # ← 钉钉开发者后台获取
APP_SECRET = "HddoBDbjGwmRrNL1RDQe3ko3GqGWqMToKnwJBEboN0eRsmzEpVUDUXx3s92DrdSs"
OPENWEATHER_API_KEY = "10d32dcb141261a308068218d8125dcb"
DASHSCOPE_API_KEY = "sk-b7c98253f43c4803a279231695584967"

# === 全局变量 ===
ws = None
access_token_info = {"token": "", "expire_time": 0}
lock = threading.Lock()
# 存储每个用户的对话历史，key为user_id，value为对话历史列表
user_conversations = {}

# --- 以下为新增：获取 WSS 地址 ---
def get_stream_connection_url():
    """✅ 关键修复：先调用 HTTP 获取真实 WSS 地址"""
    token = get_access_token()
    url = "https://api.dingtalk.com/v1.0/gateway/connections/open"
    headers = {
        "x-acs-dingtalk-access-token": token,
        "Content-Type": "application/json"
    }
    resp = requests.post(url, headers=headers, json={
        "clientId": "dingsn8oslqn0wl5sl8h",
        "clientSecret": "HddoBDbjGwmRrNL1RDQe3ko3GqGWqMToKnwJBEboN0eRsmzEpVUDUXx3s92DrdSs",
        "subscriptions": [
            {
                "topic": "*",
                "type": "EVENT"
            },
            {
                "topic": "/v1.0/im/bot/messages/get",
                "type": "CALLBACK"
            }
        ],
        "ua": "dingtalk-sdk-java/1.0.2"
    }, timeout=10)
    data = resp.json()
    if resp.status_code != 200 or "endpoint" not in data:
        raise Exception(f"获取 WSS 地址失败: {data}")
    return data["endpoint"] + "?ticket=" + data["ticket"]

def get_access_token():
    global access_token_info
    with lock:
        now = time.time()
        if now < access_token_info["expire_time"]:
            return access_token_info["token"]

        resp = requests.post(
            "https://api.dingtalk.com/v1.0/oauth2/accessToken",
            json={"appKey": APP_KEY, "appSecret": APP_SECRET}
        )
        data = resp.json()
        if "accessToken" not in data:
            raise Exception(f"获取 access_token 失败: {data}")
        token = data["accessToken"]
        expire = now + data.get("expireIn", 7200) - 300
        access_token_info.update({"token": token, "expire_time": expire})
        return token


def get_sock_code_by_name(name: str) -> str | None:
    sock_name = weather_mcp_with_llm("test_user", f"{name}，直接输出A股股票代码,当存在多个股市时返回A股代码，并且加上股市标志开头，比如sh,sz等,不要其他任何废话")
    logging.info(f"[DEBUG] 获取股票代码({name}) -> {sock_name}")
    return sock_name

def normalize_code(symbol: str) -> str | None:
    """将股票名称或代码标准化为 sina 格式：sh600519 / sz000001"""
    symbol = symbol.strip().upper()

    # 若已是标准格式（sh/sz/hk 开头），直接返回
    if re.match(r'^(SH|SZ|HK)\d{6}$', symbol):
        return symbol

    # 尝试提取6位数字代码
    code_match = re.search(r'\d{6}', symbol)
    if code_match:
        code = code_match.group()
        # 简单判断：60/68 开头为沪市，00/30 开头为深市
        if code.startswith(('60', '68')):
            return f"sh{code}"
        elif code.startswith(('00', '30')):
            return f"sz{code}"
        else:
            return f"sh{code}"  # 默认沪市兜底
    return get_sock_code_by_name(symbol)



def get_stock_data(code):
    """获取完整股票数据"""
    try:
        url = f"http://qt.gtimg.cn/q={code}"
        response = requests.get(url, timeout=5)
        data = response.text.split('~')
        logging.info(f"[DEBUG] 获取股票数据({code}) -> {data}")
        return {
            'name': data[1],                # 股票名称
            'price': float(data[3]),         # 当前价格
            'close': float(data[4]),         # 昨收价格
            'open': float(data[5]),          # 今开
            'high': float(data[33]),         # 最高
            'low': float(data[34]),          # 最低
            'volume': int(data[6]),          # 成交量（手）
            'turnover': float(data[37]),     # 成交额（万）
            'change_amt': float(data[31]),    # 涨跌额
            'change_percent': float(data[32].strip('%'))  # 涨跌幅
        }
    except Exception as ex:
        print("获取数据失败:", ex)
        return None


def get_stock_quote(symbol: str) -> dict | None:
    """
    通过新浪 HQ 接口获取股票实时行情（稳定可靠版）
    支持：A股(sh/sz)、港股(hk)、美股(gb_)
    示例输入："600519", "贵州茅台", "腾讯", "AAPL"
    """
    code = normalize_code(symbol)
    if not code:
        return None
    try:
        stock_data = get_stock_data(code)
        if not stock_data:
            return None
        return stock_data
    except Exception as ex:
        # 可选：记录日志（不 print，保持干净）
        print(f"[DEBUG] get_stock_quote({symbol}) failed: {ex}")
        return None


def _to_float(x):
    try:
        return float(x) if x and x != "-" else 0.0
    except (ValueError, TypeError):
        return 0.0


def _to_int(x):
    try:
        return int(float(x)) if x and x != "-" else 0
    except (ValueError, TypeError):
        return 0


def get_sn_belong_to(sn: str) -> dict:
    """✅ 获取SN所属通道信息"""
    url = f"http://192.168.1.128:8890/api/v1/sn/query/judgeSnBelong"
    body = {
        "sn": sn
    }
    resp = requests.post(url, json=body)
    return resp.json()


# --- 原有逻辑（天气 & LLM）保持不变 ---
def get_weather(city: str) -> dict:
    try:
        geo = requests.get(
            "https://api.openweathermap.org/geo/1.0/direct",
            params={"q": city, "limit": 1, "appid": OPENWEATHER_API_KEY},
            timeout=5
        ).json()
        if not geo:
            return {"error": f"未找到城市: {city}"}
        lat, lon = geo[0]['lat'], geo[0]['lon']
        weather = requests.get(
            "https://api.openweathermap.org/data/2.5/weather",
            params={
                "lat": lat, "lon": lon,
                "appid": OPENWEATHER_API_KEY,
                "units": "metric",
                "lang": "zh_cn"
            },
            timeout=5
        ).json()
        return {
            "city": city,
            "temperature": round(weather['main']['temp']),
            "description": weather['weather'][0]['description'],
            "humidity": weather['main']['humidity'],
            "success": True
        }
    except Exception as ex:
        return {"error": f"查询失败: {ex}", "success": False}

import dashscope
from dashscope import Generation
dashscope.api_key = DASHSCOPE_API_KEY

TOOLS = [{
    "type": "function",
    "function": {
        "name": "get_weather",
        "description": "查询指定城市的当前天气",
        "parameters": {"type": "object", "properties": {"city": {"type": "string"}}, "required": ["city"]}
    }
},
{
    "type": "function",
    "function": {
        "name": "get_sn_belong_to",
        "description": "查询当前SN属于哪个通道",
        "parameters": {"type": "object", "properties": {"sn": {"type": "string"},"SN": {"type": "string"},"机器": {"type":"string"},"终端": {"type":"string"}}, "required": []}
    }
},
{
    "type": "function",
    "function": {
        "name": "get_stock_quote",
        "description": "根据股票名称或代码获取实时行情（最新价、涨跌幅、成交量等），支持A股、港股、美股。",
        "parameters": {
            "symbol": {
                "type": "string",
                "description": "股票代码（如 '600519'）或名称（如 '贵州茅台'、'腾讯控股'、'AAPL'）"
            }
        }, "required": ["symbol"]}
}]

def weather_mcp_with_llm(user_id: str, user_query: str) -> str:
    # 获取或初始化该用户的对话历史
    if user_id not in user_conversations:
        user_conversations[user_id] = []
    
    # 将当前用户消息添加到对话历史中
    user_conversations[user_id].append({"role": "user", "content": user_query})
    
    # 限制对话历史长度，防止过长
    if len(user_conversations[user_id]) > 10:  # 最多保留10轮对话
        user_conversations[user_id] = user_conversations[user_id][-10:]
    
    messages = user_conversations[user_id][:]
    
    try:
        resp = Generation.call(
            model="qwen-max",
            messages=messages,
            tools=TOOLS,
            tool_choice="auto"
        )
        
        # 处理响应对象，如果是生成器则获取第一个值
        if hasattr(resp, '__iter__') and not hasattr(resp, 'output'):
            try:
                # 使用list()获取生成器的所有值
                resp_list = list(resp)
                if resp_list:
                    resp = resp_list[0]
                else:
                    return "处理出错: 无法获取响应"
            except Exception as ex:
                return f"处理出错: 无法获取响应 - {ex}"
        msg = resp.output.choices[0].message
        if msg.get("tool_calls"):
            
            tool = msg["tool_calls"][0]
            logging.info(f"llm调用🔧 工具调用: {json.dumps(tool, indent=2, ensure_ascii=False)}")
            function_name = tool["function"]["name"]
            args = json.loads(tool["function"]["arguments"])
            llm_res = None
            if function_name == "get_weather":
                city = args.get("city")
                logging.info(f"llm调用🔧 获取天气信息: {city}")
                llm_res = get_weather(city)
            elif function_name == "get_sn_belong_to":
                sn = args.get("sn") or args.get("SN") or args.get("机器") or args.get("终端")
                logging.info(f"llm调用🔧 获取SN所属通道: {sn}")
                llm_res = get_sn_belong_to(sn)
            elif function_name == "get_stock_quote":
                stock_name = args.get("symbol")
                logging.info(f"llm调用🔧 获取股票信息: {stock_name}")
                llm_res = get_stock_quote(stock_name)
            messages.extend([
                msg,
                {"role": "tool", "content": json.dumps(llm_res, ensure_ascii=False), "tool_call_id": tool["id"]}
            ])
            final = Generation.call(model="qwen-max", messages=messages)
            
            # 处理最终响应对象
            if hasattr(final, '__iter__') and not hasattr(final, 'output'):
                try:
                    # 使用list()获取生成器的所有值
                    final_list = list(final)
                    if final_list:
                        final = final_list[0]
                    else:
                        return "处理出错: 无法获取最终响应"
                except Exception as ex:
                    return f"处理出错: 无法获取最终响应 - {ex}"
                
            final_output = getattr(final, 'output', final)
            reply_content = str(final_output.choices[0].message.content) or "已处理。"
        else:
            reply_content = str(msg.content) or "我理解了～"
        
        # 将助手回复也加入对话历史
        user_conversations[user_id].append({"role": "assistant", "content": reply_content})
        
        # 同样限制对话历史长度
        if len(user_conversations[user_id]) > 10:
            user_conversations[user_id] = user_conversations[user_id][-10:]
            
        return reply_content
    except Exception as ex:
        return f"处理出错: {str(ex)[:80]}"

def send_to_dingtalk(title, content, at_user_id):
    timestamp = str(round(time.time() * 1000))
    secret = None

    if secret:
        # 计算签名
        string_to_sign = f"{timestamp}\n{secret}"
        hmac_code = hmac.new(
            secret.encode("utf-8"),
            string_to_sign.encode("utf-8"),
            hashlib.sha256
        ).digest()
        sign = urllib.parse.quote_plus(base64.b64encode(hmac_code))
        webhook_url = f"https://oapi.dingtalk.com/robot/send?access_token=xxx&timestamp={timestamp}&sign={sign}"
    else:
        webhook_url = "https://oapi.dingtalk.com/robot/send?access_token=9d8c6451f7a53cfce758c05aa05eb8ff291edfd8113c704280365909a64b4af5"

    message = {
        "msgtype": "text",
        "title": title,
        "text": {
            "content": f"{content}"
        },
        "at":{
            "atUserIds": [
                at_user_id
            ],
        }
    }

    resp = requests.post(webhook_url, json=message)
    if resp.status_code == 200 and resp.json().get("errcode") == 0:
        print("✅ 钉钉消息发送成功！")
    else:
        print(f"❌ 钉钉发送失败: {resp.text}")


# --- WebSocket 消息处理（修复版）---
def send_reply(conversation_id: str, sender_id: str, content: str,user_id: str):
    if not ws or not getattr(ws, 'sock', None) or not getattr(ws.sock, 'connected', False):
        logging.warning("❌ WebSocket 未连接，无法回复")
        return

    try:
        msg = {
            "header": {
                "eventId": f"reply_{int(time.time()*1000)}",
                #"eventType": "im.message.send",
                #"eventType": "system.send_message",
                "eventType": "robot.interaction"
            },
            "payload": {
                "conversationId": conversation_id,
                "robotCode": APP_KEY, # 使用APP_KEY作为robotCode
                "senderId": sender_id,
                "msgKey": "sampleText",
                "msgParam": json.dumps({
                    "content": content
                }, ensure_ascii=False)
            }
        }

        logging.info(f"📤 发送回复消息: {json.dumps(msg, indent=2, ensure_ascii=False)}")
        #ws.send(json.dumps(msg, ensure_ascii=False))
        send_to_dingtalk(title="机器人回复", content=content, at_user_id=user_id)
        logging.info("✅ 回复消息发送成功")
    except Exception as ex:
        logging.error(f"❌ 发送回复失败: {ex}")

def on_message(ws, message):
    try:
        #logging.info(f"📨 原始消息: {message}")
        data = json.loads(message)
        logging.info(f"📦 完整消息内容: {json.dumps(data, indent=2, ensure_ascii=False)}")
        # 尝试多种可能的消息格式
        topic = None
        payload = {}
        # 格式1: 标准格式（header + payload）
        if "header" in data and "payload" in data:
            topic = data.get("header", {}).get("topic")
            payload = data.get("payload", {})
        # 格式2: 直接包含事件类型
        elif "topic" in data:
            topic = data.get("topic")
            payload = data
        # 格式3: 其他可能格式
        else:
            # 尝试从消息体中查找关键字段
            if "data" in data:
                payload = json.loads(data.get("data", "{}"))
                topic = "/v1.0/im/bot/messages/get"  # 假设是机器人消息
        logging.info(f"🔍 解析后的事件类型: {topic}")

        if not topic:
            logging.warning(f"❓ 无法识别消息格式: {data}")
            return

        if topic == "/v1.0/im/bot/messages/get":
            text = payload["text"]["content"].strip()
            sender_id = payload["senderId"]
            conversation_id = payload["conversationId"]
            at_users = payload.get("atUsers", [])
            bot_id = "$:LWCP_v1:$QbJeQE/U3gG5HCoDz/9KlPIG7HbHOyGL"  # 机器人自己的 dingtalkId
            user_id = payload["senderStaffId"]
            logging.info(f"🤖 机器人ID: {bot_id}")
            logging.info(f"💬 原始消息内容: {text}")
            logging.info(f"👥 @用户列表: {at_users}")
            is_at_me = any(u.get("dingtalkId") == bot_id for u in at_users)
            logging.info(f"🔍 是否@了机器人: {is_at_me}")
            if not is_at_me:
                logging.info("❌ 消息未@机器人，忽略")
                return
            if text.startswith(f"@{bot_id}"):
                text = text[len(f"@{bot_id}"):].strip()
            logging.info(f"🎯 处理后的消息: '{text}'")

            if text:
                logging.info("🔄 开始调用LLM处理...")
                # 传递用户ID以启用连续对话功能
                reply = weather_mcp_with_llm(sender_id, text)
                logging.info(f"📤 准备回复: {reply}")
                send_reply(conversation_id, sender_id, reply,user_id)
            else:
                logging.info("❌ 消息内容为空，忽略")
    except Exception as ex:
        logging.exception(f"💥 处理消息异常: {ex}")

def on_error(ws, error):
    logging.error(f"❌ WebSocket 错误: {error}")

def on_close(ws, close_status_code, close_msg):
    logging.info(f"🔌 WebSocket 连接关闭: {close_status_code} - {close_msg}")

def on_open(ws):
    logging.info("✅ WebSocket 连接成功！正在注册...")
    # 获取token
    token = get_access_token()
    # 发送注册事件（必须！）
    register_msg = {
        "header": {
            "eventType": "system.register",
            "eventId": f"reg_{int(time.time()*1000)}"
        },
        "payload": {
            "appKey": APP_KEY,
            "appSecret": APP_SECRET,
            "scope": "ROBOT",
            "eventTypes": [
                "im.robot.message.receive"
            ],
            "robotCode": APP_KEY,
            "token": token  # 添加token
        }
    }
    logging.info(f"📝 发送注册消息: {json.dumps(register_msg, indent=2)}")
    ws.send(json.dumps(register_msg))
    logging.info("📡 已发送注册事件，等待消息...")

def test_connection():
    """测试连接和认证是否正常"""
    try:
        # 测试获取token
        token = get_access_token()
        logging.info(f"✅ Token获取成功: {token[:20]}...")

        # 测试获取WSS地址
        wss_url = get_stream_connection_url()
        logging.info(f"✅ WSS地址获取成功: {wss_url}")

        # 测试天气API
        weather = get_weather("北京")
        logging.info(f"✅ 天气API测试: {weather}")

        #测试股票信息API
        stock_info = get_stock_quote("601857")
        logging.info(f"✅ 股票信息API测试: {stock_info}")

        # 测试LLM
        # 修改测试调用以符合新函数签名
        llm_test = weather_mcp_with_llm("test_user", "今天天气怎么样？")
        logging.info(f"✅ LLM测试: {llm_test[:50]}...")

        llm_sock_name = weather_mcp_with_llm("test_user", f"中国电信，直接输出股票代码，不要其他任何废话")
        logging.info(f"✅ 股票名称测试: {llm_sock_name}")
        return True
    except Exception as ex:
        logging.error(f"❌ 连接测试失败: {ex}")
        return False


# --- 主程序 ---
if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(levelname)s - %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )

    # 先测试所有连接
    logging.info("🧪 开始连接测试...")
    if not test_connection():
        logging.error("💥 连接测试失败，请检查配置")
        exit(1)

    logging.info("✅ 所有连接测试通过")

    try:
        wss_url = get_stream_connection_url()
        logging.info(f"🔗 获取到 WSS 地址")

        ws = websocket.WebSocketApp(
            wss_url,
            on_open=on_open,
            on_message=on_message,
            on_error=on_error,
            on_close=on_close
        )

        print("🚀 正在连接钉钉 Stream 服务...")
        # 添加重连机制
        ws.run_forever(reconnect=5)  # 5秒重连间隔
    except Exception as e:
        logging.exception(f"💥 启动失败: {e}")