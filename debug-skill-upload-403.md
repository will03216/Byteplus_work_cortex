# Debug Session: skill-upload-403
- **Status**: [OPEN]
- **Issue**: Uploading or saving a skill from the UI returns a 403 and the browser shows "Network error — please try again"
- **Debug Server**: Pending
- **Log File**: .dbg/trae-debug-log-skill-upload-403.ndjson

## Reproduction Steps
1. Open the skills UI in the browser.
2. Attempt to upload or save a skill.
3. Observe the 403 response and the UI network error.

## Hypotheses & Verification
| ID | Hypothesis | Likelihood | Effort | Evidence |
|----|------------|------------|--------|----------|
| A | The `PUT` request to the skill API is missing auth credentials because the frontend `fetch()` call is not sending cookies in the current deployment context. | High | Low | Pending |
| B | The request is hitting a proxy or middleware path that blocks non-GET requests to `/api/skills/*`, producing a 403 before FastAPI handles it. | High | Medium | Pending |
| C | The backend auth check rejects the session for this endpoint only, and the frontend collapses the error into a generic network message. | Medium | Low | Pending |
| D | The UI action described as "uploading a skill" actually uses a separate campaign-creation zip endpoint with stricter validation or auth, and that endpoint is returning 403. | Medium | Medium | Pending |
| E | A CSRF-like protection, origin check, or cookie policy is rejecting browser write requests while allowing reads. | Medium | Medium | Pending |

## Log Evidence
- Live host probe:
  - `GET /health` on `http://69.5.17.49` returns `200` with `{"status":"ok","db":true}`.
  - `GET /skills/seedance` redirects to `/login` when unauthenticated.
  - `GET /api/skills/campaigns` returns `401` when unauthenticated.
  - Unauthenticated `POST /api/skills` with multipart form-data reaches FastAPI and returns JSON `401`, which shows the route exists and basic multipart POST is not universally blocked.
- Reproduction evidence from user logs:
  - During a failed cloud upload attempt, app logs show `GET /api/skills/campaigns` and `GET /api/skills/common`, but no `POST /api/skills`.
  - This means the failing upload request either never reaches FastAPI or returns a non-JSON edge response that the UI mishandles.
  - Browser network capture shows the upload request is intercepted by Zscaler and replaced with an HTML warning page before it reaches the app.
  - The warning explicitly says zip uploads to `http://69.5.17.49/api/skills` are blocked because the site is categorized as `Miscellaneous or Unknown`.
- ECS container evidence:
  - `DATABASE_URL` is set inside the running container.
  - `/app/db/migrations` contains all three SQL migration files.
  - `/app/skills/common`, `/app/skills/seedance`, and `/app/skills/kling` files are present inside the running container.
  - This further weakens the "missing deployed DB scripts" hypothesis.
  - `grep -n "api/debug/client-event" /app/ui/server.py` returns no match in the running container.
  - `grep -n "logClientDebugEvent" /app/ui/templates/app.html` returns no match in the running container.
  - Public `GET /api/debug/client-event?stage=manual-test` returns `303 /login`, consistent with the old catch-all route and inconsistent with the new API route.
  - `docker ps` / `docker inspect` show the container is running the registry `latest` image created recently.
  - Local `git status --short` shows `ui/server.py` and `ui/templates/app.html` are modified but uncommitted in the working tree.
  - Conclusion: the ECS container is current relative to the pushed image, but that image does not include the latest local debugging changes.
- Code evidence:
  - App auth guard raises `401`, not `403`.
  - On startup with `DATABASE_URL`, the app calls `create_tables()` and `seed_skill_files_from_disk()`.
  - This weakens the "DB scripts are missing" hypothesis unless container startup is failing before seed/bootstrap completes.
  - UI currently calls `res.json()` unconditionally for skill upload/save; a non-JSON `403` HTML response will throw and be shown as a fake network error.

## Verification Conclusion
- Current strongest hypotheses:
  - `B`: confirmed. An upstream corporate network security layer blocks `.zip` upload before FastAPI receives the request.
  - `C`: rejected. Backend auth is not the primary blocker for the failing upload.
  - `D`: partially confirmed earlier for debug-route testing only; deploy drift was fixed separately.
- Next evidence needed:
  - User verification after deploying the non-zip upload workaround.
  - Optional confirmation that the improved UI error handling surfaces HTML/HTTP failures more clearly.
