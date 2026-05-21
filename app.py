from flask import Flask, render_template, request, send_file, jsonify
import urllib.request
import urllib.parse
import json
import time
import io

app = Flask(__name__)


class IAScraper:
    def fetch_url(self, url, retries=5, wait=5.0):
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        for attempt in range(1, retries + 1):
            try:
                with urllib.request.urlopen(req, timeout=30) as res:
                    return json.loads(res.read().decode())
            except Exception:
                if attempt < retries:
                    time.sleep(wait * attempt)
                else:
                    raise

    def resolve_email(self, user_input):
        if "@" in user_input and not user_input.startswith("http"):
            return user_input, False
        if user_input.startswith("https://archive.org/details/"):
            path = (
                user_input
                .replace("https://archive.org/details/", "")
                .split("?")[0]
                .rstrip("/")
            )
            if path.startswith("@"):
                raise ValueError(
                    "User profile URLs (@) are not supported. "
                    "Please enter an email address or item URL."
                )
            return self._email_from_metadata(path), True
        raise ValueError("Please enter an email address or item URL.")

    def _email_from_metadata(self, identifier):
        data = self.fetch_url(f"https://archive.org/metadata/{identifier}")
        email = data.get("metadata", {}).get("uploader", "")
        if not email or "@" not in email:
            email = data.get("uploader", "")
        if not email or "@" not in email:
            raise ValueError("Could not retrieve uploader email from metadata.")
        return email

    def fetch_batch(self, email, mediatype="", cursor=None):
        base = "https://archive.org/services/search/v1/scrape"
        query = f"uploader:{email}"
        if mediatype:
            query += f" AND mediatype:{mediatype}"

        params = [
            ("q", query),
            ("fields", "identifier,addeddate"),
            ("sorts", "addeddate asc"),
            ("count", 1000),
        ]
        if cursor:
            params.append(("cursor", cursor))

        data = self.fetch_url(base + "?" + urllib.parse.urlencode(params))

        items = [item.get("identifier") for item in data.get("items", []) if item.get("identifier")]
        return {
            "items": items,
            "cursor": data.get("cursor"),
            "total": data.get("total", 0),
            "has_more": bool(data.get("cursor") and data.get("items"))
        }


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/batch")
def batch():
    user_input = request.args.get("user_input", "").strip()
    mediatype  = request.args.get("mediatype", "").strip()
    cursor     = request.args.get("cursor", "").strip() or None

    scraper = IAScraper()
    email, _ = scraper.resolve_email(user_input)

    result = scraper.fetch_batch(email, mediatype, cursor)
    result["email"] = email
    return jsonify(result)


@app.route("/download", methods=["POST"])
def download():
    content  = request.form.get("content", "")
    filename = request.form.get("filename", "archive_urls.txt")
    buf = io.BytesIO(content.encode("utf-8"))
    return send_file(buf, as_attachment=True, download_name=filename, mimetype="text/plain")


if __name__ == "__main__":
    app.run(debug=True)