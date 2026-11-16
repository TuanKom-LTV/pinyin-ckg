import json
import time
import websocket
import urllib.request

URL = "http://127.0.0.1:9222/json"
tabs = json.load(urllib.request.urlopen(URL))

page = next(
    (tab for tab in tabs if tab.get("type") == "page"
     and "127.0.0.1:8000" in tab.get("url", "")),
    None
)

if not page:
    print("KHONG TIM THAY TAB PINYIN")
    for tab in tabs:
        print(tab.get("type"), tab.get("url"))
    raise SystemExit(1)

print("TAB:", page["url"])

ws = websocket.create_connection(
    page["webSocketDebuggerUrl"],
    timeout=10,
    suppress_origin=True
)

ws.send(json.dumps({
    "id": 1,
    "method": "Runtime.enable"
}))

ws.send(json.dumps({
    "id": 2,
    "method": "Log.enable"
}))

ws.send(json.dumps({
    "id": 3,
    "method": "Page.enable"
}))

print("\nDANG DOC LOI TRINH DUYET...")

deadline = time.time() + 8

while time.time() < deadline:
    try:
        message = json.loads(ws.recv())
    except Exception:
        break

    method = message.get("method", "")
    params = message.get("params", {})

    if method == "Runtime.exceptionThrown":
        details = params.get("exceptionDetails", {})
        print("\nLOI JAVASCRIPT:")
        print(details.get("text"))
        print(details.get("url"))
        print(details.get("lineNumber"))

    elif method == "Log.entryAdded":
        entry = params.get("entry", {})
        if entry.get("level") == "error":
            print("\nLOI TRINH DUYET:")
            print(entry.get("text"))
            print(entry.get("url"))
            print(entry.get("lineNumber"))

ws.close()
print("\nHOAN TAT KIEM TRA")
