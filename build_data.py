"""
Postie Helper data build - run by GitHub Actions every night.

Makes site/data/points.json containing every UK:
  - toilet from the Toilet Map open dataset (CC BY 4.0)
  - toilet, post box, Post Office and Royal Mail parcel locker from OpenStreetMap (ODbL)
  - place that OpenStreetMap says has toilets: petrol stations, supermarkets, shops,
    shopping centres, motorway services, libraries, stations, public buildings,
    cafes, pubs, fast food and restaurants
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

# ---------------- what kind of place is this toilet at? ----------------
# Toilet Map entries only have a name, so guess from it (first match wins)
NAME_CATS = [
    ("fuel", r"\b(shell|bp|esso|texaco|jet|gulf|murco|valero|filling station|petrol station|service station)\b"),
    ("services", r"\b(services|service area|moto|welcome break|roadchef|extra msa)\b"),
    ("supermarket", r"\b(tesco|sainsbury'?s?|asda|morrisons|aldi|lidl|waitrose|co-?op|iceland|booths|m ?& ?s|marks (and|&) spencer)\b"),
    ("mall", r"\b(shopping cent(re|er)|mall|retail park)\b"),
    ("shop", r"\b(b ?& ?q|homebase|wickes|ikea|dunelm|garden cent(re|er)|boots|primark|john lewis|debenhams|next|the range|b ?& ?m|matalan)\b"),
    ("library", r"\blibrary\b"),
    ("station", r"\b(station|bus station|coach station|interchange)\b"),
    ("fast_food", r"\b(mcdonald'?s|kfc|burger king|subway|five guys|taco bell|popeyes)\b"),
    ("cafe", r"\b(costa|starbucks|caff[eè] nero|greggs|pret|cafe|café|coffee|tea ?room)\b"),
    ("pub", r"\b(pub|inn|arms|tavern|wetherspoon|brewery|bar)\b"),
    ("public", r"\b(town hall|community (cent(re|er)|hall)|village hall|leisure cent(re|er)|museum|visitor cent(re|er)|civic|council|church|hospital|sports cent(re|er))\b"),
]
NAME_CATS = [(c, re.compile(p, re.I)) for c, p in NAME_CATS]

# Sit-in chains that almost always have customer toilets. Included even when nobody has
# recorded it, and flagged as unconfirmed. Takeaway-only brands (Domino's, Papa John's,
# Greggs, most Subways) are left out because they usually have none.
ASSUME_CHAINS = re.compile(r"\b(mcdonald'?s|kfc|burger king|nando'?s|five guys|taco bell|popeyes|wendy'?s|"
                           r"pizza hut|wagamama|t ?g ?i ?friday'?s|frankie (and|&) benny'?s|harvester|"
                           r"toby carvery|beefeater|brewers fayre|hungry horse|miller (and|&) carter)\b", re.I)

def cat_from_name(name):
    for c, rx in NAME_CATS:
        if rx.search(name or ""):
            return c
    return None

def cat_from_tags(p):
    a, s = p.get("amenity", ""), p.get("shop", "")
    if a == "fuel": return "fuel"
    if p.get("highway") in ("services", "rest_area"): return "services"
    if s == "supermarket": return "supermarket"
    if s in ("mall", "department_store") or a == "marketplace": return "mall"
    if s: return "shop"
    if a == "library": return "library"
    if p.get("railway") in ("station", "halt") or p.get("public_transport") == "station" or a == "bus_station": return "station"
    if a == "cafe": return "cafe"
    if a in ("pub", "bar", "biergarten"): return "pub"
    if a == "fast_food": return "fast_food"
    if a in ("restaurant", "food_court", "ice_cream"): return "restaurant"
    if (a in ("townhall", "community_centre", "social_centre", "arts_centre", "place_of_worship", "hospital",
              "clinic", "courthouse", "theatre", "cinema")
            or p.get("leisure") in ("sports_centre", "leisure_centre", "park", "water_park")
            or p.get("tourism") in ("museum", "attraction", "gallery", "zoo", "theme_park", "information")):
        return "public"
    return "place"

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
        c = cat_from_name(t.get("name"))
        if c:
            t["cat"] = c
        item = {"k": "t", "s": "tm", "a": round(lat, 5), "o": round(lon, 5), "t": t}
        if r.get("opening_times"):
            try:
                item["h"] = json.loads(r["opening_times"])
            except ValueError:
                pass
        out.append(item)
    return out

# ---------------- OpenStreetMap ----------------
OSM_KEEP = ["name", "brand", "ref", "collection_times", "check_date:collection_times", "post_box:type",
            "royal_cypher", "postal_code", "addr:postcode", "addr:street", "addr:housenumber",
            "opening_hours", "operator", "fee", "charge", "wheelchair", "changing_table", "access",
            "toilets", "toilets:wheelchair", "toilets:access", "toilets:fee", "toilets:changing_table", "changing_table", "v", "assumed"]

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
                    "nwr/amenity=post_office,parcel_locker,toilets,fuel,vending_machine",
                    "nwr/toilets=yes,customers", "nwr/highway=services",
                    "nwr/amenity=fast_food,restaurant",
                    "-o", "/tmp/f.osm.pbf", "--overwrite"], check=True)
    subprocess.run(["osmium", "export", "/tmp/f.osm.pbf", "-f", "geojsonseq",
                    "-o", "/tmp/f.geojsonseq", "--overwrite", "--add-unique-id=type_id",
                    "--geometry-types=point,polygon"], check=True)
    out, seen, fuels = [], set(), []
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
            try:
                lat, lon = centroid(feat["geometry"])
            except Exception:
                continue
            if not inside(lat, lon):
                continue
            has_toilets = p.get("toilets") in ("yes", "customers")
            rm = re.search(r"royal\s*mail", " ".join(p.get(x, "") for x in ("brand", "operator", "name")), re.I)
            cat = None
            if am == "post_box":
                k = "p"
            elif am == "post_office":
                k = "o"
            elif am == "parcel_locker" or (am == "vending_machine" and "parcel_pickup" in p.get("vending", "")):
                if not rm:
                    continue
                k = "l"
            elif am == "toilets":
                if p.get("access") in ("private", "no"):
                    continue
                k = "t"
            elif am == "fuel":
                fuels.append((lat, lon))
                if not has_toilets:
                    continue
                k, cat = "t", "fuel"
                p = dict(p, v="1")
            elif has_toilets or p.get("highway") == "services":
                k, cat = "t", cat_from_tags(p)
                p = dict(p, v="1")          # a venue rather than a toilet block
                if not has_toilets:
                    p["assumed"] = "1"      # motorway services: toilets taken as read
            elif am in ("fast_food", "restaurant") and ASSUME_CHAINS.search(
                    " ".join(p.get(x, "") for x in ("brand", "name", "operator"))):
                if p.get("toilets") == "no":
                    continue
                k, cat = "t", cat_from_tags(p)
                p = dict(p, v="1", assumed="1")
            else:
                continue
            t = {x: p[x] for x in OSM_KEEP if p.get(x)}
            if p.get("changing_place") == "yes" or p.get("changing_table:adult") == "yes":
                t["cp"] = "1"          # Changing Places: adult-sized bench and hoist
            if cat:
                t["cat"] = cat
            out.append({"k": k, "s": "osm", "id": fid, "a": round(lat, 5), "o": round(lon, 5), "t": t})
    return out, fuels

# ---------------- merge ----------------
def metres(a, b):
    dy = (a["a"] - b["a"]) * 111320
    dx = (a["o"] - b["o"]) * 111320 * math.cos(math.radians(a["a"]))
    return math.hypot(dx, dy)

def cell(lat, lon):
    return round(lat * 300), round(lon * 200)      # roughly 370 m x 350 m

def nearby(index, lat, lon):
    gy, gx = cell(lat, lon)
    return [y for dy in (-1, 0, 1) for dx in (-1, 0, 1) for y in index.get((gy + dy, gx + dx), [])]

def main():
    tm = toilet_map()
    om, fuels = osm()

    # Toilet Map toilets within 40 m of a petrol station are at that station
    fuel_index = {}
    for lat, lon in fuels:
        fuel_index.setdefault(cell(lat, lon), []).append({"a": lat, "o": lon})
    for x in tm:
        if "cat" not in x["t"] and any(metres(x, y) < 40 for y in nearby(fuel_index, x["a"], x["o"])):
            x["t"]["cat"] = "fuel"

    # plain toilets first: Toilet Map, then OSM toilets it doesn't already have (30 m)
    index = {}
    def remember(x):
        index.setdefault(cell(x["a"], x["o"]), []).append(x)
    for x in tm:
        remember(x)
    toilets = list(tm)
    others = []
    for x in om:
        if x["k"] != "t":
            others.append(x)
        elif "cat" not in x["t"]:
            if not any(metres(x, y) < 30 for y in nearby(index, x["a"], x["o"])):
                toilets.append(x)
                remember(x)
    # then places with toilets; if one already sits within 40 m (60 m for petrol and services),
    # keep the existing toilet and give it the place's type and toilet details instead
    for x in om:
        if x["k"] != "t" or "cat" not in x["t"]:
            continue
        limit = 60 if x["t"]["cat"] in ("fuel", "services") else 40
        near = [y for y in nearby(index, x["a"], x["o"]) if metres(x, y) < limit]
        if near:
            y = min(near, key=lambda y: metres(x, y))
            y["t"].setdefault("cat", x["t"]["cat"])
            place = x["t"].get("name") or x["t"].get("brand")
            if place and place != y["t"].get("name"):
                y["t"].setdefault("at", place)
            if x["t"].get("cp"):
                y["t"]["cp"] = "1"
            for key in ("toilets", "toilets:access"):
                if key in x["t"]:
                    y["t"].setdefault(key, x["t"][key])
            continue
        toilets.append(x)
        remember(x)

    points = toilets + others
    counts = {}
    for x in points:
        counts[x["k"]] = counts.get(x["k"], 0) + 1
        c = x["t"].get("cat")
        if c:
            counts["at_" + c] = counts.get("at_" + c, 0) + 1
        if x["t"].get("cp"):
            counts["changing_places"] = counts.get("changing_places", 0) + 1
        if x["t"].get("assumed"):
            counts["unconfirmed"] = counts.get("unconfirmed", 0) + 1
    print("Counts:", json.dumps(counts, sort_keys=True))
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
