"""
BooKlet — hybrid book recommendation engine.

Same architecture as its film sibling: precomputed neighbor lists from an
item-item collaborative model (weight 0.6) and a TF-IDF content model over
reader shelf tags + author (weight 0.4), blended with a Bayesian popularity
prior that fades as the reader's profile grows. Every pick carries the rated
book that contributed most as its explanation.
"""

import json
import re
import urllib.parse
import urllib.request
import zlib
from pathlib import Path

DATA = Path(__file__).parent / "data"

W_CF = 0.9   # tuned with evaluate.py (leave-one-out HR@10); was 0.6
W_CB = 0.1
NEUTRAL = 2.9  # Goodreads ratings skew high

OL_SEARCH = ("https://openlibrary.org/search.json?q={q}&limit=8&fields="
             "key,title,author_name,first_publish_year,cover_i,subject,"
             "ratings_average,ratings_count,edition_count")
OL_UA = {"User-Agent": "BooKlet/1.0 (student portfolio project)"}
LIVE_ID_BASE = 900_000


class Engine:
    def __init__(self):
        books = json.loads((DATA / "books.json").read_text(encoding="utf-8"))
        self.by_id = {b["id"]: b for b in books}
        self.neighbors = {
            int(k): v
            for k, v in json.loads((DATA / "neighbors.json").read_text(encoding="utf-8")).items()
        }
        self.meta = json.loads((DATA / "meta.json").read_text(encoding="utf-8"))
        self.by_pop = sorted(books, key=lambda b: b["pop"], reverse=True)
        pops = [b["pop"] for b in books]
        self._pop_min, self._pop_max = min(pops), max(pops)
        self.cat_keywords = self.meta.get("category_keywords", {})
        self._titles = {self._norm(b["title"]) for b in books}

    # -- helpers ---------------------------------------------------------------

    def _pop_norm(self, b):
        span = self._pop_max - self._pop_min or 1.0
        return (b["pop"] - self._pop_min) / span

    @staticmethod
    def _era_ok(b, era):
        y = b["year"]
        if not era:
            return True
        if era == "classics":
            return y is not None and y < 1970
        if era == "20th":
            return y is not None and 1900 <= y < 2000
        if era == "contemporary":
            return y is not None and y >= 2000
        return True

    def _card(self, b, match=None, reason=None):
        return {
            "id": b["id"], "title": b["title"], "author": b["author"],
            "year": b["year"], "shelves": b["shelves"][:2], "tags": b["tags"][:3],
            "avg": b["avg"], "n": b["n"], "match": match, "reason": reason,
            "img": b.get("img"), "isbn": b.get("isbn"),
        }

    # -- public API --------------------------------------------------------------

    def onboarding(self, n=36):
        picked, per_shelf, out = set(), {}, []
        for b in self.by_pop:
            if b["id"] in picked:
                continue
            shelf = b["shelves"][0] if b["shelves"] else "Other"
            if per_shelf.get(shelf, 0) >= 4:
                continue
            per_shelf[shelf] = per_shelf.get(shelf, 0) + 1
            picked.add(b["id"])
            out.append(self._card(b))
            if len(out) >= n:
                break
        return out

    def popular(self, n=24, shelf=None, era=None, exclude=()):
        out = []
        for b in self.by_pop:
            if b["id"] in exclude:
                continue
            if shelf and shelf not in b["shelves"]:
                continue
            if not self._era_ok(b, era):
                continue
            out.append(self._card(b, reason="Beloved across the archive"))
            if len(out) >= n:
                break
        return out

    def recommend(self, user_ratings, n=24, shelf=None, era=None):
        ratings = {int(k): float(v) for k, v in (user_ratings or {}).items()
                   if int(k) in self.by_id}
        if not ratings:
            return {"results": self.popular(n, shelf, era), "cold": True}

        scores, contrib = {}, {}
        for bid, stars in ratings.items():
            w = stars - NEUTRAL
            hood = self.neighbors.get(bid, {})
            for model, w_model in (("cf", W_CF), ("cb", W_CB)):
                for nb_id, sim in hood.get(model, []):
                    if nb_id in ratings:
                        continue
                    gain = w_model * sim * w
                    scores[nb_id] = scores.get(nb_id, 0.0) + gain
                    if gain > contrib.get(nb_id, (0.0, None))[0]:
                        contrib[nb_id] = (gain, bid)

        profile_trust = min(len(ratings) / 10.0, 1.0) * 0.85
        ranked = []
        for cid, s in scores.items():
            b = self.by_id[cid]
            if s <= 0:
                continue
            if shelf and shelf not in b["shelves"]:
                continue
            if not self._era_ok(b, era):
                continue
            final = profile_trust * s + (1 - profile_trust) * self._pop_norm(b)
            ranked.append((final, s, cid))
        ranked.sort(reverse=True)

        if not ranked:
            return {"results": self.popular(n, shelf, era, exclude=set(ratings)), "cold": True}

        top = ranked[:n]
        smax, smin = top[0][0], top[-1][0]
        span = (smax - smin) or 1.0
        results = []
        for final, s, cid in top:
            b = self.by_id[cid]
            match = int(round(72 + 27 * (final - smin) / span))
            src = contrib.get(cid, (0, None))[1]
            reason = (f"Because you rated {self.by_id[src]['title']} "
                      f"{ratings[src]:g}★") if src else "Matches your reading taste"
            results.append(self._card(b, match=match, reason=reason))

        # Personal picks only reach as far as your ratings' neighbourhood. When
        # browsing a category that your taste barely touches, top the shelf up
        # with its best-loved books rather than showing a nearly empty page.
        if len(results) < n:
            have = {c["id"] for c in results} | set(ratings)
            results += self.popular(n - len(results), shelf, era, exclude=have)
        return {"results": results, "cold": False}

    def similar(self, book_id, n=12):
        b = self.by_id.get(book_id)
        if not b:
            return []
        blended = {}
        hood = self.neighbors.get(book_id, {})
        for model, w_model in (("cf", W_CF), ("cb", W_CB)):
            for nb_id, sim in hood.get(model, []):
                blended[nb_id] = blended.get(nb_id, 0.0) + w_model * sim
        ranked = sorted(blended.items(), key=lambda kv: kv[1], reverse=True)[:n]
        if not ranked:
            return []
        smax = ranked[0][1] or 1.0
        return [
            self._card(self.by_id[nb], match=int(round(72 + 27 * s / smax)),
                       reason=f"Readers of {b['title']} shelved this next")
            for nb, s in ranked if nb in self.by_id
        ]

    # -- live lookup -------------------------------------------------------------

    @staticmethod
    def _norm(title):
        return re.sub(r"[^a-z0-9]", "", (title or "").lower())[:40]

    def _categorise(self, subjects):
        """File a live result using the same keywords the pipeline used."""
        blob = " ".join(subjects).lower()
        best, best_score = None, 0
        for cat, keywords in self.cat_keywords.items():
            score = sum(1 for kw in keywords if kw in blob)
            if score > best_score:
                best, best_score = cat, score
        return [best] if best else []

    def _search_live(self, q, limit=6):
        """Fetch anything the catalogue is missing straight from Open Library.

        Results are registered in the in-memory catalogue with content
        neighbours drawn from their category, so they can be opened, rated and
        used as recommendation seeds like any other book.
        """
        url = OL_SEARCH.format(q=urllib.parse.quote(q))
        req = urllib.request.Request(url, headers=OL_UA)
        try:
            with urllib.request.urlopen(req, timeout=6) as r:
                docs = json.loads(r.read().decode("utf-8")).get("docs", [])
        except Exception:
            return []

        found = []
        for doc in docs:
            title = (doc.get("title") or "").strip()
            cover = doc.get("cover_i")
            authors = doc.get("author_name") or []
            key = doc.get("key") or title
            if not title or not cover or not authors or len(title) > 90:
                continue
            norm = self._norm(title)
            if norm in self._titles:
                continue

            subjects = [s.lower() for s in (doc.get("subject") or [])[:12]]
            cats = self._categorise(subjects)
            editions = int(doc.get("edition_count") or 1)
            # crc32, not hash() — ids must survive a restart so saved ratings hold
            bid = LIVE_ID_BASE + zlib.crc32(key.encode()) % 90_000
            if bid in self.by_id:
                continue

            book = {
                "id": bid,
                "title": title,
                "author": authors[0],
                "year": int(doc["first_publish_year"]) if doc.get("first_publish_year") else None,
                "tags": [s.replace(" ", "-") for s in subjects[:5]],
                "shelves": cats,
                "avg": round(float(doc.get("ratings_average") or 3.9), 2),
                "n": int(doc.get("ratings_count") or editions),
                "pop": 3.8,
                "img": f"https://covers.openlibrary.org/b/id/{cover}-L.jpg",
                "isbn": None,
                "src": "live",
            }
            # seed content neighbours from the top of its category
            peers = [b["id"] for b in self.by_pop
                     if cats and cats[0] in b["shelves"]][:20]
            self.by_id[bid] = book
            self.neighbors[bid] = {"cf": [], "cb": [[p, 0.7] for p in peers]}
            self._titles.add(norm)
            found.append(self._card(book))
            if len(found) >= limit:
                break
        return found

    @staticmethod
    def _words(text):
        """Lower-case, punctuation-free words so "Rich Dad Poor Dad" finds "Rich Dad, Poor Dad"."""
        return " ".join(re.sub(r"[^a-z0-9 ]", "", (text or "").lower().replace("'", "")).split())

    def search(self, q, n=20):
        q = (q or "").strip().lower()
        if len(q) < 2:
            return []
        qw = self._words(q)
        exact, starts, contains = [], [], []
        for b in self.by_id.values():
            t, a = self._words(b["title"]), self._words(b["author"])
            core = self._words(b["title"].split(" (")[0])   # "Title (Series, #1)" -> "Title"
            if qw in (t, core, re.sub(r"^(the|a|an) ", "", core)):
                exact.append(b)
            elif t.startswith(qw) or a.startswith(qw):
                starts.append(b)
            elif qw in t or qw in a:
                contains.append(b)
        for grp in (exact, starts, contains):
            grp.sort(key=lambda b: b["pop"], reverse=True)
        local = [self._card(b) for b in (exact + starts + contains)[:n]]
        # thin results usually mean the book simply isn't in the dump — go find it
        if len(local) < 5:
            local += self._search_live(q, limit=max(4, n - len(local)))
        return local[:n]
