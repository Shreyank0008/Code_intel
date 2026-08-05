"""Flask Notes — a server-rendered app (Jinja templates). Demonstrates template
detection + route parsing on a classic Flask stack."""
from flask import Flask, render_template, request, redirect, jsonify

app = Flask(__name__)
NOTES = [{"id": 1, "title": "Welcome", "body": "This is a demo note."}]


@app.route("/")
def index():
    return render_template("index.html", notes=NOTES)


@app.route("/notes/<int:nid>")
def note_detail(nid):
    note = next((n for n in NOTES if n["id"] == nid), None)
    return render_template("detail.html", note=note)


@app.route("/notes", methods=["POST"])
def create_note():
    NOTES.append({"id": len(NOTES) + 1,
                  "title": request.form.get("title", "Untitled"),
                  "body": request.form.get("body", "")})
    return redirect("/")


@app.route("/api/notes")
def api_notes():
    return jsonify(NOTES)


@app.route("/api/health")
def health():
    return jsonify({"status": "ok"})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=80)
