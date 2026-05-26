import requests

PROXIES = "http://127.0.0.1:7890"
# 如果代理需要认证，请这样写：
# PROXIES = "http://username:password@127.0.0.1:7890"

test_url = "https://s.jina.ai"

try:
    print(f"尝试通过代理 {PROXIES} 访问 {test_url} ...")
    response = requests.get(test_url, proxies={"https": PROXIES}, timeout=20)
    response.raise_for_status() # 检查HTTP错误状态
    print("Requests 成功连接！")
    print(f"Status Code: {response.status_code}")
    print(f"Response (first 500 chars): {response.text[:500]}")
except requests.exceptions.ProxyError as e:
    print(f"Requests 代理连接错误: {e}")
except requests.exceptions.ConnectionError as e:
    print(f"Requests 连接错误 (可能是代理无法到达目标): {e}")
except requests.exceptions.Timeout as e:
    print(f"Requests 请求超时: {e}")
except requests.exceptions.RequestException as e:
    print(f"Requests 发生未知错误: {e}")