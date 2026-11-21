"""HTTP dashboard, file API, and an OPDS 1.2 acquisition feed."""
from __future__ import annotations

import hmac
import mimetypes
from datetime import datetime, timezone
from xml.etree import ElementTree as ET

from flask import Flask, Response, jsonify, render_template, request, send_file, url_for

from .storage import Library
from .wireless import WirelessService

ATOM = "http://www.w3.org/2005/Atom"
OPENSEARCH = "http://a9.com/-/spec/opensearch/1.1/"
ET.register_namespace("", ATOM)
FEED_TYPE = "application/atom+xml;profile=opds-catalog;kind=acquisition"
MIME_TYPES = {
    "epub": "application/epub+zip", "pdf": "application/pdf", "cbz": "application/vnd.comicbook+zip",
    "cbr": "application/vnd.comicbook-rar", "mobi": "application/x-mobipocket-ebook",
    "azw": "application/vnd.amazon.ebook", "azw3": "application/vnd.amazon.ebook",
    "fb2": "application/x-fictionbook+xml", "djvu": "image/vnd.djvu", "txt": "text/plain",
}


def file_mime(file: dict) -> str:
    return MIME_TYPES.get(file["extension"]) or mimetypes.guess_type(file["filename"])[0] or "application/octet-stream"


def create_app(library: Library, wireless: WirelessService, http_port: int = 8000,
               http_user: str = "leafline", http_password: str = "") -> Flask:
    app = Flask(__name__, template_folder="../templates", static_folder="../static")
    app.config.update(JSON_SORT_KEYS=False)
    app.extensions["library"] = library
    app.extensions["wireless"] = wireless

    @app.before_request
    def protect_local_server():
        if http_password:
            auth = request.authorization
            valid_user = bool(auth and hmac.compare_digest((auth.username or "").encode(), http_user.encode()))
            valid_password = bool(auth and hmac.compare_digest((auth.password or "").encode(), http_password.encode()))
            if not (valid_user and valid_password):
                return Response("Authentication required", 401, {"WWW-Authenticate": 'Basic realm="Leafline", charset="UTF-8"'})
        # A cross-origin HTML form cannot set this header. No CORS is enabled.
        if request.path.startswith("/api/") and request.method in {"POST", "PATCH", "PUT", "DELETE"}:
            if request.headers.get("X-Requested-With") != "XMLHttpRequest":
                return jsonify(error="This action must be requested from the Leafline app."), 403

    @app.after_request
    def response_headers(response):
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "same-origin"
        if request.path.startswith("/api/") or request.path.startswith("/opds"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/")
    def index():
        return render_template("index.html")

    @app.get("/api/status")
    def status():
        files = library.list_files()
        ws = wireless.status()
        return jsonify({"library_name": library.setting("library_name"), "library_uuid": library.setting("library_uuid"),
                        "file_count": len(files), "total_bytes": sum(f["size"] for f in files),
                        "device_count": len(wireless.devices()), "wireless": ws,
                        "http_port": http_port, "http_url": f"http://{ws['host']}:{http_port}",
                        "opds_url": f"http://{ws['host']}:{http_port}/opds",
                        "http_password_enabled": bool(http_password), "data_directory": str(library.root)})

    @app.get("/api/files")
    def list_files():
        return jsonify(files=library.list_files())

    @app.post("/api/files")
    def add_files():
        uploads = request.files.getlist("files")
        if not uploads:
            return jsonify(error="Choose at least one file to add."), 400
        added, errors = [], []
        for upload in uploads:
            try:
                added.append(library.add_file(upload.stream, upload.filename or ""))
            except (ValueError, OSError) as exc:
                app.logger.warning("File upload failed: %s", exc)
                errors.append({"filename": (upload.filename or "Unnamed file")[:250],
                               "message": str(exc) if isinstance(exc, ValueError) else "The file could not be saved."})
        if not added:
            return jsonify(error="No files could be added.", errors=errors), 400
        return jsonify(files=added, errors=errors), 201

    @app.patch("/api/files/<file_id>")
    def edit_file(file_id):
        data = request.get_json(silent=True) or {}
        if not isinstance(data, dict) or not isinstance(data.get("title"), str) or not isinstance(data.get("author", ""), str):
            return jsonify(error="A title and author are required."), 400
        try:
            file = library.update_file(file_id, data["title"], data.get("author", ""))
        except ValueError as exc:
            return jsonify(error=str(exc)), 400
        if not file:
            return jsonify(error="File not found."), 404
        return jsonify(file=file)

    @app.delete("/api/files/<file_id>")
    def delete_file(file_id):
        if not library.delete_file(file_id):
            return jsonify(error="File not found."), 404
        return jsonify(ok=True)

    @app.get("/get/<file_id>/<path:filename>")
    def download_file(file_id, filename):
        # The filename in the URL helps OPDS readers infer format; the ID is authoritative.
        file = library.get_file(file_id)
        if not file:
            return jsonify(error="File not found."), 404
        path = library.file_path(file_id)
        if not path.is_file():
            return jsonify(error="The stored file is missing."), 404
        return send_file(path, mimetype=file_mime(file), as_attachment=True,
                         download_name=file["filename"], conditional=True, etag=True)

    @app.get("/api/files/<file_id>/cover")
    def get_cover(file_id):
        cover = library.cover_for(file_id)
        if not cover:
            return "", 404
        path, mime = cover
        return send_file(path, mimetype=mime, conditional=True, max_age=3600)

    @app.get("/api/devices")
    def devices():
        return jsonify(devices=wireless.devices())

    @app.post("/api/devices/<device_id>/disconnect")
    def disconnect_device(device_id):
        device = wireless.device(device_id)
        if not device:
            return jsonify(error="That reader is no longer connected."), 404
        device.disconnect()
        return jsonify(ok=True)

    @app.route("/api/transfers", methods=["GET", "POST"])
    def transfers():
        if request.method == "GET":
            return jsonify(transfers=library.transfers())
        data = request.get_json(silent=True) or {}
        if not isinstance(data, dict) or not isinstance(data.get("device_id"), str) or not isinstance(data.get("file_ids"), list):
            return jsonify(error="Choose a reader and at least one file."), 400
        file_ids = data["file_ids"]
        if not file_ids or len(file_ids) > 100 or any(not isinstance(item, str) for item in file_ids):
            return jsonify(error="Choose between 1 and 100 files."), 400
        device = wireless.device(data["device_id"])
        if not device:
            return jsonify(error="That reader is no longer connected."), 404
        queued, rejected = [], []
        for file_id in dict.fromkeys(file_ids):
            file = library.get_file(file_id)
            if not file:
                rejected.append({"id": file_id, "filename": "Unknown file", "message": "File not found."})
                continue
            reason = device.accepts(file)
            if reason:
                rejected.append({"id": file_id, "filename": file["filename"], "message": reason})
                continue
            try:
                queued.append(device.submit(file))
            except ConnectionError as exc:
                rejected.append({"id": file_id, "filename": file["filename"], "message": str(exc)})
        if not queued:
            return jsonify(error="None of the selected files could be queued.", rejected=rejected), 400
        return jsonify(queued=queued, rejected=rejected), 202

    @app.get("/api/activity")
    def activity():
        return jsonify(events=library.events(), transfers=library.transfers())

    @app.put("/api/settings")
    def settings():
        data = request.get_json(silent=True) or {}
        if not isinstance(data, dict) or not isinstance(data.get("library_name"), str):
            return jsonify(error="Enter a library name."), 400
        try:
            name = library.set_library_name(data["library_name"])
        except ValueError as exc:
            return jsonify(error=str(exc)), 400
        return jsonify(library_name=name)

    @app.get("/opds")
    @app.get("/opds/")
    def opds():
        query = request.args.get("q", "").strip().casefold()
        files = library.list_files()
        if query:
            files = [f for f in files if query in " ".join((f["title"], f["author"], f["filename"])).casefold()]
        feed = ET.Element(f"{{{ATOM}}}feed")
        ET.SubElement(feed, f"{{{ATOM}}}id").text = "urn:uuid:" + library.setting("library_uuid")
        ET.SubElement(feed, f"{{{ATOM}}}title").text = library.setting("library_name")
        ET.SubElement(feed, f"{{{ATOM}}}updated").text = max((f["updated_at"] for f in files), default=datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"))
        author = ET.SubElement(feed, f"{{{ATOM}}}author")
        ET.SubElement(author, f"{{{ATOM}}}name").text = "Leafline"
        ET.SubElement(feed, f"{{{ATOM}}}link", {"rel": "start", "href": url_for("opds"), "type": FEED_TYPE})
        ET.SubElement(feed, f"{{{ATOM}}}link", {"rel": "self", "href": request.full_path.rstrip("?"), "type": FEED_TYPE})
        ET.SubElement(feed, f"{{{ATOM}}}link", {"rel": "search", "href": url_for("opensearch"), "type": "application/opensearchdescription+xml"})
        ET.SubElement(feed, f"{{{ATOM}}}link", {"rel": "search", "href": url_for("opds") + "?q={searchTerms}", "type": FEED_TYPE})
        for file in files:
            entry = ET.SubElement(feed, f"{{{ATOM}}}entry")
            ET.SubElement(entry, f"{{{ATOM}}}id").text = "urn:uuid:" + file["id"]
            ET.SubElement(entry, f"{{{ATOM}}}title").text = file["title"]
            ET.SubElement(entry, f"{{{ATOM}}}updated").text = file["updated_at"]
            entry_author = ET.SubElement(entry, f"{{{ATOM}}}author")
            ET.SubElement(entry_author, f"{{{ATOM}}}name").text = file["author"] or "Unknown"
            ET.SubElement(entry, f"{{{ATOM}}}link", {
                "rel": "http://opds-spec.org/acquisition", "type": file_mime(file),
                "href": url_for("download_file", file_id=file["id"], filename=file["filename"]),
                "length": str(file["size"]), "title": (file["extension"] or "file").upper(),
            })
            if file["has_cover"]:
                cover = library.cover_for(file["id"])
                if cover:
                    for rel in ("http://opds-spec.org/image", "http://opds-spec.org/image/thumbnail"):
                        ET.SubElement(entry, f"{{{ATOM}}}link", {"rel": rel, "type": cover[1], "href": url_for("get_cover", file_id=file["id"])})
        return Response(ET.tostring(feed, encoding="utf-8", xml_declaration=True), content_type=FEED_TYPE + ";charset=utf-8")

    @app.get("/opds/search.xml")
    def opensearch():
        root = ET.Element("OpenSearchDescription", xmlns=OPENSEARCH)
        ET.SubElement(root, "ShortName").text = "Leafline"
        ET.SubElement(root, "Description").text = "Search your library"
        ET.SubElement(root, "Url", {"type": FEED_TYPE, "template": url_for("opds") + "?q={searchTerms}"})
        return Response(ET.tostring(root, encoding="utf-8", xml_declaration=True), content_type="application/opensearchdescription+xml; charset=utf-8")

    return app
