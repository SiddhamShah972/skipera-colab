import click
import httpx
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from .config import parse_cookies, prompt_for_cookies, CONFIG_FILE, DEFAULT_CONFIG, BASE_URL, HEADERS, COOKIES
import json
from loguru import logger
from .assessment.solver import GradedSolver
from .discussion.solver import DiscussionPromptSolver
from .coach.solver import CoachSolver
from .watcher.watch import Watcher
from .session_utils import get_csrf_headers, random_delay
from .reporting import send_daily_report


class Skipera(object):
    def __init__(self, course: str | None, llm: bool):
        self.user_id = None
        self.course_id = None
        self.base_url = BASE_URL
        self.session = httpx.Client(timeout=60.0, follow_redirects=True)
        self.session.headers.update(HEADERS)
        self.session.cookies.update(COOKIES)
        self.course = course
        self.llm = llm
        self.failed_items = set()
        self.daily_report = []
        if not self.get_userid():
            self.refresh_cookies()
            if not self.get_userid():
                logger.error(
                    "Cookies are invalid. Log into Coursera in your browser, close it, and retry.")
                raise SystemExit

    def refresh_cookies(self):
        logger.warning("Session expired — enter new Coursera cookies...")
        cauth = os.getenv("COURSERA_CAUTH")
        if cauth:
            logger.info("Trying cookies from COURSERA_CAUTH.")
            cookies = parse_cookies(cauth)
        else:
            cookies = prompt_for_cookies()
        self.session.cookies.clear()
        self.session.cookies.update(cookies)
        cfg = json.loads(CONFIG_FILE.read_text()
                         ) if CONFIG_FILE.exists() else DEFAULT_CONFIG.copy()
        cfg["cookies"] = cookies
        CONFIG_FILE.write_text(json.dumps(cfg, indent=2))

    def get_userid(self) -> bool:
        r = self.session.get(
            self.base_url + "adminUserPermissions.v1?q=my").json()
        try:
            self.user_id = r["elements"][0]["id"]
            logger.info("User ID: " + self.user_id)
        except KeyError:
            if r.get("errorCode"):
                logger.error("Error Encountered: " + r["errorCode"])
            return False
        return True

    def get_pending_courses(self) -> list[dict]:
        response = self.session.get(
            self.base_url + "memberships.v1",
            params={
                "includes": "courseId,courses.v1",
                "q": "me",
                "showHidden": "true",
                "filter": "current,preEnrolled",
            },
        )
        if response.status_code != 200 or "json" not in response.headers.get("content-type", ""):
            logger.warning("Coursera did not provide an enrolled-course list.")
            return []

        data = response.json()
        linked_courses = data.get("linked", {}).get("courses.v1", [])
        pending_courses = []

        for course in linked_courses:
            slug = course.get("slug")
            if slug:
                pending_courses.append({
                    "slug": slug,
                    "name": course.get("name") or slug,
                })

        return pending_courses

    def choose_course(self) -> str:
        courses = self.get_pending_courses()
        if not courses:
            click.echo(
                "Automatic pending-course listing is unavailable."
                " Enter a course slug from its Coursera URL."
            )
            return click.prompt("Course slug")

        click.echo("\nPending courses:")
        for index, course in enumerate(courses, start=1):
            click.echo(f"{index}. {course['name']} ({course['slug']})")

        choice = click.prompt(
            "Select a course",
            type=click.IntRange(min=1, max=len(courses)),
        )
        return courses[choice - 1]["slug"]

    def get_course(self) -> None:
        r = self.get_course_materials()
        self.course_id = r["elements"][0]["id"]
        all_items = r["linked"]["onDemandCourseMaterialItems.v2"]
        modules = r["linked"]["onDemandCourseMaterialModules.v1"]

        logger.info("Course ID: " + self.course_id)
        logger.info("Number of Modules: " + str(len(modules)))
        logger.info("Total items: " + str(len(all_items)))

        click.echo("\nModules:")
        for index, module in enumerate(modules, start=1):
            click.echo(f"{index}. {module.get('name') or module.get('slug') or module.get('id')}")

        raw_selection = click.prompt(
            "Enter module numbers to run, separated by commas (or press Enter for all)",
            default="",
            show_default=False,
        ).strip()
        selected_indexes = self.parse_module_selection(raw_selection, len(modules))
        selected_module_ids = {
            modules[index - 1]["id"] for index in selected_indexes
        }
        if not selected_module_ids:
            selected_module_ids = {module["id"] for module in modules}
        items_to_process = [
            item for item in all_items
            if item.get("moduleId") in selected_module_ids
        ]

        self.process_items(items_to_process, selected_module_ids)

    def run_daily(self) -> None:
        self.daily_report = []
        courses = ([{"slug": self.course, "name": self.course}]
                   if self.course else self.get_pending_courses())
        if not courses:
            raise click.ClickException("No enrolled Coursera courses were found.")

        for course in courses:
            self.course = course["slug"]
            self.failed_items.clear()
            try:
                data = self.get_course_materials()
                self.course_id = data["elements"][0]["id"]
                all_items = data["linked"]["onDemandCourseMaterialItems.v2"]
                modules = data["linked"]["onDemandCourseMaterialModules.v1"]
                completed = self.get_completed_items()

                selected_module = next(
                    (
                        module for module in modules
                        if any(
                            item.get("moduleId") == module["id"]
                            and item["id"] not in completed
                            and not item.get("isLocked", False)
                            for item in all_items
                        )
                    ),
                    None,
                )
                if selected_module is None:
                    logger.info(f"{course['name']}: no unfinished unlocked module.")
                    continue

                module_id = selected_module["id"]
                module_items = [
                    item for item in all_items
                    if item.get("moduleId") == module_id
                ]
                quiz_item = next(
                    (
                        item for item in module_items
                        if item["contentSummary"]["typeName"]
                        in {"ungradedAssignment", "staffGraded"}
                        and item["id"] not in completed
                    ),
                    None,
                )
                selected_item_ids = {
                    item["id"] for item in module_items
                    if item["contentSummary"]["typeName"]
                    not in {"ungradedAssignment", "staffGraded"}
                }
                if quiz_item is not None:
                    selected_item_ids.add(quiz_item["id"])
                    logger.info(f"{course['name']}: solving one quiz item.")
                else:
                    logger.info(f"{course['name']}: no unfinished supported quiz in module.")

                items_to_process = [
                    item for item in module_items
                    if item["id"] in selected_item_ids
                ]
                logger.info(
                    f"{course['name']}: processing module "
                    f"{selected_module.get('name') or selected_module.get('slug') or module_id}"
                )
                self.process_items(items_to_process, {module_id}, selected_item_ids)
            except Exception as error:
                logger.exception(f"Could not process {course['name']}: {error}")

        send_daily_report(self.daily_report)

    @staticmethod
    def parse_module_selection(selection: str, module_count: int) -> set[int]:
        if not selection:
            return set()

        try:
            indexes = {int(value.strip()) for value in selection.split(",")}
        except ValueError as error:
            raise click.BadParameter("use comma-separated module numbers") from error

        if any(index < 1 or index > module_count for index in indexes):
            raise click.BadParameter(f"module numbers must be between 1 and {module_count}")
        return indexes

    def get_course_materials(self) -> dict:
        r = self.session.get(self.base_url + f"onDemandCourseMaterials.v2/", params={
            "q": "slug",
            "slug": self.course,
            "includes": "modules,lessons,passableItemGroups,passableItemGroupChoices,passableLessonElements,items,"
                        "tracks,gradePolicy,gradingParameters,embeddedContentMapping",
            "fields": "moduleIds,onDemandCourseMaterialModules.v1(name,slug,description,timeCommitment,lessonIds,"
                      "optional,learningObjectives),onDemandCourseMaterialLessons.v1(name,slug,timeCommitment,"
                      "elementIds,optional,trackId),onDemandCourseMaterialPassableItemGroups.v1(requiredPassedCount,"
                      "passableItemGroupChoiceIds,trackId),onDemandCourseMaterialPassableItemGroupChoices.v1(name,"
                      "description,itemIds),onDemandCourseMaterialPassableLessonElements.v1(gradingWeight,"
                      "isRequiredForPassing),onDemandCourseMaterialItems.v2(name,originalName,slug,timeCommitment,"
                      "contentSummary,isLocked,lockableByItem,itemLockedReasonCode,trackId,lockedStatus,itemLockSummary,"
                      "customDisplayTypenameOverride),onDemandCourseMaterialTracks.v1(passablesCount),"
                      "onDemandGradingParameters.v1(gradedAssignmentGroups),"
                      "contentAtomRelations.v1(embeddedContentSourceCourseId,subContainerId)",
            "showLockedItems": True
        })

        if r.status_code != 200:
            logger.error("Please check if you are enrolled in the course!")
            raise SystemExit

        return r.json()

    def process_items(
            self,
            all_items: list[dict],
            selected_module_ids: set[str] | None = None,
            selected_item_ids: set[str] | None = None,
    ) -> None:
        selected_module_ids = selected_module_ids or set()
        selected_item_ids = selected_item_ids or {item["id"] for item in all_items}
        total = len(all_items)

        while True:
            completed = self.get_completed_items()

            try:
                fresh_data = self.get_course_materials()
                current_items = fresh_data["linked"]["onDemandCourseMaterialItems.v2"]
            except SystemExit:
                current_items = all_items

            pending_items = [
                item for item in current_items
                if item.get("moduleId") in selected_module_ids
                and item["id"] in selected_item_ids
                and item["id"] not in completed
            ]
            if not pending_items:
                logger.info(f"Finished: {total}/{total} completed.")
                break

            unlocked_items = [
                item for item in pending_items 
                if not item.get("isLocked", False) and item["id"] not in self.failed_items
            ]
            if not unlocked_items:
                logger.info(
                    f"Finished: {total - len(pending_items)}/{total} completed, {len(pending_items)} still locked/pending."
                )
                break

            concurrent_items = []
            sequential_items = []
            for item in unlocked_items:
                if item["contentSummary"]["typeName"] not in {"discussionPrompt", "ungradedAssignment", "staffGraded", "phasedPeer"}:
                    concurrent_items.append(item)
                else:
                    sequential_items.append(item)

            if concurrent_items:
                with ThreadPoolExecutor(max_workers=min(6, len(concurrent_items))) as executor:
                    futures = {
                        executor.submit(self.process_item, item): item 
                        for item in concurrent_items
                    }
                    for future in as_completed(futures):
                        item = futures[future]
                        try:
                            success = future.result()
                            if not success:
                                self.failed_items.add(item["id"])
                        except Exception as e:
                            logger.exception(f"Error in processing item: {e}")
                            self.failed_items.add(item["id"])
                continue

            if sequential_items:
                item = sequential_items[0]
                try:
                    success = self.process_item(item)
                    if not success:
                        self.failed_items.add(item["id"])
                except Exception as e:
                    logger.exception(f"Error in processing item: {e}")
                    self.failed_items.add(item["id"])
                continue

    def process_item(self, item: dict) -> bool:
        item_type = item["contentSummary"]["typeName"]
        module_id = item.get('moduleId', 'unknown')
        item_id = item['id']
        logger.info(
            f"[module:{module_id}] [item:{item_id}] Processing {item['name']}")

        success = False
        if item_type == "lecture":
            success = self.watch_item(item, self.get_video_metadata(item_id))
        elif item_type == "supplement":
            success = self.read_item(item_id)
        elif item_type in {"ungradedAssignment", "staffGraded"} and self.llm:
            success = GradedSolver(
                self.session, self.course_id, item_id).solve()
        elif item_type == "discussionPrompt" and self.llm:
            success = DiscussionPromptSolver(
                self.session, self.user_id, self.course_id, item_id).solve()
        elif item_type == "coach":
            success = CoachSolver(
                self.session, self.user_id, self.course_id, item_id).solve()
        elif item_type == "ungradedWidget":
            success = self.ungraded_widget_item(item_id)
        elif item_type == "ungradedLti":
            success = self.ungraded_lti_item(item_id)
        else:
            logger.warning(
                f"[module:{module_id}] [item:{item_id}] Unknown/skipped item type: {item_type} - skipping.")

        if success:
            if item_type == "lecture":
                report_kind = "video"
            elif item_type in {"ungradedAssignment", "staffGraded"}:
                report_kind = "quiz"
            else:
                report_kind = "other"
            self.daily_report.append({
                "kind": report_kind,
                "course": self.course,
                "module": module_id,
                "name": item["name"],
                "item_type": item_type,
            })

        return success

    def get_completed_items(self) -> set[str]:
        r = self.session.get(
            self.base_url +
            f"onDemandCoursesProgress.v1/{self.user_id}~{self.course_id}",
            params={"fields": "gradedAssignmentGroupProgress"}
        )

        if r.status_code != 200:
            logger.debug("Could not fetch course progress.")
            logger.debug(r.text)
            return set()

        data = r.json()
        elements = data.get("elements") or []
        if not elements:
            logger.debug("Course progress response has no elements.")
            return set()

        items = elements[0].get("items", {})
        return {
            item_id
            for item_id, progress in items.items()
            if progress.get("progressState") == "Completed"
        }

    def get_video_metadata(self, item_id: str) -> dict:
        r = self.session.get(self.base_url + f"onDemandLectureVideos.v1/{self.course_id}~{item_id}", params={
            "includes": "video",
            "fields": "disableSkippingForward,startMs,endMs"
        }).json()

        return {"can_skip": not r["elements"][0]["disableSkippingForward"],
                "tracking_id": r["linked"]["onDemandVideos.v1"][0]["id"]}

    def watch_item(self, item: dict, metadata: dict) -> bool:
        watcher = Watcher(self.session, item, metadata,
                          self.user_id, self.course, self.course_id)
        return watcher.watch_item()

    def read_item(self, item_id) -> bool:
        r = self.session.post(self.base_url + "onDemandSupplementCompletions.v1",
                              headers=get_csrf_headers(self.session),
                              json={
                                  "courseId": self.course_id,
                                  "itemId": item_id,
                                  "userId": int(self.user_id)
                              })
        return "Completed" in r.text

    def ungraded_widget_item(self, item_id) -> bool:
        r = self.session.get(
            self.base_url + f"onDemandWidgetSessions.v1/{self.user_id}~{self.course_id}~{item_id}",
            params={"fields": "session,sessionId"}
        )
        if r.status_code != 200:
            logger.error(f"Failed to get session for widget {item_id}: {r.status_code}")
            return False

        try:
            session_id = r.json()["elements"][0]["sessionId"]
        except (KeyError, IndexError):
            logger.error(f"Could not parse sessionId for widget {item_id}")
            return False

        res = self.session.put(
            self.base_url + f"onDemandWidgetProgress.v1/{self.user_id}~{self.course_id}~{item_id}",
            headers=get_csrf_headers(self.session),
            json={
                "sessionId": session_id,
                "progressState": "Completed"
            }
        )
        return 200 <= res.status_code < 300

    def ungraded_lti_item(self, item_id) -> bool:
        r = self.session.post(
            self.base_url + "rest/v1/lti/ungradedLaunches",
            headers=get_csrf_headers(self.session),
            json={
                "courseId": self.course_id,
                "itemId": item_id,
                "learnerId": int(self.user_id),
                "markItemCompleted": True
            }
        )
        return 200 <= r.status_code < 300


@logger.catch
@click.command()
@click.argument('slug', required=False)
@click.option('--llm', is_flag=True, help="Whether to use an LLM to solve graded assignments.")
@click.option('--daily', is_flag=True, help="Process one unfinished module for every enrolled course.")
def main(slug: str | None, llm: bool, daily: bool) -> None:
    skipera = Skipera(slug, llm)
    if daily:
        skipera.run_daily()
        return
    if not skipera.course:
        skipera.course = skipera.choose_course()
    skipera.get_course()

if __name__ == '__main__':
    main()
