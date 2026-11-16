import os
from flask import Flask, Response, render_template

app = Flask(__name__)

@app.get("/")
def home():
    """Halaman utama Persibo Fans Jogja."""
    return render_template("index.html")

@app.get("/robots.txt")
def robots():
    """Instruksi crawler mesin pencari."""
    body = """User-agent: *
Allow: /

Sitemap: https://www.persibofansjogja.web.id/sitemap.xml
"""
    return Response(body, mimetype="text/plain")


@app.get("/sitemap.xml")
def sitemap():
    """Sitemap XML untuk halaman publik."""
    body = """<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url>
    <loc>https://www.persibofansjogja.web.id/</loc>
    <changefreq>monthly</changefreq>
    <priority>1.0</priority>
  </url>
</urlset>
"""
    return Response(body, mimetype="application/xml")


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
