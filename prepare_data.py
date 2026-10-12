"""
BooKlet — data preparation pipeline.

Builds the catalogue from two sources:

  1. goodbooks-10k — 10,000 Goodreads books with ~6M ratings. These carry the
     collaborative signal (readers who liked X also liked Y).
  2. Open Library subjects — hundreds more books per category, so the shelves
     stay stocked with contemporary and non-Western titles the 2017 Goodreads
     dump never had (self-help, business, Indian writing, technology...).

Categories are not guessed from a single tag. Every goodbooks title is scored
against each category using its *weighted* reader-shelf counts, so 1984 lands
in Science Fiction and Classics rather than whichever keyword matched first.

Outputs:
  data/books.json      catalogue: id, title, author, year, category, tags, cover
  data/neighbors.json  cf (collaborative, goodbooks only) + cb (content, all)
  data/meta.json       stats and the category list for the UI

Run once before starting the app:  python prepare_data.py
"""

import json
import re
import sys
import time
import urllib.request
from pathlib import Path

import numpy as np
import pandas as pd
from scipy.sparse import csr_matrix
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity
from sklearn.preprocessing import normalize

GOODBOOKS_URL = "https://raw.githubusercontent.com/zygmuntz/goodbooks-10k/master/"
FILES = ["books.csv", "ratings.csv", "book_tags.csv", "tags.csv"]
OL_SUBJECT = "https://openlibrary.org/subjects/{slug}.json?limit={limit}"
OL_BOOKS = "https://openlibrary.org/api/books?bibkeys={keys}&format=json&jscmd=data"
OL_UA = {"User-Agent": "BooKlet/1.0 (student portfolio project)"}
ISBN_CACHE_NAME = "isbn_covers.json"

ROOT = Path(__file__).parent
RAW = ROOT / "data" / "raw"
OUT = ROOT / "data"
OL_CACHE = RAW / "openlibrary.json"

TOP_K = 30
USER_MOD = 4
BAYES_M = 5000
SUPP_ID_BASE = 100_000     # supplemental books live above the goodbooks ids
PER_SUBJECT = 220          # fetched per Open Library slug before filtering

# Housekeeping shelves that describe the reader, not the book.
JUNK_TAG = re.compile(
    r"^(to-read|currently|owned|own|my-|favorites?|favourites?|kindle|library"
    r"|audio|ebook|e-book|borrowed|wish|to-buy|re-read|reread|default|book-club"
    r"|calibre|series|hardcover|paperback|dnf|abandoned|shelf|all-time|read-in"
    r"|\d{4}|books-i|i-own|home|spanish|english|german)"
)

# category -> (goodreads shelf keywords, open-library subject slugs)
# Keywords are matched against reader shelves and weighted by how many readers
# used them, so a book's dominant subject wins.
CATEGORIES = {
    "Fantasy":               (("fantasy", "high-fantasy", "epic-fantasy", "urban-fantasy", "magic", "dragons"),
                              ("fantasy", "epic_fantasy")),
    "Science Fiction":       (("science-fiction", "sci-fi", "scifi", "dystopia", "dystopian", "space", "cyberpunk", "time-travel"),
                              ("science_fiction", "dystopia")),
    "Mystery & Crime":       (("mystery", "crime", "detective", "noir", "murder", "whodunit"),
                              ("mystery_and_detective_stories", "crime")),
    "Thriller & Suspense":   (("thriller", "suspense", "espionage", "spy"),
                              ("thriller", "suspense")),
    "Horror":                (("horror", "gothic", "vampires", "zombies", "ghosts", "supernatural"),
                              ("horror", "ghost_stories")),
    "Romance":               (("romance", "love", "chick-lit", "romantic"),
                              ("romance", "love_stories")),
    "Historical Fiction":    (("historical-fiction", "historical", "wwii", "world-war-ii"),
                              ("historical_fiction",)),
    "Classics":              (("classics", "classic", "literature", "literary-fiction"),
                              ("classic_literature", "classics")),
    "Contemporary Fiction":  (("contemporary", "fiction", "modern", "novel"),
                              ("fiction", "literature")),
    "Young Adult":           (("young-adult", "ya", "teen", "coming-of-age"),
                              ("young_adult_fiction", "coming_of_age")),
    "Children's":            (("childrens", "children", "picture-books", "middle-grade", "juvenile"),
                              ("juvenile_literature", "children_s_stories")),
    "Comics & Graphic":      (("graphic-novels", "graphic-novel", "comics", "manga", "superheroes"),
                              ("comics_and_graphic_novels", "comic_books_strips_etc")),
    "Poetry":                (("poetry", "poems", "verse"),
                              ("poetry",)),
    "Biography & Memoir":    (("biography", "memoir", "autobiography"),
                              ("biography", "autobiography")),
    "History":               (("history", "war", "military-history", "ancient", "medieval"),
                              ("history", "world_history")),
    "Science & Nature":      (("science", "physics", "biology", "astronomy", "evolution", "medicine", "nature"),
                              ("science", "popular_science", "natural_history")),
    "Technology & Coding":   (("technology", "programming", "computer-science", "software", "coding", "data-science", "artificial-intelligence"),
                              ("computers", "computer_programming", "artificial_intelligence")),
    "Business & Economics":  (("business", "economics", "management", "leadership", "entrepreneurship", "finance", "marketing"),
                              ("business", "economics", "management", "entrepreneurship")),
    "Self-Help & Habits":    (("self-help", "selfhelp", "personal-development", "productivity", "habits", "motivational"),
                              ("self-help", "success_in_business", "personal_development")),
    "Psychology":            (("psychology", "behavior", "neuroscience", "mental-health", "cognitive"),
                              ("psychology", "behavioral_economics")),
    "Philosophy":            (("philosophy", "existentialism", "stoicism", "ethics"),
                              ("philosophy", "ethics")),
    "Spirituality & Faith":  (("spirituality", "religion", "christian", "buddhism", "faith", "islam", "theology"),
                              ("religion", "spirituality", "spiritual_life")),
    "Travel & Adventure":    (("travel", "adventure", "survival", "exploration"),
                              ("travel", "adventure_stories")),
    "Health & Wellbeing":    (("health", "fitness", "nutrition", "diet", "wellness", "mindfulness"),
                              ("health", "physical_fitness", "mindfulness")),
    "Food & Cooking":        (("cookbooks", "cooking", "food", "recipes", "baking"),
                              ("cooking", "cookbooks")),
    "Indian Writing":        (("india", "indian", "indian-authors", "desi"),
                              ("indic_fiction", "india", "indian_literature")),
}
CATEGORY_LIST = list(CATEGORIES)

# "fiction" is a substring of "non-fiction", and every novel is shelved as
# "fiction" by someone — so the catch-all category needs both a block list and
# a lighter touch, or it swallows the whole library.
NEGATIVE = {
    "Contemporary Fiction": ("non-fiction", "nonfiction", "science-fiction",
                             "historical-fiction", "fan-fiction", "fanfiction"),
}
WEAK_KEYWORD = {"fiction": 0.3, "novel": 0.3, "modern": 0.5, "literature": 0.6}


def download_goodbooks():
    RAW.mkdir(parents=True, exist_ok=True)
    for name in FILES:
        dest = RAW / name
        if dest.exists() and dest.stat().st_size > 0:
            print(f"{name}: already present")
            continue
        print(f"downloading {name} ...")
        urllib.request.urlretrieve(GOODBOOKS_URL + name, dest)


# ---------------------------------------------------------------- categories

def score_categories(tag_weights):
    """tag_weights: {shelf_name: reader_count} -> ranked category names."""
    scores = {}
    for cat, (keywords, _) in CATEGORIES.items():
        total = 0.0
        blocked = NEGATIVE.get(cat, ())
        for tag, count in tag_weights.items():
            if any(bad in tag for bad in blocked):
                continue
            for kw in keywords:
                weight = WEAK_KEYWORD.get(kw, 1.0)
                if tag == kw:
                    total += count * 3 * weight   # exact shelf match counts most
                    break
                if kw in tag:
                    total += count * weight
                    break
        if total:
            scores[cat] = total
    if not scores:
        return []
    ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
    top = ranked[0][1]
    # keep runners-up only when they are a real signal, not noise
    return [c for c, s in ranked[:3] if s >= top * 0.28]


# ------------------------------------------------------------- open library

def fetch_openlibrary():
    """Pull popular works per category. Cached to disk after the first run."""
    if OL_CACHE.exists():
        try:
            cached = json.loads(OL_CACHE.read_text(encoding="utf-8"))
            print(f"open library: {len(cached)} works from cache")
            return cached
        except ValueError:
            pass

    print("fetching Open Library subjects (first run only) ...")
    seen, works = set(), []
    for cat, (_, slugs) in CATEGORIES.items():
        got = 0
        for slug in slugs:
            url = OL_SUBJECT.format(slug=slug, limit=PER_SUBJECT)
            req = urllib.request.Request(url, headers=OL_UA)
            try:
                with urllib.request.urlopen(req, timeout=40) as r:
                    payload = json.loads(r.read().decode("utf-8"))
            except Exception as exc:
                print(f"  ! {slug}: {exc}")
                continue
            for w in payload.get("works", []):
                key = w.get("key")
                title = (w.get("title") or "").strip()
                cover = w.get("cover_id")
                year = w.get("first_publish_year")
                authors = w.get("authors") or []
                # a cover and a real author are what make the shelf look right
                if not key or key in seen or not title or not cover or not authors:
                    continue
                if not year or year < 1800 or year > 2026:
                    continue
                if len(title) > 90:
                    continue
                seen.add(key)
                works.append({
                    "key": key,
                    "title": title,
                    "author": authors[0].get("name", "Unknown"),
                    "year": int(year),
                    "cover_id": int(cover),
                    "editions": int(w.get("edition_count") or 1),
                    "category": cat,
                    "subjects": [s.lower() for s in (w.get("subject") or [])[:12]],
                })
                got += 1
            time.sleep(0.4)  # be polite to the API
        print(f"  {cat}: +{got}")

    OL_CACHE.write_text(json.dumps(works, separators=(",", ":")), encoding="utf-8")
    print(f"open library: {len(works)} works fetched")
    return works


def enrich_covers(catalog):
    """Goodreads has no jacket for a chunk of its own catalogue. Ask Open
    Library for those by ISBN — batched 40 at a time — so the shelves fill in."""
    cache_file = RAW / ISBN_CACHE_NAME
    cache = {}
    if cache_file.exists():
        try:
            cache = json.loads(cache_file.read_text(encoding="utf-8"))
        except ValueError:
            cache = {}

    need = [c for c in catalog if not c["img"] and c.get("isbn")]
    print(f"covers: {len(need)} books have no jacket, asking Open Library ...")

    pending = [c["isbn"] for c in need if c["isbn"] not in cache]
    for start in range(0, len(pending), 40):
        batch = pending[start : start + 40]
        keys = ",".join(f"ISBN:{i}" for i in batch)
        req = urllib.request.Request(OL_BOOKS.format(keys=keys), headers=OL_UA)
        try:
            with urllib.request.urlopen(req, timeout=40) as r:
                payload = json.loads(r.read().decode("utf-8"))
        except Exception as exc:
            print(f"  ! batch {start//40}: {exc}")
            payload = {}
        for isbn in batch:
            entry = payload.get(f"ISBN:{isbn}") or {}
            cover = (entry.get("cover") or {})
            cache[isbn] = cover.get("large") or cover.get("medium") or None
        time.sleep(0.3)
        if start and start % 800 == 0:
            cache_file.write_text(json.dumps(cache, separators=(",", ":")), encoding="utf-8")

    cache_file.write_text(json.dumps(cache, separators=(",", ":")), encoding="utf-8")

    filled = 0
    for c in need:
        url = cache.get(c["isbn"])
        if url:
            c["img"] = url
            filled += 1
    print(f"covers: filled {filled} of {len(need)}")


def main():
    t0 = time.time()
    download_goodbooks()

    books = pd.read_csv(RAW / "books.csv")
    tags = pd.read_csv(RAW / "tags.csv")
    book_tags = pd.read_csv(RAW / "book_tags.csv")

    # ---- reader shelves, weighted ------------------------------------------
    book_tags = book_tags.merge(tags, on="tag_id")
    book_tags["tag_name"] = book_tags["tag_name"].astype(str).str.lower()
    book_tags = book_tags[~book_tags["tag_name"].str.match(JUNK_TAG)]
    book_tags = book_tags.sort_values(["goodreads_book_id", "count"], ascending=[True, False])

    weights = {}
    for gid, grp in book_tags.groupby("goodreads_book_id"):
        head = grp.head(25)
        weights[gid] = dict(zip(head["tag_name"], head["count"]))

    books["tag_weights"] = books["goodreads_book_id"].map(weights)
    books["tag_weights"] = books["tag_weights"].apply(lambda x: x if isinstance(x, dict) else {})
    books["tag_list"] = books["tag_weights"].apply(lambda d: list(d)[:12])
    books["cats"] = books["tag_weights"].apply(score_categories)

    # ---- popularity ---------------------------------------------------------
    C = float(books["average_rating"].mean())
    v, R = books["work_ratings_count"], books["average_rating"]
    books["pop"] = (v / (v + BAYES_M)) * R + (BAYES_M / (v + BAYES_M)) * C

    # ---- collaborative model (goodbooks only) -------------------------------
    print("streaming ratings.csv (sampling 1 in", USER_MOD, "users) ...")
    kept = []
    for chunk in pd.read_csv(RAW / "ratings.csv", chunksize=1_000_000):
        kept.append(chunk[chunk["user_id"] % USER_MOD == 0])
    r = pd.concat(kept, ignore_index=True)
    n_users, n_ratings = r["user_id"].nunique(), len(r)
    print(f"sampled {n_ratings:,} ratings from {n_users:,} users")

    r["centered"] = r["rating"] - r.groupby("user_id")["rating"].transform("mean")
    uid = {u: i for i, u in enumerate(r["user_id"].unique())}
    X = csr_matrix(
        (r["centered"], (r["book_id"] - 1, r["user_id"].map(uid))),
        shape=(10000, len(uid)),
    )
    Xn = normalize(X)
    print(f"CF matrix: {X.shape[0]} books x {X.shape[1]} users")

    cf_neighbors = {}
    CHUNK = 500
    for start in range(0, 10000, CHUNK):
        sims = (Xn[start : start + CHUNK] @ Xn.T).toarray()
        for row_off in range(sims.shape[0]):
            i = start + row_off
            row = sims[row_off]
            row[i] = -1.0
            idx = np.argpartition(row, -TOP_K)[-TOP_K:]
            idx = idx[np.argsort(row[idx])[::-1]]
            cf_neighbors[i + 1] = [
                [int(j + 1), round(float(row[j]), 4)] for j in idx if row[j] > 0.04
            ]

    # ---- catalogue: goodbooks ------------------------------------------------
    catalog = []
    known_titles = set()
    for b in books.itertuples():
        year = b.original_publication_year
        img = str(b.image_url) if pd.notna(b.image_url) else ""
        title = str(b.title)
        known_titles.add(re.sub(r"[^a-z0-9]", "", title.lower())[:40])
        catalog.append({
            "id": int(b.book_id),
            "title": title,
            "author": str(b.authors).split(",")[0].strip(),
            "year": int(year) if pd.notna(year) else None,
            "tags": b.tag_list[:5],
            "shelves": b.cats,
            "avg": round(float(b.average_rating), 2),
            "n": int(b.work_ratings_count),
            "pop": round(float(b.pop), 4),
            "img": None if (not img or "nophoto" in img) else img,
            "isbn": str(int(b.isbn13)) if pd.notna(b.isbn13) else None,
            "src": "gb",
            "doc": " ".join(list(b.tag_weights)[:15]),
        })

    enrich_covers(catalog)

    # ---- catalogue: open library supplement ---------------------------------
    supp = fetch_openlibrary()
    added = 0
    for i, w in enumerate(supp):
        norm = re.sub(r"[^a-z0-9]", "", w["title"].lower())[:40]
        if norm in known_titles:
            continue
        known_titles.add(norm)
        # editions in print is the best popularity proxy Open Library exposes
        pop = min(4.35, 3.55 + float(np.log1p(w["editions"])) / 9.0)
        catalog.append({
            "id": SUPP_ID_BASE + i,
            "title": w["title"],
            "author": w["author"],
            "year": w["year"],
            "tags": [s.replace(" ", "-") for s in w["subjects"][:5]],
            "shelves": [w["category"]],
            "avg": round(float(3.9 + min(w["editions"], 200) / 800.0), 2),
            "n": w["editions"],
            "pop": round(pop, 4),
            "img": f"https://covers.openlibrary.org/b/id/{w['cover_id']}-L.jpg",
            "isbn": None,
            "src": "ol",
            "doc": " ".join(s.replace(" ", "_") for s in w["subjects"][:12]),
        })
        added += 1
    print(f"supplement: +{added} books from Open Library")

    # ---- content model (every book, both sources) ----------------------------
    def doc_of(c):
        author = re.sub(r"[^a-z ]", "", c["author"].lower()).replace(" ", "_")
        cats = " ".join(s.lower().replace(" ", "_").replace("&", "and") for s in c["shelves"])
        return f"{c['doc']} {cats} {cats} {author} {author}"

    docs = [doc_of(c) for c in catalog]
    tfidf = TfidfVectorizer(min_df=2, max_features=60000)
    M = tfidf.fit_transform(docs)
    print(f"CB matrix: {M.shape[0]} books x {M.shape[1]} terms")

    ids = np.array([c["id"] for c in catalog])
    cb_neighbors = {}
    for start in range(0, M.shape[0], CHUNK):
        sims = cosine_similarity(M[start : start + CHUNK], M)
        for row_off in range(sims.shape[0]):
            i = start + row_off
            row = sims[row_off]
            row[i] = -1.0
            idx = np.argpartition(row, -TOP_K)[-TOP_K:]
            idx = idx[np.argsort(row[idx])[::-1]]
            cb_neighbors[int(ids[i])] = [
                [int(ids[j]), round(float(row[j]), 4)] for j in idx if row[j] > 0.08
            ]

    for c in catalog:
        c.pop("doc", None)

    neighbors = {
        str(c["id"]): {
            "cf": cf_neighbors.get(c["id"], []) if c["src"] == "gb" else [],
            "cb": cb_neighbors.get(c["id"], []),
        }
        for c in catalog
        if cf_neighbors.get(c["id"]) or cb_neighbors.get(c["id"])
    }

    per_cat = {}
    for c in catalog:
        for s in c["shelves"]:
            per_cat[s] = per_cat.get(s, 0) + 1

    meta = {
        "n_books": len(catalog),
        "n_goodbooks": int(len(books)),
        "n_supplemental": added,
        "n_ratings": int(n_ratings),
        "n_users": int(n_users),
        "shelves": [c for c in CATEGORY_LIST if per_cat.get(c, 0) >= 12],
        "per_category": per_cat,
        # keywords let the engine file live search results into a category too
        "category_keywords": {c: list(k) for c, (k, _) in CATEGORIES.items()},
        "dataset": "goodbooks-10k + Open Library",
        "built_at": time.strftime("%Y-%m-%d %H:%M"),
    }

    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "books.json").write_text(json.dumps(catalog, separators=(",", ":")), encoding="utf-8")
    (OUT / "neighbors.json").write_text(json.dumps(neighbors, separators=(",", ":")), encoding="utf-8")
    (OUT / "meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")

    print(f"\nwrote {len(catalog)} books ({added} supplemental), "
          f"{len(neighbors)} neighbour lists in {time.time()-t0:.1f}s")
    print("books per category:")
    for c in CATEGORY_LIST:
        print(f"  {c:<24} {per_cat.get(c, 0)}")


if __name__ == "__main__":
    sys.exit(main())
