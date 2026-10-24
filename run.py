"""Local Auto Trader entry point. Credentials are entered in the browser UI."""
import uvicorn

if __name__ == "__main__":
    import sys
    import threading
    import webbrowser
    if "--open-browser" in sys.argv:
        opener = threading.Timer(1.5, lambda: webbrowser.open("http://127.0.0.1:8000"))
        opener.daemon = True
        opener.start()
    uvicorn.run("app.main:app", host="127.0.0.1", port=8000, access_log=False)
