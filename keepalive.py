import os
import threading
from flask import Flask

app = Flask(__name__)


@app.route("/")
def home():
    return "Bellion is awake."


@app.route("/health")
def health():
    return {"status": "ok"}


def run():
    port = int(os.environ.get("PORT", "5000"))
    app.run(host="0.0.0.0", port=port)


def start():
    t = threading.Thread(target=run, daemon=True)
    t.start()
