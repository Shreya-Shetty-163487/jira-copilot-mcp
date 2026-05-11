import json
import os
import uuid
from datetime import datetime
from flask import Flask, render_template, request, redirect, url_for, abort

app = Flask(__name__)

DB_FILE = "db.json"


def load_db():
    if not os.path.exists(DB_FILE):
        with open(DB_FILE, "w") as f:
            json.dump({"posts": []}, f)
    with open(DB_FILE, "r") as f:
        return json.load(f)


def save_db(data):
    with open(DB_FILE, "w") as f:
        json.dump(data, f, indent=2)


@app.route("/")
def index():
    db = load_db()
    posts = sorted(db["posts"], key=lambda p: p["created_at"], reverse=True)
    return render_template("index.html", posts=posts)


@app.route("/post/<post_id>")
def view_post(post_id):
    db = load_db()
    post = next((p for p in db["posts"] if p["id"] == post_id), None)
    if post is None:
        abort(404)
    return render_template("view.html", post=post)


@app.route("/create", methods=["GET", "POST"])
def create_post():
    if request.method == "POST":
        title = request.form.get("title", "").strip()
        author = request.form.get("author", "").strip()
        content = request.form.get("content", "").strip()

        if not title or not content or not author:
            return render_template("create.html", error="All fields are required.")

        db = load_db()
        new_post = {
            "id": str(uuid.uuid4()),
            "title": title,
            "author": author,
            "content": content,
            "created_at": datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"),
            "updated_at": None,
            "comments": [],
        }
        db["posts"].append(new_post)
        save_db(db)
        return redirect(url_for("view_post", post_id=new_post["id"]))

    return render_template("create.html")


@app.route("/edit/<post_id>", methods=["GET", "POST"])
def edit_post(post_id):
    db = load_db()
    post = next((p for p in db["posts"] if p["id"] == post_id), None)
    if post is None:
        abort(404)

    if request.method == "POST":
        title = request.form.get("title", "").strip()
        author = request.form.get("author", "").strip()
        content = request.form.get("content", "").strip()

        if not title or not content or not author:
            return render_template("edit.html", post=post, error="All fields are required.")

        post["title"] = title
        post["author"] = author
        post["content"] = content
        post["updated_at"] = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
        save_db(db)
        return redirect(url_for("view_post", post_id=post_id))

    return render_template("edit.html", post=post)


@app.route("/delete/<post_id>", methods=["POST"])
def delete_post(post_id):
    db = load_db()
    post = next((p for p in db["posts"] if p["id"] == post_id), None)
    if post is None:
        abort(404)
    db["posts"] = [p for p in db["posts"] if p["id"] != post_id]
    save_db(db)
    return redirect(url_for("index"))


@app.route("/post/<post_id>/comment", methods=["POST"])
def add_comment(post_id):
    db = load_db()
    post = next((p for p in db["posts"] if p["id"] == post_id), None)
    if post is None:
        abort(404)

    author = request.form.get("author", "").strip()
    body = request.form.get("body", "").strip()

    if not author or not body:
        return redirect(url_for("view_post", post_id=post_id))

    comment = {
        "id": str(uuid.uuid4()),
        "author": author,
        "body": body,
        "created_at": datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"),
    }

    if "comments" not in post:
        post["comments"] = []
    post["comments"].append(comment)
    save_db(db)
    return redirect(url_for("view_post", post_id=post_id))


if __name__ == "__main__":
    app.run(debug=True)
