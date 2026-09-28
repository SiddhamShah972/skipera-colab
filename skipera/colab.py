import json
from getpass import getpass
from pathlib import Path


DEFAULT_CONFIG = {
    "cookies": {},
    "perplexity_api_key": "",
    "gemini_api_key": "",
    "perplexity_model": "sonar-pro",
    "gemini_model": "gemini-3.1-flash-lite",
}


def _read_cookies(cookies: dict | str | None) -> dict:
    raw_cookies = cookies
    if raw_cookies is None:
        raw_cookies = getpass("Paste Coursera cookies as JSON: ")

    if isinstance(raw_cookies, str):
        try:
            raw_cookies = json.loads(raw_cookies)
        except json.JSONDecodeError as error:
            raise ValueError("cookies must be a JSON object") from error

    if not isinstance(raw_cookies, dict) or not raw_cookies.get("CAUTH"):
        raise ValueError("cookies must be a dictionary containing CAUTH")

    return raw_cookies


def _save_cookies(cookies: dict) -> None:
    config_dir = Path.home() / ".skipera"
    config_dir.mkdir(parents=True, exist_ok=True)
    config_file = config_dir / "config.json"

    config = DEFAULT_CONFIG.copy()
    if config_file.exists():
        config.update(json.loads(config_file.read_text(encoding="utf-8")))
    config["cookies"] = cookies
    config_file.write_text(json.dumps(config, indent=2), encoding="utf-8")


def _choose_index(prompt: str, maximum: int) -> int:
    while True:
        try:
            choice = int(input(prompt))
        except ValueError:
            print("Enter a whole number.")
            continue
        if 1 <= choice <= maximum:
            return choice
        print(f"Enter a number from 1 to {maximum}.")


def run(cookies: dict | str | None = None, llm: bool = False) -> None:
    """Run Skipera's interactive course and module workflow in Google Colab."""
    _save_cookies(_read_cookies(cookies))

    from .main import Skipera

    app = Skipera(None, llm)
    courses = app.get_pending_courses()
    if not courses:
        raise RuntimeError("No enrolled courses were returned by Coursera.")

    print("\nEnrolled courses:")
    for index, course in enumerate(courses, start=1):
        print(f"{index}. {course['name']} ({course['slug']})")
    app.course = courses[_choose_index("Select a course: ", len(courses)) - 1]["slug"]

    data = app.get_course_materials()
    app.course_id = data["elements"][0]["id"]
    all_items = data["linked"]["onDemandCourseMaterialItems.v2"]
    modules = data["linked"]["onDemandCourseMaterialModules.v1"]

    print("\nModules:")
    for index, module in enumerate(modules, start=1):
        print(f"{index}. {module.get('name') or module.get('slug') or module.get('id')}")
    selected_module = modules[_choose_index("Select a module to run: ", len(modules)) - 1]
    selected_module_ids = {selected_module["id"]}
    items_to_process = [
        item for item in all_items
        if item.get("moduleId") in selected_module_ids
    ]

    app.process_items(items_to_process, selected_module_ids)
