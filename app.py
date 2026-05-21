from flask import Flask, render_template, request, send_file, Response, stream_with_context
import urllib.request
import urllib.parse
import json
import time
import io
import threading

app = Flask(__name__)

_cancel_flags: dict[str, bool] = {}
_cancel_lock = threading.Lock()


class CancelledError(Exception):
    pass


class IAScraper:
    def __init__(self, cancel_check=None):
        self._cancelled = cancel_check or (lambda: False)

    def fetch_url(self, url, retries=5, wait=5.0):
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        for attempt in range(1, retries + 1):
            if self._cancelled():
                raise CancelledError()
            try:
                with urllib.request.urlopen(req, timeout=30) as res:
                    return json.loads(res.read().decode())
            except CancelledError:
                raise
            except Exception as ex:
                if attempt < retries:
                    time.sleep(wait * attempt)
                else:
                    raise

    def resolve_email(self, user_input):
        if "@" in user_input and not user_input.startswith("http"):
            return user_input, False          # (email, was_resolved)
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
            return self._email_from_metadata(path), True   # resolved from metadata
        raise ValueError("Please enter an email address or item URL.")

    def _email_from_metadata(self, identifier):
        data = self.fetch_url(f"https://archive.org/metadata/{identifier}")
        email = data.get("metadata", {}).get("uploader", "")
        if not email or "@" not in email:
            email = data.get("uploader", "")
        if not email or "@" not in email:
            raise ValueError("Could not retrieve uploader email from metadata.")
        return email

    def iter_identifiers(self, email, mediatype=""):
        """
        Yields tuples:
          ("total", n)   — once, from first API response
          ("item", ident) — for each identifier found
        """
        base = "https://archive.org/services/search/v1/scrape"
        query = f"uploader:{email}"
        if mediatype:
            query += f" AND mediatype:{mediatype}"

        seen: set[str] = set()
        cursor = None
        first_batch = True

        while True:
            if self._cancelled():
                return

            params = [
                ("q", query),
                ("fields", "identifier,addeddate"),
                ("sorts", "addeddate asc"),
                ("count", 1000),
            ]
            if cursor:
                params.append(("cursor", cursor))

            data = self.fetch_url(base + "?" + urllib.parse.urlencode(params))

            if first_batch:
                yield ("total", data.get("total", 0))
                first_batch = False

            items = data.get("items", [])
            for item in items:
                ident = item.get("identifier")
                if ident and ident not in seen:
                    seen.add(ident)
                    yield ("item", ident)

            cursor = data.get("cursor")
            if not items or not cursor:
                break
            yield ": keepalive\n\n"
            time.sleep(0.5)


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/stream")
def stream():
    user_input = request.args.get("user_input", "").strip()
    mediatype  = request.args.get("mediatype", "").strip()
    req_id     = request.args.get("req_id", "")

    def sse(msg: str) -> str:
        return f"data: {msg}\n\n"

    def generate():
        with _cancel_lock:
            _cancel_flags[req_id] = False

        def cancelled():
            with _cancel_lock:
                return _cancel_flags.get(req_id, False)

        scraper = IAScraper(cancel_check=cancelled)
        count = 0

        try:
            yield sse("LOG|Resolving uploader...")
            email, was_resolved = scraper.resolve_email(user_input)

            # If input was an item URL, send the resolved email back to update the field
            if was_resolved:
                yield sse(f"EMAIL|{email}")

            yield sse(f"LOG|Uploader: {email}")
            yield sse("LOG|Fetching items...")

            for kind, value in scraper.iter_identifiers(email, mediatype):
                if cancelled():
                    break

                if kind == "total":
                    yield sse(f"TOTAL|{value}")
                elif kind == "item":
                    count += 1
                    details  = f"https://archive.org/details/{value}"
                    compress = f"https://archive.org/compress/{value}"
                    yield sse(f"FOUND|{details}|{compress}")

            if cancelled():
                yield sse(f"CANCELLED|{count}")
            else:
                yield sse(f"COMPLETE|{count}")

        except CancelledError:
            yield sse(f"CANCELLED|{count}")
        except GeneratorExit:
            pass
        except Exception as ex:
            yield sse(f"ERROR|{ex}")
        finally:
            with _cancel_lock:
                _cancel_flags.pop(req_id, None)

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@app.route("/stop", methods=["POST"])
def stop():
    data   = request.get_json(silent=True) or {}
    req_id = data.get("req_id", "")
    with _cancel_lock:
        if req_id in _cancel_flags:
            _cancel_flags[req_id] = True
    return {"ok": True}


@app.route("/download", methods=["POST"])
def download():
    content  = request.form.get("content", "")
    filename = request.form.get("filename", "archive_urls.txt")
    buf = io.BytesIO(content.encode("utf-8"))
    return send_file(buf, as_attachment=True, download_name=filename, mimetype="text/plain")


if __name__ == "__main__":
    app.run(debug=True)
