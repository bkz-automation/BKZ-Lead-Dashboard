"""Collect public Moroccan business leads and append new ones to Google Sheets.

The free default source is OpenStreetMap through public Overpass endpoints.
Google Places and Moroccan public directories remain explicitly selectable,
and official websites are inspected for public contact details.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import html
import json
import logging
import os
import random
import re
import sys
import time
import unicodedata
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path
from typing import Iterable
from urllib.parse import urljoin, urlparse

import gspread
import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from google.oauth2.service_account import Credentials
from requests.adapters import HTTPAdapter


SHEET_COLUMNS = [
    "lead_id", "company_name", "industry", "location", "email", "phone",
    "website", "business_description", "automation_opportunity", "score",
    "contact_status", "personalised_message", "sent_at",
]
OPTIONAL_LEAD_COLUMNS = [
    "ai_buying_score", "recommended_service", "website_status", "facebook_url",
    "instagram_url", "linkedin_url", "contact_quality",
]
AI_BUYING_SCORE_COLUMNS = [
    "ai_buying_score", "priority", "recommended_offer", "score_breakdown",
]
CONTACT_COLUMNS = ["whatsapp", "contact_form_url"]
LEAD_EXPORT_COLUMNS = SHEET_COLUMNS + AI_BUYING_SCORE_COLUMNS + CONTACT_COLUMNS
SCHEDULER_WORKSHEET = "_collector_state"

# AI Buying Score V1 is deliberately data-driven so commercial tuning does not
# require changing the scoring flow below.
AI_BUYING_SCORE_WEIGHTS = {
    "sector_fit": {"max": 25, "high": 25, "medium": 18, "general": 10},
    "digital_maturity": {"max": 15, "website": 8, "domain_email": 4, "social": 3},
    "company_size_proxy": {"max": 12, "scale_signal": 12, "complete_contact": 7, "basic_footprint": 4},
    "operational_pain": {"max": 12, "workflow_signal": 12, "high_fit_sector": 8, "general": 4},
    "buying_intent": {"max": 10, "workflow_online": 10, "digital_contact": 7, "limited": 3},
    "reachability": {"max": 8, "whatsapp": 8, "mobile": 6, "email": 4, "landline": 1},
    "business_email": {"max": 6, "present": 6},
    "decision_maker": {"max": 6, "signal": 6},
    "growth_signals": {"max": 4, "signal": 4, "digital_footprint": 2},
}
AI_BUYING_SCORE_PENALTIES = {
    "no_website": -20,
    "landline_only": -15,
    "inactive_company": -25,
}
AI_BUYING_SCORE_LIMITS = {"minimum": 0, "maximum": 100}
AI_BUYING_SCORE_PRIORITY_THRESHOLDS = ((85, "A+"), (70, "A"), (55, "B"), (40, "C"))
AI_BUYING_SCORE_BASE_MAX = sum(component["max"] for component in AI_BUYING_SCORE_WEIGHTS.values())

AI_BUYING_SCORE_SOCIAL_FIELDS = ("facebook_url", "instagram_url", "linkedin_url")
AI_BUYING_SCORE_HIGH_FIT_TERMS = (
    "restaurant", "cafe", "hotel", "riad", "hote", "traiteur", "clinique",
    "medical", "dentaire", "pharmac", "immobili", "ecole", "formation",
    "creche", "salon", "beaute", "spa", "fitness", "sport", "agence",
    "voyage", "centre appels",
)
AI_BUYING_SCORE_MEDIUM_FIT_TERMS = (
    "avocat", "comptable", "architect", "construction", "transport", "securite",
    "nettoyage", "garage", "automobile", "magasin", "boutique", "supermarche",
    "bijouter", "opticien", "imprimer", "grossiste", "distributeur",
)
AI_BUYING_SCORE_OPERATIONAL_TERMS = (
    "reservation", "rendez vous", "commande", "devis", "inscription", "livraison",
    "consultation", "booking", "appointment", "order",
)
AI_BUYING_SCORE_SCALE_TERMS = (
    "groupe", "group", "franchise", "succursale", "plusieurs", "multi",
    "agences", "locations", "branches", "equipe", "team", "staff",
)
AI_BUYING_SCORE_GROWTH_TERMS = (
    "nouveau", "new", "ouverture", "opening", "recrut", "expansion", "developp",
    "promotion", "offre", "online", "en ligne", "reservation", "booking",
)
AI_BUYING_SCORE_DECISION_MAKER_TERMS = (
    "gerant", "gerante", "directeur", "directrice", "director", "owner", "founder",
    "fondateur", "fondatrice", "ceo", "manager",
)
AI_BUYING_SCORE_INACTIVE_TERMS = (
    "ferme", "closed", "permanently closed", "cessation", "liquidation", "inactive",
)
AI_RECOMMENDED_OFFER_RULES = (
    (("restaurant", "cafe", "hotel", "riad", "hote", "traiteur"), "AI WhatsApp Ordering and Reservation Assistant"),
    (("clinique", "medical", "dentaire", "radiologie", "laboratoire", "veterinaire"), "AI Receptionist and Appointment Assistant"),
    (("immobili", "location", "architect"), "AI Lead Qualification and Viewing Assistant"),
    (("ecole", "formation", "creche", "auto ecole"), "AI Admissions and Prospect Follow-up Assistant"),
    (("salon", "beaute", "spa", "fitness", "sport"), "AI Booking and Client Follow-up Assistant"),
    (("agence", "avocat", "comptable", "etudes"), "AI Lead Qualification and CRM Follow-up Assistant"),
    (("boutique", "magasin", "supermarche", "bijouter", "opticien"), "AI Customer Service and Order Assistant"),
)
AI_RECOMMENDED_OFFER_DEFAULT = "AI Lead Qualification and Follow-up Assistant"

# Editable collection coverage. Environment variables LEAD_CITIES and LEAD_SECTORS
# take precedence when a narrower or different campaign is required.
SUPPORTED_CITIES = ["Agadir", "Casablanca", "Marrakech"]
COLLECTION_CITIES = ["Agadir", "Marrakech"]
COLLECTION_SECTORS = [
    "Accounting Firms", "Law Firms", "Medical Clinics", "Dental Clinics",
    "Laboratories", "Pharmacies", "Real Estate Agencies", "Construction Companies",
    "Architecture Firms", "Engineering Firms", "Logistics Companies", "Freight Forwarders",
    "Transport Companies", "Hotels", "Riads", "Travel Agencies", "Car Rental",
    "Automotive Dealers", "Automotive Repair", "Industrial Suppliers", "Manufacturers",
    "Import Export Companies", "Printing Companies", "Marketing Agencies", "Digital Agencies",
    "Call Centers", "BPO Companies", "Training Centers", "Private Schools",
    "Language Centers", "Security Companies", "Cleaning Companies", "IT Companies",
    "Telecom Companies", "Furniture Companies", "Wholesale Businesses", "Distribution Companies",
]

DEFAULT_CITIES = ["Agadir", "Casablanca", "Marrakech"]

DEFAULT_SECTORS = [
    "agences immobilières", "cliniques dentaires", "cabinets médicaux",
    "pharmacies", "restaurants", "cafés", "hôtels", "maisons d’hôtes",
    "riads", "salles de sport", "centres de fitness", "écoles privées",
    "centres de formation", "crèches", "cabinets d’avocats",
    "cabinets comptables", "agences de voyage",
    "agences de location de voitures", "salons de coiffure",
    "instituts de beauté", "spas", "centres esthétiques",
    "garages automobiles", "concessionnaires automobiles",
    "sociétés de transport", "sociétés de nettoyage", "sociétés de sécurité",
    "entreprises de construction", "architectes", "bureaux d’études",
    "magasins de meubles", "boutiques de vêtements", "bijouteries", "opticiens",
    "laboratoires d’analyses", "centres de radiologie", "vétérinaires",
    "imprimeries", "agences marketing", "agences web",
    "entreprises informatiques", "centres d’appels", "grossistes",
    "distributeurs", "supermarchés", "pâtisseries", "boulangeries",
    "écoles de langues", "auto-écoles", "espaces de coworking",
    "sociétés d’événementiel", "photographes professionnels", "traiteurs",
]

PRIORITY_SECTORS = [
    "restaurants", "hôtels", "pharmacies", "salons de coiffure",
    "instituts de beauté", "écoles privées", "cliniques dentaires",
    "cabinets médicaux", "agences immobilières", "salles de sport",
]

SPREADSHEET_ID = "1V4sKtbuJQy-9fMhg3GHyS16BcYW6A0Euheew8FutMvc"
# Use every implemented free public directory by default.  OSM remains first because
# it is the most stable, but a business absent from OSM can now still be discovered
# by a Moroccan directory during the same run.  Google Places remains opt-in because
# it requires a billed API key and is governed by separate platform terms.
DEFAULT_SOURCES = ["osm_overpass", "pages_maroc", "maroc_annuaire", "pj_ma"]
OVERPASS_ENDPOINTS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.nchc.org.tw/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
]
OVERPASS_AREA_CACHE: dict[str, int] = {}
OVERPASS_RUN_REQUESTS = 0

OSM_SECTOR_TAGS: dict[str, list[tuple[str, str]]] = {
    "accounting firms": [("office", "accountant")],
    "law firms": [("office", "lawyer")],
    "medical clinics": [("healthcare", "clinic"), ("amenity", "clinic")],
    "dental clinics": [("healthcare", "dentist"), ("amenity", "dentist")],
    "laboratories": [("healthcare", "laboratory")],
    "real estate agencies": [("office", "estate_agent")],
    "construction companies": [("office", "construction_company")],
    "architecture firms": [("office", "architect")],
    "engineering firms": [("office", "engineer")],
    "logistics companies": [("office", "logistics")],
    "freight forwarders": [("office", "logistics")],
    "transport companies": [("office", "logistics")],
    "travel agencies": [("shop", "travel_agency")],
    "car rental": [("amenity", "car_rental")],
    "automotive dealers": [("shop", "car")],
    "automotive repair": [("shop", "car_repair")],
    "industrial suppliers": [("office", "company")],
    "manufacturers": [("office", "company")],
    "import export companies": [("office", "company")],
    "printing companies": [("shop", "copyshop")],
    "marketing agencies": [("office", "advertising_agency")],
    "digital agencies": [("office", "it")],
    "call centers": [("office", "telecommunication")],
    "bpo companies": [("office", "telecommunication")],
    "training centers": [("amenity", "training")],
    "private schools": [("amenity", "school")],
    "language centers": [("amenity", "language_school")],
    "security companies": [("office", "security")],
    "cleaning companies": [("office", "cleaning")],
    "it companies": [("office", "it")],
    "telecom companies": [("office", "telecommunication")],
    "furniture companies": [("shop", "furniture")],
    "wholesale businesses": [("shop", "wholesale")],
    "distribution companies": [("office", "company")],
    "restaurants": [("amenity", "restaurant")],
    "cafes": [("amenity", "cafe")],
    "hotels": [("tourism", "hotel")],
    "maisons d hotes": [("tourism", "guest_house")],
    "riads": [("tourism", "guest_house")],
    "pharmacies": [("amenity", "pharmacy")],
    "salons de coiffure": [("shop", "hairdresser")],
    "instituts de beaute": [("shop", "beauty")],
    "spas": [("leisure", "spa")],
    "centres esthetiques": [("shop", "beauty")],
    "agences de voyage": [("shop", "travel_agency")],
    "salles de sport": [("leisure", "fitness_centre")],
    "centres de fitness": [("leisure", "fitness_centre")],
    "cabinets d avocats": [("office", "lawyer")],
    "agences immobilieres": [("office", "estate_agent")],
    "veterinaires": [("amenity", "veterinary")],
    "ecoles privees": [("amenity", "school")],
    "cliniques dentaires": [("healthcare", "dentist"), ("amenity", "dentist")],
    "cabinets medicaux": [("healthcare", "doctor"), ("amenity", "doctors")],
    "creches": [("amenity", "kindergarten")],
    "centres de formation": [("amenity", "training")],
    "ecoles de langues": [("amenity", "language_school")],
    "auto ecoles": [("amenity", "driving_school")],
    "cabinets comptables": [("office", "accountant")],
    "agences de location de voitures": [("amenity", "car_rental")],
    "garages automobiles": [("shop", "car_repair")],
    "concessionnaires automobiles": [("shop", "car")],
    "societes de transport": [("office", "logistics")],
    "societes de nettoyage": [("office", "cleaning")],
    "societes de securite": [("office", "security")],
    "entreprises de construction": [("office", "construction_company")],
    "architectes": [("office", "architect")],
    "bureaux d etudes": [("office", "consulting")],
    "magasins de meubles": [("shop", "furniture")],
    "boutiques de vetements": [("shop", "clothes")],
    "bijouteries": [("shop", "jewelry")],
    "opticiens": [("shop", "optician")],
    "laboratoires d analyses": [("healthcare", "laboratory")],
    "centres de radiologie": [("healthcare", "centre")],
    "imprimeries": [("shop", "copyshop")],
    "agences marketing": [("office", "advertising_agency")],
    "agences web": [("office", "it")],
    "entreprises informatiques": [("office", "it")],
    "centres d appels": [("office", "telecommunication")],
    "grossistes": [("shop", "wholesale")],
    "distributeurs": [("office", "company")],
    "supermarches": [("shop", "supermarket")],
    "patisseries": [("shop", "pastry")],
    "boulangeries": [("shop", "bakery")],
    "espaces de coworking": [("office", "coworking")],
    "societes d evenementiel": [("office", "event_management")],
    "photographes professionnels": [("craft", "photographer")],
    "traiteurs": [("craft", "caterer")],
}
BLOCKED_HOSTS = {
    "duckduckgo.com", "google.com", "facebook.com", "instagram.com",
    "linkedin.com", "youtube.com", "tripadvisor.com", "booking.com",
    "x.com", "twitter.com", "tiktok.com", "pinterest.com",
    "bing.com", "msn.com", "yahoo.com", "yelp.com", "mapcarta.com",
    "trip.com", "expedia.com", "telecontact.ma", "annuaire-gratuit.ma",
    "charika.ma", "kerix.net", "kompass.com", "marocannuaire.org",
    "pj.ma", "pagesjaunes.ma", "pages-maroc.com", "openstreetmap.org",
    "wa.me", "whatsapp.com", "maps.google.com",
    "leconomiste.com", "hespress.com", "le360.ma", "medias24.com",
}
EMAIL_RE = re.compile(r"[A-Z0-9._%+\-]+@[A-Z0-9.\-]+\.[A-Z]{2,}", re.I)
PHONE_RE = re.compile(r"(?:(?:\+|00)212[\s.()\-/]*[5-7](?:[\s.()\-/]*\d){8}|0[5-7](?:[\s.()\-/]*\d){8})")
MOROCCAN_DIGIT_TRANSLATION = str.maketrans(
    "٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹", "01234567890123456789"
)


@dataclass(frozen=True)
class Settings:
    cities: list[str]
    sectors: list[str]
    max_results: int
    max_total: int
    target_leads: int
    max_searches: int | None
    sources: list[str]
    delay_min: float
    delay_max: float
    connect_timeout: float
    read_timeout: float
    fast_mode: bool
    allow_landlines: bool
    google_maps_api_key: str
    max_place_details: int
    spreadsheet_id: str
    worksheet_name: str
    credentials_file: str
    dry_run: bool


@dataclass(frozen=True)
class SearchTask:
    """One globally-budgeted provider/city/sector discovery operation."""
    provider: str
    city: str
    sector: str


@dataclass(frozen=True)
class SchedulerCursor:
    city_index: int = 0
    sector_index: int = 0
    provider_index: int = 0


def cursor_for_task(settings: Settings, task: SearchTask) -> SchedulerCursor:
    return SchedulerCursor(
        city_index=settings.cities.index(task.city),
        sector_index=settings.sectors.index(task.sector),
        provider_index=settings.sources.index(task.provider),
    )


def cursor_position(settings: Settings, plan: list[SearchTask], cursor: SchedulerCursor) -> int:
    if not (
        0 <= cursor.city_index < len(settings.cities)
        and 0 <= cursor.sector_index < len(settings.sectors)
        and 0 <= cursor.provider_index < len(settings.sources)
    ):
        logging.warning("Stored scheduler cursor is outside the current configuration; resetting to start")
        return 0
    target = SearchTask(
        settings.sources[cursor.provider_index],
        settings.cities[cursor.city_index],
        settings.sectors[cursor.sector_index],
    )
    try:
        return plan.index(target)
    except ValueError:
        logging.warning("Stored scheduler cursor is not in the current plan; resetting to start")
        return 0


@dataclass
class RunStatistics:
    searches_by_provider: Counter[str]
    searches_by_city: Counter[str]
    searches_by_sector: Counter[str]
    candidates_discovered: int = 0
    candidates_enriched: int = 0
    candidates_rejected: int = 0
    duplicates_skipped: int = 0

    @classmethod
    def create(cls) -> "RunStatistics":
        return cls(Counter(), Counter(), Counter())

    @property
    def searches(self) -> int:
        return sum(self.searches_by_provider.values())

    def record_search(self, task: SearchTask) -> None:
        self.searches_by_provider[task.provider] += 1
        self.searches_by_city[task.city] += 1
        self.searches_by_sector[task.sector] += 1


def round_robin_search_plan(settings: Settings) -> list[SearchTask]:
    """Interleave providers, cities and sectors while covering their full product."""
    plan: list[SearchTask] = []
    for sector_index in range(len(settings.sectors)):
        city_offset = sector_index % len(settings.cities)
        cities = settings.cities[city_offset:] + settings.cities[:city_offset]
        for city_index, city in enumerate(cities):
            sector = settings.sectors[sector_index]
            for provider_index in range(len(settings.sources)):
                provider = settings.sources[
                    (provider_index + sector_index + city_index) % len(settings.sources)
                ]
                plan.append(SearchTask(provider, city, sector))
    return plan


def csv_setting(name: str, default: list[str]) -> list[str]:
    value = os.getenv(name, "").strip()
    return [item.strip() for item in value.split(",") if item.strip()] if value else list(default)


def load_settings(
    dry_run: bool = False,
    cities_override: list[str] | None = None,
    sectors_override: list[str] | None = None,
    max_searches: int | None = None,
    sources_override: list[str] | None = None,
    target_leads: int = 30,
    fast_mode: bool = True,
    allow_landlines: bool = False,
    max_place_details: int = 30,
) -> Settings:
    load_dotenv()
    cities = cities_override or csv_setting("LEAD_CITIES", COLLECTION_CITIES)
    allowed = {normalise_text(city): city for city in SUPPORTED_CITIES}
    invalid = [city for city in cities if normalise_text(city) not in allowed]
    if invalid:
        raise ValueError(
            "LEAD_CITIES may contain only Agadir, Casablanca and Marrakech; "
            f"invalid: {', '.join(invalid)}"
        )
    cities = [allowed[normalise_text(city)] for city in cities]
    delay_min = float(os.getenv("LEAD_REQUEST_DELAY_MIN", "1.5"))
    delay_max = float(os.getenv("LEAD_REQUEST_DELAY_MAX", "3.0"))
    if delay_min < 0 or delay_max < delay_min:
        raise ValueError("Request delays must satisfy 0 <= minimum <= maximum")
    sources = sources_override or list(DEFAULT_SOURCES)
    invalid_sources = [source for source in sources if source not in SOURCE_ADAPTERS]
    if invalid_sources:
        raise ValueError(
            f"Unknown sources: {', '.join(invalid_sources)}. "
            f"Available sources: {', '.join(SOURCE_ADAPTERS)}"
        )
    return Settings(
        cities=cities,
        sectors=prioritize_sectors(sectors_override or csv_setting("LEAD_SECTORS", COLLECTION_SECTORS)),
        max_results=(
            min(5, max(1, int(os.getenv("LEAD_MAX_RESULTS_PER_SECTOR_CITY", "5"))))
            if fast_mode else max(1, int(os.getenv("LEAD_MAX_RESULTS_PER_SECTOR_CITY", "5")))
        ),
        max_total=max(1, int(os.getenv("LEAD_MAX_TOTAL_PER_RUN", "100"))),
        target_leads=max(1, target_leads),
        max_searches=max_searches,
        sources=sources,
        delay_min=delay_min,
        delay_max=delay_max,
        connect_timeout=4.0,
        read_timeout=8.0,
        fast_mode=fast_mode,
        allow_landlines=allow_landlines,
        google_maps_api_key=os.getenv("GOOGLE_MAPS_API_KEY", "").strip(),
        max_place_details=max(1, max_place_details),
        spreadsheet_id=os.getenv("GOOGLE_SHEET_ID", SPREADSHEET_ID).strip() or SPREADSHEET_ID,
        worksheet_name=os.getenv("GOOGLE_WORKSHEET_NAME", "Leads").strip() or "Leads",
        credentials_file=os.getenv("GOOGLE_SERVICE_ACCOUNT_FILE", "service_account.json"),
        dry_run=dry_run or os.getenv("LEAD_DRY_RUN", "").lower() in {"1", "true", "yes"},
    )


def normalise_text(value: str) -> str:
    value = unicodedata.normalize("NFKD", value or "")
    value = "".join(c for c in value if not unicodedata.combining(c)).lower()
    return re.sub(r"[^a-z0-9]+", " ", value).strip()


def prioritize_sectors(sectors: list[str]) -> list[str]:
    priorities = {normalise_text(sector): index for index, sector in enumerate(PRIORITY_SECTORS)}
    return [
        sector for _, sector in sorted(
            enumerate(sectors),
            key=lambda item: (priorities.get(normalise_text(item[1]), len(priorities)), item[0]),
        )
    ]


def normalise_phone(value: str) -> str:
    digits = re.sub(r"\D", "", str(value or "").translate(MOROCCAN_DIGIT_TRANSLATION))
    if digits.startswith("00212"):
        digits = digits[2:]
    if digits.startswith("0") and len(digits) == 10:
        digits = "212" + digits[1:]
    return digits


def canonical_moroccan_phone(value: str) -> str:
    digits = normalise_phone(value)
    return "+" + digits if re.fullmatch(r"212[5-7]\d{8}", digits) else ""


def classify_moroccan_phone(value: str) -> str:
    digits = normalise_phone(value)
    if re.fullmatch(r"212[67]\d{8}", digits):
        return "mobile"
    if re.fullmatch(r"2125\d{8}", digits):
        return "landline"
    return "unknown"


def preferred_moroccan_phone(values: Iterable[str]) -> str:
    normalized = list(dict.fromkeys(
        phone for phone in (canonical_moroccan_phone(value) for value in values) if phone
    ))
    for classification in ("mobile", "landline"):
        for phone in normalized:
            if classify_moroccan_phone(phone) == classification:
                return phone
    return ""


def canonical_url(value: str) -> str:
    if not value:
        return ""
    value = value.strip()
    if not value.startswith(("http://", "https://")):
        value = "https://" + value
    parsed = urlparse(value)
    host = (parsed.hostname or "").lower().removeprefix("www.")
    if not host:
        return ""
    path = re.sub(r"/+", "/", parsed.path).rstrip("/")
    return f"https://{host}{path}"


def canonical_website_url(value: str) -> str:
    normalized = canonical_url(value)
    host = (urlparse(normalized).hostname or "").lower().removeprefix("www.")
    return f"https://{host}" if host else ""


def host_is_blocked(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower().removeprefix("www.")
    return not host or any(host == item or host.endswith("." + item) for item in BLOCKED_HOSTS)


class SectorTimeout(RuntimeError):
    """Raised when the current sector exceeds its hard time budget."""


class SourceAccessError(RuntimeError):
    """A genuine source connection, timeout, or blocked-page failure."""

    def __init__(self, message: str, overpass_requests: int = 0):
        super().__init__(message)
        self.overpass_requests = overpass_requests


class PublicWebClient:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.session = requests.Session()
        self.session.mount("https://", HTTPAdapter(max_retries=0))
        self.session.mount("http://", HTTPAdapter(max_retries=0))
        self.session.headers.update({
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/126.0.0.0 Safari/537.36"
            ),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
            "Accept-Language": "fr-FR,fr;q=0.9,en-US;q=0.7,en;q=0.6",
        })
        self._last_request = 0.0
        self.sector_deadline: float | None = None
        self.text_search_requests = 0
        self.place_details_requests = 0
        self.places_discovered = 0
        self.overpass_requests = 0
        self.osm_businesses_discovered = 0
        self.last_overpass_endpoint = ""
        self.last_overpass_status = 0

    def start_sector_timer(self, seconds: float = 20.0) -> None:
        self.sector_deadline = time.monotonic() + seconds

    def clear_sector_timer(self) -> None:
        self.sector_deadline = None

    def sector_seconds_remaining(self) -> float:
        if self.sector_deadline is None:
            return float("inf")
        return self.sector_deadline - time.monotonic()

    def request(self, method: str, url: str, **kwargs: object) -> requests.Response:
        requested_timeout = kwargs.pop("_timeout", None)
        raise_for_status = bool(kwargs.pop("_raise_for_status", True))
        budget = self.sector_seconds_remaining()
        if budget <= 0:
            raise SectorTimeout("Sector exceeded its 20-second hard timeout")
        target_delay = random.uniform(self.settings.delay_min, self.settings.delay_max)
        if self.settings.fast_mode:
            target_delay = min(target_delay, 0.5)
        remaining = target_delay - (time.monotonic() - self._last_request)
        if remaining > 0:
            if remaining >= budget:
                raise SectorTimeout("Sector exceeded its 20-second hard timeout during request delay")
            time.sleep(remaining)
        budget = self.sector_seconds_remaining()
        if budget <= 0:
            raise SectorTimeout("Sector exceeded its 20-second hard timeout")
        if requested_timeout:
            connect_timeout, read_timeout = map(float, requested_timeout)
        else:
            connect_timeout = min(self.settings.connect_timeout, max(0.1, budget / 3))
            read_timeout = min(self.settings.read_timeout, max(0.1, budget - connect_timeout))
        try:
            response = self.session.request(
                method,
                url,
                timeout=(connect_timeout, read_timeout),
                **kwargs,
            )
            self._last_request = time.monotonic()
            if raise_for_status:
                response.raise_for_status()
            return response
        except requests.RequestException as exc:
            self._last_request = time.monotonic()
            if self.sector_seconds_remaining() <= 0:
                raise SectorTimeout("Sector exceeded its 20-second hard timeout") from exc
            raise RuntimeError(f"Request failed for {url}: {exc}") from exc

    def fetch_soup(self, url: str, **kwargs: object) -> tuple[BeautifulSoup, requests.Response]:
        response = self.request("GET", url, allow_redirects=True, **kwargs)
        if "html" not in response.headers.get("Content-Type", "").lower():
            raise RuntimeError(f"Non-HTML response from {url}")
        return BeautifulSoup(response.text[:2_000_000], "html.parser"), response

    def enrich_business(self, result: dict[str, str], sector: str, city: str) -> dict[str, str] | None:
        website = result.get("website", "")
        email = result.get("email", "")
        phone = result.get("phone", "")
        description = result.get("business_description", "")
        if not website:
            return directory_lead_to_sheet(result, sector, city)
        original_deadline = self.sector_deadline
        website_deadline = time.monotonic() + 8.0
        self.sector_deadline = min(original_deadline, website_deadline) if original_deadline else website_deadline
        try:
            soup, response = self.fetch_soup(website)
        except SectorTimeout as exc:
            logging.debug("Website enrichment reached its 8-second budget for %s: %s", website, exc)
            self.sector_deadline = original_deadline
            return directory_lead_to_sheet(result, sector, city)
        except RuntimeError as exc:
            logging.warning("Official website inaccessible; keeping directory data %s: %s", website, exc)
            self.sector_deadline = original_deadline
            return directory_lead_to_sheet(result, sector, city)
        try:
            for node in soup(["script", "style", "noscript", "svg"]):
                node.decompose()
            page_text = clean_text(soup.get_text(" ", strip=True))
            evidence = " ".join((result["company_name"], result["location"], page_text[:100_000]))
            if not result.get("_osm_identity") and not valid_location(evidence, response.url, city):
                logging.info("Official website lacks target-city evidence; keeping validated directory data: %s", response.url)
                return directory_lead_to_sheet(result, sector, city)
            result = dict(result)
            result["email"] = preferred_public_email(soup, page_text, response.url) or email
            result["contact_form_url"] = contact_form_from_soup(soup, response.url)
            whatsapp_phone = explicit_whatsapp_from_soup(soup)
            page_phone = first_moroccan_phone(soup, page_text)
            result["phone"] = whatsapp_phone or page_phone or phone
            result["whatsapp_confirmed"] = bool(
                result.get("whatsapp_confirmed") or whatsapp_phone
            )
            if whatsapp_phone:
                logging.info("Explicit WhatsApp found: %s | %s", result.get("company_name", ""), whatsapp_phone)
                logging.info("Mobile found: %s | %s", result.get("company_name", ""), whatsapp_phone)
            result["website"] = canonical_url(response.url)
            result["business_description"] = extract_description(soup, description, sector, city)
            needs_more_contact = (
                not result["email"]
                or classify_moroccan_phone(result["phone"]) != "mobile"
                or not result["whatsapp_confirmed"]
            )
            if needs_more_contact:
                # Contact information is commonly split across a contact page, an
                # about page, and a legal/footer page.  Keep this bounded so one
                # slow site cannot consume the sector budget.
                for internal_url in candidate_internal_pages(soup, response.url)[:3]:
                    if time.monotonic() >= website_deadline:
                        break
                    try:
                        internal_soup, _ = self.fetch_soup(internal_url)
                    except (RuntimeError, SectorTimeout):
                        continue
                    internal_text = clean_text(internal_soup.get_text(" ", strip=True))
                    result["email"] = result["email"] or preferred_public_email(
                        internal_soup, internal_text, response.url
                    )
                    result["contact_form_url"] = result.get("contact_form_url") or contact_form_from_soup(
                        internal_soup, internal_url
                    )
                    internal_whatsapp = explicit_whatsapp_from_soup(internal_soup)
                    internal_phone = first_moroccan_phone(internal_soup, internal_text)
                    if internal_whatsapp:
                        result["phone"] = internal_whatsapp
                        result["whatsapp_confirmed"] = True
                        logging.info("Explicit WhatsApp found: %s | %s", result.get("company_name", ""), internal_whatsapp)
                    elif (
                        classify_moroccan_phone(result.get("phone", "")) != "mobile"
                        and classify_moroccan_phone(internal_phone) == "mobile"
                    ):
                        result["phone"] = internal_phone
                    if result["email"] and result["whatsapp_confirmed"]:
                        break
            return directory_lead_to_sheet(result, sector, city)
        finally:
            self.sector_deadline = original_deadline


def clean_text(value: str, limit: int | None = None) -> str:
    value = re.sub(r"\s+", " ", html.unescape(value or "")).strip()
    return value[:limit].rstrip() if limit else value


def valid_location(text: str, url: str, city: str) -> bool:
    normalized = normalise_text(text)
    city_present = normalise_text(city) in normalized
    morocco_present = any(marker in normalized for marker in ("maroc", "morocco", "royaume du maroc"))
    host = (urlparse(url).hostname or "").lower()
    return city_present and (morocco_present or host.endswith(".ma"))


def directory_location_valid(value: str, city: str) -> bool:
    """Require explicit evidence for the requested city in each directory entry."""
    normalized = normalise_text(value)
    return normalise_text(city) in normalized


def sector_slug(sector: str) -> str:
    return normalise_text(sector).replace(" ", "-")


def closest_business_card(heading: BeautifulSoup, city: str) -> BeautifulSoup:
    card = heading
    for _ in range(5):
        parent = card.parent
        if parent is None:
            break
        card = parent
        text = clean_text(card.get_text(" ", strip=True))
        if directory_location_valid(text, city) and len(text) >= len(city) + 3:
            return card
    return heading.parent or heading


def website_matches_business(website: str, company_name: str) -> bool:
    host = (urlparse(website).hostname or "").lower().removeprefix("www.")
    host_name = normalise_text(host.rsplit(".", 1)[0])
    company = normalise_text(company_name)
    generic_tokens = {
        "business", "company", "societe", "restaurant", "hotel", "clinique",
        "agence", "maroc", "morocco", "officiel", "official", "groupe",
    }
    tokens = {token for token in company.split() if len(token) >= 4 and token not in generic_tokens}
    return bool(
        host_name and company and (
            any(token in host_name for token in tokens)
            or SequenceMatcher(None, host_name.replace(" ", ""), company.replace(" ", "")).ratio() >= 0.58
        )
    )


def structured_directory_urls(soup: BeautifulSoup) -> list[str]:
    urls: list[str] = []
    for node in soup.select('script[type="application/ld+json"]'):
        try:
            payload = json.loads(node.string or node.get_text())
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        stack = [payload]
        while stack:
            value = stack.pop()
            if isinstance(value, dict):
                for key, child in value.items():
                    if key in {"url", "sameAs"}:
                        stack.extend(child if isinstance(child, list) else [child])
                    elif isinstance(child, (dict, list)):
                        stack.append(child)
            elif isinstance(value, list):
                stack.extend(value)
            elif isinstance(value, str) and value.startswith(("http://", "https://")):
                urls.append(value)
    for node in soup.select('meta[property="og:url"][content]'):
        urls.append(node.get("content", ""))
    return urls


def external_business_website(soup: BeautifulSoup, source_url: str, company_name: str = "") -> str:
    source_host = (urlparse(source_url).hostname or "").lower().removeprefix("www.")
    rejected = BLOCKED_HOSTS | {
        "pages-maroc.com", "marocannuaire.org", "pj.ma", "maps.google.com",
        "google.fr", "wa.me", "whatsapp.com", "bit.ly", "linktr.ee",
        "doubleclick.net", "googleadservices.com", "adservice.google.com",
    }
    candidates: list[tuple[int, str]] = []
    preferred_labels = {"site web", "website", "site officiel", "visiter le site", "www"}
    company_tokens = {token for token in normalise_text(company_name).split() if len(token) >= 4}
    for link in soup.select("a[href]"):
        href = link.get("href", "").strip()
        if href.startswith(("mailto:", "tel:", "javascript:", "#")):
            continue
        url = urljoin(source_url, href)
        parsed = urlparse(url)
        if parsed.scheme.lower() not in {"http", "https"}:
            continue
        host = (parsed.hostname or "").lower().removeprefix("www.")
        if not host or host == source_host or any(host == item or host.endswith("." + item) for item in rejected):
            continue
        if re.search(r"\.(?:pdf|jpg|jpeg|png|gif|webp|zip)(?:$|\?)", parsed.path, re.I):
            continue
        label = normalise_text(link.get_text(" ", strip=True))
        company_domain_match = any(token in normalise_text(host) for token in company_tokens)
        if label not in preferred_labels and "officiel" not in label and not company_domain_match:
            continue
        score = 100 if label in preferred_labels else 0
        score += 30 if "officiel" in label else 0
        score += 15 if host.endswith(".ma") else 0
        score += 40 if company_domain_match else 0
        if score and website_matches_business(url, company_name):
            candidates.append((score, canonical_website_url(url)))
    for url in structured_directory_urls(soup):
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower().removeprefix("www.")
        if parsed.scheme not in {"http", "https"} or not host or host == source_host:
            continue
        if any(host == item or host.endswith("." + item) for item in rejected):
            continue
        if website_matches_business(url, company_name):
            candidates.append((60, canonical_website_url(url)))
    return max(candidates, default=(0, ""), key=lambda candidate: candidate[0])[1]


def parse_directory_detail(
    client: PublicWebClient, entry: dict[str, str]
) -> dict[str, str]:
    source_url = entry["source_url"]
    try:
        soup, _ = client.fetch_soup(source_url)
    except SectorTimeout:
        raise
    except RuntimeError as exc:
        logging.debug("Directory detail unavailable %s: %s", source_url, exc)
        return entry
    text = clean_text(soup.get_text(" ", strip=True))
    email = first_public_email(soup, text)
    source_host = (urlparse(source_url).hostname or "").lower().removeprefix("www.")
    if email.endswith("@" + source_host) or email in {"contact@pagesmaroc.com", "info@pj.ma"}:
        email = ""
    enriched = dict(entry)
    enriched["email"] = email or entry.get("email", "")
    enriched["phone"] = first_moroccan_phone(soup, text) or entry.get("phone", "")
    discovered_website = external_business_website(soup, source_url, entry.get("company_name", ""))
    enriched["website"] = discovered_website or entry.get("website", "")
    return enriched


def pagination_links(soup: BeautifulSoup, current_url: str) -> list[str]:
    host = (urlparse(current_url).hostname or "").lower()
    links: list[str] = []
    for link in soup.select('a[href], link[rel="next"]'):
        label = normalise_text(link.get_text(" ", strip=True))
        rel = " ".join(link.get("rel", [])) if isinstance(link.get("rel"), list) else str(link.get("rel", ""))
        if not (label.isdigit() or label in {"suivant", "next"} or "next" in rel.lower()):
            continue
        url = urljoin(current_url, link.get("href", ""))
        if (urlparse(url).hostname or "").lower() == host:
            links.append(url)
    return links


def parse_heading_directory_page(
    client: PublicWebClient,
    soup: BeautifulSoup,
    page_url: str,
    sector: str,
    city: str,
    detail_pattern: str,
) -> list[dict[str, str]]:
    entries: list[dict[str, str]] = []
    for heading in soup.select("h2, h3, h4"):
        link = heading.select_one("a[href]")
        name = clean_company_name(heading.get_text(" ", strip=True))
        if not name or normalise_text(name) in {normalise_text(sector), normalise_text(city)}:
            continue
        card = closest_business_card(heading, city)
        card_text = clean_text(card.get_text(" ", strip=True), 600)
        if not directory_location_valid(card_text, city):
            continue
        detail_url = urljoin(page_url, link.get("href", "")) if link else page_url
        if detail_pattern and detail_pattern not in detail_url.lower():
            continue
        phone = first_moroccan_phone(card, card_text)
        email = first_public_email(card, card_text)
        entries.append({
            "company_name": name,
            "industry": sector,
            "location": f"{city}, Morocco",
            "source_url": detail_url,
            "website": "",
            "phone": phone,
            "email": email,
            "business_description": clean_text(card_text, 450),
        })
    return entries


def crawl_directory_pages(
    client: PublicWebClient,
    first_url: str,
    params: dict[str, str] | None,
    sector: str,
    city: str,
    limit: int,
    detail_pattern: str,
) -> list[dict[str, str]]:
    queue: list[tuple[str, dict[str, str] | None]] = [(first_url, params)]
    visited: set[str] = set()
    entries: list[dict[str, str]] = []
    while queue and len(entries) < limit and len(visited) < 10:
        page_url, page_params = queue.pop(0)
        try:
            soup, response = client.fetch_soup(page_url, params=page_params)
        except SectorTimeout:
            raise
        except RuntimeError as exc:
            logging.warning("Skipping inaccessible directory page %s: %s", page_url, exc)
            raise SourceAccessError(str(exc)) from exc
        page_markers = normalise_text(soup.get_text(" ", strip=True))
        if any(marker in page_markers for marker in (
            "captcha", "access denied", "verify you are human", "temporarily blocked",
            "too many requests", "cloudflare ray id",
        )):
            raise SourceAccessError(f"Blocked response from {response.url}")
        resolved_url = response.url
        if resolved_url in visited:
            continue
        visited.add(resolved_url)
        parsed = parse_heading_directory_page(client, soup, resolved_url, sector, city, detail_pattern)
        if not parsed:
            logging.info("Source category page contained no business cards; skipping without detail-page visits: %s", resolved_url)
            break
        existing = {(normalise_text(item["company_name"]), item["source_url"]) for item in entries}
        entries.extend(item for item in parsed if (normalise_text(item["company_name"]), item["source_url"]) not in existing)
        if not client.settings.fast_mode:
            for next_url in pagination_links(soup, resolved_url):
                if next_url not in visited and all(next_url != queued[0] for queued in queue):
                    queue.append((next_url, None))
    detailed: list[dict[str, str]] = []
    for entry in entries[:limit]:
        if client.sector_seconds_remaining() <= 0:
            raise SectorTimeout("Sector exceeded its 20-second hard timeout before detail enrichment")
        detailed.append(parse_directory_detail(client, entry))
    return detailed


def adapter_pages_maroc(client: PublicWebClient, sector: str, city: str, limit: int) -> list[dict[str, str]]:
    url = f"https://www.pages-maroc.com/{sector_slug(sector)}-{city.upper()}.html"
    detail_pattern = f"{sector_slug(sector)}-{city.lower()}-"
    return crawl_directory_pages(client, url, None, sector, city, limit, detail_pattern)


def adapter_maroc_annuaire(client: PublicWebClient, sector: str, city: str, limit: int) -> list[dict[str, str]]:
    url = "https://www.marocannuaire.org/Recherche/recherch_par_activite_ville.php"
    params = {"activite": sector, "ville": city.upper()}
    return crawl_directory_pages(client, url, params, sector, city, limit, "details_infos")


PJ_INTERFACE_LABELS = {
    "filtres de recherche", "aucun resultat trouve", "rechercher", "resultats",
    "resultat", "accueil", "suivant", "precedent",
}


def is_pj_interface_label(value: str) -> bool:
    normalized = normalise_text(value)
    if normalized in PJ_INTERFACE_LABELS:
        return True
    if normalized.startswith("aucun ") and "trouv" in normalized:
        return True
    return normalized.startswith("filtre") and "recherche" in normalized


def json_ld_contact_values(soup: BeautifulSoup) -> tuple[list[str], list[str], list[str]]:
    phones: list[str] = []
    emails: list[str] = []
    urls: list[str] = []
    for node in soup.select('script[type="application/ld+json"]'):
        try:
            payload = json.loads(node.string or node.get_text())
        except (TypeError, ValueError):
            continue
        stack = [payload]
        while stack:
            value = stack.pop()
            if isinstance(value, list):
                stack.extend(value)
            elif isinstance(value, dict):
                for key, child in value.items():
                    if key == "telephone" and isinstance(child, str):
                        phones.append(child)
                    elif key == "email" and isinstance(child, str):
                        emails.append(child.removeprefix("mailto:"))
                    elif key in {"url", "sameAs"}:
                        stack.extend(child if isinstance(child, list) else [child])
                    elif isinstance(child, (dict, list)):
                        stack.append(child)
            elif isinstance(value, str) and value.startswith(("http://", "https://")):
                urls.append(value)
    return phones, emails, urls


def pj_real_business_cards(
    soup: BeautifulSoup, page_url: str, sector: str, city: str, limit: int
) -> list[dict[str, str]]:
    entries: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    selectors = (
        "article", ".search-result-item", ".result-item", ".professional-card",
        ".company-card", ".result-card", ".search-result", ".result", ".card",
        "[itemtype*='LocalBusiness']", "li",
    )
    for card in soup.select(", ".join(selectors)):
        heading = card.select_one("h2, h3, h4, [itemprop='name']")
        if not heading:
            continue
        name = clean_company_name(heading.get_text(" ", strip=True))
        if not name or is_pj_interface_label(name):
            continue
        card_text = clean_text(card.get_text(" ", strip=True), 700)
        if not directory_location_valid(card_text, city):
            continue
        link = heading.select_one("a[href]") or card.select_one(
            "a[href*='detail'], a[href*='profession'], a[href*='fiche'], a[href]"
        )
        href = link.get("href", "") if link else ""
        if not href:
            data_link = card.select_one("[data-href], [data-url]")
            href = (
                card.get("data-href", "") or card.get("data-url", "")
                or (data_link.get("data-href", "") if data_link else "")
                or (data_link.get("data-url", "") if data_link else "")
            )
        if not href:
            onclick = card.get("onclick", "")
            match = re.search(r"(?:location(?:\.href)?\s*=|window\.open\()[\s'\"]*([^'\"),;]+)", onclick)
            href = match.group(1) if match else ""
        if not href:
            continue
        detail_url = urljoin(page_url, href)
        parsed = urlparse(detail_url)
        host = (parsed.hostname or "").lower().removeprefix("www.")
        path = parsed.path.lower()
        if (
            parsed.scheme not in {"http", "https"} or host != "pj.ma"
            or path in {"", "/"} or "search-results" in path
            or any(marker in path for marker in ("guide", "villes", "login", "connexion"))
        ):
            continue
        key = (normalise_text(name), detail_url)
        if key in seen:
            continue
        seen.add(key)
        address_node = card.select_one(".address, .adresse, [itemprop='address']")
        address = clean_text(address_node.get_text(" ", strip=True), 300) if address_node else card_text
        entry = {
            "company_name": name,
            "industry": sector,
            "location": f"{city}, Morocco",
            "source_url": detail_url,
            "website": "",
            "phone": "",
            "email": "",
            "business_description": clean_text(f"{sector}. {address}", 450),
        }
        logging.debug("PJ candidate name=%r detail_url=%s", name, detail_url)
        entries.append(entry)
        if len(entries) >= limit:
            break
    return entries


def enrich_pj_detail(client: PublicWebClient, entry: dict[str, str], city: str) -> dict[str, str] | None:
    try:
        soup, response = client.fetch_soup(entry["source_url"])
    except SectorTimeout:
        raise
    except RuntimeError as exc:
        logging.debug("PJ detail inaccessible: %s: %s", entry["source_url"], exc)
        return None
    page_text = clean_text(soup.get_text(" ", strip=True))
    if not directory_location_valid(page_text + " " + entry["location"], city):
        return None
    json_phones, json_emails, _ = json_ld_contact_values(soup)
    raw_phones = [link.get("href", "")[4:] for link in soup.select('a[href^="tel:"]')]
    raw_phones.extend(moroccan_phone_matches(page_text))
    raw_phones.extend(json_phones)
    phone = preferred_moroccan_phone(raw_phones)
    normalized_mobile = phone if classify_moroccan_phone(phone) == "mobile" else ""
    emails = public_email_candidates(soup, page_text)
    emails.extend(email.lower() for email in json_emails if EMAIL_RE.fullmatch(email))
    emails = list(dict.fromkeys(
        email for email in emails if not email.endswith("@pj.ma") and email != "info@pj.ma"
    ))
    website = external_business_website(soup, response.url, entry["company_name"])
    enriched = dict(entry)
    enriched["phone"] = phone
    enriched["email"] = emails[0] if emails else ""
    enriched["website"] = website
    logging.debug(
        "PJ candidate name=%r detail_url=%s raw_phones=%r normalized_mobile=%r emails=%r websites=%r",
        entry["company_name"], entry["source_url"], raw_phones, normalized_mobile,
        emails, [website] if website else [],
    )
    return enriched


def adapter_pj_ma(client: PublicWebClient, sector: str, city: str, limit: int) -> list[dict[str, str]]:
    url = "https://www.pj.ma/search-results"
    params = {"activity": sector.upper(), "city": city.upper()}
    try:
        soup, response = client.fetch_soup(url, params=params)
    except SectorTimeout:
        raise
    except RuntimeError as exc:
        raise SourceAccessError(str(exc)) from exc
    cards = pj_real_business_cards(soup, response.url, sector, city, min(5, limit))
    if not cards:
        logging.info("PJ category page contained no real business cards: %s / %s", sector, city)
        return []
    enriched: list[dict[str, str]] = []
    for card in cards:
        detail = enrich_pj_detail(client, card, city)
        if detail:
            enriched.append(detail)
    return enriched


def google_display_name(place: dict[str, object]) -> str:
    value = place.get("displayName", {})
    return clean_text(value.get("text", "")) if isinstance(value, dict) else clean_text(str(value))


def adapter_google_places(
    client: PublicWebClient, sector: str, city: str, limit: int
) -> list[dict[str, str]]:
    if not client.settings.google_maps_api_key:
        raise ValueError("GOOGLE_MAPS_API_KEY is missing. Add it to .env before using google_places.")
    text_mask = ",".join((
        "places.id", "places.displayName", "places.formattedAddress", "places.types",
        "places.rating", "places.userRatingCount",
    ))
    headers = {
        "X-Goog-Api-Key": client.settings.google_maps_api_key,
        "X-Goog-FieldMask": text_mask,
        "Content-Type": "application/json",
    }
    logging.info("Google Text Search started: %s à %s, Maroc", sector, city)
    client.text_search_requests += 1
    try:
        response = client.request(
            "POST",
            "https://places.googleapis.com/v1/places:searchText",
            headers=headers,
            json={
                "textQuery": f"{sector} à {city}, Maroc",
                "regionCode": "MA",
                "languageCode": "fr",
                "maxResultCount": 20,
            },
        )
        places = response.json().get("places", [])
    except (requests.RequestException, RuntimeError, ValueError) as exc:
        raise SourceAccessError(f"Google Text Search failed: {exc}") from exc

    city_places = [
        place for place in places
        if isinstance(place, dict)
        and place.get("id")
        and directory_location_valid(str(place.get("formattedAddress", "")), city)
    ]
    client.places_discovered += len(city_places)
    details_mask = ",".join((
        "id", "displayName", "formattedAddress", "internationalPhoneNumber",
        "nationalPhoneNumber", "websiteUri", "googleMapsUri", "types", "rating",
        "userRatingCount", "businessStatus",
    ))
    entries: list[dict[str, str]] = []
    for place in city_places[:min(limit, client.settings.max_place_details)]:
        place_id = str(place["id"])
        logging.info("Google place found: %s | %s", google_display_name(place), place_id)
        logging.info("Place Details requested: %s", place_id)
        client.place_details_requests += 1
        try:
            detail_response = client.request(
                "GET",
                f"https://places.googleapis.com/v1/places/{place_id}",
                headers={
                    "X-Goog-Api-Key": client.settings.google_maps_api_key,
                    "X-Goog-FieldMask": details_mask,
                },
            )
            detail = detail_response.json()
        except (requests.RequestException, RuntimeError, ValueError) as exc:
            logging.warning("Google Place Details skipped for %s: %s", place_id, exc)
            continue
        address = clean_text(str(detail.get("formattedAddress", "")))
        if not directory_location_valid(address, city):
            continue
        if detail.get("businessStatus") and detail.get("businessStatus") != "OPERATIONAL":
            continue
        phone = preferred_moroccan_phone([
            str(detail.get("internationalPhoneNumber", "")),
            str(detail.get("nationalPhoneNumber", "")),
        ])
        classification = classify_moroccan_phone(phone)
        if classification == "mobile":
            logging.info("Mobile found: %s | %s", google_display_name(detail), phone)
        elif classification == "landline" and not client.settings.allow_landlines:
            logging.info("Landline ignored: %s | %s", google_display_name(detail), phone)
        website = canonical_website_url(str(detail.get("websiteUri", "")))
        website_host = (urlparse(website).hostname or "").lower().removeprefix("www.")
        rejected_website_hosts = BLOCKED_HOSTS | {
            "pages-maroc.com", "marocannuaire.org", "pj.ma", "maps.google.com",
            "wa.me", "whatsapp.com",
        }
        if any(
            website_host == host or website_host.endswith("." + host)
            for host in rejected_website_hosts
        ):
            website = ""
        if website:
            logging.info("Website found: %s | %s", google_display_name(detail), website)
        types = ", ".join(str(value) for value in detail.get("types", [])[:4])
        rating = detail.get("rating", place.get("rating", ""))
        reviews = detail.get("userRatingCount", place.get("userRatingCount", ""))
        description = clean_text(
            f"Google category: {types or sector}. Address: {address}. "
            f"Rating: {rating or 'N/A'} from {reviews or 0} reviews.",
            450,
        )
        opportunity = automation_opportunity(sector)
        if reviews:
            opportunity = clean_text(
                f"{opportunity.rstrip('.')} et le suivi des avis clients ({reviews} avis visibles).",
                300,
            )
        entries.append({
            "company_name": google_display_name(detail) or google_display_name(place),
            "industry": sector,
            "location": f"{city}, Morocco",
            "source_url": str(detail.get("googleMapsUri", "")),
            "website": website,
            "phone": phone,
            "email": "",
            "business_description": description,
            "automation_opportunity": opportunity,
            "_google_place_id": place_id,
        })
    return entries


def explicit_whatsapp_number(values: Iterable[str]) -> str:
    candidates: list[str] = []
    for value in values:
        value = html.unescape(str(value or ""))
        parsed = urlparse(value)
        host = (parsed.hostname or "").lower().removeprefix("www.")
        if host in {"wa.me", "api.whatsapp.com", "web.whatsapp.com", "whatsapp.com"} or parsed.scheme == "whatsapp":
            candidates.extend(re.findall(r"(?:212|0)[67]\d{8}", value.translate(MOROCCAN_DIGIT_TRANSLATION)))
        else:
            candidates.append(value)
    phone = preferred_moroccan_phone(candidates)
    return phone if classify_moroccan_phone(phone) == "mobile" else ""


def explicit_whatsapp_from_soup(soup: BeautifulSoup) -> str:
    values: list[str] = []
    for link in soup.select("a[href]"):
        href = link.get("href", "")
        label = normalise_text(link.get_text(" ", strip=True))
        host = (urlparse(href).hostname or "").lower().removeprefix("www.")
        if host in {"wa.me", "api.whatsapp.com", "web.whatsapp.com", "whatsapp.com"} or "whatsapp" in label:
            values.append(href)
    return explicit_whatsapp_number(values)


def contact_form_from_soup(soup: BeautifulSoup, page_url: str) -> str:
    """Return the page containing a usable public contact form, if present."""
    for form in soup.select("form"):
        evidence = normalise_text(" ".join((
            form.get("action", ""), form.get("id", ""),
            " ".join(form.get("class", [])), form.get_text(" ", strip=True)[:500],
        )))
        has_contact_input = bool(form.select_one(
            'input[type="email"], input[type="tel"], textarea, '
            'input[name*="email" i], input[name*="phone" i], input[name*="message" i]'
        ))
        if has_contact_input or any(word in evidence for word in ("contact", "message", "demande", "devis")):
            return canonical_url(page_url)
    return ""


def overpass_json(client: PublicWebClient, query: str) -> dict[str, object]:
    global OVERPASS_RUN_REQUESTS
    last_error: Exception | None = None
    logging.debug("Final Overpass query:\n%s", query)
    headers = {
        "Content-Type": "application/x-www-form-urlencoded",
        "User-Agent": "BKZ-Lead-Collector/1.0",
        "Accept": "application/json",
    }
    for endpoint in OVERPASS_ENDPOINTS:
        client.overpass_requests += 1
        OVERPASS_RUN_REQUESTS += 1
        try:
            response = client.request(
                "POST", endpoint, data={"data": query}, headers=headers,
                _timeout=(5.0, 25.0), _raise_for_status=False,
            )
            client.last_overpass_endpoint = endpoint
            client.last_overpass_status = response.status_code
            logging.info("Overpass endpoint HTTP status: %s | %d", endpoint, response.status_code)
            if response.status_code == 406:
                last_error = RuntimeError("HTTP 406 request-format failure")
                logging.warning("Overpass request-format failure: %s | HTTP 406", endpoint)
                continue
            if not 200 <= response.status_code < 300:
                last_error = RuntimeError(f"HTTP {response.status_code}")
                continue
            content_type = response.headers.get("Content-Type", "").casefold()
            if "json" not in content_type:
                last_error = RuntimeError(f"non-JSON response ({content_type or 'unknown content type'})")
                logging.warning("Overpass returned HTML/non-JSON; endpoint skipped: %s", endpoint)
                continue
            payload = response.json()
            if isinstance(payload, dict):
                return payload
        except (RuntimeError, ValueError, requests.RequestException) as exc:
            last_error = exc
            logging.warning("Overpass endpoint unavailable: %s: %s", endpoint, exc)
    raise SourceAccessError(
        f"All Overpass endpoints failed: {last_error}", client.overpass_requests
    )


def build_overpass_query(sector: str, city: str) -> str:
    mappings = OSM_SECTOR_TAGS.get(normalise_text(sector), [])
    selectors = "".join(
        f'{element_type}["{key}"="{value}"](area.searchArea);'
        for key, value in mappings
        for element_type in ("node", "way", "relation")
    )
    return (
        f'[out:json][timeout:25];('
        f'area["boundary"="administrative"]["name"="{city}"];'
        f'area["boundary"="administrative"]["name:fr"="{city}"];'
        f'area["boundary"="administrative"]["name:en"="{city}"];'
        f')->.searchArea;({selectors});out tags center;'
    )


def adapter_osm_overpass(
    client: PublicWebClient, sector: str, city: str, limit: int
) -> list[dict[str, str]]:
    mappings = OSM_SECTOR_TAGS.get(normalise_text(sector), [])
    if not mappings:
        logging.debug("No OSM tag mapping for sector: %s", sector)
        return []
    cache_key = normalise_text(city)
    if cache_key not in OVERPASS_AREA_CACHE:
        OVERPASS_AREA_CACHE[cache_key] = 1
        logging.info("Overpass city area resolved: %s | direct area query", city)
    query = build_overpass_query(sector, city)
    payload = overpass_json(client, query)
    entries: list[dict[str, str]] = []
    for element in payload.get("elements", []):
        if not isinstance(element, dict) or element.get("type") not in {"node", "way", "relation"}:
            continue
        tags = element.get("tags", {})
        if not isinstance(tags, dict):
            continue
        name = clean_company_name(str(tags.get("name", "")))
        if not name:
            continue
        tagged_city = str(tags.get("addr:city", ""))
        if tagged_city and normalise_text(tagged_city) != normalise_text(city):
            continue
        phone_values = [str(tags.get(key, "")) for key in (
            "phone", "contact:phone", "mobile", "contact:mobile",
        )]
        phone_values.extend(moroccan_phone_matches(" ".join(phone_values)))
        whatsapp_values = [str(tags.get(key, "")) for key in ("whatsapp", "contact:whatsapp")]
        whatsapp_phone = explicit_whatsapp_number(whatsapp_values)
        phone = whatsapp_phone or preferred_moroccan_phone(phone_values)
        website = canonical_website_url(str(tags.get("website") or tags.get("contact:website") or ""))
        if website and host_is_blocked(website):
            website = ""
        email = clean_text(str(tags.get("email") or tags.get("contact:email") or "")).lower()
        if not EMAIL_RE.fullmatch(email):
            email = ""
        osm_identity = f'{element["type"]}/{element["id"]}'
        description_tags = [
            f"{key}={tags[key]}" for key in ("amenity", "shop", "office", "tourism", "healthcare", "craft")
            if tags.get(key)
        ]
        street = clean_text(str(tags.get("addr:street", "")))
        entry = {
            "company_name": name,
            "industry": sector,
            "location": f"{city}, Morocco",
            "source_url": f"https://www.openstreetmap.org/{osm_identity}",
            "website": website,
            "phone": phone,
            "email": email,
            "business_description": clean_text(
                f"OpenStreetMap: {', '.join(description_tags) or sector}. {street}, {city}.", 450
            ),
            "_osm_identity": osm_identity,
            "whatsapp_confirmed": bool(whatsapp_phone),
        }
        client.osm_businesses_discovered += 1
        logging.info("OSM business discovered: %s | %s", name, osm_identity)
        if classify_moroccan_phone(phone) == "mobile":
            logging.info("Mobile found: %s | %s", name, phone)
        if whatsapp_phone:
            logging.info("Explicit WhatsApp found: %s | %s", name, whatsapp_phone)
        if website:
            logging.info("Website found: %s | %s", name, website)
        if email:
            logging.info("Email found: %s | %s", name, email)
        entries.append(entry)
        if len(entries) >= max(20, limit):
            break
    return entries


SOURCE_ADAPTERS = {
    "osm_overpass": adapter_osm_overpass,
    "google_places": adapter_google_places,
    "pages_maroc": adapter_pages_maroc,
    "maroc_annuaire": adapter_maroc_annuaire,
    "pj_ma": adapter_pj_ma,
}


def strong_company_match(left: str, right: str) -> bool:
    left_normalized = normalise_text(left)
    right_normalized = normalise_text(right)
    if not left_normalized or not right_normalized:
        return False
    if left_normalized == right_normalized:
        return True
    if min(len(left_normalized), len(right_normalized)) < 8:
        return False
    return SequenceMatcher(None, left_normalized, right_normalized).ratio() >= 0.94


def merge_fallback_candidate(
    primary: dict[str, str], fallback: dict[str, str], fallback_name: str
) -> bool:
    changed = False
    primary_phone_class = classify_moroccan_phone(primary.get("phone", ""))
    fallback_phone = canonical_moroccan_phone(fallback.get("phone", ""))
    fallback_phone_class = classify_moroccan_phone(fallback_phone)
    if fallback_phone_class == "mobile" and primary_phone_class != "mobile":
        primary["phone"] = fallback_phone
        changed = True
        logging.info("Mobile found through fallback: %s | %s", fallback_name, primary["company_name"])
    elif not primary.get("phone") and fallback_phone_class == "landline":
        primary["phone"] = fallback_phone
        changed = True
    if not primary.get("email") and fallback.get("email"):
        primary["email"] = fallback["email"]
        changed = True
        logging.info("Email found through fallback: %s | %s", fallback_name, primary["company_name"])
    fallback_website = canonical_website_url(fallback.get("website", ""))
    if (
        not primary.get("website") and fallback_website
        and website_matches_business(fallback_website, primary.get("company_name", ""))
    ):
        primary["website"] = fallback_website
        changed = True
        logging.info("Website found through fallback: %s | %s", fallback_name, primary["company_name"])
    fallback_description = clean_text(fallback.get("business_description", ""), 450)
    if fallback_description and len(fallback_description) > len(primary.get("business_description", "")):
        primary["business_description"] = fallback_description
        changed = True
    return changed


def apply_directory_fallbacks(
    client: PublicWebClient,
    entries: list[dict[str, str]],
    primary_source: str,
    sector: str,
    city: str,
    limit: int,
    disabled_sources: set[str],
) -> tuple[list[dict[str, str]], int, set[str], set[str]]:
    enriched = [dict(entry) for entry in entries]
    enriched_indexes: set[int] = set()
    failed_sources: set[str] = set()
    successful_sources: set[str] = set()
    fallback_names = [
        source for source in client.settings.sources
        if source in {"pages_maroc", "maroc_annuaire", "pj_ma"} and source != primary_source
    ]
    for fallback_index, fallback_name in enumerate(fallback_names):
        if (
            fallback_name == primary_source or fallback_name in disabled_sources
        ):
            continue
        remaining_sources = len(fallback_names) - fallback_index
        remaining_budget = client.sector_seconds_remaining()
        if remaining_budget < 2.0:
            break
        original_deadline = client.sector_deadline
        client.sector_deadline = time.monotonic() + max(2.0, remaining_budget / remaining_sources)
        logging.info("Fallback source attempted: %s | %s | %s", fallback_name, sector, city)
        try:
            candidates = SOURCE_ADAPTERS[fallback_name](client, sector, city, min(5, limit))
            successful_sources.add(fallback_name)
        except SourceAccessError as exc:
            failed_sources.add(fallback_name)
            logging.warning("Fallback source access failed: %s: %s", fallback_name, exc)
            continue
        except SectorTimeout:
            logging.debug("Fallback source reached its allocated enrichment budget: %s", fallback_name)
            continue
        except RuntimeError as exc:
            logging.debug("Fallback source unavailable from %s: %s", fallback_name, exc)
            continue
        finally:
            client.sector_deadline = original_deadline
        for index, entry in enumerate(enriched):
            matches = [candidate for candidate in candidates if (
                directory_location_valid(candidate.get("location", ""), city)
                and strong_company_match(entry.get("company_name", ""), candidate.get("company_name", ""))
            )]
            if len(matches) != 1:
                continue
            if merge_fallback_candidate(entry, matches[0], fallback_name):
                enriched_indexes.add(index)
    return enriched, len(enriched_indexes), failed_sources, successful_sources


def directory_lead_to_sheet(result: dict[str, str], sector: str, city: str) -> dict[str, str] | None:
    name = clean_company_name(result.get("company_name", ""))
    if not name or not directory_location_valid(result.get("location", ""), city):
        return None
    phone = result.get("phone", "")
    website = canonical_website_url(result.get("website", ""))
    source_url = result.get("source_url", "")
    description = result.get("business_description", "") or f"{sector.capitalize()} basée à {city}."
    if source_url and source_url not in description:
        description = clean_text(f"{description} Source publique: {source_url}", 450)
    place_id = result.get("_google_place_id", "")
    osm_identity = result.get("_osm_identity", "")
    if place_id:
        lead_id = f"gplace_{place_id}"
    elif osm_identity:
        lead_id = "osm_" + osm_identity.replace("/", "_")
    else:
        lead_id = make_lead_id(name, phone, website or source_url)
    return {
        "lead_id": lead_id,
        "company_name": name,
        "industry": sector,
        "location": f"{city}, Morocco",
        "email": result.get("email", ""),
        "phone": phone,
        "website": website,
        "business_description": description,
        "automation_opportunity": result.get("automation_opportunity", "") or automation_opportunity(sector),
        "score": "",
        "contact_status": "Not Contacted",
        "personalised_message": "",
        "sent_at": "",
        "_google_place_id": place_id,
        "_osm_identity": osm_identity,
        "whatsapp_confirmed": bool(result.get("whatsapp_confirmed")),
        "whatsapp": canonical_moroccan_phone(result.get("phone", "")) if result.get("whatsapp_confirmed") else "",
        "contact_form_url": canonical_url(result.get("contact_form_url", "")),
    }


def apply_phone_policy(lead: dict[str, str], allow_landlines: bool) -> dict[str, str]:
    lead = dict(lead)
    phone = canonical_moroccan_phone(lead.get("phone", ""))
    classification = classify_moroccan_phone(phone)
    lead["_phone_classification"] = classification
    if classification == "mobile":
        lead["phone"] = phone
    elif classification == "landline" and allow_landlines:
        lead["phone"] = phone
    else:
        if classification == "landline":
            lead["_landline_phone"] = phone
        lead["phone"] = ""
    return lead


def execute_source_pipeline(
    settings: Settings,
    source_name: str,
    sector: str,
    city: str,
    limit: int,
    disabled_sources: set[str] | None = None,
) -> tuple[list[dict[str, str]], int, int, int, set[str], set[str], int, int, int, int]:
    """Run all network access and parsing for one source inside a timed worker."""
    client = PublicWebClient(settings)
    phase_budget = 9.0 if settings.fast_mode else 60.0
    client.start_sector_timer(phase_budget)
    accepted_limit = min(5, limit) if source_name == "osm_overpass" else limit
    candidate_limit = min(20, max(accepted_limit, 20)) if source_name == "osm_overpass" else min(
        20 if source_name == "google_places" else 5, limit
    )
    parsed = SOURCE_ADAPTERS[source_name](client, sector, city, candidate_limit)
    candidates_discovered = (
        client.places_discovered if source_name == "google_places"
        else client.osm_businesses_discovered if source_name == "osm_overpass"
        else len(parsed[:candidate_limit])
    )
    # Discovery has its own bounded window.  Once candidates exist, give them a
    # separate bounded enrichment window so fallback adapters are not starved.
    client.start_sector_timer(phase_budget)
    for item in parsed[:candidate_limit]:
        logging.info("Primary candidate retained for enrichment: %s", item.get("company_name", ""))
    parsed, _, failed_sources, successful_sources = apply_directory_fallbacks(
        client, parsed, source_name, sector, city, candidate_limit, disabled_sources or set(),
    )
    leads: list[dict[str, str]] = []
    rejected_no_mobile_email = 0
    candidates_enriched = 0
    seen: set[str] = set()
    for item in parsed[:candidate_limit]:
        if len(leads) >= accepted_limit:
            break
        source_key = "|".join((
            normalise_text(item.get("company_name", "")),
            normalise_phone(item.get("phone", "")),
            canonical_url(item.get("website", "")),
        ))
        if not directory_location_valid(item.get("location", ""), city) or source_key in seen:
            continue
        seen.add(source_key)
        candidates_enriched += 1
        if item.get("website"):
            logging.info("Website found: %s | %s", item.get("company_name", ""), item["website"])
        else:
            logging.info("Website not found: %s", item.get("company_name", ""))
        try:
            lead = client.enrich_business(item, sector, city)
        except SectorTimeout:
            lead = directory_lead_to_sheet(item, sector, city)
        if lead and lead.get("email"):
            logging.info("Email found: %s | %s", lead.get("company_name", ""), lead["email"])
        elif lead:
            logging.info("Email not found: %s", lead.get("company_name", ""))
        if not lead:
            continue
        lead = apply_phone_policy(lead, settings.allow_landlines)
        classification = lead.get("_phone_classification")
        accepted_contact = bool(
            lead.get("website") or lead.get("email") or lead.get("phone")
            or lead.get("whatsapp_confirmed") or lead.get("_landline_phone")
        )
        if accepted_contact:
            # Score only leads that passed the existing contact acceptance policy.
            # The score never changes whether a lead is accepted or rejected.
            buying_score = calculate_ai_buying_score(lead)
            lead["ai_buying_score"] = str(buying_score["score"])
            lead["priority"] = str(buying_score["priority"])
            lead["recommended_offer"] = str(buying_score["recommended_offer"])
            lead["score_breakdown"] = json.dumps(buying_score["breakdown"], ensure_ascii=False, sort_keys=True)
            leads.append(lead)
            reason = (
                "mobile" if classification == "mobile"
                else "public email" if lead.get("email")
                else "website" if lead.get("website")
                else "business phone"
            )
            logging.debug("Candidate name=%r accepted reason=%s", lead.get("company_name", ""), reason)
            logging.info("Lead accepted: %s | %s", lead.get("company_name", ""), reason)
            logging.info(
                "AI buying score: %s | score=%s | priority=%s | offer=%s",
                lead.get("company_name", ""), buying_score["score"],
                buying_score["priority"], buying_score["recommended_offer"],
            )
        else:
            rejected_no_mobile_email += 1
            logging.info("Lead rejected after all enrichment sources exhausted: %s", lead.get("company_name", ""))
            logging.info("Lead rejected: %s", lead.get("company_name", ""))
            logging.debug("Candidate name=%r rejected reason=no mobile or public email", lead.get("company_name", ""))

    def priority(lead: dict[str, str]) -> int:
        whatsapp = bool(lead.get("whatsapp_confirmed"))
        mobile = lead.get("_phone_classification") == "mobile"
        website = bool(lead.get("website"))
        email = bool(lead.get("email"))
        if whatsapp and website:
            return 5
        if mobile and website:
            return 4
        if email and website:
            return 3
        if mobile:
            return 2
        return 1 if email else 0

    leads.sort(key=priority, reverse=True)
    return (
        leads, rejected_no_mobile_email, candidates_discovered, candidates_enriched,
        failed_sources, successful_sources, client.text_search_requests,
        client.place_details_requests, client.overpass_requests,
        client.osm_businesses_discovered,
    )


def first_public_email(soup: BeautifulSoup, text: str) -> str:
    candidates = public_email_candidates(soup, text)
    return candidates[0] if candidates else ""


def cloudflare_email(value: str) -> str:
    """Decode Cloudflare's public data-cfemail value when it is present in HTML."""
    try:
        encoded = bytes.fromhex(value)
        if len(encoded) < 2:
            return ""
        key = encoded[0]
        return "".join(chr(byte ^ key) for byte in encoded[1:])
    except ValueError:
        return ""


def deobfuscate_public_email_text(text: str) -> str:
    text = html.unescape(text or "")
    text = re.sub(r"\s*(?:\[|\()\s*(?:at|arrobase)\s*(?:\]|\))\s*", "@", text, flags=re.I)
    text = re.sub(r"\s+(?:at|arrobase)\s+", "@", text, flags=re.I)
    text = re.sub(r"\s*(?:\[|\()\s*(?:dot|point)\s*(?:\]|\))\s*", ".", text, flags=re.I)
    text = re.sub(r"\s+(?:dot|point)\s+", ".", text, flags=re.I)
    return text


def moroccan_phone_matches(text: str) -> list[str]:
    return PHONE_RE.findall(str(text or "").translate(MOROCCAN_DIGIT_TRANSLATION))


def public_email_candidates(soup: BeautifulSoup, text: str) -> list[str]:
    candidates: list[str] = []
    for link in soup.select('a[href^="mailto:"]'):
        candidates.append(link.get("href", "")[7:].split("?", 1)[0])
    candidates.extend(cloudflare_email(node.get("data-cfemail", "")) for node in soup.select("[data-cfemail]"))
    candidates.extend(EMAIL_RE.findall(deobfuscate_public_email_text(text)))
    valid: list[str] = []
    rejected_domains = {"example.com", "sentry.io", "wixpress.com", "cloudflare.com"}
    for value in candidates:
        value = value.strip(" .,:;()[]<>'\"").lower()
        if not EMAIL_RE.fullmatch(value):
            continue
        if value.endswith((".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg")):
            continue
        domain = value.rsplit("@", 1)[-1]
        if domain in rejected_domains or value.startswith(("noreply@", "no-reply@")):
            continue
        if value not in valid:
            valid.append(value)
    return valid


def preferred_public_email(soup: BeautifulSoup, text: str, website: str) -> str:
    candidates = public_email_candidates(soup, text)
    website_host = (urlparse(website).hostname or "").lower().removeprefix("www.")
    for email in candidates:
        email_domain = email.rsplit("@", 1)[-1]
        if email_domain == website_host or email_domain.endswith("." + website_host):
            return email
    return candidates[0] if candidates else ""


def candidate_internal_pages(soup: BeautifulSoup, homepage_url: str) -> list[str]:
    fallback_paths = [
        "/contact", "/nous-contacter", "/contactez-nous", "/contact-us",
        "/fr/contact", "/a-propos", "/qui-sommes-nous", "/mentions-legales",
    ]
    contact_markers = (
        "contact", "nous contacter", "contactez", "coordonnees", "coordonnees",
        "whatsapp", "telephone", "tel", "email", "ecrire",
    )
    homepage_host = (urlparse(homepage_url).hostname or "").lower().removeprefix("www.")
    scored: dict[str, int] = {}
    for link in soup.select("a[href]"):
        url = urljoin(homepage_url, link.get("href", ""))
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower().removeprefix("www.")
        path = parsed.path.rstrip("/").lower() or "/"
        if parsed.scheme not in {"http", "https"} or host != homepage_host:
            continue
        if re.search(r"\.(?:pdf|jpg|jpeg|png|gif|webp|zip)(?:$|\?)", path, re.I):
            continue
        label = normalise_text(link.get_text(" ", strip=True))
        evidence = f"{label} {normalise_text(path)}"
        score = sum(marker in evidence for marker in contact_markers)
        if score:
            normalized = canonical_url(url)
            scored[normalized] = max(scored.get(normalized, 0), score)
    found = [url for url, _ in sorted(scored.items(), key=lambda item: (-item[1], item[0]))]
    for path in fallback_paths:
        url = canonical_url(urljoin(homepage_url, path))
        if url not in found:
            found.append(url)
    return found


def first_moroccan_phone(soup: BeautifulSoup, text: str) -> str:
    candidates = []
    for link in soup.select('a[href^="tel:"]'):
        candidates.append(link.get("href", "")[4:])
    candidates.extend(moroccan_phone_matches(text))
    return preferred_moroccan_phone(candidates)


def extract_company_name(soup: BeautifulSoup, search_title: str, url: str) -> str:
    for selector, attribute in (
        ('meta[property="og:site_name"]', "content"),
        ('meta[property="og:title"]', "content"),
        ("title", None),
        ("h1", None),
    ):
        node = soup.select_one(selector)
        value = node.get(attribute, "") if node and attribute else node.get_text(" ", strip=True) if node else ""
        value = clean_company_name(value)
        if value:
            return value
    fallback = search_title or (urlparse(url).hostname or "").split(".")[0]
    return clean_company_name(fallback)


def clean_company_name(value: str) -> str:
    value = clean_text(value, 150)
    value = re.split(r"\s+[|–—]\s+|\s+-\s+", value, maxsplit=1)[0].strip()
    if normalise_text(value) in {"accueil", "home", "bienvenue"} or len(value) < 2:
        return ""
    return value


def extract_description(soup: BeautifulSoup, fallback: str, sector: str, city: str) -> str:
    node = soup.select_one('meta[name="description"], meta[property="og:description"]')
    description = node.get("content", "") if node else fallback
    description = clean_text(description, 450)
    return description or f"{sector.capitalize()} basée à {city}."


def automation_opportunity(sector: str) -> str:
    key = normalise_text(sector)
    if any(word in key for word in ("clinique", "medical", "dentaire", "radiologie", "laboratoire", "veterinaire")):
        return "Automatiser les prises de rendez-vous, rappels patients et demandes de suivi."
    if any(word in key for word in ("hotel", "riad", "hote", "restaurant", "cafe", "traiteur", "voyage")):
        return "Automatiser les réservations, confirmations, avis clients et relances WhatsApp."
    if any(word in key for word in ("ecole", "formation", "creche", "auto ecole")):
        return "Automatiser les demandes d’inscription, le suivi des prospects et les rappels."
    if any(word in key for word in ("agence", "immobili", "avocat", "comptable", "architect", "etudes")):
        return "Automatiser la qualification des demandes, la prise de rendez-vous et les relances CRM."
    if any(word in key for word in ("boutique", "magasin", "bijouter", "opticien", "grossiste", "distributeur", "supermarche")):
        return "Automatiser les demandes produits, le suivi des commandes et la fidélisation client."
    return "Automatiser la qualification des prospects, les devis, rendez-vous et relances clients."


def recommended_ai_offer(sector: str) -> str:
    """Choose one deterministic BKZ offer from the lead's business sector."""
    key = normalise_text(sector)
    for keywords, offer in AI_RECOMMENDED_OFFER_RULES:
        if any(word in key for word in keywords):
            return offer
    return AI_RECOMMENDED_OFFER_DEFAULT


def calculate_ai_buying_score(lead: dict[str, str]) -> dict[str, object]:
    """Return an explainable, deterministic 0-100 buying score for one accepted lead.

    The supplied component maxima add to 98.  Their relative weights are retained and
    the base is normalized to 100 before applying explicit risk penalties.
    """
    sector = normalise_text(lead.get("industry", ""))
    description = normalise_text(" ".join((
        lead.get("company_name", ""),
        lead.get("business_description", ""),
        lead.get("automation_opportunity", ""),
    )))
    website = canonical_website_url(lead.get("website", ""))
    email = str(lead.get("email", "")).strip().casefold()
    phone_classification = str(lead.get("_phone_classification", ""))
    whatsapp = bool(lead.get("whatsapp_confirmed"))
    website_host = (urlparse(website).hostname or "").lower().removeprefix("www.")
    email_domain = email.rsplit("@", 1)[-1] if "@" in email else ""
    social_presence = any(bool(lead.get(field)) for field in AI_BUYING_SCORE_SOCIAL_FIELDS)

    if any(term in sector for term in AI_BUYING_SCORE_HIGH_FIT_TERMS):
        sector_fit, sector_reason = AI_BUYING_SCORE_WEIGHTS["sector_fit"]["high"], "sector has a high-volume automation use case"
    elif any(term in sector for term in AI_BUYING_SCORE_MEDIUM_FIT_TERMS):
        sector_fit, sector_reason = AI_BUYING_SCORE_WEIGHTS["sector_fit"]["medium"], "sector has a practical automation use case"
    else:
        sector_fit, sector_reason = AI_BUYING_SCORE_WEIGHTS["sector_fit"]["general"], "sector fit is general rather than vertical-specific"

    digital_maturity = 0
    digital_reasons: list[str] = []
    if website:
        digital_maturity += AI_BUYING_SCORE_WEIGHTS["digital_maturity"]["website"]
        digital_reasons.append("official website")
    if email and website_host and (email_domain == website_host or email_domain.endswith("." + website_host)):
        digital_maturity += AI_BUYING_SCORE_WEIGHTS["digital_maturity"]["domain_email"]
        digital_reasons.append("domain-matched business email")
    if social_presence:
        digital_maturity += AI_BUYING_SCORE_WEIGHTS["digital_maturity"]["social"]
        digital_reasons.append("public business social profile")

    if any(term in description for term in AI_BUYING_SCORE_SCALE_TERMS):
        company_size, company_size_reason = AI_BUYING_SCORE_WEIGHTS["company_size_proxy"]["scale_signal"], "multi-site, team, or group signal found"
    elif website and email and phone_classification == "mobile":
        company_size, company_size_reason = AI_BUYING_SCORE_WEIGHTS["company_size_proxy"]["complete_contact"], "complete public contact footprint"
    elif website or email:
        company_size, company_size_reason = AI_BUYING_SCORE_WEIGHTS["company_size_proxy"]["basic_footprint"], "basic public business footprint"
    else:
        company_size, company_size_reason = 0, "no size proxy available"

    if any(term in description for term in AI_BUYING_SCORE_OPERATIONAL_TERMS):
        operational_pain, operational_reason = AI_BUYING_SCORE_WEIGHTS["operational_pain"]["workflow_signal"], "booking, order, quote, or appointment workflow detected"
    elif any(term in sector for term in AI_BUYING_SCORE_HIGH_FIT_TERMS):
        operational_pain, operational_reason = AI_BUYING_SCORE_WEIGHTS["operational_pain"]["high_fit_sector"], "sector commonly has repetitive customer workflows"
    else:
        operational_pain, operational_reason = AI_BUYING_SCORE_WEIGHTS["operational_pain"]["general"], "general follow-up opportunity"

    if any(term in description for term in AI_BUYING_SCORE_OPERATIONAL_TERMS) and website:
        buying_intent, buying_intent_reason = AI_BUYING_SCORE_WEIGHTS["buying_intent"]["workflow_online"], "active service workflow is visible online"
    elif website and (email or phone_classification == "mobile" or whatsapp):
        buying_intent, buying_intent_reason = AI_BUYING_SCORE_WEIGHTS["buying_intent"]["digital_contact"], "active digital contact path is available"
    elif website or email:
        buying_intent, buying_intent_reason = AI_BUYING_SCORE_WEIGHTS["buying_intent"]["limited"], "limited public buying-intent evidence"
    else:
        buying_intent, buying_intent_reason = 0, "no public buying-intent evidence"

    if whatsapp:
        reachability, reachability_reason = AI_BUYING_SCORE_WEIGHTS["reachability"]["whatsapp"], "explicit WhatsApp contact"
    elif phone_classification == "mobile":
        reachability, reachability_reason = AI_BUYING_SCORE_WEIGHTS["reachability"]["mobile"], "public Moroccan mobile"
    elif email:
        reachability, reachability_reason = AI_BUYING_SCORE_WEIGHTS["reachability"]["email"], "public email contact"
    elif phone_classification == "landline":
        reachability, reachability_reason = AI_BUYING_SCORE_WEIGHTS["reachability"]["landline"], "landline only"
    else:
        reachability, reachability_reason = 0, "no reachable contact"

    business_email = AI_BUYING_SCORE_WEIGHTS["business_email"]["present"] if email else 0
    business_email_reason = "public business email" if email else "no public business email"
    email_local_part = email.split("@", 1)[0] if "@" in email else ""
    if any(term in f"{email_local_part} {description}" for term in AI_BUYING_SCORE_DECISION_MAKER_TERMS):
        decision_maker, decision_maker_reason = AI_BUYING_SCORE_WEIGHTS["decision_maker"]["signal"], "decision-maker signal in public contact data"
    else:
        decision_maker, decision_maker_reason = 0, "no decision-maker signal available"
    if any(term in description for term in AI_BUYING_SCORE_GROWTH_TERMS):
        growth_signals, growth_reason = AI_BUYING_SCORE_WEIGHTS["growth_signals"]["signal"], "growth or active-service signal found"
    elif website and (email or phone_classification == "mobile"):
        growth_signals, growth_reason = AI_BUYING_SCORE_WEIGHTS["growth_signals"]["digital_footprint"], "active digital business footprint"
    else:
        growth_signals, growth_reason = 0, "no public growth signal"

    components = {
        "sector_fit": {"points": sector_fit, "max": AI_BUYING_SCORE_WEIGHTS["sector_fit"]["max"], "reason": sector_reason},
        "digital_maturity": {"points": digital_maturity, "max": AI_BUYING_SCORE_WEIGHTS["digital_maturity"]["max"], "reason": ", ".join(digital_reasons) or "no digital maturity signal"},
        "company_size_proxy": {"points": company_size, "max": AI_BUYING_SCORE_WEIGHTS["company_size_proxy"]["max"], "reason": company_size_reason},
        "operational_pain": {"points": operational_pain, "max": AI_BUYING_SCORE_WEIGHTS["operational_pain"]["max"], "reason": operational_reason},
        "buying_intent": {"points": buying_intent, "max": AI_BUYING_SCORE_WEIGHTS["buying_intent"]["max"], "reason": buying_intent_reason},
        "reachability": {"points": reachability, "max": AI_BUYING_SCORE_WEIGHTS["reachability"]["max"], "reason": reachability_reason},
        "business_email": {"points": business_email, "max": AI_BUYING_SCORE_WEIGHTS["business_email"]["max"], "reason": business_email_reason},
        "decision_maker": {"points": decision_maker, "max": AI_BUYING_SCORE_WEIGHTS["decision_maker"]["max"], "reason": decision_maker_reason},
        "growth_signals": {"points": growth_signals, "max": AI_BUYING_SCORE_WEIGHTS["growth_signals"]["max"], "reason": growth_reason},
    }
    raw_total = sum(int(component["points"]) for component in components.values())
    normalized_total = round(raw_total * AI_BUYING_SCORE_LIMITS["maximum"] / AI_BUYING_SCORE_BASE_MAX)
    penalties: dict[str, int] = {}
    if not website:
        penalties["no_website"] = AI_BUYING_SCORE_PENALTIES["no_website"]
    if phone_classification == "landline":
        penalties["landline_only"] = AI_BUYING_SCORE_PENALTIES["landline_only"]
    if any(term in description for term in AI_BUYING_SCORE_INACTIVE_TERMS):
        penalties["inactive_company"] = AI_BUYING_SCORE_PENALTIES["inactive_company"]
    penalty_total = sum(penalties.values())
    score = max(AI_BUYING_SCORE_LIMITS["minimum"], min(AI_BUYING_SCORE_LIMITS["maximum"], normalized_total + penalty_total))
    priority = next((label for threshold, label in AI_BUYING_SCORE_PRIORITY_THRESHOLDS if score >= threshold), "D")
    reasons = [
        f"{name.replace('_', ' ')}: {component['points']}/{component['max']} ({component['reason']})"
        for name, component in components.items() if component["points"]
    ]
    reasons.extend(f"penalty {name.replace('_', ' ')}: {points}" for name, points in penalties.items())
    return {
        "score": score,
        "priority": priority,
        "recommended_offer": recommended_ai_offer(lead.get("industry", "")),
        "breakdown": {
            "components": components,
            "raw_total": raw_total,
            "normalized_total": normalized_total,
            "penalties": penalties,
            "penalty_total": penalty_total,
        },
        "reasons": reasons,
    }


def make_lead_id(name: str, phone: str, website: str) -> str:
    raw = "|".join((normalise_text(name), normalise_phone(phone), canonical_url(website)))
    return "lead_" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def identity_keys(lead: dict[str, str]) -> set[str]:
    keys = set()
    if lead.get("lead_id", "").strip():
        keys.add("lead_id:" + lead["lead_id"].strip().casefold())
    if lead.get("_google_place_id", "").strip():
        keys.add("place_id:" + lead["_google_place_id"].strip())
    if lead.get("_osm_identity", "").strip():
        keys.add("osm:" + lead["_osm_identity"].strip().casefold())
    if normalise_text(lead.get("company_name", "")):
        keys.add("name:" + normalise_text(lead["company_name"]))
        if normalise_text(lead.get("location", "")):
            keys.add(
                "name_city:" + normalise_text(lead["company_name"]) + "|"
                + normalise_text(lead["location"].split(",", 1)[0])
            )
    if normalise_phone(lead.get("phone", "")):
        keys.add("phone:" + normalise_phone(lead["phone"]))
    if canonical_url(lead.get("website", "")):
        keys.add("web:" + canonical_website_url(lead["website"]))
    if lead.get("email", "").strip():
        keys.add("email:" + lead["email"].strip().casefold())
    return keys


def sheets_worksheet(settings: Settings) -> gspread.Worksheet:
    credentials = Credentials.from_service_account_file(
        settings.credentials_file,
        scopes=["https://www.googleapis.com/auth/spreadsheets"],
    )
    return gspread.authorize(credentials).open_by_key(settings.spreadsheet_id).worksheet(settings.worksheet_name)


def scheduler_state_worksheet(lead_worksheet: gspread.Worksheet) -> gspread.Worksheet:
    spreadsheet = lead_worksheet.spreadsheet
    try:
        return spreadsheet.worksheet(SCHEDULER_WORKSHEET)
    except gspread.WorksheetNotFound:
        state = spreadsheet.add_worksheet(title=SCHEDULER_WORKSHEET, rows=8, cols=2)
        state.update([["key", "value"]], range_name="A1:B1", value_input_option="RAW")
        logging.info("Created persistent scheduler state worksheet: %s", SCHEDULER_WORKSHEET)
        return state


def load_scheduler_cursor(lead_worksheet: gspread.Worksheet) -> SchedulerCursor:
    state = scheduler_state_worksheet(lead_worksheet)
    values = state.get_all_values()
    stored = {row[0]: row[1] for row in values[1:] if len(row) >= 2 and row[0]}
    try:
        return SchedulerCursor(
            city_index=int(stored.get("city_index", 0)),
            sector_index=int(stored.get("sector_index", 0)),
            provider_index=int(stored.get("provider_index", 0)),
        )
    except ValueError:
        logging.warning("Persistent scheduler state is malformed; resetting to start")
        return SchedulerCursor()


def save_scheduler_cursor(lead_worksheet: gspread.Worksheet, cursor: SchedulerCursor) -> None:
    rows = [
        ["key", "value"],
        ["city_index", str(cursor.city_index)],
        ["sector_index", str(cursor.sector_index)],
        ["provider_index", str(cursor.provider_index)],
        ["updated_at_utc", datetime.now(timezone.utc).isoformat()],
    ]
    scheduler_state_worksheet(lead_worksheet).update(
        rows, range_name="A1:B5", value_input_option="RAW"
    )


def normalise_sheet_header(value: str) -> str:
    return (value or "").strip().casefold()


def ensure_export_columns(worksheet: gspread.Worksheet) -> list[str]:
    """Append collector-managed export columns when an existing sheet lacks them."""
    headers = worksheet.row_values(1)
    present = {normalise_sheet_header(header) for header in headers if normalise_sheet_header(header)}
    missing = [column for column in AI_BUYING_SCORE_COLUMNS + CONTACT_COLUMNS if column not in present]
    if not missing:
        return headers
    start_column = len(headers) + 1
    end_column = start_column + len(missing) - 1
    target_range = f"{excel_column_name(start_column)}1:{excel_column_name(end_column)}1"
    worksheet.update([missing], range_name=target_range, value_input_option="RAW")
    headers.extend(missing)
    logging.info("Added collector export columns: %s", ", ".join(missing))
    return headers


def read_existing(worksheet: gspread.Worksheet) -> tuple[list[dict[str, str]], list[str]]:
    values = worksheet.get_all_values()
    worksheet_headers = values[0] if values else []
    logging.info("Actual worksheet headers: %r", worksheet_headers)
    header_positions: dict[str, int] = {}
    for index, header in enumerate(worksheet_headers):
        normalized = normalise_sheet_header(header)
        if normalized and normalized not in header_positions:
            header_positions[normalized] = index

    missing = [column for column in LEAD_EXPORT_COLUMNS if column not in header_positions]
    if missing:
        logging.error("Required worksheet headers missing: %s", ", ".join(missing))
        raise ValueError(
            "Required worksheet headers missing: " + ", ".join(missing) + ". No rows were changed."
        )

    existing: list[dict[str, str]] = []
    for row in values[1:]:
        existing.append({
            column: row[header_positions[column]] if header_positions[column] < len(row) else ""
            for column in SHEET_COLUMNS
        })
    return existing, worksheet_headers


def write_new_leads(
    worksheet: gspread.Worksheet,
    leads: list[dict[str, str]],
    worksheet_headers: list[str],
) -> None:
    if not leads:
        return
    header_positions: dict[str, int] = {}
    for index, header in enumerate(worksheet_headers):
        normalized = normalise_sheet_header(header)
        if normalized and normalized not in header_positions:
            header_positions[normalized] = index
    missing = [column for column in SHEET_COLUMNS if column not in header_positions]
    if missing:
        raise ValueError(
            "Required worksheet headers missing: " + ", ".join(missing) + ". No rows were written."
        )

    defaults = {
        "score": "",
        "contact_status": "Not Contacted",
        "personalised_message": "",
        "sent_at": "",
    }
    rows: list[list[str]] = []
    for lead in leads:
        row = [""] * len(worksheet_headers)
        for column in LEAD_EXPORT_COLUMNS:
            # The normal collection path has already migrated the header.  Keeping
            # this guard preserves older direct callers and repair/test utilities.
            if column in header_positions:
                row[header_positions[column]] = str(lead.get(column, defaults.get(column, "")))
        rows.append(row)
    if any(len(row) != len(worksheet_headers) for row in rows):
        raise RuntimeError("Generated lead row width does not match worksheet headers; no rows were written")

    column_a = worksheet.col_values(1)
    last_non_empty_row = max(
        (index for index, value in enumerate(column_a, start=1) if str(value).strip()),
        default=0,
    )
    next_row = last_non_empty_row + 1
    end_row = next_row + len(rows) - 1
    target_range = f"A{next_row}:{excel_column_name(len(worksheet_headers))}{end_row}"
    logging.info("Google Sheets next_row=%d", next_row)
    logging.info("Google Sheets target range=%s", target_range)
    logging.info("Google Sheets first row preview=%r", rows[0])
    worksheet.update(rows, range_name=target_range, value_input_option="RAW")


def excel_column_name(column_number: int) -> str:
    if column_number < 1:
        raise ValueError("Column number must be positive")
    name = ""
    while column_number:
        column_number, remainder = divmod(column_number - 1, 26)
        name = chr(65 + remainder) + name
    return name


def repair_misaligned_rows(
    settings: Settings,
    start_row: int,
    end_row: int,
    confirm: bool,
) -> int:
    if (start_row, end_row) != (16, 20):
        raise ValueError("The repair helper is restricted to rows 16 through 20")
    worksheet = sheets_worksheet(settings)
    headers = worksheet.row_values(1)
    logging.info("Actual worksheet headers: %r", headers)
    normalized_headers = [normalise_sheet_header(header) for header in headers]
    missing = [column for column in SHEET_COLUMNS if column not in normalized_headers]
    if missing:
        logging.error("Required worksheet headers missing: %s", ", ".join(missing))
        raise ValueError("Required worksheet headers missing: " + ", ".join(missing))

    website_index = normalized_headers.index("website")
    description_index = normalized_headers.index("business_description")
    opportunity_index = normalized_headers.index("automation_opportunity")
    blank_indexes = [index for index, header in enumerate(normalized_headers) if not header]
    blank_before_website = [index for index in blank_indexes if index < website_index]
    if not blank_before_website:
        raise ValueError("No blank header column exists before website; repair aborted")
    blank_index = max(blank_before_website)
    if [blank_index + 1, blank_index + 2, blank_index + 3] != [
        website_index, description_index, opportunity_index,
    ]:
        raise ValueError(
            "The blank/website/business_description/automation_opportunity columns are not adjacent as expected; repair aborted"
        )

    end_column = excel_column_name(len(headers))
    range_name = f"A{start_row}:{end_column}{end_row}"
    fetched_rows = worksheet.get(range_name)
    original_rows = [list(row) + [""] * (len(headers) - len(row)) for row in fetched_rows]
    while len(original_rows) < end_row - start_row + 1:
        original_rows.append([""] * len(headers))
    original_rows = [row[:len(headers)] for row in original_rows]

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup_path = Path.cwd() / f"leads_rows_{start_row}_{end_row}_backup_{timestamp}.json"
    backup_payload = {
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "spreadsheet_id": settings.spreadsheet_id,
        "worksheet": settings.worksheet_name,
        "range": range_name,
        "headers": headers,
        "rows": original_rows,
    }
    backup_path.write_text(json.dumps(backup_payload, ensure_ascii=False, indent=2), encoding="utf-8")
    logging.info("Repair backup created: %s", backup_path)

    repaired_rows: list[list[str]] = []
    changed_rows: list[int] = []
    for offset, original in enumerate(original_rows):
        row_number = start_row + offset
        repaired = list(original)
        shift_detected = (
            not original[opportunity_index].strip()
            and any(original[index].strip() for index in range(blank_index, opportunity_index))
        )
        if shift_detected:
            for index in range(opportunity_index, blank_index, -1):
                repaired[index] = original[index - 1]
            repaired[blank_index] = ""
            changed_rows.append(row_number)
        repaired_rows.append(repaired)
        preview = {
            "blank_column": {"before": original[blank_index], "after": repaired[blank_index]},
            "website": {"before": original[website_index], "after": repaired[website_index]},
            "business_description": {"before": original[description_index], "after": repaired[description_index]},
            "automation_opportunity": {"before": original[opportunity_index], "after": repaired[opportunity_index]},
        }
        logging.info("Repair preview row %d: %s", row_number, json.dumps(preview, ensure_ascii=False))

    if not changed_rows:
        logging.info("No shifted rows detected in rows %d to %d; nothing was changed", start_row, end_row)
        return 0
    if not confirm:
        logging.info(
            "Dry-run repair preview only. Detected rows: %s. Re-run with --confirm-repair to write changes.",
            ", ".join(map(str, changed_rows)),
        )
        return 0

    if any(len(row) != len(headers) for row in repaired_rows):
        raise RuntimeError("Repair row width mismatch; no rows were written")
    worksheet.update(repaired_rows, range_name=range_name, value_input_option="RAW")
    logging.info("Repair completed for rows: %s", ", ".join(map(str, changed_rows)))
    return 0


def cleanup_outside_main_range(settings: Settings, confirm: bool) -> int:
    """Find and optionally clear misplaced 20-cell lead blocks after column T."""
    worksheet = sheets_worksheet(settings)
    values = worksheet.get_all_values()
    detected: list[dict[str, object]] = []
    for row_number, row in enumerate(values, start=1):
        column_a = row[0].strip() if row else ""
        if column_a:
            continue
        index = 20  # zero-based column U; A:T must never be inspected as cleanup targets
        while index < len(row):
            value = str(row[index]).strip()
            if value.startswith("lead_"):
                start_column = index + 1
                end_column = start_column + 19
                range_name = (
                    f"{excel_column_name(start_column)}{row_number}:"
                    f"{excel_column_name(end_column)}{row_number}"
                )
                block = list(row[index:index + 20])
                block.extend([""] * (20 - len(block)))
                detected.append({
                    "row": row_number,
                    "range": range_name,
                    "values": block,
                })
                index += 20
            else:
                index += 1

    if not detected:
        logging.info("No misplaced lead blocks were found after column T; nothing was changed")
        return 0

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup_path = Path.cwd() / f"leads_outside_A_T_backup_{timestamp}.json"
    backup_payload = {
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "spreadsheet_id": settings.spreadsheet_id,
        "worksheet": settings.worksheet_name,
        "detected_blocks": detected,
    }
    backup_path.write_text(json.dumps(backup_payload, ensure_ascii=False, indent=2), encoding="utf-8")
    logging.info("Outside-range cleanup backup created: %s", backup_path)
    for block in detected:
        logging.info(
            "Cleanup preview: row=%s range=%s values=%r",
            block["row"], block["range"], block["values"],
        )

    ranges = [str(block["range"]) for block in detected]
    if not confirm:
        logging.info(
            "Dry-run cleanup preview only. Re-run with --confirm-cleanup to clear: %s",
            ", ".join(ranges),
        )
        return 0

    if any(range_name.split(":", 1)[0].rstrip("0123456789") in {
        excel_column_name(column) for column in range(1, 21)
    } for range_name in ranges):
        raise RuntimeError("Cleanup safety check detected a target in A:T; no cells were cleared")
    worksheet.batch_clear(ranges)
    logging.info("Cleared misplaced cells after column T only: %s", ", ".join(ranges))
    return 0


def collect(settings: Settings) -> int:
    global OVERPASS_RUN_REQUESTS
    if "google_places" in settings.sources and not settings.google_maps_api_key:
        raise ValueError(
            "GOOGLE_MAPS_API_KEY is missing. Add GOOGLE_MAPS_API_KEY to .env before using google_places."
        )
    if "osm_overpass" in settings.sources:
        OVERPASS_AREA_CACHE.clear()
        OVERPASS_RUN_REQUESTS = 0
    run_limit = min(settings.target_leads, settings.max_total)
    limit_stop_reason = (
        "target leads reached" if settings.target_leads <= settings.max_total
        else "maximum total leads reached"
    )
    logging.info("Configured cities: %s", ", ".join(settings.cities))
    logging.info("Configured sectors: %d", len(settings.sectors))
    logging.info("Configured sources: %s", ", ".join(settings.sources))
    logging.info("Fast mode: %s", "enabled" if settings.fast_mode else "disabled")
    logging.info("Target leads: %d", run_limit)
    logging.info("Leads remaining: %d", run_limit)
    worksheet = None
    persisted_cursor = SchedulerCursor()
    existing: list[dict[str, str]] = []
    worksheet_headers: list[str] = []
    if not settings.dry_run:
        try:
            worksheet = sheets_worksheet(settings)
            ensure_export_columns(worksheet)
            existing, worksheet_headers = read_existing(worksheet)
            persisted_cursor = load_scheduler_cursor(worksheet)
            logging.info("Read %d existing leads from worksheet %s", len(existing), settings.worksheet_name)
        except Exception as exc:
            raise RuntimeError(f"Google Sheets setup/read failed; no changes made: {exc}") from exc
    else:
        logging.info("Dry run enabled: Google Sheets will not be read or changed")

    known = set().union(*(identity_keys(lead) for lead in existing)) if existing else set()
    new_leads: list[dict[str, str]] = []
    searches = found = duplicates = skipped_empty_sectors = 0
    rejected_no_mobile_email = 0
    candidates_discovered_total = 0
    candidates_enriched_total = 0
    text_search_requests_total = 0
    place_details_requests_total = 0
    overpass_requests_total = 0
    osm_businesses_discovered_total = 0
    source_consecutive_failures = {source: 0 for source in settings.sources}
    circuit_open: set[str] = set()
    statistics = RunStatistics.create()
    search_plan = round_robin_search_plan(settings)
    required_searches = len(search_plan)
    search_budget = settings.max_searches if settings.max_searches is not None else required_searches
    start_position = cursor_position(settings, search_plan, persisted_cursor)
    next_position = start_position
    schedule_position = -1
    logging.info("Search budget this run: %d | full plan size: %d", search_budget, required_searches)
    logging.info(
        "Current cursor: city_index=%d sector_index=%d provider_index=%d | plan_position=%d",
        persisted_cursor.city_index, persisted_cursor.sector_index,
        persisted_cursor.provider_index, start_position,
    )

    stop = False
    stop_reason = "configured cities and sectors exhausted"
    logging.info("Round-robin schedule: deterministic full city/sector/provider coverage")
    for sector_index, sector in enumerate(settings.sectors):
        scheduled_cities = settings.cities[sector_index % len(settings.cities):] + settings.cities[:sector_index % len(settings.cities)]
        for city_index, city in enumerate(scheduled_cities):
            if (
                len(new_leads) >= run_limit
                or searches >= search_budget
            ):
                stop = True
                stop_reason = (
                    limit_stop_reason if len(new_leads) >= run_limit
                    else "run search budget exhausted; cursor saved"
                )
                break
            logging.info("City scheduled: %s", city)
            logging.info("Sector started: %s | City: %s", sector, city)
            sector_started = time.monotonic()
            sector_timed_out = False
            results: list[dict[str, str]] = []
            provider_offset = (sector_index + city_index) % len(settings.sources)
            sector_sources = settings.sources[provider_offset:] + settings.sources[:provider_offset]
            for source_name in sector_sources:
                schedule_position += 1
                if schedule_position < start_position:
                    continue
                if searches >= search_budget:
                    stop = True
                    stop_reason = "run search budget exhausted; cursor saved"
                    next_position = schedule_position
                    break
                if source_name in circuit_open:
                    logging.info("Source circuit breaker open; skipped for rest of run: %s", source_name)
                    next_position = schedule_position + 1
                    continue
                leads_needed = run_limit - len(new_leads)
                if source_name == "google_places":
                    detail_budget = settings.max_place_details - place_details_requests_total
                    remaining = min(20, leads_needed, detail_budget)
                else:
                    remaining = min(settings.max_results - len(results), leads_needed)
                if remaining <= 0:
                    if source_name == "google_places":
                        stop_reason = "maximum Place Details requests reached"
                        stop = True
                    break
                searches += 1
                next_position = schedule_position + 1
                statistics.record_search(SearchTask(source_name, city, sector))
                logging.info("Source used: %s | Sector: %s | City: %s", source_name, sector, city)
                logging.info(
                    "Search budget after iteration %d: remaining=%d | provider=%s | city=%s | sector=%s",
                    searches, search_budget - searches, source_name, city, sector,
                )
                source_timeout_failure = False
                pipeline_rejected = 0
                pipeline_discovered = 0
                pipeline_enriched = 0
                pipeline_failed_sources: set[str] = set()
                pipeline_successful_sources: set[str] = set()
                pipeline_text_requests = 0
                pipeline_detail_requests = 0
                pipeline_overpass_requests = 0
                pipeline_osm_discovered = 0
                executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
                future = executor.submit(
                    execute_source_pipeline, settings, source_name, sector, city, remaining,
                    # Every provider has its own globally-budgeted schedule entry.
                    # Do not make hidden, uncounted provider calls during enrichment.
                    set(settings.sources),
                )
                try:
                    if settings.fast_mode:
                        (
                            parsed, pipeline_rejected, pipeline_discovered, pipeline_enriched,
                            pipeline_failed_sources, pipeline_successful_sources,
                            pipeline_text_requests, pipeline_detail_requests,
                            pipeline_overpass_requests, pipeline_osm_discovered,
                        # Fast mode has separate nine-second discovery and enrichment
                        # windows, keeping 50 searches inside the Actions run limit.
                        ) = future.result(timeout=20.0)
                    else:
                        (
                            parsed, pipeline_rejected, pipeline_discovered, pipeline_enriched,
                            pipeline_failed_sources, pipeline_successful_sources,
                            pipeline_text_requests, pipeline_detail_requests,
                            pipeline_overpass_requests, pipeline_osm_discovered,
                        ) = future.result(timeout=max(0.1, 20.0 - (time.monotonic() - sector_started)))
                except concurrent.futures.TimeoutError:
                    future.cancel()
                    sector_timed_out = True
                    parsed = []
                    source_timeout_failure = True
                    logging.warning("Search hard timeout reached after 20 seconds")
                except SectorTimeout as exc:
                    sector_timed_out = True
                    parsed = []
                    source_timeout_failure = True
                    logging.warning("Sector hard timeout reached after 20 seconds: %s", exc)
                except SourceAccessError as exc:
                    parsed = []
                    source_timeout_failure = True
                    pipeline_overpass_requests = getattr(exc, "overpass_requests", 0)
                    logging.warning("Source access failure: %s: %s", source_name, exc)
                except Exception as exc:
                    logging.warning("Source skipped safely: %s (%s)", source_name, exc)
                    parsed = []
                    source_timeout_failure = "timeout" in str(exc).casefold() or "timed out" in str(exc).casefold()
                finally:
                    executor.shutdown(wait=False, cancel_futures=True)
                rejected_no_mobile_email += pipeline_rejected
                candidates_discovered_total += pipeline_discovered
                candidates_enriched_total += pipeline_enriched
                statistics.candidates_rejected += pipeline_rejected
                statistics.candidates_discovered += pipeline_discovered
                statistics.candidates_enriched += pipeline_enriched
                text_search_requests_total += pipeline_text_requests
                place_details_requests_total += pipeline_detail_requests
                overpass_requests_total += pipeline_overpass_requests
                osm_businesses_discovered_total += pipeline_osm_discovered
                if source_timeout_failure:
                    source_consecutive_failures[source_name] += 1
                else:
                    source_consecutive_failures[source_name] = 0
                for failed_source in pipeline_failed_sources:
                    source_consecutive_failures[failed_source] = source_consecutive_failures.get(failed_source, 0) + 1
                for successful_source in pipeline_successful_sources:
                    source_consecutive_failures[successful_source] = 0
                for checked_source, failures in source_consecutive_failures.items():
                    if failures >= 2 and checked_source not in circuit_open:
                        circuit_open.add(checked_source)
                        logging.warning(
                            "Source circuit breaker opened after %d real access failures: %s",
                            failures, checked_source,
                        )
                if source_consecutive_failures[source_name] >= 2:
                    circuit_open.add(source_name)
                if sector_timed_out:
                    break
                results.extend(parsed[:remaining])
                logging.info("Source parsed: %s | businesses=%d", source_name, len(parsed[:remaining]))
            logging.info("Source discovery completed: %s / %s (%d businesses)", sector, city, len(results))
            if (sector_timed_out or time.monotonic() - sector_started >= 20.0) and not results:
                skipped_empty_sectors += 1
                logging.info("Sector exceeded 20 seconds and was skipped: %s | City: %s", sector, city)
                logging.info("Sector duration: %.2f seconds | %s | %s", time.monotonic() - sector_started, sector, city)
                continue
            if not results:
                skipped_empty_sectors += 1
                logging.info("Skipped empty sector: %s | City: %s", sector, city)
                logging.info("Sector duration: %.2f seconds | %s | %s", time.monotonic() - sector_started, sector, city)
                if stop:
                    break
                continue
            results.sort(key=lambda item: bool(item.get("phone") or item.get("email")), reverse=True)
            for lead in results:
                if len(new_leads) >= run_limit:
                    stop = True
                    stop_reason = limit_stop_reason
                    break
                found += 1
                keys = identity_keys(lead)
                if keys & known:
                    duplicates += 1
                    statistics.duplicates_skipped += 1
                    logging.info("Duplicate skipped: %s", lead.get("company_name", ""))
                    continue
                known.update(keys)
                new_leads.append(lead)
                logging.info("Lead found: %s (%s)", lead["company_name"], city)
                logging.info("Leads remaining: %d", run_limit - len(new_leads))
                if len(new_leads) >= run_limit:
                    stop = True
                    stop_reason = limit_stop_reason
                    logging.info("Target reached: %d leads", run_limit)
                    break
            logging.info("Sector duration: %.2f seconds | %s | %s", time.monotonic() - sector_started, sector, city)
        logging.info("Sector cycle processed: %s", sector)
        if stop:
            break

    if next_position >= required_searches:
        next_position = 0
        if not stop:
            stop_reason = "full search plan completed; cursor wrapped to beginning"
    next_cursor = cursor_for_task(settings, search_plan[next_position])

    if worksheet is not None and new_leads:
        try:
            write_new_leads(worksheet, new_leads, worksheet_headers)
        except Exception as exc:
            raise RuntimeError(f"Google Sheets explicit A:T write failed: {exc}") from exc
    if worksheet is not None:
        try:
            save_scheduler_cursor(worksheet, next_cursor)
        except Exception as exc:
            raise RuntimeError(f"Scheduler cursor save failed: {exc}") from exc
    logging.info("Searches completed this run: %d", searches)
    logging.info("Remaining searches in current plan cycle: %d", (required_searches - next_position) % required_searches)
    logging.info(
        "Next cursor position: city_index=%d sector_index=%d provider_index=%d | plan_position=%d",
        next_cursor.city_index, next_cursor.sector_index, next_cursor.provider_index, next_position,
    )
    logging.info("Run summary: source searches completed=%d, leads found=%d, duplicates removed=%d, leads appended=%d",
                 searches, found, duplicates, 0 if settings.dry_run else len(new_leads))
    logging.info("Searches per provider: %s", json.dumps(dict(sorted(statistics.searches_by_provider.items())), sort_keys=True))
    logging.info("Searches per city: %s", json.dumps(dict(sorted(statistics.searches_by_city.items())), sort_keys=True))
    logging.info("Searches per sector: %s", json.dumps(dict(sorted(statistics.searches_by_sector.items())), sort_keys=True))
    logging.info("Search budget remaining: %d", search_budget - searches)
    logging.info("Text Search requests: %d", text_search_requests_total)
    logging.info("Place Details requests: %d", place_details_requests_total)
    overpass_requests_total = max(overpass_requests_total, OVERPASS_RUN_REQUESTS)
    logging.info("Overpass requests: %d", overpass_requests_total)
    logging.info("OSM businesses discovered: %d", osm_businesses_discovered_total)
    logging.info("Places discovered: %d", candidates_discovered_total)
    logging.info("Candidates discovered: %d", candidates_discovered_total)
    logging.info("Candidates enriched: %d", candidates_enriched_total)
    leads_with_website = sum(bool(lead.get("website")) for lead in new_leads)
    leads_with_email = sum(bool(lead.get("email")) for lead in new_leads)
    leads_with_mobile = sum(lead.get("_phone_classification") == "mobile" for lead in new_leads)
    leads_with_confirmed_whatsapp = sum(bool(lead.get("whatsapp_confirmed")) for lead in new_leads)
    leads_with_landline_only = sum(lead.get("_phone_classification") == "landline" for lead in new_leads)
    leads_with_email_only = sum(
        bool(lead.get("email")) and lead.get("_phone_classification") != "mobile"
        for lead in new_leads
    )
    logging.info("Leads with website: %d", leads_with_website)
    logging.info("Leads with email: %d", leads_with_email)
    logging.info("Leads with mobile: %d", leads_with_mobile)
    logging.info("Leads with confirmed WhatsApp: %d", leads_with_confirmed_whatsapp)
    logging.info("Leads with landline only: %d", leads_with_landline_only)
    logging.info("Leads with email only: %d", leads_with_email_only)
    logging.info("Leads rejected after full enrichment: %d", rejected_no_mobile_email)
    logging.info("Duplicates skipped: %d", duplicates)
    logging.info("Leads appended: %d", 0 if settings.dry_run else len(new_leads))
    logging.info("Skipped empty sectors: %d", skipped_empty_sectors)
    logging.info("Stop reason: %s", stop_reason)
    if settings.dry_run:
        logging.info("Dry-run candidates (not appended): %d", len(new_leads))
    return 0


def test_overpass(settings: Settings) -> int:
    """Run exactly one city/sector Overpass query without Sheets or enrichment."""
    city = settings.cities[0]
    sector = settings.sectors[0]
    if normalise_text(sector) not in OSM_SECTOR_TAGS:
        raise ValueError(f"No OSM mapping is configured for sector: {sector}")
    client = PublicWebClient(settings)
    payload = overpass_json(client, build_overpass_query(sector, city))
    elements = payload.get("elements", [])
    element_count = len(elements) if isinstance(elements, list) else 0
    print(f"endpoint used: {client.last_overpass_endpoint}")
    print(f"HTTP status: {client.last_overpass_status}")
    print(f"number of OSM elements returned: {element_count}")
    print(f"Overpass requests attempted: {client.overpass_requests}")
    return 0


def run_mocked_osm_tests() -> int:
    """Offline parser checks; no Overpass, website, paid API, or Sheets request is made."""
    class FakeResponse:
        headers = {"Content-Type": "application/json"}

        def __init__(
            self, payload: dict[str, object], url: str = "https://example.ma",
            status_code: int = 200, content_type: str = "application/json",
        ):
            self._payload = payload
            self.url = url
            self.status_code = status_code
            self.headers = {"Content-Type": content_type}

        def json(self) -> dict[str, object]:
            return self._payload

    class FakeOverpassClient:
        def __init__(self) -> None:
            self.overpass_requests = 0
            self.osm_businesses_discovered = 0
            self.queries: list[str] = []
            self.last_overpass_endpoint = ""
            self.last_overpass_status = 0

        def request(self, method: str, url: str, **kwargs: object) -> FakeResponse:
            query = str((kwargs.get("data") or {}).get("data", ""))
            self.queries.append(query)
            assert kwargs.get("headers") == {
                "Content-Type": "application/x-www-form-urlencoded",
                "User-Agent": "BKZ-Lead-Collector/1.0",
                "Accept": "application/json",
            }
            assert kwargs.get("_timeout") == (5.0, 25.0)
            return FakeResponse({"elements": [
                {
                    "type": "node", "id": 101,
                    "tags": {
                        "name": "Restaurant Atlas", "amenity": "restaurant",
                        "addr:city": "Agadir", "addr:street": "Avenue Hassan II",
                        "phone": "05 28 00 00 00", "contact:mobile": "07 12 34 56 78",
                        "website": "https://restaurant-atlas.ma/menu?ref=osm",
                        "contact:whatsapp": "https://wa.me/212712345678",
                    },
                },
                {"type": "way", "id": 202, "tags": {"name": "Café Test", "amenity": "cafe", "addr:city": "Agadir"}},
                {"type": "relation", "id": 303, "tags": {"name": "Outside", "amenity": "restaurant", "addr:city": "Rabat"}},
            ]})

    OVERPASS_AREA_CACHE.clear()
    fake = FakeOverpassClient()
    businesses = adapter_osm_overpass(fake, "restaurants", "Agadir", 5)  # type: ignore[arg-type]
    assert len(businesses) == 2
    atlas = businesses[0]
    assert atlas["_osm_identity"] == "node/101"
    assert atlas["phone"] == "+212712345678"
    assert atlas["website"] == "https://restaurant-atlas.ma"
    assert atlas["whatsapp_confirmed"] is True
    assert len(fake.queries) == 1
    assert 'node["amenity"="restaurant"](area.searchArea);' in fake.queries[0]
    assert 'way["amenity"="restaurant"](area.searchArea);' in fake.queries[0]
    assert 'relation["amenity"="restaurant"](area.searchArea);' in fake.queries[0]
    assert fake.queries[0].endswith("out tags center;")
    adapter_osm_overpass(fake, "cafés", "Agadir", 5)  # type: ignore[arg-type]
    assert len(fake.queries) == 2

    class FallbackClient(FakeOverpassClient):
        def request(self, method: str, url: str, **kwargs: object) -> FakeResponse:
            self.queries.append(str((kwargs.get("data") or {}).get("data", "")))
            attempt = len(self.queries)
            if attempt == 1:
                return FakeResponse({}, url, 406)
            if attempt == 2:
                return FakeResponse({}, url, 200, "text/html")
            return FakeResponse({"elements": [{"type": "node", "id": 1}]}, url)

    fallback = FallbackClient()
    fallback_payload = overpass_json(
        fallback, build_overpass_query("restaurants", "Agadir")
    )  # type: ignore[arg-type]
    assert len(fallback_payload["elements"]) == 1
    assert fallback.overpass_requests == 3
    assert fallback.last_overpass_endpoint == OVERPASS_ENDPOINTS[2]
    assert fallback.last_overpass_status == 200

    homepage = BeautifulSoup(
        '<html><body>Agadir Maroc <a href="https://wa.me/212612345678">WhatsApp</a>'
        '<a href="mailto:contact@example.ma">Email</a></body></html>', "html.parser"
    )
    contact_navigation = BeautifulSoup(
        '<html><body><a href="/products">Produits</a>'
        '<a href="/contactez-nous">Contactez-nous</a>'
        '<a href="/mentions-legales">Mentions légales</a></body></html>',
        "html.parser",
    )
    internal_pages = candidate_internal_pages(contact_navigation, "https://example.ma")
    assert internal_pages[0] == "https://example.ma/contactez-nous"
    assert "https://example.ma/contact" in internal_pages
    protected_email = "cloud@example.ma"
    cfemail = bytes([0x12] + [ord(char) ^ 0x12 for char in protected_email]).hex()
    email_soup = BeautifulSoup(
        f'<html><body>contact [at] example [dot] ma <span data-cfemail="{cfemail}"></span></body></html>',
        "html.parser",
    )
    extracted_emails = public_email_candidates(email_soup, email_soup.get_text(" ", strip=True))
    assert set(extracted_emails) == {"contact@example.ma", protected_email}
    assert preferred_moroccan_phone(["٠٧ ١٢ ٣٤ ٥٦ ٧٨"]) == "+212712345678"
    assert explicit_whatsapp_number(["whatsapp://send?phone=212612345678"]) == "+212612345678"
    assert explicit_whatsapp_number(["https://web.whatsapp.com/send?phone=212712345678"]) == "+212712345678"

    class FakeWorksheet:
        def __init__(self) -> None:
            self.updated_rows: list[list[str]] = []
            self.updated_range = ""

        def col_values(self, column: int) -> list[str]:
            assert column == 1
            return ["lead_id"]

        def update(self, rows: list[list[str]], range_name: str, value_input_option: str) -> None:
            assert value_input_option == "RAW"
            self.updated_rows = rows
            self.updated_range = range_name

    reordered_headers = [
        "lead_id", "company_name", "industry", "location", "email", "phone", "website",
        "score", "contact_status", "personalised_message", "sent_at", "recommended_service",
        "why_good_prospect", "replied_at", "reply_status", "reply_summary", "gmail_message_id",
        "", "business_description", "automation_opportunity",
    ]
    fake_worksheet = FakeWorksheet()
    write_new_leads(fake_worksheet, [{
        "lead_id": "lead_test", "company_name": "Example", "industry": "restaurants",
        "location": "Agadir, Morocco", "email": "contact@example.ma", "phone": "+212612345678",
        "website": "https://example.ma", "business_description": "Public business description.",
        "automation_opportunity": "Public automation opportunity.",
    }], reordered_headers)  # type: ignore[arg-type]
    assert fake_worksheet.updated_range == "A2:T2"
    assert fake_worksheet.updated_rows[0][6] == "https://example.ma"
    assert fake_worksheet.updated_rows[0][18] == "Public business description."
    assert fake_worksheet.updated_rows[0][19] == "Public automation opportunity."
    website_client = PublicWebClient.__new__(PublicWebClient)
    website_client.sector_deadline = None
    website_client.fetch_soup = lambda url, **kwargs: (homepage, FakeResponse({}, url))  # type: ignore[method-assign]
    enriched = website_client.enrich_business({
        "company_name": "Example", "location": "Agadir, Morocco",
        "website": "https://example.ma", "phone": "", "email": "",
        "source_url": "https://www.openstreetmap.org/node/1",
        "_osm_identity": "node/1", "whatsapp_confirmed": False,
    }, "restaurants", "Agadir")
    assert enriched and enriched["phone"] == "+212612345678"
    assert enriched["whatsapp_confirmed"] is True
    assert enriched["email"] == "contact@example.ma"
    logging.info("Mocked OSM and website WhatsApp tests passed")
    return 0


def parse_args(argv: Iterable[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="Collect without reading or writing Google Sheets")
    parser.add_argument("--verbose", action="store_true", help="Enable debug logging")
    parser.add_argument(
        "--cities", nargs="+", metavar="CITY",
        help="Target cities (only Agadir, Casablanca, and Marrakech are allowed; comma-separated also works)",
    )
    parser.add_argument(
        "--sectors", nargs="+", metavar="SECTOR",
        help="Business sectors to search (quote multi-word sectors; comma-separated also works)",
    )
    parser.add_argument(
        "--max-searches", type=int,
        help="Maximum number of source/sector/city adapter searches for this run",
    )
    parser.add_argument(
        "--target-leads", type=int, default=30,
        help="Stop immediately after collecting this many new deduplicated leads (default: 30)",
    )
    parser.add_argument(
        "--max-place-details", type=int, default=30,
        help="Maximum Google Place Details requests per run (default: 30)",
    )
    parser.add_argument(
        "--fast-mode", action=argparse.BooleanOptionalAction, default=True,
        help="Use no directory pagination and bounded enrichment (enabled by default)",
    )
    parser.add_argument(
        "--allow-landlines", action="store_true",
        help="Allow Moroccan 05/+2125 landlines in the stored phone field",
    )
    parser.add_argument(
        "--sources", nargs="+", metavar="SOURCE",
        help=f"Sources to use (available: {', '.join(SOURCE_ADAPTERS)}; comma-separated also works)",
    )
    parser.add_argument(
        "--repair-misaligned-rows", nargs=2, type=int, metavar=("START", "END"),
        help="Preview repair of the known misaligned lead rows (restricted to rows 16 20)",
    )
    parser.add_argument(
        "--confirm-repair", action="store_true",
        help="Apply the requested repair after creating a local JSON backup",
    )
    parser.add_argument(
        "--cleanup-outside-main-range", action="store_true",
        help="Preview misplaced lead cells found after column T",
    )
    parser.add_argument(
        "--confirm-cleanup", action="store_true",
        help="Clear detected post-T lead blocks after creating a local JSON backup",
    )
    parser.add_argument("--self-test-osm", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument(
        "--test-overpass", action="store_true",
        help="Run one live Overpass query only; do not access Google Sheets or enrich websites",
    )
    return parser.parse_args(argv)


def flatten_cli_values(values: list[str] | None) -> list[str] | None:
    if not values:
        return None
    flattened = [part.strip() for value in values for part in value.split(",") if part.strip()]
    return flattened or None


def main(argv: Iterable[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
    )
    try:
        if args.self_test_osm:
            return run_mocked_osm_tests()
        if args.test_overpass:
            test_settings = load_settings(
                dry_run=True,
                cities_override=flatten_cli_values(args.cities),
                sectors_override=flatten_cli_values(args.sectors),
                sources_override=["osm_overpass"],
                target_leads=1,
            )
            return test_overpass(test_settings)
        if args.confirm_cleanup and not args.cleanup_outside_main_range:
            raise ValueError("--confirm-cleanup requires --cleanup-outside-main-range")
        if args.cleanup_outside_main_range and args.repair_misaligned_rows:
            raise ValueError("Cleanup and repair commands cannot run together")
        if args.cleanup_outside_main_range:
            cleanup_settings = load_settings(
                cities_override=flatten_cli_values(args.cities),
                sectors_override=flatten_cli_values(args.sectors),
                sources_override=flatten_cli_values(args.sources),
            )
            return cleanup_outside_main_range(cleanup_settings, args.confirm_cleanup)
        if args.confirm_repair and not args.repair_misaligned_rows:
            raise ValueError("--confirm-repair requires --repair-misaligned-rows 16 20")
        if args.repair_misaligned_rows:
            repair_settings = load_settings(
                cities_override=flatten_cli_values(args.cities),
                sectors_override=flatten_cli_values(args.sectors),
                sources_override=flatten_cli_values(args.sources),
            )
            return repair_misaligned_rows(
                repair_settings,
                args.repair_misaligned_rows[0],
                args.repair_misaligned_rows[1],
                args.confirm_repair,
            )
        if args.max_searches is not None and args.max_searches < 1:
            raise ValueError("--max-searches must be at least 1")
        if args.target_leads < 1:
            raise ValueError("--target-leads must be at least 1")
        if args.max_place_details < 1:
            raise ValueError("--max-place-details must be at least 1")
        return collect(load_settings(
            dry_run=args.dry_run,
            cities_override=flatten_cli_values(args.cities),
            sectors_override=flatten_cli_values(args.sectors),
            max_searches=args.max_searches,
            sources_override=flatten_cli_values(args.sources),
            target_leads=args.target_leads,
            fast_mode=args.fast_mode,
            allow_landlines=args.allow_landlines,
            max_place_details=args.max_place_details,
        ))
    except (ValueError, RuntimeError, OSError, gspread.GSpreadException) as exc:
        logging.error("Collector stopped safely: %s", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
