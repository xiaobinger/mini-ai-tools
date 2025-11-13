import dashscope
from dashscope import Generation
from typing import Dict, Any
import requests
import json

# === 配置 ===
DASHSCOPE_API_KEY = "sk-b7c98253f43c4803a279231695584967"  # ←← 替换为你自己的
OPENWEATHER_API_KEY = "10d32dcb141261a308068218d8125dcb"  # ←← 同样需要

dashscope.api_key = DASHSCOPE_API_KEY

# === 工具函数：获取天气 ===
def get_weather(city: str) -> Dict[str, Any]:
    """真实天气查询函数，供 LLM 调用"""
    # 1. 获取经纬度
    geo_url = "http://api.openweathermap.org/geo/1.0/direct"
    geo_params = {"q": city, "limit": 1, "appid": OPENWEATHER_API_KEY}
    try:
        geo_resp = requests.get(geo_url, params=geo_params, timeout=5)
        if not geo_resp.json():
            return {"error": f"未找到城市: {city}"}
        lat, lon = geo_resp.json()[0]['lat'], geo_resp.json()[0]['lon']
        
        # 2. 获取天气
        weather_url = "https://api.openweathermap.org/data/2.5/weather"
        weather_params = {
            "lat": lat,
            "lon": lon,
            "appid": OPENWEATHER_API_KEY,
            "units": "metric",
            "lang": "zh_cn"
        }
        weather_resp = requests.get(weather_url, params=weather_params, timeout=5)
        data = weather_resp.json()
        return {
            "city": city,
            "temperature": round(data['main']['temp']),
            "description": data['weather'][0]['description'],
            "humidity": data['main']['humidity']
        }
    except Exception as e:
        return {"error": f"查询失败: {str(e)}"}

# === 定义工具（Function Schema）===
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "get_weather",
            "description": "查询指定城市的当前天气情况",
            "parameters": {
                "type": "object",
                "properties": {
                    "city": {
                        "type": "string",
                        "description": "城市名称，例如：深圳、北京、上海"
                    }
                },
                "required": ["city"]
            }
        }
    }
]

# === MCP 主逻辑 ===
def weather_mcp_with_llm(user_query: str) -> str:
    """使用大模型 + 工具调用实现天气查询"""
    messages = [{"role": "user", "content": user_query}]
    
    # 第一步：让 LLM 决定是否调用工具
    response = Generation.call(
        model="qwen-max",  # 或 qwen-plus, qwen-turbo
        messages=messages,
        tools=TOOLS,
        tool_choice="auto"
    )
    
    # 检查是否需要调用函数
    if response.output.choices[0].message.tool_calls:
        tool_call = response.output.choices[0].message.tool_calls[0]
        function_name = tool_call["function"]["name"]
        arguments = json.loads(tool_call["function"]["arguments"])  # 注意：生产环境建议用 json.loads
        
        if function_name == "get_weather":
            city = arguments.get("city")
            weather_result = get_weather(city)
            
            # 把函数结果返回给 LLM，让它生成自然语言回答
            messages.append(response.output.choices[0].message)
            messages.append({
                "role": "tool",
                "content": str(weather_result),
                "tool_call_id": tool_call["id"]
            })
            
            final_response = Generation.call(
                model="qwen-max",
                messages=messages
            )
            return final_response.output.choices[0].message.content
    else:
        # 不需要调用工具，直接回答
        return response.output.choices[0].message.content

# === 交互测试 ===
if __name__ == "__main__":
    print("🌤️ 智能天气助手（基于通义千问 + Function Calling）")
    print("示例：'深圳今天天气如何？'、'北京冷吗？'、'帮我查下上海的天气'")
    print("输入 '退出' 结束\n")
    while True:
        query = input("你: ").strip()
        if query in ["退出", "quit", "exit","退下"]:
            break
        if not query:
            continue
        
        try:
            answer = weather_mcp_with_llm(query)
            print(f"助手: {answer}\n")
        except Exception as e:
            testRes = Generation.call(
                model="qwen-max",
                messages=[{"role": "user", "content": f"{query}"}]
            )
            print(f"助手: {testRes.output.text}\n")