import os
import json
import asyncio
import re
import html
from difflib import SequenceMatcher
from datetime import datetime
from typing import Dict, List, Tuple, Optional
from aiohttp import web

from aiogram import Bot, Dispatcher, F
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton, FSInputFile
from aiogram.filters import CommandStart
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.context import FSMContext

try:
    from openai import AsyncOpenAI
except ImportError:
    AsyncOpenAI = None

TOKEN = os.environ.get("PARFERA_BOT_TOKEN")
if not TOKEN:
    raise RuntimeError("Не задан PARFERA_BOT_TOKEN")
OPENAI_API_KEY = os.environ.get("PARFERA_OPENAI_API_KEY")
OPENAI_MODEL = os.environ.get("PARFERA_OPENAI_MODEL", "gpt-5.6-luna")
OPENAI_CLIENT = AsyncOpenAI(api_key=OPENAI_API_KEY) if (OPENAI_API_KEY and AsyncOpenAI) else None
ADMIN_CHAT_ID = os.environ.get("PARFERA_ADMIN_CHAT_ID")

with open("catalog.json", encoding="utf-8") as f:
    PRODUCTS = json.load(f)["products"]



def norm(text: str) -> str:
    text = str(text or "").lower().replace("ё", "е")
    return re.sub(r"\s+", " ", text).strip()


def rub(value):
    if value in (None, "", 0):
        return None
    return f"{int(value):,}".replace(",", " ") + " ₽"


def clean_product_name(name: str) -> str:
    """Human-friendly fragrance name for the Telegram shop."""
    name = str(name or "").strip()
    # Keep the supplier naming visible for now, as requested.
    return re.sub(r"\s+", " ", name).strip(" -")


def display_name(p):
    return clean_product_name(p.get("name", ""))


def fragrance_title(p):
    """Compact customer-facing title: fragrance + concentration."""
    raw = clean_product_name(p.get("name", ""))
    brand = BRAND_DISPLAY.get(BRAND_FOR_ID.get(p.get("id"), ""), "") if "BRAND_FOR_ID" in globals() else ""
    if brand:
        raw = re.sub(r"^" + re.escape(brand) + r"\s+", "", raw, flags=re.I)
    raw = re.sub(r"\s+TESTER\b", "", raw, flags=re.I)
    raw = re.sub(r"\s+(?:без крышки|с крышкой|пробник)\b", "", raw, flags=re.I)
    raw = re.sub(r"\s+\d+(?:[.,]\d+)?\s*ml\b", "", raw, flags=re.I)
    raw = re.sub(r"\s*\((?:m|w|u)\)\b", "", raw, flags=re.I)
    raw = re.sub(r"\s*\+.*$", "", raw)
    raw = re.sub(r"\s+", " ", raw).strip(" -·")
    m = re.search(r"\b(edp|edt|parfum|parfume|extrait|eau de parfum|eau de toilette)\b", raw, re.I)
    concentration = None
    if m:
        concentration = m.group(1).upper().replace("PARFUME", "PARFUM")
        raw = (raw[:m.start()] + raw[m.end():]).strip(" -·")
    if not raw:
        raw = display_name(p)
    return f"{raw} · {concentration}" if concentration else raw


def group_key(p):
    # Group one fragrance into one card. Volume, TESTER and supplier condition
    # markers are variants, not separate fragrances. Concentration (EDT/EDP/EDP
    # etc.) remains part of the key so genuinely different formulations do not merge.
    name = str(p.get("base_name") or p.get("name", ""))
    name = re.sub(r"\s+TESTER\b", "", name, flags=re.I)
    name = re.sub(r"\s+(?:без крышки|с крышкой)\b", "", name, flags=re.I)
    name = re.sub(r"\s+пробник\b", "", name, flags=re.I)
    name = re.sub(r"\s+\d+(?:[.,]\d+)?\s*ml\b", "", name, flags=re.I)
    name = re.sub(r"\s*\((?:m|w|u)\)\b", "", name, flags=re.I)
    return norm(name)


# Client-facing filter: show only recognizable perfume bottles/testers in normal sizes.
# Supplier rows for miniatures, samples, cosmetics and gift bundles stay in the raw
# catalog but are hidden from search, brand lists and product option buttons.
HIDDEN_VARIANT_MARKERS = (
    "body lotion", "shower gel", "after shave", "af/sh", "shampoo",
    "deo", "deodorant", "body spray", "body wash", "body lotion", "body cream",
    "cosm", "cosmetic", "shampoo", "conditioner", "hand wash", "hand cream",
    "soap", "hair", "bath", "shower", "lotion", "cream", "balm", "gel",
    "candle", "diffuser", "home fragrance", "mini", "male minis", "female minis",
    "set ", "gift set", "набор",
)

def variant_is_client_friendly(p):
    name = norm(p.get("name", ""))
    if any(marker in name for marker in HIDDEN_VARIANT_MARKERS):
        return False
    # Bundles such as "+ shower gel" are not individual perfume options.
    if " + " in name or name.startswith("+"):
        return False
    # Exclude personal-care and home-fragrance products even when they have a normal volume.
    if re.search(r"\b(?:body|shower|bath|hair|hand|face|soap|wash|lotion|cream|balm|gel|shampoo|conditioner|deodorant|candle|diffuser)\b", name, re.I):
        return False
    m = re.search(r"(\d+(?:[.,]\d+)?)\s*ml\b", name, re.I)
    if not m:
        return False
    volume = float(m.group(1).replace(",", "."))
    return 30 <= volume <= 200

def visible_variants(items):
    return [p for p in items if variant_is_client_friendly(p)]

def visible_group(item):
    p = BY_ID[item] if isinstance(item, str) else item
    return visible_variants(GROUPS.get(group_key(p), [p]))

SEARCH_TEXT = {p["id"]: norm(f'{p.get("name", "")} {p.get("article", "")}') for p in PRODUCTS}
BY_ID = {p["id"]: p for p in PRODUCTS}
PAGE_SIZE = 10
USER_SEARCH: Dict[int, str] = {}
CARTS: Dict[int, List[dict]] = {}

# One fragrance = one group containing all its volumes/testers.
GROUPS: Dict[str, List[dict]] = {}
for p in PRODUCTS:
    GROUPS.setdefault(group_key(p), []).append(p)


def sort_variants(items):
    def vol_num(p):
        m = re.search(r"(\d+(?:[.,]\d+)?)\s*ml", p.get("name", ""), re.I)
        return float(m.group(1).replace(",", ".")) if m else 9999
    def variant_order(p):
        return 1 if p.get("tester") or p.get("tester_price_rub") else 0
    return sorted(items, key=lambda p: (vol_num(p), variant_order(p), p.get("name", "")))


def unique_variants(items):
    """Remove duplicate supplier rows that have identical visible purchase options."""
    out = []
    seen = set()
    for p in sort_variants(items):
        vol = p.get("volume") or ""
        b = p.get("bottle_price_rub")
        t = p.get("tester_price_rub")
        # Some supplier rows can repeat the same bottle/tester option.
        key = (norm(vol), b, t, bool(p.get("tester")))
        if key in seen:
            continue
        seen.add(key)
        out.append(p)
    return out


def lowest_group_price(items):
    prices = []
    for p in items:
        if p.get("bottle_price_rub"):
            prices.append(int(p["bottle_price_rub"]))
        if p.get("tester_price_rub"):
            prices.append(int(p["tester_price_rub"]))
    return min(prices) if prices else None


# ---------- Automatic brand detection ----------
# The supplier file has no dedicated brand column.  Use a curated list of
# multi-word brand prefixes first; only then fall back to the first token.
# This prevents false brands such as MAX, ANTONIO, ACQUA, MAISON, THE, etc.
# from being shown as separate brands when the real brand is multi-word.
BRAND_PREFIXES = {
    'MAX AZRIA': 'Max Azria',
    'MAX MARA': 'Max Mara',
    'MAX PHILIP': 'Max Philip',
    'ANTONIO BANDERAS': 'Antonio Banderas',
    'HERMES': 'Hermès',
    'ORMONDE JAYNE': 'Ormonde Jayne',
    'ACQUA DI BIELLA': 'Acqua di Biella',
    'ACQUA DI PARMA': 'Acqua di Parma',
    'KEIKO MECHERI': 'Keiko Mecheri',
    'KARL LAGERFELD': 'Karl Lagerfeld',
    'RAMON BEJAR': 'Ramon Bejar',
    'FILIPPO SORCINELLI': 'Filippo Sorcinelli',
    'JO LOVES': 'Jo Loves',
    'JO MALONE': 'Jo Malone',
    'BOIS 1920': 'Bois 1920',
    'DR. GRITTI': 'Dr. Gritti',
    'DR. VRANJES': 'Dr. Vranjes',
    'RICHARD DEBOR': 'Richard Debor',
    'RICHARD JAMES': 'Richard James',
    'RICHARD MAISON DE PARFUM': 'Richard Maison de Parfum',
    'MARC JACOBS': 'Marc Jacobs',
    'GIAN MARCO VENTURI': 'Gian Marco Venturi',
    'BRUNO ACAMPORA': 'Bruno Acampora',
    'JACQUES BOGART': 'Jacques Bogart',
    'ANNA ROZENMEER': 'Anna Rozenmeer',
    'ANNA SUI': 'Anna Sui',
    'THOMAS DE MONACO': 'Thomas de Monaco',
    'THOMAS KOSMALA': 'Thomas Kosmala',
    'STEPHANE HUMBERT LUCAS': 'Stephane Humbert Lucas',
    'ALEXANDRE J': 'Alexandre J',
    'ROSENDO MATEU': 'Rosendo Mateu',
    'MARC-ANTOINE BARROIS': 'Marc-Antoine Barrois',
    'JEROBOAM': 'Jeroboam',
    'JOVOY PARIS': 'Jovoy Paris',
    'TAUER PERFUMES': 'Tauer Perfumes',
    'FRAGRANCE DU BOIS': 'Fragrance Du Bois',
    'LABORATORIO OLFATTIVO': 'Laboratorio Olfattivo',
    'ARABIAN OUD': 'Arabian Oud',
    'GRAHAM & POTT': 'Graham & Pott',
    'MILLER HARRIS': 'Miller Harris',
    'L ARTISAN': 'L’Artisan Parfumeur',
    'COMME DES GARCONS': 'Comme des Garçons',
    'MAISON CATALIYA': 'Maison Cataliya',
    'MAISON REBATCHI': 'Maison Rebatchi',
    'PARFUMS BDK PARIS': 'Parfums BDK Paris',
    'PARFUMS MDCI': 'Parfums MDCI',
    'PARFUMS DUSITA': 'Parfums Dusita',
    'PARFUMS SOPHISTE': 'Parfums Sophiste',
    'THE HARMONIST': 'The Harmonist',
    'THE GATE': 'The Gate',
    'EMPORIO ARMANI': 'Emporio Armani',
    'DRIES VAN NOTEN': 'Dries Van Noten',
    'GIANFRANCO FERRE': 'Gianfranco Ferré',
    'LORENZO VILLORESI': 'Lorenzo Villoresi',
    'FRANCESCA DELL ORO': 'Francesca Dell’Oro',
    'ELIE SAAB': 'Elie Saab',
    'JOHN VARVATOS': 'John Varvatos',
    'J-SCENT': 'J-Scent',
    'NINA RICCI': 'Nina Ricci',
    'VICTORIA S SECRET': 'Victoria’s Secret',
    'ZADIG & VOLTAIRE': 'Zadig & Voltaire',
    'MONCLER': 'Moncler',
    'COURREGES': 'Courrèges',
    'MENDITTOROSA': 'Mendittorosa',
    'BINET-PAPILLON': 'Binet-Papillon',
    'CHRISTIAN DIOR': 'CHRISTIAN DIOR',
    'CHRISTIAN LOUBOUTIN': 'CHRISTIAN LOUBOUTIN',
    'MAISON FRANCIS KURKDJIAN': 'MAISON FRANCIS KURKDJIAN',
    'MAISON CRIVELLI': 'MAISON CRIVELLI',
    'MAISON TAHITE': 'MAISON TAHITE',
    'MAISON MARTIN MARGIELA': 'MAISON MARTIN MARGIELA',
    'MAISON VIOLET': 'MAISON VIOLET',
    'MAISON CATALIYA': 'MAISON CATALIYA',
    'MAISON REBATCHI': 'MAISON REBATCHI',
    'THE DIFFERENT COMPANY': 'THE DIFFERENT COMPANY',
    'THE MERCHANT OF VENICE': 'THE MERCHANT OF VENICE',
    'THE WOODS COLLECTION': 'THE WOODS COLLECTION',
    'THE HARMONIST': 'THE HARMONIST',
    'THE HOUSE OF OUD': 'THE HOUSE OF OUD',
    'THE GATE': 'THE GATE',
    'PARFUMS DE MARLY': 'PARFUMS DE MARLY',
    'PARFUMS BDK PARIS': 'PARFUMS BDK PARIS',
    'PARFUMS ET SENTEURS DU PAYS': 'PARFUMS ET SENTEURS DU PAYS',
    'PARFUMS MDCI': 'PARFUMS MDCI',
    'PARFUMS DE NICOLAI': 'PARFUMS DE NICOLAI',
    'PARFUMS DUSITA': 'PARFUMS DUSITA',
    'PARFUMS SOPHISTE': 'PARFUMS SOPHISTE',
    'PARFUMS 137': 'PARFUMS 137',
    'LES LIQUIDES IMAGINAIRES': 'LES LIQUIDES IMAGINAIRES',
    'LES EAUX PRIMORDIALES': 'LES EAUX PRIMORDIALES',
    'LES SOEURS DE NOE': 'LES SOEURS DE NOE',
    'LES FLEURS DE BACH': 'LES FLEURS DE BACH',
    'LES BAINS GUERBOIS': 'LES BAINS GUERBOIS',
    'LES CONTES D ORIENT': 'LES CONTES D ORIENT',
    'LES COPAINS': 'LES COPAINS',
    'LES DESTINATIONS': 'LES DESTINATIONS',
    'PIERRE GUILLAUME': 'PIERRE GUILLAUME',
    'PIERRE BALMAIN': 'PIERRE BALMAIN',
    'PIERRE CARDIN': 'PIERRE CARDIN',
    'FRENCH AVENUE': 'FRENCH AVENUE',
    'AL HARAMAIN': 'AL HARAMAIN',
    'AL JAZEERA': 'AL JAZEERA',
    'AL ATTAAR': 'AL ATTAAR',
    'ATELIER COLOGNE': 'ATELIER COLOGNE',
    'ATELIER DES ORS': 'ATELIER DES ORS',
    'ATELIER FLOU': 'ATELIER FLOU',
    'ATELIER MATERI': 'ATELIER MATERI',
    'EX NIHILO': 'EX NIHILO',
    'LE LABO': 'LE LABO',
    'LE BONHEUR': 'LE BONHEUR',
    'LE GALION': 'LE GALION',
    'LA SULTANE DE SABA': 'LA SULTANE DE SABA',
    'LA MAISON DE LA VANILLE': 'LA MAISON DE LA VANILLE',
    'LA MANUFACTURE': 'LA MANUFACTURE',
    'LA BOUCHE ROUGE': 'LA BOUCHE ROUGE',
    'HOUSE OF BRANDT': 'HOUSE OF BRANDT',
    'M.INT': 'M.INT',
    'V CANTO': 'V CANTO',
    'HAUTE FRAGRANCE COMPANY': 'HAUTE FRAGRANCE COMPANY',
    '12 PARFUMEURS FRANCAIS': '12 PARFUMEURS FRANCAIS',
    'TOM FORD': 'TOM FORD',
    'GIORGIO ARMANI': 'GIORGIO ARMANI',
    'DOLCE & GABBANA': 'DOLCE & GABBANA',
    'CAROLINA HERRERA': 'CAROLINA HERRERA',
    'CALVIN KLEIN': 'CALVIN KLEIN',
    'PACO RABANNE': 'PACO RABANNE',
    'HUGO BOSS': 'HUGO BOSS',
    'JIMMY CHOO': 'JIMMY CHOO',
    'JEAN PAUL GAULTIER': 'JEAN PAUL GAULTIER',
    'RALPH LAUREN': 'RALPH LAUREN',
    'ELIZABETH ARDEN': 'ELIZABETH ARDEN',
    'SALVATORE FERRAGAMO': 'SALVATORE FERRAGAMO',
    'THIERRY MUGLER': 'THIERRY MUGLER',
    'NARCISO RODRIGUEZ': 'NARCISO RODRIGUEZ',
    'ESTEE LAUDER': 'ESTEE LAUDER',
    'MICHAEL KORS': 'MICHAEL KORS',
    'VIKTOR & ROLF': 'VIKTOR & ROLF',
    'ISSEY MIYAKE': 'ISSEY MIYAKE',
    'FRANCK BOCLET': 'FRANCK BOCLET',
    'FREDERIC MALLE': 'FREDERIC MALLE',
    'CLAUDE ANDRIE': 'CLAUDE ANDRIE',
    'JULIETTE HAS A GUN': 'JULIETTE HAS A GUN',
    'MARC-ANTOINE BARROIS': 'MARC-ANTOINE BARROIS',
    'MARC ANTOINE BARROIS': 'MARC ANTOINE BARROIS',
    'LOUIS VUITTON': 'LOUIS VUITTON',
    'SERGE LUTENS': 'SERGE LUTENS',
    'MEMO PARIS': 'MEMO PARIS',
    'HISTOIRES DE PARFUMS': 'HISTOIRES DE PARFUMS',
    'COMPTOIR SUD PACIFIQUE': 'COMPTOIR SUD PACIFIQUE',
    'ETAT LIBRE D ORANGE': 'ETAT LIBRE D ORANGE',
    'ELECTIMUSS': 'ELECTIMUSS',
    'MONTBLANC': 'MONTBLANC',
    'DIPTYQUE': 'DIPTYQUE',
    'PENHALIGONS': 'PENHALIGONS',
    'BYREDO': 'BYREDO',
    'XERJOFF': 'XERJOFF',
    'AMOUAGE': 'AMOUAGE',
    'VERSACE': 'VERSACE',
    'CHANEL': 'CHANEL',
    'GUERLAIN': 'GUERLAIN',
    'MONTALE': 'MONTALE',
    'MANCERA': 'MANCERA',
    'KILIAN': 'KILIAN',
    'AJMAL': 'AJMAL',
    'ARMAF': 'ARMAF',
    'BVLGARI': 'BVLGARI',
    'GUCCI': 'GUCCI',
    'GIVENCHY': 'GIVENCHY',
    'LATTAFA': 'LATTAFA',
    'ROJA': 'ROJA',
    'CREED': 'CREED',
    'PRADA': 'PRADA',
    'CHLOE': 'CHLOE',
    'CARTIER': 'CARTIER',
    'KENZO': 'KENZO',
    'BURBERRY': 'BURBERRY',
    'TRUSSARDI': 'TRUSSARDI',
    'LACOSTE': 'LACOSTE',
    'NISHANE': 'NISHANE',
    'INITIO': 'INITIO',
    'AZZARO': 'AZZARO',
    'LANCOME': 'LANCOME',
    'LALIQUE': 'LALIQUE',
    'ROCHAS': 'ROCHAS',
    'DAVIDOFF': 'DAVIDOFF',
    'COACH': 'COACH',
    'MOSCHINO': 'MOSCHINO',
    'ESCADA': 'ESCADA',
    'BOND': 'BOND',
    'CHOPARD': 'CHOPARD',
    'ZARKOPERFUME': 'ZARKOPERFUME',
    'NICOLAI': 'NICOLAI',
    'FLORAIKU': 'FLORAIKU',
    'PUREDISTANCE': 'PUREDISTANCE',
    'BOADICEA': 'BOADICEA',
    'MIZENSIR': 'MIZENSIR',
    'MASQUE': 'MASQUE',
    'VILHELM': 'VILHELM',
    'FRAGONARD': 'FRAGONARD',
    'CLIVE CHRISTIAN': 'CLIVE CHRISTIAN',
    'ATKINSONS': 'ATKINSONS',
    'ARGOS': 'ARGOS',
    'MORESQUE': 'MORESQUE',
    'BORTNIKOFF': 'BORTNIKOFF',
    'CARON': 'CARON',
    'COQUILLETE': 'COQUILLETE',
    'PERRIS': 'PERRIS',
    'ORLOV': 'ORLOV',
    'SUPERZ': 'SUPERZ',
    'ARTEOLFATTO': 'ARTEOLFATTO',
    'BOUCHERON': 'BOUCHERON',
    'JUSBOX': 'JUSBOX',
    'NOBILE': 'NOBILE',
    'ESSENTIAL': 'ESSENTIAL',
    'VAN CLEEF & ARPELS': 'VAN CLEEF & ARPELS',
    'JACQUES FATH': 'JACQUES FATH',
    'EMANUEL UNGARO': 'EMANUEL UNGARO',
    'STEFANO RICCI': 'STEFANO RICCI',
    'ROBERTO CAVALLI': 'ROBERTO CAVALLI',
    'SARAH BAKER': 'SARAH BAKER',
    'JENNY GLOW': 'JENNY GLOW',
    'BANANA REPUBLIC': 'BANANA REPUBLIC',
    'LENGling': 'LENGLING',
    'LENGLING': 'LENGLING',
    'DRIES VAN NOTEN': 'DRIES VAN NOTEN',
    'OLFACTIVE STUDIO': 'OLFACTIVE STUDIO',
    'VINCE CAMUTO': 'VINCE CAMUTO',
    'AMOUROUD': 'AMOUROUD',
    'REINE DE SABA': 'REINE DE SABA',
    'ANFAS': 'ANFAS',
    'EMPORIO ARMANI': 'EMPORIO ARMANI',
    'GIARDINO SEGRETO': 'GIARDINO SEGRETO',
    'REGALIEN': 'REGALIEN',
    'ZIMAYA': 'ZIMAYA',
    'ALGHABRA': 'ALGHABRA',
    'OLIBANUM': 'OLIBANUM',
    'THEODOROS KALOTINIS': 'THEODOROS KALOTINIS',
    'BYBOZO': 'BYBOZO',
    'FRANCESCA DELL ORO': 'FRANCESCA DELL ORO',
    'ILLUMINUM': 'ILLUMINUM',
    'PARLE MOI DE PARFUM': 'PARLE MOI DE PARFUM',
    'SALVADOR DALI': 'SALVADOR DALI',
    'GUESS': 'GUESS',
    'ATTAR COLLECTION': 'ATTAR COLLECTION',
    'KENNETH COLE': 'KENNETH COLE',
    'COSTUME NATIONAL': 'COSTUME NATIONAL',
    'ESCENTRIC MOLECULES': 'ESCENTRIC MOLECULES',
    'POLICE': 'POLICE',
    'DUNHILL': 'DUNHILL',
    'LOEWE': 'LOEWE',
    'MATIERE PREMIERE': 'MATIERE PREMIERE',
    'OSCAR DE LA RENTA': 'OSCAR DE LA RENTA',
    'PATRICE RIVIERE': 'PATRICE RIVIERE',
    'RASASI': 'RASASI',
    'RICHARD': 'RICHARD',
    'RANCE': 'RANCE',
    'MILLER HARRIS': 'MILLER HARRIS',
    'STEPHANIE DE BRUIJN': 'STEPHANIE DE BRUIJN',
    'AREEJ AL AMEER': 'AREEJ AL AMEER',
    'LM PARFUMS': 'LM PARFUMS',
    'ARD AL ZAAFARAN': 'ARD AL ZAAFARAN',
    'GRAHAM & POTT': 'GRAHAM & POTT',
    'ELLA K': 'ELLA K',
    'DAVID JOURQUIN': 'DAVID JOURQUIN',
    'GIANFRANCO FERRE': 'GIANFRANCO FERRE',
    'LORENZO VILLORESI': 'LORENZO VILLORESI',
    'GGEMA': 'GGEMA',
    'EMPEROR': 'EMPEROR',
    'JUL': 'JUL',
    'NINA RICCI': 'NINA RICCI',
    'WELTON LONDON': 'WELTON LONDON',
}

def canonical_token(token: str) -> str:
    # Ignore punctuation differences such as DR. / DR and apostrophes.
    return re.sub(r"[^A-Z0-9À-ÿА-Яа-яЁё]", "", str(token).upper())

def word_tokens(text: str) -> List[str]:
    return re.findall(r"[A-Za-z0-9À-ÿА-Яа-яЁё&'\-]+", str(text or "").upper())

PREFIX_ITEMS = sorted(
    [(tuple(canonical_token(x) for x in k.split()), v) for k, v in BRAND_PREFIXES.items()],
    key=lambda item: -len(item[0])
)

def detect_brand(name: str) -> str:
    tokens = word_tokens(name)
    if not tokens:
        return "UNKNOWN"
    canon = tuple(canonical_token(x) for x in tokens)
    for prefix, display in PREFIX_ITEMS:
        if len(canon) >= len(prefix) and canon[:len(prefix)] == prefix:
            return display
    return tokens[0]

BRAND_FOR_ID: Dict[str, str] = {}
BRAND_DISPLAY: Dict[str, str] = {}
for p in PRODUCTS:
    key = detect_brand(p.get("name", ""))
    BRAND_FOR_ID[p["id"]] = key
    BRAND_DISPLAY.setdefault(key, key.title())

# Client-facing catalog only: a brand is listed when it has at least one
# normal perfume fragrance after the variant filter.


BRANDS: Dict[str, List[dict]] = {}
for p in PRODUCTS:
    BRANDS.setdefault(BRAND_FOR_ID[p["id"]], []).append(p)

def valid_brand_key(key: str) -> bool:
    label = BRAND_DISPLAY.get(key, key).strip()
    letters = re.sub(r"[^A-Za-zА-Яа-яЁё]", "", label)
    if len(letters) < 2:
        return False
    if not re.search(r"[A-Za-zА-Яа-яЁё]", label):
        return False
    return True

BRAND_KEYS = sorted([k for k in BRANDS if valid_brand_key(k)], key=lambda x: BRAND_DISPLAY[x].lower())
BRAND_ID_TO_KEY = {str(i): key for i, key in enumerate(BRAND_KEYS)}
BRAND_KEY_TO_ID = {key: str(i) for i, key in enumerate(BRAND_KEYS)}

# Pre-build one-fragrance groups per brand for fast browsing.
BRAND_GROUPS: Dict[str, List[List[dict]]] = {}
for bkey, products in BRANDS.items():
    seen = set()
    groups = []
    for p in products:
        gk = group_key(p)
        if gk not in seen and visible_variants(GROUPS[gk]):
            seen.add(gk)
            groups.append(visible_variants(GROUPS[gk]))
    groups.sort(key=lambda items: display_name(next((x for x in items if not x.get("tester") and x.get("bottle_price_rub")), items[0])).lower())
    BRAND_GROUPS[bkey] = groups

# Product images: one image per fragrance + concentration + gender.
# Volume and tester variants reuse the same image.
IMAGE_MAP = {
    "versace eros edt (m)": "versace_eros_edt.jpg",
    "versace eros edp (m)": "versace_eros_edp.jpg",
    "versace eros parfume (m)": "versace_eros_parfum.jpg",
    "versace eros flame edp (m)": "versace_eros_flame.jpg",
    "versace eros energy edp (m)": "versace_eros_energy.jpg",
    "versace eros najim parfume (m)": "versace_eros_najim.jpg",
    "versace eau fraiche edt (m)": "versace_eau_fraiche.jpg",
    "versace eau fraiche extreme edp (m)": "versace_eau_fraiche_extreme.jpg",
    "versace pour homme dylan blue pour homme edt (m)": "versace_dylan_blue.jpg",
    "versace pour homme edt (m)": "versace_pour_homme.jpg",
    "versace pour homme oud noir edp (m)": "versace_oud_noir.jpg",
    "versace pour femme dylan turquoise edt (w)": "versace_dylan_turquoise.jpg",
    "versace pour femme dylan purple edp (w)": "versace_dylan_purple.jpg",
    "versace bright crystal edt (w)": "versace_bright_crystal.jpg",
    "versace bright crystal parfum (w)": "versace_bright_crystal_parfum.jpg",
    "versace bright crystal absolu edp (w)": "versace_bright_crystal_absolu.jpg",
    "versace crystal noir edt (w)": "versace_crystal_noir.jpg",
    "versace crystal noir parfum (w)": "versace_crystal_noir_parfum.jpg",
    "versace yellow diamond edt (w)": "versace_yellow_diamond.jpg",
    "versace crystal emerald edp (w)": "versace_crystal_emerald.jpg",
    "versace woman edp (w)": "versace_woman.jpg",
}
for group_name, filename in IMAGE_MAP.items():
    for p in GROUPS.get(norm(group_name), []):
        p["image_url"] = os.path.join("images", filename)


AI_HISTORY: Dict[int, List[dict]] = {}
AI_LAST_RESULTS: Dict[int, List[str]] = {}
AI_MAX_HISTORY = 8

AI_SYSTEM_PROMPT = """
Ты — PARFERA AI, персональный парфюмерный консультант магазина PARFERA.
Твоя задача — не просто искать текст, а понять желание клиента и подобрать ему реальные ароматы из каталога.

ГЛАВНОЕ ПРАВИЛО:
- Никогда не придумывай товар, цену, объём или наличие.
- Клиенту можно показывать ТОЛЬКО те товары, которые вернул search_catalog.
- Если хочешь предложить известный тебе аромат как кандидата, сначала проверь его через search_catalog.
- Для описательного запроса («свежий женский на осень», «сладкий подарок», «похожее на Erba Pura») используй свои знания о парфюмерии, чтобы выбрать несколько КОНКРЕТНЫХ названий-кандидатов, затем проверь каждый кандидат через search_catalog.
- Для запроса с названием аромата сначала ищи именно название, а не весь текст запроса.
- Можно сделать несколько вызовов search_catalog за один запрос клиента, чтобы проверить 3–6 кандидатов.
- Если кандидат не найден, не показывай его и попробуй следующий.
- Если после проверки подходящих товаров нет, честно скажи, что в текущем каталоге подходящего варианта не найдено.
- Если клиент спрашивает «похожее на X», сначала найди X, затем предложи несколько реально найденных альтернатив. Не утверждай точную идентичность, если нет достаточных данных.
- Учитывай пол, бюджет, объём, сезон, повод, настроение и желаемый характер аромата, если клиент их указал.
- Если клиент не указал важную деталь, не задавай длинную анкету: лучше предложи 3–5 вариантов или задай один короткий вопрос.
- Отвечай на русском, дружелюбно и премиально, как живой консультант бутика.
- Не начинай каждый ответ с «В каталоге есть». Говори естественно: «Я бы посмотрел…», «Для вашего запроса хорошо подходят…».
- Не перегружай ответ. Обычно достаточно 3–5 рекомендаций.
- В результатах показывай только парфюмерию: eau de parfum, eau de toilette, parfum, extrait и другие полноценные ароматы. Не показывай body wash, lotion, cream, gel, shampoo, deodorant, свечи, диффузоры, наборы и другую косметику.
- Если найдено несколько вариантов одного аромата, выбирай наиболее релевантные парфюмерные версии и не засоряй выдачу дублями.
- Используй HTML-разметку Telegram: <b>жирный</b>, <i>курсив</i>. Не используй Markdown ** или __.
- Название, концентрацию, объём и цену бери только из результатов search_catalog.
"""

AI_STOPWORDS = {
    "и", "или", "на", "для", "мне", "мужской", "мужская", "мужское", "женский", "женская", "женское",
    "унисекс", "аромат", "аромата", "ароматы", "хочу", "нужен", "нужна", "нужно", "подбери", "подобрать",
    "посоветуй", "порекомендуй", "ищу", "найди", "есть", "что", "чтобы", "до", "руб", "рублей", "рубля",
    "р", "лет", "года", "год", "осень", "зима", "весна", "лето", "подарок", "подарка", "похожее", "похожий",
    "похожая", "похожие", "свежий", "свежая", "свежие", "сладкий", "сладкая", "сладкие", "легкий", "легкая",
    "тяжелый", "тяжелая", "теплый", "теплая", "насыщенный", "насыщенная", "цветочный", "цветочная", "древесный",
    "древесная", "мускусный", "мускусная", "вечерний", "вечерняя", "дневной", "дневная", "офис", "работу",
    "бергамотом", "бергамот", "ванилью", "ваниль", "розой", "роза", "мускусом", "мускус", "уда", "удом"
}

QUERY_ALIASES = {
    "флер наркотик": "fleur narcotique",
    "флер наркотик ex nihilo": "ex nihilo fleur narcotique",
    "флер наркoтик": "fleur narcotique",
    "кирке": "kirke",
    "кирка": "kirke",
    "теренци кирке": "tiziana terenzi kirke",
    "эрба пура": "erba pura",
    "эрба пура": "erba pura",
    "экс нихило": "ex nihilo",
    "крид авентус": "creed aventus",
    "авентус": "aventus",
}

RU_TO_EN = str.maketrans({
    "а":"a","б":"b","в":"v","г":"g","д":"d","е":"e","ё":"e","ж":"zh","з":"z","и":"i","й":"y",
    "к":"k","л":"l","м":"m","н":"n","о":"o","п":"p","р":"r","с":"s","т":"t","у":"u","ф":"f",
    "х":"kh","ц":"ts","ч":"ch","ш":"sh","щ":"shch","ъ":"","ы":"y","ь":"","э":"e","ю":"yu","я":"ya"
})

def normalize_ai_query(text: str) -> str:
    q = norm(text).replace("’", "'")
    for src, dst in sorted(QUERY_ALIASES.items(), key=lambda x: len(x[0]), reverse=True):
        if src in q:
            return q.replace(src, dst)
    return q


def ai_search_tokens(text: str) -> List[str]:
    q = normalize_ai_query(text)
    # Preserve Latin product names and also transliterate Cyrillic queries.
    latin = q.translate(RU_TO_EN)
    raw_tokens = re.findall(r"[a-z0-9]+", latin)
    return [w for w in raw_tokens if len(w) > 1 and w not in AI_STOPWORDS]


def token_similarity(a: str, b: str) -> float:
    if a == b:
        return 1.0
    if a in b or b in a:
        return 0.88
    return SequenceMatcher(None, a, b).ratio()


def ai_candidate_search(query: str = "", brand: str = "", gender: str = "", max_price: Optional[int] = None,
                        volume: Optional[int] = None, limit: int = 8) -> List[dict]:
    """Deterministic catalog search. It may use fuzzy matching, but never invents inventory."""
    q = normalize_ai_query(query)
    tokens = ai_search_tokens(query)
    brand_q = normalize_ai_query(brand)
    brand_tokens = ai_search_tokens(brand) if brand else []
    gender_q = gender.lower().strip()
    scored = []

    for p in PRODUCTS:
        if not variant_is_client_friendly(p):
            continue
        raw = norm(p.get("name", ""))
        raw_latin = raw.translate(RU_TO_EN)
        bkey = BRAND_FOR_ID.get(p.get("id"), "")
        blabel = norm(BRAND_DISPLAY.get(bkey, bkey))
        blabel_latin = blabel.translate(RU_TO_EN)

        if brand_q and brand_q not in blabel and brand_q not in raw and brand_q.translate(RU_TO_EN) not in raw_latin:
            if brand_tokens and not all(any(token_similarity(bt, rt) >= 0.72 for rt in re.findall(r"[a-z0-9]+", blabel_latin)) for bt in brand_tokens):
                continue

        if gender_q in {"m", "male", "м", "мужской"} and not re.search(r"\(m\)", raw, re.I):
            continue
        if gender_q in {"w", "female", "ж", "женский"} and not re.search(r"\(w\)", raw, re.I):
            continue
        if gender_q in {"u", "unisex", "унисекс"} and re.search(r"\((?:m|w)\)", raw, re.I):
            continue

        if volume is not None:
            m = re.search(r"(\d+(?:[.,]\d+)?)\s*ml\b", raw, re.I)
            if not m or int(float(m.group(1).replace(",", "."))) != int(volume):
                continue

        prices = [int(v) for v in (p.get("bottle_price_rub"), p.get("tester_price_rub")) if v]
        if max_price is not None and (not prices or min(prices) > int(max_price)):
            continue

        score = 0.0
        if q:
            if q in raw or q in blabel:
                score += 100
            elif q.translate(RU_TO_EN) in raw_latin:
                score += 95
            raw_tokens = re.findall(r"[a-z0-9]+", raw_latin)
            if tokens:
                sims = [max(token_similarity(t, rt) for rt in raw_tokens) for t in tokens]
                # Exact/fuzzy name search: all meaningful tokens should be reasonably represented.
                if all(v >= 0.70 for v in sims):
                    score += 30 + sum(sims) * 8
                elif any(v >= 0.78 for v in sims):
                    score += 8 + max(sims) * 5
                else:
                    continue
        else:
            score = 1.0

        gk = group_key(p)
        # Prefer the cheapest visible bottle/tester only as a deterministic tie-breaker.
        group = unique_variants(visible_group(p))
        rep = next((x for x in group if x.get("bottle_price_rub")), p)
        price = min([int(p.get("bottle_price_rub") or 10**9), int(p.get("tester_price_rub") or 10**9)])
        scored.append((score, price, fragrance_title(rep).lower(), gk, rep))

    # One card per fragrance/concentration/gender.
    best_by_group = {}
    for row in scored:
        gk = row[3]
        if gk not in best_by_group or row[:3] > best_by_group[gk][:3]:
            best_by_group[gk] = row
    rows = list(best_by_group.values())
    rows.sort(key=lambda x: (-x[0], x[1], x[2]))
    return [x[4] for x in rows[:max(1, min(int(limit or 8), 12))]]


def ai_tool_result(candidates: List[dict]) -> dict:
    out = []
    for p in candidates:
        prices = []
        if p.get("bottle_price_rub"):
            prices.append(f"флакон {p.get('volume','')} — {rub(p['bottle_price_rub'])}")
        if p.get("tester_price_rub"):
            prices.append(f"тестер {p.get('volume','')} — {rub(p['tester_price_rub'])}")
        out.append({
            "id": p["id"],
            "brand": BRAND_DISPLAY.get(BRAND_FOR_ID.get(p["id"], ""), ""),
            "name": fragrance_title(p),
            "supplier_name": p.get("name", ""),
            "prices": prices,
            "gender": "мужской" if re.search(r"\(m\)", p.get("name", ""), re.I) else "женский" if re.search(r"\(w\)", p.get("name", ""), re.I) else "унисекс"
        })
    return {"count": len(out), "items": out}


async def ai_assist(uid: int, user_text: str) -> Tuple[str, List[dict]]:
    """Run PARFERA AI with verified catalog search results only."""
    if OPENAI_CLIENT is None:
        return ("🤖 <b>Умный помощник пока не подключён.</b>\n\nНо поиск по каталогу уже работает. Нажмите «🔎 Поиск» или напишите название бренда/аромата.", [])

    history = AI_HISTORY.setdefault(uid, [])
    history.append({"role": "user", "content": user_text})
    history[:] = history[-AI_MAX_HISTORY:]

    tool = {
        "type": "function",
        "name": "search_catalog",
        "description": "Проверить конкретный бренд, аромат или кандидата по реальному каталогу PARFERA. Для описательного запроса сначала выбери конкретные названия-кандидаты из своих знаний и проверяй их по одному. Для описательного запроса сразу проверь 3–5 конкретных кандидатов; несколько вызовов инструмента можно выполнять одновременно.",
        "strict": True,
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Конкретное название аромата или бренда для проверки. Не передавай сюда весь описательный запрос клиента."},
                "brand": {"type": "string", "description": "Бренд, если его нужно отдельно ограничить. Иначе пустая строка."},
                "gender": {"type": "string", "enum": ["", "m", "w", "u"], "description": "m мужской, w женский, u унисекс. Иначе пустая строка."},
                "max_price": {"anyOf": [{"type": "integer"}, {"type": "null"}], "description": "Максимальная цена в рублях, если клиент её указал; иначе null."},
                "volume": {"anyOf": [{"type": "integer"}, {"type": "null"}], "description": "Желаемый объём в мл, если указан; иначе null."},
                "limit": {"type": "integer", "minimum": 1, "maximum": 12}
            },
            "required": ["query", "brand", "gender", "max_price", "volume", "limit"],
            "additionalProperties": False
        }
    }

    input_items = [{"role": "system", "content": AI_SYSTEM_PROMPT}] + history
    final_text = ""
    collected: List[dict] = []
    seen_ids = set()

    for _ in range(3):
        response = await OPENAI_CLIENT.responses.create(model=OPENAI_MODEL, input=input_items, tools=[tool], parallel_tool_calls=True)
        calls = [x for x in response.output if getattr(x, "type", "") == "function_call"]
        if not calls:
            final_text = response.output_text or "Не удалось сформировать ответ. Попробуйте уточнить запрос."
            break
        input_items += response.output
        for call in calls:
            try:
                args = json.loads(call.arguments or "{}")
            except json.JSONDecodeError:
                args = {}
            candidates = ai_candidate_search(
                query=str(args.get("query") or ""),
                brand=str(args.get("brand") or ""),
                gender=str(args.get("gender") or ""),
                max_price=args.get("max_price"),
                volume=args.get("volume"),
                limit=min(int(args.get("limit") or 5), 5),
            )
            for p in candidates:
                # Deduplicate by fragrance/concentration/gender, not by volume/tester row.
                group_id = group_key(p)
                if group_id not in seen_ids:
                    seen_ids.add(group_id)
                    collected.append(p)
            # Keep the consultant result compact and useful.
            if len(collected) > 5:
                collected = collected[:5]
            payload = json.dumps(ai_tool_result(candidates), ensure_ascii=False)
            input_items.append({"type": "function_call_output", "call_id": call.call_id, "output": payload})

    if not final_text and collected:
        # Do the final answer in a clean, separate text-only request.
        # Do NOT reuse the previous tool-call objects here: some Responses API
        # models can keep requesting tools instead of emitting final text when
        # the full tool-call history is passed back in.
        try:
            verified = ai_tool_result(collected[:5])
            final_prompt = (
                "Ты — PARFERA AI. Сформируй окончательный ответ клиенту на русском.\n"
                f"Запрос клиента: {user_text}\n\n"
                "Ниже только проверенные позиции из реального каталога PARFERA. "
                "Используй ТОЛЬКО их и не придумывай товары, цены, объёмы или наличие.\n"
                + json.dumps(verified, ensure_ascii=False) + "\n\n"
                "Выбери 3–5 наиболее подходящих позиций. Если вариантов меньше 3, покажи столько, сколько есть. "
                "Для каждого кратко объясни соответствие запросу, но не придумывай конкретные ноты или свойства, "
                "если их нет в данных. Обязательно укажи название, концентрацию, объём и цену из каталога. "
                "Пиши естественно и премиально. Используй HTML Telegram <b> и <i>, не Markdown. Не рассуждай о процессе поиска и не повторяй запрос клиента. "
                "КРИТИЧЕСКИ ВАЖНО: в конце ответа добавь отдельной строкой строго в формате SELECTED_IDS: id1,id2,id3 — "
                "только ID тех позиций, которые ты реально рекомендовал в тексте, в том же порядке. Не добавляй туда другие ID."
            )
            final_response = await OPENAI_CLIENT.responses.create(
                model=OPENAI_MODEL,
                input=[
                    {"role": "system", "content": AI_SYSTEM_PROMPT},
                    {"role": "user", "content": final_prompt},
                ],
                tools=[],
            )
            raw_final = (final_response.output_text or "").strip()
            # AI must explicitly return the IDs it recommended. We use these IDs
            # to build buttons, so text and buttons can never point to different products.
            selected_match = re.search(r"SELECTED_IDS\s*:\s*([^\n]+)", raw_final, flags=re.I)
            selected_ids = []
            if selected_match:
                allowed = {str(p["id"]): p for p in collected[:5]}
                for token in selected_match.group(1).split(","):
                    pid = token.strip().strip("` ")
                    if pid in allowed and pid not in selected_ids:
                        selected_ids.append(pid)
                raw_final = raw_final[:selected_match.start()].rstrip()
            if selected_ids:
                selected_products = [BY_ID[pid] for pid in selected_ids]
                # Keep only products actually mentioned by the AI.
                collected = selected_products
            if selected_ids:
                final_text = raw_final
            else:
                # If the model failed to return the required IDs, never expose
                # potentially mismatched AI text. Build a safe catalog-only response.
                lines = ["💬 <b>PARFERA AI рекомендует:</b>", ""]
                for i, p in enumerate(collected[:5], 1):
                    price = rub(p["bottle_price_rub"]) if p.get("bottle_price_rub") else "цена уточняется"
                    lines.append(f"<b>{i}. {html.escape(fragrance_title(p))}</b> — {p.get('volume','')} мл, {price}")
                final_text = "\n".join(lines)
        except Exception as e:
            print(f"PARFERA AI finalization error: {type(e).__name__}: {e!r}")

    if not final_text:
        if collected:
            lines = ["💬 <b>Подобрал варианты из каталога PARFERA:</b>", ""]
            for p in collected[:5]:
                prices = []
                if p.get("bottle_price_rub"):
                    prices.append(f"{p.get('volume','')} — {rub(p['bottle_price_rub'])}")
                if p.get("tester_price_rub"):
                    prices.append(f"тестер — {rub(p['tester_price_rub'])}")
                price_text = ", ".join(prices) if prices else "цена уточняется"
                lines.append(f"• <b>{html.escape(fragrance_title(p))}</b> — {price_text}")
            final_text = "\n".join(lines)
        else:
            final_text = "Не удалось выполнить поиск. Попробуйте написать бренд или название аромата."
    # Safety net: do not let Markdown bold markers appear literally in Telegram HTML mode.
    final_text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", final_text, flags=re.S)
    final_text = re.sub(r"__(.+?)__", r"<b>\1</b>", final_text, flags=re.S)
    history.append({"role": "assistant", "content": final_text})
    history[:] = history[-AI_MAX_HISTORY:]
    AI_LAST_RESULTS[uid] = [p["id"] for p in collected]
    return final_text, collected


class SearchState(StatesGroup):
    waiting = State()


dp = Dispatcher()


def home_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💬 PARFERA AI — подобрать аромат", callback_data="ai_start")],
        [InlineKeyboardButton(text="🛍 Каталог", callback_data="catalog"),
         InlineKeyboardButton(text="🔎 Поиск", callback_data="search")],
        [InlineKeyboardButton(text="🛒 Корзина", callback_data="cart"),
         InlineKeyboardButton(text="👤 Консультант", callback_data="consultant")]
    ])


def back_home_kb():
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="← Главное меню", callback_data="home")]])


def product_group(item):
    p = BY_ID[item] if isinstance(item, str) else item
    return GROUPS.get(group_key(p), [p])


def product_text(p):
    title = display_name(p)
    raw = str(p.get("name", ""))
    lines = [f"<b>{title}</b>", "✨ Оригинальная парфюмерия"]
    # Keep supplier technical markers available, but present the main metadata
    # in a compact premium-card style.
    meta = []
    m = re.search(r"\b(edp|edt|parfum|extrait)\b", raw, re.I)
    if m:
        meta.append(m.group(1).upper())
    if re.search(r"\(m\)", raw, re.I):
        meta.append("Мужской")
    elif re.search(r"\(w\)", raw, re.I):
        meta.append("Женский")
    if meta:
        lines.append(" · ".join(meta))
    lines.append("")
    if "VERSACE EROS EDT" in raw.upper():
        lines += ["Свежий, яркий и чувственный аромат с мятой, зелёным яблоком, лимоном, ванилью и древесными нотами.", ""]
    else:
        lines += ["Нишевая и оригинальная парфюмерия PARFERA.", ""]
    lines.append("<b>ВЫБЕРИТЕ ОБЪЁМ И ВАРИАНТ</b>")
    return "\n".join(lines)


def product_kb(pid, brand_id=None, brand_page=0, gender=None):
    group = unique_variants(visible_group(pid))
    rows = []
    option_rows = []
    for p in group:
        vol = p.get("volume") or ""
        options = []
        if p.get("bottle_price_rub"):
            options.append(InlineKeyboardButton(text=f"🧴 {vol} — {rub(p['bottle_price_rub'])}", callback_data=f"addv:{p['id']}:bottle"))
        if p.get("tester_price_rub"):
            options.append(InlineKeyboardButton(text=f"🧪 {vol} · Тестер — {rub(p['tester_price_rub'])}", callback_data=f"addv:{p['id']}:tester"))
        if options:
            option_rows.append(options)
    # Two-column selector on phones when there are many variants; long tester
    # labels remain full-width for readability.
    for opts in option_rows:
        if len(opts) == 2 and all(len(x.text) <= 25 for x in opts):
            rows.append(opts)
        else:
            rows.extend([[x] for x in opts])
    rows.append([InlineKeyboardButton(text="🛒 Корзина", callback_data="cart")])
    if brand_id is None:
        bkey = BRAND_FOR_ID.get(pid)
        brand_id = BRAND_KEY_TO_ID.get(bkey) if bkey else None
    if brand_id == "ai":
        rows.append([InlineKeyboardButton(text="← К результатам AI", callback_data="ai_back")])
    elif brand_id is not None:
        rows.append([InlineKeyboardButton(text="← К товарам бренда", callback_data=(f"gbrand:{gender}:{brand_id}:{brand_page}" if gender else f"brand:{brand_id}:{brand_page}"))])
    else:
        rows.append([InlineKeyboardButton(text="← К брендам", callback_data="brands:0")])
    rows.append([InlineKeyboardButton(text="🔎 Новый поиск", callback_data="search")])
    rows.append([InlineKeyboardButton(text="← Главное меню", callback_data="home")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def gender_results(gender: str, page: int = 0):
    matches = []
    seen = set()
    for p in PRODUCTS:
        if not variant_is_client_friendly(p):
            continue
        raw = str(p.get("name", ""))
        ok = re.search(rf"\({gender}\)", raw, re.I) if gender != "u" else not re.search(r"\((?:m|w)\)", raw, re.I)
        if ok:
            gk = group_key(p)
            if gk not in seen:
                seen.add(gk); matches.append(p)
    matches.sort(key=lambda p: fragrance_title(p).lower())
    start = page * PAGE_SIZE
    return matches, matches[start:start + PAGE_SIZE]


def search_results(query: str, page: int = 0):
    words = [w for w in norm(query).split() if w]
    raw = [p for p in PRODUCTS if variant_is_client_friendly(p) and all(w in SEARCH_TEXT[p["id"]] for w in words)]
    # Search results should also be fragrance cards, not supplier rows.
    matches = []
    seen = set()
    for p in raw:
        gk = group_key(p)
        if gk in seen:
            continue
        seen.add(gk)
        matches.append(p)
    start = page * PAGE_SIZE
    return matches, matches[start:start + PAGE_SIZE]


def results_kb(items: List[dict], page: int, total: int):
    rows = []
    for p in items:
        title = fragrance_title(p)
        if len(title) > 48:
            title = title[:45] + "…"
        price = lowest_group_price(visible_group(p))
        if price:
            title += f" · от {rub(price)}"
        rows.append([InlineKeyboardButton(text=title, callback_data=f'product:{p["id"]}')])
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="← Назад", callback_data=f"page:{page-1}"))
    if (page + 1) * PAGE_SIZE < total:
        nav.append(InlineKeyboardButton(text="Далее →", callback_data=f"page:{page+1}"))
    if nav:
        rows.append(nav)
    rows.append([InlineKeyboardButton(text="🔎 Новый поиск", callback_data="search")])
    rows.append([InlineKeyboardButton(text="🛒 Корзина", callback_data="cart")])
    rows.append([InlineKeyboardButton(text="← Главное меню", callback_data="home")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def brands_kb(page: int = 0):
    per_page = 15
    start = page * per_page
    keys = BRAND_KEYS[start:start + per_page]
    rows = []
    for key in keys:
        bid = BRAND_KEY_TO_ID[key]
        count = len(BRAND_GROUPS[key])
        label = BRAND_DISPLAY[key]
        if len(label) > 28:
            label = label[:25] + "…"
        rows.append([InlineKeyboardButton(text=f"{label} · {count}", callback_data=f"brand:{bid}:0")])
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="← Назад", callback_data=f"brands:{page-1}"))
    if (page + 1) * per_page < len(BRAND_KEYS):
        nav.append(InlineKeyboardButton(text="Далее →", callback_data=f"brands:{page+1}"))
    if nav:
        rows.append(nav)
    rows += [
        [InlineKeyboardButton(text="🔎 Найти аромат", callback_data="search")],
        [InlineKeyboardButton(text="🛒 Корзина", callback_data="cart")],
        [InlineKeyboardButton(text="← В каталог", callback_data="catalog")],
        [InlineKeyboardButton(text="← Главное меню", callback_data="home")]
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


POPULAR_BRANDS = [
    "versace", "chanel", "christian dior", "dior", "xerjoff", "amouage", "tom ford",
    "giorgio armani", "dolce & gabbana", "carolina herrera", "yves saint laurent",
    "narciso rodriguez", "jo malone", "byredo", "le labo", "maison francis kurkdjian",
    "parfums de marly", "creed", "initio", "montale", "mancera", "memo paris", "kilian"
]
DESIGNER_BRANDS = [
    "versace", "chanel", "christian dior", "dior", "giorgio armani", "dolce & gabbana",
    "carolina herrera", "calvin klein", "paco rabanne", "hugo boss", "jean paul gaultier",
    "ralph lauren", "elizabeth arden", "salvatore ferragamo", "thierry mugler",
    "narciso rodriguez", "michael kors", "viktor & rolf", "issey miyake", "marc jacobs",
    "nina ricci", "elie saab", "john varvatos", "moncler", "zadig & voltaire", "courreges"
]
NICHE_BRANDS = [
    "xerjoff", "amouage", "parfums de marly", "maison francis kurkdjian", "initio",
    "creed", "byredo", "le labo", "memo paris", "kilian", "montale", "mancera",
    "atelier des ors", "ex nihilo", "frederic malle", "juliette has a gun", "serge lutens",
    "histoires de parfums", "etat libre d'orange", "les liquides imaginaires",
    "les eaux primordiales", "pierre guillaume", "tauer perfumes", "fragrance du bois",
    "laboratorio olfattivo", "arabian oud"
]

def _brand_matches(names):
    wanted = {canonical_token(x) for x in names}
    return [k for k in BRAND_KEYS if canonical_token(k) in wanted or canonical_token(BRAND_DISPLAY[k]) in wanted]

def category_kb(kind: str):
    names = {"popular": POPULAR_BRANDS, "niche": NICHE_BRANDS, "designer": DESIGNER_BRANDS}.get(kind, [])
    keys = _brand_matches(names)
    rows = [[InlineKeyboardButton(text=f"{BRAND_DISPLAY[k]} · {len(BRAND_GROUPS[k])}", callback_data=f"brand:{BRAND_KEY_TO_ID[k]}:0")] for k in keys]
    rows += [
        [InlineKeyboardButton(text="🔤 Все бренды", callback_data="brands:0")],
        [InlineKeyboardButton(text="🔎 Найти аромат", callback_data="search")],
        [InlineKeyboardButton(text="🛒 Корзина", callback_data="cart")],
        [InlineKeyboardButton(text="← Каталог", callback_data="catalog")]
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _group_items(group) -> List[dict]:
    # BRAND_GROUPS normally stores lists of product variants, but older
    # catalog builds may contain a single product dict. Normalize both.
    if isinstance(group, dict):
        return [group]
    if isinstance(group, (list, tuple)):
        return [x for x in group if isinstance(x, dict)]
    return []

def _group_matches_gender(group, gender: str) -> bool:
    items = _group_items(group)
    if not items:
        return False
    names = [str(x.get("name", "")) for x in items]
    if gender in ("m", "w"):
        return any(bool(re.search(rf"\({gender}\)", name, re.I)) for name in names)
    return any(not bool(re.search(r"\((?:m|w)\)", name, re.I)) for name in names)

def gender_brand_keys(gender: str):
    return [key for key in BRAND_KEYS if any(_group_matches_gender(g, gender) for g in BRAND_GROUPS.get(key, []))]

def gender_brands_kb(gender: str, page: int = 0):
    keys = gender_brand_keys(gender)
    per_page = 15
    start = page * per_page
    rows = []
    for key in keys[start:start + per_page]:
        bid = BRAND_KEY_TO_ID[key]
        count = sum(1 for g in BRAND_GROUPS[key] if _group_matches_gender(g, gender))
        rows.append([InlineKeyboardButton(text=f"{BRAND_DISPLAY[key]} · {count}", callback_data=f"gbrand:{gender}:{bid}:0")])
    nav=[]
    if page > 0: nav.append(InlineKeyboardButton(text="← Назад", callback_data=f"gbrands:{gender}:{page-1}"))
    if (page+1)*per_page < len(keys): nav.append(InlineKeyboardButton(text="Далее →", callback_data=f"gbrands:{gender}:{page+1}"))
    if nav: rows.append(nav)
    rows += [[InlineKeyboardButton(text="🔎 Найти аромат", callback_data="search")], [InlineKeyboardButton(text="🛒 Корзина", callback_data="cart")], [InlineKeyboardButton(text="← Каталог", callback_data="catalog")]]
    return InlineKeyboardMarkup(inline_keyboard=rows)

def gender_brand_products_kb(gender: str, brand_id: str, page: int = 0):
    key = BRAND_ID_TO_KEY[brand_id]
    groups = [g for g in BRAND_GROUPS.get(key, []) if _group_matches_gender(g, gender)]
    total = len(groups)
    start = page * PAGE_SIZE
    items = groups[start:start + PAGE_SIZE]
    rows = []
    for group in items:
        variants = _group_items(group)
        if not variants:
            continue
        p = variants[0]
        title = fragrance_title(p)
        if len(title)>44: title=title[:41]+"…"
        price=lowest_group_price(group)
        if price: title += f" · от {rub(price)}"
        rows.append([InlineKeyboardButton(text=title, callback_data=f"product:{p['id']}:{brand_id}:{page}:{gender}")])
    nav=[]
    if page>0: nav.append(InlineKeyboardButton(text="← Назад", callback_data=f"gbrand:{gender}:{brand_id}:{page-1}"))
    if (page+1)*PAGE_SIZE<total: nav.append(InlineKeyboardButton(text="Далее →", callback_data=f"gbrand:{gender}:{brand_id}:{page+1}"))
    if nav: rows.append(nav)
    rows += [[InlineKeyboardButton(text="← К брендам", callback_data=f"gbrands:{gender}:0")],[InlineKeyboardButton(text="🛒 Корзина", callback_data="cart")],[InlineKeyboardButton(text="🔎 Новый поиск", callback_data="search")],[InlineKeyboardButton(text="← Главное меню", callback_data="home")]]
    return InlineKeyboardMarkup(inline_keyboard=rows), total

def brand_products_kb(brand_id: str, page: int = 0):
    key = BRAND_ID_TO_KEY[brand_id]
    groups = BRAND_GROUPS[key]
    total = len(groups)
    start = page * PAGE_SIZE
    items = groups[start:start + PAGE_SIZE]
    rows = []
    for group in items:
        p = group[0]
        title = fragrance_title(p)
        if len(title) > 44:
            title = title[:41] + "…"
        price = lowest_group_price(group)
        if price:
            title += f" · от {rub(price)}"
        rows.append([InlineKeyboardButton(text=title, callback_data=f"product:{p['id']}:{brand_id}:{page}")])
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="← Назад", callback_data=f"brand:{brand_id}:{page-1}"))
    if (page + 1) * PAGE_SIZE < total:
        nav.append(InlineKeyboardButton(text="Далее →", callback_data=f"brand:{brand_id}:{page+1}"))
    if nav:
        rows.append(nav)
    rows += [
        [InlineKeyboardButton(text="← К брендам", callback_data="brands:0")],
        [InlineKeyboardButton(text="🛒 Корзина", callback_data="cart")],
        [InlineKeyboardButton(text="🔎 Новый поиск", callback_data="search")],
        [InlineKeyboardButton(text="← Главное меню", callback_data="home")]
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def cart_text(uid: int):
    items = CARTS.get(uid, [])
    if not items:
        return "🛒 <b>Ваша корзина пуста</b>\n\nДобавьте аромат из каталога."
    total = sum(x["price"] * x["qty"] for x in items)
    lines = [f"🛒 <b>Ваша корзина</b>\n{len(items)} позиция(й)\n"]
    for i, x in enumerate(items, 1):
        lines.append(f"{i}. {x['name']}\n   {x['type']} · {x['volume']} · {x['qty']} × {rub(x['price'])}")
    lines.append(f"\n<b>Итого: {rub(total)}</b>")
    return "\n".join(lines)


def cart_kb(uid: int):
    items = CARTS.get(uid, [])
    rows = []
    for i, x in enumerate(items):
        rows.append([
            InlineKeyboardButton(text="−", callback_data=f"qty:{i}:-1"),
            InlineKeyboardButton(text=f"{i+1}: {x['qty']}", callback_data="noop"),
            InlineKeyboardButton(text="+", callback_data=f"qty:{i}:1"),
            InlineKeyboardButton(text="🗑", callback_data=f"del:{i}")
        ])
    if items:
        rows.append([InlineKeyboardButton(text="📦 Оформить заказ", callback_data="checkout")])
    rows.append([InlineKeyboardButton(text="← Продолжить покупки", callback_data="catalog")])
    rows.append([InlineKeyboardButton(text="← Главное меню", callback_data="home")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def edit_or_replace(message, text, reply_markup=None):
    if message.photo:
        await message.delete()
        return await message.answer(text, reply_markup=reply_markup)
    return await message.edit_text(text, reply_markup=reply_markup)


def brand_key_from_query(query: str):
    q = norm(query).replace("’", "'")
    # Exact match against the client-facing brand names/keys.
    for key in BRAND_KEYS:
        if q == norm(BRAND_DISPLAY[key]).replace("’", "'") or q == norm(key).replace("’", "'"):
            return key
    return None


async def show_search_results(target_message, user_id: int, page: int = 0):
    query = USER_SEARCH.get(user_id, "")
    if query.startswith("__gender__:"):
        gender = query.split(":", 1)[1]
        matches, items = gender_results(gender, page)
        title = "👩 Для неё" if gender == "w" else "👨 Для него" if gender == "m" else "⚪ Унисекс"
        await edit_or_replace(target_message, f"<b>{title}</b>\n\nНайдено ароматов: <b>{len(matches)}</b>", results_kb(items, page, len(matches)))
        return
    exact_brand = brand_key_from_query(query)
    if exact_brand is not None and page == 0:
        brand_id = BRAND_KEY_TO_ID[exact_brand]
        total = len(BRAND_GROUPS[exact_brand])
        text = f"<b>{BRAND_DISPLAY[exact_brand]}</b>\n\nАроматов: <b>{total}</b>\nВыберите аромат:"
        await edit_or_replace(target_message, text, brand_products_kb(brand_id, 0))
        return
    matches, items = search_results(query, page)
    if not matches:
        await edit_or_replace(target_message, f'🔎 По запросу «{query}» ничего не найдено.\n\nПопробуйте название бренда или аромата.', back_home_kb())
        return
    await edit_or_replace(target_message, f'🔎 Найдено: <b>{len(matches)}</b>\nЗапрос: «{query}»\n\nВыберите товар:', results_kb(items, page, len(matches)))


async def send_product(message, p, brand_id=None, brand_page=0, gender=None):
    text = product_text(p)
    kb = product_kb(p["id"], brand_id, brand_page)
    image = p.get("image_url")
    if image:
        try:
            await message.answer_photo(photo=FSInputFile(image), caption=text, reply_markup=kb)
            return
        except Exception as e:
            print(f"Photo send failed for {p['id']}: {e}")
    await message.answer(text, reply_markup=kb)


HOME_TEXT = "<b>PARFERA</b>\n\nНишевая парфюмерия и персональный подбор.\n\n💬 <b>Просто напишите, какой аромат вы ищете.</b>\nНапример: «женский сладкий до 7000», «Versace Eros 100 мл» или «что-нибудь похожее на Erba Pura»."
MAIN_IMAGE = os.path.join("images", "parfera_ai_main.jpg")


async def send_home(message: Message):
    if os.path.exists(MAIN_IMAGE):
        try:
            await message.answer_photo(photo=FSInputFile(MAIN_IMAGE), caption=HOME_TEXT, reply_markup=home_kb())
            return
        except Exception as e:
            print(f"Main image send failed: {e}")
    await message.answer(HOME_TEXT, reply_markup=home_kb())


@dp.message(CommandStart())
async def start(message: Message, state: FSMContext):
    await state.clear()
    await send_home(message)


@dp.callback_query(F.data == "home")
async def home(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    try:
        await callback.message.delete()
    except Exception:
        pass
    await send_home(callback.message)
    await callback.answer()


@dp.callback_query(F.data == "catalog")
async def catalog(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    visible_groups_total = sum(len(v) for v in BRAND_GROUPS.values())
    text = f"🛍 <b>Каталог PARFERA</b>\n\nАроматов: <b>{visible_groups_total}</b> · Брендов: <b>{len(BRAND_KEYS)}</b>\n\nВыберите раздел:"
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⭐ Популярные бренды", callback_data="cat:popular")],
        [InlineKeyboardButton(text="💎 Нишевая парфюмерия", callback_data="cat:niche")],
        [InlineKeyboardButton(text="✨ Дизайнерские бренды", callback_data="cat:designer")],
        [InlineKeyboardButton(text="👩 Для неё", callback_data="cat:w")],
        [InlineKeyboardButton(text="👨 Для него", callback_data="cat:m")],
        [InlineKeyboardButton(text="⚪ Унисекс", callback_data="cat:u")],
        [InlineKeyboardButton(text="🔤 Все бренды", callback_data="brands:0")],
        [InlineKeyboardButton(text="🔎 Найти аромат", callback_data="search")],
        [InlineKeyboardButton(text="🛒 Корзина", callback_data="cart")],
        [InlineKeyboardButton(text="← Главное меню", callback_data="home")],
    ])
    await edit_or_replace(callback.message, text, kb)
    await callback.answer()


@dp.callback_query(F.data.startswith("cat:"))
async def catalog_category(callback: CallbackQuery):
    kind = callback.data.split(":", 1)[1]
    if kind in {"popular", "niche", "designer"}:
        titles = {"popular": "⭐ Популярные бренды", "niche": "💎 Нишевая парфюмерия", "designer": "✨ Дизайнерские бренды"}
        await edit_or_replace(callback.message, f"<b>{titles[kind]}</b>\n\nВыберите бренд:", category_kb(kind))
        await callback.answer()
        return
    gender = {"w": "w", "m": "m", "u": "u"}.get(kind)
    if gender:
        title = "👩 Для неё" if gender == "w" else "👨 Для него" if gender == "m" else "⚪ Унисекс"
        keys = gender_brand_keys(gender)
        total = sum(sum(1 for g in BRAND_GROUPS[k] if _group_matches_gender(g, gender)) for k in keys)
        await edit_or_replace(callback.message, f"<b>{title}</b>\n\nБрендов: <b>{len(keys)}</b> · Ароматов: <b>{total}</b>\n\nВыберите бренд:", gender_brands_kb(gender, 0))
        await callback.answer()


@dp.callback_query(F.data.startswith("gbrands:"))
async def gender_brands_page(callback: CallbackQuery):
    _, gender, page = callback.data.split(":")
    title = "👩 Для неё" if gender == "w" else "👨 Для него" if gender == "m" else "⚪ Унисекс"
    keys = gender_brand_keys(gender)
    await edit_or_replace(callback.message, f"<b>{title}</b>\n\nБрендов: <b>{len(keys)}</b>\n\nВыберите бренд:", gender_brands_kb(gender, int(page)))
    await callback.answer()

@dp.callback_query(F.data.startswith("gbrand:"))
async def gender_brand_page(callback: CallbackQuery):
    _, gender, brand_id, page = callback.data.split(":")
    key = BRAND_ID_TO_KEY.get(brand_id)
    if not key:
        await callback.answer("Бренд не найден", show_alert=True); return
    title = BRAND_DISPLAY[key]
    kb, total = gender_brand_products_kb(gender, brand_id, int(page))
    await edit_or_replace(callback.message, f"<b>{title}</b>\n\nАроматов: <b>{total}</b>\nВыберите аромат:", kb)
    await callback.answer()

@dp.callback_query(F.data.startswith("brands:"))
async def brands_page(callback: CallbackQuery):
    page = int(callback.data.split(":")[1])
    text = f"🛍 <b>Бренды PARFERA</b>\n\nДоступно брендов: <b>{len(BRAND_KEYS)}</b>\n\nВыберите бренд:"
    await edit_or_replace(callback.message, text, brands_kb(page))
    await callback.answer()


@dp.callback_query(F.data.startswith("brand:"))
async def brand_page(callback: CallbackQuery):
    _, brand_id, page = callback.data.split(":")
    key = BRAND_ID_TO_KEY.get(brand_id)
    if not key:
        await callback.answer("Бренд не найден", show_alert=True)
        return
    total = len(BRAND_GROUPS.get(key, []))
    title = BRAND_DISPLAY[key]
    text = f"<b>{title}</b>\n\nАроматов: <b>{total}</b>\nВыберите аромат:"
    await edit_or_replace(callback.message, text, brand_products_kb(brand_id, int(page)))
    await callback.answer()


@dp.callback_query(F.data == "ai_start")
async def ai_start(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await edit_or_replace(callback.message,
        "💬 <b>PARFERA AI</b>\n\nПросто напишите, что вы ищете — я помогу найти реальные позиции в каталоге PARFERA.\n\nНапример:\n• «Женский сладкий до 7000 ₽»\n• «Versace Eros 100 мл»\n• «Мужской свежий аромат»\n• «Что есть похожее на Erba Pura?»",
        InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="← Главное меню", callback_data="home")]]))
    await callback.answer()


@dp.callback_query(F.data == "ai_back")
async def ai_back(callback: CallbackQuery):
    uid = callback.from_user.id
    results = [BY_ID[x] for x in AI_LAST_RESULTS.get(uid, []) if x in BY_ID]
    if results:
        rows = [[InlineKeyboardButton(text=f"{fragrance_title(p)[:42]}", callback_data=f"product:{p['id']}:ai:0")] for p in results[:10]]
        rows.append([InlineKeyboardButton(text="💬 Новый запрос", callback_data="ai_start")])
        rows.append([InlineKeyboardButton(text="← Главное меню", callback_data="home")])
        await edit_or_replace(callback.message, "💬 <b>Результаты PARFERA AI</b>\n\nВыберите аромат:", InlineKeyboardMarkup(inline_keyboard=rows))
    else:
        await edit_or_replace(callback.message, "💬 <b>PARFERA AI</b>\n\nНапишите следующий запрос.", back_home_kb())
    await callback.answer()




@dp.callback_query(F.data.in_({"search", "catalog_search"}))
async def search(callback: CallbackQuery, state: FSMContext):
    await state.set_state(SearchState.waiting)
    await edit_or_replace(callback.message, "🔎 <b>Поиск по каталогу</b>\n\nВведите бренд, название аромата или артикул.\n\nНапример: <b>Versace Eros</b>, <b>Erba Pura</b> или <b>000-002</b>.", back_home_kb())
    await callback.answer()


@dp.message(SearchState.waiting)
async def do_search(message: Message, state: FSMContext):
    query = message.text.strip() if message.text else ""
    if not query:
        await message.answer("Введите текст для поиска.")
        return
    USER_SEARCH[message.from_user.id] = query
    await state.clear()
    exact_brand = brand_key_from_query(query)
    if exact_brand is not None:
        brand_id = BRAND_KEY_TO_ID[exact_brand]
        total = len(BRAND_GROUPS[exact_brand])
        await message.answer(f"<b>{BRAND_DISPLAY[exact_brand]}</b>\n\nАроматов: <b>{total}</b>\nВыберите аромат:", reply_markup=brand_products_kb(brand_id, 0))
        return
    matches, items = search_results(query, 0)
    if not matches:
        await message.answer(f'🔎 По запросу «{query}» ничего не найдено.\n\nПопробуйте более короткий запрос.', reply_markup=back_home_kb())
        return
    await message.answer(f'🔎 Найдено: <b>{len(matches)}</b>\nЗапрос: «{query}»\n\nВыберите товар:', reply_markup=results_kb(items, 0, len(matches)))


@dp.message(F.text == "/myid")
async def myid(message: Message):
    await message.answer(
        f"🆔 Ваш Telegram ID: <code>{message.from_user.id}</code>\n\n"
        "Этот ID нужен для настройки получения заказов в Render.\n"
        "Никому не отправляйте токены или пароли."
    )


@dp.message(F.text)
async def ai_free_text(message: Message, state: FSMContext):
    # SearchState has its own handler above; this handler is for ordinary messages from the main screen.
    current = await state.get_state()
    if current is not None:
        return
    text = (message.text or "").strip()
    if not text or text.startswith("/"):
        return
    await message.bot.send_chat_action(chat_id=message.chat.id, action="typing")
    try:
        answer, candidates = await ai_assist(message.from_user.id, text)
    except Exception as e:
        print(f"PARFERA AI error: {type(e).__name__}: {e!r}")
        await message.answer("🤖 Сейчас не получилось выполнить умный поиск. Попробуйте ещё раз или воспользуйтесь каталогом.", reply_markup=home_kb())
        return
    rows = []
    for p in candidates[:5]:
        title = fragrance_title(p)
        rows.append([InlineKeyboardButton(text=f"🧴 {title[:58]}", callback_data=f"product:{p['id']}:ai:0")])
    rows.append([InlineKeyboardButton(text="💬 Новый запрос", callback_data="ai_start")])
    rows.append([InlineKeyboardButton(text="🛍 Каталог", callback_data="catalog")])
    await message.answer(answer, reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


@dp.callback_query(F.data.startswith("page:"))
async def page(callback: CallbackQuery):
    await show_search_results(callback.message, callback.from_user.id, int(callback.data.split(":")[1]))
    await callback.answer()


@dp.callback_query(F.data.startswith("product:"))
async def product(callback: CallbackQuery):
    parts = callback.data.split(":")
    pid = parts[1]
    brand_id = parts[2] if len(parts) > 2 else None
    brand_page = int(parts[3]) if len(parts) > 3 else 0
    gender = parts[4] if len(parts) > 4 else None
    p = BY_ID.get(pid)
    if not p:
        await callback.answer("Товар не найден", show_alert=True)
        return
    if not visible_group(p):
        await callback.answer("Эта позиция скрыта из клиентского каталога", show_alert=True)
        return
    await callback.message.delete()
    await send_product(callback.message, p, brand_id, brand_page, gender)
    await callback.answer()


@dp.callback_query(F.data.startswith("addv:"))
async def add_variant(callback: CallbackQuery):
    _, pid, typ = callback.data.split(":")
    p = BY_ID.get(pid)
    if not p:
        await callback.answer("Товар не найден", show_alert=True)
        return
    if typ == "bottle":
        price = p.get("bottle_price_rub")
        label = "Флакон"
    else:
        price = p.get("tester_price_rub")
        label = "Тестер"
    if not price:
        await callback.answer("Этот вариант недоступен", show_alert=True)
        return
    uid = callback.from_user.id
    cart = CARTS.setdefault(uid, [])
    key = (pid, typ)
    existing = next((x for x in cart if (x["pid"], x["type_key"]) == key), None)
    if existing:
        existing["qty"] += 1
    else:
        cart.append({"pid": pid, "type_key": typ, "name": display_name(p), "volume": p.get("volume", ""), "type": label, "price": int(price), "qty": 1})
    await callback.answer("Добавлено в корзину")
    await callback.message.answer(cart_text(uid), reply_markup=cart_kb(uid))


@dp.callback_query(F.data == "cart")
async def cart(callback: CallbackQuery):
    uid = callback.from_user.id
    await edit_or_replace(callback.message, cart_text(uid), cart_kb(uid))
    await callback.answer()


@dp.callback_query(F.data.startswith("qty:"))
async def qty(callback: CallbackQuery):
    _, idx, delta = callback.data.split(":")
    uid = callback.from_user.id
    items = CARTS.get(uid, [])
    i = int(idx)
    if 0 <= i < len(items):
        items[i]["qty"] += int(delta)
        if items[i]["qty"] <= 0:
            items.pop(i)
    await edit_or_replace(callback.message, cart_text(uid), cart_kb(uid))
    await callback.answer()


@dp.callback_query(F.data.startswith("del:"))
async def delete_item(callback: CallbackQuery):
    uid = callback.from_user.id
    i = int(callback.data.split(":")[1])
    items = CARTS.get(uid, [])
    if 0 <= i < len(items):
        items.pop(i)
    await edit_or_replace(callback.message, cart_text(uid), cart_kb(uid))
    await callback.answer("Удалено")


@dp.callback_query(F.data == "checkout")
async def checkout(callback: CallbackQuery):
    uid = callback.from_user.id
    items = CARTS.get(uid, [])
    if not items:
        await callback.answer("Корзина пуста", show_alert=True)
        return

    if not ADMIN_CHAT_ID:
        await edit_or_replace(
            callback.message,
            "📦 <b>Заказ</b>\n\nСейчас приём заказов ещё не настроен. Администратору нужно один раз указать <b>PARFERA_ADMIN_CHAT_ID</b> в Render.",
            InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="← Корзина", callback_data="cart")],
                [InlineKeyboardButton(text="← Главное меню", callback_data="home")],
            ])
        )
        await callback.answer()
        return

    total = sum(x["price"] * x["qty"] for x in items)
    order_no = datetime.now().strftime("%d%m%H%M%S")
    user = callback.from_user
    username = f"@{user.username}" if user.username else "не указан"
    full_name = html.escape(" ".join(filter(None, [user.first_name, user.last_name]))) or "не указано"

    lines = [
        f"🛍 <b>НОВЫЙ ЗАКАЗ PARFERA №{order_no}</b>",
        "",
        f"👤 Клиент: {full_name}",
        f"💬 Telegram: {html.escape(username)}",
        f"🆔 ID: <code>{user.id}</code>",
        "",
        "<b>Состав заказа:</b>",
    ]
    for i, x in enumerate(items, 1):
        lines.append(
            f"{i}. {html.escape(str(x['name']))} — {html.escape(str(x['type']))}, "
            f"{html.escape(str(x['volume']))} · {x['qty']} × {rub(x['price'])} ₽"
        )
    lines += ["", f"💰 <b>Итого: {rub(total)} ₽</b>", "", "📩 Заказ отправлен из Telegram-магазина PARFERA."]
    order_text = "\n".join(lines)

    try:
        await callback.bot.send_message(chat_id=int(ADMIN_CHAT_ID), text=order_text)
    except Exception as e:
        print(f"Order send failed: {e}")
        await callback.answer("Не удалось отправить заказ консультанту", show_alert=True)
        return

    CARTS.pop(uid, None)
    await edit_or_replace(
        callback.message,
        f"✅ <b>Заказ принят!</b>\n\nНомер заказа: <b>№{order_no}</b>\n\nВаш заказ отправлен консультанту PARFERA <b>@Parfera</b>. Он свяжется с вами в Telegram.",
        InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="💬 Консультант @Parfera", url="https://t.me/Parfera")],
            [InlineKeyboardButton(text="🛍 Продолжить покупки", callback_data="catalog")],
            [InlineKeyboardButton(text="← Главное меню", callback_data="home")],
        ])
    )
    await callback.answer("Заказ отправлен")


@dp.callback_query(F.data == "noop")
async def noop(callback: CallbackQuery):
    await callback.answer()


@dp.callback_query(F.data == "popular")
async def popular(callback: CallbackQuery):
    await edit_or_replace(callback.message, "⭐ <b>Популярное</b>\n\nПодключим после теста каталога.", back_home_kb())
    await callback.answer()


@dp.callback_query(F.data == "new")
async def new(callback: CallbackQuery):
    await edit_or_replace(callback.message, "🆕 <b>Новинки</b>\n\nПодключим после теста каталога.", back_home_kb())
    await callback.answer()


@dp.callback_query(F.data == "consultant")
async def consultant(callback: CallbackQuery):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💬 Написать консультанту", url="https://t.me/Parfera")],
        [InlineKeyboardButton(text="🔎 Подобрать аромат", callback_data="search")],
        [InlineKeyboardButton(text="← Главное меню", callback_data="home")],
    ])
    await edit_or_replace(callback.message, "👤 <b>Консультант PARFERA</b>\n\nЕсли нужна помощь с выбором аромата — напишите консультанту <b>@Parfera</b>.\n\nОн поможет подобрать аромат под ваш вкус, задачу и бюджет.", kb)
    await callback.answer()


async def health(request: web.Request):
    return web.Response(text="OK")


async def run_web_server():
    app = web.Application()
    app.router.add_get("/", health)
    app.router.add_get("/health", health)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.environ.get("PORT", "10000"))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    print(f"HTTP server started on 0.0.0.0:{port}")
    return runner


async def main():
    bot = Bot(TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    runner = None
    try:
        await bot.delete_webhook(drop_pending_updates=True)
        runner = await run_web_server()
        print(f"Catalog loaded: {len(PRODUCTS)} products, {len(BRAND_KEYS)} brands, {len(GROUPS)} fragrance groups")
        print("Starting Telegram long polling...")
        await dp.start_polling(bot, drop_pending_updates=True)
    finally:
        if runner is not None:
            await runner.cleanup()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
