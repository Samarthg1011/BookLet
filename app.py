"""
BooKlet — Flask application.

Serves the Gallery UI and a small JSON API over the hybrid book engine.

Run:
    python prepare_data.py   (first time only)
    python app.py            (dev)  or  python serve.py  (waitress, prod)
"""

from flask import Flask, jsonify, render_template, request

from engine import Engine

app = Flask(__name__)
engine = Engine()

ERAS = ["classics", "20th", "contemporary"]


def clean_ratings(raw, limit=500):
    """Accept {id: stars}; silently drop anything malformed instead of 500-ing."""
    out = {}
    if not isinstance(raw, dict):
        return out
    for k, v in list(raw.items())[:limit]:
        try:
            out[int(k)] = min(5.0, max(0.5, float(v)))
        except (TypeError, ValueError):
            continue
    return out


def clean_n(raw, default=24, hi=60):
    try:
        return max(1, min(int(raw), hi))
    except (TypeError, ValueError):
        return default


@app.get("/healthz")
def healthz():
    return jsonify({"ok": True})


@app.get("/")
def index():
    return render_template("index.html")


@app.get("/api/init")
def api_init():
    return jsonify({
        "meta": engine.meta,
        "shelves": engine.meta["shelves"],
        "eras": ERAS,
        "onboarding": engine.onboarding(36),
        "popular": engine.popular(16),
    })


@app.post("/api/recommend")
def api_recommend():
    body = request.get_json(silent=True)
    body = body if isinstance(body, dict) else {}
    out = engine.recommend(
        clean_ratings(body.get("ratings")),
        n=clean_n(body.get("n")),
        shelf=body.get("shelf") or None,
        era=body.get("era") or None,
    )
    return jsonify(out)


@app.get("/api/similar/<int:book_id>")
def api_similar(book_id):
    return jsonify({"results": engine.similar(book_id)})


@app.get("/api/search")
def api_search():
    return jsonify({"results": engine.search(request.args.get("q", ""))})


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5211, debug=False)
