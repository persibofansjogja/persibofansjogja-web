import os
from flask import Flask, render_template

app = Flask(__name__)

@app.get("/")
def home():
    """Halaman utama Persibo Fans Jogja."""
    return render_template("index.html")

@app.get("/health")
def health():
    """Endpoint sederhana untuk pemeriksaan status aplikasi."""
    return {"status": "ok", "app": "Persibo Fans Jogja"}

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(
        host="0.0.0.0",
        port=port,
        debug=os.environ.get("FLASK_DEBUG") == "1"
    )
