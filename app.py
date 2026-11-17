import os
import secrets
import time
from flask import Flask, request, redirect, url_for, send_from_directory, render_template_string, session

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY") or secrets.token_hex(16)
# 登录密码通过环境变量 FILE_PASSWORD 设置，不要写进代码/仓库里
PASSWORD = os.environ.get("FILE_PASSWORD") or secrets.token_hex(8)
UPLOAD_DIR = os.environ.get("UPLOAD_DIR", "uploads")
MAX_MB = int(os.environ.get("MAX_MB", "50"))
app.config["MAX_CONTENT_LENGTH"] = MAX_MB * 1024 * 1024
CHUNK_SIZE = 512 * 1024  # 每块 512KB，低于网关约 1MB 的限制
CHUNK_TOTAL_LIMIT = 20 * 1024 * 1024  # 分块传总上限 20MB
os.makedirs(UPLOAD_DIR, exist_ok=True)

KEEP_SECONDS = 24 * 3600  # 文件只保留 24 小时，临时中转


def safe_name(name):
    name = (name or "").replace("\\", "/").split("/")[-1].strip()
    if not name or name in (".", ".."):
        return "file"
    return name


def cleanup():
    now = time.time()
    try:
        for name in os.listdir(UPLOAD_DIR):
            path = os.path.join(UPLOAD_DIR, name)
            if os.path.isfile(path) and now - os.path.getmtime(path) > KEEP_SECONDS:
                os.remove(path)
    except Exception:
        pass


def human_size(n):
    if n < 1024:
        return "%d B" % n
    if n < 1024 * 1024:
        return "%.1f KB" % (n / 1024)
    return "%.1f MB" % (n / 1024 / 1024)


LOGIN_PAGE = """<!doctype html>
<html lang="zh-CN">
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>文件中转站</title>
<style>body{font-family:sans-serif;max-width:480px;margin:80px auto;padding:0 16px;text-align:center}input{padding:10px;font-size:16px;width:70%}button{padding:10px 18px;font-size:16px}</style>
</head>
<body>
<h1>文件中转站</h1>
<p>输入密码进入</p>
<form action="/login" method="post">
<input type="password" name="password" placeholder="密码" required autofocus>
<button type="submit">进入</button>
</form>
</body>
</html>"""

MAIN_PAGE = """<!doctype html>
<html lang="zh-CN">
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>文件中转站</title>
<style>body{font-family:sans-serif;max-width:640px;margin:40px auto;padding:0 16px}li{margin:8px 0}button{cursor:pointer}</style>
</head>
<body>
<h1>文件中转站</h1>
<p>临时传个小文件，24 小时后自动清理。1MB 以内直接传；大一点的文件（最长 20MB，比如手机照片）会自动分块传，不用你手动拆。</p>
<form id="upform" action="/upload" method="post" enctype="multipart/form-data">
<input type="file" name="file" id="fileinput" required>
<button type="submit">上传</button>
</form>
<p id="upmsg" style="color:#555"></p>
<script>
document.getElementById("upform").addEventListener("submit", async function(e){
  var f = document.getElementById("fileinput").files[0];
  if (!f) return;
  if (f.size > 20*1024*1024) { alert("文件超过 20MB，传不了"); e.preventDefault(); return; }
  if (f.size <= 1024*1024) return; // 小文件走原来的直接上传
  e.preventDefault();
  var msg = document.getElementById("upmsg");
  var uid = Date.now() + "-" + Math.random().toString(36).slice(2,8);
  var total = Math.ceil(f.size / (512*1024));
  for (var i = 0; i < total; i++) {
    msg.textContent = "正在传第 " + (i+1) + "/" + total + " 块…";
    var fd = new FormData();
    fd.append("chunk", f.slice(i*512*1024, (i+1)*512*1024));
    fd.append("filename", f.name);
    fd.append("upload_id", uid);
    fd.append("chunk_index", i);
    fd.append("total_chunks", total);
    var r = await fetch("/upload_chunk", {method: "POST", body: fd});
    if (!r.ok) { msg.textContent = "第 " + (i+1) + " 块传失败了，刷新页面再试一次"; return; }
  }
  msg.textContent = "传完了，正在合并…";
  location.reload();
});
</script>
<h2>已上传的文件</h2>
{% if files %}
<ul>
{% for name, size in files %}
<li><a href="/download/{{ name|urlencode }}">{{ name|e }}</a>（{{ size }}）
<form action="/delete/{{ name|urlencode }}" method="post" style="display:inline"><button type="submit">删除</button></form></li>
{% endfor %}
</ul>
{% else %}
<p>还没有文件。</p>
{% endif %}
<p><a href="/logout">退出登录</a></p>
</body>
</html>"""


@app.route("/")
def index():
    if not session.get("ok"):
        return render_template_string(LOGIN_PAGE)
    cleanup()
    files = []
    for name in sorted(os.listdir(UPLOAD_DIR), key=lambda n: os.path.getmtime(os.path.join(UPLOAD_DIR, n)), reverse=True):
        path = os.path.join(UPLOAD_DIR, name)
        if os.path.isfile(path):
            files.append((name, human_size(os.path.getsize(path))))
    return render_template_string(MAIN_PAGE, files=files, max_mb=MAX_MB)


@app.route("/login", methods=["POST"])
def login():
    if request.form.get("password") == PASSWORD:
        session["ok"] = True
    return redirect(url_for("index"))


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("index"))


@app.route("/upload", methods=["POST"])
def upload():
    if not session.get("ok"):
        return redirect(url_for("index"))
    f = request.files.get("file")
    if f and f.filename:
        name = safe_name(f.filename)
        f.save(os.path.join(UPLOAD_DIR, name))
    return redirect(url_for("index"))


@app.route("/upload_chunk", methods=["POST"])
def upload_chunk():
    if not session.get("ok"):
        return "login required", 403
    f = request.files.get("chunk")
    uid = (request.form.get("upload_id") or "").replace("/", "").replace("\\", "")
    name = safe_name(request.form.get("filename"))
    try:
        idx = int(request.form.get("chunk_index", "0"))
        total = int(request.form.get("total_chunks", "1"))
    except ValueError:
        return "bad index", 400
    if not f or not uid or total < 1 or idx < 0 or idx >= total:
        return "bad request", 400
    chunk_dir = os.path.join(UPLOAD_DIR, ".chunks", uid)
    os.makedirs(chunk_dir, exist_ok=True)
    f.save(os.path.join(chunk_dir, "%d.part" % idx))
    if len(os.listdir(chunk_dir)) == total:
        # 最后一块到了，按顺序拼成完整文件
        dest = os.path.join(UPLOAD_DIR, name)
        with open(dest, "wb") as out:
            for i in range(total):
                with open(os.path.join(chunk_dir, "%d.part" % i), "rb") as part:
                    out.write(part.read())
        for i in range(total):
            os.remove(os.path.join(chunk_dir, "%d.part" % i))
        os.rmdir(chunk_dir)
    return "ok"


@app.route("/download/<path:name>")
def download(name):
    if not session.get("ok"):
        return redirect(url_for("index"))
    return send_from_directory(UPLOAD_DIR, safe_name(name), as_attachment=True)


@app.route("/delete/<path:name>", methods=["POST"])
def delete(name):
    if not session.get("ok"):
        return redirect(url_for("index"))
    path = os.path.join(UPLOAD_DIR, safe_name(name))
    if os.path.isfile(path):
        os.remove(path)
    return redirect(url_for("index"))


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8080"))
    app.run(host="0.0.0.0", port=port)
