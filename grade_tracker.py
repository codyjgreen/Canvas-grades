#!/usr/bin/env python3
"""
Student Grade Tracker (running grade across previous / current / future courses)
-----------------------------------------------------------------------------
For every student who has ever been enrolled in a course listed in the
production Courses spreadsheet (every cohort-month tab — past, current, and
future), pulls their real Canvas grades and answers two questions:

  1) What is their current grade, per course AND rolled up across their
     whole program?
  2) What do they need to score on everything still ungraded to finish with
     at least a 70%? If even a perfect score on everything remaining can't
     get them to 70%, that's flagged as mathematically impossible (i.e.
     already failed).

Runs as a one-shot GitHub Actions job (see .github/workflows/grade-tracker.yml)
rather than inside Apps Script, so there's no 6-minute execution cap to work
around — it just runs start to finish in a single pass.

HOW THE GRADE MATH WORKS (points-based projection)
For every published, submittable, point-bearing assignment in a course:
  - If the student has a real score (and isn't excused), those points count
    as "earned" and "possible-so-far".
  - If there's no score yet (not graded, whether or not the due date has
    passed), the assignment's points go into "possible-remaining" — i.e.
    it's still an opportunity, not a zero.
Current % = earned / possible-so-far.
To hit 70% overall:
    neededPoints = 0.70 * (possible-so-far + possible-remaining) - earned
    neededPct    = neededPoints / possible-remaining * 100
  - neededPct > 100%  -> CANNOT reach 70% even with a perfect score on the rest.
  - neededPct <= 0%   -> passing is already mathematically locked in.
  - otherwise         -> that's the average needed on everything left.

This is an unweighted, points-based approximation. If a course has weighted
assignment groups turned on in Canvas, this script also pulls Canvas's own
official current_score for comparison and flags the course as "Weighted
grading — projection is approximate" so it isn't mistaken for gospel.

REQUIRED ENVIRONMENT VARIABLES (set as GitHub Actions secrets)
  CANVAS_TOKEN                 Canvas API access token
  CANVAS_DOMAIN                e.g. https://flatiron.instructure.com
  COURSES_SPREADSHEET_ID       the production Courses tracking sheet (read-only)
  GRADE_TRACKER_SPREADSHEET_ID the "Student Grade Tracker" sheet this writes to
  GOOGLE_SERVICE_ACCOUNT_JSON  full JSON key contents for a service account
                                that has been shared on both sheets (Viewer
                                on Courses, Editor on Student Grade Tracker)

See README.md for full one-time setup steps.
"""

import json
import os
import re
import sys
import time
from datetime import datetime, timezone

import gspread
import requests
from google.oauth2.service_account import Credentials

PASSING_THRESHOLD = 0.70
PER_COURSE_SHEET_NAME = "Per-Course Grades"
SUMMARY_SHEET_NAME = "Student Program Summary"
COHORT_TAB_PATTERN = re.compile(r"^\d{1,2}/\d{4}$")

PER_COURSE_HEADERS = [
    "Student Name", "Student Email", "Canvas User ID", "Canvas Course ID", "Course Code", "Course Name",
    "Course Due Date", "Current Grade % (computed)", "Canvas Current Grade %", "Points Earned",
    "Points Possible So Far", "Points Remaining", "% Needed on Remaining", "Status", "Weighted Grading?",
    "Last Checked",
]

SUMMARY_HEADERS = [
    "Student Name", "Student Email", "Canvas User ID", "# Courses", "Total Points Earned",
    "Total Points Possible So Far", "Total Points Remaining", "Running Program Grade %",
    "% Needed on Remaining (Program-wide)", "Program Status", "Failed Courses", "At-Risk Courses",
    "Last Checked",
]

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive.readonly",
]


def env(name, required=True):
    val = os.environ.get(name)
    if val is not None:    
        val = val.strip()
    if required and not val:
        print(f"ERROR: missing required environment variable {name}", file=sys.stderr)
        sys.exit(1)
    return val


def get_canvas_config():
    return {
        "token": env("CANVAS_TOKEN"),
        "domain": env("CANVAS_DOMAIN").rstrip("/"),
    }


def get_sheets_client():
    raw = env("GOOGLE_SERVICE_ACCOUNT_JSON")
    info = json.loads(raw)
    creds = Credentials.from_service_account_info(info, scopes=SCOPES)
    return gspread.authorize(creds)


def find_col(header, needle):
    needle = needle.lower()
    for i, h in enumerate(header):
        if needle in str(h or "").lower():
            return i
    return -1


def canvas_get_all(path, config, params=None):
    """Paginated Canvas GET. Returns all pages combined."""
    url = config["domain"] + path
    query = dict(params or {})
    query.setdefault("per_page", 100)
    results = []
    headers = {"Authorization": "Bearer " + config["token"]}
    while url:
        resp = requests.get(url, headers=headers, params=query, timeout=30)
        if resp.status_code >= 300:
            raise RuntimeError(f"Canvas API error ({resp.status_code}) for {resp.url}: {resp.text[:500]}")
        results.extend(resp.json())
        url = resp.links.get("next", {}).get("url")
        query = None  # next URL already has all query params baked in
    return results


def get_all_program_courses(gc, courses_sheet_id):
    """
    Scans every MM/YYYY cohort tab in the Courses spreadsheet (no date or
    Archived filtering — we want the student's full history: previous,
    current, and future courses) and returns a de-duplicated course list.
    """
    ss = gc.open_by_key(courses_sheet_id)
    by_id = {}

    for ws in ss.worksheets():
        name = ws.title.strip()
        if not COHORT_TAB_PATTERN.match(name):
            continue

        values = ws.get_all_values()
        if len(values) < 2:
            continue
        header = values[0]

        idx_course_id = find_col(header, "Canvas Course Number")
        idx_course_name = find_col(header, "Course Name")
        idx_end_date = find_col(header, "End Date")

        if idx_course_id == -1:
            continue

        for row in values[1:]:
            if idx_course_id >= len(row):
                continue
            course_id = (row[idx_course_id] or "").strip()
            if not course_id or course_id in by_id:
                continue

            full_name = row[idx_course_name] if idx_course_name != -1 and idx_course_name < len(row) else ""
            course_code = full_name.split("//")[0].strip() if "//" in full_name else full_name
            end_date = ""
            if idx_end_date != -1 and idx_end_date < len(row) and row[idx_end_date]:
                end_date = row[idx_end_date]

            by_id[course_id] = {
                "courseId": course_id,
                "courseCode": course_code,
                "courseName": full_name or course_code,
                "endDate": end_date,
            }

    return list(by_id.values())


def get_course_weighting(course_id, config):
    try:
        resp = requests.get(
            f"{config['domain']}/api/v1/courses/{course_id}",
            headers={"Authorization": "Bearer " + config["token"]},
            timeout=30,
        )
        if resp.status_code >= 300:
            return False
        return bool(resp.json().get("apply_assignment_group_weights"))
    except Exception:
        return False


def get_active_enrollments_with_grades(course_id, config):
    enrollments = canvas_get_all(
        f"/api/v1/courses/{course_id}/enrollments",
        config,
        params={
            "type[]": "StudentEnrollment",
            "state[]": "active",
            "include[]": ["user", "email"],
        },
    )
    out = {}
    for e in enrollments:
        uid = e.get("user_id")
        if not uid:
            continue
        grades = e.get("grades") or {}
        user = e.get("user") or {}
        out[uid] = {
            "name": user.get("name") or f"User {uid}",
            "email": user.get("email") or user.get("login_id") or "",
            "canvasCurrentScore": grades.get("current_score"),
            "canvasCurrentGrade": grades.get("current_grade") or "",
        }
    return out


def compute_course_grades(course_id, config, enrollments_map):
    """
    Computes per-student point totals for one course using Canvas's bulk
    submissions endpoint (one paginated call instead of one call per
    assignment). Returns dict userId -> {earned, possibleGraded, possibleRemaining}.
    """
    assignments = canvas_get_all(f"/api/v1/courses/{course_id}/assignments", config)
    points_by_id = {}
    for a in assignments:
        if not a.get("published"):
            continue
        types = a.get("submission_types") or []
        if not types or "none" in types or "not_graded" in types:
            continue
        if a.get("grading_type") == "not_graded":
            continue
        points = a.get("points_possible")
        if not points or points <= 0:
            continue
        points_by_id[a["id"]] = points

    totals = {uid: {"earned": 0.0, "possibleGraded": 0.0, "possibleRemaining": 0.0} for uid in enrollments_map}

    if not points_by_id:
        return totals

    submissions = canvas_get_all(
        f"/api/v1/courses/{course_id}/students/submissions",
        config,
        params={"student_ids[]": "all"},
    )

    covered = set()
    for s in submissions:
        aid = s.get("assignment_id")
        uid = s.get("user_id")
        points = points_by_id.get(aid)
        if points is None or uid not in totals:
            continue
        covered.add((uid, aid))

        if s.get("excused") is True:
            continue

        score = s.get("score")
        if score is not None:
            totals[uid]["earned"] += score
            totals[uid]["possibleGraded"] += points
        else:
            totals[uid]["possibleRemaining"] += points

    # Any active student missing a submission record for a given assignment
    # entirely (rare, but possible) still counts that assignment as remaining.
    for uid in totals:
        for aid, points in points_by_id.items():
            if (uid, aid) not in covered:
                totals[uid]["possibleRemaining"] += points

    return totals


def project_grade(earned, possible_graded, possible_remaining):
    current_pct = (earned / possible_graded * 100) if possible_graded > 0 else None

    if possible_remaining <= 0:
        if possible_graded <= 0:
            return {"currentPct": None, "neededPct": None, "status": "No graded work yet"}
        passed = (earned / possible_graded) >= PASSING_THRESHOLD
        return {
            "currentPct": current_pct,
            "neededPct": None,
            "status": "PASSING (final)" if passed else "FAILED (final)",
        }

    needed_points = PASSING_THRESHOLD * (possible_graded + possible_remaining) - earned
    needed_pct = needed_points / possible_remaining * 100

    if needed_pct > 100:
        status = "CANNOT reach 70% — already mathematically failed"
    elif needed_pct <= 0:
        status = "On track — passing already secured"
    else:
        status = f"Needs {round(needed_pct, 1)}% on remaining work"

    return {"currentPct": current_pct, "neededPct": needed_pct, "status": status}


def r1(x):
    return round(x, 1) if x is not None else ""


def main():
    canvas_config = get_canvas_config()
    gc = get_sheets_client()

    courses_sheet_id = env("COURSES_SPREADSHEET_ID")
    tracker_sheet_id = env("GRADE_TRACKER_SPREADSHEET_ID")

    print("Loading course list from Courses spreadsheet...")
    courses = get_all_program_courses(gc, courses_sheet_id)
    print(f"Found {len(courses)} distinct courses across all cohort tabs.")

    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    per_course_rows = []
    students = {}

    for i, course in enumerate(courses, start=1):
        print(f"[{i}/{len(courses)}] {course['courseCode']} (Canvas ID {course['courseId']})")
        try:
            enrollments_map = get_active_enrollments_with_grades(course["courseId"], canvas_config)
            is_weighted = get_course_weighting(course["courseId"], canvas_config)
            totals = compute_course_grades(course["courseId"], canvas_config, enrollments_map)

            for uid, t in totals.items():
                enr = enrollments_map[uid]
                proj = project_grade(t["earned"], t["possibleGraded"], t["possibleRemaining"])

                per_course_rows.append([
                    enr["name"], enr["email"], uid, course["courseId"], course["courseCode"], course["courseName"],
                    str(course["endDate"]), r1(proj["currentPct"]), r1(enr["canvasCurrentScore"]),
                    r1(t["earned"]), r1(t["possibleGraded"]), r1(t["possibleRemaining"]),
                    r1(proj["neededPct"]), proj["status"], "Yes — projection is approximate" if is_weighted else "No",
                    now,
                ])

                s = students.setdefault(uid, {
                    "name": enr["name"], "email": enr["email"], "earned": 0.0, "possibleGraded": 0.0,
                    "possibleRemaining": 0.0, "failed": [], "atRisk": [], "courseCount": 0,
                })
                s["earned"] += t["earned"]
                s["possibleGraded"] += t["possibleGraded"]
                s["possibleRemaining"] += t["possibleRemaining"]
                s["courseCount"] += 1
                if "FAILED" in proj["status"] or "CANNOT reach" in proj["status"]:
                    s["failed"].append(course["courseCode"])
                elif "Needs" in proj["status"]:
                    s["atRisk"].append(course["courseCode"])
        except Exception as err:
            print(f"  ERROR on course {course['courseId']}: {err}", file=sys.stderr)
            per_course_rows.append([
                "", "", "", course["courseId"], course["courseCode"], course["courseName"], str(course["endDate"]),
                "", "", "", "", "", "", f"ERROR: {err}", "", now,
            ])

        # Be polite to Canvas's rate limiter across a large historical scan.
        time.sleep(0.2)

    print("Writing Per-Course Grades tab...")
    ss = gc.open_by_key(tracker_sheet_id)
    per_course_ws = ss.worksheet(PER_COURSE_SHEET_NAME) if _has_ws(ss, PER_COURSE_SHEET_NAME) else ss.add_worksheet(PER_COURSE_SHEET_NAME, rows=1000, cols=len(PER_COURSE_HEADERS))
    per_course_ws.clear()
    per_course_ws.update([PER_COURSE_HEADERS] + per_course_rows, value_input_option="RAW")

    print("Writing Student Program Summary tab...")
    summary_rows = []
    for uid, s in students.items():
        proj = project_grade(s["earned"], s["possibleGraded"], s["possibleRemaining"])
        summary_rows.append([
            s["name"], s["email"], uid, s["courseCount"], r1(s["earned"]), r1(s["possibleGraded"]),
            r1(s["possibleRemaining"]), r1(proj["currentPct"]), r1(proj["neededPct"]), proj["status"],
            ", ".join(s["failed"]), ", ".join(s["atRisk"]), now,
        ])

    summary_ws = ss.worksheet(SUMMARY_SHEET_NAME) if _has_ws(ss, SUMMARY_SHEET_NAME) else ss.add_worksheet(SUMMARY_SHEET_NAME, rows=1000, cols=len(SUMMARY_HEADERS))
    summary_ws.clear()
    summary_ws.update([SUMMARY_HEADERS] + summary_rows, value_input_option="RAW")

    print(f"Done. {len(courses)} courses, {len(students)} students.")


def _has_ws(ss, title):
    try:
        ss.worksheet(title)
        return True
    except gspread.exceptions.WorksheetNotFound:
        return False


if __name__ == "__main__":
    main()
