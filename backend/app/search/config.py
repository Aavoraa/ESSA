"""Elasticsearch settings, read from the environment (run.bat loads them from
essa-intake/.env). Nothing is hard-coded; with ELASTICSEARCH_ENABLED unset the
whole package is inert and ESSA runs on PostgreSQL alone, exactly as before."""
import os


def _env(name, default=""):
    return (os.environ.get(name) or "").strip() or default


def _flag(name, default=False):
    v = _env(name)
    return default if not v else v.lower() in ("1", "true", "yes", "on")


ENABLED = _flag("ELASTICSEARCH_ENABLED")
URL = _env("ELASTICSEARCH_URL", "https://localhost:9200")
USERNAME = _env("ELASTICSEARCH_USERNAME")
PASSWORD = _env("ELASTICSEARCH_PASSWORD")
API_KEY = _env("ELASTICSEARCH_API_KEY")
CA_CERTS = _env("ELASTICSEARCH_CA_CERTS") or None
VERIFY_CERTS = _flag("ELASTICSEARCH_VERIFY_CERTS", True)
#: seconds a request may wait on Elasticsearch before the PostgreSQL path is used
REQUEST_TIMEOUT = float(_env("ELASTICSEARCH_TIMEOUT", "5"))

#: every index and alias ESSA owns starts with this (the essa_app role is
#: granted on `essa-*` and nothing else)
PREFIX = _env("ELASTICSEARCH_INDEX_PREFIX", "essa-")

#: how often the background sync runs, and how stale the index may be before
#: reads stop trusting it and use PostgreSQL instead
SYNC_SECONDS = max(2.0, float(_env("ESSA_SEARCH_SYNC_SECONDS", "10")))
MAX_LAG_SECONDS = max(SYNC_SECONDS * 3, float(_env("ESSA_SEARCH_MAX_LAG_SECONDS", "60")))
#: how often the shop-product → warehouse-product link is re-checked
MAPPING_CHECK_SECONDS = float(_env("ESSA_SEARCH_MAPPING_CHECK_SECONDS", "300"))
