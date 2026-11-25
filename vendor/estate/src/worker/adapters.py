"""Reading the four services' answers — pure functions, no network, no js.

Every function here takes a decoded body and returns our own shape. Nothing
in this module imports `js`, so the same code that runs in the Worker runs
under plain CPython against the saved fixtures, which is the only way these
rules get tested at all.

Three rules the estate taught us, each of them measured:

1. An HTTP code is not an answer. The CI gateway returns 200 with
   `{"message": "signature is invalid"}` when it rejects a token, and the
   NBFC endpoint returns 404 with `"status": "success"` and an empty list to
   mean "this product has no finance schemes". So each adapter decides
   emptiness from the body, and a body it cannot read is INVALID — never a
   zero, never "no offers".

2. A field name is not a meaning. The same offer reports
   `discountValue: 10` from getOffers (ten percent, capped at
   `maxBenefites: 1000`) and `discountValue: 1000` from offers/ (the
   resolved cap in rupees). Read either alone and you print "₹10 off" or
   "1000% off", so value is never read without its unit.

3. A timestamp is not a timestamp. PIM emits fractional seconds of whatever
   length it happens to have — `2026-10-31T07:09:46.56Z` — which
   `datetime.fromisoformat` rejected on two of thirty products and silently
   handed us last month's price. Hence `parse_time`.
"""
from __future__ import annotations

import datetime
import html as _html
import re

UTC = datetime.timezone.utc

_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"\s+")
_FRACTION = re.compile(r"\.(\d{1,6})\d*")


def text_of(markup) -> str:
    """Offer copy arrives as HTML. Return something safe to print."""
    if not markup or not isinstance(markup, str):
        return ""
    plain = _html.unescape(_TAG.sub(" ", markup))
    return _WS.sub(" ", plain.replace(" ", " ")).strip(" ·•-/\t")


_BLOCKS = re.compile(r"<(h[1-6]|p|div|li)\b[^>]*>(.*?)</\1\s*>", re.I | re.S)


def _inline(fragment: str) -> str:
    """Text of one block as a browser shows it: inline tags (`<strong>`,
    `<span>`) vanish without leaving a space, so "₹.<b>600/-</b>" reads
    "₹.600/-" and not "₹. 600"."""
    plain = _html.unescape(_TAG.sub("", fragment or ""))
    return _WS.sub(" ", plain.replace("\xa0", " ")).strip()


def offer_parts(markup) -> tuple[str, str]:
    """An offer's copy as the shop prints it: (title, body).

    The shop's copy is a stack of blocks whose first one, in green on the
    page, is the title — "Bank Offer :", "Special Offers", "EMI :" — and the
    rest the sentence under it. Copy with no blocks comes back as one body.
    """
    if not markup or not isinstance(markup, str):
        return "", ""
    blocks = [b for b in (_inline(m.group(2)) for m in _BLOCKS.finditer(markup)) if b]
    if not blocks:
        return "", text_of(markup)
    return blocks[0], " ".join(blocks[1:])


def number(value):
    """Prices arrive as `"49990"` and as `54990` in the same array."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return value
    try:
        cleaned = str(value).replace(",", "").strip()
        return float(cleaned) if "." in cleaned else int(cleaned)
    except (TypeError, ValueError):
        return None


def parse_time(stamp):
    """ISO-8601 with however many fractional digits the upstream felt like."""
    if not stamp or not isinstance(stamp, str):
        return None
    cleaned = _FRACTION.sub(lambda m: "." + m.group(1).ljust(6, "0"),
                            stamp.strip().replace("Z", "+00:00"))
    try:
        when = datetime.datetime.fromisoformat(cleaned)
    except ValueError:
        return None
    return when if when.tzinfo else when.replace(tzinfo=UTC)


def live_now(start, end, now) -> bool:
    """A window is live when it has started and has not ended."""
    opens, closes = parse_time(start), parse_time(end)
    if opens and opens > now:
        return False
    if closes and closes <= now:
        return False
    return True


def current(entries, now, key: str = "price"):
    """The value in force today out of a dated list.

    PIM keeps price history in one array, newest first but not guaranteed so,
    every `endDate` usually null. The value in force is the one with the
    latest start that has already started and has not ended. Verified
    against the search index on 30 production products: 30 of 30 agree on
    both selling price and MRP once `parse_time` handles short fractions.
    """
    best, best_start = None, None
    for entry in entries or []:
        if not isinstance(entry, dict):
            continue
        opens = parse_time(entry.get("startDate"))
        if opens is None or not live_now(entry.get("startDate"),
                                        entry.get("endDate"), now):
            continue
        if best_start is None or opens > best_start:
            best, best_start = number(entry.get(key)), opens
    return best, (best_start.isoformat() if best_start else None)


# ── PIM product ────────────────────────────────────────────────────────────

def _spec_groups(specifications) -> list:
    """The active attribute groups that have at least one filled value.

    Shared by the product itself and the related products beside it, so the
    compare table lines both up from the same reading of the same shape.
    """
    groups = []
    for group in ((specifications or {}).get("attributeGroups") or []):
        if not isinstance(group, dict) or group.get("isActive") is False:
            continue
        attributes = [
            {"code": a.get("code"), "name": a.get("name"),
             "value": (a.get("value") or "").strip()}
            for a in (group.get("attributes") or [])
            if isinstance(a, dict) and (a.get("value") or "").strip()
        ]
        if attributes:
            groups.append({"code": group.get("code"), "name": group.get("name"),
                           "attributes": attributes})
    return groups


def _variants(details, here: dict) -> dict:
    """The model's variant chooser, as PIM defines it and the shop draws it.

    `variantdetails` is the source of the shop's own chips: every axis of the
    model ("Display Size", "Strap size", not only colour and storage), its
    values in PIM's order under PIM's labels ("1.42 Ton", where the attribute
    code says "1-42-ton"), and every sibling item with the values that pick
    it. The search index is not a substitute: it files colours by family, so
    Moto's Orient Blue and Country Air are both "Blue" there and one of them
    vanished from the chooser.

    A value PIM has switched off (status 0) is left out, as the shop leaves it
    out, and so is a sibling that needs one (`offered` false: the watch's
    "small" strap is on a live record but on no chip). A sibling flagged
    inactive is kept but marked, so a chip can decline to link to it — the
    item being viewed is sometimes one of those.
    """
    def listed(value) -> list:
        return value if isinstance(value, list) else []

    details = details if isinstance(details, dict) else {}
    axes = []
    for axis in listed(details.get("variant_attr_scheme")):
        if not isinstance(axis, dict) or not axis.get("code"):
            continue
        values = [{"code": str(v["code"]), "name": text_of(v.get("name")) or str(v["code"])}
                  for v in listed(axis.get("values"))
                  if isinstance(v, dict) and v.get("code")
                  and v.get("status", 1) not in (0, "0", False)]
        if values:
            axes.append({"code": str(axis["code"]),
                         "name": text_of(axis.get("name")) or str(axis["code"]),
                         "values": values})
    shown = {a["code"]: {v["code"] for v in a["values"]} for a in axes}
    options = []
    for option in listed(details.get("variants")):
        if not isinstance(option, dict) or not isinstance(option.get("attr"), dict):
            continue
        code = str(option.get("erp_item_code") or "").strip()
        if code:
            attr = {str(k): str(v) for k, v in option["attr"].items() if v is not None}
            options.append({"item_code": code, "slug": option.get("code") or None,
                            "attr": attr,
                            "active": option.get("isActive") is not False,
                            "offered": all(attr[a] in shown[a] for a in shown if a in attr)})
    return {"axes": axes, "options": options,
            "current": ({str(k): str(v) for k, v in here.items() if v is not None}
                        if isinstance(here, dict) else {})}


def product(body: dict) -> tuple[str, dict]:
    """Specifications and identity. No price lives here, by design."""
    doc = ((body or {}).get("data") or {}).get("product")
    if not isinstance(doc, dict) or not doc:
        return "invalid", {"reason": "no data.product in the response"}

    groups = _spec_groups(doc.get("specifications"))

    category = doc.get("category") or {}
    breadcrumb = [a.get("name") for a in (category.get("ancestors") or [])
                  if isinstance(a, dict) and a.get("name")]
    if category.get("name"):
        breadcrumb.append(category["name"])
    here = (doc.get("variant") or {}).get("attr") if isinstance(doc.get("variant"), dict) else {}
    here = here if isinstance(here, dict) else {}
    # A chooser is decoration: a variant block in a shape nobody expected
    # costs the chips, never the specs, the price gate or the page.
    try:
        variants = _variants(doc.get("variantdetails"), here)
    except Exception:                                          # noqa: BLE001
        variants = {"axes": [], "options": [], "current": {}}

    return "ok", {
        "item_code": str(doc.get("item_code") or doc.get("erp_item_code") or ""),
        "sku": doc.get("sku") or None,
        "name": doc.get("name") or doc.get("description") or None,
        "slug": doc.get("slug") or doc.get("code") or None,
        "brand": (doc.get("brand") or {}).get("name"),
        "model": (doc.get("model") or {}).get("name"),
        # The model's code is what the shop's reviews are filed under.
        "model_code": (doc.get("model") or {}).get("code") or None,
        "family": (doc.get("family") or {}).get("name"),
        "category": category.get("name"),
        "breadcrumb": breadcrumb,
        # The variant attributes, which is what "same phone, 256GB" means.
        "variant": here,
        "variants": variants,
        "image": (doc.get("image") or {}).get("url"),
        # A gallery entry wraps its URL in `media`; the top level carries
        # only an id and a flag. Reading `url` straight off the entry
        # returned None for every photo and left the page with one.
        "gallery": [u for u in
                    ((g.get("media") or {}).get("url") or g.get("url")
                     for g in (doc.get("gallery") or []) if isinstance(g, dict))
                    if u][:8],
        "rating": number(doc.get("starrating") if doc.get("starrating") is not None
                         else doc.get("rating")),
        "review_count": number(doc.get("review_count")),
        "sellable": doc.get("sellable"),
        "active": doc.get("isActive"),
        # The merchandiser's switch for the listing itself. Off, the shop
        # prints "Currently Unavailable" with no price and no buy button,
        # however much stock Central Inventory counts — Sony 54092 had one
        # unit in a branch and a live price while the shop sold nothing.
        "listed": doc.get("isListActive"),
        "specification_groups": groups,
        "specification_count": sum(len(g["attributes"]) for g in groups),
        # The shop's own "Key Specifications" bullets, and the field is
        # misleading twice over: the entries are objects, not strings, and
        # the sentence lives in `name` while `code` holds an unrelated
        # attribute code. Returning the objects raw gave the page nothing
        # to print, so it fell back to the first few specification rows —
        # "Network Type", "SIM Type" — where the shop shows "50MP + 2MP
        # Rear Camera | 32MP Front Camera".
        "highlights": [text_of(h.get("name") if isinstance(h, dict) else h)
                       for h in ((doc.get("specifications") or {})
                                 .get("attributeHighlights") or [])
                       if (h.get("name") if isinstance(h, dict) else h)][:8],
    }


# ── PIM pricing ────────────────────────────────────────────────────────────

def pricing(body: dict, channel: str, now=None) -> tuple[str, dict]:
    """The price in force on one price list, and the MRP it sits under."""
    now = now or datetime.datetime.now(UTC)
    block = ((body or {}).get("data") or {}).get("pricing")
    if not isinstance(block, dict):
        return "invalid", {"reason": "no data.pricing in the response"}

    wanted = channel.upper()
    lists = [b for b in (block.get("prices") or []) if isinstance(b, dict)]
    chosen = next((b for b in lists if str(b.get("pl", "")).upper() == wanted), None)
    # A price list we did not ask for is not a substitute for the one we did.
    # Say which list answered so a SHOP price is never printed as an ONLINE
    # one, and leave the price absent rather than borrow another channel's.
    if chosen is None:
        return "empty", {"reason": f"no {wanted} price list on this product",
                         "price_lists": [b.get("pl") for b in lists]}

    price, since = current(chosen.get("sp"), now)
    mrp, _ = current(((block.get("mrp") or [{}])[0] or {}).get("price"), now)

    percent = None
    if price and mrp and mrp > price:
        percent = int(round((mrp - price) * 100.0 / mrp))

    taxes = [{"name": t.get("name"), "value": number(t.get("value")),
              "unit": t.get("unit"), "type": t.get("trans_type")}
             for t in (block.get("taxclass") or [])
             if isinstance(t, dict) and t.get("isActive")]

    return ("ok" if price is not None else "empty"), {
        "price": price,
        "mrp": mrp,
        "currency": "INR",
        "discount_percent": percent,
        "price_list": chosen.get("pl"),
        "in_force_since": since,
        "price_changes_on_record": len(chosen.get("sp") or []),
        "pre_order": block.get("pre_order"),
        # The merchandiser's instruction not to show a price at all. Ignoring
        # it would print a number the shop itself hides.
        "hide_selling_price": bool(block.get("hideSellingPrice")),
        "taxes": taxes,
        "category": (block.get("category") or {}).get("name"),
    }


# ── Central Inventory ──────────────────────────────────────────────────────

# The states poorvika's own store locator lists (its open area_list call).
_STATE = {"TN": "Tamil Nadu", "KA": "Karnataka", "PY": "Pondicherry"}
# One spelling per place. Branch names spell some the way the counter staff
# say them, and the same town twice ("Velacherry", "Velachery"); a shopper
# should not see two names for one place or miss one by typing the other.
# Keyed on the name with spaces and dots taken out.
_SPELL = {"COVAI": "Coimbatore", "KOVAI": "Coimbatore", "THIRUPUR": "Tiruppur",
          "TIRUPUR": "Tiruppur", "THIRUNELVELI": "Tirunelveli", "TRICHY": "Trichy",
          "VELACHERRY": "Velachery", "ANNANAGAR": "Anna Nagar", "ANNASALAI": "Anna Salai",
          "TNAGAR": "T. Nagar", "DINDIVANAM": "Tindivanam", "VILUPPURAM": "Villupuram",
          "THIRUVANNAMALAI": "Tiruvannamalai", "THIRUVARUR": "Tiruvarur",
          "GUDUVANCHERRY": "Guduvanchery", "PERAMBALLUR": "Perambalur",
          "MOULIVAKKAM": "Mowlivakkam", "BELLARY": "Ballari", "BALLARI": "Ballari",
          "AYNAVARAM": "Ayanavaram"}
# Localities of the three cities whose branch names carry only the locality
# ("TN-ADYAR", "KA-JP NAGAR"). Only places that are unmistakably inside the
# city are listed: a town on its edge (Sriperumbudur, Anekal) keeps no city
# rather than a wrong one.
_IN_CITY = {
    "Chennai": """ADAMBAKKAM ADYAR AMBATTUR AMINJIKARAI ANNANAGAR ARUMBAKKAM AVADI
        AYNAVARAM CHROMEPET CPRAMASAMYROAD EKKATTUTHANGAL IYYAPANTHANGAL KANDANCHAVADI
        KODAMBAKKAM KOLATHUR KUMANANCHAVADI KUNDRATHUR MADIPAKKAM MADURAVOYAL MANALI
        MEDAVAKKAM MGRNAGAR MOGAPPAIR MOOLAKADAI MOULIVAKKAM MOWLIVAKKAM MRNAGAR MYLAPORE
        NANGANALLUR PADI PALAVAKKAM PALLAVARAM PARRYSHIGHCOURTOPP PERAMBUR PERAVALLUR
        PONDYBAZAAR POONAMALLEE PORUR PURASAIVAKKAM REDHILLS ROYAPETTAH SHANTHICOLONY
        SHOLINGANALLUR TAMBARAMEAST TAMBARAMWEST TNAGAR THORAIPAKKAM TIRUVOTTIYUR
        TRIPLICANE VALASARAVAKKAM VELACHERY VELACHERRY VILLIVAKKAM VYASARPADI
        WASHERMENPET WESTMAMBALAM""",
    "Bengaluru": """AREKERE BANASHANKARI BANASWADI BASAVANAGUDI BASAVESHWARANAGAR
        BOMMANAHALLI BTMLAYOUT2NDSTAGE CHANDRALAYOUT CHIKKABANAVARA CITYMARKET
        CKNAGARELECTRONICCITYHOSAROAD CMHROAD DHASARAHALLI FRAZERTOWN GARVEBHAVIPALAYA
        HONGASANDRA JALAHALLI JPNAGAR KAMAKSHIPALYA KAMMANAHALLI KAMMASANDRA KENGERI
        KENGERIMYSOREROAD KORAMANGALA KRPURAM KUMARASWAMYLAYOUT LAGGERE MADIWALA
        MAHALAKSHMILAYOUT MALLESWARAM MATHIKERE MOODALAPALYA PEENYA RAMAMURTHYNAGAR
        RRNAGAR RTNAGAR SAHAKARANAGAR SANJAYNAGAR SUNKADAKATTE TCPALYA TAVAREKERE
        THANISANDRA THIPPASANDRA UTTARAHALLI VIDYARANYAPURA VIGNANANAGAR VIJAYANAGAR
        YELACHENAHALLI YELAHANKA YESWANTHPUR""",
    "Pondicherry": "ANNASALAI JAWAHARNAGAR PROVIDENCE VILLIANUR",
}
_CITY_OF = {key: city for city, keys in _IN_CITY.items() for key in keys.split()}
# Both names a shopper may type for a city: the one on the sign and the one
# in the news. Matched by the store panel's search, never shown.
_ALSO = {"Bengaluru": "bangalore", "Chennai": "madras", "Pondicherry": "puducherry pondy",
         "Mysore": "mysuru", "Mangalore": "mangaluru", "Belgaum": "belagavi",
         "Gulbarga": "kalaburagi", "Hubli": "hubballi", "Shimoga": "shivamogga",
         "Tanjore": "thanjavur", "Trichy": "tiruchirappalli tiruchi", "Tumkur": "tumakuru",
         "Thoothukudi": "tuticorin", "Thenkasi": "tenkasi", "Coimbatore": "covai kovai",
         "Tiruppur": "tirupur thirupur", "Chikmagalur": "chikkamagaluru", "Hospet": "hosapete",
         "Ballari": "bellary", "Vijayapur": "bijapur"}
# A store format, not a place: "TN-COVAI-100FT ROAD-HIFI" is the HiFi store.
_FORMAT = {"HIFI": "HiFi", "LIVE": "Live"}
_VOWEL = re.compile(r"[AEIOUY]")
# Initialisms that do carry a vowel, so the rule below would lower-case them.
_INITIALS = {"OT", "HAL", "HO", "ECR", "OMR", "VOC", "ITI", "LIC", "TTK", "RTO", "YVD"}
_WORD = {"HIFI": "HiFi", "APLS": "Appliances"}
_ORDINAL = re.compile(r"^(\d+)(ST|ND|RD|TH)$")
_FEET = re.compile(r"^(\d+)FT$")


def _key(text: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", str(text or "").upper())


def _words(text: str) -> str:
    """"KOPPIKAR ROAD" -> "Koppikar Road"; "JP NAGAR" -> "JP Nagar".

    A short word with no vowel is an initialism (JP, CMH, HSR, KR) and stays
    upper case, as do the few listed that have one (OT, HAL); "BY" is a word.
    Dotted initials read as initials ("T.NAGAR" -> "T. Nagar"), ordinals as
    ordinals ("2ND" -> "2nd"), and "100FT" as "100 Feet".
    """
    out = []
    for word in text.split():
        if word in _WORD:
            out.append(_WORD[word])
        elif "." in word:
            bits = [b for b in word.split(".") if b]
            initials = [b for b in bits if len(b) == 1]
            rest = [b for b in bits if len(b) > 1]
            out.append("".join(f"{b}." for b in initials)
                       + (" " if initials and rest else "")
                       + " ".join(_words(b) for b in rest))
        elif _ORDINAL.match(word):
            out.append(word.lower())
        elif _FEET.match(word):
            out.append(_FEET.sub(r"\1 Feet", word))
        elif (word in _INITIALS or any(ch.isdigit() for ch in word)
              or (len(word) <= 3 and not _VOWEL.search(word))):
            out.append(word)
        else:
            out.append(word.capitalize())
    return " ".join(out)


def _place(part: str) -> str:
    """One name part in its one spelling, or tidied as written."""
    return _SPELL.get(_key(part)) or _words(part)


def branch_place(name) -> dict:
    """Where a Central Inventory branch is, read off its name.

    The names follow one pattern — state, then an optional showroom kind,
    then city and area: "TN-COVAI-VADAVALLI" is Vadavalli in Coimbatore,
    "TN-A-SALEM-MEYYANUR BY PASS" the appliances showroom on the Meyyanur
    bypass in Salem, "KA-MALL-MANGALORE CITY CENTRE" a mall store, and a
    trailing "HIFI" or "LIVE" the store's format. "Z ... WAREHOUSE" is stock a
    shopper cannot walk in and buy. A one-part name is a locality; its city is
    filled in only for the localities listed above, never guessed. A trailing
    number ("GULBARGA 2") is the branch's own numbering, kept apart as `nth`.
    """
    empty = {"label": None, "state": None, "city": None, "area": None, "kind": None,
             "format": None, "nth": None, "search": ""}
    raw = str(name or "").strip().upper()
    if not raw:
        return empty
    if raw.startswith("Z ") or "WAREHOUSE" in raw:
        bare = raw[2:] if raw.startswith("Z ") else raw
        code = bare[:2]
        rest = bare[3:] if code in _STATE and bare[2:3] == "-" else bare
        return dict(empty, label=_words(rest.replace("-", " ")), state=_STATE.get(code),
                    kind="warehouse")
    parts = [p.strip() for p in raw.split("-") if p.strip()]
    state = _STATE.get(parts[0]) if parts else None
    if state:
        parts = parts[1:]
    kind = "mobile"
    if parts and parts[0] == "A":
        kind, parts = "appliances", parts[1:]
    elif parts and parts[0] == "MALL":
        kind, parts = "mall", parts[1:]
    fmt = None
    if parts:
        tail = parts[-1].split()
        if tail and tail[-1] in _FORMAT:
            fmt = _FORMAT[tail[-1]]
            tail = tail[:-1]
            parts = parts[:-1] + ([" ".join(tail)] if tail else [])
    nth = None
    if parts:
        tail = parts[-1].rsplit(" ", 1)
        if len(tail) == 2 and tail[1].isdigit():
            nth, parts = int(tail[1]), parts[:-1] + [tail[0]]
    city = area = None
    if len(parts) >= 2 and _key(parts[0]) in _CITY_OF:
        # "TN-AMBATTUR-OT JUNCTION": the first part is itself a locality of a
        # listed city, so it belongs with the area — "OT Junction, Ambattur".
        city = _CITY_OF[_key(parts[0])]
        area = ", ".join([" ".join(_place(p) for p in parts[1:]), _place(parts[0])])
    elif len(parts) >= 2:
        city = _place(parts[0])
        area = " ".join(_place(p) for p in parts[1:])
    elif parts:
        area = _place(parts[0])
        city = _CITY_OF.get(_key(parts[0]))
    label = (f"{area}, {city}" if city and area and city != area
             else (area or city or _words(raw)))
    search = " ".join(x for x in (label, raw.replace("-", " "), city, area, state,
                                  _ALSO.get(city or ""), _ALSO.get(area or ""),
                                  _ALSO.get(state or ""), fmt) if x).lower()
    return {"label": label, "state": state, "city": city, "area": area, "kind": kind,
            "format": fmt, "nth": nth, "search": search}


def _number_branches(branches: list) -> None:
    """Tell apart branches that would otherwise read the same.

    "Gulbarga" and "Gulbarga 2" are two stores; a shopper sees "Gulbarga" and
    "Gulbarga · Store 2". A lone "Arekere 1" is just "Arekere".
    """
    seen = {}
    for b in branches:
        if b.get("kind") != "warehouse" and b.get("label"):
            seen.setdefault((b["label"], b.get("kind")), []).append(b)
    for group in seen.values():
        if len(group) < 2:
            continue
        for b in group:
            if b.get("nth"):
                b["label"] = f"{b['label']} · Store {b['nth']}"


def stock(body: dict) -> tuple[str, dict]:
    """Live quantity, and branch-level detail when the gateway gives it.

    `{"message": "signature is invalid"}` arrives with HTTP 200. Reading
    that as zero is how a stocked product gets a Notify Me button, so an
    unreadable body is INVALID and the caller is told we could not ask.
    """
    data = (body or {}).get("data")
    if not isinstance(data, dict):
        return "invalid", {"reason": str((body or {}).get("message")
                                        or "no data block in the response")[:160]}

    quantity = number(data.get("stock"))
    if quantity is None:
        return "invalid", {"reason": "no stock figure in the response"}

    branches = [dict({"code": b.get("branchcode"), "name": b.get("branchname"),
                      "quantity": number(b.get("quantity"))},
                     **branch_place(b.get("branchname")))
                for b in (data.get("stock_data") or [])
                if isinstance(b, dict)]
    _number_branches(branches)
    counted = sum(b["quantity"] or 0 for b in branches)

    out = {
        "in_stock": quantity > 0,
        "quantity": quantity,
        "actual_stock": number(data.get("actual_stock")),
        "channel_limit": number(data.get("stock_limit")),
        "branches": branches,
        "branch_count": len(branches),
    }
    # The two numbers are not always the same thing — a channel limit can cap
    # what the branches hold. Report the gap instead of picking a winner.
    if branches and counted != quantity:
        out["branch_total"] = counted
        out["note"] = ("the channel figure and the branch totals differ; "
                       "`quantity` is what the gateway says is sellable")
    return "ok", out


_LI = re.compile(r"<li[^>]*>(.*?)</li>", re.S | re.I)
# The gateway writes "Delivery by 2:7 PM Today"; the shop prints "02:07 PM today".
_CLOCK = re.compile(r"\b(\d{1,2}):(\d{1,2})\s*([AP]M)\b", re.I)


def _when(text: str) -> str:
    text = _CLOCK.sub(lambda m: f"{int(m.group(1)):02d}:{int(m.group(2)):02d} "
                                f"{m.group(3).upper()}", text)
    return re.sub(r"\bToday\b", "today", text)


def delivery(body: dict, pincode: str) -> tuple[str, dict]:
    """How fast it can reach one pincode, as Central Inventory quotes it.

    The shop's "Delivery Options" box, from the same call its page makes:
    Regular Delivery with a date range, 2 hours Delivery with a clock time.
    An unserviceable or unknown pincode answers `data: []` — that is "we do
    not deliver there", not a failure. Each mode's note arrives as styled
    HTML; only its bullet points are kept, as plain text.
    """
    rows = (body or {}).get("data")
    if not isinstance(rows, list):
        return "invalid", {"reason": str((body or {}).get("message")
                                        or "no data list in the response")[:160]}
    options = []
    for row in rows:
        if not isinstance(row, dict) or not row.get("mode"):
            continue
        options.append({
            "mode": text_of(row.get("mode")),
            "when": _when(text_of(row.get("delivery_time"))),
            "charges": number(row.get("charges")),
            "icon": row.get("icon"),
            "notes": [t for t in (text_of(li) for li in
                                  _LI.findall(row.get("info") or "")) if t],
        })
    return ("ok" if options else "empty"), {"pincode": pincode,
                                            "options": options}


# ── Other products: bought together, related ───────────────────────────────

def _shown_price(entry: dict, channel: str, now) -> tuple:
    """(price the card shows, MRP) for a product listed beside this one.

    The same rule as the product page: the channel's price list in force,
    less any activation offer live today — PIM carries both on the entry.
    """
    wanted = channel.upper()
    lists = [b for b in (entry.get("prices") or []) if isinstance(b, dict)]
    chosen = next((b for b in lists if str(b.get("pl", "")).upper() == wanted),
                  None)
    price, _ = current((chosen or {}).get("sp"), now)
    mrp, _ = current(((entry.get("mrp") or [{}])[0] or {}).get("price"), now)
    if price is not None:
        price -= sum(number(a.get("discountAmount")) or 0
                     for a in (entry.get("activation_offer") or [])
                     if isinstance(a, dict)
                     and live_now(a.get("startDate"), a.get("endDate"), now))
    if entry.get("hideSellingPrice"):
        price = None
    return price, mrp


def _listed(entries, channel: str, now, with_specs: bool) -> list:
    out = []
    for entry in entries or []:
        if not isinstance(entry, dict):
            continue
        code = str(entry.get("item_code") or entry.get("erp_item_code") or "")
        if not code:
            continue
        price, mrp = _shown_price(entry, channel, now)
        row = {"item_code": code,
               "name": entry.get("name"),
               "slug": entry.get("slug") or entry.get("code"),
               "image": (entry.get("image") or {}).get("url") or entry.get("thumb"),
               "price": price,
               "mrp": mrp}
        if with_specs:
            row["specification_groups"] = _spec_groups(entry.get("specifications"))
        out.append(row)
    return out


def _product_list(body: dict, field: str, channel: str, now,
                  with_specs: bool) -> tuple[str, dict]:
    """One of PIM's product lists. A product with no such list answers
    `{"status": 404, "error": "no data found"}` — an empty shelf, not a fault."""
    now = now or datetime.datetime.now(UTC)
    data = (body or {}).get("data")
    if not isinstance(data, dict):
        if (body or {}).get("status") == 404:
            return "empty", {"items": []}
        return "invalid", {"reason": f"no data.{field} in the response"}
    items = _listed(data.get(field), channel, now, with_specs)
    return ("ok" if items else "empty"), {"items": items}


def together(body: dict, channel: str, now=None) -> tuple[str, dict]:
    """The shop's "Frequently Bought Together" shelf."""
    return _product_list(body, "boughttogether", channel, now, False)


def related(body: dict, channel: str, now=None) -> tuple[str, dict]:
    """Related products, each with its specifications for the compare table."""
    return _product_list(body, "relatedproducts", channel, now, True)


# ── Offers ─────────────────────────────────────────────────────────────────

# Measured across 87 live offers on 30 production products. Every one of
# these came back from the estate; nothing here is invented, and an
# unrecognised type is carried through under its own name rather than being
# forced into one of ours.
OFFER_KINDS = {
    "standard emi": "emi_availability",
    "instant discount": "instant_discount",
    "instant discount auto": "instant_discount_auto",
    "sellout/activation offers": "activation_offer",
    "offer text": "message",
    "standard discount": "standard_discount",
}

_BANK = re.compile(r"\bbank\b|\bcard[s]?\b", re.I)


def _basis(row: dict, label: dict) -> dict:
    """What the number means. Unit first, value second, cap third."""
    stated = number(row.get("discountValue"))
    unit = ((row.get("discountValues") or {}).get("type")
            or label.get("discountUnit") or "")
    value = number((row.get("discountValues") or {}).get("value"))
    if value is None:
        value = number(label.get("discountValue"))
    cap = number(label.get("maxBenefites"))
    kind = {"flatdiscount": "flat", "percentage": "percentage"}.get(
        str(unit).lower().replace(" ", ""), "unstated")

    out = {"basis": kind, "value": value, "cap": cap, "stated_amount": stated}
    if kind == "percentage" and value:
        # The rupee figure the detail endpoint returns for a percentage offer
        # is the CAP, not the discount: 10% capped at ₹1,000 arrives as
        # 1000, and on a ₹5,000 product the shopper gets ₹500. So the honest
        # phrase is "up to", and the amount is left for whoever knows the
        # price and the eligibility — which is not this function.
        out["note"] = ("a percentage offer: `stated_amount` is the cap, not "
                       "the discount")
    return out


def _benefit(basis: dict):
    """The rupees this offer is actually worth, as the engine resolved them.

    `offers/` has already done the arithmetic — a 7.5% offer capped at
    ₹2,000 on a ₹38,999 phone arrives as 2000, not 7.5 and not 2924 — so
    `stated_amount` is the number to trust. The cap is the fallback for a
    label-only offer, where nobody has resolved anything yet.
    """
    stated = basis.get("stated_amount")
    if stated:
        return stated
    value, cap = basis.get("value"), basis.get("cap")
    if basis.get("basis") == "flat" and value:
        return min(value, cap) if cap else value
    return cap or None


def price_block(pricing: dict, offers_block: dict, channel: str) -> dict:
    """The shop's own price block, rebuilt from the shop's own two sources.

    Two numbers the page shows, built from two kinds of offer:

    The SELLING price is what the shopper is shown: the list less any live
    "Sellout/Activation" offer on this channel (`shouldReduceOnPrice`). Do not
    check this against the shop's `sprice` field — that is the LIST, and the
    shop's page takes the activation off in the browser. Comparing with
    `sprice` on 2026-11-07 wrongly concluded the shop had stopped subtracting.
    The rendered page, re-read 2026-11-09: boAt Rockerz Zen 54734 ₹1,399
    (1,999 − 600), Lenovo LOQ 55854 ₹1,60,990 (1,96,990 − 36,000), Sony
    WF-C710N 50360 ₹6,990 (8,990 − 2,000), its store figure off the same 2,000.

    The EFFECTIVE price underneath is the selling price minus the single best
    card offer (`reduceEffective`). Card offers are **alternatives, not a
    stack** — you pay with one card — so the shop shows the best single one,
    never their sum. With no card offer the effective price is the selling
    price itself: the shop still prints it under both channels.
    An offer tagged EMI-only (payment.transaction_types == ['emi']) still
    counts: the shop's own "Effective Price @ Online" includes it — Bosch fridge
    55453 shows ₹1,03,990 (1,08,990 − the EMI-tagged ₹5,000) and the PS5 54486
    ₹66,490 (69,990 − ₹3,500), checked on poorvika.com 2026-11-09.
    """
    listed = pricing.get("price")
    if listed is None:
        return {"status": "no price"}

    mine = [o for o in (offers_block.get("offers") or [])
            if not o.get("channels") or channel.lower() in o["channels"]]

    # Sellout/activation offers come off the ticket on the channel that
    # carries them — the price the shopper sees (see the docstring on `sprice`).
    off_the_ticket = [o for o in mine if o.get("reduces_price") and o.get("amount")]
    activation = sum(o["amount"] for o in off_the_ticket)
    selling = listed - activation

    cards = [o for o in mine if o.get("reduces_effective")
             and not o.get("reduces_price") and o.get("amount")]
    best = max(cards, key=lambda o: o["amount"]) if cards else None

    mrp = pricing.get("mrp")
    percent = None
    if mrp and selling and mrp > selling:
        percent = int(round((mrp - selling) * 100.0 / mrp))

    block = {
        "mrp": mrp,
        "list_price": listed,
        "selling_price": selling,
        "discount_percent": percent,
        "channel": channel,
        "inclusive_of_taxes": True,
        # How long this price has stood, which PIM knows and nobody was
        # printing. A price set yesterday and one unchanged for two years
        # are different facts about the same number.
        "since": pricing.get("in_force_since"),
        "changes": pricing.get("price_changes_on_record"),
        # The merchandiser's instruction not to show a price at all.
        "hide_selling_price": bool(pricing.get("hide_selling_price")),
    }
    if activation:
        block["activation_discount"] = activation
        block["activation_offers"] = [
            {"offer_id": o["offer_id"], "amount": o["amount"], "label": o["label"]}
            for o in off_the_ticket]
    block["effective_price"] = selling - (best["amount"] if best else 0)
    block["offers_considered"] = len(cards)
    # Every card offer, best first — the shop's "Calculate Effective Price"
    # sheet lets the shopper pick their own bank's instead of the best one.
    block["card_offers"] = [{"offer_id": o["offer_id"], "amount": o["amount"],
                             "label": o["label"], "payment": o.get("payment")}
                            for o in sorted(cards, key=lambda o: -o["amount"])]
    if best:
        block["best_offer"] = {"offer_id": best["offer_id"],
                               "amount": best["amount"],
                               "label": best["label"],
                               "payment": best.get("payment")}
        block["basis"] =("the single best card offer on this channel, not a "
                          "total — these are alternatives and do not stack")
    return block


def offers(detail: dict, labels: dict, channel: str, now=None) -> tuple[str, dict]:
    """The two offer endpoints are two projections of one answer.

    Verified on 30 production products: `getOffers` and `offers/` return the
    same offer set, matched by `offerId`, with the same type counts. They do
    not carry the same fields, so neither replaces the other —
    `offers/` holds the display copy and the payment eligibility, `getOffers`
    holds the unit, the cap, the validity window and the Active flag. We
    merge them by id and say which side each fact came from.
    """
    now = now or datetime.datetime.now(UTC)
    rows = (detail or {}).get("data")
    index: dict[str, dict] = {}
    listed = ((labels or {}).get("data") or {}).get("offerList")
    for row in (listed or []):
        if isinstance(row, dict) and row.get("offerId"):
            index[str(row["offerId"])] = row

    if not isinstance(rows, list):
        # The detail endpoint said nothing we can read. The label endpoint
        # alone is still worth something, so fall back to it rather than
        # claiming the product has no offers.
        if not index:
            return "invalid", {"reason": "neither offer endpoint returned a list"}
        rows = []

    wanted = channel.lower()
    out, skipped = [], {"wrong_channel": 0, "expired": 0, "inactive": 0}
    seen = set()

    def channels_of(row) -> list:
        values = row.get("channel") or []
        names = []
        for value in values:
            if isinstance(value, dict):
                names.append(str(value.get("name", "")).lower())
            else:
                names.append(str(value).lower())
        return [n for n in names if n]

    for row in rows:
        if not isinstance(row, dict):
            continue
        offer_id = str(row.get("offerId") or "")
        label = index.get(offer_id, {})
        seen.add(offer_id)
        names = channels_of(row) or channels_of(label)
        if names and wanted not in names:
            skipped["wrong_channel"] += 1
            continue
        if label and str(label.get("status", "Active")).lower() != "active":
            skipped["inactive"] += 1
            continue
        if label and not live_now(label.get("startDate"), label.get("endDate"), now):
            skipped["expired"] += 1
            continue

        raw_type = str(row.get("offers_type") or label.get("offerType") or "").strip()
        copy = text_of(row.get("offer_text"))
        payment = {k: v for k, v in (
            ("card_types", row.get("payment_type") or []),
            ("transaction_types", row.get("transaction_type") or []),
            ("gateways", row.get("payment_gateway") or []))if v}

        kind = OFFER_KINDS.get(raw_type.lower(), "other")
        basis = _basis(row, label)
        title, body = offer_parts(row.get("offer_text"))
        offer = {
            "offer_id": offer_id,
            "kind": kind,
            "stated_type": raw_type,
            "label": copy,
            "title": title or None,
            "text": body or None,
            "discount": basis,
            # What the rule engine says this offer DOES to the price. The
            # shop's own product page is built from these two flags, and
            # they do different jobs: see `price_block`.
            "amount": _benefit(basis),
            "reduces_price": bool(label.get("shouldReduceOnPrice")
                                  and kind == "activation_offer"),
            "reduces_effective": bool(label.get("reduceEffective")),
            "terms": text_of(row.get("t_c") or label.get("termsConditions")),
            "channels": names,
            "section": row.get("section_type"),
            "banner": bool(row.get("bannerEnable")),
        }
        if payment:
            offer["payment"] = payment
        # The bank is named in the copy and nowhere else — there is no
        # structured issuer field in either response. Say that it is a
        # text match rather than pretending to a field we do not have.
        if _BANK.search(copy or ""):
            offer["mentions_bank_in_text"] = True
        if label:
            offer["valid_until"] = label.get("endDate")
            offer["offer_name"] = label.get("offerName")
        if row.get("minValue") not in (None, ""):
            offer["minimum_spend"] = number(row.get("minValue"))
        if row.get("maxValue") not in (None, ""):
            offer["maximum_benefit"] = number(row.get("maxValue"))
        out.append(offer)

    # A label-only offer still counts. It happens when the detail endpoint
    # omits a row the rule engine lists.
    for offer_id, label in index.items():
        if offer_id in seen:
            continue
        names = channels_of(label)
        if names and wanted not in names:
            skipped["wrong_channel"] += 1
            continue
        if not live_now(label.get("startDate"), label.get("endDate"), now):
            skipped["expired"] += 1
            continue
        kind = OFFER_KINDS.get(str(label.get("offerType", "")).lower(), "other")
        basis = _basis({}, label)
        markup = label.get("bannerDiscription") or label.get("offerDescription")
        title, body = offer_parts(markup)
        out.append({
            "offer_id": offer_id,
            "kind": kind,
            "stated_type": label.get("offerType"),
            "label": text_of(markup),
            "title": title or None,
            "text": body or None,
            "discount": basis,
            "amount": _benefit(basis),
            "reduces_price": bool(label.get("shouldReduceOnPrice")
                                  and kind == "activation_offer"),
            "reduces_effective": bool(label.get("reduceEffective")),
            "terms": text_of(label.get("termsConditions")),
            "channels": names,
            "valid_until": label.get("endDate"),
            "offer_name": label.get("offerName"),
            "labels_only": True,
        })

    buckets: dict[str, list] = {}
    for offer in out:
        buckets.setdefault(offer["kind"], []).append(offer)

    return ("ok" if out else "empty"), {
        "offers": out,
        "count": len(out),
        "by_kind": {k: len(v) for k, v in buckets.items()},
        "skipped": {k: v for k, v in skipped.items() if v},
        # Nothing in either response says whether two offers can be taken
        # together, so nothing here adds them up. Said out loud because the
        # temptation to sum a column of discounts is considerable.
        "stacking": "unknown — the estate publishes no combination rules, so "
                    "these are not additive and no effective price is derived",
    }


# ── NBFC finance ───────────────────────────────────────────────────────────

def finance(body: dict, channel: str, now=None) -> tuple[str, dict]:
    """EMI schemes, grouped by the company that funds them.

    NBFC — a Non-Banking Financial Company, Bajaj Finance and DMI Finance
    being the ones this estate names. The endpoint answers HTTP 404 with
    `"status": "success"` and `data: []` for a product with no schemes, so
    404 here is EMPTY and not a failure: measured on 30 production products,
    all 30 answered exactly that way.

    `interest` is passed through untouched. The samples hold 5 and 12 with
    no unit anywhere in the response, and reading either as an annual rate
    would be a guess printed as a number.
    """
    now = now or datetime.datetime.now(UTC)
    rows = (body or {}).get("data")
    if rows is None and (body or {}).get("status") is None:
        return "invalid", {"reason": "no data block in the response"}
    if not rows:
        return "empty", {"providers": [], "scheme_count": 0,
                         "reason": "this product has no finance schemes"}

    wanted = channel.lower()
    providers, dropped = [], 0
    for group in rows:
        if not isinstance(group, dict):
            continue
        schemes = []
        for scheme in (group.get("schemas") or []):
            if not isinstance(scheme, dict):
                continue
            names = [str((c or {}).get("name", "")).lower()
                     for c in (scheme.get("channel") or [])
                     if isinstance(c, dict)]
            if scheme.get("isActive") is False or names and wanted not in names:
                dropped += 1
                continue
            if not live_now(scheme.get("startDate"), scheme.get("endDate"), now):
                dropped += 1
                continue
            schemes.append({
                "finance_name": scheme.get("financeName"),
                "scheme_name": scheme.get("schemeName"),
                "total_emi_months": number(scheme.get("totalEmiMonths")),
                # `initalEmiMonths` upstream. The misspelling is kept at the
                # boundary and corrected once, here, so nothing downstream
                # has to remember it.
                "initial_emi_months": number(scheme.get("initalEmiMonths")),
                "emi_per_month": number(scheme.get("emiPerMonth")),
                "initial_payment": number(scheme.get("initialPayment")),
                "processing_fees": number(scheme.get("processingFees")),
                "interest": number(scheme.get("interest")),
                "interest_unit": None,
                "total_emi_amount": number(scheme.get("totalEmiAmt")),
                "valid_until": scheme.get("endDate"),
                "brand": scheme.get("brand"),
            })
        if schemes:
            schemes.sort(key=lambda s: (s["emi_per_month"] or 0))
            providers.append({"provider": group.get("_id"),
                              "scheme_count": len(schemes), "schemes": schemes})

    total = sum(p["scheme_count"] for p in providers)
    return ("ok" if total else "empty"), {
        "providers": providers,
        "scheme_count": total,
        **({"dropped_not_live_or_other_channel": dropped} if dropped else {}),
        "totals_note": "every figure is the one the finance company published; "
                       "nothing is recalculated and `interest` carries no "
                       "unit in the source",
    }


# ── EMI, from the calculator the shop itself calls ─────────────────────────

def emi(body: dict) -> tuple[str, dict]:
    """Bank EMI plans — the line the shop's product page leads with.

    This is not the NBFC endpoint and does not replace it. NBFC lists
    finance *companies* (Bajaj, DMI) and answered 404 on all 30 production
    products measured; this lists the **card** plans a bank will give, and
    it is what "Standard EMI starts from ₹1,105/month for HDFC Bank Debit
    Card" is built from.

    The response holds several kinds of EMI, and they are not comparable:

        No Cost EMI    6 months, the interest handed back as cashback
        Low Cost EMI   9-24 months, part of the interest handed back
        Standard EMI   3-48 months, 20 card providers, interest as charged

    A No Cost plan over 6 months has the largest instalment of the three
    and is the *cheapest* offer on the page, so "the smallest number wins"
    is wrong here. Each kind keeps its own cheapest, and the headline is
    the Standard one — the same one the shop quotes.
    """
    groups = ((body or {}).get("data") or {}).get("emiDetails")
    if not isinstance(groups, list):
        return "invalid", {"reason": "no data.emiDetails in the response"}

    kinds, total = [], 0
    for group in groups:
        if not isinstance(group, dict):
            continue
        providers, cheapest = [], None
        for provider in (group.get("providers") or []):
            plans = []
            for plan in (provider.get("tenureDetails") or []):
                monthly = number(plan.get("monthlyInstallment"))
                if not monthly:
                    continue
                one = {
                    "months": number(plan.get("tenure")),
                    "per_month": monthly,
                    # Left as the bank's own string — "14.00%" — because
                    # turning it into a number invites recomputing the
                    # schedule from it, and the bank's total is already here.
                    "rate": plan.get("rateOfInterest"),
                    "total_payable": number(plan.get("totalEMIAmountPayable")),
                    "interest": number(plan.get("totalInterestPayToBank")),
                }
                for field, name in (("instantDiscount", "instant_discount"),
                                    ("totalCashbackOffered", "cashback")):
                    if number(plan.get(field)):
                        one[name] = number(plan.get(field))
                if plan.get("cashbackRemark"):
                    one["cashback_note"] = plan["cashbackRemark"]
                plans.append(one)
                if cheapest is None or monthly < cheapest["per_month"]:
                    cheapest = {"per_month": monthly,
                                "months": number(plan.get("tenure")),
                                "provider": provider.get("name"),
                                "type": group.get("type")}
            if plans:
                plans.sort(key=lambda x: x["per_month"])
                providers.append({"provider": provider.get("name"),
                                  "plan_count": len(plans), "plans": plans})
        if providers:
            providers.sort(key=lambda x: x["plans"][0]["per_month"])
            total += len(providers)
            kinds.append({"type": group.get("type"),
                          "supplier": group.get("supplier"),
                          "provider_count": len(providers),
                          "starts_from": cheapest,
                          "providers": providers})

    if not kinds:
        return "empty", {"types": [], "reason": "no EMI plans at this price"}

    standard = next((k for k in kinds
                     if "standard" in str(k.get("type", "")).lower()), kinds[0])
    return "ok", {
        "headline": standard["starts_from"],
        "types": kinds,
        "kind_count": len(kinds),
        "provider_count": total,
        "note": "quoted by the bank through the shop's own calculator; "
                "nothing here is recalculated",
    }


# ── what comes with the product, and what can be added to it ──────────────

# The heading the shop prints over each kind of plan.
_PLAN_GROUP = {"INSURANCE": "Accidental and Liquid Damage Protection Plan",
               "WARRANTY": "Extended Warranty"}


def policy(policy_body: dict, addons_body: dict, price=None,
           mrp=None) -> tuple[str, dict]:
    """The assurance strip and the protection plans, both from PIM.

    The strip was hardcoded on the product page — "GST invoice: available" —
    which was true for the one product it was written against and an
    invention for the rest. It is per product and per variant, and PIM says
    so: a MacBook carries "Shipping Charges / Conditions apply" where a
    phone carries "Replacement / in 7 days".

    A plan's price depends on `planType`, and getting that wrong prints
    "₹20" for a plan that costs ₹8,600:

        FLAT        planPrice is rupees
        PERCENTAGE  planPrice is a percent OF THE SELLING PRICE

    Verified against the shop on 2026-11-04: a 20% plan reads ₹8,600 on a
    ₹42,999 phone and ₹7,800 on a ₹38,999 one, both exact. The figure the shop
    strikes through a percentage plan is `planMrpPrice` percent of the
    product's MRP — Samsung Galaxy A08 55503: 20% of ₹26,999 = ₹5,400 struck
    over ₹3,700, checked 2026-11-09 — and through a flat plan, `planMrpPrice`
    when it is the higher of the two.
    """
    rows = ((policy_body or {}).get("data") or {}).get("addons")
    assurances = []
    for row in (rows or []):
        if not isinstance(row, dict):
            continue
        # Two gates, and a product can fail either: the policy is off for
        # the product, or off for this variant of it.
        if row.get("product_policy_status") is False or \
                row.get("variant_policy_status") is False:
            continue
        title = (row.get("title") or "").strip()
        if not title:
            continue
        assurances.append({
            "title": title,
            "detail": (row.get("sub_title") or "").strip(),
            "terms": text_of(row.get("tc") or row.get("description")),
            "icon": row.get("policyimage"),
        })

    plans = []
    for row in (((addons_body or {}).get("data") or {}).get("addons") or []):
        if not isinstance(row, dict):
            continue
        stated, kind = number(row.get("planPrice")), str(row.get("planType") or "")
        struck_at = number(row.get("planMrpPrice"))
        amount, struck, basis = None, None, kind.lower() or "unstated"
        if kind.upper() == "FLAT":
            amount, struck = stated, struck_at
        elif kind.upper() == "PERCENTAGE":
            if stated and price:
                amount = int(round(float(price) * stated / 100.0))
            if struck_at and mrp:
                struck = int(round(float(mrp) * struck_at / 100.0))
        kind_of = str(row.get("type") or "").upper()
        plans.append({
            "name": text_of(row.get("description")),
            "price": amount,
            "mrp": struck if (struck and amount and struck > amount) else None,
            "basis": basis,
            "percent": stated if kind.upper() == "PERCENTAGE" else None,
            "group": _PLAN_GROUP.get(kind_of) or (kind_of.title() or "Protection Plan"),
            "type": row.get("type"),
            "plan_code": row.get("planCode"),
            "terms": text_of(row.get("terms_condition")),
        })

    if not assurances and not plans:
        return "empty", {"assurances": [], "plans": [],
                         "reason": "no policy or protection plan on this product"}
    return "ok", {"assurances": assurances, "plans": plans,
                  "plan_note": "a percentage plan is priced off the selling "
                               "price and struck off the MRP"}


# ── Ratings and reviews ────────────────────────────────────────────────────

def plain_text(value) -> str:
    """Text a customer typed: nothing to strip but extra spaces.

    `text_of` is for offer copy, which arrives as HTML and is trimmed of
    bullets and slashes; run over a review it would eat "<3" and a closing
    "/". The page escapes everything it prints, so plain text is safe.
    """
    if not value or not isinstance(value, str):
        return ""
    return _WS.sub(" ", value.replace("\xa0", " ")).strip()


def _review(row: dict) -> dict | None:
    """One published review, as the shop's page prints it."""
    if not isinstance(row, dict) or not row.get("_id") or row.get("publish") is False:
        return None
    # The summary names the writer in `customerName` and keeps a customer
    # number in `customer`; the list puts the name in `customer` itself.
    name = row.get("customerName") or row.get("customer")
    text = row.get("text") if isinstance(row.get("text"), str) else ""
    return {
        "id": str(row["_id"]),
        "rating": number(row.get("rating")),
        "title": plain_text(row.get("title")),
        # Paragraphs kept: a review is read as the customer wrote it.
        "text": [p for p in (plain_text(line) for line in text.split("\n")) if p],
        "label": plain_text(row.get("labels")) or None,
        "customer": plain_text(name) if isinstance(name, str) else None,
        "recommended": row.get("recommended") == 1,
        "likes": int(number(row.get("likeCount")) or 0),
        "dislikes": int(number(row.get("disLikeCount")) or 0),
        "written": row.get("createdAt"),
        # Which variant it was written on: reviews are shared across them.
        "variant_slug": row.get("slug") or None,
    }


def reviews(details: dict, listing: dict) -> tuple[str, dict]:
    """The model's rating, its star histogram, and the latest reviews.

    Two calls the shop's page makes: `getReviewDetails` for the averages and
    the most positive and negative review, `review` for the list. Filed by
    model, so the Teal iPhone 16 carries the review written on the Black one
    — which is what poorvika.com shows, and why PIM's per-item rating of 0
    said "be the first to review" under a product with a five-star review.
    """
    details = details if isinstance(details, dict) else {}
    listing = listing if isinstance(listing, dict) else {}
    data = details.get("data")
    if not isinstance(data, dict):
        return "invalid", {"reason": str(details.get("message")
                                        or "no data in the review summary")[:160]}
    avg = data.get("ratingAvg") if isinstance(data.get("ratingAvg"), dict) else {}
    stars = {n: int(number(avg.get(f"{word}StarRatings")) or 0)
             for n, word in ((5, "five"), (4, "four"), (3, "three"),
                             (2, "two"), (1, "one"))}
    count = int(number(avg.get("reviewCount")) or 0)
    rows = listing.get("data")
    listed = [r for r in (_review(x) for x in (rows if isinstance(rows, list) else [])) if r]
    said = data.get("frequentlyMentioned")
    mentioned = [plain_text(m.get("name") if isinstance(m, dict) else m)
                 for m in (said if isinstance(said, list) else [])
                 if isinstance(m, (dict, str))]
    out = {
        "average": number(avg.get("avgRating")),
        "count": count,
        "recommended_percent": number(avg.get("recommended")),
        "stars": stars,
        "most_positive": _review(data.get("mostPositive")),
        "most_negative": _review(data.get("mostNegative")),
        "reviews": listed,
        "total_listed": int(number(listing.get("reviewCount")) or len(listed)),
        "frequently_mentioned": [m for m in mentioned if m],
    }
    return ("ok" if count or listed else "empty"), out

