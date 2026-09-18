import os
import json
import traceback

PARFERA_AI_VERSION = "V54-BOUNDED-INDEXED-SEARCH"
import asyncio
import re
import html
from difflib import SequenceMatcher
from datetime import datetime
from typing import Dict, List, Tuple, Optional
from functools import lru_cache
from aiohttp import web

from aiogram import Bot, Dispatcher, F
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton, FSInputFile, Update
from aiogram.filters import CommandStart
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode, ChatAction
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


def ai_fragrance_title(p):
    """AI/customer option title: brand + fragrance + concentration.
    Always shows the perfume house, including cases such as EX NIHILO Fleur Narcotique.
    """
    title = fragrance_title(p)
    brand = BRAND_DISPLAY.get(BRAND_FOR_ID.get(p.get("id"), ""), "") if "BRAND_FOR_ID" in globals() else ""
    if not brand:
        brand = detect_brand(p.get("name", ""))
    brand = str(brand or "").strip()
    # Avoid duplicate brand when the cleaned fragrance title already starts with it.
    if brand and not norm(title).startswith(norm(brand)):
        return f"{brand} — {title}"
    return title


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
    # Match hidden product markers as words/phrases, not arbitrary substrings.
    # A substring check would incorrectly hide real fragrance names such as
    # ALTHAIR because they contain the word "hair" inside the name.
    for marker in HIDDEN_VARIANT_MARKERS:
        marker = norm(marker).strip()
        if not marker:
            continue
        if re.search(rf"(?<![a-z0-9а-яё]){re.escape(marker)}(?![a-z0-9а-яё])", name, re.I):
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

# Build additional brand-prefix candidates directly from the catalog. The supplier
# data does not have a reliable brand column, so this fills gaps in BRAND_PREFIXES
# automatically instead of requiring us to add every new perfume house by hand.
_TOKENIZED_PRODUCT_NAMES = [word_tokens(p.get("name", "")) for p in PRODUCTS]
_PREFIX_STATS = {}
for toks in _TOKENIZED_PRODUCT_NAMES:
    if not toks:
        continue
    for n in range(2, min(4, len(toks)) + 1):
        pref = tuple(canonical_token(x) for x in toks[:n])
        nxt = canonical_token(toks[n]) if len(toks) > n else ""
        item = _PREFIX_STATS.setdefault(pref, {"count": 0, "next": set()})
        item["count"] += 1
        if nxt:
            item["next"].add(nxt)

# Generic leading words are frequently product-line names rather than houses.
_GENERIC_BRAND_PREFIX_WORDS = {
    "THE", "EAU", "PARFUM", "PARFUMS", "COLLECTION", "COLLECTOR", "FOR",
    "MEN", "WOMEN", "WOMAN", "HOMME", "FEMME", "UNISEX", "ORIGINAL",
}

_DYNAMIC_PREFIX_ITEMS = []
for pref, stat in _PREFIX_STATS.items():
    if stat["count"] >= 3 and len(stat["next"]) >= 2 and pref[0] not in _GENERIC_BRAND_PREFIX_WORDS:
        _DYNAMIC_PREFIX_ITEMS.append((pref, " ".join(pref).title()))
_DYNAMIC_PREFIX_ITEMS.sort(key=lambda item: (-len(item[0]), item[1]))


def detect_brand(name: str) -> str:
    tokens = word_tokens(name)
    if not tokens:
        return "UNKNOWN"
    canon = tuple(canonical_token(x) for x in tokens)

    # 1. Explicit curated prefixes always win.
    for prefix, display in PREFIX_ITEMS:
        if len(canon) >= len(prefix) and canon[:len(prefix)] == prefix:
            return display

    # 2. For brands not in the curated list, infer a repeated multi-word prefix
    # from the catalog itself. This makes the search work across the whole file
    # without maintaining a manual list of hundreds of houses.
    for prefix, display in _DYNAMIC_PREFIX_ITEMS:
        if len(canon) >= len(prefix) and canon[:len(prefix)] == prefix:
            return display

    # 3. Safe fallback for one-word houses.
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

# Precompile exact multi-word brand patterns once. Rebuilding regular expressions
# for every customer message is unnecessarily expensive on a large catalog.
MULTIWORD_BRAND_PATTERNS = []
for _key in sorted(BRAND_KEYS, key=lambda k: len(norm(BRAND_DISPLAY.get(k, k))), reverse=True):
    _label = norm(BRAND_DISPLAY.get(_key, _key))
    if len(_label.split()) > 1:
        MULTIWORD_BRAND_PATTERNS.append(
            (_key, re.compile(rf"(?<![a-z0-9а-яё]){re.escape(_label)}(?![a-z0-9а-яё])", re.I))
        )

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

# Fast-search index: use the already-built client-facing fragrance groups
# instead of rescanning every supplier row on every message. This is important
# for a catalog with many volume/tester/miniature rows.
FAST_SEARCH_GROUPS_BY_BRAND: Dict[str, List[dict]] = {}
for _bkey, _groups in BRAND_GROUPS.items():
    FAST_SEARCH_GROUPS_BY_BRAND[_bkey] = [items[0] for items in _groups if items]
FAST_SEARCH_GROUPS_ALL: List[dict] = []
for _bkey in BRAND_KEYS:
    FAST_SEARCH_GROUPS_ALL.extend(FAST_SEARCH_GROUPS_BY_BRAND.get(_bkey, []))

# Product images: one image per fragrance + concentration + gender.
# Volume and tester variants reuse the same image.
IMAGE_MAP = {}
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
    "парфюм", "парфюмы", "парфюмерия", "духи", "дух", "perfume", "perfumes", "parfum", "parfums", "fragrance", "parfyum", "dukhi", "aromat", "flakon", "флакон", "флакона", "туалетная", "вода", "edt", "edp", "extrait", "parfume",
    "и", "или", "на", "для", "мне", "мужской", "мужская", "мужское", "женский", "женская", "женское",
    "унисекс", "аромат", "аромата", "ароматы", "хочу", "нужен", "нужна", "нужно", "подбери", "подобрать",
    "посоветуй", "порекомендуй", "ищу", "найди", "есть", "что", "чтобы", "до", "руб", "рублей", "рубля",
    "р", "лет", "года", "год", "осень", "зима", "весна", "лето", "подарок", "подарка", "похожее", "похожий",
    "похожая", "похожие", "свежий", "свежая", "свежие", "сладкий", "сладкая", "сладкие", "легкий", "легкая",
    "тяжелый", "тяжелая", "теплый", "теплая", "насыщенный", "насыщенная", "цветочный", "цветочная", "древесный",
    "древесная", "мускусный", "мускусная", "вечерний", "вечерняя", "дневной", "дневная", "офис", "работу",
    "бергамотом", "бергамот", "ванилью", "ваниль", "розой", "роза", "мускусом", "мускус", "уда", "удом"
}

# Wordstat test index for AMOUAGE (15.08.2026–15.09.2026).
# These aliases are intentionally scoped to AMOUAGE and only include
# unambiguous fragrance terms; generic queries such as "амуаж 46" are not
# force-mapped because they can refer to more than one product.
WORDSTAT_AMOUAGE_ALIASES = {
    "амуаж": "amouage",
    "амоуаж": "amouage",
    "амуж": "amouage",
    "амуаж гайденс": "amouage guidance",
    "гайденс амуаж": "amouage guidance",
    "амуаж гайданс": "amouage guidance",
    "гуиданс амуаж": "amouage guidance",
    "амуаж гуиденс": "amouage guidance",
    "амуаж гайден": "amouage guidance",
    "амуаж интерлюд": "amouage interlude",
    "амуаж рефлекшн": "amouage reflection",
    "амуаж хонор": "amouage honour",
    "амуаж саншайн": "amouage sunshine",
    "амуаж экзистенс": "amouage existence",
    "амуаж диа": "amouage dia",
    "амуаж эпик": "amouage epic",
    "амуаж эпика": "amouage epic",
    "амуаж гибискус": "amouage love hibiscus",
    "амуаж лав гибискус": "amouage love hibiscus",
    "амуаж лав": "amouage love",
    "амуаж лове": "amouage love",
    "амуаж блоссом": "amouage blossom love",
    "амуаж блоссом лав": "amouage blossom love",
    "амуаж тубероза": "amouage love tuberose",
    "амуаж пурпосе": "amouage purpose",
    "амуаж делайт": "amouage love delight",
    "амуаж аутлендс": "amouage outlands",
    "амуаж интерлюд мен": "amouage interlude man",
    "амуаж опус 14": "amouage opus 14 royal tobacco",
    "амуаж розовый": "amouage rose incense",
    "амуаж белый": "amouage reflection",
    "amouage guidance": "amouage guidance",
    "guidance amouage": "amouage guidance",
    "amouage interlude": "amouage interlude",
    "amouage reflection": "amouage reflection",
    "amouage honour": "amouage honour",
    "amouage sunshine": "amouage sunshine",
    "amouage existence": "amouage existence",
    "amouage dia": "amouage dia",
    "amouage epic": "amouage epic",
    "amouage hibiscus": "amouage love hibiscus",
    "love hibiscus amouage": "amouage love hibiscus",
    "amouage blossom love": "amouage blossom love",
    "amouage blossom": "amouage blossom love",
    "amouage delight": "amouage love delight",
    "love delight amouage": "amouage love delight",
    "amouage tuberose": "amouage love tuberose",
    "love tuberose amouage": "amouage love tuberose",
    "amouage purpose": "amouage purpose",
    "amouage outlands": "amouage outlands",
    "amouage imitation": "amouage imitation",
    "amouage memoir": "amouage memoir",
    "amouage jubilation": "amouage jubilation",
    "amouage journey": "amouage journey",
    "amouage sequence": "amouage sequence",
    "amouage lineage": "amouage lineage",
    "amouage overture": "amouage overture",
    "amouage lyric": "amouage lyric",
    "amouage crimson": "amouage crimson rocks",
    "amouage rocks": "amouage crimson rocks",
    "royal tobacco amouage": "amouage opus 14 royal tobacco",
    "amouage tobacco": "amouage opus 14 royal tobacco",
}

# Wordstat-derived Russian XERJOFF brand spellings.
# Works for longer queries too: «ксерджофф эрба» -> XERJOFF + ERBA.
QUERY_ALIASES = {
    "ксерджофф": "xerjoff",
    "ксерджоф": "xerjoff",
    "ксерджов": "xerjoff",
    "ксерйофф": "xerjoff",
    "ксерйоф": "xerjoff",
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
    "блу де шанель": "bleu de chanel",
    "блю де шанель": "bleu de chanel",
    "блу бе шанель": "bleu de chanel",
    "блю бе шанель": "bleu de chanel",
    "блу шанель": "bleu de chanel",
    "блю шанель": "bleu de chanel",
    "шанель шанс": "chanel chance",
    "шанел шанс": "chanel chance",
    "шанель шанc": "chanel chance",
    "крид": "creed",
    "версаче ерос": "versace eros",
    "версаче эрос": "versace eros",
    "версаче ерос": "versace eros",
    "эpoс": "eros",
    "эрос": "eros",
}

# Data-driven Wordstat aliases for XERJOFF.
# Built from the user's exports for 15.08.2026–15.09.2026.
# Only unambiguous matches to an existing XERJOFF fragrance in the price list are included.
WORDSTAT_XERJOFF_ALIASES = {
    "xerjoff erba pura": "xerjoff erba pura",
    "xerjoff 1861 naxos": "xerjoff 1861 naxos",
    "xerjoff accento": "xerjoff accento",
    "xerjoff casamorati 1888": "xerjoff casamorati 1888",
    "xerjoff xj 1861 naxos": "xerjoff 1861 naxos",
    "духи xerjoff erba pura": "xerjoff erba pura",
    "xerjoff erba gold": "xerjoff erba gold",
    "torino21 xerjoff": "xerjoff torino21",
    "tony iommi monkey special xerjoff": "xerjoff tony iommi monkey special",
    "xerjoff elle": "xerjoff elle",
    "apollonia xerjoff": "xerjoff apollonia",
    "louis xv 1722 xerjoff": "xerjoff louis xv 1722",
    "xerjoff opera": "xerjoff opera",
    "xerjoff 40 knots": "xerjoff 40 knots",
    "xerjoff alexandria ii": "xerjoff alexandria ii",
    "la capitale xerjoff": "xerjoff la capitale",
    "erba pura xerjoff отзывы": "xerjoff erba pura",
    "xerjoff louis xv 1722 rose": "xerjoff louis xv 1722",
    "xerjoff coro": "xerjoff coro",
    "xerjoff aqua regia": "xerjoff aqua regia",
    "xerjoff 1861 naxos отзывы": "xerjoff 1861 naxos",
    "xerjoff sospiro erba pura": "xerjoff erba pura",
    "аромат xerjoff erba pura": "xerjoff erba pura",
    "xerjoff casamorati 1888 tempio": "xerjoff casamorati 1888",
    "xerjoff erba pura купить": "xerjoff erba pura",
    "xerjoff xj 1861 naxos отзывы": "xerjoff 1861 naxos",
    "xerjoff de venoge louis xv 1722": "xerjoff louis xv 1722",
    "xerjoff 1861 naxos описание": "xerjoff 1861 naxos",
    "xerjoff духи женские erba pura": "xerjoff erba pura",
    "xerjoff muse": "xerjoff muse",
    "xerjoff духи accento": "xerjoff accento",
    "xerjoff laylati": "xerjoff laylati",
    "xerjoff ouverture": "xerjoff ouverture",
    "1861 naxos xerjoff аромат": "xerjoff 1861 naxos",
    "парфюм xerjoff erba pura": "xerjoff erba pura",
    "xerjoff 1861 renaissance": "xerjoff 1861 renaissance",
    "xerjoff newcleus": "xerjoff newcleus",
    "pikovaya dama xerjoff": "xerjoff pikovaya dama",
    "xerjoff sospiro accento": "xerjoff accento",
    "xerjoff soprano": "xerjoff soprano",
    "amabile xerjoff": "xerjoff amabile",
    "xerjoff erba pura цена": "xerjoff erba pura",
    "xerjoff de venoge louis xv 1722 ros": "xerjoff louis xv 1722",
    "xerjoff 1861 decas": "xerjoff 1861 decas",
    "xerjoff erba pura описание": "xerjoff erba pura",
    "xerjoff symphonium": "xerjoff symphonium",
    "elle xerjoff парфюм": "xerjoff elle",
    "xerjoff ivory route": "xerjoff ivory route",
    "xerjoff holysm": "xerjoff holysm",
    "xerjoff kemi": "xerjoff kemi",
    "xerjoff shooting stars gibeon": "xerjoff shooting stars gibeon",
    "xerjoff erba pura ноты": "xerjoff erba pura",
    "xerjoff allende": "xerjoff allende",
    "jtc 400 xerjoff": "xerjoff jtc 400",
    "xerjoff пиковая дама": "xerjoff pikovaya dama",
    "xerjoff erba pura парфюмерная": "xerjoff erba pura",
    "парфюмерная вода xerjoff erba pura": "xerjoff erba pura",
    "xerjoff wardasina": "xerjoff wardasina",
    "xj 1861 decas xerjoff": "xerjoff 1861 decas",
    "xerjoff erba pura фрагрантика": "xerjoff erba pura",
    "xerjoff 1861 naxos фрагрантика": "xerjoff 1861 naxos",
    "xerjoff k bridge club": "xerjoff k`bridge club",
    "xerjoff groove xcape": "xerjoff groove xcape",
    "xerjoff homme": "xerjoff homme",
    "xerjoff erba gold отзывы": "xerjoff erba gold",
    "xj 1861 renaissance xerjoff": "xerjoff 1861 renaissance",
    "xerjoff accento отзывы": "xerjoff accento",
    "xj 1861 naxos xerjoff описание": "xerjoff 1861 naxos",
    "empiryan xerjoff": "xerjoff empiryan",
    "пиковая дама парфюм xerjoff": "xerjoff pikovaya dama",
    "xerjoff erba pura 100": "xerjoff erba pura",
    "torino25 xerjoff": "xerjoff torino25",
    "xerjoff golden moka": "xerjoff golden moka",
    "xerjoff luna": "xerjoff luna",
    "xerjoff xj 1861 naxos фрагрантика": "xerjoff 1861 naxos",
    "xerjoff erba pura как произносится": "xerjoff erba pura",
    "xerjoff casamorati 1888 отзывы": "xerjoff casamorati 1888",
    "xerjoff 1861 naxos купить": "xerjoff 1861 naxos",
    "torino22 xerjoff": "xerjoff torino22",
    "духи xerjoff erba pura цена": "xerjoff erba pura",
    "xerjoff elle купить": "xerjoff elle",
    "xerjoff erba pura описание аромата": "xerjoff erba pura",
    "xerjoff erba pura как произносится на русском": "xerjoff erba pura",
    "xerjoff elle отзывы": "xerjoff elle",
    "xerjoff jtc 40 knots": "xerjoff 40 knots",
    "xerjoff v erba pura": "xerjoff erba pura",
    "astaral xerjoff": "xerjoff astaral",
    "xerjoff aurum": "xerjoff aurum",
    "xerjoff golden dallah": "xerjoff golden dallah",
    "40 knots xerjoff фрагрантика": "xerjoff 40 knots",
    "xerjoff ilm": "xerjoff 'ilm",
    "xerjoff erba pura edp": "xerjoff erba pura",
    "elle xerjoff парфюм купить": "xerjoff elle",
    "xerjoff erba pura аналоги": "xerjoff erba pura",
    "xerjoff erba pura духи описание": "xerjoff erba pura",
    "erba pura xerjoff fragrantica": "xerjoff erba pura",
    "hayat xerjoff": "xerjoff hayat",
    "xerjoff tony iommi monkey special купить": "xerjoff tony iommi monkey special",
    "layla xerjoff": "xerjoff layla",
    "духи xerjoff erba pura отзывы": "xerjoff erba pura",
    "xerjoff erba pura мужские": "xerjoff erba pura",
    "xerjoff erba pura оригинал": "xerjoff erba pura",
    "torino24 xerjoff": "xerjoff torino24",
    "xerjoff erba gold фрагрантика": "xerjoff erba gold",
    "xerjoff духи erba gold": "xerjoff erba gold",
    "xerjoff rose gold": "xerjoff rose gold",
    "xerjoff xj 1861 naxos ноты": "xerjoff 1861 naxos",
    "xj 1861 naxos xerjoff купить": "xerjoff 1861 naxos",
    "xerjoff sospiro opera": "xerjoff opera",
    "golden green xerjoff": "xerjoff golden green",
    "xerjoff elle духи": "xerjoff elle",
    "xj 1861 naxos xerjoff описание аромата": "xerjoff 1861 naxos",
    "xerjoff jtc comandante": "xerjoff jtc comandante",
    "xerjoff tony iommi monkey special отзывы": "xerjoff tony iommi monkey special",
    "xerjoff jtc torino21": "xerjoff torino21",
    "xerjoff shooting stars la capitale": "xerjoff la capitale",
    "accento xerjoff парфюм": "xerjoff accento",
    "opera xerjoff духи": "xerjoff opera",
    "xerjoff erba pura духи женские цена": "xerjoff erba pura",
    "xerjoff jtc more than words": "xerjoff jtc more than words",
    "xerjoff apollonia отзывы": "xerjoff apollonia",
    "xerjoff 40 knots отзывы": "xerjoff 40 knots",
    "erba pura духи xerjoff купить": "xerjoff erba pura",
    "xerjoff 1861 zefiro": "xerjoff 1861 zefiro",
    "xerjoff coro отзывы": "xerjoff coro",
    "xerjoff shooting stars nio": "xerjoff shooting stars nio",
    "erba pura xerjoff летуаль": "xerjoff erba pura",
    "xerjoff coro 2025": "xerjoff coro",
    "xerjoff tony iommi deified": "xerjoff tony iommi deified",
    "xerjoff casamorati 1888 фрагрантика": "xerjoff casamorati 1888",
    "xerjoff aqua regia отзывы": "xerjoff aqua regia",
    "xerjoff jtc don": "xerjoff jtc don",
    "xerjoff alexandria orientale": "xerjoff alexandria orientale",
    "330 xj 1861 naxos xerjoff": "xerjoff 1861 naxos",
    "xerjoff erba pura перевод": "xerjoff erba pura",
    "xerjoff accento фрагрантика": "xerjoff accento",
    "xerjoff torino23": "xerjoff torino23",
    "xerjoff erba pura 100 мл": "xerjoff erba pura",
    "erba gold xerjoff купить": "xerjoff erba gold",
    "xerjoff erba pura пирамида": "xerjoff erba pura",
    "xerjoff la capitale духи": "xerjoff la capitale",
    "xerjoff 40 knots fragrantica": "xerjoff 40 knots",
    "xerjoff amber gold": "xerjoff amber gold",
    "xerjoff amabile отзывы": "xerjoff amabile",
    "xerjoff accento мужские": "xerjoff accento",
    "xerjoff erba pura духи женские описание аромата": "xerjoff erba pura",
    "xerjoff ouverture отзывы": "xerjoff ouverture",
    "la capitale xerjoff отзывы": "xerjoff la capitale",
    "xerjoff opera отзывы": "xerjoff opera",
    "xerjoff erba pura духи мужские": "xerjoff erba pura",
    "xerjoff erba pura золотое": "xerjoff erba pura",
    "xerjoff erba pura золотое яблоко": "xerjoff erba pura",
    "xerjoff torino21 отзывы": "xerjoff torino21",
    "духи apollonia xerjoff": "xerjoff apollonia",
    "парфюм xerjoff opera": "xerjoff opera",
    "xerjoff erba pura 100ml": "xerjoff erba pura",
    "xerjoff elle фрагрантика": "xerjoff elle",
    "xerjoff erba pura ноты аромата": "xerjoff erba pura",
    "xerjoff accento edp": "xerjoff accento",
    "xerjoff apollonia фрагрантика": "xerjoff apollonia",
    "xerjoff torino21 купить": "xerjoff torino21",
    "xerjoff accento купить": "xerjoff accento",
    "духи xerjoff xj 1861 naxos": "xerjoff 1861 naxos",
    "эрба пура xerjoff": "xerjoff erba pura",
    "erba pura gold xerjoff": "xerjoff erba pura",
    "louis xv 1722 xerjoff отзывы": "xerjoff louis xv 1722",
    "xerjoff tempest": "xerjoff tempest",
    "xerjoff erba pura перевод на русский": "xerjoff erba pura",
    "xerjoff духи shooting stars gibeon": "xerjoff shooting stars gibeon",
    "xerjoff casamorati 1888 edp": "xerjoff casamorati 1888",
    "xerjoff coro купить": "xerjoff coro",
    "xerjoff erba pura состав": "xerjoff erba pura",
    "унисекс аромат xerjoff xj 1861 naxos": "xerjoff 1861 naxos",
    "xerjoff духи soprano": "xerjoff soprano",
    "xerjoff erba pura как читается": "xerjoff erba pura",
    "купить женские духи xerjoff erba pura": "xerjoff erba pura",
    "xerjoff 1861 decas отзывы": "xerjoff 1861 decas",
    "xerjoff symphonium parfum": "xerjoff symphonium",
    "xerjoff aurum духи": "xerjoff aurum",
    "pikovaya dama 2018 xerjoff": "xerjoff pikovaya dama",
    "xerjoff tony iommi monkey special обзор парфюма": "xerjoff tony iommi monkey special",
    "xerjoff tony iommi monkey special фрагана": "xerjoff tony iommi monkey special",
    "xerjoff xerjoff k empiryan": "xerjoff empiryan",
    "xerjoff v erba gold": "xerjoff erba gold",
    "xj 1861 zefiro xerjoff": "xerjoff 1861 zefiro",
    "xerjoff torino21 фрагрантика": "xerjoff torino21",
    "xerjoff louis xv 1722 rose отзывы": "xerjoff louis xv 1722",
    "xerjoff opera фрагрантика": "xerjoff opera",
    "туалетная вода xerjoff erba pura": "xerjoff erba pura",
    "парфюмерная вода xerjoff erba pura отзывы": "xerjoff erba pura",
    "xerjoff erba gold описание": "xerjoff erba gold",
    "erba pura xerjoff 25 мл": "xerjoff erba pura",
    "xerjoff irisss": "xerjoff irisss",
    "xj 1861 naxos xerjoff цена": "xerjoff 1861 naxos",
    "xerjoff coro фрагрантика": "xerjoff coro",
    "duran duran black moonlight xerjoff": "xerjoff duran duran black moonlight",
    "xerjoff p 33 blank page edition": "xerjoff p.33 blank page edition",
    "xerjoff shooting stars cruz del sur ii": "xerjoff shooting stars cruz del sur ii",
    "xerjoff aqua regia купить": "xerjoff aqua regia",
    "xerjoff erba pura духи тестер": "xerjoff erba pura",
    "xerjoff empiryan parfum описание": "xerjoff empiryan",
    "xerjoff opera купить": "xerjoff opera",
    "xerjoff v accento": "xerjoff accento",
    "xerjoff sospiro erba gold": "xerjoff erba gold",
    "xerjoff holysm отзывы": "xerjoff holysm",
    "xerjoff soprano отзывы": "xerjoff soprano",
    "xerjoff accento описание": "xerjoff accento",
    "xerjoff muse духи": "xerjoff muse",
    "xerjoff muse отзывы": "xerjoff muse",
    "xerjoff erba pura дезодорант": "xerjoff erba pura",
    "духи xerjoff alexandria ii": "xerjoff alexandria ii",
    "xerjoff laylati отзывы": "xerjoff laylati",
    "xerjoff sospiro laylati": "xerjoff laylati",
    "xerjoff jtc fatal charme": "xerjoff jtc fatal charme",
    "xerjoff xj 1861 naxos edp 100 ml": "xerjoff 1861 naxos",
    "xerjoff ouverture в стерлитамаке купить": "xerjoff ouverture",
    "xerjoff ether": "xerjoff ether",
    "save me xerjoff": "xerjoff save me",
    "что за аромат у xerjoff golden moka": "xerjoff golden moka",
    "xerjoff alexandria ii fragrantica": "xerjoff alexandria ii",
    "xerjoff erba gold fragrantica": "xerjoff erba gold",
    "xerjoff erba pura духи оригинал": "xerjoff erba pura",
    "xerjoff erba pura чем пахнут": "xerjoff erba pura",
    "xerjoff coro 2025 отзывы": "xerjoff coro",
    "парфюмерная вода xerjoff torino21": "xerjoff torino21",
    "xerjoff xi 1861 naxos": "xerjoff 1861 naxos",
    "xerjoff casamorati 1888 tempio dacqua": "xerjoff casamorati 1888",
    "xerjoff join the club torino22": "xerjoff torino22",
    "xerjoff alexandria ii фрагрантика": "xerjoff alexandria ii",
    "xerjoff wardasina отзывы": "xerjoff wardasina",
    "xerjoff sospiro opera grande": "xerjoff opera",
    "xerjoff 40 knots купить": "xerjoff 40 knots",
    "xerjoff de venoge louis xv 1722 2026": "xerjoff louis xv 1722",
    "xerjoff accento ноты": "xerjoff accento",
    "парфюм женский xerjoff erba pura": "xerjoff erba pura",
    "xerjoff erba pura духи женские отзывы": "xerjoff erba pura",
    "xerjoff tony iommi monkey special тестер": "xerjoff tony iommi monkey special",
    "xerjoff luna отзывы": "xerjoff luna",
    "xerjoff shooting stars nio parfum": "xerjoff shooting stars nio",
    "apollonia xerjoff купить": "xerjoff apollonia",
    "xerjoff groove xcape отзывы": "xerjoff groove xcape",
    "xerjoff erba pura отзывы покупателей": "xerjoff erba pura",
    "xerjoff opera fragrantica": "xerjoff opera",
    "xerjoff erba pura рив гош": "xerjoff erba pura",
    "xerjoff opera вес флакона": "xerjoff opera",
    "xerjoff sospiro accento viola": "xerjoff accento",
    "xerjoff erba pura духи ноты": "xerjoff erba pura",
    "erba gold xerjoff ноты": "xerjoff erba gold",
    "xerjoff shooting stars gibeon отзывы": "xerjoff shooting stars gibeon",
    "xerjoff valvet collection erba pura": "xerjoff erba pura",
    "xerjoff xjv coro": "xerjoff coro",
    "xerjoff sospiro erba pura magica": "xerjoff erba pura",
    "духи xerjoff erba pura перевод на русский": "xerjoff erba pura",
    "xerjoff xj 1861 naxos купить минск": "xerjoff 1861 naxos",
    "xerjoff jtc shunkoin": "xerjoff jtc shunkoin",
    "xerjoff casamorati levar del sole": "xerjoff casamorati levar del sole",
    "xerjoff erba pura как переводится": "xerjoff erba pura",
    "xerjoff la capitale фрагрантика": "xerjoff la capitale",
    "xerjoff shooting stars apollonia": "xerjoff apollonia",
    "xerjoff oud stars zafar": "xerjoff oud stars zafar",
    "xerjoff tony iommi monkey special фрагрантика": "xerjoff tony iommi monkey special",
    "xerjoff louis xv 1722 rose купить": "xerjoff louis xv 1722",
    "louis vuitton pacific chill vs xerjoff torino21": "xerjoff torino21",
    "xerjoff kemi отзывы": "xerjoff kemi",
    "xerjoff tony iommi monkey special купить пермь": "xerjoff tony iommi monkey special",
    "xerjoff newcleus твердые": "xerjoff newcleus",
    "омаж на xerjoff erba pura": "xerjoff erba pura",
    "rose gold xerjoff фрагрантика": "xerjoff rose gold",
    "духи xerjoff pikovaya dama отзывы": "xerjoff pikovaya dama",
    "парфюм xerjoff sospiro erba pura": "xerjoff erba pura",
    "xerjoff laylati купить": "xerjoff laylati",
    "xerjoff jtc k bridge club": "xerjoff k`bridge club",
    "xerjoff erba pura вид с низу": "xerjoff erba pura",
    "xerjoff torino22 и аналог": "xerjoff torino22",
    "xerjoff xj 1861 renaissance отзывы": "xerjoff 1861 renaissance",
    "kind of blue xerjoff": "xerjoff kind of blue",
    "xerjoff alexandria ii hair mist": "xerjoff alexandria ii",
    "xerjoff erba pura самый громкий и шлейфовый": "xerjoff erba pura",
    "xerjoff 1861 naxos духи мужские": "xerjoff 1861 naxos",
    "духи xerjoff erba gold в германии": "xerjoff erba gold",
    "xerjoff newcleus отзывы": "xerjoff newcleus",
    "xerjoff 17 17 elle": "xerjoff elle",
    "xerjoff la capitale золотое яблоко": "xerjoff la capitale",
    "xerjoff shooting stars gibeon new gibeon": "xerjoff shooting stars gibeon",
    "xerjoff духи shooting stars gibeon оригинал": "xerjoff shooting stars gibeon",
    "xerjoff pikovaya dama купить": "xerjoff pikovaya dama",
    "xerjoff jabir": "xerjoff jabir",
    "sospiro opera и xerjoff разница": "xerjoff opera",
    "xerjoff xxy": "xerjoff xxy",
    "xerjoff опера отзывы": "xerjoff opera",
    "xerjoff amabile на сайте рандеву": "xerjoff amabile",
    "erba pura xerjoff отзывы что за аромат": "xerjoff erba pura",
    "духи xerjoff erba pura тестер отзывы": "xerjoff erba pura",
    "xerjoff elle 30мл цена": "xerjoff elle",
    "xerjoff layla отзывы": "xerjoff layla",
    "xerjoff allende отзывы": "xerjoff allende",
    "xerjoff torino21 дезодорант": "xerjoff torino21",
    "xerjoff v erba pura u edp 2ml": "xerjoff erba pura",
    "xerjoff ivory route отзывы": "xerjoff ivory route",
    "xerjoff духи apollonia 50 мл": "xerjoff apollonia",
    "xerjoff alexandria ii отзывы": "xerjoff alexandria ii",
    "фрагрантика xerjoff groove xcape": "xerjoff groove xcape",
    "парфюм xerjoff erba pura отзывы": "xerjoff erba pura",
    "xerjoff erba pura цена дти фри щанхай": "xerjoff erba pura",
    "xerjoff erba pura мужские или женские": "xerjoff erba pura",
    "alexandria iii xerjoff": "xerjoff alexandria iii",
    "xerjoff ouverture в стерлитамаке купить пробник": "xerjoff ouverture",
    "xerjoff духи erba pura 100 31521n": "xerjoff erba pura",
    "духи coro xerjoff желтые": "xerjoff coro",
    "xerjoff 40 knots состав": "xerjoff 40 knots",
    "xerjoff х 1861 naxos": "xerjoff 1861 naxos",
    "xerjoff erba gold fragrantica отзывы": "xerjoff erba gold",
    "apollonia xerjoff пирамида": "xerjoff apollonia",
    "отзывы xj 1861 naxos xerjoff 100ml": "xerjoff 1861 naxos",
    "xerjoff newcleus не жидкие": "xerjoff newcleus",
    "erba pura xerjoff стойкость": "xerjoff erba pura",
    "xerjoff sospiro erba pura 50 ml основная": "xerjoff erba pura",
    "saffira xerjoff erba pura": "xerjoff erba pura",
    "torino25 xerjoff обзор": "xerjoff torino25",
    "xerjoff erba pura edp отзывы": "xerjoff erba pura",
    "xerjoff erba pura духи авито": "xerjoff erba pura",
    "accento xerjoff это мужские или женские": "xerjoff accento",
    "xerjoff xj 1861 naxos пробник купить": "xerjoff 1861 naxos",
    "xerjoff 40 knots разлив": "xerjoff 40 knots",
    "xerjoff coro пирамида": "xerjoff coro",
    "xerjoff casamorati 1888 levar del sole": "xerjoff casamorati 1888",
    "xerjoff erba pura новый дизайн": "xerjoff erba pura",
    "ксерджофф эрба пура": "xerjoff erba pura",
    "духи ксерджофф эрба пура": "xerjoff erba pura",
    "пиковая дама ксерджофф": "xerjoff pikovaya dama",
    "ксерджофф эрба голд": "xerjoff erba gold",
    "ксерджофф эрба пура описание": "xerjoff erba pura",
    "ксерджофф опера": "xerjoff opera",
    "аромат ксерджофф эрба пура": "xerjoff erba pura",
    "ксерджофф эрба пура описание аромата": "xerjoff erba pura",
    "пиковая дама ксерджофф парфюм": "xerjoff pikovaya dama",
    "ксерджофф эрба пура отзывы": "xerjoff erba pura",
    "ксерджофф 1861 naxos": "xerjoff 1861 naxos",
    "парфюм ксерджофф эрба пура": "xerjoff erba pura",
    "ксерджофф лайлати": "xerjoff laylati",
    "ксерджофф 40 кнотс": "xerjoff 40 knots",
    "ксерджофф эрба пура фрагрантика": "xerjoff erba pura",
    "ксерджофф соспиро эрба пура": "xerjoff erba pura",
    "ксерджофф ерба пура": "xerjoff erba pura",
    "ксерджофф опера отзывы": "xerjoff opera",
    "ксерджофф эрба голд отзывы": "xerjoff erba gold",
    "опера ксерджофф парфюм": "xerjoff opera",
    "ксерджофф элле": "xerjoff elle",
    "ксерджофф erba pura": "xerjoff erba pura",
    "ксерджофф сопрано": "xerjoff soprano",
    "ксерджофф 1861 naxos отзывы": "xerjoff 1861 naxos",
    "ксерджофф эрба пура описание аромата и отзывы": "xerjoff erba pura",
    "духи ксерджофф эрба пура женские": "xerjoff erba pura",
    "отзывы о парфюме ксерджофф эрба пура": "xerjoff erba pura",
    "парфюм ксерджофф symphonium": "xerjoff symphonium",
    "ксерджофф эрба пура фото люкс": "xerjoff erba pura",
    "ксерджофф аурум отзывы": "xerjoff aurum",
    "парфюм ксерджофф аурум отзывы": "xerjoff aurum",
    "парфюмерное масло эрба пура от ксерджофф": "xerjoff erba pura",
    "ксерджофф erba gold": "xerjoff erba gold",
    "духи мусе ксерджофф": "xerjoff muse",
    "ксерджофф луна парфюм": "xerjoff luna",
    "ксерджофф пиковая дама распаковка": "xerjoff pikovaya dama"
}

QUERY_ALIASES.update(WORDSTAT_XERJOFF_ALIASES)
# Data-driven Wordstat aliases for PARFUMS DE MARLY.
# Built from the user's exports for 15.08.2026–15.09.2026.
# Only queries containing an unambiguous fragrance name from the PARFERA price list are included.
WORDSTAT_PDM_ALIASES = {
    "parfums de marly althair": "parfums de marly althair",
    "parfums de marly delina": "parfums de marly delina",
    "valaya parfums de marly": "parfums de marly valaya",
    "parfums de marly altair": "parfums de marly althair",
    "parfums de marly layton": "parfums de marly layton",
    "parfums de marly delina exclusif": "parfums de marly delina exclusif",
    "athenais parfums de marly": "parfums de marly athenais",
    "valaya exclusif parfums de marly": "parfums de marly valaya exclusif",
    "parfums de marly sedley": "parfums de marly sedley",
    "parfums de marly perseus": "parfums de marly perseus",
    "herod parfums de marly": "parfums de marly herod",
    "parfums de marly percival": "parfums de marly percival",
    "parfums de marly la rosee": "parfums de marly delina la rosee",
    "delina de rosee parfums marly": "parfums de marly delina la rosee",
    "parfums de marly althair exclusif": "parfums de marly althair exclusif",
    "haltane parfums de marly": "parfums de marly haltane",
    "parfums de marly meliora": "parfums de marly meliora",
    "parfums de marly oriana": "parfums de marly oriana",
    "oajan parfums de marly": "parfums de marly oajan",
    "parfums de marly paris": "parfums de marly perseus",
    "parfums de marly pegasus": "parfums de marly pegasus",
    "greenley parfums de marly": "parfums de marly greenley",
    "palatine parfums de marly": "parfums de marly palatine",
    "parfums de marly layton exclusif": "parfums de marly layton exclusif",
    "parfums de marly castley": "parfums de marly castley",
    "delina la ros e parfums de marly": "parfums de marly delina la rosee",
    "parfums de marly cassili": "parfums de marly cassili",
    "parfums de marly valero": "parfums de marly valero",
    "greenly parfums de marly": "parfums de marly greenley",
    "parfums de marly safanad": "parfums de marly safanad",
    "parfums de marly altair exclusif": "parfums de marly althair exclusif",
    "kalan parfums de marly": "parfums de marly kalan",
    "parfums de marly pegasus exclusif": "parfums de marly pegasus exclusif",
    "parfums de marly carlisle": "parfums de marly carlisle",
    "parfums de marly godolphin": "parfums de marly godolphin",
    "athalia parfums de marly": "parfums de marly athalia",
    "parfums de marly eragon": "parfums de marly eragon",
    "parfums de marly galloway": "parfums de marly galloway",
    "parfums de marly carios": "parfums de marly carios",
    "parfums de marly la rose": "parfums de marly delina la rosee",
    "parfums de marly castle": "parfums de marly castley",
    "parfums de marly delina rose": "parfums de marly delina la rosee",
    "parfums de marly altair fragrantica": "parfums de marly althair",
    "parfums de marly delina la rose": "parfums de marly delina la rosee",
    "delina parfums de marly fragrantica": "parfums de marly delina",
    "parfums de marly byerley": "parfums de marly byerley",
    "parfums de marly darcy": "parfums de marly darcy",
    "parfums de marly layton fragrantica": "parfums de marly layton",
    "альтаир parfums de marly": "parfums de marly althair",
    "althar parfums de marly": "parfums de marly althair",
    "valaya parfums de marly fragrantica": "parfums de marly valaya",
    "parfums de marly althair fragrantica": "parfums de marly althair",
    "delina exclusif parfums de marly fragrantica": "parfums de marly delina exclusif",
    "althar parfums de marly fragrantica": "parfums de marly althair",
    "parfums de marly shagya": "parfums de marly shagya",
    "valya parfums de marly": "parfums de marly valaya",
    "parfums de marly deline": "parfums de marly delina",
    "parfums de marly nisean": "parfums de marly nisean",
    "darley parfums de marly": "parfums de marly darley",
    "parfums de marly althair m": "parfums de marly althair",
    "parfums de marly althair альтаир": "parfums de marly althair exclusif",
    "sidley parfums de marly": "parfums de marly sedley",
    "parfums de marly kuhuyan": "parfums de marly kuhuyan",
    "parfums de marly delina делина": "parfums de marly delina exclusif",
    "parfums de marly altheir": "parfums de marly althair",
    "parfums de marly sedley fragrantica": "parfums de marly sedley",
    "perseus parfums de marly fragrantica": "parfums de marly perseus",
    "parfums de marly althaiir": "parfums de marly althair",
    "parfums de marly palantine": "parfums de marly palatine",
    "parfums de marly habdan": "parfums de marly habdan",
    "parfums de marly layton m": "parfums de marly layton",
    "parfums de marly althair exlusif": "parfums de marly althair exclusif",
    "parfums de marly altair exclusif fragrantica": "parfums de marly althair exclusif",
    "parfums de marly haltane fragrantica": "parfums de marly haltane",
    "palantin parfums de marly": "parfums de marly palatine",
    "parfums de marly athenais fragrantica": "parfums de marly athenais",
    "parfums de marly лейтон": "parfums de marly layton",
    "cassili parfums de marly fragrantica": "parfums de marly cassili",
    "parfums de marly oajan fragrantica": "parfums de marly oajan",
    "kalan parfums de marly fragrantica": "parfums de marly kalan",
    "parfums de marly pegasus fragrantica": "parfums de marly pegasus",
    "parfums de marly layton exclusif fragrantica": "parfums de marly layton exclusif",
    "parfums de marly orian": "parfums de marly oriana",
    "parfums de marly akaster": "parfums de marly akaster"
}

WORDSTAT_PDM_ALIASES.pop("parfums de marly althair альтаир", None)
WORDSTAT_PDM_ALIASES.pop("parfums de marly delina делина", None)
QUERY_ALIASES.update(WORDSTAT_PDM_ALIASES)
QUERY_ALIASES.update(WORDSTAT_AMOUAGE_ALIASES)

RU_TO_EN = str.maketrans({
    "а":"a","б":"b","в":"v","г":"g","д":"d","е":"e","ё":"e","ж":"zh","з":"z","и":"i","й":"y",
    "к":"k","л":"l","м":"m","н":"n","о":"o","п":"p","р":"r","с":"s","т":"t","у":"u","ф":"f",
    "х":"kh","ц":"ts","ч":"ch","ш":"sh","щ":"shch","ъ":"","ы":"y","ь":"","э":"e","ю":"yu","я":"ya"
})

@lru_cache(maxsize=4096)
def normalize_ai_query(text: str) -> str:
    """Normalize a customer query through a bounded, word-safe alias chain.

    Word boundaries are essential: an alias such as «... delina la rose» must
    not repeatedly match the beginning of «... delina la rosee».
    """
    q = norm(text).replace("’", "'")
    used_aliases = set()
    for _ in range(5):
        changed = False
        for pattern, alias_dst, alias_src in QUERY_ALIAS_PATTERNS:
            # Each directed alias may fire at most once. This prevents an
            # expanding Wordstat alias such as "amouage blossom" ->
            # "amouage blossom love" from expanding itself repeatedly.
            if alias_src in used_aliases:
                continue
            new_q, count = pattern.subn(alias_dst, q)
            if count and new_q != q:
                q = new_q
                used_aliases.add(alias_src)
                changed = True
                break
        if not changed:
            break
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


def _brand_match_score(query_token: str, brand_token: str) -> float:
    """Similarity for identifying a brand, deliberately stricter than perfume-name matching."""
    a = canonical_token(query_token).lower()
    b = canonical_token(brand_token).lower()
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    if a.replace("y", "i") == b.replace("y", "i"):
        return 0.97
    # Common Cyrillic transliteration variants are often one letter apart.
    return SequenceMatcher(None, a, b).ratio()


# Catalog-wide brand aliases.
# Exact aliases remain useful for irregular names, while the phonetic layer
# below generates Russian forms for every brand present in the catalog.
BRAND_QUERY_ALIASES = {
    "парфюм де марли": "PARFUMS DE MARLY",
    "парфюмс де марли": "PARFUMS DE MARLY",
    "парфамс де марли": "PARFUMS DE MARLY",
    "парфюм дэ марли": "PARFUMS DE MARLY",
    "парфюмс дэ марли": "PARFUMS DE MARLY",
    "amouage": "AMOUAGE",
    "амуаж": "AMOUAGE",
    "амоуаж": "AMOUAGE",
    "амуж": "AMOUAGE",
    "амуаш": "AMOUAGE",
    "амуаг": "AMOUAGE",
}

# Synchronize explicit brand aliases with the main query normalizer.
# This is catalog-wide: any Russian/phonetic brand alias can be removed from
# the query as a canonical Latin brand before fragrance-name matching.
# Specific full-query Wordstat aliases remain authoritative because setdefault
# never overwrites them.
for _brand_alias_src, _brand_alias_dst in BRAND_QUERY_ALIASES.items():
    QUERY_ALIASES.setdefault(norm(_brand_alias_src), norm(_brand_alias_dst))

# Drop self-expanding aliases where the source phrase is literally contained
# in its own destination (for example "amouage blossom" ->
# "amouage blossom love"). Such rows are useful as Wordstat hints but unsafe
# as iterative normalizers because they can grow on every pass.
for _src, _dst in list(QUERY_ALIASES.items()):
    if norm(_src) != norm(_dst) and norm(_src) in norm(_dst):
        QUERY_ALIASES.pop(_src, None)

# Precompile all query-alias patterns once. The alias table is static after
# startup; recompiling dozens of regular expressions for every message is
# unnecessary and can make repeated searches slow.
QUERY_ALIAS_PATTERNS = [
    (re.compile(rf"(?<![a-z0-9а-яё]){re.escape(alias_src)}(?![a-z0-9а-яё])", re.I), alias_dst, alias_src)
    for alias_src, alias_dst in sorted(QUERY_ALIASES.items(), key=lambda pair: len(pair[0]), reverse=True)
    if alias_src
]


def _latin_brand_to_cyrillic_variants(value: str) -> List[str]:
    """Generate a small set of Russian phonetic spellings for a Latin brand."""
    s = re.sub(r"[^a-z0-9 ]+", " ", norm(value))
    s = re.sub(r"\s+", " ", s).strip()
    if not s:
        return []

    digraphs = [
        ("shch", "щ"), ("sch", "щ"), ("zh", "ж"), ("ch", "ч"),
        ("sh", "ш"), ("kh", "х"), ("ts", "ц"), ("yu", "ю"),
        ("ya", "я"), ("yo", "ё"), ("ph", "ф"),
    ]
    singles = {
        "a":"а","b":"б","c":"к","d":"д","e":"е","f":"ф","g":"г",
        "h":"х","i":"и","j":"й","k":"к","l":"л","m":"м","n":"н",
        "o":"о","p":"п","q":"к","r":"р","s":"с","t":"т","u":"у",
        "v":"в","w":"в","x":"кс","y":"й","z":"з"
    }

    def translit(word: str) -> str:
        out, i = [], 0
        while i < len(word):
            matched = False
            for src, dst in digraphs:
                if word.startswith(src, i):
                    out.append(dst)
                    i += len(src)
                    matched = True
                    break
            if not matched:
                out.append(singles.get(word[i], word[i]))
                i += 1
        return "".join(out)

    base = " ".join(translit(w) for w in s.split())
    variants = {base}

    # Common Russian spelling variants for a few Latin letters.
    variants.add(base.replace("кс", "х"))
    variants.add(base.replace("й", "и"))
    variants.add(base.replace("й", "дж"))
    variants.add(base.replace("дж", "ж"))
    variants.add(base.replace("в", "у"))
    variants.add(base.replace("е", "э"))
    # Common Russian spellings can drop/reduce transliteration consonants.
    variants.add(base.replace("ф", "в"))
    variants.add(base.replace("г", "ж"))
    return [v for v in variants if v]


def _brand_alias_similarity(query_token: str, brand_key: str) -> float:
    """Compare a user token against both Latin and generated Russian brand forms."""
    q = norm(query_token)
    if not q:
        return 0.0

    label = norm(BRAND_DISPLAY.get(brand_key, brand_key))
    candidates = [label]
    candidates += _latin_brand_to_cyrillic_variants(label)

    best = 0.0
    for cand in candidates:
        for part in re.findall(r"[a-z0-9а-яё]+", cand.lower()):
            # Compare in the user's script first; also compare transliterated forms.
            best = max(best, _brand_match_score(q, part))
            q_lat = q.translate(RU_TO_EN)
            part_lat = part.translate(RU_TO_EN)
            best = max(best, _brand_match_score(q_lat, part_lat))
    return best



# Bounded brand index. Brand resolution must not compare every query token with
# every catalog brand. Exact component and one-edit deletion keys are enough for
# normal spelling/typo cases; irregular Russian spellings are covered by the
# catalog-wide aliases above.
_BRAND_COMPONENT_INDEX: Optional[Dict[str, set]] = None
_BRAND_COMPONENT_DELETE_INDEX: Optional[Dict[str, set]] = None

def _build_brand_component_indexes():
    global _BRAND_COMPONENT_INDEX, _BRAND_COMPONENT_DELETE_INDEX
    if _BRAND_COMPONENT_INDEX is not None:
        return
    generic = {
        "the", "eau", "ea", "parfum", "parfums", "perfume", "collection",
        "collector", "for", "men", "women", "woman", "homme", "femme",
        "original", "house", "de", "di", "du", "des", "et", "of",
    }
    idx: Dict[str, set] = {}
    didx: Dict[str, set] = {}
    for key in BRAND_KEYS:
        label = norm(BRAND_DISPLAY.get(key, key)).translate(RU_TO_EN)
        for token in re.findall(r"[a-z0-9]+", label):
            if token in generic or len(token) < 4:
                continue
            idx.setdefault(token, set()).add(key)
            for i in range(len(token)):
                didx.setdefault(token[:i] + token[i + 1:], set()).add(key)
    _BRAND_COMPONENT_INDEX = idx
    _BRAND_COMPONENT_DELETE_INDEX = didx

@lru_cache(maxsize=4096)
def fuzzy_brand_key(text: str) -> Optional[str]:
    """Catalog-wide brand resolver.

    Important: many catalog brands are stored as multi-word houses (for example
    "Christian Dior"), while customers often type only the distinctive part
    ("Dior"). Therefore a single strong match to a distinctive brand component
    is enough to identify a multi-word house. Generic words such as THE, PARFUM,
    COLLECTION, etc. are never allowed to identify a brand on their own.

    This is intentionally dynamic: it uses the brands detected from the current
    catalog and does not contain one-off rules for Chanel, Dior, Versace, etc.
    """
    q_raw = norm(text)
    q_normalized = normalize_ai_query(text)

    # First recognize an exact canonical multi-word brand phrase anywhere in
    # the original OR normalized query. The normalized form is important for
    # Wordstat aliases such as «delina de rosee parfums marly», which become
    # «parfums de marly delina la rosee» before brand matching.
    for _key, _pattern in MULTIWORD_BRAND_PATTERNS:
        if _pattern.search(q_raw) or _pattern.search(q_normalized):
            return _key

    # Resolve known brand spellings inside longer queries.
    embedded_aliases = sorted(
        list(BRAND_QUERY_ALIASES.items()) +
        [("ксерджофф", "XERJOFF"), ("ксерджоф", "XERJOFF"),
         ("ксерджов", "XERJOFF"), ("ксерйофф", "XERJOFF"),
         ("ксерйоф", "XERJOFF")],
        key=lambda pair: len(pair[0]), reverse=True,
    )
    for alias_src, alias_brand in embedded_aliases:
        if re.search(rf"(?<![a-z0-9а-яё]){re.escape(alias_src)}(?![a-z0-9а-яё])", q_raw):
            for key in BRAND_KEYS:
                if norm(BRAND_DISPLAY.get(key, key)) == norm(alias_brand) or norm(key) == norm(alias_brand):
                    return key

    # Universal Russian/phonetic brand resolution. This runs before fragrance
    # matching, so a typo such as «амуж» can never become ASHORE or another
    # similarly spelled perfume name.
    raw_tokens = re.findall(r"[a-zа-яё0-9]+", q_raw)
    # Generic words such as «parfums» / «parfum» occur in many brand names
    # and must never select a brand by themselves. This is especially important
    # after a Russian multi-word house name has been partially normalized.
    _GENERIC_BRAND_QUERY_TOKENS = {
        "the", "eau", "parfum", "parfums", "perfume", "collection",
        "collector", "for", "men", "women", "woman", "homme", "femme",
        "original", "house", "de", "di", "du", "des", "et", "of",
        "парфюм", "парфюмы", "парфюмерия",
    }
    _build_brand_component_indexes()
    assert _BRAND_COMPONENT_INDEX is not None
    assert _BRAND_COMPONENT_DELETE_INDEX is not None

    for qt in raw_tokens:
        if len(qt) < 4 or qt in _GENERIC_BRAND_QUERY_TOKENS:
            continue

        # Exact distinctive component.
        candidate_keys = set(_BRAND_COMPONENT_INDEX.get(qt, set()))

        # One-edit typo in a distinctive component.
        if not candidate_keys:
            candidate_keys.update(_BRAND_COMPONENT_DELETE_INDEX.get(qt, set()))
            for i in range(len(qt)):
                candidate_keys.update(_BRAND_COMPONENT_DELETE_INDEX.get(qt[:i] + qt[i + 1:], set()))

        if candidate_keys:
            candidates = sorted(
                (( _brand_alias_similarity(qt, key), key) for key in candidate_keys),
                reverse=True,
            )
            top_score, top_key = candidates[0]
            second_score = candidates[1][0] if len(candidates) > 1 else 0.0
            if top_score >= 0.84 or (top_score >= 0.79 and top_score - second_score >= 0.07):
                return top_key

    q = normalize_ai_query(text)
    q_latin = q.translate(RU_TO_EN)
    q_tokens = re.findall(r"[a-z0-9]+", q_latin)
    if not q_tokens:
        return None

    generic = {
        "the", "ea", "eau", "parfum", "parfums", "perfume", "collection",
        "collector", "for", "men", "women", "woman", "homme", "femme",
        "original", "house", "de", "di"
    }

    # Count distinctive brand components. A component that belongs to only one
    # known house is safe to use for a one-word customer query.
    token_to_keys: Dict[str, set] = {}
    brand_tokens_by_key: Dict[str, List[str]] = {}
    for key in BRAND_KEYS:
        label = norm(BRAND_DISPLAY.get(key, key))
        bt = re.findall(r"[a-z0-9]+", label.translate(RU_TO_EN))
        bt = [b for b in bt if b not in generic]
        brand_tokens_by_key[key] = bt
        for b in set(bt):
            token_to_keys.setdefault(b, set()).add(key)

    # Only inspect brands already surfaced by the bounded component index.
    candidate_brand_keys = set()
    for qt in q_tokens:
        candidate_brand_keys.update(_BRAND_COMPONENT_INDEX.get(qt, set()))
        if len(qt) >= 4:
            candidate_brand_keys.update(_BRAND_COMPONENT_DELETE_INDEX.get(qt, set()))
            for i in range(len(qt)):
                candidate_brand_keys.update(_BRAND_COMPONENT_DELETE_INDEX.get(qt[:i] + qt[i + 1:], set()))

    best_key, best_score = None, 0.0
    for key in candidate_brand_keys:
        bt = brand_tokens_by_key.get(key, [])
        if not bt:
            continue

        # For each brand component find its best match in the user's query.
        component_matches = []
        for b in bt:
            best = max((_brand_match_score(qt, b) for qt in q_tokens), default=0.0)
            component_matches.append(best)

        strong = [v for v in component_matches if v >= 0.84]
        coverage = len(strong) / len(bt)
        avg = sum(component_matches) / len(component_matches)

        # Exact/near-exact phrase or full multi-word match is strongest.
        phrase_bonus = 0.20 if len(bt) > 1 and " ".join(bt) in q_latin else 0.0
        score = coverage * 0.55 + avg * 0.25 + phrase_bonus

        accepted = False

        # Full/near-full multi-word brand.
        if len(bt) > 1 and coverage >= 0.75 and avg >= 0.82:
            accepted = True

        # Customer wrote only one distinctive component, e.g. "dior" for
        # "Christian Dior", "malone" for "Jo Malone", etc.
        if not accepted:
            for qt in q_tokens:
                if qt in generic or len(qt) < 4:
                    continue
                for b in bt:
                    sim = _brand_match_score(qt, b)
                    if sim >= 0.90 and len(token_to_keys.get(b, set())) == 1:
                        single_score = 0.88 + min(0.10, (len(qt) - 4) * 0.01)
                        if single_score > best_score:
                            best_score = single_score
                            best_key = key
                        accepted = True
                        break
                if accepted:
                    break

        if accepted and score > best_score:
            best_score, best_key = score, key

    return best_key


DESCRIPTIVE_AI_WORDS = {
    "свежий", "свежая", "свежие", "сладкий", "сладкая", "сладкие", "легкий", "легкая",
    "легкие", "тяжелый", "теплый", "теплая", "цветочный", "цветочная", "древесный",
    "мускусный", "вечерний", "дневной", "осенний", "зимний", "летний", "весенний",
    "бергамот", "бергамотом", "ваниль", "ванилью", "роза", "розой", "мускус", "мускусом",
    "похожий", "похожая", "похожие", "похожее", "аромат", "аромата", "ароматы",
    "хочу", "ищу", "нужен", "нужна", "подбери", "подобрать", "посоветуй", "найди",
    "мужской", "мужская", "мужское", "женский", "женская", "женское", "унисекс",
    "до", "руб", "рублей", "на", "для", "мне", "с", "без", "осень", "зима", "лето", "весна"
}

def is_name_like_query(text: str) -> bool:
    tokens = ai_search_tokens(text)
    if not tokens or len(tokens) > 7:
        return False
    brand = fuzzy_brand_key(text)
    if brand:
        # With a recognized brand, the remaining words must look like a name,
        # otherwise leave it to the semantic AI flow.
        brand_tokens = set(ai_search_tokens(BRAND_DISPLAY.get(brand, brand)))
        rest = [t for t in tokens if all(token_similarity(t, b) < 0.76 for b in brand_tokens)]
        if not rest:
            return True
        return any(t not in DESCRIPTIVE_AI_WORDS and len(t) >= 3 for t in rest)
    return any(t not in DESCRIPTIVE_AI_WORDS and len(t) >= 4 for t in tokens)


def _infer_explicit_brand_from_query(query: str) -> Optional[str]:
    """Strict catalog-wide brand resolver used before fragrance-name matching."""
    return fuzzy_brand_key(query)


def ai_candidate_search(query: str = "", brand: str = "", gender: str = "", max_price: Optional[int] = None,
                        volume: Optional[int] = None, limit: int = 8) -> List[dict]:
    """Deterministic catalog search.

    An explicitly written brand is a HARD filter, even if OpenAI forgot to put
    the brand into the tool's separate `brand` argument. Name matching is done
    only against the remaining fragrance words, never against the brand word.
    """
    q = normalize_ai_query(query)
    tokens = ai_search_tokens(query)
    explicit_brand = _infer_explicit_brand_from_query(query) if not brand else None

    if brand:
        requested_brand = fuzzy_brand_key(brand) or fuzzy_brand_key(q)
    else:
        requested_brand = explicit_brand

    brand_q = normalize_ai_query(brand) if brand else (
        normalize_ai_query(BRAND_DISPLAY.get(explicit_brand, explicit_brand))
        if explicit_brand else ""
    )
    brand_tokens = ai_search_tokens(brand_q) if brand_q else []

    # Remove the brand from the name part. This is crucial for queries such as
    # "Chanel chans": "chanel" scopes the search, while "chans" identifies Chance.
    name_tokens = [
        t for t in tokens
        if not (brand_tokens and any(token_similarity(t, bt) >= 0.82 for bt in brand_tokens))
    ]

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

        if requested_brand is not None:
            if bkey != requested_brand:
                continue
        elif brand_q:
            if brand_q not in blabel and brand_q not in raw and brand_q.translate(RU_TO_EN) not in raw_latin:
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
        if name_tokens:
            raw_tokens = re.findall(r"[a-z0-9]+", raw_latin)
            sims = [max(_fragrance_token_score(t, rt) for rt in raw_tokens) for t in name_tokens] if raw_tokens else []
            # Every requested fragrance word must have a strong counterpart.
            # If a brand is explicitly known, allow a slightly wider typo
            # tolerance because the hard brand filter already protects us from
            # unrelated products (e.g. "Chanel chans" -> Chanel Chance).
            # A recognized brand is a hard safety boundary, so name matching
            # can tolerate stronger phonetic/transliteration errors.
            min_similarity = 0.72 if requested_brand is not None else 0.78
            if not sims or not all(v >= min_similarity for v in sims):
                continue

            exact_count = sum(
                1 for t in name_tokens
                if any(t == rt for rt in raw_tokens)
            )
            avg = sum(sims) / len(sims)
            score = 100 + avg * 40 + (exact_count / len(name_tokens)) * 120

            # Whole-word exact matches get a large advantage over partial
            # matches such as CHANCE -> CHANCERY.
            if all(v >= 0.90 for v in sims):
                score += 40
        elif requested_brand is not None or brand_q:
            # Brand-only request: show products from that brand.
            score = 20
        elif q:
            # No explicit brand: preserve broad catalogue search behaviour.
            raw_tokens = re.findall(r"[a-z0-9]+", raw_latin)
            if raw_tokens:
                sims = [max(_fragrance_token_score(t, rt) for rt in raw_tokens) for t in tokens]
                if not sims or not all(v >= 0.70 for v in sims):
                    continue
                score = 30 + sum(sims) * 8
            else:
                continue
        else:
            score = 1.0

        gk = group_key(p)
        group = unique_variants(visible_group(p))
        rep = next((variant for variant in group if variant.get("bottle_price_rub")), p)
        price = min(int(p.get("bottle_price_rub") or 10**9), int(p.get("tester_price_rub") or 10**9))
        scored.append((score, price, fragrance_title(rep).lower(), gk, rep))

    # One card per fragrance/concentration/gender.
    best_by_group = {}
    for row in scored:
        gk = row[3]
        if gk not in best_by_group or row[:3] > best_by_group[gk][:3]:
            best_by_group[gk] = row

    rows = list(best_by_group.values())
    rows.sort(key=lambda row: (-row[0], row[1], row[2]))
    return [row[4] for row in rows[:max(1, min(int(limit or 8), 12))]]


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



def _fast_base_name(p: dict) -> str:
    """Base fragrance name for ultra-fast local typo/transliteration matching."""
    name = norm(p.get("name", ""))
    name = re.sub(r"\b(?:edp|edt|parfum|parfume|extrait|eau de parfum|eau de toilette)\b", " ", name, flags=re.I)
    name = re.sub(r"\b\d+(?:[.,]\d+)?\s*ml\b", " ", name, flags=re.I)
    name = re.sub(r"\btester\b|\bпробник\b|\b(?:без крышки|с крышкой)\b", " ", name, flags=re.I)
    name = re.sub(r"\s*\((?:m|w|u)\)", " ", name, flags=re.I)
    return re.sub(r"\s+", " ", name).strip(" -·")


def _query_name_tokens(query: str, resolved_brand: Optional[str]) -> List[str]:
    """Return meaningful fragrance words after removing the resolved brand.

    Important for Russian brand spellings: a customer may write a house name
    phonetically (e.g. «парфюм де марли»). Transliteration token-by-token can
    turn that into several words that do not individually resemble the Latin
    brand stored in the catalog. Remove a resolved brand alias as a phrase
    before doing token-level matching.
    """
    q = norm(query)
    if resolved_brand:
        # First remove any explicit catalog-wide brand alias as a whole phrase.
        # This is generic: every alias registered in BRAND_QUERY_ALIASES is
        # handled, not only PARFUMS DE MARLY.
        for alias_src, alias_brand in sorted(
            BRAND_QUERY_ALIASES.items(), key=lambda pair: len(pair[0]), reverse=True
        ):
            if norm(alias_brand) == norm(BRAND_DISPLAY.get(resolved_brand, resolved_brand)):
                q = re.sub(
                    rf"(?<![a-z0-9а-яё]){re.escape(norm(alias_src))}(?![a-z0-9а-яё])",
                    " ",
                    q,
                )
        q = re.sub(r"\s+", " ", q).strip()

    tokens = ai_search_tokens(q)
    if not resolved_brand:
        return tokens

    bt = ai_search_tokens(BRAND_DISPLAY.get(resolved_brand, resolved_brand))
    remaining = []
    used = set()
    for t in tokens:
        match = next((i for i, b in enumerate(bt) if i not in used and _brand_match_score(t, b) >= 0.82), None)
        if match is not None:
            used.add(match)
        else:
            remaining.append(t)
    return remaining


def _token_variants(token: str) -> List[str]:
    """Generate safe multilingual/phonetic variants for a single search token.

    This is deliberately generic rather than a list of perfume-specific aliases.
    It helps Russian phonetic spellings such as «саваж» match French/Latin
    catalogue spellings such as SAUVAGE.
    """
    base = canonical_token(token).lower()
    if not base:
        return []

    variants = {base}

    # Common Russian -> Latin phonetic alternatives.
    replacements = (
        ("shch", "sh"),
        ("zh", "g"),
        ("kh", "h"),
        ("ch", "sh"),
        ("ts", "c"),
    )
    for src, dst in replacements:
        if src in base:
            variants.add(base.replace(src, dst))

    # Reverse variants cover cases where the catalogue uses the French/English
    # spelling while the customer entered a Russian phonetic spelling.
    reverse = (
        ("g", "zh"),
        ("h", "kh"),
        ("sh", "ch"),
        ("c", "ts"),
    )
    for src, dst in reverse:
        if src in base:
            variants.add(base.replace(src, dst))

    return list(variants)


def _fragrance_phonetic_key(token: str) -> str:
    """Generic phonetic key for perfume names.

    Normalizes common Russian/Latin spelling differences without tying the
    search to any one perfume. In particular:
      X <-> KS, C/K/Q, G/H, J/ZH, W/V, Y/I.
    This makes forms such as «наксос» -> NAXOS and
    «гибискус» -> HIBISCUS work across the whole catalog.
    """
    keys = []
    for v in _token_variants(token):
        v = v.lower()

        # Normalize multi-letter sounds before removing vowels.
        v = v.replace("shch", "sh")
        v = v.replace("ch", "sh")
        v = v.replace("zh", "h")
        v = v.replace("ts", "c")
        v = v.replace("ph", "f")

        # Latin spelling vs Russian phonetic spelling.
        v = v.replace("x", "ks")
        v = v.replace("q", "k")
        v = v.replace("c", "k")
        v = v.replace("g", "h")
        v = v.replace("j", "h")
        v = v.replace("w", "v")
        v = v.replace("y", "i")
        v = v.replace("kh", "h")

        # Remove vowels after consonant normalization.
        v = re.sub(r"[aeiouy]+", "", v)
        v = re.sub(r"(.)\\1+", r"\\1", v)
        if v:
            keys.append(v)
    return max(keys, key=len, default="")


def _fragrance_token_score(query_token: str, product_token: str) -> float:
    """Typo/phonetic score for fragrance names."""
    base = _name_token_score(query_token, product_token)
    qk = _fragrance_phonetic_key(query_token)
    pk = _fragrance_phonetic_key(product_token)
    if qk and pk:
        if qk == pk:
            return max(base, 0.98)
        return max(base, SequenceMatcher(None, qk, pk).ratio() * 0.96)
    return base


def _name_token_score(query_token: str, product_token: str) -> float:
    """Universal typo/transliteration score for fragrance names."""
    av = _token_variants(query_token)
    bv = _token_variants(product_token)
    if not av or not bv:
        return 0.0

    best = 0.0
    for a in av:
        for b in bv:
            if a == b:
                best = max(best, 1.0)
                continue
            if min(len(a), len(b)) >= 4:
                common = 0
                for x, y in zip(a, b):
                    if x != y:
                        break
                    common += 1
                if common >= 4:
                    best = max(best, max(0.90, SequenceMatcher(None, a, b).ratio()))
            if a in b or b in a:
                best = max(best, max(0.86, SequenceMatcher(None, a, b).ratio()))
            best = max(best, SequenceMatcher(None, a, b).ratio())
    return best


# ---------- Bounded indexed fragrance search ----------
# Search must never compare a query against every fragrance with SequenceMatcher.
# Build small inverted indexes lazily from the already client-visible fragrance groups.
_FAST_TOKEN_INDEX: Optional[Dict[str, set]] = None
_FAST_PHONETIC_INDEX: Optional[Dict[str, set]] = None
_FAST_DELETE_INDEX: Optional[Dict[str, set]] = None
_FAST_PREFIX_INDEX: Optional[Dict[str, set]] = None
_FAST_GROUP_BY_KEY: Optional[Dict[str, dict]] = None


def _build_fast_search_indexes():
    global _FAST_TOKEN_INDEX, _FAST_PHONETIC_INDEX, _FAST_DELETE_INDEX, _FAST_PREFIX_INDEX, _FAST_GROUP_BY_KEY
    if _FAST_TOKEN_INDEX is not None:
        return

    token_index: Dict[str, set] = {}
    phonetic_index: Dict[str, set] = {}
    delete_index: Dict[str, set] = {}
    prefix_index: Dict[str, set] = {}
    group_by_key: Dict[str, dict] = {}

    for p in FAST_SEARCH_GROUPS_ALL:
        gk = group_key(p)
        if gk in group_by_key:
            continue
        group_by_key[gk] = p

        base = _fast_base_name(p).translate(RU_TO_EN)
        for token in re.findall(r"[a-z0-9]+", base):
            if len(token) < 2:
                continue
            token_index.setdefault(token, set()).add(gk)
            if len(token) >= 4:
                prefix_index.setdefault(token[:4], set()).add(gk)

            pk = _fragrance_phonetic_key(token)
            if pk:
                phonetic_index.setdefault(pk, set()).add(gk)

            # One-edit typo candidates. This index is deliberately bounded:
            # only deletion keys are stored, never pairwise comparisons.
            if len(token) >= 4:
                for i in range(len(token)):
                    delete_index.setdefault(token[:i] + token[i + 1:], set()).add(gk)

    _FAST_TOKEN_INDEX = token_index
    _FAST_PHONETIC_INDEX = phonetic_index
    _FAST_DELETE_INDEX = delete_index
    _FAST_PREFIX_INDEX = prefix_index
    _FAST_GROUP_BY_KEY = group_by_key


def _fast_candidate_group_keys(name_tokens: List[str], resolved_brand: Optional[str]) -> set:
    """Retrieve a bounded candidate set using exact, phonetic and one-edit indexes."""
    _build_fast_search_indexes()
    assert _FAST_TOKEN_INDEX is not None
    assert _FAST_PHONETIC_INDEX is not None
    assert _FAST_DELETE_INDEX is not None
    assert _FAST_PREFIX_INDEX is not None
    assert _FAST_GROUP_BY_KEY is not None

    if not name_tokens:
        if resolved_brand:
            return {
                gk for gk, p in _FAST_GROUP_BY_KEY.items()
                if BRAND_FOR_ID.get(p.get("id"), "") == resolved_brand
            }
        return set(_FAST_GROUP_BY_KEY)

    per_token = []
    for token in name_tokens:
        token = canonical_token(token).lower()

        # Exact token hits are terminal at the candidate-retrieval level.
        # Do not let a broad phonetic bucket (e.g. ALTHAIR -> many LEATHER
        # products) compete with a real exact product token.
        exact = set(_FAST_TOKEN_INDEX.get(token, set()))
        if exact:
            if resolved_brand:
                brand_exact = {
                    gk for gk in exact
                    if BRAND_FOR_ID.get(_FAST_GROUP_BY_KEY[gk].get("id"), "") == resolved_brand
                }
                if brand_exact:
                    candidates = brand_exact
                else:
                    # An exact token exists in another house, but not inside the
                    # explicitly requested brand. Fall through to typo/phonetic
                    # retrieval within the brand instead of returning nothing.
                    candidates = set()
            else:
                candidates = exact
        else:
            candidates = set()

        if not candidates:
            pk = _fragrance_phonetic_key(token)
            if pk:
                phonetic_hits = _FAST_PHONETIC_INDEX.get(pk, set())
                # Phonetic consonant skeletons can collide for short/common
                # words (e.g. LEATHER). Use them only when the bucket is small;
                # exact and one-edit indexes remain the primary path.
                if len(phonetic_hits) <= 12:
                    candidates.update(phonetic_hits)

            # One-character insertion/deletion/substitution typo.
            # Check both directions: the product may be one character longer
            # than the query (ALTAIR -> ALTHAIR), or the query may contain the
            # extra/mistyped character.
            if len(token) >= 4:
                candidates.update(_FAST_DELETE_INDEX.get(token, set()))
                for i in range(len(token)):
                    candidates.update(_FAST_DELETE_INDEX.get(token[:i] + token[i + 1:], set()))

            # Bounded prefix fallback handles common abbreviated/misspelled
            # forms such as "chans" -> CHANCE without scanning the catalog.
            if not candidates and len(token) >= 4:
                prefix_hits = _FAST_PREFIX_INDEX.get(token[:4], set())
                if len(prefix_hits) <= 200:
                    candidates.update(prefix_hits)

        # If no indexed hit exists, do not fall back to a full-catalog fuzzy scan.
        per_token.append(candidates)

    if not per_token:
        return set()

    # Prefer intersection: every requested name word should identify the same fragrance.
    intersection = set.intersection(*per_token)
    candidates = intersection if intersection else set.union(*per_token)

    if resolved_brand:
        candidates = {
            gk for gk in candidates
            if BRAND_FOR_ID.get(_FAST_GROUP_BY_KEY[gk].get("id"), "") == resolved_brand
        }
        # A broad phonetic bucket may contain no item from the requested brand.
        # Retry the bounded prefix bucket before giving up.
        if not candidates and len(name_tokens) == 1 and len(name_tokens[0]) >= 4:
            prefix_hits = _FAST_PREFIX_INDEX.get(name_tokens[0][:4], set())
            if len(prefix_hits) <= 200:
                candidates = {
                    gk for gk in prefix_hits
                    if BRAND_FOR_ID.get(_FAST_GROUP_BY_KEY[gk].get("id"), "") == resolved_brand
                }
    return candidates


def _fast_product_name_tokens(p: dict, resolved_brand: Optional[str]) -> List[str]:
    base = _fast_base_name(p).translate(RU_TO_EN)
    tokens = re.findall(r"[a-z0-9]+", base)

    # Even without a user-supplied brand, product-side brand words must not be
    # treated as fragrance-name words. Use the catalog's detected brand for the
    # current product. This makes a bare "DELINA" exact rather than requiring the
    # user to type the house name as well.
    product_brand = resolved_brand or BRAND_FOR_ID.get(p.get("id"), "")
    if not product_brand:
        return tokens

    brand_text = normalize_ai_query(BRAND_DISPLAY.get(product_brand, product_brand)).translate(RU_TO_EN)
    brand_tokens = re.findall(r"[a-z0-9]+", brand_text)
    kept = []
    used = set()
    for pt in tokens:
        match = next(
            (i for i, bt in enumerate(brand_tokens)
             if i not in used and _brand_match_score(pt, bt) >= 0.82),
            None,
        )
        if match is not None:
            used.add(match)
        else:
            kept.append(pt)
    return kept


def fast_ai_name_search(query: str, limit: int = 12) -> List[dict]:
    """Universal bounded search for the client-facing perfume catalog.

    Priority:
      1) exact fragrance name after removing brand/service words;
      2) exact phonetic/transliteration match;
      3) indexed one-edit typo candidates;
      4) score only that small candidate set.

    There is intentionally no full-catalog SequenceMatcher fallback.
    """
    if not is_name_like_query(query):
        return []

    resolved_brand = fuzzy_brand_key(query)
    name_tokens = _query_name_tokens(query, resolved_brand)
    brand_only = resolved_brand is not None and not name_tokens

    _build_fast_search_indexes()
    assert _FAST_GROUP_BY_KEY is not None

    candidate_keys = _fast_candidate_group_keys(name_tokens, resolved_brand)
    if not candidate_keys:
        return []

    candidates = [
        _FAST_GROUP_BY_KEY[gk] for gk in candidate_keys
        if gk in _FAST_GROUP_BY_KEY
    ]

    # Exact/phonetic terminal matching. A plain "DELINA" must not expand to
    # DELINA EXCLUSIF or DELINA LA ROSEE; "ALTHAIR" must not expand to ALTHAIR
    # EXCLUSIF. This is checked before fuzzy scoring.
    if name_tokens and not brand_only:
        exact_rows = []
        for p in candidates:
            ptokens = _fast_product_name_tokens(p, resolved_brand)
            if len(ptokens) != len(name_tokens):
                continue

            matched = set()
            ok = True
            for qt in name_tokens:
                qk = _fragrance_phonetic_key(qt)
                found = None
                for idx, pt in enumerate(ptokens):
                    if idx in matched:
                        continue
                    # Token equality is the strongest terminal condition.
                    if qt == pt:
                        found = idx
                        break
                    # Equal phonetic key covers Russian/Latin transliteration,
                    # but does not equate ALTHAIR with ALTAIR.
                    if qk and qk == _fragrance_phonetic_key(pt):
                        found = idx
                        break
                if found is None:
                    ok = False
                    break
                matched.add(found)

            if ok:
                exact_rows.append(p)

        if exact_rows:
            exact_rows.sort(key=lambda p: ai_fragrance_title(p).lower())
            return exact_rows[:max(1, min(int(limit or 12), 12))]

    scored = []
    for p in candidates:
        if resolved_brand and BRAND_FOR_ID.get(p.get("id"), "") != resolved_brand:
            continue

        ptokens = _fast_product_name_tokens(p, resolved_brand)
        if not ptokens and name_tokens:
            continue

        if brand_only:
            score = 20.0
        else:
            sims = [
                max((_fragrance_token_score(qt, pt) for pt in ptokens), default=0.0)
                for qt in name_tokens
            ]
            threshold = 0.72 if resolved_brand else 0.78
            if not sims or any(v < threshold for v in sims):
                continue

            avg = sum(sims) / len(sims)
            exact_count = sum(
                1 for qt in name_tokens
                if any(qt == pt for pt in ptokens)
            )
            # Prefer the shortest fragrance-name match when the requested words
            # are equally close. This prevents a typo for ALTHAIR from expanding
            # into ALTHAIR EXCLUSIF, and applies to flankers catalog-wide.
            extra_tokens = max(0, len(ptokens) - len(name_tokens))
            score = avg * 100 + exact_count * 45 - extra_tokens * 28

            if all(v >= 0.90 for v in sims):
                score += 35

        scored.append((score, p))

    if not scored:
        return []

    scored.sort(key=lambda row: (-row[0], ai_fragrance_title(row[1]).lower()))

    if brand_only:
        return [p for _, p in scored[:max(1, min(int(limit or 12), 12))]]

    best = scored[0][0]
    # Typo matches should stay close to the best candidate and must not fill the
    # list with unrelated products from the same brand.
    threshold = best - (16 if resolved_brand else 12)
    return [p for score, p in scored if score >= threshold][:max(1, min(int(limit or 12), 12))]

def fast_ai_response(query: str, candidates: List[dict]) -> str:
    lines = ["💬 <b>PARFERA AI</b>", "", f"Нашёл варианты по запросу «{html.escape(query)}»:", ""]
    for i, p in enumerate(candidates, 1):
        title = html.escape(fragrance_title(p))
        prices = []
        if p.get("bottle_price_rub"):
            prices.append(f"{p.get('volume','')} — {rub(p['bottle_price_rub'])}")
        if p.get("tester_price_rub"):
            prices.append(f"тестер — {rub(p['tester_price_rub'])}")
        lines.append(f"<b>{i}. {title}</b>")
        if prices:
            lines.append(" · ".join(prices))
        lines.append("")
    lines.append("Выберите нужную концентрацию:")
    return "\n".join(lines).strip()


async def ai_assist(uid: int, user_text: str) -> Tuple[str, List[dict]]:
    """PARFERA AI: search real catalog, then rank verified IDs and build text/buttons from the same IDs."""
    if OPENAI_CLIENT is None:
        return ("🤖 <b>Умный помощник пока не подключён.</b>\n\nНо поиск по каталогу уже работает. Нажмите «🔎 Поиск» или напишите название бренда/аромата.", [])

    history = AI_HISTORY.setdefault(uid, [])
    history.append({"role": "user", "content": user_text})
    history[:] = history[-AI_MAX_HISTORY:]

    tool = {
        "type": "function",
        "name": "search_catalog",
        "description": "Проверить конкретный бренд, аромат или кандидата по реальному каталогу PARFERA.",
        "strict": True,
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Конкретное название аромата или бренда."},
                "brand": {"type": "string", "description": "Бренд, если нужен отдельный фильтр. Иначе пустая строка."},
                "gender": {"type": "string", "enum": ["", "m", "w", "u"]},
                "max_price": {"anyOf": [{"type": "integer"}, {"type": "null"}]},
                "volume": {"anyOf": [{"type": "integer"}, {"type": "null"}]},
                "limit": {"type": "integer", "minimum": 1, "maximum": 12}
            },
            "required": ["query", "brand", "gender", "max_price", "volume", "limit"],
            "additionalProperties": False
        }
    }

    input_items = [{"role": "system", "content": AI_SYSTEM_PROMPT}] + history
    collected: List[dict] = []
    seen_groups = set()

    # Search phase: at most 3 tool rounds. All returned products are real catalog items.
    for _ in range(3):
        response = await OPENAI_CLIENT.responses.create(
            model=OPENAI_MODEL,
            input=input_items,
            tools=[tool],
            parallel_tool_calls=False,
        )
        calls = [output_item for output_item in response.output if getattr(output_item, "type", "") == "function_call"]
        if not calls:
            break

        input_items += response.output
        for call in calls:
            try:
                args = json.loads(call.arguments or "{}")
            except Exception:
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
                gk = group_key(p)
                if gk not in seen_groups:
                    seen_groups.add(gk)
                    collected.append(p)

            payload = json.dumps(ai_tool_result(candidates), ensure_ascii=False)
            input_items.append({
                "type": "function_call_output",
                "call_id": call.call_id,
                "output": payload
            })

            if len(collected) >= 12:
                break

        if len(collected) >= 12:
            break

    if not collected:
        history.append({"role": "assistant", "content": "Не удалось найти подходящие позиции в текущем каталоге."})
        history[:] = history[-AI_MAX_HISTORY:]
        AI_LAST_RESULTS[uid] = []
        return "Не удалось найти подходящие позиции в текущем каталоге. Попробуйте указать бренд, название или бюджет.", []

    # Rank phase: AI returns ONLY catalog IDs + short reasons.
    verified = ai_tool_result(collected[:12])
    rank_prompt = (
        "Ты — PARFERA AI, премиальный консультант по парфюмерии.\n"
        f"Запрос клиента: {user_text}\n\n"
        "Ниже список ТОЛЬКО реально найденных позиций из каталога. "
        "Выбери 3–5 лучших совпадений. Не придумывай новые товары.\n"
        + json.dumps(verified, ensure_ascii=False)
        + "\n\n"
        "Верни ТОЛЬКО валидный JSON-массив объектов без Markdown и без пояснений.\n"
        'Формат: [{"id":"ТОЧНЫЙ_ID_ИЗ_СПИСКА","reason":"короткая причина на русском"}, ...]\n'
        "ID должны быть только из переданного списка. reason — максимум 1 короткое предложение, "
        "без выдумывания конкретных нот, если их нет в данных."
    )

    selected = []
    try:
        rank_response = await OPENAI_CLIENT.responses.create(
            model=OPENAI_MODEL,
            input=[
                {"role": "system", "content": AI_SYSTEM_PROMPT},
                {"role": "user", "content": rank_prompt},
            ],
            tools=[],
        )
        raw = (rank_response.output_text or "").strip()
        # Extract JSON array even if the model accidentally wrapped it in whitespace/code fences.
        m = re.search(r"\[\s*\{.*\}\s*\]", raw, flags=re.S)
        if m:
            data = json.loads(m.group(0))
            if not isinstance(data, list):
                data = []

            # Build a strict allow-list from verified catalog products. Never trust
            # the model's JSON shape and never index an unvalidated key.
            allowed = {}
            for product in collected[:12]:
                if not isinstance(product, dict):
                    continue
                pid = str(product.get("id") or "").strip()
                if pid:
                    allowed[pid] = product

            selected_ids = set()
            for item in data:
                if not isinstance(item, dict):
                    continue
                pid = str(item.get("id") or "").strip()
                if not pid or pid not in allowed or pid in selected_ids:
                    continue
                selected_ids.add(pid)
                reason = str(item.get("reason") or "").strip()
                selected.append({"product": allowed[pid], "reason": reason[:240]})
                if len(selected) >= 5:
                    break
    except Exception as e:
        print(f"PARFERA AI ranking error: {type(e).__name__}: {e!r}")
        traceback.print_exc()

    # Guaranteed fallback: ranking is optional; verified catalog candidates are not.
    if not selected:
        for product in collected[:5]:
            if isinstance(product, dict) and product.get("id"):
                selected.append({"product": product, "reason": ""})

    # Final safety check: only real product dictionaries with a valid ID reach the UI.
    selected = [
        item for item in selected
        if isinstance(item, dict)
        and isinstance(item.get("product"), dict)
        and item["product"].get("id")
    ][:5]

    products = [item["product"] for item in selected]
    AI_LAST_RESULTS[uid] = [str(product.get("id")) for product in products if product.get("id")]

    # Build the visible answer from the EXACT same product objects used for buttons.
    # This makes text/button mismatch impossible.
    lines = ["💬 <b>PARFERA AI рекомендует</b>", ""]
    for i, item in enumerate(selected[:5], 1):
        p = item["product"]
        title = html.escape(ai_fragrance_title(p))
        raw_name = str(p.get("name", ""))
        concentration = ""
        cm = re.search(r"\b(EDP|EDT|PARFUM|EXTRAIT)\b", raw_name, re.I)
        if cm:
            concentration = cm.group(1).upper()

        volume_text = str(p.get("volume") or "").strip()
        price_text = rub(p["bottle_price_rub"]) if p.get("bottle_price_rub") else (
            rub(p["tester_price_rub"]) if p.get("tester_price_rub") else "уточняется"
        )

        lines.append(f"<b>{i}. {title}</b>")
        meta = []
        if concentration:
            meta.append(concentration)
        if volume_text:
            meta.append(f"{volume_text} мл")
        meta.append(price_text)
        lines.append(" · ".join(meta))

        reason = item["reason"].strip()
        if reason:
            lines.append(f"<i>{html.escape(reason)}</i>")
        lines.append("")

    lines.append("⭐ <b>Хотите посмотреть варианты подробнее?</b>")
    final_text = "\n".join(lines).strip()

    history.append({"role": "assistant", "content": final_text})
    history[:] = history[-AI_MAX_HISTORY:]
    return final_text, products


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
    lines = [f"<b>{title}</b>", "✨ Оригинальный аромат"]
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
    lines.append("<b>ВЫБЕРИТЕ ОБЪЁМ</b>")
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
    """Universal catalog search used by the regular Search button.
    Supports exact names, articles, Cyrillic/Latin spelling and common typos.
    Never invents products: every result comes directly from PRODUCTS.
    """
    original = str(query or "").strip()
    q = normalize_ai_query(original)

    # 1) Article/SKU is the strongest exact signal. Only scan supplier rows when
    # the query actually looks like an article (digits are present); perfume-name
    # queries should go straight to the indexed name resolver.
    q_norm = norm(original)
    article_hits = []
    if q_norm and re.search(r"\d", q_norm):
        article_hits = [
            p for p in PRODUCTS
            if variant_is_client_friendly(p)
            and q_norm in norm(str(p.get("article", "")))
        ]

    # 2) Concrete perfume-name queries use the same verified local resolver as
    # PARFERA AI. This prevents the regular Search button from broad fuzzy matches.
    resolved = []
    if not article_hits:
        try:
            if is_name_like_query(query):
                resolved = fast_ai_name_search(query, limit=100)
            if not resolved:
                resolved = ai_candidate_search(query=query, limit=100)
        except Exception as e:
            print(f"PARFERA regular search resolver error: {type(e).__name__}: {e!r}", flush=True)

    raw = article_hits or resolved

    # 2b) Last local pass: compare the normalized/transliterated query directly
    # with each catalog name. This catches short names and spelling variants even
    # if the scoring resolver is too conservative.
    if not raw and q and not is_name_like_query(query):
        q_latin = q.translate(RU_TO_EN)
        q_tokens = re.findall(r"[a-z0-9]+", q_latin)
        direct_scored = []
        if q_tokens:
            for p in PRODUCTS:
                if not variant_is_client_friendly(p):
                    continue
                name_latin = norm(p.get("name", "")).translate(RU_TO_EN)
                name_tokens = re.findall(r"[a-z0-9]+", name_latin)
                if not name_tokens:
                    continue
                sims = [max(token_similarity(t, nt) for nt in name_tokens) for t in q_tokens]
                if all(v >= 0.68 for v in sims):
                    direct_scored.append((sum(sims), p))
        direct_scored.sort(key=lambda x: -x[0])
        raw = [p for _, p in direct_scored[:100]]

    # 3) If the user entered only a brand (including a typo/transliteration),
    # show the brand's fragrance groups instead of returning nothing.
    if not raw:
        bkey = fuzzy_brand_key(q)
        if bkey and bkey in BRAND_GROUPS:
            raw = [variants[0] for variants in BRAND_GROUPS[bkey] if variants]

    matches = []
    seen = set()
    for item in raw:
        gk = group_key(item)
        if gk in seen:
            continue
        seen.add(gk)
        matches.append(item)

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
        return "🛒 <b>Корзина пуста</b>\n\nВыберите аромат и добавьте его сюда."
    total = sum(x["price"] * x["qty"] for x in items)
    lines = [f"🛒 <b>Корзина</b>\n{len(items)} поз.\n"]
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
    await edit_or_replace(target_message, f'🔎 <b>Найдено: {len(matches)}</b>\n«{query}»\n\nВыберите аромат:', results_kb(items, page, len(matches)))


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


HOME_TEXT = "<b>PARFERA</b>\n\n✨ Оригинальная нишевая парфюмерия.\n\n<b>Что ищем?</b>\nНапишите бренд или аромат — например: «Versace Eros 100 мл» или «Erba Pura»."
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
    text = f"🛍 <b>Каталог PARFERA</b>\n\n<b>{visible_groups_total}</b> ароматов · <b>{len(BRAND_KEYS)}</b> брендов\n\nВыберите категорию:"
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
        await edit_or_replace(callback.message, f"<b>{title}</b>\n\n<b>{len(keys)}</b> брендов · <b>{total}</b> ароматов\n\nВыберите бренд:", gender_brands_kb(gender, 0))
        await callback.answer()


@dp.callback_query(F.data.startswith("gbrands:"))
async def gender_brands_page(callback: CallbackQuery):
    _, gender, page = callback.data.split(":")
    title = "👩 Для неё" if gender == "w" else "👨 Для него" if gender == "m" else "⚪ Унисекс"
    keys = gender_brand_keys(gender)
    await edit_or_replace(callback.message, f"<b>{title}</b>\n\n<b>{len(keys)}</b> брендов\n\nВыберите бренд:", gender_brands_kb(gender, int(page)))
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
    text = f"🛍 <b>Бренды PARFERA</b>\n\n<b>{len(BRAND_KEYS)}</b> брендов\n\nВыберите бренд:"
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
        "💬 <b>PARFERA AI</b>\n\nОпишите, какой аромат хотите — AI подберёт варианты из каталога.\n\nНапример:\n• «женский сладкий до 7000 ₽»\n• «мужской свежий»\n• «похожий на Erba Pura»\n\n⚡ Нужен конкретный аромат? Используйте обычный поиск — он быстрее.",
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
        await edit_or_replace(callback.message, "💬 <b>Результаты</b>\n\nВыберите аромат:", InlineKeyboardMarkup(inline_keyboard=rows))
    else:
        await edit_or_replace(callback.message, "💬 <b>PARFERA AI</b>\n\nНапишите следующий запрос.", back_home_kb())
    await callback.answer()




@dp.callback_query(F.data.in_({"search", "catalog_search"}))
async def search(callback: CallbackQuery, state: FSMContext):
    await state.set_state(SearchState.waiting)
    await edit_or_replace(callback.message, "🔎 <b>Поиск по каталогу</b>\n\nВведите бренд, аромат или артикул.\n\n⚡ Быстрый поиск сразу проверит каталог.\n\nНапример: <b>Versace Eros</b>, <b>Erba Pura</b> или <b>000-002</b>.", back_home_kb())
    await callback.answer()


@dp.message(SearchState.waiting)
async def do_search(message: Message, state: FSMContext):
    query = message.text.strip() if message.text else ""
    if not query:
        await message.answer("🔎 Напишите бренд, название аромата или артикул.")
        return
    USER_SEARCH[message.from_user.id] = query
    print(f"PARFERA SEARCH QUERY: {query!r}", flush=True)
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
    print(f"PARFERA AI MESSAGE: {text!r}", flush=True)

    # HARD BRAND-ONLY PATH: if the entire message is a known brand alias,
    # never run fuzzy perfume-name matching and never let AI broaden it.
    # Example: «Амуж» / «Амуаж» -> AMOUAGE catalog only.
    brand_only_key = fuzzy_brand_key(text)
    brand_only_alias = norm(text) in BRAND_QUERY_ALIASES
    if brand_only_key and (brand_only_alias or not _query_name_tokens(text, brand_only_key)):
        if brand_only_alias:
            print(f"BRAND ALIAS RESOLVED: {text!r} -> {brand_only_key!r}", flush=True)
        brand_products = [
            p for p in PRODUCTS
            if variant_is_client_friendly(p) and BRAND_FOR_ID.get(p.get("id")) == brand_only_key
        ]
        # Keep one representative per fragrance group, preserving the existing
        # catalog ordering.
        seen_groups = set()
        unique_brand_products = []
        for prod in brand_products:
            gk = group_key(prod)
            if gk in seen_groups:
                continue
            seen_groups.add(gk)
            unique_brand_products.append(prod)
        unique_brand_products = unique_brand_products[:12]
        if unique_brand_products:
            AI_LAST_RESULTS[message.from_user.id] = [p["id"] for p in unique_brand_products]
            rows = [[InlineKeyboardButton(text=f"🧴 {ai_fragrance_title(p)[:58]}", callback_data=f"product:{p['id']}:ai:0")] for p in unique_brand_products]
            rows.append([InlineKeyboardButton(text="💬 Новый запрос", callback_data="ai_start")])
            rows.append([InlineKeyboardButton(text="🛍 Каталог", callback_data="catalog")])
            title = BRAND_DISPLAY.get(brand_only_key, brand_only_key)
            await message.answer(
                f"💬 <b>PARFERA AI</b>\n\n<b>{html.escape(title)}</b>\n\nВыберите аромат:",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=rows)
            )
            return

    # FAST PATH: product-name queries are resolved entirely locally.
    # This avoids the 30–45 sec OpenAI round-trip for obvious names/typos.
    fast_candidates = fast_ai_name_search(text, limit=12)

    if fast_candidates:
        AI_LAST_RESULTS[message.from_user.id] = [p["id"] for p in fast_candidates]
        rows = [[InlineKeyboardButton(text=f"🧴 {ai_fragrance_title(p)[:58]}", callback_data=f"product:{p['id']}:ai:0")] for p in fast_candidates]
        rows.append([InlineKeyboardButton(text="💬 Новый запрос", callback_data="ai_start")])
        rows.append([InlineKeyboardButton(text="🛍 Каталог", callback_data="catalog")])
        await message.answer(fast_ai_response(text, fast_candidates), reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
        return

    # Never let the conversational AI broaden a concrete perfume-name query
    # into unrelated products of the same brand. Name queries are catalogue
    # lookups; recommendation queries are the only ones that reach OpenAI.
    if is_name_like_query(text):
        brand_key = fuzzy_brand_key(text)
        name_tokens = _query_name_tokens(text, brand_key)
        if brand_key and name_tokens:
            print(
                f"LOCAL SEARCH MISS / AI BLOCKED: query={text!r}, "
                f"brand={brand_key!r}, name_tokens={name_tokens!r}",
                flush=True,
            )
            await message.answer(
                f"🔎 По запросу «{html.escape(text)}» точной позиции не найдено.\\n\\n"
                "Попробуйте написать название иначе.",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="🛍 Открыть каталог", callback_data="catalog")],
                    [InlineKeyboardButton(text="💬 Новый запрос", callback_data="ai_start")],
                ]),
            )
            return

    # Telegram's "typing…" indicator expires after a few seconds, so keep
    # refreshing it while the AI is thinking/searching. This runs only for
    # the slower natural-language consultant flow; the local fast name search
    # above returns immediately and does not need the indicator.
    async def keep_typing():
        try:
            while True:
                await message.bot.send_chat_action(chat_id=message.chat.id, action=ChatAction.TYPING)
                await asyncio.sleep(3)
        except asyncio.CancelledError:
            return
        except Exception as e:
            print(f"PARFERA typing indicator error: {type(e).__name__}: {e!r}")

    # Send the first typing action immediately, before starting the AI request.
    # Telegram displays this only temporarily in the chat header, so refresh it
    # every few seconds until the AI response is ready.
    try:
        await message.bot.send_chat_action(chat_id=message.chat.id, action=ChatAction.TYPING)
    except Exception as e:
        print(f"PARFERA typing initial error: {type(e).__name__}: {e!r}")
    typing_task = asyncio.create_task(keep_typing())
    try:
        answer, candidates = await ai_assist(message.from_user.id, text)
    except Exception as e:
        print(f"PARFERA AI error: {type(e).__name__}: {e!r}")
        traceback.print_exc()
        await message.answer("🤖 Не удалось выполнить подбор. Попробуйте ещё раз или откройте каталог.", reply_markup=home_kb())
        return
    finally:
        typing_task.cancel()
        try:
            await typing_task
        except asyncio.CancelledError:
            pass
    rows = []
    for p in candidates[:5]:
        title = ai_fragrance_title(p)
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
            "📦 <b>Оформление заказа</b>\n\nПриём заказов ещё не настроен. Укажите <b>PARFERA_ADMIN_CHAT_ID</b> в Render.",
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
        f"✅ <b>Заказ принят!</b>\n\n№<b>{order_no}</b>\n\nЗаказ отправлен консультанту <b>@Parfera</b>. Он свяжется с вами в Telegram.",
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
    await edit_or_replace(callback.message, "⭐ <b>Популярное</b>\n\nРаздел скоро будет доступен.", back_home_kb())
    await callback.answer()


@dp.callback_query(F.data == "new")
async def new(callback: CallbackQuery):
    await edit_or_replace(callback.message, "🆕 <b>Новинки</b>\n\nРаздел скоро будет доступен.", back_home_kb())
    await callback.answer()


@dp.callback_query(F.data == "consultant")
async def consultant(callback: CallbackQuery):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💬 Написать консультанту", url="https://t.me/Parfera")],
        [InlineKeyboardButton(text="🔎 Подобрать аромат", callback_data="search")],
        [InlineKeyboardButton(text="← Главное меню", callback_data="home")],
    ])
    await edit_or_replace(callback.message, "👤 <b>Консультант PARFERA</b>\n\nНе знаете, что выбрать? Напишите <b>@Parfera</b> — поможем подобрать аромат под ваш вкус и бюджет.", kb)
    await callback.answer()


async def health(request: web.Request):
    return web.Response(text="OK")


async def telegram_webhook(request: web.Request):
    """Receive Telegram updates via webhook and pass them to aiogram."""
    try:
        data = await request.json()
        update_id = data.get("update_id", "?") if isinstance(data, dict) else "?"
        print(f"WEBHOOK UPDATE RECEIVED: update_id={update_id}", flush=True)
        update = Update.model_validate(data, context={"bot": request.app["bot"]})
        await dp.feed_update(request.app["bot"], update)
        print(f"WEBHOOK UPDATE PROCESSED: update_id={update_id}", flush=True)
        return web.Response(text="OK")
    except Exception as e:
        print(f"PARFERA webhook error: {type(e).__name__}: {e!r}", flush=True)
        traceback.print_exc()
        return web.Response(status=500, text="ERROR")


async def run_web_server(bot: Bot):
    app = web.Application()
    app["bot"] = bot
    app.router.add_get("/", health)
    app.router.add_get("/health", health)
    app.router.add_post("/webhook", telegram_webhook)

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
        # Use Telegram long polling for the Render service. This is more reliable
        # here than a custom webhook and avoids Telegram -> Render webhook routing issues.
        # Keep the HTTP server alive so Render health checks continue to work.
        runner = await run_web_server(bot)

        print("TELEGRAM MODE: LONG POLLING", flush=True)
        try:
            await bot.delete_webhook(drop_pending_updates=False)
            print("WEBHOOK CLEARED OK", flush=True)
        except Exception as e:
            print(f"WEBHOOK CLEAR ERROR: {type(e).__name__}: {e!r}", flush=True)
            traceback.print_exc()
            raise

        try:
            info = await bot.get_webhook_info()
            print(
                "WEBHOOK AFTER CLEAR: "
                f"url={info.url!r}, pending_update_count={info.pending_update_count}",
                flush=True,
            )
        except Exception as e:
            print(f"WEBHOOK INFO ERROR: {type(e).__name__}: {e!r}", flush=True)

        print(f"Catalog loaded: {len(PRODUCTS)} products, {len(BRAND_KEYS)} brands, {len(GROUPS)} fragrance groups", flush=True)
        print("STARTING TELEGRAM POLLING...", flush=True)

        await dp.start_polling(
            bot,
            drop_pending_updates=False,
            allowed_updates=dp.resolve_used_update_types(),
        )
    finally:
        if runner is not None:
            await runner.cleanup()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
