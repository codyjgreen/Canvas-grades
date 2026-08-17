# Student Grade Tracker

Pulls every student's real Canvas grades across every course in the Courses
tracking spreadsheet (past, current, and future cohort tabs), and for each
student:

- Computes their current grade, per course and rolled up across their
  whole program.
- Computes what they need to score on everything still ungraded to
  finish with at least a 70%.
- Flags courses (and, in the program-wide summary, students) where even a
  perfect score on everything remaining can't reach 70%, meaning already
  mathematically failed.

Results land in two tabs of the Student Grade Tracker Google Sheet: a
Per-Course Grades tab with one row per student per course, and a Student
Program Summary tab with one row per student, aggregated across every
course they've ever been enrolled in.

Runs as a scheduled GitHub Action (see .github/workflows/grade-tracker.yml),
once a day by default, or on demand from the Actions tab.

## One time setup

### Step 1: Create a Google Cloud service account

This lets the GitHub Action read and write your Google Sheets without a
human logging in.

1. Go to console.cloud.google.com and create a new project, or use an
   existing one. Name doesn't matter, for example "canvas grade tracker".
2. In the search bar, search for "Google Sheets API" and click Enable.
   Do the same for "Google Drive API".
3. Go to APIs and Services, then Credentials, then Create Credentials,
   then Service account. Give it any name, for example grade-tracker-bot,
   and click Done. You don't need to grant it any project level roles.
4. Click into the service account you just created, then the Keys tab,
   then Add Key, then Create new key, choose JSON, then Create. A json
   file will download. Keep it, you'll paste its contents into a GitHub
   secret in step 3 below.
5. Copy the service account's email address. It looks something like
   grade-tracker-bot at your-project dot iam.gserviceaccount.com, and it's
   on the service account's detail page, and also inside the downloaded
   JSON as client_email.

### Step 2: Share both spreadsheets with the service account

Open the Courses spreadsheet, the production tracking sheet, click Share,
paste the service account email, set it to Viewer, and send. Uncheck
"Notify people" since it's a bot, not a person.

Open the Student Grade Tracker spreadsheet at this address:
docs.google.com/spreadsheets/d/1NLAEm-S_sv53o3s0UfAn1O5qMisgMeSiKXKbg5zleWA
Click Share, paste the same service account email, set it to Editor, and
send.

### Step 3: Add GitHub repo secrets

In codyjgreen/Canvas-grades, go to Settings, then Secrets and variables,
then Actions, then New repository secret, and add each of these:

Secret name CANVAS_TOKEN, value: the same Canvas API token used by the
other grade check scripts.

Secret name CANVAS_DOMAIN, value: https colon slash slash flatiron dot
instructure dot com.

Secret name COURSES_SPREADSHEET_ID, value: the Courses tracking sheet's
ID, from its URL.

Secret name GRADE_TRACKER_SPREADSHEET_ID, value:
1NLAEm-S_sv53o3s0UfAn1O5qMisgMeSiKXKbg5zleWA

Secret name GOOGLE_SERVICE_ACCOUNT_JSON, value: the entire contents of
the JSON key file downloaded in step 1. Paste the whole thing, including
the curly braces.

### Step 4: Run it

Go to the Actions tab in the repo, then the Student Grade Tracker
workflow, then Run workflow, to trigger it manually the first time. After
that it runs automatically every day at 12:00 UTC. Edit the cron line in
the workflow file to change the schedule.

## How the grade math works

For every published, submittable, point bearing assignment in a course:

- If the student has a real score, and isn't excused, those points count
  as earned and possible so far.
- If there's no score yet, whether or not the due date has passed, the
  assignment's points go into possible remaining. It's still an open
  opportunity, not treated as a zero.

The formulas are:

current percent equals earned divided by possible so far.

needed points equals 0.70 times the sum of possible so far plus possible
remaining, minus earned.

needed percent equals needed points divided by possible remaining, times
100.

If needed percent is greater than 100 percent, the student cannot reach
70 percent even with a perfect score on everything left. If needed
percent is 0 percent or less, passing is already mathematically locked
in. Otherwise, that's the average the student needs on everything still
ungraded.

This is an unweighted, points based approximation. If a course has
weighted assignment groups turned on in Canvas, the script also pulls
Canvas's own official current score for comparison in the "Canvas
Current Grade %" column, and flags the course as weighted grading with
an approximate projection note, so the estimate isn't mistaken for
exact.

## Files

- grade_tracker.py is the script.
- requirements.txt lists the Python dependencies.
- .github/workflows/grade-tracker.yml is the scheduled and on demand
  GitHub Actions workflow.
