"""
Postie Helper data build - run by GitHub Actions every night.

Makes site/data/points.json containing every UK:
  - toilet from the Toilet Map open dataset (CC BY 4.0)
  - toilet, post box, Post Office and Royal Mail parcel locker from OpenStreetMap (ODbL)
then copies the app files into site/ ready to publish on GitHub Pages.
"""
import csv, io, json, math, os, re, shutil, subprocess, sys, glob, urllib.request
from datetime import datetime, timezone

csv.field_size_limit(10**8)
UA = {"User-Agent": "postie-helper-build (GitHub Actions)"}
UK = (49.8, 61.0, -8.7, 2.0)          # lat min/max, lon min/max
SITE = "site"

def inside(lat, lon):
    return UK[0] < lat < UK[1] and UK[2] < lon < UK[3]

def fetch(url, dest=None):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=600) as r:
        if dest:
            with open(dest, "wb") as f:
                shutil.copyfileobj(r, f, 1 << 20)
            return dest
        return r.read()

# ---------------- Toilet Map ----------------
TM_KEEP = ["id", "name", "accessible", "baby_change", "radar", "no_payment",
           "payment_details", "notes", "men", "women", "all_gender"]

def toilet_map():
    text = None
    try:
        page = fetch("https://www.toiletmap.org.uk/dataset").decode("utf-8", "replace")
        m = re.search(r'https://[^"\'\s<>]+?\.csv\?download=1', page.replace("&amp;", "&"))
        if not m:
            raise RuntimeError("CSV link not found on dataset page")
        text = fetch(m.group(0)).decode("utf-8", "replace")
        print("Toilet Map: downloaded latest CSV")
    except Exception as e:
        print("Toilet Map download failed:", e)
        local = sorted(glob.glob("*toilet*.csv"))
        if local:
            text = open(local[-1], encoding="utf-8", errors="replace").read()
            print("Toilet Map: using", local[-1], "from the repo instead")
    if not text:
        return []
    out = []
    for r in csv.DictReader(io.StringIO(text)):
        if r.get("active") != "true":
            continue
        try:
            lat, lon = float(r["latitude"]), float(r["longitude"])
        except (KeyError, ValueError):
            continue
        if not inside(lat, lon):
            continue
        t = {k: r[k].strip() for k in TM_KEEP if r.get(k, "").strip()}
        if "notes" in t and len(t["notes"]) > 280:
            t["notes"] = t["notes"][:277] + "..."
        item = {"k": "t", "s": "tm", "a": round(lat, 5), "o": round(lon, 5), "t": t}
        if r.get("opening_times"):
            try:
                item["h"] = json.loads(r["opening_times"])
            except ValueError:
                pass
        out.append(item)
    return out

# ---------------- OpenStreetMap ----------------
OSM_KEEP = ["name", "ref", "collection_times", "check_date:collection_times", "post_box:type",
            "royal_cypher", "postal_code", "addr:postcode", "addr:street", "addr:housenumber",
            "opening_hours", "operator", "brand", "fee", "charge", "wheelchair",
            "changing_table", "access"]

def centroid(geom):
    pts = []
    def walk(c):
        if isinstance(c[0], (int, float)):
            pts.append(c)
        else:
            for x in c:
                walk(x)
    walk(geom["coordinates"])
    return sum(p[1] for p in pts) / len(pts), sum(p[0] for p in pts) / len(pts)

def osm():
    pbf = fetch("https://download.geofabrik.de/europe/united-kingdom-latest.osm.pbf", "/tmp/uk.osm.pbf")
    subprocess.run(["osmium", "tags-filter", pbf, "n/amenity=post_box",
                    "nwr/amenity=post_office,parcel_locker,toilets",
                    "-o", "/tmp/f.osm.pbf", "--overwrite"], check=True)
    subprocess.run(["osmium", "export", "/tmp/f.osm.pbf", "-f", "geojsonseq",
                    "-o", "/tmp/f.geojsonseq", "--overwrite", "--add-unique-id=type_id",
                    "--geometry-types=point,polygon"], check=True)
    out, seen = [], set()
    with open("/tmp/f.geojsonseq", encoding="utf-8") as f:
        for line in f:
            line = line.strip().lstrip("\x1e")
            if not line:
                continue
            feat = json.loads(line)
            p = feat.get("properties") or {}
            am = p.get("amenity")
            fid = feat.get("id") or ""
            if fid in seen:
                continue
            seen.add(fid)
            if am == "post_box":
                k = "p"
            elif am == "post_office":
                k = "o"
            elif am == "parcel_locker":
                txt = " ".join(p.get(x, "") for x in ("brand", "operator", "name"))
                if not re.search(r"royal\s*mail", txt, re.I):
                    continue
                k = "l"
            elif am == "toilets":
                if p.get("access") in ("private", "no"):
                    continue
                k = "t"
            else:
                continue
            try:
                lat, lon = centroid(feat["geometry"])
            except Exception:
                continue
            if not inside(lat, lon):
                continue
            t = {x: p[x] for x in OSM_KEEP if p.get(x)}
            out.append({"k": k, "s": "osm", "id": fid, "a": round(lat, 5), "o": round(lon, 5), "t": t})
    return out

# ---------------- merge ----------------
def metres(a, b):
    dy = (a["a"] - b["a"]) * 111320
    dx = (a["o"] - b["o"]) * 111320 * math.cos(math.radians(a["a"]))
    return math.hypot(dx, dy)

def main():
    tm = toilet_map()
    om = osm()
    # drop OSM toilets that the Toilet Map already has (within 30 m)
    grid = {}
    for x in tm:
        grid.setdefault((round(x["a"] * 300), round(x["o"] * 200)), []).append(x)
    kept = []
    for x in om:
        if x["k"] == "t":
            gy, gx = round(x["a"] * 300), round(x["o"] * 200)
            near = [y for dy in (-1, 0, 1) for dx in (-1, 0, 1) for y in grid.get((gy + dy, gx + dx), [])]
            if any(metres(x, y) < 30 for y in near):
                continue
        kept.append(x)
    points = tm + kept
    counts = {}
    for x in points:
        counts[x["k"]] = counts.get(x["k"], 0) + 1
    print("Counts:", counts)
    if counts.get("p", 0) < 10000:
        sys.exit("Too few post boxes - something went wrong, keeping the previous site")

    os.makedirs(SITE + "/data", exist_ok=True)
    for name in ("index.html", "sw.js"):
        if os.path.exists(name):
            shutil.copy(name, SITE)
    meta = {"built": datetime.now(timezone.utc).isoformat(timespec="minutes"), "counts": counts}
    with open(SITE + "/data/points.json", "w", encoding="utf-8") as f:
        json.dump({"meta": meta, "points": points}, f, separators=(",", ":"), ensure_ascii=False)
    print("Wrote", os.path.getsize(SITE + "/data/points.json") // 1024, "KB")

if __name__ == "__main__":
    main()
