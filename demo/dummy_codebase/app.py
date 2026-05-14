"""Minimal Flask API — intentionally incomplete for demo purposes."""

from flask import Flask, jsonify, request, abort

app = Flask(__name__)

# In-memory store
_items = {}
_next_id = 1


@app.route("/items", methods=["GET"])
def get_items():
    return jsonify(list(_items.values()))


@app.route("/items/<int:item_id>", methods=["GET"])
def get_item(item_id):
    item = _items.get(item_id)
    if not item:
        abort(404)
    return jsonify(item)


@app.route("/items", methods=["POST"])
def create_item():
    global _next_id
    data = request.get_json(force=True)
    if not data or "name" not in data:
        abort(400)
    item = {"id": _next_id, "name": data["name"]}
    _items[_next_id] = item
    _next_id += 1
    return jsonify(item), 201


# TODO: add GET /health endpoint (see DEMO-10)


if __name__ == "__main__":
    app.run(debug=True)
