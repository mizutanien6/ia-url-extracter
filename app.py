from gevent import monkey
monkey.patch_all()

from flask import Flask, render_template, request, jsonify
import urllib.request
import urllib.parse
import json
import time
import xml.etree.ElementTree as ET

app = Flask(__name__)

class IAScraper:
    def _open(self, url, retries=5, wait=5.0):
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        for attempt in range(1, retries + 1):
            try:
                with urllib.request.urlopen(req, timeout=30) as res:
                    return res.read().decode("utf-8")
            except Exception:
                if attempt < retries:
                    time.sleep(wait * attempt)
                else:
                    raise

    def fetch_url(self, url, retries=5, wait=5.0):
        return json.loads(self._open(url, retries, wait))

    def fetch_text(self, url, retries=5, wait=5.0):
        return self._open(url, retries, wait)

    # ── 入力解決 ────────────────────────────────────────────
    def resolve_input(self, user_input):
        """
        Returns:
          {"type": "uploader", "email": "..."}
          {"type": "collection", "id": "...", "title": "..."}
          {"type": "blogger", "blog_url": "..."}
        """
        # メールアドレス直接入力
        if "@" in user_input and not user_input.startswith("http"):
            return {"type": "uploader", "email": user_input}

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
                    "Please enter an email address or item/collection URL."
                )
            return self._resolve_from_metadata(path)

        # archive.org 以外のURL → Blogger（サイトマップ）として扱う
        if user_input.startswith("http://") or user_input.startswith("https://"):
            return {"type": "blogger", "blog_url": user_input}

        raise ValueError("Please enter an email address or archive.org URL.")

    def _resolve_from_metadata(self, identifier):
        data = self.fetch_url(f"https://archive.org/metadata/{identifier}")
        meta = data.get("metadata", {})
        mediatype = meta.get("mediatype", "")

        if mediatype == "collection":
            return {
                "type":  "collection",
                "id":    identifier,
                "title": meta.get("title", identifier),
            }

        # アイテムURL → uploaderメールを取得
        email = meta.get("uploader", "") or data.get("uploader", "")
        if not email or "@" not in email:
            raise ValueError("Could not retrieve uploader email from metadata.")
        return {"type": "uploader", "email": email}

    # ── Uploader バッチ（Scrape API / カーソルベース）────────
    def fetch_batch_uploader(self, email, mediatype="", cursor=None):
        base  = "https://archive.org/services/search/v1/scrape"
        query = f"uploader:{email}"
        if mediatype:
            query += f" AND mediatype:{mediatype}"

        params = [
            ("q",      query),
            ("fields", "identifier"),
            ("sorts",  "addeddate asc"),
            ("count",  10000),
        ]
        if cursor:
            params.append(("cursor", cursor))

        data  = self.fetch_url(base + "?" + urllib.parse.urlencode(params))
        items = [
            item.get("identifier")
            for item in data.get("items", [])
            if isinstance(item, dict) and item.get("identifier")
        ]
        return {
            "items":    items,
            "cursor":   data.get("cursor"),
            "total":    data.get("total", 0),
            "has_more": bool(data.get("cursor") and data.get("items")),
        }

    # ── Collection バッチ（advancedsearch / ページベース）───
    def fetch_batch_collection(self, collection_id, mediatype="", cursor=None):
        base  = "https://archive.org/services/search/v1/scrape"
        query = f"collection:{collection_id}"
        if mediatype:
            query += f" AND mediatype:{mediatype}"

        params = [
            ("q",      query),
            ("fields", "identifier"),
            ("sorts",  "addeddate asc"),
            ("count",  10000),
        ]
        if cursor:
            params.append(("cursor", cursor))

        data  = self.fetch_url(base + "?" + urllib.parse.urlencode(params))
        items = [
            item.get("identifier")
            for item in data.get("items", [])
            if isinstance(item, dict) and item.get("identifier")
        ]
        return {
            "items":    items,
            "cursor":   data.get("cursor"),
            "total":    data.get("total", 0),
            "has_more": bool(data.get("cursor") and data.get("items")),
        }

    # ── Blogger バッチ（サイトマップ / 子サイトマップ単位）──
    @staticmethod
    def _local(tag):
        return tag.split("}")[-1]

    def _parse_sitemap(self, xml_text):
        """Returns (記事URLのリスト, 子サイトマップURLのリスト)"""
        root = ET.fromstring(xml_text)
        urls, children = [], []
        for node in root:
            kind = self._local(node.tag)
            loc = None
            for sub in node.iter():
                if self._local(sub.tag) == "loc" and sub.text:
                    loc = sub.text.strip()
                    break
            if not loc:
                continue
            if kind == "url":
                urls.append(loc)
            elif kind == "sitemap":
                children.append(loc)
        return urls, children

    def _collect_sitemap(self, sitemap_url, depth=0):
        """入れ子サイトマップを再帰的に辿って全URLを返す"""
        urls, children = self._parse_sitemap(self.fetch_text(sitemap_url))
        if depth < 5:
            for child in children:
                time.sleep(0.5)  # サーバー負荷軽減
                urls.extend(self._collect_sitemap(child, depth + 1))
        return urls

    def fetch_batch_blogger(self, blog_url, cursor=None):
        blog_url = blog_url.strip()
        if blog_url.lower().endswith(".xml"):
            sitemap_url = blog_url
        else:
            sitemap_url = f"{blog_url.rstrip('/')}/sitemap.xml"

        urls, children = self._parse_sitemap(self.fetch_text(sitemap_url))

        # 入れ子なし → 1回で全部返す
        if not children:
            items = sorted(set(urls))
            return {
                "items":    items,
                "cursor":   None,
                "total":    len(items),
                "has_more": False,
            }

        # 入れ子あり → cursor = 次に処理する子サイトマップの番号
        idx = int(cursor) if cursor else 0
        items = list(urls) if idx == 0 else []
        if idx < len(children):
            items.extend(self._collect_sitemap(children[idx], depth=1))
        items = sorted(set(items))

        nxt = idx + 1
        has_more = nxt < len(children)
        return {
            "items":    items,
            "cursor":   str(nxt) if has_more else None,
            "total":    0,
            "has_more": has_more,
        }


# ── Routes ──────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/resolve")
def resolve():
    """入力解決エンドポイント（バッチ開始前に1回だけ呼ぶ）"""
    user_input = request.args.get("user_input", "").strip()
    if not user_input:
        return jsonify({"error": "No input provided"}), 400
    try:
        scraper = IAScraper()
        result  = scraper.resolve_input(user_input)
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 400


@app.route("/batch")
def batch():
    mode = request.args.get("mode", "uploader").strip()

    try:
        scraper = IAScraper()

        if mode == "blogger":
            blog_url = request.args.get("blog_url", "").strip()
            cursor   = request.args.get("cursor", "").strip() or None
            if not blog_url:
                return jsonify({"error": "blog_url required"}), 400
            result = scraper.fetch_batch_blogger(blog_url, cursor)

        elif mode == "collection":
            collection_id = request.args.get("collection_id", "").strip()
            mediatype     = request.args.get("mediatype", "").strip()
            cursor        = request.args.get("cursor", "").strip() or None
            if not collection_id:
                return jsonify({"error": "collection_id required"}), 400
            result = scraper.fetch_batch_collection(collection_id, mediatype, cursor)

        else:  # uploader
            email     = request.args.get("email", "").strip()
            mediatype = request.args.get("mediatype", "").strip()
            cursor    = request.args.get("cursor", "").strip() or None
            if not email or "@" not in email:
                return jsonify({"error": "Valid email address required"}), 400
            result = scraper.fetch_batch_uploader(email, mediatype, cursor)

        return jsonify(result)

    except Exception as e:
        return jsonify({"error": str(e)}), 500


if __name__ == "__main__":
    app.run(debug=True, host='0.0.0.0')
