import socket, threading, time, uvicorn, webview, os
from viewer.app import app

# pick a free port and keep the socket open, so nothing can take it in between
s = socket.socket(); s.bind(("127.0.0.1", 0))
port = s.getsockname()[1]

server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="info"))
threading.Thread(target=server.run, kwargs={"sockets": [s]}, daemon=True).start()

# wait until the port actually accepts connections
for _ in range(100):
    try:
        socket.create_connection(("127.0.0.1", port), timeout=0.3).close()
        break
    except OSError:
        time.sleep(0.1)
else:
    raise SystemExit("Server never started. Read the log above for the error.")

class Api:
    """Methods here are callable from the page as window.pywebview.api.<name>()."""
    def pick_folder(self):
        kind = getattr(getattr(webview, "FileDialog", None), "FOLDER", None) or webview.FOLDER_DIALOG
        result = webview.windows[0].create_file_dialog(kind)
        return result[0] if result else None

url = f"http://127.0.0.1:{port}"
print("Serving on", url)
webview.create_window("Photo Backup", url + "/phone.html", width=1280, height=850, js_api=Api())
webview.start(debug=False)   # debug=True enables right-click → Inspect Element
# window closed: let a running import stop after its current file (never half-way)
from viewer import phone
phone.S.cancel = True
for _ in range(100):                 # wait up to 10 s
    if phone.S.state != "importing":
        break
    time.sleep(0.1)
server.should_exit = True
os._exit(0)
