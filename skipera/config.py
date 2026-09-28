import json
import os
import sys
from pathlib import Path

import click
from loguru import logger

CONFIG_DIR = Path.home() / ".skipera"
CONFIG_FILE = CONFIG_DIR / "config.json"

DEFAULT_CONFIG = {
    "cookies": {},
    "openrouter_api_key": "",
    "openrouter_model": "nvidia/nemotron-3-ultra-550b-a55b:free",
    "perplexity_api_key": "",
    "gemini_api_key": "",
    "perplexity_model": "sonar-pro",
    "gemini_model": "gemini-3.1-flash-lite"
}


def fetch_browser_cookies() -> dict:
    try:
        import browser_cookie3
    except ImportError:
        logger.error(
            "browser-cookie3 not installed. Run: pip install browser-cookie3")
        return {}

    browsers = [
        ("Chrome", browser_cookie3.chrome),
        ("Firefox", browser_cookie3.firefox),
        ("Edge", browser_cookie3.edge),
    ]

    for name, browser_fn in browsers:
        try:
            cj = browser_fn(domain_name=".coursera.org")
            cookies = {c.name: c.value for c in cj}
            if "CAUTH" in cookies:
                logger.success(f"Fetched Coursera cookies from {name}")
                return cookies
        except Exception:
            continue

    logger.warning(
        "Could not find Coursera cookies in any browser. Make sure you're logged into Coursera.")
    return {}


def prompt_for_cookies() -> dict:
    while True:
        raw_cookies = click.prompt(
            'Paste Coursera cookies as JSON (for example, {"CAUTH": "..."})',
            hide_input=True,
        )
        try:
            cookies = json.loads(raw_cookies)
        except json.JSONDecodeError:
            logger.error("Cookies must be valid JSON.")
            continue

        if not isinstance(cookies, dict) or not cookies.get("CAUTH"):
            logger.error("The cookie JSON must contain a CAUTH value.")
            continue

        return cookies


def load_config() -> dict:
    if not CONFIG_FILE.exists():
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        CONFIG_FILE.write_text(json.dumps(DEFAULT_CONFIG, indent=2))

    config = json.loads(CONFIG_FILE.read_text())

    if not config.get("cookies"):
        cauth = os.getenv("COURSERA_CAUTH")
        if cauth:
            config["cookies"] = {"CAUTH": cauth}
        else:
            logger.info("No cookies in config — enter them to continue.")
            config["cookies"] = prompt_for_cookies()
        CONFIG_FILE.write_text(json.dumps(config, indent=2))
        logger.info(f"Cookies saved to {CONFIG_FILE}")

    return config


_config = load_config()

# URLs (constant, not user-configurable)
BASE_URL = "https://www.coursera.org/api/"
GRAPHQL_URL = "https://www.coursera.org/graphql-gateway"
PERPLEXITY_API_URL = "https://api.perplexity.ai/chat/completions"
OPENROUTER_API_URL = "https://openrouter.ai/api/v1/chat/completions"

# User-configurable
COOKIES = _config["cookies"]
OPENROUTER_API_KEY = os.getenv(
    "OPENROUTER_API_KEY", _config.get("openrouter_api_key", ""))
OPENROUTER_MODEL = os.getenv("OPENROUTER_MODEL") or _config.get(
    "openrouter_model", "nvidia/nemotron-3-ultra-550b-a55b:free")
PERPLEXITY_API_KEY = os.getenv(
    "PERPLEXITY_API_KEY", _config.get("perplexity_api_key", ""))
GEMINI_API_KEY = os.getenv(
    "GEMINI_API_KEY", _config.get("gemini_api_key", ""))
PERPLEXITY_MODEL = _config.get("perplexity_model", "sonar-pro")
GEMINI_MODEL = _config.get("gemini_model", "gemini-3.1-flash-lite")

HEADERS = {
    'user-agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36',
    'x-coursera-application': 'ondemand',
    'x-coursera-version': '3bfd497de04ae0fef167b747fd85a6fbc8fb55df',
    'x-requested-with': 'XMLHttpRequest',
}
